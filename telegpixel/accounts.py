"""
Account & runtime state management for TelegPixel.
Loads accounts from TELEGPIXEL_ACCOUNTS_JSON (GitHub Actions secret) or local data/accounts.json,
and merges non-sensitive runtime coin balances/streaks from data/state.json.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import telegpixel.env_loader  # noqa: F401

logger = logging.getLogger("telegpixel.accounts")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_FILE = PROJECT_ROOT / "data" / "accounts.json"
DEFAULT_STATE_FILE = PROJECT_ROOT / "data" / "state.json"


@dataclass
class AccountRecord:
    user_id: int
    username: str
    session: str
    first_name: str = ""
    phone: str = ""
    enabled: bool = True
    device_model: str = "Desktop"
    system_version: str = "Windows 10"
    app_version: str = "5.8.3 x64"
    # Runtime telemetry (persisted in state.json without sessions)
    balance: Optional[int] = None
    usd_value: Optional[float] = None
    streak_days: Optional[int] = None
    last_reward_coins: int = 0
    last_status: Optional[str] = None
    last_checkin_at: Optional[str] = None
    last_checked_at: Optional[str] = None
    last_error: Optional[str] = None

    @property
    def display_name(self) -> str:
        uname = (self.username or "").strip().lstrip("@")
        if uname and not uname.startswith("id"):
            return f"@{uname}"
        if self.first_name and self.first_name.strip() and self.first_name.strip() != "-":
            return self.first_name.strip()
        if self.phone:
            digits = self.phone.lstrip("+")
            if len(digits) > 6:
                return f"+{digits[:3]}***{digits[-4:]}"
        return f"ID:{self.user_id}"

    def to_secret_dict(self) -> Dict[str, Any]:
        """Minimal dict stored in local accounts.json and GitHub Secret TELEGPIXEL_ACCOUNTS_JSON."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "first_name": self.first_name,
            "phone": self.phone,
            "session": self.session,
            "enabled": self.enabled,
            "device_model": self.device_model,
            "system_version": self.system_version,
            "app_version": self.app_version,
        }

    def to_public_state_dict(self) -> Dict[str, Any]:
        """Non-sensitive state dict safe to cache/commit in CI."""
        return {
            "user_id": self.user_id,
            "username": self.username,
            "first_name": self.first_name,
            "balance": self.balance,
            "usd_value": self.usd_value,
            "streak_days": self.streak_days,
            "last_reward_coins": self.last_reward_coins,
            "last_status": self.last_status,
            "last_checkin_at": self.last_checkin_at,
            "last_checked_at": self.last_checked_at,
            "last_error": self.last_error,
        }


class AccountManager:
    """Loads, updates, and persists multi-account configurations and coin state."""

    def __init__(
        self,
        accounts_file: Optional[Path] = None,
        state_file: Optional[Path] = None,
    ) -> None:
        self.accounts_file = accounts_file or Path(
            os.getenv("TELEGPIXEL_ACCOUNTS_FILE", str(DEFAULT_ACCOUNTS_FILE))
        )
        self.state_file = state_file or Path(
            os.getenv("TELEGPIXEL_STATE_FILE", str(DEFAULT_STATE_FILE))
        )
        self._accounts: List[AccountRecord] = []
        self.load()

    def load(self) -> List[AccountRecord]:
        raw_items: List[Dict[str, Any]] = []

        # 1. Load from TELEGPIXEL_ACCOUNTS_JSON (and optional sharded _1.._5 like ThundrCoin)
        env_chunks: List[str] = []
        base_env = (os.getenv("TELEGPIXEL_ACCOUNTS_JSON") or "").strip()
        if base_env:
            env_chunks.append(base_env)
        for idx in range(1, 6):
            chunk = (os.getenv(f"TELEGPIXEL_ACCOUNTS_JSON_{idx}") or "").strip()
            if chunk:
                env_chunks.append(chunk)

        for chunk in env_chunks:
            try:
                parsed = json.loads(chunk)
                if isinstance(parsed, list):
                    raw_items.extend(parsed)
                elif isinstance(parsed, dict) and isinstance(parsed.get("accounts"), list):
                    raw_items.extend(parsed["accounts"])
            except Exception as e:
                logger.error(f"Failed to parse TELEGPIXEL_ACCOUNTS_JSON: {e}")

        # 2. If not supplied via env, load from local data/accounts.json
        if not raw_items and self.accounts_file.exists():
            try:
                parsed = json.loads(self.accounts_file.read_text(encoding="utf-8"))
                if isinstance(parsed, list):
                    raw_items = parsed
                elif isinstance(parsed, dict) and isinstance(parsed.get("accounts"), list):
                    raw_items = parsed["accounts"]
            except Exception as e:
                logger.error(f"Failed to read {self.accounts_file}: {e}")

        # 3. Load persisted public state (balances, streaks, timestamps)
        state_by_uid: Dict[int, Dict[str, Any]] = {}
        if self.state_file.exists():
            try:
                state_data = json.loads(self.state_file.read_text(encoding="utf-8"))
                for entry in state_data.get("accounts", []):
                    uid = int(entry.get("user_id", 0))
                    if uid:
                        state_by_uid[uid] = entry
            except Exception as e:
                logger.warning(f"Could not read state file {self.state_file}: {e}")

        loaded: List[AccountRecord] = []
        seen_uids: set[int] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            session_str = str(item.get("session") or item.get("string_session") or "").strip()
            if not session_str:
                continue
            uid = int(item.get("user_id") or 0)
            if uid and uid in seen_uids:
                continue
            if uid:
                seen_uids.add(uid)

            st = state_by_uid.get(uid, {})
            rec = AccountRecord(
                user_id=uid,
                username=str(item.get("username") or st.get("username") or f"id{uid}").lstrip("@"),
                session=session_str,
                first_name=str(item.get("first_name") or st.get("first_name") or ""),
                phone=str(item.get("phone") or ""),
                enabled=bool(item.get("enabled", True)),
                device_model=str(item.get("device_model") or "Desktop"),
                system_version=str(item.get("system_version") or "Windows 10"),
                app_version=str(item.get("app_version") or "5.8.3 x64"),
                balance=st.get("balance", item.get("balance")),
                usd_value=st.get("usd_value", item.get("usd_value")),
                streak_days=st.get("streak_days", item.get("streak_days")),
                last_reward_coins=int(st.get("last_reward_coins", item.get("last_reward_coins", 0)) or 0),
                last_status=st.get("last_status", item.get("last_status")),
                last_checkin_at=st.get("last_checkin_at", item.get("last_checkin_at")),
                last_checked_at=st.get("last_checked_at", item.get("last_checked_at")),
                last_error=st.get("last_error", item.get("last_error")),
            )
            loaded.append(rec)

        self._accounts = loaded
        return self._accounts

    def list_accounts(self, enabled_only: bool = True) -> List[AccountRecord]:
        if enabled_only:
            return [a for a in self._accounts if a.enabled]
        return list(self._accounts)

    def upsert_account(self, record: AccountRecord) -> None:
        for i, existing in enumerate(self._accounts):
            if (record.user_id and existing.user_id == record.user_id) or (
                record.username and existing.username.lower() == record.username.lower()
            ):
                # Preserve existing balance telemetry if new record has None
                if record.balance is None:
                    record.balance = existing.balance
                    record.usd_value = existing.usd_value
                    record.streak_days = existing.streak_days
                    record.last_checkin_at = existing.last_checkin_at
                self._accounts[i] = record
                return
        self._accounts.append(record)

    def remove_account(self, identifier: str) -> bool:
        ident = identifier.strip().lstrip("@").lower()
        before = len(self._accounts)
        self._accounts = [
            a
            for a in self._accounts
            if a.username.lower() != ident and str(a.user_id) != ident
        ]
        return len(self._accounts) < before

    def save_accounts(self) -> None:
        """Save local accounts (including StringSessions) to gitignored data/accounts.json."""
        self.accounts_file.parent.mkdir(parents=True, exist_ok=True)
        payload = [a.to_secret_dict() for a in self._accounts]
        self.accounts_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_state(self) -> None:
        """Save non-sensitive coin balances and check-in history to data/state.json."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "accounts": [a.to_public_state_dict() for a in self._accounts],
        }
        self.state_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def export_secret_json(self) -> str:
        """Compact JSON string suitable for GitHub Actions Secret TELEGPIXEL_ACCOUNTS_JSON."""
        return json.dumps([a.to_secret_dict() for a in self._accounts if a.enabled], ensure_ascii=False)
