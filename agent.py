"""
agent.py
"""
import asyncio
import json
import random
from datetime import datetime

from config.settings import settings
from core.database import (
    init_db, log_scan, save_slot, mark_slot_booked,
    create_pending_action, get_config,
    get_all_accounts,
)
from core.scraper import VisaScraper, AppointmentSlot
from core.notifier import notifier
from core.logger import log
from bot.telegram_handler import telegram_bot, booking_decisions, set_agent


def clear_all_sessions():
    """Delete all saved session cookie files (sessions/*.json)."""
    from pathlib import Path
    session_dir = Path(__file__).parent / "sessions"
    if not session_dir.exists():
        session_dir = Path(__file__).parent / "core" / ".." / "sessions"
    try:
        sdir = Path(__file__).parent / "sessions"
        if sdir.exists():
            count = 0
            for f in sdir.glob("*.json"):
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass
            log.info(f"🗑️ Cleared {count} saved session(s)")
        else:
            log.info("No sessions folder to clear")
    except Exception as e:
        log.warning(f"Could not clear sessions: {e}")


class VisaAgent:
    def __init__(self):
        self._stop_event      = asyncio.Event()   # bot-level shutdown
        self._force_scan      = asyncio.Event()   # wake the sleep loop early (/scan)
        self._abort_scan      = asyncio.Event()   # signal current scan to stop (/stop)
        self._scanning        = False
        self._current_scraper: VisaScraper | None = None
        self._scrapers: list = []   # all scrapers running in parallel (realtime)
        self._scan_minutes    = None   # None = every minute; set = only these minutes each hour
        self.scan_count       = 0
        self._last_session_clear = 0.0   # timestamp of last 30-min session wipe

    # ------------------------------------------------------------------ #
    #  Load config from DB
    # ------------------------------------------------------------------ #

    async def _load_config(self) -> bool:
        portal_url = await get_config("portal_url")
        if not portal_url:
            log.warning("No config in DB — waiting for /setup")
            return False

        settings.portal_url       = portal_url
        settings.visa_type        = await get_config("visa_type")        or "B1/B2"
        settings.embassy_location = await get_config("embassy_location") or "Dhaka"
        settings.date_from        = await get_config("date_from")        or ""
        settings.date_to          = await get_config("date_to")          or ""
        settings.auto_book        = (await get_config("auto_book"))      == "1"

        interval = await get_config("scan_interval")
        settings.scan_interval_minutes = int(interval) if interval else 10

        log.info(
            f"Config: {portal_url} | {settings.visa_type} | "
            f"{settings.date_from} → {settings.date_to}"
        )
        return True

    async def _get_accounts(self) -> list[dict]:
        """New accounts table first, fall back to old students JSON."""
        accounts = await get_all_accounts()
        if accounts:
            return accounts

        raw = await get_config("students")
        if raw:
            try:
                students = json.loads(raw)
                return [
                    {
                        "id":               i + 1,
                        "username":         s.get("email", ""),
                        "password":         s.get("password", ""),
                        "security_answers": {},
                    }
                    for i, s in enumerate(students)
                ]
            except Exception:
                pass
        return []

    # ------------------------------------------------------------------ #
    #  Public control methods (called by Telegram commands)
    # ------------------------------------------------------------------ #

    def trigger_scan_now(self):
        """Wake the sleeping scan loop immediately (/scan, /resume)."""
        self._force_scan.set()

    async def abort_current_scan(self):
        """
        Stop the in-progress scan as fast as possible:
        - Sets the abort flag so the account loop exits between iterations
        - Tells the scraper to abort all internal loops immediately
        - Closes any open browser immediately
        """
        self._abort_scan.set()
        # Abort + close ALL parallel scrapers (realtime mode)
        for sc in list(getattr(self, "_scrapers", [])):
            try:
                sc.abort()
            except Exception:
                pass
            try:
                await sc.stop()
            except Exception:
                pass
        self._scrapers = []
        if self._current_scraper:
            try:
                self._current_scraper.abort()
            except Exception:
                pass
            try:
                await self._current_scraper.stop()
            except Exception:
                pass
            self._current_scraper = None
        self._scanning = False
        log.info("Scan aborted by command")
    # ------------------------------------------------------------------ #
    #  REAL-TIME single-account mode
    # ------------------------------------------------------------------ #

    async def _seconds_until_next_minute(self, lead_seconds: float = 6.0) -> float:
        """Sleep length so we wake `lead_seconds` before the next :00 boundary
        (so the calendar read lands right on the new minute)."""
        now = datetime.now()
        secs_into_minute = now.second + now.microsecond / 1_000_000.0
        until_next = 60.0 - secs_into_minute
        sleep_for = until_next - lead_seconds
        if sleep_for < 0:
            sleep_for += 60.0
        return sleep_for

    async def _seconds_until_next_scan(self, lead_seconds: float = 3.0) -> float:
        """If specific scan minutes are set (e.g. {10,11,12}), wait until the
        next time the clock's minute is one of them — lead_seconds before its
        :00. Otherwise fall back to every-minute mode."""
        from datetime import timedelta
        target_minutes = self._scan_minutes
        now = datetime.now()

        if not target_minutes:
            return await self._seconds_until_next_minute(lead_seconds)

        base = now.replace(second=0, microsecond=0)
        for i in range(1, 24 * 60 + 1):   # search up to 24h ahead
            candidate = base + timedelta(minutes=i)
            if candidate.minute in target_minutes:
                target = candidate - timedelta(seconds=lead_seconds)
                delta = (target - now).total_seconds()
                if delta < 0:
                    continue
                return delta
        return 60.0

    async def _abortable_sleep(self, seconds: float):
        """Sleep that wakes immediately on /stop."""
        slept = 0.0
        step = 0.25
        while slept < seconds:
            if self._abort_scan.is_set() or self._stop_event.is_set():
                return
            await asyncio.sleep(min(step, seconds - slept))
            slept += step

    # ------------------------------------------------------------------ #
    #  One account's persistent real-time worker (runs in parallel)
    # ------------------------------------------------------------------ #

    async def _realtime_account_worker(self, account: dict, start_delay: float = 0):
        """Keeps ONE account's browser open and, every minute, reloads + checks
        the calendar. Re-logs in on breakage. Books + stops on slot found.
        Many of these run together via asyncio.gather — one per account.
        `start_delay` only staggers the FIRST browser launch so windows don't
        all open at once; the per-minute :00 scan timing is unaffected."""
        name = account.get("username", "Account")
        acc_date_from = account.get("date_from") or settings.date_from
        acc_date_to   = account.get("date_to")   or settings.date_to

        # Stagger only the initial browser open (not the scan timing)
        if start_delay > 0:
            await self._abortable_sleep(start_delay)
            if self._abort_scan.is_set() or self._stop_event.is_set():
                return False

        scraper = None
        booked = False

        try:
            while not self._abort_scan.is_set() and not self._stop_event.is_set():
                if (await get_config("agent_paused")) == "1":
                    log.info(f"[{name}] Paused — worker exiting")
                    break

                # (Re)establish this account's calendar page if needed
                if scraper is None:
                    scraper = VisaScraper(
                        account_name=name,
                        account_security_answers=account.get("security_answers", {}),
                        account_date_from=acc_date_from,
                        account_date_to=acc_date_to,
                    )
                    self._scrapers.append(scraper)   # track for /stop
                    try:
                        await scraper.start()
                        scraper._login_username = account["username"]
                        scraper._login_password = account["password"]
                    except Exception as e:
                        log.error(f"[{name}] Browser start failed: {e}")
                        try:
                            await scraper.stop()
                        except Exception:
                            pass
                        try:
                            self._scrapers.remove(scraper)
                        except ValueError:
                            pass
                        scraper = None
                        await self._abortable_sleep(10)
                        continue

                    ready = False
                    try:
                        ready = await scraper.prepare_calendar_page()
                    except Exception as e:
                        from core.scraper import ScanAborted
                        if isinstance(e, ScanAborted):
                            break
                        log.error(f"[{name}] prepare_calendar_page raised: {e}")

                    if not ready:
                        # Login worked but calendar/schedule didn't load yet.
                        # DON'T close the browser — keep it open and let the
                        # per-minute reload loop keep trying on the same page.
                        # Only a truly dead session closes+re-logs in (below).
                        log.info(f"[{name}] Calendar not ready yet — keeping browser open, retrying each minute")
                        await notifier.send_telegram(
                            f"📭 <b>{name}</b> — calendar not ready yet, browser staying open, retrying every minute."
                        )
                    else:
                        await notifier.send_telegram(
                            f"✅ <b>{name}</b> logged in. Watching calendar every minute."
                        )

                # Sleep until ~3s before the next scan time (every minute, OR
                # only the custom minutes if /setminutes was used)
                sleep_for = await self._seconds_until_next_scan(lead_seconds=3.0)
                await self._abortable_sleep(sleep_for)
                if self._abort_scan.is_set() or self._stop_event.is_set():
                    break

                # Reload + read calendar (lands ~on :00)
                try:
                    status, slots = await scraper.reload_and_read_calendar()
                except Exception as e:
                    from core.scraper import ScanAborted
                    if isinstance(e, ScanAborted):
                        break
                    log.error(f"[{name}] reload_and_read_calendar raised: {e}")
                    status, slots = "need_relogin", []

                now_str = datetime.now().strftime("%H:%M:%S")

                if status == "need_relogin":
                    # ONLY here — session actually died (login form seen).
                    log.info(f"[{name}] Session died — closing browser, re-login")
                    await notifier.send_telegram(
                        f"♻️ Session ended for <b>{name}</b> — logging in again."
                    )
                    try:
                        await scraper.stop()
                    except Exception:
                        pass
                    try:
                        self._scrapers.remove(scraper)
                    except ValueError:
                        pass
                    scraper = None
                    continue

                if status == "no_calendar":
                    # Calendar/schedule didn't load this minute, but session is
                    # alive. Browser STAYS OPEN — report and retry next minute.
                    log.info(f"[{name}] [{now_str}] Calendar not available — browser stays open, retrying")
                    await notifier.send_telegram(
                        f"📭 [{now_str}] Calendar not available — <b>{name}</b> (browser open, will retry)"
                    )
                    continue

                await log_scan("found" if slots else "empty", slots_found=len(slots))

                if not slots:
                    log.info(f"[{name}] [{now_str}] No slots this minute")
                    await notifier.send_telegram(
                        f"🔍 [{now_str}] No slots — <b>{name}</b>"
                    )
                    continue

                # Slot found!
                log.info(f"[{name}] [{now_str}] 🎉 {len(slots)} slot(s) found")
                await notifier.send_telegram(
                    f"🎯 [{now_str}] {len(slots)} slot(s) found — <b>{name}</b>!"
                )
                saved_ids = []
                for slot in slots:
                    sid = await save_slot(slot.slot_date, slot.slot_time,
                                          slot.location, slot.slots_available)
                    saved_ids.append(sid)

                if settings.auto_book:
                    await self._book_slot(scraper, slots[0], account, saved_ids[0])
                    booked = True
                    break
                else:
                    await self._ask_and_book(scraper, slots, account, saved_ids)
                    booked = True
                    break

        except Exception as e:
            from core.scraper import ScanAborted
            if not isinstance(e, ScanAborted):
                log.error(f"[{name}] worker error: {e}", exc_info=True)
        finally:
            if scraper is not None:
                try:
                    await scraper.stop()
                except Exception:
                    pass
                try:
                    self._scrapers.remove(scraper)
                except ValueError:
                    pass
        return booked

    # ------------------------------------------------------------------ #
    #  Real-time loop — launches one worker per account, in parallel
    # ------------------------------------------------------------------ #

    async def run_realtime_loop(self):
        """Launch a persistent real-time worker for EVERY configured account,
        all running in parallel. Each keeps its own browser open and checks the
        calendar every minute on :00. Number of accounts = number of browsers,
        controlled entirely from Telegram (/setup, /addaccount, /accounts)."""
        if self._scanning:
            log.warning("Realtime loop already running — skipping")
            return

        if (await get_config("agent_paused")) == "1":
            log.info("Agent paused — not starting realtime loop")
            return

        if not await self._load_config():
            await notifier.send_telegram("⚠️ <b>Bot not configured.</b> Send /setup first.")
            return

        accounts = await self._get_accounts()
        if not accounts:
            await notifier.send_telegram("⚠️ <b>No accounts.</b> Send /setup or /addaccount.")
            return

        self._scanning = True
        self._abort_scan.clear()
        self._scrapers = []   # all parallel scrapers, for /stop

        # Load custom scan minutes from DB (None = every minute)
        raw_minutes = await get_config("scan_minutes")
        if raw_minutes:
            try:
                self._scan_minutes = {int(x) for x in raw_minutes.split(",") if x.strip().isdigit()}
                if not self._scan_minutes:
                    self._scan_minutes = None
            except Exception:
                self._scan_minutes = None
        else:
            self._scan_minutes = None
        if self._scan_minutes:
            log.info(f"Scan minutes: {sorted(self._scan_minutes)} (each hour)")
        else:
            log.info("Scan mode: every minute")

        await notifier.send_telegram(
            f"⏱ <b>Real-time mode started</b>\n"
            f"👥 {len(accounts)} account(s) in parallel\n"
            f"🔁 Each checks every minute, right on :00 seconds.\n"
            f"🌐 One browser per account stays open."
        )

        try:
            # Start each worker a few seconds apart so the visible Chrome
            # windows don't launch at the exact same instant and collide.
            # This only staggers the BROWSER OPENING — every worker still
            # scans together on the :00 second once it's running.
            tasks = []
            for i, account in enumerate(accounts):
                tasks.append(self._realtime_account_worker(account, start_delay=i * 8))
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._scanning = False
            for sc in list(self._scrapers):
                try:
                    await sc.stop()
                except Exception:
                    pass
            self._scrapers = []
            self._current_scraper = None
            await notifier.send_telegram("🛑 Real-time mode stopped.")
    # ------------------------------------------------------------------ #
    #  Main scan cycle
    # ------------------------------------------------------------------ #

    async def run_scan_cycle(self):
        if self._scanning:
            log.warning("Scan already running — skipping overlapping call")
            return

        paused = await get_config("agent_paused")
        if paused == "1":
            log.info("Agent paused — skipping scan")
            self._abort_scan.clear()
            return

        config_ok = await self._load_config()
        if not config_ok:
            await notifier.send_telegram(
                "⚠️ <b>Bot not configured yet!</b>\n"
                "Send /setup to configure accounts and start scanning."
            )
            return

        accounts = await self._get_accounts()
        if not accounts:
            await notifier.send_telegram(
                "⚠️ <b>No accounts found!</b>\n"
                "Send /setup or /addaccount to add accounts."
            )
            return

        # ── Everything after this line is inside try/finally ── #
        self._scanning = True
        self._abort_scan.clear()

        try:
            self.scan_count += 1
            now = datetime.now().strftime("%H:%M:%S")
            log.info(f"━━━ Scan #{self.scan_count} | {len(accounts)} account(s) | {now} ━━━")

            await notifier.send_telegram(
                f"🔍 <b>Scan #{self.scan_count} started</b>\n"
                f"👥 Checking {len(accounts)} account(s)\n"
                f"📅 Each account uses its own date range\n"
                f"⏰ Time: {now}"
            )

            total_slots_found = 0

            for idx, account in enumerate(accounts, 1):
                # Check abort signal between accounts
                if self._abort_scan.is_set():
                    log.info("Scan aborted — stopping account loop")
                    await notifier.send_telegram("🛑 Scan stopped. Browser closed.")
                    break

                name = account.get("username", f"Account {idx}")
                log.info(f"Scanning for {name} ({idx}/{len(accounts)})")

                # Per-account date window (falls back to global settings if blank)
                acc_date_from = account.get("date_from") or settings.date_from
                acc_date_to   = account.get("date_to")   or settings.date_to

                scraper = VisaScraper(
                    account_name=name,
                    account_security_answers=account.get("security_answers", {}),
                    account_date_from=acc_date_from,
                    account_date_to=acc_date_to,
                )
                self._current_scraper = scraper

                try:
                    if self._abort_scan.is_set():
                        break
                    await scraper.start()
                    settings.portal_username = account["username"]
                    settings.portal_password = account["password"]

                    if self._abort_scan.is_set():
                        break
                    login_ok = await scraper.login()
                    if not login_ok:
                        log.error(f"Login failed: {name}")
                        await notifier.send_telegram(
                            f"🚫 <b>Login failed</b> for <b>{name}</b>\n"
                            "Check credentials with /addaccount."
                        )
                        await log_scan("error", message=f"Login failed: {name}")
                        continue

                    if self._abort_scan.is_set():
                        break

                    log.info(f"Login OK for {name} — scanning slots...")
                    slots = await scraper.scan_appointments()

                    if not slots:
                        log.info(f"No slots found for {name}")
                        await notifier.send_telegram(
                            f"❌ No slots found for <b>{name}</b>\n"
                            f"📅 Checked: {acc_date_from} → {acc_date_to}\n"
                            f"Next scan in <b>{settings.scan_interval_minutes} min</b>"
                        )
                        continue

                    # Slots found!
                    total_slots_found += len(slots)
                    log.info(f"🎉 {len(slots)} slot(s) found for {name}!")

                    saved_ids = []
                    for slot in slots:
                        sid = await save_slot(
                            slot.slot_date, slot.slot_time,
                            slot.location, slot.slots_available
                        )
                        saved_ids.append(sid)

                    if settings.auto_book:
                        await self._book_slot(scraper, slots[0], account, saved_ids[0])
                    else:
                        await self._ask_and_book(scraper, slots, account, saved_ids)

                except Exception as e:
                    # ScanAborted means user pressed /stop — exit quietly
                    from core.scraper import ScanAborted
                    if isinstance(e, ScanAborted):
                        log.info(f"Scan aborted for {name}")
                        self._current_scraper = None
                        try:
                            await scraper.stop()
                        except Exception:
                            pass
                        break
                    log.error(f"Error for {name}: {e}", exc_info=True)
                    await notifier.send_telegram(
                        f"⚠️ <b>Error scanning {name}</b>\n"
                        f"<code>{str(e)[:200]}</code>"
                    )
                finally:
                    self._current_scraper = None
                    try:
                        await scraper.stop()
                    except Exception:
                        pass

                if not self._abort_scan.is_set():
                    await asyncio.sleep(random.uniform(1, 2))

            if total_slots_found > 0:
                await log_scan("found", slots_found=total_slots_found)
            else:
                await log_scan("empty", slots_found=0)

        finally:
            # ALWAYS reset the lock — no matter what happens above
            self._scanning = False
            self._current_scraper = None

    # ------------------------------------------------------------------ #
    #  Ask user and book
    # ------------------------------------------------------------------ #

    async def _ask_and_book(
        self,
        scraper: VisaScraper,
        slots: list,
        account: dict,
        saved_ids: list
    ):
        name = account.get("username", "Account")

        action_id = await create_pending_action(
            "confirm_booking",
            {
                "account_username": name,
                "slot_date":        slots[0].slot_date,
                "slot_time":        slots[0].slot_time,
                "location":         slots[0].location,
            }
        )

        lines = [f"🎯 <b>SLOT FOUND for {name}!</b>\n"]
        for i, slot in enumerate(slots[:5], 1):
            lines.append(
                f"  {i}. 📅 <b>{slot.slot_date}</b>  ⏰ {slot.slot_time}\n"
                f"     📍 {slot.location}  ({slot.slots_available} seat(s))"
            )
        lines.append(f"\n👉 Reply <b>YES {action_id}</b> to book slot #1")
        lines.append(f"👉 Reply <b>NO {action_id}</b> to skip")
        lines.append(f"\n⏰ Expires in <b>10 minutes</b>")

        await notifier.send_urgent_telegram("\n".join(lines))
        log.info(f"Waiting for reply on action #{action_id} (10 min timeout)")

        loop = asyncio.get_event_loop()
        fut  = loop.create_future()
        booking_decisions[action_id] = fut

        try:
            confirmed = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            log.info(f"Timeout on action #{action_id}")
            await notifier.send_telegram(
                f"⏰ <b>Timeout!</b> No reply for <b>{name}</b>\n"
                "Slot skipped. Bot continues scanning."
            )
            return
        finally:
            booking_decisions.pop(action_id, None)

        if confirmed:
            await self._book_slot(scraper, slots[0], account,
                                  saved_ids[0] if saved_ids else None)
        else:
            log.info(f"User skipped booking for {name}")
            await notifier.send_telegram(
                f"⏭ Slot skipped for <b>{name}</b>. Continuing to scan..."
            )

    async def _book_slot(
        self,
        scraper: VisaScraper,
        slot: AppointmentSlot,
        account: dict,
        slot_db_id: int | None = None
    ):
        name = account.get("username", "Account")
        log.info(f"Booking for {name}: {slot.slot_date} {slot.slot_time}")

        ref = None
        try:
            ref = await scraper.book_appointment(slot)
        except Exception as e:
            log.error(f"Booking exception: {e}")

        screenshot = None
        try:
            screenshot = await scraper.screenshot("booking_final")
        except Exception:
            pass

        if ref:
            if slot_db_id:
                await mark_slot_booked(slot_db_id, ref)
            await notifier.send_urgent_telegram(
                f"✅ <b>BOOKING CONFIRMED!</b>\n"
                f"👤 Account: <b>{name}</b>\n"
                f"📅 Date: <b>{slot.slot_date}</b>\n"
                f"⏰ Time: <b>{slot.slot_time}</b>\n"
                f"📍 Location: <b>{slot.location}</b>\n"
                f"🔖 Reference: <b>{ref}</b>"
            )
            if screenshot:
                await notifier.send_telegram_photo(screenshot, "📸 Confirmation screenshot")
            log.info(f"✅ Booking done for {name} — Ref: {ref}")
        else:
            await notifier.send_telegram(
                f"❌ <b>Booking FAILED</b> for <b>{name}</b>\n"
                "Please book manually. Bot continues scanning."
            )

    # ------------------------------------------------------------------ #
    #  Scan loop — interval starts AFTER all accounts finish
    # ------------------------------------------------------------------ #

    async def _scan_loop(self):
        while not self._stop_event.is_set():
            self._force_scan.clear()

            # Check paused BEFORE scanning
            paused = await get_config("agent_paused")
            if paused == "1":
                log.info("Agent paused — waiting for /resume")
                # Wait until /resume triggers force_scan (or bot stops)
                try:
                    await asyncio.wait_for(self._force_scan.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
                continue   # re-check paused at top of loop

            # Every 30 minutes, wipe saved sessions so stale cookies don't
            # cause Cloudflare/login problems.
            now_ts = asyncio.get_event_loop().time()
            if now_ts - self._last_session_clear >= 1800:   # 1800s = 30 min
                clear_all_sessions()
                self._last_session_clear = now_ts

            # REAL-TIME MODE: this runs its own internal every-minute loop and
            # only returns when a slot is booked, /stop is pressed, or it's
            # paused. So we don't need the old interval-based wait here.
            await self.run_realtime_loop()

            # If the realtime loop returned (booked/stopped/paused), wait for a
            # /resume or /scan before starting it again.
            log.info("Realtime loop ended — waiting for /resume or /scan.")
            try:
                await asyncio.wait_for(self._force_scan.wait(), timeout=3600)
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ #
    #  Start / Stop
    # ------------------------------------------------------------------ #

    async def start(self):
        log.info("=" * 55)
        log.info("   VISA APPOINTMENT BOT — Starting")
        log.info("=" * 55)

        if not settings.telegram_bot_token:
            log.error("TELEGRAM_BOT_TOKEN missing in .env!")
            return

        await init_db()
        log.info("Database ready ✓")

        set_agent(self)
        await telegram_bot.start()

        log.info("Testing Telegram connection...")
        ok = await notifier.test_connection()
        if not ok:
            log.error(
                "❌ Cannot send Telegram messages!\n"
                "   Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env\n"
                "   Make sure you sent /start to your bot first!"
            )

        existing = await get_config("portal_url")
        if existing:
            log.info("Existing config found — resuming")
            await notifier.send_telegram(
                "♻️ <b>Bot restarted!</b>\n"
                "Previous config found — resuming scans.\n\n"
                "Commands: /scan /stop /resume /status /accounts"
            )
            await self._load_config()
        else:
            log.info("No config — waiting for /setup on Telegram")
            await notifier.send_telegram(
                "🚀 <b>Visa Bot is online!</b>\n\n"
                "Send /setup to configure accounts and start scanning.\n"
                "Use /addaccount to add accounts one by one."
            )

        self._scan_task = asyncio.create_task(self._scan_loop())

        log.info("Bot running. Press Ctrl+C to stop.")
        await self._stop_event.wait()

    async def stop(self):
        log.info("Stopping...")
        self._stop_event.set()
        self._force_scan.set()    # wake the sleep so the loop exits cleanly
        await self.abort_current_scan()
        if hasattr(self, "_scan_task"):
            self._scan_task.cancel()
            try:
                await self._scan_task
            except (asyncio.CancelledError, Exception):
                pass
        await telegram_bot.stop()
        await notifier.send_telegram("🛑 <b>Visa Bot stopped.</b>")


async def main():
    agent = VisaAgent()
    try:
        await agent.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await agent.stop()
        log.info("Done.")


if __name__ == "__main__":
    asyncio.run(main())
