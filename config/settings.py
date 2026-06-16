"""
config/settings.py
Only loads Telegram credentials from .env.
Everything else is collected via Telegram conversation and stored in DB.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # These are the ONLY static values needed
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id:   str = os.getenv("TELEGRAM_CHAT_ID", "")

    # These are populated dynamically from DB during scan
    portal_url:       str = ""
    portal_username:  str = ""
    portal_password:  str = ""
    visa_type:        str = "Tourist"
    embassy_location: str = ""
    num_applicants:   int = 1
    date_from:        str = ""
    date_to:          str = ""
    scan_interval_minutes: int = 10
    auto_book:        bool = False
    captcha_api_key:  str = ""
    notify_channel:   str = "telegram"


settings = Settings()
