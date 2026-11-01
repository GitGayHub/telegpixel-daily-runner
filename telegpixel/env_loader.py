"""Automatic .env loader for TelegPixel."""

from __future__ import annotations

from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

if ENV_FILE.exists():
    # override=True ensures project .env always wins over global Windows env vars (e.g. eBay bot token)
    load_dotenv(dotenv_path=ENV_FILE, override=True)
