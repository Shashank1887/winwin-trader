"""Fail-safe Telegram Bot alert sender and .env persistence."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import dotenv_values, set_key


@dataclass
class TelegramConfig:
    """Telegram Bot alert configuration."""

    bot_token: str = ""
    chat_id: str = ""
    enabled: bool = True

    def is_ready(self) -> bool:
        return bool(self.enabled and self.bot_token.strip() and self.chat_id.strip())


def load_telegram_config(env_path: str | Path = ".env") -> TelegramConfig:
    """Load Telegram Bot credentials from .env or environment variables."""
    path = Path(env_path)
    vals = dotenv_values(path) if path.exists() else {}
    token = str(vals.get("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")).strip()
    chat_id = str(vals.get("TELEGRAM_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
    enabled_raw = str(vals.get("TELEGRAM_ENABLED") or os.getenv("TELEGRAM_ENABLED", "true")).strip().lower()
    enabled = enabled_raw not in {"0", "false", "no", "off"}
    return TelegramConfig(bot_token=token, chat_id=chat_id, enabled=enabled)


def save_telegram_config(cfg: TelegramConfig, env_path: str | Path = ".env") -> None:
    """Persist Telegram Bot credentials to .env."""
    path = Path(env_path)
    if not path.exists():
        path.write_text("", encoding="utf-8")
    set_key(str(path), "TELEGRAM_BOT_TOKEN", cfg.bot_token.strip())
    set_key(str(path), "TELEGRAM_CHAT_ID", cfg.chat_id.strip())
    set_key(str(path), "TELEGRAM_ENABLED", "true" if cfg.enabled else "false")


def send_telegram_message(
    message: str,
    cfg: TelegramConfig | None = None,
    timeout: float = 6.0,
) -> tuple[bool, str]:
    """Send a Telegram alert synchronously and return (ok, status_detail)."""
    config = cfg or load_telegram_config()
    if not config.enabled:
        return False, "Telegram alerts are disabled."
    if not config.bot_token.strip() or not config.chat_id.strip():
        return False, "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required."

    url = f"https://api.telegram.org/bot{config.bot_token.strip()}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": config.chat_id.strip(), "text": message},
            timeout=timeout,
        )
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("ok"):
            return True, "Telegram alert sent."
        desc = data.get("description") or f"HTTP {resp.status_code}"
        return False, f"Telegram API error: {desc}"
    except Exception as exc:
        return False, f"Telegram request failed: {exc}"


def notify_async(message: str, cfg: TelegramConfig | None = None) -> None:
    """Dispatch a Telegram alert in a background daemon thread without blocking trades."""
    config = cfg or load_telegram_config()
    if not config.is_ready():
        return

    def _worker() -> None:
        send_telegram_message(message, cfg=config)

    threading.Thread(target=_worker, name="winwin-tg-notify", daemon=True).start()
