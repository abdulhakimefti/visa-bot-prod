"""
bot/telegram_handler.py — BUTTON-BASED UI

User-friendly upgrade:
- /menu shows all actions as tappable buttons
- /accounts lists accounts as 1,2,3 with a Delete button each (no IDs to remember)
- Portal URL step has a "Use default" button
- Auto-book step has YES / NO buttons
- After setup, an "Add another account" button
- Free-text is still used ONLY where it must be (email, password, security
  answers, dates) because those can't be buttons.

All other files (database.py, agent.py, scraper.py) are UNCHANGED.
"""
import asyncio
import json
import re
from datetime import datetime
from telegram import (
    Update, BotCommand,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, ConversationHandler, CallbackQueryHandler,
    TypeHandler, ApplicationHandlerStop,
)
from telegram.constants import ParseMode

from config.settings import settings
from core.database import (
    get_scan_summary, get_pending_action,
    resolve_pending_action, set_config, get_config,
    add_account, remove_account, list_accounts,
)
from core.logger import log

# ── Setup wizard conversation states ────────────────────────────────────── #
(
    PORTAL_URL,
    NUM_ACCOUNTS,
    ACCOUNT_EMAIL,
    ACCOUNT_PASS,
    SEC_Q1_KEYWORD,
    SEC_Q1_ANSWER,
    SEC_Q2_KEYWORD,
    SEC_Q2_ANSWER,
    SEC_Q3_KEYWORD,
    SEC_Q3_ANSWER,
    ACCOUNT_DATE_FROM,
    ACCOUNT_DATE_TO,
    SCAN_INTERVAL,
    AUTO_BOOK,
) = range(14)

# ── Add-account wizard states ────────────────────────────────────────────── #
(
    ADD_EMAIL,
    ADD_PASS,
    ADD_SEC1_KW,
    ADD_SEC1_ANS,
    ADD_SEC2_KW,
    ADD_SEC2_ANS,
    ADD_SEC3_KW,
    ADD_SEC3_ANS,
    ADD_DATE_FROM,
    ADD_DATE_TO,
) = range(14, 24)

DEFAULT_PORTAL = "https://www.usvisascheduling.com/"
# Only this Telegram group can use the bot (your team's private group)
ALLOWED_CHAT_ID = -5547708084

booking_decisions: dict[int, asyncio.Future] = {}
agent_ref = None


def set_agent(agent):
    global agent_ref
    agent_ref = agent

def _is_allowed(update) -> bool:
    """True only if the message/callback comes from the allowed group."""
    try:
        chat_id = update.effective_chat.id
    except Exception:
        return False
    return chat_id == ALLOWED_CHAT_ID
# ── Small helpers to build button layouts ──────────────────────────────── #

def _menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Scan now", callback_data="menu:scan"),
         InlineKeyboardButton("📊 Status", callback_data="menu:status")],
        [InlineKeyboardButton("⏸ Stop", callback_data="menu:stop"),
         InlineKeyboardButton("▶️ Resume", callback_data="menu:resume")],
        [InlineKeyboardButton("👥 Accounts", callback_data="menu:accounts"),
         InlineKeyboardButton("➕ Add account", callback_data="menu:addaccount")],
        [InlineKeyboardButton("⚙️ Full setup", callback_data="menu:setup")],
    ])


def _accounts_markup(accounts: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for a in accounts:
        buttons.append([
            InlineKeyboardButton(
                f"❌ Delete  {a['username'][:25]}",
                callback_data=f"del_acc:{a['id']}"
            )
        ])
    buttons.append([InlineKeyboardButton("➕ Add account", callback_data="menu:addaccount")])
    return InlineKeyboardMarkup(buttons)


def _accounts_text(accounts: list[dict], header="👥 <b>Your accounts:</b>") -> str:
    lines = [header, ""]
    for i, a in enumerate(accounts, 1):
        dfrom = a.get("date_from") or "—"
        dto   = a.get("date_to") or "—"
        lines.append(f"<b>{i}.</b> {a['username']}")
        lines.append(f"     📅 {dfrom} → {dto}")
    return "\n".join(lines)


class TelegramBot:
    def __init__(self):
        self.app: Application | None = None

    # ── /start & /menu ──────────────────────────────────────────────────── #

    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "👋 <b>US Embassy Visa Appointment Bot</b>\n\n"
            "I watch the Dhaka portal and book the moment a slot opens.\n\n"
            "Tap a button below to begin 👇",
            parse_mode=ParseMode.HTML,
            reply_markup=_menu_markup(),
        )

    async def cmd_menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "📋 <b>Main Menu</b>\nTap an action:",
            parse_mode=ParseMode.HTML,
            reply_markup=_menu_markup(),
        )

    # ── Menu button router ──────────────────────────────────────────────── #

    async def on_menu(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        action = query.data.split(":", 1)[1]

        if action == "scan":
            paused = await get_config("agent_paused")
            if paused == "1":
                await query.edit_message_text("⏸ Scanner is paused. Tap ▶️ Resume first.",
                                              reply_markup=_menu_markup())
                return
            if getattr(agent_ref, "_scanning", False):
                await query.edit_message_text("🔄 A scan is already running.",
                                              reply_markup=_menu_markup())
                return
            await query.edit_message_text("🔍 Starting scan now...")
            if agent_ref:
                agent_ref.trigger_scan_now()

        elif action == "status":
            logs = await get_scan_summary(10)
            if not logs:
                await query.edit_message_text("No scans yet. Tap ⚙️ Full setup to start.",
                                              reply_markup=_menu_markup())
                return
            lines = ["📊 <b>Recent Scans:</b>\n"]
            for e in logs:
                icon = "✅" if e["status"] == "found" else ("❌" if e["status"] == "error" else "🔍")
                t = e["scanned_at"][:16].replace("T", " ")
                lines.append(f"  {icon} {t} — {e['status']} ({e['slots_found']} slots)")
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                          reply_markup=_menu_markup())

        elif action == "stop":
            await set_config("agent_paused", "1")
            if agent_ref and getattr(agent_ref, "_scanning", False):
                await query.edit_message_text("⏸ Stopping scan and closing browser...")
                await agent_ref.abort_current_scan()
                await query.edit_message_text("✅ Stopped. Tap ▶️ Resume to start again.",
                                              reply_markup=_menu_markup())
            else:
                await query.edit_message_text("⏸ Scanner paused. Tap ▶️ Resume to continue.",
                                              reply_markup=_menu_markup())

        elif action == "resume":
            await set_config("agent_paused", "0")
            try:
                from agent import clear_all_sessions
                clear_all_sessions()
            except Exception:
                pass
            await query.edit_message_text("▶️ Resumed. Old sessions cleared. Scanning now...",
                                          reply_markup=_menu_markup())
            if agent_ref:
                agent_ref.trigger_scan_now()

        elif action == "accounts":
            accounts = await list_accounts()
            if not accounts:
                await query.edit_message_text(
                    "No accounts yet. Tap ➕ Add account or ⚙️ Full setup.",
                    reply_markup=_menu_markup(),
                )
                return
            await query.edit_message_text(
                _accounts_text(accounts),
                parse_mode=ParseMode.HTML,
                reply_markup=_accounts_markup(accounts),
            )

        elif action == "addaccount":
            # Can't start a ConversationHandler from a callback easily, so we
            # tell the user to tap the command. (Most reliable cross-version.)
            await query.edit_message_text(
                "➕ <b>Add account</b>\n\nSend /addaccount to begin adding one.",
                parse_mode=ParseMode.HTML,
            )

        elif action == "setup":
            await query.edit_message_text(
                "⚙️ <b>Full setup</b>\n\nSend /setup to begin configuration.",
                parse_mode=ParseMode.HTML,
            )

    # ── /status (still works as a command) ──────────────────────────────── #

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        logs = await get_scan_summary(10)
        if not logs:
            await update.message.reply_text("No scans yet. Send /setup to start.")
            return
        lines = ["📊 <b>Recent Scans:</b>\n"]
        for e in logs:
            icon = "✅" if e["status"] == "found" else ("❌" if e["status"] == "error" else "🔍")
            t    = e["scanned_at"][:16].replace("T", " ")
            lines.append(f"  {icon} {t} — {e['status']} ({e['slots_found']} slots)")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await set_config("agent_paused", "1")
        scanning = getattr(agent_ref, "_scanning", False)
        if agent_ref and scanning:
            await update.message.reply_text("⏸ Stopping current scan and closing browser...")
            await agent_ref.abort_current_scan()
            await update.message.reply_text(
                "✅ Scan stopped. Browser closed.\nTap ▶️ Resume to start again.",
                reply_markup=_menu_markup(),
            )
        else:
            await update.message.reply_text(
                "⏸ Scanner <b>paused</b>. Tap ▶️ Resume to continue.",
                parse_mode=ParseMode.HTML, reply_markup=_menu_markup(),
            )

    async def cmd_resume(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        await set_config("agent_paused", "0")
        try:
            from agent import clear_all_sessions
            clear_all_sessions()
        except Exception:
            pass
        await update.message.reply_text(
            "▶️ Scanner <b>resumed</b>. Old sessions cleared. Starting scan now...",
            parse_mode=ParseMode.HTML,
        )
        if agent_ref:
            agent_ref.trigger_scan_now()

    async def cmd_scan(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        paused = await get_config("agent_paused")
        if paused == "1":
            await update.message.reply_text("⏸ Scanner is paused. Send /resume first.")
            return
        if getattr(agent_ref, "_scanning", False):
            await update.message.reply_text("🔄 Scan is already running.")
            return
        await update.message.reply_text("🔍 Starting scan now...")
        if agent_ref:
            agent_ref.trigger_scan_now()

    async def cmd_setminutes(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        """Set which minutes of each hour to scan.
          /setminutes 10 11 12   → scan at :10, :11, :12 every hour
          /setminutes every      → scan every minute (default)
        Takes effect after /stop then /resume (or restart)."""
        text = (update.message.text or "").strip()
        parts = text.split()[1:]   # drop the command itself

        if not parts:
            current = await get_config("scan_minutes")
            mode = f"minutes {current} each hour" if current else "every minute"
            await update.message.reply_text(
                "⏱ <b>Scan timing</b>\n\n"
                f"Current: <b>{mode}</b>\n\n"
                "Set specific minutes (each hour):\n"
                "<code>/setminutes 10 11 12</code>\n\n"
                "Or scan every minute:\n"
                "<code>/setminutes every</code>\n\n"
                "After changing, send /stop then /resume to apply.",
                parse_mode=ParseMode.HTML,
            )
            return

        if parts[0].lower() == "every":
            await set_config("scan_minutes", "")
            await update.message.reply_text(
                "✅ Scan mode: <b>every minute</b>.\n"
                "Send /stop then /resume to apply.",
                parse_mode=ParseMode.HTML,
            )
            return

        minutes = []
        for p in parts:
            if p.isdigit() and 0 <= int(p) <= 59:
                minutes.append(int(p))
        if not minutes:
            await update.message.reply_text(
                "⚠️ Give minutes between 0 and 59, e.g. <code>/setminutes 10 11 12</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        minutes = sorted(set(minutes))
        await set_config("scan_minutes", ",".join(str(m) for m in minutes))
        await update.message.reply_text(
            f"✅ Scan minutes set: <b>{minutes}</b> each hour.\n"
            f"(e.g. {minutes[0]:02d}, {minutes[-1]:02d} past every hour)\n\n"
            "Send /stop then /resume to apply.",
            parse_mode=ParseMode.HTML,
        )

    # ── /cancel ─────────────────────────────────────────────────────────── #

    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        had_data = bool(ctx.user_data)
        ctx.user_data.clear()
        msg = "❌ Wizard cancelled." if had_data else "Nothing to cancel."
        await update.message.reply_text(
            f"{msg} Send /menu for options.",
            reply_markup=_menu_markup(),
        )
        return ConversationHandler.END

    # ── /accounts ───────────────────────────────────────────────────────── #

    async def cmd_accounts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        accounts = await list_accounts()
        if not accounts:
            await update.message.reply_text(
                "No accounts configured yet.\nSend /addaccount or /setup.",
                reply_markup=_menu_markup(),
            )
            return
        await update.message.reply_text(
            _accounts_text(accounts),
            parse_mode=ParseMode.HTML,
            reply_markup=_accounts_markup(accounts),
        )

    async def on_delete_account(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        try:
            account_id = int(query.data.split(":", 1)[1])
        except Exception:
            await query.edit_message_text("⚠️ Invalid delete request.")
            return

        await remove_account(account_id)
        accounts = await list_accounts()
        if not accounts:
            await query.edit_message_text(
                "✅ Account deleted.\n\nNo accounts left. Send /addaccount or /setup.",
                reply_markup=_menu_markup(),
            )
            return
        await query.edit_message_text(
            _accounts_text(accounts, header="✅ Deleted.\n\n👥 <b>Remaining accounts:</b>"),
            parse_mode=ParseMode.HTML,
            reply_markup=_accounts_markup(accounts),
        )

    async def cmd_removeaccount(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = (update.message.text or "").strip()
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await update.message.reply_text("Tip: just send /accounts and tap ❌ Delete.")
            return
        account_id = int(parts[1])
        await remove_account(account_id)
        await update.message.reply_text(f"✅ Account #{account_id} removed.")

    # ── /addaccount wizard ───────────────────────────────────────────────── #

    async def cmd_addaccount(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data.clear()
        ctx.user_data["sec_answers"] = {}
        await update.message.reply_text(
            "➕ <b>Add Account</b>\n\n"
            "Send /cancel anytime to stop.\n\n"
            "📧 Type the portal email / username:",
            parse_mode=ParseMode.HTML,
        )
        return ADD_EMAIL

    async def add_get_email(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["add_email"] = update.message.text.strip()
        await update.message.reply_text("🔑 Type the portal password:")
        return ADD_PASS

    async def add_get_pass(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        pwd = update.message.text.strip()
        if not pwd:
            await update.message.reply_text("⚠️ Password empty. Type the portal password:")
            return ADD_PASS
        ctx.user_data["add_pass"] = pwd
        await update.message.reply_text(
            "🔐 <b>Security Question 1</b>\n"
            "Type a keyword from your 1st security question\n"
            "(e.g. <code>mother</code>, <code>pet</code>, <code>city</code>):",
            parse_mode=ParseMode.HTML,
        )
        return ADD_SEC1_KW

    async def add_sec1_kw(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["add_s1kw"] = update.message.text.strip().lower()
        await update.message.reply_text(
            f"Keyword: <code>{ctx.user_data['add_s1kw']}</code>\n🔑 Type the answer:",
            parse_mode=ParseMode.HTML,
        )
        return ADD_SEC1_ANS

    async def add_sec1_ans(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["sec_answers"][ctx.user_data["add_s1kw"]] = update.message.text.strip()
        await update.message.reply_text(
            "✅ Q1 saved!\n\n🔐 <b>Security Question 2</b>\nType keyword:",
            parse_mode=ParseMode.HTML,
        )
        return ADD_SEC2_KW

    async def add_sec2_kw(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["add_s2kw"] = update.message.text.strip().lower()
        await update.message.reply_text(
            f"Keyword: <code>{ctx.user_data['add_s2kw']}</code>\n🔑 Type the answer:",
            parse_mode=ParseMode.HTML,
        )
        return ADD_SEC2_ANS

    async def add_sec2_ans(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["sec_answers"][ctx.user_data["add_s2kw"]] = update.message.text.strip()
        await update.message.reply_text(
            "✅ Q2 saved!\n\n🔐 <b>Security Question 3</b>\nType keyword:",
            parse_mode=ParseMode.HTML,
        )
        return ADD_SEC3_KW

    async def add_sec3_kw(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["add_s3kw"] = update.message.text.strip().lower()
        await update.message.reply_text(
            f"Keyword: <code>{ctx.user_data['add_s3kw']}</code>\n🔑 Type the answer:",
            parse_mode=ParseMode.HTML,
        )
        return ADD_SEC3_ANS

    async def add_sec3_ans(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["sec_answers"][ctx.user_data["add_s3kw"]] = update.message.text.strip()
        await update.message.reply_text(
            "✅ Q3 saved!\n\n"
            "📅 <b>Date range for this account</b>\n\n"
            "Type date FROM (YYYY-MM-DD):\nExample: <code>2025-08-01</code>",
            parse_mode=ParseMode.HTML,
        )
        return ADD_DATE_FROM

    async def add_date_from(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ds = update.message.text.strip()
        try:
            datetime.strptime(ds, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("⚠️ Use format YYYY-MM-DD. Example: 2025-08-01")
            return ADD_DATE_FROM
        ctx.user_data["add_date_from"] = ds
        await update.message.reply_text(
            "📅 Type date TO (YYYY-MM-DD):\nExample: <code>2025-12-31</code>",
            parse_mode=ParseMode.HTML,
        )
        return ADD_DATE_TO

    async def add_date_to(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ds = update.message.text.strip()
        try:
            datetime.strptime(ds, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("⚠️ Use format YYYY-MM-DD.")
            return ADD_DATE_TO

        email      = ctx.user_data["add_email"]
        sec_ans    = ctx.user_data["sec_answers"]
        password   = ctx.user_data["add_pass"]
        date_from  = ctx.user_data["add_date_from"]
        date_to    = ds

        new_id = await add_account(email, password, sec_ans, date_from, date_to)
        kws = ", ".join(sec_ans.keys())
        await update.message.reply_text(
            f"✅ <b>Account added!</b>\n"
            f"📧 {email}\n"
            f"🔐 Keywords: {kws}\n"
            f"📅 {date_from} → {date_to}\n\n"
            "Send /accounts to see all, or /menu for options.",
            parse_mode=ParseMode.HTML,
            reply_markup=_menu_markup(),
        )
        ctx.user_data.clear()
        return ConversationHandler.END

    # ── /setup wizard ────────────────────────────────────────────────────── #

    async def cmd_setup(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data.clear()
        ctx.user_data["accounts"]    = []
        ctx.user_data["sec_answers"] = {}

        await update.message.reply_text(
            "⚙️ <b>Visa Bot — Setup</b>\n\n"
            "Send /cancel anytime to stop.\n\n"
            "🌐 <b>Step 1: Portal URL</b>\n\n"
            "Tap the button to use the default Bangladesh portal,\n"
            "or type your own URL.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Use default portal", callback_data="setup_default_url")]
            ]),
        )
        return PORTAL_URL

    async def on_setup_default_url(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        ctx.user_data["portal_url"] = DEFAULT_PORTAL
        await query.edit_message_text(
            "✅ Using default portal.\n\n"
            "👥 <b>Step 2:</b> How many accounts do you want to add now?\n"
            "Type a number (e.g. <code>1</code>):",
            parse_mode=ParseMode.HTML,
        )
        return NUM_ACCOUNTS

    async def get_portal_url(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text.lower() == "default":
            text = DEFAULT_PORTAL
        if not text.startswith("http"):
            await update.message.reply_text("⚠️ Must start with http:// — try again or tap the default button.")
            return PORTAL_URL
        ctx.user_data["portal_url"] = text
        await update.message.reply_text(
            "👥 <b>Step 2:</b> How many accounts do you want to add now?\n"
            "Type a number (e.g. <code>1</code>):",
            parse_mode=ParseMode.HTML,
        )
        return NUM_ACCOUNTS

    async def get_num_accounts(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            n = int(update.message.text.strip())
            if n < 1 or n > 50:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Type a number between 1 and 50.")
            return NUM_ACCOUNTS
        ctx.user_data["num_accounts"]    = n
        ctx.user_data["current_account"] = 0
        ctx.user_data["accounts"]        = []
        await update.message.reply_text(
            f"📧 <b>Account 1 of {n}</b>\n\nType portal email / username:",
            parse_mode=ParseMode.HTML,
        )
        return ACCOUNT_EMAIL

    async def get_account_email(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["temp_email"] = update.message.text.strip()
        i = ctx.user_data["current_account"] + 1
        n = ctx.user_data["num_accounts"]
        await update.message.reply_text(
            f"🔑 <b>Account {i} of {n}</b>\n\nType portal password:",
            parse_mode=ParseMode.HTML,
        )
        return ACCOUNT_PASS

    async def get_account_pass(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        pwd = update.message.text.strip()
        if not pwd:
            await update.message.reply_text("⚠️ Password empty. Type the portal password:")
            return ACCOUNT_PASS
        ctx.user_data["temp_pass"] = pwd
        ctx.user_data["sec_answers"] = {}
        i = ctx.user_data["current_account"] + 1
        n = ctx.user_data["num_accounts"]
        await update.message.reply_text(
            f"✅ Credentials for account {i}/{n} noted.\n\n"
            "🔐 <b>Security Questions</b>\n"
            "Type a keyword from your <b>1st</b> security question\n"
            "(e.g. <code>mother</code>, <code>pet</code>, <code>hero</code>):",
            parse_mode=ParseMode.HTML,
        )
        return SEC_Q1_KEYWORD

    async def get_sec_q1_keyword(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["s1kw"] = update.message.text.strip().lower()
        await update.message.reply_text(
            f"Keyword: <code>{ctx.user_data['s1kw']}</code>\n🔑 Type your answer:",
            parse_mode=ParseMode.HTML,
        )
        return SEC_Q1_ANSWER

    async def get_sec_q1_answer(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["sec_answers"][ctx.user_data["s1kw"]] = update.message.text.strip()
        await update.message.reply_text(
            "✅ Q1 saved!\n\n🔐 Keyword for <b>2nd</b> security question:",
            parse_mode=ParseMode.HTML,
        )
        return SEC_Q2_KEYWORD

    async def get_sec_q2_keyword(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["s2kw"] = update.message.text.strip().lower()
        await update.message.reply_text(
            f"Keyword: <code>{ctx.user_data['s2kw']}</code>\n🔑 Type your answer:",
            parse_mode=ParseMode.HTML,
        )
        return SEC_Q2_ANSWER

    async def get_sec_q2_answer(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["sec_answers"][ctx.user_data["s2kw"]] = update.message.text.strip()
        await update.message.reply_text(
            "✅ Q2 saved!\n\n🔐 Keyword for <b>3rd</b> security question:",
            parse_mode=ParseMode.HTML,
        )
        return SEC_Q3_KEYWORD

    async def get_sec_q3_keyword(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["s3kw"] = update.message.text.strip().lower()
        await update.message.reply_text(
            f"Keyword: <code>{ctx.user_data['s3kw']}</code>\n🔑 Type your answer:",
            parse_mode=ParseMode.HTML,
        )
        return SEC_Q3_ANSWER

    async def get_sec_q3_answer(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ctx.user_data["sec_answers"][ctx.user_data["s3kw"]] = update.message.text.strip()
        i = ctx.user_data["current_account"] + 1
        n = ctx.user_data["num_accounts"]
        await update.message.reply_text(
            f"✅ Security questions for account {i}/{n} saved!\n\n"
            "📅 <b>Date range for this account</b>\n\n"
            "Type date FROM (YYYY-MM-DD):\nExample: <code>2025-08-01</code>",
            parse_mode=ParseMode.HTML,
        )
        return ACCOUNT_DATE_FROM

    async def get_date_from(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ds = update.message.text.strip()
        try:
            datetime.strptime(ds, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("⚠️ Use format YYYY-MM-DD. Example: 2025-08-01")
            return ACCOUNT_DATE_FROM
        ctx.user_data["temp_date_from"] = ds
        await update.message.reply_text(
            "📅 Type date TO (YYYY-MM-DD):\nExample: <code>2025-12-31</code>",
            parse_mode=ParseMode.HTML,
        )
        return ACCOUNT_DATE_TO

    async def get_date_to(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        ds = update.message.text.strip()
        try:
            datetime.strptime(ds, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("⚠️ Use format YYYY-MM-DD.")
            return ACCOUNT_DATE_TO
        ctx.user_data["temp_date_to"] = ds

        ctx.user_data["accounts"].append({
            "email":            ctx.user_data["temp_email"],
            "password":         ctx.user_data["temp_pass"],
            "security_answers": dict(ctx.user_data["sec_answers"]),
            "date_from":        ctx.user_data["temp_date_from"],
            "date_to":          ctx.user_data["temp_date_to"],
        })
        ctx.user_data["current_account"] += 1
        current = ctx.user_data["current_account"]
        total   = ctx.user_data["num_accounts"]

        if current < total:
            await update.message.reply_text(
                f"✅ Account {current}/{total} saved!\n\n"
                f"📧 <b>Account {current + 1} of {total}</b>\n\nType email:",
                parse_mode=ParseMode.HTML,
            )
            ctx.user_data["sec_answers"] = {}
            return ACCOUNT_EMAIL

        await update.message.reply_text(
            f"✅ All {total} account(s) saved!\n\n"
            "⏱ <b>Scan interval</b> in minutes?\n"
            "Type a number. Recommended: <code>30</code>",
            parse_mode=ParseMode.HTML,
        )
        return SCAN_INTERVAL

    async def get_scan_interval(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            mins = int(update.message.text.strip())
            if mins < 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("⚠️ Type a number (0 or more).")
            return SCAN_INTERVAL
        ctx.user_data["scan_interval"] = mins
        await update.message.reply_text(
            "🤖 <b>Auto-book when a slot is found?</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ YES — book instantly", callback_data="autobook:yes")],
                [InlineKeyboardButton("❓ NO — ask me first", callback_data="autobook:no")],
            ]),
        )
        return AUTO_BOOK

    async def on_auto_book(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        choice = query.data.split(":", 1)[1]
        ctx.user_data["auto_book"] = (choice == "yes")

        first_acc = ctx.user_data["accounts"][0] if ctx.user_data["accounts"] else {}
        await set_config("portal_url",       ctx.user_data["portal_url"])
        await set_config("visa_type",        "B1/B2")
        await set_config("embassy_location", "Dhaka")
        await set_config("date_from",        first_acc.get("date_from", ""))
        await set_config("date_to",          first_acc.get("date_to", ""))
        await set_config("scan_interval",    str(ctx.user_data["scan_interval"]))
        await set_config("auto_book",        "1" if ctx.user_data["auto_book"] else "0")
        await set_config("agent_paused",     "0")

        for acc in ctx.user_data["accounts"]:
            new_id = await add_account(
                acc["email"], acc["password"], acc["security_answers"],
                acc.get("date_from", ""), acc.get("date_to", ""),
            )
            log.info(f"Account #{new_id} saved: {acc['email']}")

        if ctx.user_data["accounts"]:
            first_sec = ctx.user_data["accounts"][0]["security_answers"]
            await set_config("security_answers", json.dumps(first_sec))
            try:
                from core import scraper as scraper_module
                scraper_module.SECURITY_ANSWERS.update(first_sec)
            except Exception:
                pass

        total = ctx.user_data["num_accounts"]
        acc_lines = "\n".join(
            f"  • {a['email']}  📅 {a['date_from']} → {a['date_to']}"
            for a in ctx.user_data["accounts"]
        )
        await query.edit_message_text(
            "🎉 <b>Setup Complete!</b>\n\n"
            f"📍 Embassy: Dhaka\n"
            f"👥 <b>Accounts ({total}):</b>\n{acc_lines}\n"
            f"⏱ Scan every: {ctx.user_data['scan_interval']} min\n"
            f"🤖 Auto-book: {'Yes' if ctx.user_data['auto_book'] else 'No'}\n\n"
            "🔍 <b>Starting first scan now...</b>",
            parse_mode=ParseMode.HTML,
        )

        try:
            from agent import clear_all_sessions
            clear_all_sessions()
        except Exception:
            pass

        if agent_ref:
            agent_ref.trigger_scan_now()

        ctx.user_data.clear()
        return ConversationHandler.END

    # ── Booking YES/NO replies + security answer relay ───────────────────── #

    async def handle_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        text  = (update.message.text or "").strip().upper()
        match = re.match(r"^(YES|NO)\s+(\d+)$", text)
        if not match:
            from pathlib import Path
            reply_file   = Path(__file__).parent.parent / "telegram_reply.tmp"
            pending_file = Path(__file__).parent.parent / "telegram_reply.pending"
            if pending_file.exists() and not reply_file.exists():
                reply_file.write_text(update.message.text.strip())
                await update.message.reply_text("✅ Answer received! Filling in the form...")
            else:
                await update.message.reply_text(
                    "Send /menu for options, or reply YES/NO <id> for bookings."
                )
            return

        decision, action_id = match.group(1), int(match.group(2))
        action = await get_pending_action(action_id)
        if not action or action["status"] != "pending":
            await update.message.reply_text(f"Action #{action_id} not found or already resolved.")
            return

        status = "confirmed" if decision == "YES" else "rejected"
        await resolve_pending_action(action_id, status)
        fut = booking_decisions.get(action_id)
        if fut and not fut.done():
            fut.set_result(decision == "YES")

        if decision == "YES":
            await update.message.reply_text(
                f"✅ Booking <b>confirmed</b> for #{action_id}. Booking now...",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.message.reply_text("⏭ Slot skipped. Bot continues scanning.")

    # ── Start / Stop ─────────────────────────────────────────────────────── #

    async def start(self):
        if not settings.telegram_bot_token:
            log.error("TELEGRAM_BOT_TOKEN missing!")
            return

        self.app = Application.builder().token(settings.telegram_bot_token).build()

        setup_conv = ConversationHandler(
            entry_points=[CommandHandler("setup", self.cmd_setup)],
            states={
                PORTAL_URL: [
                    CallbackQueryHandler(self.on_setup_default_url, pattern=r"^setup_default_url$"),
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_portal_url),
                ],
                NUM_ACCOUNTS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_num_accounts)],
                ACCOUNT_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_account_email)],
                ACCOUNT_PASS:  [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_account_pass)],
                SEC_Q1_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_sec_q1_keyword)],
                SEC_Q1_ANSWER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_sec_q1_answer)],
                SEC_Q2_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_sec_q2_keyword)],
                SEC_Q2_ANSWER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_sec_q2_answer)],
                SEC_Q3_KEYWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_sec_q3_keyword)],
                SEC_Q3_ANSWER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_sec_q3_answer)],
                ACCOUNT_DATE_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_date_from)],
                ACCOUNT_DATE_TO:   [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_date_to)],
                SCAN_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.get_scan_interval)],
                AUTO_BOOK: [CallbackQueryHandler(self.on_auto_book, pattern=r"^autobook:")],
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
            allow_reentry=True,
        )

        addaccount_conv = ConversationHandler(
            entry_points=[CommandHandler("addaccount", self.cmd_addaccount)],
            states={
                ADD_EMAIL:   [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_get_email)],
                ADD_PASS:    [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_get_pass)],
                ADD_SEC1_KW: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_sec1_kw)],
                ADD_SEC1_ANS:[MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_sec1_ans)],
                ADD_SEC2_KW: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_sec2_kw)],
                ADD_SEC2_ANS:[MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_sec2_ans)],
                ADD_SEC3_KW: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_sec3_kw)],
                ADD_SEC3_ANS:[MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_sec3_ans)],
                ADD_DATE_FROM:[MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_date_from)],
                ADD_DATE_TO: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.add_date_to)],
            },
            fallbacks=[CommandHandler("cancel", self.cmd_cancel)],
            allow_reentry=True,
        )

        # ── GATEKEEPER — block anyone outside the allowed group ──
        from telegram.ext import filters as _f
        async def _block_outsiders(update, ctx):
            # Silently ignore anything not from the allowed group
            return
        # This runs FIRST (group -1) and stops processing for outsiders
        from telegram.ext import TypeHandler
        async def _gate(update, ctx):
            if not _is_allowed(update):
                raise ApplicationHandlerStop
        self.app.add_handler(TypeHandler(Update, _gate), group=-1)

        self.app.add_handler(CommandHandler("start",         self.cmd_start))
        self.app.add_handler(CommandHandler("menu",          self.cmd_menu))
        self.app.add_handler(CommandHandler("scan",          self.cmd_scan))
        self.app.add_handler(CommandHandler("status",        self.cmd_status))
        self.app.add_handler(CommandHandler("stop",          self.cmd_stop))
        self.app.add_handler(CommandHandler("resume",        self.cmd_resume))
        self.app.add_handler(CommandHandler("accounts",      self.cmd_accounts))
        self.app.add_handler(CommandHandler("removeaccount", self.cmd_removeaccount))
        self.app.add_handler(CommandHandler("setminutes",    self.cmd_setminutes))
        self.app.add_handler(setup_conv)
        self.app.add_handler(addaccount_conv)
        # Button handlers (outside conversations)
        self.app.add_handler(CallbackQueryHandler(self.on_delete_account, pattern=r"^del_acc:"))
        self.app.add_handler(CallbackQueryHandler(self.on_menu, pattern=r"^menu:"))
        self.app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        await self.app.bot.set_my_commands([
            BotCommand("menu",          "Show the button menu"),
            BotCommand("scan",          "Start a scan right now"),
            BotCommand("stop",          "Stop scan + close browser"),
            BotCommand("resume",        "Resume scanning"),
            BotCommand("status",        "Recent scan history"),
            BotCommand("accounts",      "List / delete accounts"),
            BotCommand("addaccount",    "Add a new account"),
            BotCommand("setup",         "Full setup wizard"),
            BotCommand("setminutes",    "Set scan minutes (e.g. 10 11 12)"),
            BotCommand("start",         "Welcome & menu"),
            BotCommand("cancel",        "Cancel current wizard"),
        ])

        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started ✓")

    async def stop(self):
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()


telegram_bot = TelegramBot()