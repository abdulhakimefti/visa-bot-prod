"""
core/notifier.py — FIXED VERSION
Bug fixes:
  1. notify() always defaults to telegram if channel not set
  2. Added telegram fallback if primary channel fails
  3. Better error logging
"""
import aiohttp
from config.settings import settings
from core.logger import log


class Notifier:

    # ------------------------------------------------------------------ #
    #  Telegram
    # ------------------------------------------------------------------ #

    async def send_telegram(self, text: str, parse_mode: str = "HTML") -> bool:
        token   = settings.telegram_bot_token
        chat_id = settings.telegram_chat_id

        if not token or not chat_id:
            log.error("❌ Telegram NOT configured! Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
            return False

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": parse_mode,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        log.info("✅ Telegram message sent")
                        return True
                    else:
                        log.error(f"❌ Telegram API error: {data.get('description', data)}")
                        return False
        except aiohttp.ClientConnectorError:
            log.error("❌ Telegram: No internet connection or Telegram blocked")
            return False
        except Exception as e:
            log.error(f"❌ Telegram send error: {e}")
            return False

    async def send_telegram_photo(self, photo_path: str, caption: str = "") -> bool:
        token   = settings.telegram_bot_token
        chat_id = settings.telegram_chat_id
        if not token or not chat_id:
            return False
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        try:
            async with aiohttp.ClientSession() as session:
                with open(photo_path, "rb") as f:
                    form = aiohttp.FormData()
                    form.add_field("chat_id", chat_id)
                    form.add_field("caption", caption, content_type="text/plain")
                    form.add_field("photo", f, filename="screenshot.png", content_type="image/png")
                    async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        result = await resp.json()
                        return result.get("ok", False)
        except Exception as e:
            log.error(f"Telegram photo error: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  WhatsApp via Twilio (optional)
    # ------------------------------------------------------------------ #

    async def send_whatsapp(self, text: str) -> bool:
        sid   = getattr(settings, "twilio_account_sid", "")
        token = getattr(settings, "twilio_auth_token", "")
        if not sid or not token:
            log.warning("WhatsApp not configured — skipping")
            return False
        url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
        payload = {
            "From": getattr(settings, "twilio_from", ""),
            "To":   getattr(settings, "whatsapp_to", ""),
            "Body": text,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=payload,
                    auth=aiohttp.BasicAuth(sid, token),
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json()
                    if resp.status == 201:
                        log.info("✅ WhatsApp message sent")
                        return True
                    log.error(f"❌ Twilio error: {data}")
                    return False
        except Exception as e:
            log.error(f"WhatsApp error: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Unified send — FIXED
    # ------------------------------------------------------------------ #

    async def notify(self, text: str, photo_path: str | None = None) -> bool:
        # Always default to telegram if channel not set
        channel = (settings.notify_channel or "telegram").lower().strip()
        results = []

        if channel in ("telegram", "both"):
            results.append(await self.send_telegram(text))
            if photo_path:
                await self.send_telegram_photo(photo_path, caption="📸 Screenshot")

        if channel in ("whatsapp", "both"):
            results.append(await self.send_whatsapp(text))

        # If nothing worked, force telegram as fallback
        if not results or not any(results):
            log.warning("Channel failed — trying Telegram as fallback")
            results.append(await self.send_telegram(text))

        return any(results)

    async def send_urgent_telegram(self, text: str) -> bool:
        """Send with explicit sound + vibration — used for SLOT FOUND alerts."""
        token   = settings.telegram_bot_token
        chat_id = settings.telegram_chat_id
        if not token or not chat_id:
            log.error("Telegram not configured for urgent send")
            return False
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id":              chat_id,
            "text":                 text,
            "parse_mode":           "HTML",
            "disable_notification": False,   # explicit: sound + vibration ON
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                    if data.get("ok"):
                        log.info("✅ Urgent Telegram alert sent")
                        return True
                    log.error(f"❌ Urgent Telegram error: {data.get('description', data)}")
                    return False
        except Exception as e:
            log.error(f"❌ Urgent Telegram send error: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Quick test method
    # ------------------------------------------------------------------ #

    async def test_connection(self) -> bool:
        """Send a test message to verify Telegram is working."""
        log.info("Testing Telegram connection...")
        result = await self.send_telegram(
            "🧪 <b>Connection Test</b>\n"
            "✅ Telegram is working correctly!\n"
            "Your visa bot notifications will appear here."
        )
        if result:
            log.info("✅ Telegram connection test passed!")
        else:
            log.error("❌ Telegram connection test FAILED! Check your token and chat ID.")
        return result


notifier = Notifier()
