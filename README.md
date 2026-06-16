# 🛂 Visa Appointment Bot

Automated visa appointment scanner that monitors your embassy portal and notifies you (via Telegram or WhatsApp) the moment a slot opens — then books it for you with one reply.

---

## 📁 Project Structure

```
visa_bot/
├── agent.py                  ← Main entry point (run this)
├── requirements.txt          ← Python dependencies
├── setup.sh                  ← One-command installer
├── .env.example              ← Config template → copy to .env
│
├── config/
│   └── settings.py           ← Loads all .env variables
│
├── core/
│   ├── scraper.py            ← Playwright browser automation ⚠️ CUSTOMIZE
│   ├── notifier.py           ← Telegram + WhatsApp notifications
│   ├── database.py           ← SQLite async DB (scan logs, slots)
│   ├── captcha_solver.py     ← 2Captcha API integration
│   └── logger.py             ← Colored console + file logging
│
├── bot/
│   └── telegram_handler.py   ← Telegram bot commands & replies
│
├── logs/                     ← Auto-created log files
└── screenshots/              ← Auto-created booking screenshots
```

---

## ⚡ Quick Start

### Step 1 — Run setup
```bash
cd visa_bot
chmod +x setup.sh
./setup.sh
```

### Step 2 — Configure .env
```bash
cp .env.example .env
nano .env   # or use any text editor
```

Fill in:
| Variable | Description |
|---|---|
| `PORTAL_URL` | Full URL of the visa appointment portal |
| `PORTAL_USERNAME` | Your login email/username |
| `PORTAL_PASSWORD` | Your portal password |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) on Telegram |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID (get from [@userinfobot](https://t.me/userinfobot)) |
| `VISA_TYPE` | e.g. `Tourist`, `Business`, `Student` |
| `EMBASSY_LOCATION` | Embassy city/name as shown on portal |
| `PREFERRED_DATE_FROM` | Start of your preferred date range (YYYY-MM-DD) |
| `PREFERRED_DATE_TO` | End of your preferred date range (YYYY-MM-DD) |
| `SCAN_INTERVAL_MINUTES` | How often to scan (default: `60`) |
| `AUTO_BOOK` | `true` = book automatically, `false` = ask you first |
| `CAPTCHA_API_KEY` | Optional: your [2captcha.com](https://2captcha.com) key |

### Step 3 — Customize selectors ⚠️ REQUIRED
Open `core/scraper.py` and update the HTML selectors to match your specific visa portal. Every portal is different. See **Customization Guide** below.

### Step 4 — Run
```bash
source .venv/bin/activate
python agent.py
```

---

## 🤖 Telegram Bot Commands

Once running, control the bot from Telegram:

| Command | Action |
|---|---|
| `/start` | Show welcome & instructions |
| `/status` | See last 10 scan results |
| `/config` | View current configuration |
| `/stop` | Pause the scanner |
| `/resume` | Resume the scanner |
| `YES <id>` | Confirm booking when slot is found |
| `NO <id>` | Skip the slot, continue scanning |

---

## 🔧 Customization Guide (Scraper)

The `core/scraper.py` file contains `⚠️` markers at every section you need to customize. Here's how:

### 1. Find login selectors
- Open your visa portal in Chrome
- Right-click the username field → **Inspect**
- Note the `id`, `name`, or `class` of the input
- Update `username_selectors` list in `login()` method

### 2. Find appointment navigation
- After logging in, go to the booking page
- Note the menu link text or URL path
- Update `menu_selectors` in `scan_appointments()`

### 3. Find available date selectors
- Open the calendar page
- Right-click an **available** date → Inspect
- Note what CSS class marks it as available (e.g., `class="available"`)
- Update `available_day_selectors` in `scan_appointments()`

### 4. Check for API endpoints (advanced)
- Open Chrome DevTools → Network tab → XHR
- Navigate the calendar
- Look for JSON responses containing slot data
- If found, you can replace the DOM scraping with a direct API call (much faster!)

---

## 🛡️ Anti-Detection Features

The bot includes several measures to avoid being blocked:
- **Human-like typing** with random delays between keystrokes
- **Randomized scan intervals** (±5 minutes from configured interval)
- **Real browser fingerprint** (not detected as headless)
- **Cookie handling** for session persistence
- **Random sleep** between page interactions

---

## 📊 How It Works (Flow)

```
START
  │
  ├─ Load config from .env
  ├─ Initialize SQLite database
  ├─ Start Telegram bot (polling)
  ├─ Send startup notification
  │
  └─ SCAN LOOP (every N minutes)
       │
       ├─ Check if paused (via Telegram /stop)
       ├─ Launch Chromium browser
       ├─ Login to portal
       │    ├─ Handle cookies
       │    ├─ Solve CAPTCHA (if configured)
       │    └─ Fill username + password
       │
       ├─ Navigate to appointment section
       ├─ Select visa type, location, applicants
       ├─ Scan calendar for available dates
       │
       ├─ NO slots → log + sleep → repeat
       │
       └─ SLOTS FOUND
            ├─ Save to database
            ├─ Send Telegram/WhatsApp alert
            │
            ├─ AUTO_BOOK=true → Book immediately
            │
            └─ AUTO_BOOK=false → Wait for reply
                 ├─ "YES <id>" → Book + send confirmation
                 ├─ "NO <id>"  → Skip + continue scanning
                 └─ Timeout (10 min) → Skip + continue
```

---

## 🚨 CAPTCHA Setup (Optional but Recommended)

Most visa portals use reCAPTCHA. To handle it:

1. Sign up at [2captcha.com](https://2captcha.com) (~$3 per 1000 solves)
2. Get your API key
3. Add to `.env`: `CAPTCHA_API_KEY=your_key_here`

The bot will automatically detect and solve reCAPTCHA v2 and v3.

---

## 📱 WhatsApp Setup (Optional)

1. Create a [Twilio](https://twilio.com) account
2. Enable the WhatsApp Sandbox in Twilio console
3. Add to `.env`:
   ```
   TWILIO_ACCOUNT_SID=ACxxxxxxxx
   TWILIO_AUTH_TOKEN=xxxxxxxx
   TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
   WHATSAPP_TO=whatsapp:+8801XXXXXXXXX
   NOTIFY_CHANNEL=whatsapp
   ```

---

## ⚖️ Legal & Ethical Notice

- Use this bot only for **your own** visa appointment booking
- Do not use it to hoard multiple slots simultaneously
- Respect the portal's Terms of Service
- The bot scans at ≥60 minute intervals to minimize server load

---

## 🐛 Troubleshooting

| Problem | Solution |
|---|---|
| Login fails | Check credentials in `.env`, update selectors in `scraper.py` |
| No slots detected | Update `available_day_selectors` to match your portal's HTML |
| CAPTCHA blocks login | Add a `CAPTCHA_API_KEY` to `.env` |
| Telegram not working | Verify `BOT_TOKEN` and `CHAT_ID` are correct |
| Bot gets blocked | Increase `SCAN_INTERVAL_MINUTES` to 120+ |

Check `logs/visa_bot.log` and `screenshots/` for debugging info.
