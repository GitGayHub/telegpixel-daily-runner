"""
Async Telethon runner that executes daily /checkin (or /me balance check)
across all configured Telegram accounts in Gemini Pro Automation Bot (@sheeridverifier_bot).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

from telethon import TelegramClient, errors, functions
from telethon.sessions import StringSession

import telegpixel.env_loader  # noqa: F401
from telegpixel.accounts import AccountRecord
from telegpixel.parser import (
    CheckinStatus,
    ParsedBotResponse,
    extract_balance_info,
    is_broadcast_or_unrelated,
    parse_checkin_reply,
    parse_profile_reply,
)

logger = logging.getLogger("telegpixel.runner")

DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"
DEFAULT_TARGET_BOT = "sheeridverifier_bot"


@dataclass
class AccountRunResult:
    account: AccountRecord
    status: CheckinStatus
    coins_earned: int = 0
    balance: Optional[int] = None
    usd_value: Optional[float] = None
    streak_days: Optional[int] = None
    raw_reply: str = ""
    error_message: Optional[str] = None


class CheckinRunner:
    """Executes /checkin and fetches exact coin balances across multiple Telegram accounts."""

    def __init__(
        self,
        api_id: Optional[int] = None,
        api_hash: Optional[str] = None,
        target_bot: Optional[str] = None,
        reply_timeout: float = 22.0,
        inter_account_delay: float = 2.0,
    ) -> None:
        self.api_id = int(api_id or os.getenv("TELEGRAM_API_ID") or DEFAULT_API_ID)
        self.api_hash = str(api_hash or os.getenv("TELEGRAM_API_HASH") or DEFAULT_API_HASH).strip()
        self.target_bot = (
            target_bot or os.getenv("TARGET_BOT_USERNAME") or DEFAULT_TARGET_BOT
        ).strip().lstrip("@")
        self.reply_timeout = reply_timeout
        self.inter_account_delay = inter_account_delay

    async def _send_and_wait_message(
        self,
        client: TelegramClient,
        bot_entity: object,
        command: str,
    ) -> Tuple[str, Optional[object]]:
        sent = await client.send_message(bot_entity, command)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.reply_timeout

        while loop.time() < deadline:
            await asyncio.sleep(1.0)
            msgs = await client.get_messages(bot_entity, min_id=sent.id, limit=8)
            for msg in reversed(msgs):
                if msg.out:
                    continue
                txt = (msg.raw_text or "").strip()
                if not txt:
                    continue
                if is_broadcast_or_unrelated(txt):
                    continue
                try:
                    await client.send_read_acknowledge(bot_entity, message=msg)
                except Exception:
                    pass
                return txt, msg
        return "", None

    async def _send_and_wait_reply(
        self,
        client: TelegramClient,
        bot_entity: object,
        command: str,
    ) -> str:
        txt, _ = await self._send_and_wait_message(client, bot_entity, command)
        return txt

    @staticmethod
    def _is_join_gate_message(text: str, msg_obj: Optional[object]) -> bool:
        low = (text or "").lower()
        if any(p in low for p in ("присоединиться к каналу", "присоединиться к следующим каналам", "join the following channel", "tham gia kênh")):
            return True
        if msg_obj is not None and getattr(msg_obj, "buttons", None):
            for row in msg_obj.buttons:
                for btn in row:
                    if getattr(btn, "data", None) == b"join_gate_check":
                        return True
        return False

    async def _satisfy_join_gate(self, client: TelegramClient, msg_obj: object, label: str) -> bool:
        """Automatically join required Bot Channel / Bot Group Chat and click '✅ Joined'."""
        if not msg_obj or not getattr(msg_obj, "buttons", None):
            return False

        confirm_btn = None
        for row in msg_obj.buttons:
            for btn in row:
                url = getattr(btn, "url", None)
                data = getattr(btn, "data", None)
                if data == b"join_gate_check" or (not url and "✅" in (getattr(btn, "text", "") or "")):
                    confirm_btn = btn
                elif url and "t.me/" in url:
                    m_inv = re.search(r"t\.me/(?:\+|joinchat/)([A-Za-z0-9_-]+)", url)
                    m_pub = re.search(r"t\.me/([A-Za-z0-9_]{4,})", url)
                    try:
                        if m_inv:
                            inv_hash = m_inv.group(1)
                            logger.info(f"[{label}] Авто-вступление по инвайту t.me/+{inv_hash[:6]}...")
                            await client(functions.messages.ImportChatInviteRequest(hash=inv_hash))
                        elif m_pub:
                            chan = m_pub.group(1)
                            logger.info(f"[{label}] Авто-подписка на @{chan}...")
                            await client(functions.channels.JoinChannelRequest(channel=chan))
                    except errors.UserAlreadyParticipantError:
                        pass
                    except errors.InviteRequestSentError:
                        pass
                    except Exception as e:
                        logger.warning(f"[{label}] Не удалось вступить в {url}: {e}")
                    await asyncio.sleep(1.0)

        if confirm_btn is not None:
            try:
                await confirm_btn.click()
                await asyncio.sleep(1.5)
                return True
            except Exception as e:
                logger.warning(f"[{label}] Ошибка нажатия кнопки подтверждения подписки: {e}")
        return False

    async def process_single_account(
        self,
        account: AccountRecord,
        dry_run: bool = False,
        stats_only: bool = False,
    ) -> AccountRunResult:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        client = TelegramClient(
            session=StringSession(account.session),
            api_id=self.api_id,
            api_hash=self.api_hash,
            device_model="TelegPixel Cloud Runner",
            system_version="Ubuntu Linux",
            app_version="1.0.0",
            receive_updates=False,
        )

        try:
            await asyncio.wait_for(client.connect(), timeout=20.0)
            if not await client.is_user_authorized():
                err = "Сессия не авторизована (истекла или сброшена)"
                self._update_account_failure(account, err, now_iso)
                return AccountRunResult(
                    account=account,
                    status=CheckinStatus.FAILED,
                    balance=account.balance,
                    usd_value=account.usd_value,
                    streak_days=account.streak_days,
                    error_message=err,
                )

            me = await client.get_me()
            if me:
                account.user_id = me.id
                if me.username:
                    account.username = me.username
                if me.first_name:
                    account.first_name = me.first_name.strip()

            bot_entity = await client.get_entity(self.target_bot)

            if dry_run or stats_only:
                reply, msg_obj = await self._send_and_wait_message(client, bot_entity, "/me")
                if self._is_join_gate_message(reply, msg_obj) and msg_obj is not None:
                    await self._satisfy_join_gate(client, msg_obj, account.display_name)
                    reply, _ = await self._send_and_wait_message(client, bot_entity, "/me")
                if not reply:
                    reply = await self._send_and_wait_reply(client, bot_entity, "/start")
                parsed = parse_profile_reply(reply)
                if parsed.balance is not None:
                    account.balance = parsed.balance
                if parsed.usd_value is not None:
                    account.usd_value = parsed.usd_value
                elif account.balance is not None:
                    account.usd_value = round(account.balance * 0.04, 2)
                account.last_checked_at = now_iso
                if parsed.status == CheckinStatus.FAILED:
                    account.last_error = parsed.error_reason
                else:
                    account.last_error = None

                return AccountRunResult(
                    account=account,
                    status=parsed.status,
                    coins_earned=0,
                    balance=account.balance,
                    usd_value=account.usd_value,
                    streak_days=account.streak_days,
                    raw_reply=reply,
                    error_message=parsed.error_reason,
                )

            # Live daily /checkin execution
            reply, msg_obj = await self._send_and_wait_message(client, bot_entity, "/checkin")
            if self._is_join_gate_message(reply, msg_obj) and msg_obj is not None:
                logger.info(f"[{account.display_name}] Бот запросил подписку на канал — выполняю авто-подписку...")
                await self._satisfy_join_gate(client, msg_obj, account.display_name)
                reply, msg_obj = await self._send_and_wait_message(client, bot_entity, "/checkin")

            parsed: ParsedBotResponse = parse_checkin_reply(reply)

            # If the bot was stuck waiting for a SheerID link or giftcode, cancel and retry /checkin once
            if parsed.is_unexpected_prompt:
                logger.info(f"[{account.display_name}] Bot was awaiting previous input; sending /cancel and retrying /checkin...")
                await self._send_and_wait_reply(client, bot_entity, "/cancel")
                await asyncio.sleep(1.0)
                reply = await self._send_and_wait_reply(client, bot_entity, "/checkin")
                parsed = parse_checkin_reply(reply)
            elif parsed.status == CheckinStatus.FAILED and "обслуживании" not in (parsed.error_reason or ""):
                # If the account never started the bot before, send /start once and retry /checkin
                logger.info(f"[{account.display_name}] Initializing bot via /start and retrying /checkin...")
                await self._send_and_wait_reply(client, bot_entity, "/start")
                await asyncio.sleep(1.5)
                reply = await self._send_and_wait_reply(client, bot_entity, "/checkin")
                parsed = parse_checkin_reply(reply)

            # Always ensure we know the exact current coin balance!
            # If /checkin reply didn't include balance (e.g. "Already checked in today!"), query /me
            if parsed.balance is None:
                logger.info(f"[{account.display_name}] Fetching exact coin balance via /me...")
                await asyncio.sleep(1.0)
                me_reply = await self._send_and_wait_reply(client, bot_entity, "/me")
                if not me_reply:
                    me_reply = await self._send_and_wait_reply(client, bot_entity, "/start")
                bal, usd, streak = extract_balance_info(me_reply)
                if bal is not None:
                    parsed.balance = bal
                if usd is not None:
                    parsed.usd_value = usd
                if streak is not None and parsed.streak_days is None:
                    parsed.streak_days = streak

            # Persist updated values onto account record
            if parsed.balance is not None:
                account.balance = parsed.balance
            if parsed.usd_value is not None:
                account.usd_value = parsed.usd_value
            elif account.balance is not None:
                account.usd_value = round(account.balance * 0.04, 2)
            if parsed.streak_days is not None:
                account.streak_days = parsed.streak_days

            account.last_checked_at = now_iso
            account.last_status = parsed.status.value
            if parsed.status == CheckinStatus.CLAIMED:
                account.last_reward_coins = parsed.coins_earned
                account.last_checkin_at = now_iso
                account.last_error = None
            elif parsed.status == CheckinStatus.COOLDOWN:
                account.last_error = None
            else:
                account.last_error = parsed.error_reason

            return AccountRunResult(
                account=account,
                status=parsed.status,
                coins_earned=parsed.coins_earned,
                balance=account.balance,
                usd_value=account.usd_value,
                streak_days=account.streak_days,
                raw_reply=reply,
                error_message=parsed.error_reason,
            )

        except errors.AuthKeyDuplicatedError:
            err = "Конфликт ключа сессии (AuthKeyDuplicated) — пересоздайте сессию"
            self._update_account_failure(account, err, now_iso)
            return AccountRunResult(
                account=account, status=CheckinStatus.FAILED, balance=account.balance, error_message=err
            )
        except errors.FloodWaitError as e:
            err = f"FloodWait от Telegram ({e.seconds} сек.)"
            self._update_account_failure(account, err, now_iso)
            return AccountRunResult(
                account=account, status=CheckinStatus.FAILED, balance=account.balance, error_message=err
            )
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            logger.exception(f"Error processing account {account.display_name}")
            self._update_account_failure(account, err, now_iso)
            return AccountRunResult(
                account=account, status=CheckinStatus.FAILED, balance=account.balance, error_message=err
            )
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass

    @staticmethod
    def _update_account_failure(account: AccountRecord, err: str, now_iso: str) -> None:
        account.last_checked_at = now_iso
        account.last_status = CheckinStatus.FAILED.value
        account.last_error = err

    async def run_all(
        self,
        accounts: List[AccountRecord],
        dry_run: bool = False,
        stats_only: bool = False,
    ) -> List[AccountRunResult]:
        results: List[AccountRunResult] = []
        for idx, acc in enumerate(accounts):
            logger.info(f"[{idx + 1}/{len(accounts)}] Обработка аккаунта {acc.display_name}...")
            res = await self.process_single_account(acc, dry_run=dry_run, stats_only=stats_only)
            results.append(res)
            if idx < len(accounts) - 1 and self.inter_account_delay > 0:
                await asyncio.sleep(self.inter_account_delay)
        return results
