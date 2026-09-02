from __future__ import annotations

import json
import os
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TelegramError(RuntimeError):
    """Telegram notification failed."""


class TelegramNotifier:
    """Optional free Telegram notifier. No token is stored in the repo."""

    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = (token or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
        self.chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID", "")).strip()

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, message: str, *, timeout_seconds: float = 10.0) -> None:
        if not self.configured:
            raise TelegramError("Telegram is not configured")
        if not message.strip():
            raise TelegramError("message is empty")

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = urlencode({"chat_id": self.chat_id, "text": message}).encode("utf-8")
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise TelegramError(f"Telegram send failed: {exc}") from exc

        if not payload.get("ok"):
            raise TelegramError(f"Telegram rejected message: {payload!r}")
