"""
Parser for responses from Gemini Pro Automation Bot (@sheeridverifier_bot).
Supports English, Russian, and Vietnamese bot locales across /checkin, /me, and /start.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class CheckinStatus(str, Enum):
    CLAIMED = "claimed"
    COOLDOWN = "cooldown"
    CHECKED = "checked"
    FAILED = "failed"


@dataclass
class ParsedBotResponse:
    status: CheckinStatus
    coins_earned: int = 0
    balance: Optional[int] = None
    usd_value: Optional[float] = None
    streak_days: Optional[int] = None
    raw_text: str = ""
    error_reason: Optional[str] = None
    is_unexpected_prompt: bool = False


_EARNED_RE = re.compile(
    r"(?:💎\s*\+?|\+)\s*(\d+)\s*(?:coin|coins|монет[аы]?|xu)\b",
    re.IGNORECASE,
)

_BALANCE_LABEL_RE = re.compile(
    r"(?:Balance|Баланс|Текущее количество монет|Current\s+coins?|Số\s+dư)\s*:\s*(\d+)",
    re.IGNORECASE,
)

_BALANCE_COIN_FALLBACK_RE = re.compile(
    r"(?:💰|💳)[^\n\d]{0,35}(\d+)\s*(?:coin|coins|монет[аы]?|xu)\b",
    re.IGNORECASE,
)

_USD_RE = re.compile(
    r"(?:💰|💳|Balance|Баланс|монет[аы]?|coin|coins|xu)[^\n$]{0,30}\$\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)

_STREAK_RE = re.compile(
    r"(?:Date\s+series|Серия\s+дат|Streak|Серия|Chuỗi\s+ngày)\s*:\s*(\d+)",
    re.IGNORECASE,
)

_CLAIMED_PHRASES = (
    "attendance success",
    "посещаемость успех",
    "điểm danh thành công",
    "check-in successful",
    "checkin successful",
    "успешно отметились",
)

_COOLDOWN_PHRASES = (
    "already",
    "уже отме",
    "уже получ",
    "уже посещ",
    "сегодня уже",
    "приходите завтра",
    "come back tomorrow",
    "checked in today",
    "attended today",
    "attendance today",
    "once a day",
    "раз в день",
    "завтра",
    "tomorrow",
    "hôm nay",
    "ngày mai",
    "wait until",
    "подождите",
)

_UNEXPECTED_PROMPT_PHRASES = (
    "send your sheerid link",
    "send verification link",
    "отправьте ссылку sheerid",
    "пришлите ссылку",
    "enter your redeem code",
    "введите подарочный код",
    "gửi link sheerid",
)

_MAINTENANCE_PHRASES = (
    "under maintenance",
    "undergo maintenance",
    "на техническом обслуживании",
    "временно недоступен",
    "bảo trì",
)

_JOIN_GATE_PHRASES = (
    "присоединиться к каналу",
    "присоединиться к следующим каналам",
    "join the following channel",
    "must join",
    "tham gia kênh",
)


def extract_balance_info(text: str) -> tuple[Optional[int], Optional[float], Optional[int]]:
    """Extract (balance_coins, usd_value, streak_days) from any bot message (/checkin, /me, /start)."""
    if not text:
        return None, None, None

    balance: Optional[int] = None
    m_bal = _BALANCE_LABEL_RE.search(text) or _BALANCE_COIN_FALLBACK_RE.search(text)
    if m_bal:
        try:
            balance = int(m_bal.group(1))
        except ValueError:
            balance = None

    usd_val: Optional[float] = None
    # Prefer USD value on the same line as Balance if possible
    for line in text.splitlines():
        if any(k in line.lower() for k in ("balance", "баланс", "текущее количество монет", "số dư")):
            m_line_usd = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", line)
            if m_line_usd:
                try:
                    usd_val = float(m_line_usd.group(1))
                    break
                except ValueError:
                    pass
    if usd_val is None:
        m_usd = _USD_RE.search(text)
        if m_usd:
            try:
                usd_val = float(m_usd.group(1))
            except ValueError:
                usd_val = None

    streak: Optional[int] = None
    m_streak = _STREAK_RE.search(text)
    if m_streak:
        try:
            streak = int(m_streak.group(1))
        except ValueError:
            streak = None

    return balance, usd_val, streak


def is_broadcast_or_unrelated(text: str) -> bool:
    """Return True if an incoming bot message looks like a mass promo broadcast rather than a command reply."""
    if not text:
        return True
    low = text.lower()
    if any(
        marker in low
        for marker in (
            "official announcement",
            "broadcast message",
            "marketplace",
            "shop.appmmo",
            "exclusive offers",
            "best regards,\nthe bot management team",
        )
    ):
        return True
    return False


def parse_checkin_reply(text: str) -> ParsedBotResponse:
    """Parse the bot's reply to /checkin."""
    if not text or not text.strip():
        return ParsedBotResponse(
            status=CheckinStatus.FAILED,
            raw_text=text or "",
            error_reason="Пустой ответ от бота",
        )

    cleaned = text.strip()
    low = cleaned.lower()
    balance, usd_val, streak = extract_balance_info(cleaned)

    m_earned = _EARNED_RE.search(cleaned)
    earned = int(m_earned.group(1)) if m_earned else 0

    if any(p in low for p in _UNEXPECTED_PROMPT_PHRASES):
        return ParsedBotResponse(
            status=CheckinStatus.FAILED,
            balance=balance,
            usd_value=usd_val,
            streak_days=streak,
            raw_text=cleaned,
            error_reason="Бот ожидал ввод от предыдущей команды",
            is_unexpected_prompt=True,
        )

    if any(p in low for p in _CLAIMED_PHRASES) or ("🎉" in cleaned and earned > 0):
        return ParsedBotResponse(
            status=CheckinStatus.CLAIMED,
            coins_earned=earned if earned > 0 else 1,
            balance=balance,
            usd_value=usd_val,
            streak_days=streak,
            raw_text=cleaned,
        )

    if any(p in low for p in _COOLDOWN_PHRASES):
        return ParsedBotResponse(
            status=CheckinStatus.COOLDOWN,
            coins_earned=0,
            balance=balance,
            usd_value=usd_val,
            streak_days=streak,
            raw_text=cleaned,
        )

    if any(p in low for p in _JOIN_GATE_PHRASES):
        return ParsedBotResponse(
            status=CheckinStatus.FAILED,
            balance=balance,
            usd_value=usd_val,
            streak_days=streak,
            raw_text=cleaned,
            error_reason="Требуется подписка на канал бота (join gate)",
        )

    if any(p in low for p in _MAINTENANCE_PHRASES):
        return ParsedBotResponse(
            status=CheckinStatus.FAILED,
            balance=balance,
            usd_value=usd_val,
            streak_days=streak,
            raw_text=cleaned,
            error_reason="Бот на техническом обслуживании",
        )

    # Heuristic: if the message clearly isn't claimed and starts with ⚠️ / ❌ / ⏳ / ℹ️, treat as cooldown
    if cleaned.startswith(("⚠️", "❌", "⏳", "ℹ️", "🚫", "⏰")) and earned == 0:
        return ParsedBotResponse(
            status=CheckinStatus.COOLDOWN,
            coins_earned=0,
            balance=balance,
            usd_value=usd_val,
            streak_days=streak,
            raw_text=cleaned,
        )

    return ParsedBotResponse(
        status=CheckinStatus.FAILED,
        coins_earned=earned,
        balance=balance,
        usd_value=usd_val,
        streak_days=streak,
        raw_text=cleaned,
        error_reason=f"Неизвестный ответ: {cleaned.splitlines()[0][:80]}",
    )


def parse_profile_reply(text: str) -> ParsedBotResponse:
    """Parse the bot's reply to /me or /start to read current coin balance."""
    if not text or not text.strip():
        return ParsedBotResponse(
            status=CheckinStatus.FAILED,
            raw_text=text or "",
            error_reason="Пустой ответ на /me",
        )
    cleaned = text.strip()
    balance, usd_val, streak = extract_balance_info(cleaned)
    if balance is not None:
        return ParsedBotResponse(
            status=CheckinStatus.CHECKED,
            balance=balance,
            usd_value=usd_val,
            streak_days=streak,
            raw_text=cleaned,
        )
    return ParsedBotResponse(
        status=CheckinStatus.FAILED,
        raw_text=cleaned,
        error_reason=f"Баланс не найден в ответе: {cleaned.splitlines()[0][:80]}",
    )
