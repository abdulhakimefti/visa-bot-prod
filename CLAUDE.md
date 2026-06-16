# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Automated US Embassy visa appointment scanner targeting `usvisascheduling.com` (CGI Federal portal) for Dhaka, Bangladesh. It monitors the calendar for green (available) appointment dates, notifies via Telegram, and optionally books automatically.

## Running the bot

```powershell
# Windows
.venv\Scripts\activate
python agent.py
```

```bash
# Linux/Mac
source .venv/bin/activate
python agent.py
```

First-time setup (creates venv, installs deps, installs Playwright Chromium):
```bash
./setup.sh
```

Install dependencies manually:
```bash
pip install -r requirements.txt
playwright install chromium
```

## Architecture

### Entry point: `agent.py`
`VisaAgent` is the top-level orchestrator. It:
- Initializes SQLite DB and starts the Telegram bot
- Schedules `run_scan_cycle()` via APScheduler (default 60 min, jittered ±3 min)
- Iterates over configured students, calling `VisaScraper` per student
- Routes booking to either `_book_slot()` (auto) or `_ask_and_book()` (manual confirm)

### Configuration model
Only `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` come from `.env` (`config/settings.py`). **All other config** (portal URL, student credentials, date range, security question answers, etc.) is collected through the Telegram `/setup` wizard and persisted in SQLite `user_config` table. `VisaAgent._load_config()` reads from DB at the start of every scan.

### Core modules

| File | Responsibility |
|---|---|
| `core/scraper.py` | Playwright browser automation against `usvisascheduling.com` |
| `core/database.py` | aiosqlite CRUD — `scan_logs`, `found_slots`, `user_config`, `pending_actions` |
| `core/notifier.py` | Telegram (always) + optional WhatsApp via Twilio |
| `core/logger.py` | Colored console + file logging to `logs/` |
| `bot/telegram_handler.py` | 17-state ConversationHandler setup wizard + YES/NO booking replies |

### Booking decision flow
When `AUTO_BOOK=false`, `VisaAgent._ask_and_book()`:
1. Creates a `pending_actions` DB record and stores an `asyncio.Future` in the module-level `booking_decisions` dict (keyed by action ID)
2. Sends a Telegram message asking `YES <id>` / `NO <id>`
3. `TelegramBot.handle_message()` resolves the Future when user replies
4. 10-minute timeout via `asyncio.wait_for`

### Scraper: what is hardcoded
`core/scraper.py` is specific to the US Embassy portal:
- `HOME_URL` / `SCHEDULE_URL` point to `usvisascheduling.com`
- `DHAKA_POST_VALUE = "906af614-b0db-ec11-a7b4-001dd80234f6"` — the `#post_select` dropdown value for Dhaka
- Available dates are `td.greenday` cells in a jQuery UI datepicker
- Time slots are `input[name='schedule-entries']` radio buttons in `#time_select`
- Browser runs `headless=False` (real Chrome window) to pass Cloudflare

### Security question answers
A fallback `SECURITY_ANSWERS` dict is hardcoded in `scraper.py`. At runtime it is overridden with answers from DB (`security_answers` config key, stored as JSON). The Telegram `/setup` wizard collects keyword+answer pairs for 3 questions. Keywords are matched against the live question text by longest-match substring.

### Temp file IPC
During `book_appointment()`, the scraper waits for a Telegram reply via polling two temp files in the project root:
- `telegram_reply.pending` — signals scraper is waiting
- `telegram_reply.tmp` — contains the user's reply text

This is separate from the `asyncio.Future`-based system used in `_ask_and_book()`.

## Key environment variables (`.env`)

| Variable | Required | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | Your chat ID |

All other config is set via Telegram `/setup`.

## Database (`visa_bot.db`)

SQLite file created at project root. Tables:
- `scan_logs` — audit trail of every scan run
- `found_slots` — slots found, with `booked` flag and `booking_ref`
- `user_config` — key/value store for all runtime config (including student credentials as JSON)
- `pending_actions` — tracks YES/NO booking confirmations
