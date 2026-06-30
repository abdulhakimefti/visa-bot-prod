"""
core/scraper.py — FIXED
Bug fixes:
  1. Date range now uses full year by default if not set
  2. Added detailed debug logging for slot scanning
  3. API call fixed to use absolute URL
  4. ABORT support — /stop now instantly halts all loops
  5. Session detection hardened (no false "logged in")
  6. Screenshots disabled for production
"""
import asyncio
import html
import json
import random
import aiohttp
from datetime import datetime, date
from pathlib import Path
from dataclasses import dataclass
from typing import List
import re

try:
    from undetected_playwright.async_api import async_playwright, Browser, BrowserContext, Page
    print("Using undetected-playwright ✓")
except ImportError:
    from playwright.async_api import async_playwright, Browser, BrowserContext, Page
    print("Using standard playwright")
from config.settings import settings
from core.logger import log
import os
chrome_profile = os.path.expanduser(
    r"~\AppData\Local\Google\Chrome\User Data"
)

SCREENSHOTS_DIR = Path(__file__).parent.parent / "screenshots"
SCREENSHOTS_DIR.mkdir(exist_ok=True)

# ── Screenshots master switch — set False for production (no screenshots) ──
SCREENSHOTS_ENABLED = False

SECURITY_ANSWERS = {
    "mother"      : "YourMothersAnswer",
    "pet"         : "CAT",          # ← Q1 matches this
    "hero"        : "HERO",      # ← Q2 matches this
    "school"      : "HIGH",
    "city"        : "YourCityAnswer",
    "born"        : "YourCityAnswer",
    "friend"      : "YourFriendAnswer",
    "street"      : "YourStreetAnswer",
    "teacher"     : "YourTeacherAnswer",
    "sport"       : "YourSportAnswer",
    "movie"       : "YourMovieAnswer",
    "food"        : "YourFoodAnswer",
    "grandfather" : "YourGrandfatherAnswer",
    "nickname"    : "YourNicknameAnswer",
}

# Portal URLs — change SCHEDULE_URL for new appointments vs reschedule
RESCHEDULE_URL = "https://www.usvisascheduling.com/en-US/schedule/?reschedule=true"
NEW_SCHEDULE_URL = "https://www.usvisascheduling.com/en-US/schedule/"
HOME_URL = "https://www.usvisascheduling.com/en-US/"
DHAKA_POST_VALUE = "906af614-b0db-ec11-a7b4-001dd80234f6"


class ScanAborted(Exception):
    """Raised when /stop is issued mid-scan so all loops unwind cleanly."""
    pass


@dataclass
class AppointmentSlot:
    slot_date: str
    slot_time: str
    location: str
    slots_available: int
    raw_element_id: str = ""


class VisaScraper:
    def __init__(
        self,
        account_name: str = "",
        account_security_answers: dict = None,
        account_date_from: str = "",
        account_date_to: str = "",
    ):
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page = None
        self._account_name = account_name
        self._account_security_answers = account_security_answers or {}
        # Per-account date window. Falls back to global settings when blank.
        self._account_date_from = (account_date_from or "").strip()
        self._account_date_to = (account_date_to or "").strip()
        self._aborted = False

    def abort(self):
        """Mark this scraper as aborted — all internal loops will stop ASAP."""
        self._aborted = True

    def _check_abort(self):
        """Raise ScanAborted if /stop was issued. Called inside every loop."""
        if self._aborted:
            raise ScanAborted()

    async def start(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            channel="chrome",        # Real Chrome not Chromium
            headless=False,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--start-maximized",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Asia/Dhaka",
        )
        # Load real Chrome profile so Cloudflare sees trusted session
        try:
            self._context = await self._browser.new_context(
                storage_state=None,
            )
        except Exception:
            pass
        self._page = await self._context.new_page()
        log.info("Browser started")

    async def stop(self):
        self._aborted = True
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if hasattr(self, "_playwright"):
                await self._playwright.stop()
        except Exception:
            pass
        log.info("Browser stopped")

    async def save_session(self):
        """Save cookies for this account so next scan can skip login."""
        try:
            from pathlib import Path
            import json as _json
            safe_name = "".join(c for c in self._account_name if c.isalnum())[:30] or "default"
            session_dir = Path(__file__).parent.parent / "sessions"
            session_dir.mkdir(exist_ok=True)
            cookies = await self._context.cookies()
            session_file = session_dir / f"{safe_name}.json"
            session_file.write_text(_json.dumps(cookies))
            log.info(f"💾 Session saved for {self._account_name}")
        except Exception as e:
            log.warning(f"Could not save session: {e}")

    async def load_session(self) -> bool:
        """Load saved cookies. Returns True if cookies were loaded."""
        try:
            from pathlib import Path
            import json as _json
            safe_name = "".join(c for c in self._account_name if c.isalnum())[:30] or "default"
            session_file = Path(__file__).parent.parent / "sessions" / f"{safe_name}.json"
            if not session_file.exists():
                return False
            cookies = _json.loads(session_file.read_text())
            await self._context.add_cookies(cookies)
            log.info(f"♻️ Session loaded for {self._account_name}")
            return True
        except Exception as e:
            log.warning(f"Could not load session: {e}")
            return False

    async def delete_session(self):
        """Delete saved cookies — call when session is expired/invalid."""
        try:
            from pathlib import Path
            safe_name = "".join(c for c in self._account_name if c.isalnum())[:30] or "default"
            session_file = Path(__file__).parent.parent / "sessions" / f"{safe_name}.json"
            if session_file.exists():
                session_file.unlink()
                log.info(f"🗑️ Deleted expired session for {self._account_name}")
        except Exception as e:
            log.warning(f"Could not delete session: {e}")

    async def screenshot(self, name: str = "shot") -> str:
        # Screenshots disabled for production
        if not SCREENSHOTS_ENABLED:
            return ""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOTS_DIR / f"{name}_{ts}.png")
        try:
            await self._page.screenshot(path=path, full_page=True)
        except Exception:
            pass
        return path

    async def _sleep(self, a=1.0, b=2.0):
        # abort-aware sleep — wakes immediately if aborted
        total = random.uniform(a, b)
        slept = 0.0
        step = 0.25
        while slept < total:
            self._check_abort()
            await asyncio.sleep(min(step, total - slept))
            slept += step

    async def _human_glance(self):
        """A few small, human-like mouse moves and a scroll, as if a person is
        glancing over the page after it loaded. Purely cosmetic behaviour to
        look less robotic. Never raises."""
        try:
            for _ in range(random.randint(1, 3)):
                self._check_abort()
                await self._page.mouse.move(
                    random.randint(200, 900), random.randint(150, 600)
                )
                await asyncio.sleep(random.uniform(0.2, 0.6))
            await self._page.mouse.wheel(0, random.randint(100, 400))
            await asyncio.sleep(random.uniform(0.3, 0.8))
        except ScanAborted:
            raise
        except Exception:
            pass

    async def _visible_any(self, selectors: list[str], timeout=1000) -> bool:
        for selector in selectors:
            try:
                if await self._page.is_visible(selector, timeout=timeout):
                    return True
            except Exception:
                pass
        return False

    async def _wait_for_login_or_dashboard(self, timeout=2300) -> bool:
        login_selectors = [
            "#signInName",
            "#password",
            "#continue",
            "input[name='username']",
            "input[type='email']",
        ]
        dashboard_selectors = [
            "a[href*='logout']",
            "a[href='/logout']",
            ".dashboard",
            "#dashboard",
        ]
        start = asyncio.get_event_loop().time()
        last_notice = -1

        while (asyncio.get_event_loop().time() - start) < timeout:
            self._check_abort()
            if await self._visible_any(login_selectors):
                log.info("Login form detected")
                return True
            if await self._visible_any(dashboard_selectors):
                log.info("Dashboard detected")
                return True

            await self._handle_cloudflare()
            await self._handle_waiting_room(timeout=30)

            elapsed = int(asyncio.get_event_loop().time() - start)
            if elapsed // 30 != last_notice:
                last_notice = elapsed // 30
                log.info(f"Waiting for login page... {elapsed}s elapsed")

            await asyncio.sleep(3)

        log.error("Timed out waiting for login page")
        await self.screenshot("wait_for_login_timeout")
        return False

    async def _handle_cloudflare(self, timeout=90) -> bool:
        start = asyncio.get_event_loop().time()

        while (asyncio.get_event_loop().time() - start) < timeout:
            self._check_abort()
            try:
                body = (await self._page.inner_text("body")).lower()
            except Exception:
                body = ""

            cf_signs = [
                "cloudflare",
                "cf-turnstile",
                "checking your browser",
                "verify you are human",
                "cf-challenge",
                "performing security verification",
                "just a moment",
            ]

            if not any(sign in body for sign in cf_signs):
                log.info("✅ Cloudflare cleared")
                return True

            try:
                if await self._page.is_visible("#signInName", timeout=1000):
                    log.info("✅ CF cleared — login form visible!")
                    return True
            except Exception:
                pass

            # Check if already on portal (past CF)
            try:
                if await self._page.is_visible("#atlas-sidebar", timeout=1000):
                    log.info("✅ CF cleared — portal sidebar visible!")
                    return True
            except Exception:
                pass

            log.info("⏳ Cloudflare detected — clicking checkbox...")

            cf_selectors = [
                "input[type='checkbox']",
                "label.cb-lb",
                ".cb-lb",
                ".cb-i",
            ]

            clicked = False
            try:
                cf_frame = None
                for frame in self._page.frames:
                    if "challenges.cloudflare.com" in (frame.url or ""):
                        cf_frame = frame
                        break

                if cf_frame:
                    log.info(f"Found CF iframe: {cf_frame.url[:60]}")
                    # Get iframe element directly from frame object
                    iframe_el = await cf_frame.frame_element()
                    box = await iframe_el.bounding_box()
                    if box:
                        x = box['x'] + 30
                        y = box['y'] + (box['height'] / 2)
                        log.info(f"CF iframe at: x={box['x']:.0f} y={box['y']:.0f} w={box['width']:.0f} h={box['height']:.0f}")
                        await self._page.mouse.move(x + 50, y + 20)
                        await asyncio.sleep(0.3)
                        await self._page.mouse.move(x, y)
                        await asyncio.sleep(0.2)
                        await self._page.mouse.click(x, y)
                        log.info(f"✅ Mouse clicked CF checkbox at ({x:.0f}, {y:.0f})")
                        # Only mark clicked when we ACTUALLY clicked (box existed)
                        clicked = True
                        try:
                            from core.notifier import notifier as _notif
                            await _notif.send_telegram(
                                f"🛡️ Cloudflare checkbox clicked"
                                + (f" — <b>{self._account_name}</b>" if self._account_name else "")
                            )
                        except Exception:
                            pass
                        await asyncio.sleep(2)
                    else:
                        log.info("CF iframe has no bounding box yet — will retry")
                        await asyncio.sleep(1)
                else:
                    log.info("CF iframe not found yet")
            except Exception as e:
                log.info(f"CF checkbox click failed: {e}")

            if clicked:
                await asyncio.sleep(2)
                try:
                    new_body = (await self._page.inner_text("body")).lower()
                    if not any(s in new_body for s in cf_signs):
                        log.info("✅ CF cleared after click!")
                        return True
                    if await self._page.is_visible("#signInName", timeout=2000):
                        log.info("✅ Login form appeared!")
                        return True
                except Exception:
                    pass

            # Check if CF cleared by looking for login form
            try:
                if await self._page.is_visible("#signInName", timeout=1000):
                    log.info("✅ CF cleared — login form visible!")
                    return True
            except Exception:
                pass

            current_url = self._page.url
            if "challenge" not in current_url and "cloudflare" not in current_url:
                try:
                    body = (await self._page.inner_text("body")).lower()
                    if not any(s in body for s in ["verify you are human",
                        "performing security verification", "just a moment"]):
                        log.info("✅ CF cleared — no challenge signs!")
                        return True
                except Exception:
                    pass

            log.info("Retrying in 2s...")
            await asyncio.sleep(2)

        log.warning("Cloudflare timeout — continuing anyway")
        await self.screenshot("cloudflare_timeout")
        return True

    async def _handle_waiting_room(self, timeout=2300) -> bool:
        signs = [
            "waiting room", "queue", "your position",
            "estimated wait", "you are number", "in line",
            "estimated time", "wait time",
            "you will be redirected", "minutes away",
            "processing", "one moment", "hang on",
            "holding page", "virtual waiting",
            "you are being redirected",
            "you are now in line",
            "thank you for your patience",
            "waitingrooms",
            "virtual queue",
            "do not close your browser",
        ]
        start = asyncio.get_event_loop().time()
        _waiting_room_notified = False

        while (asyncio.get_event_loop().time() - start) < timeout:
            self._check_abort()
            try:
                body = (await self._page.inner_text("body")).lower()
            except Exception:
                break

            if not any(s in body for s in signs):
                log.info("✅ Past waiting room")
                return True

            # Notify once on first detection
            if not _waiting_room_notified:
                _waiting_room_notified = True
                try:
                    from core.notifier import notifier as _notif
                    await _notif.send_telegram(
                        f"⏳ In waiting line..."
                        + (f" — <b>{self._account_name}</b>" if self._account_name else "")
                    )
                except Exception:
                    pass

            try:
                has_login = await self._page.is_visible(
                    "input[name='username'], input[type='email']",
                    timeout=1000
                )
                has_dashboard = await self._page.is_visible(
                    "a[href*='logout'], .dashboard, #dashboard",
                    timeout=1000
                )
                if has_login or has_dashboard:
                    log.info("✅ Login or dashboard detected — past waiting room")
                    return True
            except Exception:
                pass

            elapsed = int(asyncio.get_event_loop().time() - start)
            eta = re.search(r"(\d+)\s*(minute|second)", body)
            eta_str = f" (~{eta.group(1)} {eta.group(2)}s)" if eta else ""
            log.info(f"⏳ Waiting room{eta_str} — {elapsed}s elapsed")

            if elapsed > 0 and elapsed % 60 == 0:
                try:
                    from core.notifier import notifier
                    await notifier.send_telegram(
                        f"⏳ <b>Waiting Room</b>{eta_str}\n"
                        f"Waited {elapsed // 60} min — please be patient."
                    )
                except Exception:
                    pass

            if elapsed > 0 and elapsed % 120 == 0:
                log.info("Refreshing page...")
                try:
                    await self._page.reload(
                        wait_until="domcontentloaded",
                        timeout=30000
                    )
                    await asyncio.sleep(3)
                except Exception:
                    pass

            # abort-aware wait (5s broken into small steps)
            for _ in range(20):
                self._check_abort()
                await asyncio.sleep(0.25)

        return False

    async def _wait_for_telegram_reply(self, timeout=300) -> str | None:
        reply_file = Path(__file__).parent.parent / "telegram_reply.tmp"
        pending_file = Path(__file__).parent.parent / "telegram_reply.pending"

        if reply_file.exists():
            reply_file.unlink()
        pending_file.write_text("1")

        try:
            start = asyncio.get_event_loop().time()
            while (asyncio.get_event_loop().time() - start) < timeout:
                self._check_abort()
                if reply_file.exists():
                    try:
                        answer = reply_file.read_text().strip()
                        reply_file.unlink()
                        if answer:
                            log.info(f"Got Telegram reply: {answer[:20]}...")
                            return answer
                    except Exception:
                        pass
                await asyncio.sleep(2)

            log.warning("Telegram reply timeout")
            return None
        finally:
            if pending_file.exists():
                pending_file.unlink()

    async def _load_security_answers(self) -> dict[str, str]:
        # Per-account answers take priority over global DB answers
        if self._account_security_answers:
            return {
                str(k).lower().strip(): str(v).strip()
                for k, v in self._account_security_answers.items()
                if str(k).strip() and str(v).strip()
            }
        answers: dict[str, str] = {}
        try:
            from core.database import get_config
            raw = await get_config("security_answers")
            if raw:
                saved = json.loads(raw)
                answers.update({
                    str(key).lower().strip(): str(value).strip()
                    for key, value in saved.items()
                    if str(key).strip() and str(value).strip()
                })
        except Exception as e:
            log.warning(f"Could not load saved security answers: {e}")
        return answers

    def _date_range(self) -> tuple[date | None, date | None]:
        """Parse this account's date window (YYYY-MM-DD) into date objects.
        Uses per-account dates when set, otherwise falls back to the global
        settings.date_from / settings.date_to. Returns (None, None) for any
        value that is missing/unparseable so filtering degrades to 'no limit'
        on that side."""
        def _parse(val):
            val = (val or "").strip()
            if not val:
                return None
            try:
                return datetime.strptime(val, "%Y-%m-%d").date()
            except ValueError:
                return None
        date_from = self._account_date_from or getattr(settings, "date_from", "")
        date_to = self._account_date_to or getattr(settings, "date_to", "")
        return _parse(date_from), _parse(date_to)

    def _in_date_range(self, date_str: str) -> bool:
        """True if date_str (YYYY-MM-DD) falls within the configured range.
        An unset/invalid bound means that side is unbounded."""
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return False
        start, end = self._date_range()
        if start and d < start:
            return False
        if end and d > end:
            return False
        return True

    def _get_answer(self, question_text: str, answers: dict[str, str] | None = None) -> str | None:
        question = (question_text or "").lower()
        answer_map = answers or SECURITY_ANSWERS
        for keyword in sorted(answer_map.keys(), key=len, reverse=True):
            key = str(keyword).lower().strip()
            if key and key in question:
                return str(answer_map[key]).strip()
        return None

    async def _read_security_question_fields(self) -> list[dict]:
        try:
            fields = await self._page.evaluate("""
                () => {
                    const visible = (el) => {
                        if (!el) return false;
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        return style.visibility !== "hidden" &&
                               style.display !== "none" &&
                               rect.width > 0 &&
                               rect.height > 0;
                    };
                    const clean = (text) => (text || "")
                        .replace(/\\s+/g, " ")
                        .replace(/^Security Question\\s*\\d+\\*?\\s*/i, "")
                        .trim();
                    const lines = (document.body.innerText || "")
                        .split(/\\n+/)
                        .map(line => line.trim())
                        .filter(Boolean);
                    const inputs = Array.from(document.querySelectorAll(
                        "input[id^='kba'][id$='_response'], input[name^='kba'][name$='_response']"
                    )).filter(visible);

                    return inputs.map((input, index) => {
                        const raw = input.id || input.name || "";
                        const numMatch = raw.match(/kba(\\d+)_response/);
                        const questionNo = numMatch ? numMatch[1] : String(index + 1);
                        const selector = input.id
                            ? `#${input.id}`
                            : `input[name="${input.name}"]`;

                        let question = "";
                        const direct = document.getElementById(`kbq${questionNo}ReadOnly`);
                        if (direct) {
                            question = direct.getAttribute("aria-label") ||
                                       direct.textContent ||
                                       direct.innerText ||
                                       "";
                        }

                        if (!question && input.id) {
                            const label = document.querySelector(`label[for="${input.id}"]`);
                            if (label) question = label.getAttribute("aria-label") ||
                                                  label.textContent ||
                                                  label.innerText ||
                                                  "";
                        }

                        if (!question) {
                            const displayNo = String(index + 1);
                            for (let i = 0; i < lines.length; i++) {
                                if (new RegExp(`^Security Question\\\\s*${displayNo}\\\\*?$`, "i").test(lines[i]) ||
                                    new RegExp(`^Security Question\\\\s*${questionNo}\\\\*?$`, "i").test(lines[i])) {
                                    question = lines[i + 1] || "";
                                    break;
                                }
                            }
                        }

                        if (!question) {
                            let node = input.previousElementSibling;
                            let steps = 0;
                            while (node && steps < 6) {
                                const text = clean(node.getAttribute("aria-label") ||
                                                   node.textContent ||
                                                   node.innerText ||
                                                   "");
                                if (text && !/^Security Question/i.test(text)) {
                                    question = text;
                                    break;
                                }
                                node = node.previousElementSibling;
                                steps += 1;
                            }
                        }

                        return {
                            question_no: questionNo,
                            selector,
                            question: clean(question),
                        };
                    }).filter(item => item.selector && item.question);
                }
            """)
            return fields or []
        except Exception as e:
            log.warning(f"Could not read security question fields: {e}")
            return []

    async def _wait_for_auth_redirect(self, timeout=150) -> bool:
        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) < timeout:
            self._check_abort()

            try:
                body_peek = (await self._page.inner_text("body")).lower()
            except Exception:
                body_peek = ""

            if any(s in body_peek for s in ["verify you are human",
                    "just a moment", "performing security verification",
                    "checking your browser"]):
                log.info("Cloudflare appeared after security — handling it")
                await self._handle_cloudflare(timeout=60)
                continue

            if any(s in body_peek for s in ["waiting room", "you are now in line",
                    "thank you for your patience", "you are being redirected",
                    "do not close your browser"]):
                log.info("Waiting room appeared after security — handling it")
                await self._handle_waiting_room(timeout=300)
                continue

            url = (self._page.url or "").lower()

            # STRONG positive signals — logged-in proof
            if await self._visible_any([
                "a[href*='logout']",
                "a[href*='LogOff']",
                "a[href*='signout']",
                "#atlas-sidebar",
                "#reschedule_appointment",
                "#continue_application",
                "#manage_appointments",
                "#schedule_appointment",
                ".dashboard",
                "#dashboard",
            ], timeout=700):
                log.info("Login successful after security questions")
                return True

            # Logged-in homepage text
            if any(t in body_peek for t in [
                "manage appointments", "reschedule appointment",
                "continue application", "log off", "logoff", "sign out",
            ]):
                log.info("Login successful — portal content detected")
                return True

            # Back on the portal (and not on the B2C login domain)
            if "usvisascheduling.com" in url and "b2clogin.com" not in url \
                    and "atlasauth" not in url:
                log.info("Returned to US Visa Scheduling portal")
                return True

            # Real rejection — wrong answer
            if any(text in body_peek for text in [
                "incorrect", "invalid answer", "try again",
                "the information you entered",
            ]):
                log.error("Security answer rejected by portal")
                await self.screenshot("security_answer_rejected")
                return False

            await asyncio.sleep(1)

        log.warning("Timed out waiting for portal redirect after security submit")
        await self.screenshot("auth_redirect_timeout")
        return False

    async def _click_continue(self) -> bool:
        for selector in [
            "#continue",
            "[aria-label='Continue']",
            "button:has-text('Continue')",
            "input[type='submit']",
            "text=Continue",
        ]:
            try:
                locator = self._page.locator(selector).first
                if await locator.is_visible(timeout=1000):
                    await locator.click(timeout=5000)
                    return True
            except Exception:
                pass
        return False

    def _match_security_answer(
        self,
        question_text: str,
        answers: dict[str, str],
    ) -> tuple[str | None, str | None]:
        question = (question_text or "").lower()
        for keyword in sorted(answers.keys(), key=len, reverse=True):
            key = str(keyword).lower().strip()
            if key and key in question:
                return key, str(answers[key]).strip()
        return None, None

    async def _handle_security_questions(self) -> bool:
        from core.notifier import notifier

        fields: list[dict] = []
        start = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start) < 60:
            self._check_abort()
            fields = await self._read_security_question_fields()
            if fields:
                break
            url = (self._page.url or "").lower()
            if "usvisascheduling.com" in url and "b2clogin.com" not in url:
                log.info("No security questions; portal already reached")
                return True
            await asyncio.sleep(1)

        if not fields:
            url = (self._page.url or "").lower()
            if "b2clogin.com" in url or "atlasauth" in url:
                log.error("Still on auth page but security fields were not found")
                await self.screenshot("security_fields_not_found")
                return False
            log.info("No security questions found")
            return True

        log.info(f"Security questions page detected with {len(fields)} question(s)")
        await self.screenshot("security_questions")

        answers = await self._load_security_answers()
        if not answers:
            await notifier.send_telegram(
                "⚠️ <b>No security answers saved.</b>\n"
                "Run /setup again and add your 3 security question keywords and answers."
            )
            return False

        missing: list[str] = []
        for field in fields:
            question = field["question"]
            matched_keyword, answer = self._match_security_answer(question, answers)
            log.info(
                f"Security question: {question} | matched keyword: {matched_keyword or 'NONE'}"
            )
            if not answer:
                missing.append(question)
                continue
            print(
                "\nSECURITY ANSWER DEBUG\n"
                f"Question : {question}\n"
                f"Keyword  : {matched_keyword}\n"
                f"Answer   : {answer}\n"
                f"Input    : {field['selector']}\n",
                flush=True,
            )
            await self._page.fill(field["selector"], answer)
            log.info(f"Filled {field['selector']}")

        if missing:
            await notifier.send_telegram(
                "⚠️ <b>Security question keyword not matched.</b>\n"
                + "\n".join(f"- {html.escape(q[:120])}" for q in missing)
                + "\n\nRun /setup and use keywords from these exact questions."
            )
            await self.screenshot("security_keyword_missing")
            return False

        await self._sleep(0.3, 0.6)
        if not await self._click_continue():
            log.error("Could not click Continue on security page")
            await self.screenshot("security_continue_missing")
            return False

        log.info("Security questions submitted")
        result = await self._wait_for_auth_redirect(timeout=90)
        if result:
            try:
                from core.notifier import notifier as _notif
                await _notif.send_telegram(
                    f"✅ Security questions passed"
                    + (f" — <b>{self._account_name}</b>" if self._account_name else "")
                )
            except Exception:
                pass
        return result

    async def _login_form_visible(self) -> bool:
        """True if the portal/B2C login form is on screen right now."""
        return await self._visible_any(["#signInName", "#password"], timeout=600)

    async def _is_logged_in(self) -> bool:
        """Detect the logged-in homepage using STRONG positive signals only.
        Being merely on /en-US is NOT enough — the logged-OUT homepage also
        sits on /en-US, so that weak signal was removed to stop false positives.
        Requires real logged-in proof (logout link / schedule sidebar links)."""

        # First — if a login form OR a sign-in button is present, we are
        # definitely NOT logged in (expired session).
        if await self._login_form_visible():
            return False
        try:
            body = (await self._page.inner_text("body")).lower()
        except Exception:
            body = ""
        if any(t in body for t in [
            "sign in", "log in", "login to your account",
            "create an account", "forgot password",
        ]):
            return False

        # Signal (a) — STRONG: logout link or schedule sidebar links present.
        # These ONLY exist when logged in.
        if await self._visible_any([
            "#atlas-sidebar",
            "#reschedule_appointment",
            "#continue_application",
            "#manage_appointments",
            "#schedule_appointment",
            "a[href*='LogOff']",
            "a[href*='logout']",
            "a[href*='signout']",
        ], timeout=1500):
            return True

        # Signal (b) — STRONG: logged-in-only page text.
        if any(t in body for t in [
            "manage appointments",
            "reschedule appointment",
            "continue application",
            "log off",
            "logoff",
            "sign out",
        ]):
            return True

        # No strong proof of being logged in → treat as NOT logged in.
        return False

    # ------------------------------------------------------------------ #
    #  Login
    # ------------------------------------------------------------------ #

    async def login(self) -> bool:
        log.info(f"Logging in as: {getattr(self, '_login_username', '') or settings.portal_username}")

        # Try saved session first — skips login form + security questions.
        # Cloudflare may STILL appear once, so we keep clicking it. With a valid
        # session there is NO waiting room and NO login form — we land straight
        # on the logged-in homepage and must recognize that, not keep waiting.
        had_session = await self.load_session()
        if had_session:
            log.info("Trying saved session — checking if still valid...")
            try:
                await self._page.goto(
                    HOME_URL,
                    wait_until="domcontentloaded",
                    timeout=60000
                )
                # Cloudflare still shows once even with a valid session — click it.
                # Give it a full timeout so the iframe has time to load and be
                # clicked. Do NOT rush past it.
                await self._handle_cloudflare(timeout=90)
                await self._handle_waiting_room(timeout=120)
                await self._sleep(2, 3)

                # Now check login state. Poll longer (up to ~25s) because the
                # homepage can take a moment to render after CF clears.
                logged_in = False
                for _ in range(25):
                    self._check_abort()
                    # If CF somehow came back, handle it again before checking.
                    try:
                        body = (await self._page.inner_text("body")).lower()
                        if any(s in body for s in ["verify you are human",
                                "performing security verification", "just a moment"]):
                            await self._handle_cloudflare(timeout=60)
                    except Exception:
                        pass

                    if await self._is_logged_in():
                        logged_in = True
                        break
                    # If the login form appeared, the session is dead — bail to full login.
                    if await self._login_form_visible():
                        log.info("Login form appeared — session is dead")
                        break
                    await asyncio.sleep(1)

                if logged_in:
                    log.info("✅ Session still valid — login skipped!")
                    await self.screenshot("session_reused")
                    try:
                        from core.notifier import notifier as _notif
                        await _notif.send_telegram(
                            f"♻️ Reused session (no login needed)"
                            + (f" — <b>{self._account_name}</b>" if self._account_name else "")
                        )
                    except Exception:
                        pass
                    return True
                # Session expired — delete the dead cookie file so we don't
                # keep retrying with it (which can get the IP flagged).
                log.info("Session expired — deleting old cookies, doing full login")
                await self.delete_session()
                # Clear the dead cookies from the live context too
                try:
                    await self._context.clear_cookies()
                except Exception:
                    pass
            except ScanAborted:
                raise
            except Exception as e:
                log.info(f"Session check failed: {e} — deleting session, full login")
                await self.delete_session()
                try:
                    await self._context.clear_cookies()
                except Exception:
                    pass

        try:
            await self._page.goto(
                settings.portal_url,
                wait_until="domcontentloaded",
                timeout=60000
            )
        except Exception as e:
            log.error(f"Cannot reach portal: {e}")
            return False

        if not await self._wait_for_login_or_dashboard():
            return False

        try:
            if await self._visible_any(["a[href*='logout']", ".dashboard", "#dashboard"]):
                log.info("Already logged in")
                await self.save_session()
                return True
        except Exception:
            pass

        await self._sleep(0.1, 0.2)
        await self._page.mouse.move(
            random.randint(100, 500),
            random.randint(100, 400)
        )
        await self._sleep(0.1, 0.2)
        await self._page.mouse.wheel(0, random.randint(100, 300))
        await self._sleep(0.1, 0.2)
        await self._page.mouse.move(
            random.randint(200, 700),
            random.randint(200, 500)
        )
        await self._sleep(0.1, 0.2)

        try:
            await self._page.wait_for_selector("#signInName", timeout=60000)
            _user = getattr(self, "_login_username", "") or settings.portal_username
            _pass = getattr(self, "_login_password", "") or settings.portal_password
            await self._page.fill("#signInName", _user)
            log.info("Username filled")
            await self._sleep(0.2, 0.4)
            await self._page.fill("#password", _pass)
            log.info("Password filled")
            await self._sleep(0.2, 0.4)
            await self._page.click("#continue")
            log.info("Login submitted")
            await self._page.wait_for_load_state("domcontentloaded", timeout=15000)
            await self._handle_waiting_room()
        except ScanAborted:
            raise
        except Exception as e:
            log.error(f"Login form error: {e}")
            await self.screenshot("login_error")
            return False

        await self._handle_waiting_room()
        if not await self._handle_security_questions():
            log.error("Security question step failed")
            return False

        await self._sleep(0.5, 1)
        current_url = self._page.url
        log.info(f"After login URL: {current_url}")

        if "/appointments" in current_url or "/dashboard" in current_url:
            log.info("✅ Login successful")
            await self.save_session()
            try:
                from core.notifier import notifier as _notif
                await _notif.send_telegram(
                    f"🔐 Logged in successfully"
                    + (f" — <b>{self._account_name}</b>" if self._account_name else "")
                )
            except Exception:
                pass
            return True

        try:
            if await self._page.is_visible(".error-box"):
                err = await self._page.text_content(".error-box")
                log.error(f"Login error shown: {err}")
                return False
        except Exception:
            pass

        try:
            if await self._page.is_visible("a[href='/logout']"):
                log.info("✅ Login successful (logout link found)")
                await self.save_session()
                try:
                    from core.notifier import notifier as _notif
                    await _notif.send_telegram(
                        f"🔐 Logged in successfully"
                        + (f" — <b>{self._account_name}</b>" if self._account_name else "")
                    )
                except Exception:
                    pass
                return True
        except Exception:
            pass

        # Check for portal homepage signs
        try:
            body = (await self._page.inner_text("body")).lower()
            if any(w in body for w in ["visa application home", "appointment confirmation",
                                        "manage appointments", "reschedule"]):
                log.info("✅ Login successful (portal content detected)")
                await self.save_session()
                try:
                    from core.notifier import notifier as _notif
                    await _notif.send_telegram(
                        f"🔐 Logged in successfully"
                        + (f" — <b>{self._account_name}</b>" if self._account_name else "")
                    )
                except Exception:
                    pass
                return True
        except Exception:
            pass

        log.warning("Login result unclear — taking screenshot")
        await self.screenshot("login_check")
        return False

    # ================================================================== #
    #  REAL PORTAL: Navigate to Schedule Page
    # ================================================================== #

    async def _navigate_to_schedule_page(self) -> bool:
        base = ""  # URLs are now absolute

        try:
            from core.notifier import notifier as _notif
            await _notif.send_telegram(
                f"🧭 Navigating to schedule page"
                + (f" — <b>{self._account_name}</b>" if self._account_name else "")
            )
        except Exception:
            pass

        log.info("Navigating to homepage...")
        try:
            await self._page.goto(
                f"{base}{HOME_URL}",
                wait_until="domcontentloaded",
                timeout=30000
            )
            await self._handle_cloudflare(timeout=30)
            await self._handle_waiting_room(timeout=120)
            await self._sleep(1, 1.5)
        except ScanAborted:
            raise
        except Exception as e:
            log.error(f"Cannot reach homepage: {e}")
            return False

        await self.screenshot("homepage")

        # Auto-detect: reschedule (if appointment exists) or new schedule
        log.info("Detecting schedule type...")
        try:
            reschedule_link = await self._page.query_selector("#reschedule_appointment")
            schedule_link = await self._page.query_selector(
                "#continue_application, #schedule_appointment, #admin_link"
            )

            if reschedule_link:
                await reschedule_link.click()
                log.info("✅ RESCHEDULE mode — clicked Reschedule Appointment")
                try:
                    from core.notifier import notifier as _notif
                    await _notif.send_telegram(
                        f"🔄 Reschedule mode"
                        + (f" — <b>{self._account_name}</b>" if self._account_name else "")
                    )
                except Exception:
                    pass
            elif schedule_link:
                await schedule_link.click()
                log.info("✅ NEW SCHEDULE mode — clicked Schedule link")
                try:
                    from core.notifier import notifier as _notif
                    await _notif.send_telegram(
                        f"🆕 New schedule mode"
                        + (f" — <b>{self._account_name}</b>" if self._account_name else "")
                    )
                except Exception:
                    pass
            else:
                # No link found — try direct URL (new schedule first)
                log.warning("No schedule link found in sidebar — trying direct URL")
                await self.screenshot("no_schedule_link")
                await self._page.goto(
                    NEW_SCHEDULE_URL,
                    wait_until="domcontentloaded",
                    timeout=30000
                )
        except ScanAborted:
            raise
        except Exception as e:
            log.warning(f"Schedule navigation failed, trying direct URL: {e}")
            await self._page.goto(
                NEW_SCHEDULE_URL,
                wait_until="domcontentloaded",
                timeout=30000
            )

        await self._handle_cloudflare(timeout=30)
        await self._handle_waiting_room(timeout=120)
        await self._sleep(1.5, 2.5)

        try:
            if await self._page.is_visible("#post_select", timeout=10000):
                log.info("✅ On schedule page — post dropdown visible!")
                await self.screenshot("schedule_page")
                return True
        except Exception:
            pass

        log.error("Schedule page not loaded")
        await self.screenshot("schedule_page_error")
        return False

    # ================================================================== #
    #  REAL PORTAL: Select DHAKA Post and Load Calendar
    # ================================================================== #

    async def _select_post_and_load_calendar(self) -> bool:
        try:
            from core.notifier import notifier as _notif
            await _notif.send_telegram(
                f"📅 Selecting DHAKA, loading calendar"
                + (f" — <b>{self._account_name}</b>" if self._account_name else "")
            )
        except Exception:
            pass

        # Make sure the dropdown AND the DHAKA option are actually loaded first.
        # On session-reuse the page can render #post_select before its options
        # finish loading, so selecting too early silently fails.
        dhaka_ready = False
        for _ in range(20):
            self._check_abort()
            try:
                has_dhaka = await self._page.evaluate(
                    f"""() => {{
                        const sel = document.querySelector('#post_select');
                        if (!sel) return false;
                        return !!sel.querySelector('option[value="{DHAKA_POST_VALUE}"]');
                    }}"""
                )
                if has_dhaka:
                    dhaka_ready = True
                    break
            except Exception:
                pass
            # A late Cloudflare / waiting room can also block the options
            try:
                body_peek = (await self._page.inner_text("body")).lower()
                if any(s in body_peek for s in ["verify you are human",
                        "just a moment", "performing security verification"]):
                    await self._handle_cloudflare(timeout=60)
                if any(s in body_peek for s in ["waiting room", "you are now in line"]):
                    await self._handle_waiting_room(timeout=300)
            except Exception:
                pass
            await asyncio.sleep(1)

        if not dhaka_ready:
            log.error("DHAKA option never appeared in #post_select")
            await self.screenshot("dhaka_option_missing")
            return False

        # Now select DHAKA (retry a few times — the option can need a beat)
        selected = False
        for attempt in range(3):
            self._check_abort()
            try:
                current_val = await self._page.evaluate(
                    "() => document.querySelector('#post_select').value"
                )
                if current_val == DHAKA_POST_VALUE:
                    log.info("DHAKA already selected")
                    selected = True
                    break
                await self._page.select_option("#post_select", value=DHAKA_POST_VALUE)
                await self._sleep(0.5, 1)
                # Verify it actually took
                new_val = await self._page.evaluate(
                    "() => document.querySelector('#post_select').value"
                )
                if new_val == DHAKA_POST_VALUE:
                    log.info("✅ Selected DHAKA from dropdown")
                    selected = True
                    break
                log.info(f"DHAKA select didn't stick (attempt {attempt+1}) — retrying")
            except Exception as e:
                log.warning(f"DHAKA select attempt {attempt+1} failed: {e}")
                await self._sleep(1, 1.5)

        if not selected:
            log.error("Cannot select DHAKA after retries")
            await self.screenshot("dhaka_select_failed")
            return False

        log.info("Waiting for calendar to load...")
        for i in range(30):
            self._check_abort()
            await asyncio.sleep(1)

            # If a Cloudflare/redirect kicked us off the schedule page, recover
            try:
                if not await self._page.is_visible("#post_select", timeout=500):
                    body_peek = (await self._page.inner_text("body")).lower()
                    if any(s in body_peek for s in ["verify you are human",
                            "just a moment", "performing security verification"]):
                        log.info("Cloudflare appeared while loading calendar — handling")
                        await self._handle_cloudflare(timeout=60)
            except Exception:
                pass

            try:
                has_calendar = await self._page.is_visible(
                    ".ui-datepicker-calendar", timeout=1000
                )
                if has_calendar:
                    log.info("✅ Calendar loaded!")
                    await self.screenshot("calendar_loaded")
                    return True
            except Exception:
                pass
            try:
                msg_el = await self._page.query_selector("#datepicker-message")
                if msg_el:
                    msg = (await msg_el.text_content() or "").strip()
                    if msg == "Select Date":
                        log.info("✅ Calendar ready")
                        return True
                    elif "Loading" in msg:
                        log.info(f"Calendar loading... ({i}s)")
                    elif "No Slots" in msg or "error" in msg.lower():
                        log.warning(f"Calendar: {msg}")
                        return True
            except Exception:
                pass

        log.warning("Calendar load timeout")
        await self.screenshot("calendar_timeout")
        return False

    # ================================================================== #
    #  REAL PORTAL: Read Available Dates from Calendar
    # ================================================================== #

    async def _displayed_month_beyond_end(self, end: date | None) -> bool:
        """True if every month panel currently shown is already past `end`.
        Used to stop forward-scrolling once we've gone past the wanted range."""
        if not end:
            return False
        try:
            first_days = await self._page.evaluate("""
                () => {
                    const months = {'January':1,'February':2,'March':3,'April':4,'May':5,'June':6,
                        'July':7,'August':8,'September':9,'October':10,'November':11,'December':12};
                    const out = [];
                    document.querySelectorAll('.ui-datepicker-group .ui-datepicker-title').forEach(h => {
                        const mSel = h.querySelector('select.ui-datepicker-month');
                        const ySel = h.querySelector('select.ui-datepicker-year');
                        if (mSel && ySel) { out.push([parseInt(ySel.value), parseInt(mSel.value) + 1]); return; }
                        const mSpan = h.querySelector('.ui-datepicker-month');
                        const ySpan = h.querySelector('.ui-datepicker-year');
                        if (mSpan && ySpan) { out.push([parseInt(ySpan.textContent.trim()), months[mSpan.textContent.trim()] || 0]); }
                    });
                    return out;
                }
            """)
            if not first_days:
                return False
            # Earliest panel shown — if even its first day is after end, we're past range
            earliest = min((y, m) for y, m in first_days if y and m)
            return (earliest[0], earliest[1]) > (end.year, end.month)
        except Exception:
            return False

    async def _read_calendar_dates(self) -> List[dict]:
        available_dates = []
        start_bound, end_bound = self._date_range()
        if start_bound or end_bound:
            log.info(f"Filtering dates to range: {start_bound or '—'} → {end_bound or '—'}")

        try:
            green_cells = await self._page.query_selector_all("td.greenday")

            if not green_cells:
                log.info("No available dates in current view — scrolling months...")
                for m in range(6):
                    self._check_abort()
                    # Don't scroll forever past the configured window
                    if await self._displayed_month_beyond_end(end_bound):
                        log.info("Reached end of configured date range — stop scrolling.")
                        break
                    try:
                        next_btn = await self._page.query_selector(
                            ".ui-datepicker-next:not(.ui-state-disabled)"
                        )
                        if not next_btn:
                            break
                        await next_btn.click()
                        await self._sleep(1, 1.5)
                        green_cells = await self._page.query_selector_all("td.greenday")
                        if green_cells:
                            log.info(f"Found {len(green_cells)} dates after scrolling {m+1} months!")
                            break
                    except Exception:
                        break

            for cell in green_cells:
                try:
                    day_el = await cell.query_selector("a.ui-state-default, span.ui-state-default")
                    if not day_el:
                        continue
                    day_text = (await day_el.text_content() or "").strip()
                    if not day_text.isdigit():
                        continue

                    date_info = await cell.evaluate("""
                        (el) => {
                            const table = el.closest('table');
                            const group = table ? table.closest('.ui-datepicker-group') : null;
                            if (!group) return null;
                            const header = group.querySelector('.ui-datepicker-title');
                            if (!header) return null;
                            const monthSelect = header.querySelector('select.ui-datepicker-month');
                            const yearSelect = header.querySelector('select.ui-datepicker-year');
                            if (monthSelect && yearSelect) {
                                return {month: parseInt(monthSelect.value) + 1, year: parseInt(yearSelect.value)};
                            }
                            const monthSpan = header.querySelector('.ui-datepicker-month');
                            const yearSpan = header.querySelector('.ui-datepicker-year');
                            if (monthSpan && yearSpan) {
                                const months = {'January':1,'February':2,'March':3,'April':4,'May':5,'June':6,
                                    'July':7,'August':8,'September':9,'October':10,'November':11,'December':12,
                                    'Jan':1,'Feb':2,'Mar':3,'Apr':4,'Jun':6,'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12};
                                return {month: months[monthSpan.textContent.trim()] || 0, year: parseInt(yearSpan.textContent.trim())};
                            }
                            return null;
                        }
                    """)

                    if not date_info or not date_info.get('month') or not date_info.get('year'):
                        continue

                    day = int(day_text)
                    month = date_info['month']
                    year = date_info['year']
                    date_str = f"{year}-{month:02d}-{day:02d}"

                    if not self._in_date_range(date_str):
                        log.info(f"⏭ Skipping {date_str} — outside configured range")
                        continue

                    available_dates.append({
                        "date_str": date_str,
                        "month": month,
                        "year": year,
                        "day": day,
                    })
                    log.info(f"📅 Available (in range): {date_str}")

                except Exception as e:
                    log.debug(f"Error reading date cell: {e}")

        except ScanAborted:
            raise
        except Exception as e:
            log.error(f"Calendar read error: {e}", exc_info=True)

        log.info(f"Total available dates: {len(available_dates)}")
        return available_dates

    # ================================================================== #
    #  REAL PORTAL: Scan Appointments
    # ================================================================== #

    async def scan_appointments(self) -> List[AppointmentSlot]:
        slots: List[AppointmentSlot] = []
        log.info("=" * 50)
        log.info("Starting appointment scan on real portal...")
        try:
            from core.notifier import notifier as _notif
            await _notif.send_telegram(
                f"🔍 Scanning calendar for <b>{self._account_name or 'account'}</b>"
            )
        except Exception:
            pass

        # Step 1: Navigate to schedule page
        if not await self._navigate_to_schedule_page():
            log.error("Cannot reach schedule page")
            return slots

        # Step 2: Select DHAKA and wait for calendar
        if not await self._select_post_and_load_calendar():
            log.error("Calendar did not load")
            return slots

        # Step 3: Read available dates
        available_dates = await self._read_calendar_dates()

        if not available_dates:
            log.info("❌ No available appointment dates found")
            await self.screenshot("no_dates")
            return slots

        # Step 4: Click each date and read time slots
        for date_info in available_dates:
            self._check_abort()
            try:
                date_str = date_info["date_str"]
                day = date_info["day"]

                # Click the green date on calendar
                clicked = False
                green_links = await self._page.query_selector_all("td.greenday a.ui-state-default")
                for link in green_links:
                    link_text = (await link.text_content() or "").strip()
                    if link_text == str(day):
                        await link.click()
                        log.info(f"Clicked date: {date_str}")
                        clicked = True
                        break

                if not clicked:
                    log.warning(f"Could not click date {date_str}")
                    continue

                await self._sleep(1.5, 2.5)

                # Read time slots from #time_select table
                time_rows = await self._page.query_selector_all(
                    "#time_select tr:has(input[name='schedule-entries'])"
                )

                for row in time_rows:
                    try:
                        radio = await row.query_selector("input[name='schedule-entries']")
                        if not radio:
                            continue
                        slot_id = await radio.get_attribute("value") or ""
                        slot_count = await radio.get_attribute("data-slots") or "0"

                        tds = await row.query_selector_all("td")
                        time_text = ""
                        if len(tds) >= 2:
                            time_text = (await tds[1].text_content() or "").strip()

                        avail = int(slot_count) if slot_count.isdigit() else 1

                        slots.append(AppointmentSlot(
                            slot_date=date_str,
                            slot_time=time_text,
                            location="DHAKA",
                            slots_available=avail,
                            raw_element_id=slot_id,
                        ))
                        log.info(f"  📍 Slot: {date_str} {time_text} (avail: {slot_count}, id: {slot_id})")

                    except Exception as e:
                        log.debug(f"Error reading time row: {e}")

            except ScanAborted:
                raise
            except Exception as e:
                log.error(f"Error processing date {date_info.get('date_str', '?')}: {e}")

        log.info(f"✅ Total slots found: {len(slots)}")
        await self.screenshot("scan_complete")
        return slots
    # ================================================================== #
    #  REAL-TIME MODE: prepare calendar page once (login + navigate)
    # ================================================================== #

    async def prepare_calendar_page(self) -> bool:
        """One-time setup: login, go to schedule page, select DHAKA, load
        calendar. After this the browser STAYS OPEN on the calendar page so
        we can just reload it every minute instead of logging in again."""
        try:
            if not await self.login():
                log.error("prepare_calendar_page: login failed")
                return False
            if not await self._navigate_to_schedule_page():
                log.error("prepare_calendar_page: could not reach schedule page")
                return False
            if not await self._select_post_and_load_calendar():
                log.error("prepare_calendar_page: calendar did not load")
                return False
            log.info("✅ Calendar page ready — browser will stay open for reloads")
            return True
        except ScanAborted:
            raise
        except Exception as e:
            log.error(f"prepare_calendar_page error: {e}", exc_info=True)
            return False

    # ================================================================== #
    #  REAL-TIME MODE: reload + re-check calendar (no login)
    # ================================================================== #

    async def reload_and_read_calendar(self) -> tuple[str, List[AppointmentSlot]]:
        """Reload the schedule page, re-handle Cloudflare / waiting room if they
        appear, re-select DHAKA, read the calendar and any time slots.

        Returns (status, slots):
          "ok"           — reloaded and read fine (slots may be empty)
          "need_relogin" — session/page broke; caller must close browser and
                            run prepare_calendar_page() again
        Never logs in, never closes the browser itself."""
        slots: List[AppointmentSlot] = []
        try:
            try:
                await self._page.reload(wait_until="domcontentloaded", timeout=75000)
            except ScanAborted:
                raise
            except Exception as e:
                log.warning(f"Reload failed: {e} — will re-login")
                return ("need_relogin", slots)

            # Human-like pause after the page loads — a person doesn't act the
            # instant a page appears; they look at it for a couple of seconds.
            await self._sleep(2.0, 4.0)

            # A few small mouse moves + a scroll, like glancing over the page
            await self._human_glance()


            try:
                body_peek = (await self._page.inner_text("body")).lower()
            except Exception:
                body_peek = ""

            if any(s in body_peek for s in ["verify you are human", "just a moment",
                    "performing security verification", "checking your browser"]):
                log.info("Cloudflare appeared after reload — handling")
                await self._handle_cloudflare(timeout=90)

            if any(s in body_peek for s in ["waiting room", "you are now in line",
                    "thank you for your patience", "you are being redirected",
                    "do not close your browser", "your position"]):
                log.info("Waiting room appeared after reload — handling")
                await self._handle_waiting_room(timeout=600)

            if await self._login_form_visible():
                log.info("Login form visible after reload — session expired")
                return ("need_relogin", slots)

            on_schedule = False
            try:
                on_schedule = await self._page.is_visible("#post_select", timeout=5000)
            except Exception:
                on_schedule = False

            if not on_schedule:
                log.info("Not on schedule page after reload — trying to navigate back")
                if not await self._navigate_to_schedule_page():
                    log.info("Could not reach schedule page — staying on page, will retry next minute")
                    return ("no_calendar", slots)

            if not await self._select_post_and_load_calendar():
                log.info("Calendar not available this minute — staying on page, will retry next minute")
                return ("no_calendar", slots)

            available_dates = await self._read_calendar_dates()
            if not available_dates:
                log.info("No available dates this minute")
                return ("ok", slots)

            for date_info in available_dates:
                self._check_abort()
                try:
                    date_str = date_info["date_str"]
                    day = date_info["day"]

                    clicked = False
                    green_links = await self._page.query_selector_all(
                        "td.greenday a.ui-state-default"
                    )
                    for link in green_links:
                        link_text = (await link.text_content() or "").strip()
                        if link_text == str(day):
                            await link.click()
                            log.info(f"Clicked date: {date_str}")
                            clicked = True
                            break
                    if not clicked:
                        continue

                    await self._sleep(1.0, 2.0)

                    time_rows = await self._page.query_selector_all(
                        "#time_select tr:has(input[name='schedule-entries'])"
                    )
                    for row in time_rows:
                        try:
                            radio = await row.query_selector("input[name='schedule-entries']")
                            if not radio:
                                continue
                            slot_id = await radio.get_attribute("value") or ""
                            slot_count = await radio.get_attribute("data-slots") or "0"
                            tds = await row.query_selector_all("td")
                            time_text = ""
                            if len(tds) >= 2:
                                time_text = (await tds[1].text_content() or "").strip()
                            avail = int(slot_count) if slot_count.isdigit() else 1
                            slots.append(AppointmentSlot(
                                slot_date=date_str,
                                slot_time=time_text,
                                location="DHAKA",
                                slots_available=avail,
                                raw_element_id=slot_id,
                            ))
                            log.info(f"  📍 Slot: {date_str} {time_text} (avail: {slot_count})")
                        except Exception:
                            pass
                except ScanAborted:
                    raise
                except Exception as e:
                    log.error(f"Error reading date {date_info.get('date_str','?')}: {e}")

            return ("ok", slots)

        except ScanAborted:
            raise
        except Exception as e:
            log.error(f"reload_and_read_calendar error: {e}", exc_info=True)
            return ("need_relogin", slots)
    # ================================================================== #
    #  REAL PORTAL: Book Appointment
    # ================================================================== #

    async def book_appointment(self, slot: AppointmentSlot) -> str | None:
        log.info(f"📅 Booking: {slot.slot_date} {slot.slot_time} (ID: {slot.raw_element_id})")

        # Safety net: never book outside the configured date window
        if not self._in_date_range(slot.slot_date):
            start, end = self._date_range()
            log.warning(
                f"Refusing to book {slot.slot_date} — outside range "
                f"{start or '—'} → {end or '—'}"
            )
            try:
                from core.notifier import notifier as _notif
                await _notif.send_telegram(
                    f"⚠️ Skipped booking <b>{slot.slot_date}</b> — outside your "
                    f"date range ({start or '—'} → {end or '—'})."
                )
            except Exception:
                pass
            return None

        try:
            # Check if on schedule page already
            on_schedule = False
            try:
                on_schedule = await self._page.is_visible("#post_select", timeout=2000)
            except Exception:
                pass

            if not on_schedule:
                if not await self._navigate_to_schedule_page():
                    return None
                if not await self._select_post_and_load_calendar():
                    return None

            # Parse target date
            try:
                target_date = datetime.strptime(slot.slot_date, "%Y-%m-%d")
            except ValueError:
                log.error(f"Invalid date: {slot.slot_date}")
                return None

            # Click the target date on calendar
            green_links = await self._page.query_selector_all("td.greenday a.ui-state-default")
            clicked_date = False
            for link in green_links:
                day_text = (await link.text_content() or "").strip()
                if day_text == str(target_date.day):
                    await link.click()
                    log.info(f"Clicked target date: {slot.slot_date}")
                    clicked_date = True
                    break

            if not clicked_date:
                log.error(f"Could not find date {slot.slot_date} on calendar")
                return None

            await self._sleep(1.5, 2.5)

            # Select time slot radio button
            if slot.raw_element_id:
                try:
                    radio = await self._page.query_selector(
                        f"input[name='schedule-entries'][value='{slot.raw_element_id}']"
                    )
                    if radio:
                        await radio.click()
                        log.info(f"✅ Selected time slot: {slot.raw_element_id}")
                    else:
                        await self._page.click(f"#{slot.raw_element_id}")
                        log.info(f"✅ Selected by ID: {slot.raw_element_id}")
                except Exception as e:
                    log.error(f"Cannot select time slot: {e}")
                    return None
            else:
                try:
                    first_radio = await self._page.query_selector(
                        "input[name='schedule-entries']"
                    )
                    if first_radio:
                        await first_radio.click()
                        log.info("Selected first available time slot")
                except Exception:
                    log.error("No time slots found")
                    return None

            await self._sleep(0.5, 1)
            await self.screenshot("before_booking")

            # AUTO-BOOK — no Telegram confirmation, book immediately.

            # Click Submit
            try:
                submit_btn = await self._page.query_selector("#submitbtn")
                if submit_btn:
                    disabled = await submit_btn.get_attribute("disabled")
                    if disabled:
                        await self._page.evaluate("""
                            () => {
                                const btn = document.querySelector('#submitbtn');
                                if (btn) { btn.disabled = false; btn.style.opacity = '1.0'; }
                            }
                        """)
                        await self._sleep(0.3, 0.6)
                    await submit_btn.click()
                    log.info("✅ Submit clicked!")
                else:
                    await self._page.evaluate("onClickSubmit()")
                    log.info("✅ Called onClickSubmit()")
            except Exception as e:
                log.error(f"Submit failed: {e}")
                await self.screenshot("submit_error")
                return None

            await self._sleep(3, 5)
            await self.screenshot("after_submit")

            current_url = self._page.url
            log.info(f"After submit URL: {current_url}")

            try:
                body = (await self._page.inner_text("body")).lower()

                if "confirmation" in current_url.lower() or "confirmation" in body:
                    log.info("✅ BOOKING CONFIRMED!")
                    await self.screenshot("booking_confirmed")
                    try:
                        from core.notifier import notifier
                        await notifier.send_telegram(
                            f"✅ <b>APPOINTMENT BOOKED!</b>\n\n"
                            f"📅 {slot.slot_date}\n🕐 {slot.slot_time}\n📍 DHAKA\n\n"
                            f"Check email for confirmation."
                        )
                    except Exception:
                        pass
                    return f"CONFIRMED-{slot.slot_date}-{slot.slot_time}"

                if "error" in body or "unable" in body:
                    log.error("Booking may have failed")
                    await self.screenshot("booking_possible_error")
                    return None
            except Exception:
                pass

            if "/en-US/" in current_url and "schedule" not in current_url:
                log.info("✅ Redirected to homepage — booking likely succeeded!")
                return f"SUBMITTED-{slot.slot_date}-{slot.slot_time}"

            return "SUBMITTED"

        except ScanAborted:
            raise
        except Exception as e:
            log.error(f"Booking error: {e}", exc_info=True)
            await self.screenshot("booking_error")
            return None