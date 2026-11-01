"""
Local account import & login utilities:
1. Extract sessions non-destructively from AyuGram / Telegram Desktop tdata and mint
   independent cloud StringSessions (preventing AuthKeyDuplicatedError on GitHub Actions).
2. Add extra accounts directly via Terminal QR Code or Phone + Code.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

from telethon import TelegramClient, errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession, StringSession

from telegpixel.accounts import AccountRecord
from telegpixel.runner import DEFAULT_API_HASH, DEFAULT_API_ID

logger = logging.getLogger("telegpixel.importer")

DEFAULT_AYUGRAM_TDATA = Path.home() / "Documents" / "AyuGram" / "tdata"

DC_ADDRESSES = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}


def read_tdata_slots(tdata_path: Path, passcode: str = "") -> List[Tuple[int, int, int, bytes]]:
    """
    Read raw (slot_index, user_id, main_dc_id, main_auth_key_256b) from an AyuGram / TDesktop tdata folder.
    Does not touch map/stickers files (compatible with modern AyuGram versions) and never modifies tdata.
    """
    try:
        import opentele.td as td
        from PyQt5.QtCore import QByteArray, QDataStream
    except ImportError as e:
        raise RuntimeError(
            "Для авто-импорта из папки AyuGram требуются пакеты opentele и PyQt5 "
            "(pip install opentele PyQt5)."
        ) from e

    if not tdata_path.exists():
        raise FileNotFoundError(f"Папка tdata не найдена: {tdata_path}")

    key_data = td.Storage.ReadFile("key_data", str(tdata_path))
    salt, key_encrypted, info_encrypted = QByteArray(), QByteArray(), QByteArray()
    key_data.stream >> salt >> key_encrypted >> info_encrypted

    pass_bytes = QByteArray(passcode.encode("utf-8")) if passcode else QByteArray()
    passcode_key = td.Storage.CreateLocalKey(salt, pass_bytes)
    key_inner = td.Storage.DecryptLocal(key_encrypted, passcode_key)
    local_key = td.AuthKey(key_inner.stream.readRawData(256))

    info = td.Storage.DecryptLocal(info_encrypted, local_key)
    count = info.stream.readInt32()
    indices = [info.stream.readInt32() for _ in range(count)]

    slots: List[Tuple[int, int, int, bytes]] = []
    for idx in indices:
        data_name = td.Storage.ComposeDataString("data", idx)
        data_name_key = td.Storage.ComputeDataNameKey(data_name)
        file_part = td.Storage.ToFilePart(data_name_key)
        mtp = td.Storage.ReadEncryptedFile(file_part, str(tdata_path), local_key)
        _block_id = mtp.stream.readInt32()
        serialized = QByteArray()
        mtp.stream >> serialized

        stream = QDataStream(serialized)
        stream.setVersion(QDataStream.Version.Qt_5_1)
        uid = stream.readInt32()
        dc = stream.readInt32()
        if uid == -1 and dc == -1:
            uid = stream.readUInt64()
            dc = stream.readInt32()

        key_count = stream.readInt32()
        main_key: Optional[bytes] = None
        for _ in range(key_count):
            k_dc = stream.readInt32()
            k_bytes = stream.readRawData(256)
            if k_dc == dc:
                main_key = k_bytes
        if main_key:
            slots.append((idx, int(uid), int(dc), main_key))

    return slots


async def mint_cloud_session_from_slot(
    dc: int,
    key_bytes: bytes,
    api_id: int = DEFAULT_API_ID,
    api_hash: str = DEFAULT_API_HASH,
    password_provider: Optional[Callable[[str], Optional[str]]] = None,
) -> Optional[AccountRecord]:
    """
    Use a local tdata slot (same PC IP) to authorize a brand-new independent StringSession
    suitable for GitHub Actions without risking AuthKeyDuplicatedError.
    """
    td_sess = MemorySession()
    ip, port = DC_ADDRESSES[dc]
    td_sess.set_dc(dc, ip, port)
    td_sess.auth_key = AuthKey(data=key_bytes)

    td_client = TelegramClient(
        session=td_sess,
        api_id=api_id,
        api_hash=api_hash,
        device_model="Desktop",
        system_version="Windows 11",
        app_version="5.8.3 x64",
        receive_updates=False,
    )
    await td_client.connect()
    try:
        if not await td_client.is_user_authorized():
            logger.warning("Слот в AyuGram не авторизован — пропуск.")
            return None

        me = await td_client.get_me()
        uname = me.username or f"id{me.id}"
        fname = (me.first_name or "").strip()
        phone = me.phone or ""
        display = f"@{me.username}" if me.username else (fname or f"ID:{me.id}")

        new_client = TelegramClient(
            session=StringSession(),
            api_id=api_id,
            api_hash=api_hash,
            device_model="TelegPixel Cloud Runner",
            system_version="Ubuntu Linux",
            app_version="1.0.0",
            receive_updates=False,
        )
        await new_client.connect()
        try:
            export_res = await new_client(
                functions.auth.ExportLoginTokenRequest(
                    api_id=api_id,
                    api_hash=api_hash,
                    except_ids=[],
                )
            )
            await td_client(functions.auth.AcceptLoginTokenRequest(token=export_res.token))

            try:
                final_res = await new_client(
                    functions.auth.ExportLoginTokenRequest(
                        api_id=api_id,
                        api_hash=api_hash,
                        except_ids=[],
                    )
                )
                if isinstance(final_res, types.auth.LoginTokenMigrateTo):
                    await new_client._switch_dc(final_res.dc_id)
                    final_res = await new_client(
                        functions.auth.ImportLoginTokenRequest(token=final_res.token)
                    )

                if isinstance(final_res, types.auth.LoginTokenSuccess):
                    await new_client._on_login(final_res.authorization.user)
                else:
                    raise RuntimeError(f"Неожиданный ответ авторизации: {type(final_res).__name__}")

            except errors.SessionPasswordNeededError:
                if password_provider is None:
                    logger.warning(f"Для {display} включён облачный пароль 2FA — нужен ввод пароля.")
                    return None
                pwd = password_provider(display)
                if not pwd:
                    logger.warning(f"Пароль 2FA для {display} не введён — пропуск.")
                    return None
                await new_client.sign_in(password=pwd)

            session_str = new_client.session.save()
            return AccountRecord(
                user_id=int(me.id),
                username=uname,
                session=session_str,
                first_name=fname,
                phone=phone,
                enabled=True,
            )
        finally:
            await new_client.disconnect()
    finally:
        await td_client.disconnect()


async def import_from_ayugram_tdata(
    tdata_path: Optional[Path] = None,
    existing_user_ids: Optional[Set[int]] = None,
    password_provider: Optional[Callable[[str], Optional[str]]] = None,
    force_reimport: bool = False,
) -> Tuple[List[AccountRecord], List[int]]:
    """
    Import all accounts found in AyuGram tdata.
    Returns (imported_records, skipped_2fa_user_ids).
    """
    path = tdata_path or DEFAULT_AYUGRAM_TDATA
    existing = existing_user_ids or set()
    slots = read_tdata_slots(path)

    imported: List[AccountRecord] = []
    needs_2fa: List[int] = []

    for slot_idx, uid, dc, key_bytes in slots:
        if not force_reimport and uid in existing:
            logger.info(f"Аккаунт ID {uid} (слот {slot_idx}) уже импортирован — пропуск.")
            continue
        rec = await mint_cloud_session_from_slot(
            dc=dc,
            key_bytes=key_bytes,
            password_provider=password_provider,
        )
        if rec is not None:
            imported.append(rec)
        else:
            needs_2fa.append(uid)

    return imported, needs_2fa


def render_ascii_qr(url: str) -> str:
    """Render a compact Unicode half-block QR code for terminal scanning."""
    import qrcode

    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    lines: List[str] = []
    for r in range(0, len(matrix), 2):
        row_top = matrix[r]
        row_bot = matrix[r + 1] if r + 1 < len(matrix) else [False] * len(row_top)
        chars: List[str] = []
        for top, bot in zip(row_top, row_bot):
            if top and bot:
                chars.append("█")
            elif top and not bot:
                chars.append("▀")
            elif not top and bot:
                chars.append("▄")
            else:
                chars.append(" ")
        lines.append("".join(chars))
    return "\n".join(lines)


async def login_account_via_qr(
    password_provider: Callable[[str], Optional[str]],
    print_fn: Callable[[str], None] = print,
    api_id: int = DEFAULT_API_ID,
    api_hash: str = DEFAULT_API_HASH,
) -> Optional[AccountRecord]:
    """Interactive QR login from phone (Telegram -> Settings -> Devices -> Link Desktop Device)."""
    client = TelegramClient(
        session=StringSession(),
        api_id=api_id,
        api_hash=api_hash,
        device_model="TelegPixel Cloud Runner",
        system_version="Ubuntu Linux",
        app_version="1.0.0",
        receive_updates=True,
    )
    await client.connect()
    try:
        qr = await client.qr_login()
        print_fn("\n📱 Откройте Telegram на телефоне -> Настройки -> Устройства -> Подключить устройство:")
        print_fn(render_ascii_qr(qr.url))
        print_fn("⏳ Ожидание сканирования QR-кода (60 сек)...")
        try:
            user = await qr.wait(timeout=60)
        except errors.SessionPasswordNeededError:
            pwd = password_provider("QR-аккаунт")
            if not pwd:
                return None
            user = await client.sign_in(password=pwd)

        session_str = client.session.save()
        return AccountRecord(
            user_id=int(user.id),
            username=user.username or f"id{user.id}",
            session=session_str,
            first_name=(user.first_name or "").strip(),
            phone=user.phone or "",
            enabled=True,
        )
    finally:
        await client.disconnect()


async def login_account_via_phone(
    phone: str,
    code_provider: Callable[[], str],
    password_provider: Callable[[str], Optional[str]],
    api_id: int = DEFAULT_API_ID,
    api_hash: str = DEFAULT_API_HASH,
) -> Optional[AccountRecord]:
    """Interactive phone + Telegram/SMS code login."""
    client = TelegramClient(
        session=StringSession(),
        api_id=api_id,
        api_hash=api_hash,
        device_model="TelegPixel Cloud Runner",
        system_version="Ubuntu Linux",
        app_version="1.0.0",
        receive_updates=False,
    )
    await client.connect()
    try:
        await client.send_code_request(phone)
        code = code_provider().strip()
        try:
            user = await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            pwd = password_provider(phone)
            if not pwd:
                return None
            user = await client.sign_in(password=pwd)

        session_str = client.session.save()
        return AccountRecord(
            user_id=int(user.id),
            username=user.username or f"id{user.id}",
            session=session_str,
            first_name=(user.first_name or "").strip(),
            phone=user.phone or phone.lstrip("+"),
            enabled=True,
        )
    finally:
        await client.disconnect()
