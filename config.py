from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _csv_ints(name: str) -> tuple[int, ...]:
    result: list[int] = []
    for raw in os.getenv(name, "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            result.append(int(raw))
        except ValueError:
            continue
    return tuple(dict.fromkeys(result))


SESSION_SECRET = os.getenv("SESSION_SECRET", "ramix-music-local-development-secret")
COOKIE_HTTPS_ONLY = _bool("COOKIE_HTTPS_ONLY", False)
ADMIN_EMAILS = tuple(
    item.strip().lower()
    for item in os.getenv("ADMIN_EMAILS", "").split(",")
    if item.strip()
)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = _int("PORT", 8000)
SITE_URL = os.getenv("SITE_URL", f"http://127.0.0.1:{PORT}").rstrip("/")

def _path(name: str, default: Path) -> Path:
    raw = Path(os.getenv(name, str(default))).expanduser()
    if not raw.is_absolute():
        raw = BASE_DIR / raw
    return raw.resolve()


DATA_DIR = _path("DATA_DIR", BASE_DIR / "data")
DATABASE_PATH = _path("DATABASE_PATH", DATA_DIR / "ramix_music.sqlite3")
UPLOAD_ROOT = _path("UPLOAD_ROOT", DATA_DIR / "uploads")
IMPORT_DIR = _path("IMPORT_DIR", DATA_DIR / "imports")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_ENABLED = _bool("BOT_ENABLED", True)
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@")
ADMIN_TELEGRAM_IDS = _csv_ints("ADMIN_TELEGRAM_IDS")
TELEGRAM_POLLING_TIMEOUT = max(10, min(_int("TELEGRAM_POLLING_TIMEOUT", 30), 60))
TELEGRAM_OUTBOX_INTERVAL = max(1, min(_int("TELEGRAM_OUTBOX_INTERVAL", 3), 60))
TELEGRAM_AUTH_TTL_MINUTES = max(3, min(_int("TELEGRAM_AUTH_TTL_MINUTES", 15), 60))
MIN_PAYOUT_RUB = max(100, _int("MIN_PAYOUT_RUB", 1000))

RAILWAY_ENVIRONMENT = os.getenv("RAILWAY_ENVIRONMENT", "").strip()
