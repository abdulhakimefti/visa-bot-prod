"""
core/captcha_solver.py
Handles reCAPTCHA v2/v3 via 2Captcha API.
Falls back gracefully if no API key is set.
"""
import aiohttp
import asyncio
from core.logger import log


class CaptchaSolver:
    BASE_URL = "https://2captcha.com"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.enabled = bool(api_key)

    async def solve_recaptcha_v2(self, site_key: str, page_url: str) -> str | None:
        """Submit reCAPTCHA v2 and return token string."""
        if not self.enabled:
            log.warning("No CAPTCHA API key set — skipping CAPTCHA solving")
            return None

        log.info("Submitting reCAPTCHA v2 to 2captcha...")
        async with aiohttp.ClientSession() as session:
            # Step 1: submit
            submit_url = (
                f"{self.BASE_URL}/in.php?key={self.api_key}"
                f"&method=userrecaptcha&googlekey={site_key}"
                f"&pageurl={page_url}&json=1"
            )
            async with session.get(submit_url) as resp:
                data = await resp.json()
                if data.get("status") != 1:
                    log.error(f"CAPTCHA submit failed: {data}")
                    return None
                captcha_id = data["request"]

            log.info(f"CAPTCHA submitted (id={captcha_id}), waiting for solution...")

            # Step 2: poll for result (up to 120s)
            for _ in range(24):
                await asyncio.sleep(5)
                result_url = (
                    f"{self.BASE_URL}/res.php?key={self.api_key}"
                    f"&action=get&id={captcha_id}&json=1"
                )
                async with session.get(result_url) as resp:
                    result = await resp.json()
                    if result.get("status") == 1:
                        log.info("✅ CAPTCHA solved!")
                        return result["request"]
                    if result.get("request") == "ERROR_CAPTCHA_UNSOLVABLE":
                        log.error("CAPTCHA unsolvable")
                        return None

        log.error("CAPTCHA solving timed out")
        return None

    async def solve_recaptcha_v3(self, site_key: str, page_url: str, action: str = "verify") -> str | None:
        """Submit reCAPTCHA v3."""
        if not self.enabled:
            return None
        async with aiohttp.ClientSession() as session:
            submit_url = (
                f"{self.BASE_URL}/in.php?key={self.api_key}"
                f"&method=userrecaptcha&version=v3&googlekey={site_key}"
                f"&pageurl={page_url}&action={action}&min_score=0.3&json=1"
            )
            async with session.get(submit_url) as resp:
                data = await resp.json()
                if data.get("status") != 1:
                    return None
                captcha_id = data["request"]

            for _ in range(24):
                await asyncio.sleep(5)
                result_url = (
                    f"{self.BASE_URL}/res.php?key={self.api_key}"
                    f"&action=get&id={captcha_id}&json=1"
                )
                async with session.get(result_url) as resp:
                    result = await resp.json()
                    if result.get("status") == 1:
                        return result["request"]
        return None
