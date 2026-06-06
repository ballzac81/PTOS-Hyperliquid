"""
Optional Telegram notifications.
Set TELEGRAM_TOKEN and TELEGRAM_CHAT_ID to enable.
If not set, all send() calls are no-ops.
"""

import os
import logging
import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self):
        self.token   = os.environ.get("TELEGRAM_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        if self.enabled:
            logger.info("Telegram notifications enabled")
        else:
            logger.info("Telegram notifications disabled (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID not set)")

    def send(self, text: str):
        if not self.enabled:
            return
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            resp = requests.post(url, json={"chat_id": self.chat_id, "text": text}, timeout=5)
            resp.raise_for_status()
        except Exception as e:
            logger.warning(f"Telegram send failed: {e}")
