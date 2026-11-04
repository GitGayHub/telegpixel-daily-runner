"""
Local account import, standalone root login, and safe tdata detach utilities:
1. Extract sessions non-destructively from AyuGram / Telegram Desktop tdata and mint
   independent cloud StringSessions (preventing AuthKeyDuplicatedError on GitHub Actions).
2. Add or re-authorize accounts directly via Phone + Code or Terminal QR Code (creates a
   standalone root session with zero link to AyuGram, so AyuGram is never needed).
3. Detach extra accounts locally from AyuGram's tdata without calling Telegram's server-side
   LogOut (preventing Telegram from cascade-revoking fresh <24h child sessions).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from telethon import TelegramClient, errors, functions, types
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession, StringSession

from telegpixel.accounts import DEFAULT_ACCOUNTS_FILE, AccountRecord
from telegpixel.runner import (
    DEFAULT_API_HASH,
    DEFAULT_API_ID,
    DESKTOP_APP_VERSION,
    DESKTOP_DEVICE_MODEL,
    DESKTOP_SYSTEM_VERSION,
)

logger = logging.getLogger("telegpixel.importer")

DEFAULT_AYUGRAM_TDATA = Path.home() / "Documents" / "AyuGram" / "tdata"
DEFAULT_PENDING_AUTH_DIR = DEFAULT_ACCOUNTS_FILE.parent

DC_ADDRESSES = {
    1: ("149.154.175.53", 443),
    2: ("149.154.167.51", 443),
    3: ("149.154.175.100", 443),
    4: ("149.154.167.91", 443),
    5: ("91.108.56.130", 443),
}


REALISTIC_PC_PROFILES: List[Tuple[str, str, str]] = [
    ("ASUS System Product Name", "Windows 10 x64", "5.8.3 x64"),
    ("MS-7C91", "Windows 10 x64", "5.8.3 x64"),
    ("LENOVO 82FG", "Windows 11 x64", "5.8.3 x64"),
    ("HP Pavilion Laptop 15", "Windows 11 x64", "5.8.3 x64"),
    ("Gigabyte B550M DS3H", "Windows 10 x64", "5.8.3 x64"),
    ("Dell XPS 15 9520", "Windows 11 x64", "5.8.3 x64"),
]


def select_hardware_profile(seed: str) -> Tuple[str, str, str]:
    """Deterministically select a realistic Windows OEM profile per account seed."""
    digest = hashlib.sha256((seed or "default").encode("utf-8")).digest()
    idx = int.from_bytes(digest[:4], "big") % len(REALISTIC_PC_PROFILES)
    return REALISTIC_PC_PROFILES[idx]


def _make_desktop_client(
    session: object,
    api_id: int = DEFAULT_API_ID,
    api_hash: str = DEFAULT_API_HASH,
    receive_updates: bool = False,
    device_model: Optional[str] = None,
    system_version: Optional[str] = None,
    app_version: Optional[str] = None,
) -> TelegramClient:
    return TelegramClient(
        session=session,
        api_id=api_id,
        api_hash=api_hash,
        device_model=device_model or DESKTOP_DEVICE_MODEL,
        system_version=system_version or DESKTOP_SYSTEM_VERSION,
        app_version=app_version or DESKTOP_APP_VERSION,
        lang_code="en",
        system_lang_code="en-US",
        receive_updates=receive_updates,
    )


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D+", "", phone or "")
    return f"+{digits}" if digits else ""


async def check_session_authorized(
    account: AccountRecord,
    api_id: int = DEFAULT_API_ID,
    api_hash: str = DEFAULT_API_HASH,
) -> bool:
    """Return True if the account's saved StringSession is still valid on Telegram servers."""
    if not account.session:
        return False
    client = _make_desktop_client(
        session=StringSession(account.session),
        api_id=api_id,
        api_hash=api_hash,
        receive_updates=False,
        device_model=account.device_model,
        system_version=account.system_version,
        app_version=account.app_version,
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=15.0)
        return bool(await client.is_user_authorized())
    except Exception as e:
        logger.debug(f"Session check failed for {account.display_name}: {e}")
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


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
            "Для работы с папкой AyuGram требуются пакеты opentele и PyQt5 "
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


def is_ayugram_running() -> bool:
    """Check whether AyuGram.exe is currently running on Windows."""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq AyuGram.exe", "/NH"],
            stderr=subprocess.DEVNULL,
        ).decode("cp866", errors="ignore")
        return "ayugram.exe" in out.lower()
    except Exception:
        return False


def close_ayugram() -> bool:
    """Gracefully terminate AyuGram.exe before editing local tdata files."""
    if not is_ayugram_running():
        return True
    subprocess.run(
        ["taskkill", "/IM", "AyuGram.exe", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return not is_ayugram_running()


def detach_accounts_from_tdata(
    tdata_path: Path,
    remove_user_ids: Set[int],
    passcode: str = "",
) -> List[int]:
    """
    Locally remove specific user_ids from AyuGram's key_datas WITHOUT calling Telegram's
    server-side auth.LogOutRequest().
    This keeps child QR sessions alive even if the parent AyuGram session was <24h old!
    """
    try:
        import opentele.td as td
        from PyQt5.QtCore import QBuffer, QByteArray, QDataStream, QIODevice
    except ImportError as e:
        raise RuntimeError("Для локальной отвязки из AyuGram требуются пакеты opentele и PyQt5.") from e

    if is_ayugram_running():
        raise RuntimeError(
            "AyuGram.exe сейчас запущен. Закройте AyuGram (или используйте флаг --close-ayugram), "
            "чтобы он не перезаписал key_datas при выходе."
        )

    if not tdata_path.exists():
        raise FileNotFoundError(f"Папка tdata не найдена: {tdata_path}")

    slots = read_tdata_slots(tdata_path, passcode=passcode)
    slot_to_uid: Dict[int, int] = {idx: uid for idx, uid, _, _ in slots}

    kd = td.Storage.ReadFile("key_data", str(tdata_path))
    orig_version = int(kd.version)
    salt, key_encrypted, info_encrypted = QByteArray(), QByteArray(), QByteArray()
    kd.stream >> salt >> key_encrypted >> info_encrypted

    pass_bytes = QByteArray(passcode.encode("utf-8")) if passcode else QByteArray()
    passcode_key = td.Storage.CreateLocalKey(salt, pass_bytes)
    key_inner = td.Storage.DecryptLocal(key_encrypted, passcode_key)
    local_key = td.AuthKey(key_inner.stream.readRawData(256))

    info = td.Storage.DecryptLocal(info_encrypted, local_key)
    count = info.stream.readInt32()
    indices = [info.stream.readInt32() for _ in range(count)]
    active_index = info.stream.readInt32() if not info.stream.atEnd() else (indices[0] if indices else 0)

    kept_indices: List[int] = []
    removed_indices: List[int] = []
    removed_uids: List[int] = []
    for idx in indices:
        uid = slot_to_uid.get(idx)
        if uid is not None and uid in remove_user_ids:
            removed_indices.append(idx)
            removed_uids.append(uid)
        else:
            kept_indices.append(idx)

    if not removed_indices:
        return []

    if not kept_indices:
        raise RuntimeError(
            "В AyuGram должен остаться хотя бы один основной аккаунт (нельзя удалять все слоты сразу)."
        )

    new_active = active_index if active_index in kept_indices else kept_indices[0]

    # 1. Backup original key_datas
    backup_dir = tdata_path / "_telegpixel_detached"
    backup_dir.mkdir(parents=True, exist_ok=True)
    key_datas_file = tdata_path / "key_datas"
    if key_datas_file.exists():
        shutil.copy2(key_datas_file, backup_dir / "key_datas.bak")

    # 2. Build updated encrypted info block preserving original local_key & TDF version
    desc = td.Storage.EncryptedDescriptor(4 + 4 * len(kept_indices) + 4)
    desc.stream.writeInt32(len(kept_indices))
    for idx in kept_indices:
        desc.stream.writeInt32(idx)
    desc.stream.writeInt32(new_active)
    new_info_encrypted = td.Storage.PrepareEncrypted(desc, local_key)

    payload_qba = QByteArray()
    buf = QBuffer()
    buf.setBuffer(payload_qba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    out_stream = QDataStream(buf)
    out_stream.setVersion(QDataStream.Version.Qt_5_1)
    out_stream << salt << key_encrypted << new_info_encrypted
    buf.close()

    payload_bytes = bytes(payload_qba.data())
    tdf_magic = b"TDF$"
    ver_bytes = orig_version.to_bytes(4, "little")
    md5_input = payload_bytes + len(payload_bytes).to_bytes(4, "little") + ver_bytes + tdf_magic
    digest = hashlib.md5(md5_input).digest()

    key_datas_file.write_bytes(tdf_magic + ver_bytes + payload_bytes + digest)
    for stale_suffix in ("0", "1"):
        stale = tdata_path / f"key_data{stale_suffix}"
        if stale.exists():
            shutil.move(str(stale), str(backup_dir / stale.name))

    # 3. Move detached slot files into _telegpixel_detached so AyuGram stays clean
    for idx in removed_indices:
        data_name = td.Storage.ComposeDataString("data", idx)
        file_part = td.Storage.ToFilePart(td.Storage.ComputeDataNameKey(data_name))
        for suffix in ("s", "0", "1"):
            f = tdata_path / f"{file_part}{suffix}"
            if f.exists():
                shutil.move(str(f), str(backup_dir / f.name))
        folder = tdata_path / file_part
        if folder.exists() and folder.is_dir():
            dest_folder = backup_dir / file_part
            if dest_folder.exists():
                shutil.rmtree(dest_folder, ignore_errors=True)
            shutil.move(str(folder), str(dest_folder))

    return removed_uids


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

    td_client = _make_desktop_client(
        session=td_sess,
        api_id=api_id,
        api_hash=api_hash,
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
        dev_model, sys_ver, app_ver = select_hardware_profile(phone or str(me.id))

        new_client = _make_desktop_client(
            session=StringSession(),
            api_id=api_id,
            api_hash=api_hash,
            receive_updates=False,
            device_model=dev_model,
            system_version=sys_ver,
            app_version=app_ver,
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
                device_model=dev_model,
                system_version=sys_ver,
                app_version=app_ver,
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
    seed: Optional[str] = None,
) -> Optional[AccountRecord]:
    """Interactive QR login from phone (Telegram -> Settings -> Devices -> Link Desktop Device)."""
    dev_model, sys_ver, app_ver = select_hardware_profile(seed or os.urandom(4).hex())
    client = _make_desktop_client(
        session=StringSession(),
        api_id=api_id,
        api_hash=api_hash,
        receive_updates=True,
        device_model=dev_model,
        system_version=sys_ver,
        app_version=app_ver,
    )
    await client.connect()
    try:
        qr = await client.qr_login()
        user = None
        for attempt in range(3):
            if attempt > 0:
                await qr.recreate()
                print_fn("\n🔄 Обновлён QR-код (предыдущий истёк):")
            else:
                print_fn("\n📱 Откройте Telegram на ТЕЛЕФОНЕ -> Настройки -> Устройства -> Подключить устройство:")
            print_fn(render_ascii_qr(qr.url))
            print_fn("⏳ Ожидание сканирования QR-кода с телефона...")
            try:
                user = await qr.wait(timeout=30)
                break
            except asyncio.TimeoutError:
                continue
            except errors.SessionPasswordNeededError:
                for _ in range(3):
                    pwd = password_provider("QR-аккаунт")
                    if not pwd:
                        return None
                    try:
                        user = await client.sign_in(password=pwd)
                        break
                    except errors.PasswordHashInvalidError:
                        print_fn("⚠️ Неверный облачный 2FA-пароль, попробуйте ещё раз.")
                break

        if user is None:
            return None

        session_str = client.session.save()
        return AccountRecord(
            user_id=int(user.id),
            username=user.username or f"id{user.id}",
            session=session_str,
            first_name=(user.first_name or "").strip(),
            phone=user.phone or "",
            enabled=True,
            device_model=dev_model,
            system_version=sys_ver,
            app_version=app_ver,
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
    """Interactive phone + Telegram/SMS code login (creates a standalone root session)."""
    norm_phone = normalize_phone(phone)
    dev_model, sys_ver, app_ver = select_hardware_profile(norm_phone)
    client = _make_desktop_client(
        session=StringSession(),
        api_id=api_id,
        api_hash=api_hash,
        receive_updates=False,
        device_model=dev_model,
        system_version=sys_ver,
        app_version=app_ver,
    )
    await client.connect()
    try:
        await client.send_code_request(norm_phone)
        code = re.sub(r"\D+", "", code_provider().strip())
        try:
            user = await client.sign_in(phone=norm_phone, code=code)
        except errors.SessionPasswordNeededError:
            user = None
            for _ in range(3):
                pwd = password_provider(norm_phone)
                if not pwd:
                    return None
                try:
                    user = await client.sign_in(password=pwd)
                    break
                except errors.PasswordHashInvalidError:
                    logger.warning("Неверный облачный 2FA-пароль — введите актуальный пароль.")
            if user is None:
                return None

        session_str = client.session.save()
        return AccountRecord(
            user_id=int(user.id),
            username=user.username or f"id{user.id}",
            session=session_str,
            first_name=(user.first_name or "").strip(),
            phone=user.phone or norm_phone.lstrip("+"),
            enabled=True,
            device_model=dev_model,
            system_version=sys_ver,
            app_version=app_ver,
        )
    finally:
        await client.disconnect()


def _pending_auth_file(phone: str, pending_dir: Optional[Path] = None) -> Path:
    digits = re.sub(r"\D+", "", phone)
    base = pending_dir or DEFAULT_PENDING_AUTH_DIR
    base.mkdir(parents=True, exist_ok=True)
    return base / f"pending_auth_{digits}.json"


async def send_login_code(
    phone: str,
    pending_dir: Optional[Path] = None,
    api_id: int = DEFAULT_API_ID,
    api_hash: str = DEFAULT_API_HASH,
) -> str:
    """
    Step 1 of two-step standalone login: send login code to phone and persist temporary
    handshake state in gitignored data/pending_auth_<digits>.json.
    """
    norm_phone = normalize_phone(phone)
    if not norm_phone:
        raise ValueError("Некорректный номер телефона.")

    dev_model, sys_ver, app_ver = select_hardware_profile(norm_phone)
    client = _make_desktop_client(
        session=StringSession(),
        api_id=api_id,
        api_hash=api_hash,
        receive_updates=False,
        device_model=dev_model,
        system_version=sys_ver,
        app_version=app_ver,
    )
    await client.connect()
    try:
        sent = await client.send_code_request(norm_phone)
        state = {
            "phone": norm_phone,
            "phone_code_hash": sent.phone_code_hash,
            "session": client.session.save(),
            "device_model": dev_model,
            "system_version": sys_ver,
            "app_version": app_ver,
        }
        _pending_auth_file(norm_phone, pending_dir).write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return type(sent.type).__name__
    finally:
        await client.disconnect()


async def complete_login_code(
    phone: str,
    code: str,
    password: Optional[str] = None,
    pending_dir: Optional[Path] = None,
    api_id: int = DEFAULT_API_ID,
    api_hash: str = DEFAULT_API_HASH,
) -> AccountRecord:
    """
    Step 2 of two-step standalone login: verify 5-digit code (+ optional 2FA password)
    and return a permanent standalone AccountRecord.
    """
    norm_phone = normalize_phone(phone)
    pfile = _pending_auth_file(norm_phone, pending_dir)
    if not pfile.exists():
        raise FileNotFoundError(
            f"Нет активного запроса кода для {norm_phone}. Сначала вызовите send-code."
        )

    state = json.loads(pfile.read_text(encoding="utf-8"))
    dev_model = state.get("device_model") or DESKTOP_DEVICE_MODEL
    sys_ver = state.get("system_version") or DESKTOP_SYSTEM_VERSION
    app_ver = state.get("app_version") or DESKTOP_APP_VERSION
    client = _make_desktop_client(
        session=StringSession(state["session"]),
        api_id=api_id,
        api_hash=api_hash,
        receive_updates=False,
        device_model=dev_model,
        system_version=sys_ver,
        app_version=app_ver,
    )
    await client.connect()
    try:
        clean_code = re.sub(r"\D+", "", code.strip())
        try:
            user = await client.sign_in(
                phone=state["phone"],
                code=clean_code,
                phone_code_hash=state["phone_code_hash"],
            )
        except errors.SessionPasswordNeededError:
            if not password:
                raise RuntimeError(
                    f"Для {norm_phone} включён облачный пароль 2FA — передайте --password."
                )
            user = await client.sign_in(password=password)

        session_str = client.session.save()
        pfile.unlink(missing_ok=True)
        return AccountRecord(
            user_id=int(user.id),
            username=user.username or f"id{user.id}",
            session=session_str,
            first_name=(user.first_name or "").strip(),
            phone=user.phone or norm_phone.lstrip("+"),
            enabled=True,
            device_model=dev_model,
            system_version=sys_ver,
            app_version=app_ver,
        )
    finally:
        await client.disconnect()


async def fetch_telegram_service_code(
    account: AccountRecord,
    max_age_seconds: int = 1800,
) -> Optional[Tuple[str, datetime, str]]:
    """
    Fetch the latest 5-digit Telegram service login code from chat 777000 (Telegram Notifications).
    Allows logging into Telegram on phone or desktop even if the physical SIM card is lost!
    """
    client = _make_desktop_client(
        StringSession(account.session),
        device_model=account.device_model,
        system_version=account.system_version,
        app_version=account.app_version,
    )
    await client.connect()
    try:
        from datetime import timezone
        now_utc = datetime.now(timezone.utc)
        async for msg in client.iter_messages(777000, limit=8):
            if not msg.text:
                continue
            if max_age_seconds > 0 and msg.date:
                age = (now_utc - msg.date).total_seconds()
                if age > max_age_seconds:
                    continue
            match = re.search(
                r'(?:login code|код для входа|код подтверждения|код|code)\D*?(\d{5})',
                msg.text,
                re.IGNORECASE,
            )
            if match:
                return match.group(1), msg.date, msg.text
            all_digits = re.findall(r'\b\d{5}\b', msg.text)
            if all_digits:
                return all_digits[0], msg.date, msg.text
        return None
    finally:
        await client.disconnect()


async def accept_qr_login_token(account: AccountRecord, token_or_url: str) -> bool:
    """
    Accept a Telegram Desktop / Mobile QR login token (tg://login?token=...) using the account's
    active cloud session. Instantly authorizes Telegram on a new device without SMS or codes!
    """
    import base64
    import urllib.parse

    raw = token_or_url.strip()
    if "token=" in raw:
        parsed = urllib.parse.urlparse(raw)
        q = urllib.parse.parse_qs(parsed.query)
        token_str = q.get("token", [""])[0]
    else:
        token_str = raw

    if not token_str:
        raise ValueError("Не передан token для QR-входа.")

    rem = len(token_str) % 4
    if rem:
        token_str += "=" * (4 - rem)
    token_bytes = base64.urlsafe_b64decode(token_str)

    client = _make_desktop_client(
        StringSession(account.session),
        device_model=account.device_model,
        system_version=account.system_version,
        app_version=account.app_version,
    )
    await client.connect()
    try:
        res = await client(functions.auth.AcceptLoginTokenRequest(token=token_bytes))
        return bool(res)
    finally:
        await client.disconnect()


def _patch_opentele() -> None:
    """Monkey-patch opentele to support unlimited accounts and fix recursion & localKey bugs in SaveTData."""
    try:
        import opentele.td as td
        from opentele.td import Account, TDesktop

        td.TDesktop.kMaxAccounts = 100

        def fixed_api_setter(self, value):
            self._Account__api = value

        Account.api = Account.api.setter(fixed_api_setter)
    except Exception as e:
        logger.debug(f"opentele patching skipped or failed: {e}")


async def export_accounts_to_tdata(
    accounts: List[AccountRecord],
    target_dir: Path,
) -> List[Tuple[int, int, int, bytes]]:
    """Convert StringSessions of given accounts into a unified multi-slot tdata directory."""
    _patch_opentele()
    from opentele.api import UseCurrentSession
    from opentele.td import Account, TDesktop

    valid_accounts = [a for a in accounts if a.session]
    if not valid_accounts:
        raise ValueError("Нет аккаунтов с валидными сессиями для экспорта в tdata.")

    target_dir = Path(target_dir).resolve()
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    tdesk = TDesktop()
    for acc in valid_accounts:
        client = _make_desktop_client(
            StringSession(acc.session),
            device_model=acc.device_model,
            system_version=acc.system_version,
            app_version=acc.app_version,
        )
        await client.connect()
        try:
            await Account.FromTelethon(client, flag=UseCurrentSession, owner=tdesk)
        finally:
            await client.disconnect()

    if not tdesk.isLoaded():
        tdesk._TDesktop__generateLocalKey()
    for a in tdesk.accounts:
        a.localKey = tdesk.localKey

    tdesk.SaveTData(str(target_dir))
    return read_tdata_slots(target_dir)


async def export_accounts_to_tdata_archive(
    accounts: List[AccountRecord],
    output_zip_path: Path,
    password: Optional[str] = None,
    include_portable: bool = True,
) -> Path:
    """
    Generate full multi-account tdata + portable standalone folders for each account,
    packaged into a ZIP archive with 1-click RESTORE_TO_AYUGRAM.bat script.
    If password is provided, encrypts with AES-256 (pyzipper).
    """
    _patch_opentele()
    from opentele.api import UseCurrentSession
    from opentele.td import Account, TDesktop

    valid_accounts = [a for a in accounts if a.session]
    if not valid_accounts:
        raise ValueError("Нет аккаунтов с валидными сессиями для создания архива.")

    output_zip_path = Path(output_zip_path).resolve()
    output_zip_path.parent.mkdir(parents=True, exist_ok=True)

    staging_dir = output_zip_path.parent / f"_staging_tdata_{os.getpid()}_{int(time.time())}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)

    try:
        tdata_dir = staging_dir / "tdata"
        tdata_dir.mkdir(parents=True, exist_ok=True)
        portable_dir = staging_dir / "accounts_portable"
        portable_dir.mkdir(parents=True, exist_ok=True)

        tdesk = TDesktop()
        for idx, acc in enumerate(valid_accounts, 1):
            client = _make_desktop_client(
                StringSession(acc.session),
                device_model=acc.device_model,
                system_version=acc.system_version,
                app_version=acc.app_version,
            )
            await client.connect()
            try:
                # Add to multi-account tdesk
                await Account.FromTelethon(client, flag=UseCurrentSession, owner=tdesk)

                # Also generate standalone portable tdata for individual usage
                if include_portable:
                    clean_name = "".join(c if c.isalnum() else "_" for c in acc.display_name).strip("_") or f"acc_{idx}"
                    single_tdesk = await TDesktop.FromTelethon(client, flag=UseCurrentSession)
                    acc_tdata = portable_dir / f"{idx}_{clean_name}_tdata"
                    single_tdesk.SaveTData(str(acc_tdata))
            finally:
                await client.disconnect()

        if not tdesk.isLoaded():
            tdesk._TDesktop__generateLocalKey()
        for a in tdesk.accounts:
            a.localKey = tdesk.localKey

        tdesk.SaveTData(str(tdata_dir))

        # Add 1-click restoration script inside archive
        bat_content = (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "echo ====================================================\r\n"
            "echo   Восстановление сессий TelegPixel в AyuGram Desktop\r\n"
            "echo ====================================================\r\n"
            "echo.\r\n"
            "echo 1. Закрываем запущенный AyuGram...\r\n"
            "taskkill /F /IM AyuGram.exe >nul 2>&1\r\n"
            "taskkill /F /IM Telegram.exe >nul 2>&1\r\n"
            "timeout /t 1 /nobreak >nul\r\n"
            "\r\n"
            "set \"TARGET=%USERPROFILE%\\Documents\\AyuGram\\tdata\"\r\n"
            "if not exist \"%USERPROFILE%\\Documents\\AyuGram\" (\r\n"
            "    mkdir \"%USERPROFILE%\\Documents\\AyuGram\" >nul 2>&1\r\n"
            ")\r\n"
            "if exist \"%TARGET%\" (\r\n"
            "    echo 2. Создаём резервную копию текущего tdata...\r\n"
            "    xcopy \"%TARGET%\" \"%USERPROFILE%\\Documents\\AyuGram\\tdata_backup_%random%\\\" /E /I /Q /Y >nul 2>&1\r\n"
            ")\r\n"
            "\r\n"
            "echo 3. Копируем восстановленные сессии в AyuGram...\r\n"
            "xcopy \"%~dp0tdata\\*\" \"%TARGET%\\\" /E /I /Q /Y\r\n"
            "\r\n"
            "echo.\r\n"
            "echo ====================================================\r\n"
            "echo  [ГОТОВО] Все сессии успешно добавлены в AyuGram!\r\n"
            "echo  Запустите AyuGram — все аккаунты будут онлайн.\r\n"
            "echo ====================================================\r\n"
            "pause\r\n"
        )
        (staging_dir / "RESTORE_TO_AYUGRAM.bat").write_text(bat_content, encoding="utf-8")

        readme_content = (
            "ИНСТРУКЦИЯ ПО ВОССТАНОВЛЕНИЮ СЕССИЙ TELEGRAM / AYUGRAM\n"
            "=======================================================\n\n"
            "1. Быстрое восстановление всех аккаунтов в AyuGram:\n"
            "   Просто дважды кликните на файл 'RESTORE_TO_AYUGRAM.bat'.\n"
            "   Скрипт сам закроет AyuGram (если запущен), сделает бэкап старой папки\n"
            "   и скопирует все сессии в Documents\\AyuGram\\tdata.\n\n"
            "2. Восстановление вручную:\n"
            "   Скопируйте содержимое папки 'tdata' в вашу папку AyuGram:\n"
            "   C:\\Users\\<Имя>\\Documents\\AyuGram\\tdata\n\n"
            "3. Запуск отдельного аккаунта:\n"
            "   В папке 'accounts_portable' находятся отдельные папки tdata для каждого аккаунта.\n"
            "   Вы можете скопировать любую из них в Telegram/AyuGram Portable и зайти конкретно в этот аккаунт.\n"
        )
        (staging_dir / "README.txt").write_text(readme_content, encoding="utf-8")

        # Compress into zip
        if output_zip_path.exists():
            output_zip_path.unlink()

        if password:
            try:
                import pyzipper
                with pyzipper.AESZipFile(
                    output_zip_path,
                    "w",
                    compression=pyzipper.ZIP_DEFLATED,
                    encryption=pyzipper.WZ_AES,
                ) as zf:
                    zf.setpassword(password.encode("utf-8"))
                    for root_p, _, files in os.walk(staging_dir):
                        for f in files:
                            full_f = Path(root_p) / f
                            rel_f = full_f.relative_to(staging_dir)
                            zf.write(full_f, str(rel_f))
            except ImportError:
                import zipfile
                with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for root_p, _, files in os.walk(staging_dir):
                        for f in files:
                            full_f = Path(root_p) / f
                            rel_f = full_f.relative_to(staging_dir)
                            zf.write(full_f, str(rel_f))
        else:
            import zipfile
            with zipfile.ZipFile(output_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for root_p, _, files in os.walk(staging_dir):
                    for f in files:
                        full_f = Path(root_p) / f
                        rel_f = full_f.relative_to(staging_dir)
                        zf.write(full_f, str(rel_f))

        return output_zip_path
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def restore_tdata_archive(
    source_zip_or_dir: Path,
    target_tdata_path: Optional[Path] = None,
    password: Optional[str] = None,
    close_ayugram_first: bool = True,
) -> List[Tuple[int, int, int, bytes]]:
    """
    Extract a downloaded tdata backup zip archive and copy into AyuGram's tdata folder.
    Backs up the current tdata folder first.
    Returns the list of restored slots verified with read_tdata_slots.
    """
    source_path = Path(source_zip_or_dir).resolve()
    target_path = Path(target_tdata_path or DEFAULT_AYUGRAM_TDATA).resolve()

    if close_ayugram_first and is_ayugram_running():
        close_ayugram()

    temp_extract_dir: Optional[Path] = None
    try:
        if source_path.is_file() and source_path.suffix.lower() == ".zip":
            temp_extract_dir = target_path.parent / f"_tdata_extract_{os.getpid()}_{int(time.time())}"
            temp_extract_dir.mkdir(parents=True, exist_ok=True)
            extracted = False
            try:
                import pyzipper
                with pyzipper.AESZipFile(source_path, "r") as zf:
                    if password:
                        zf.setpassword(password.encode("utf-8"))
                    zf.extractall(temp_extract_dir)
                    extracted = True
            except Exception as e:
                logger.debug(f"pyzipper extract failed or not encrypted with AES: {e}")

            if not extracted:
                import zipfile
                with zipfile.ZipFile(source_path, "r") as zf:
                    if password:
                        zf.setpassword(password.encode("utf-8"))
                    zf.extractall(temp_extract_dir)

            search_root = temp_extract_dir
        else:
            search_root = source_path

        # Find tdata directory inside search_root
        candidate_tdata = search_root / "tdata"
        if not candidate_tdata.exists() or not (candidate_tdata / "key_datas").exists():
            if (search_root / "key_datas").exists():
                candidate_tdata = search_root
            else:
                found = list(search_root.glob("**/key_datas"))
                if found:
                    candidate_tdata = found[0].parent
                else:
                    raise FileNotFoundError(
                        f"В архиве {source_path.name} не найдена папка tdata (файл key_datas отсутствует)."
                    )

        # Backup current target_path if it exists
        if target_path.exists():
            backup_dir = target_path.parent / f"tdata_backup_{int(time.time())}"
            shutil.copytree(target_path, backup_dir, dirs_exist_ok=True)
            logger.info(f"Создан бэкап текущего tdata: {backup_dir}")

        target_path.mkdir(parents=True, exist_ok=True)
        # Copy files over
        for item in candidate_tdata.iterdir():
            dest = target_path / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

        return read_tdata_slots(target_path)
    finally:
        if temp_extract_dir and temp_extract_dir.exists():
            shutil.rmtree(temp_extract_dir, ignore_errors=True)


async def restore_accounts_to_ayugram_directly(
    accounts: List[AccountRecord],
    target_tdata_path: Optional[Path] = None,
    close_ayugram_first: bool = True,
) -> List[Tuple[int, int, int, bytes]]:
    """Convert StringSessions directly and write into AyuGram tdata (1-click local restoration)."""
    target_path = Path(target_tdata_path or DEFAULT_AYUGRAM_TDATA).resolve()
    if close_ayugram_first and is_ayugram_running():
        close_ayugram()

    if target_path.exists():
        backup_dir = target_path.parent / f"tdata_backup_{int(time.time())}"
        shutil.copytree(target_path, backup_dir, dirs_exist_ok=True)
        logger.info(f"Создан бэкап текущего tdata: {backup_dir}")

    return await export_accounts_to_tdata(accounts, target_path)

