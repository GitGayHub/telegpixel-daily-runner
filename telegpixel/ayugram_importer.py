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
import hashlib
import json
import logging
import re
import shutil
import subprocess
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


def _make_desktop_client(
    session: object,
    api_id: int = DEFAULT_API_ID,
    api_hash: str = DEFAULT_API_HASH,
    receive_updates: bool = False,
) -> TelegramClient:
    return TelegramClient(
        session=session,
        api_id=api_id,
        api_hash=api_hash,
        device_model=DESKTOP_DEVICE_MODEL,
        system_version=DESKTOP_SYSTEM_VERSION,
        app_version=DESKTOP_APP_VERSION,
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

        new_client = _make_desktop_client(
            session=StringSession(),
            api_id=api_id,
            api_hash=api_hash,
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
    client = _make_desktop_client(
        session=StringSession(),
        api_id=api_id,
        api_hash=api_hash,
        receive_updates=True,
    )
    await client.connect()
    try:
        qr = await client.qr_login()
        print_fn("\n📱 Откройте Telegram на ТЕЛЕФОНЕ -> Настройки -> Устройства -> Подключить устройство:")
        print_fn(render_ascii_qr(qr.url))
        print_fn("⏳ Ожидание сканирования QR-кода (90 сек)...")
        try:
            user = await qr.wait(timeout=90)
        except errors.SessionPasswordNeededError:
            user = None
            for _ in range(3):
                pwd = password_provider("QR-аккаунт")
                if not pwd:
                    return None
                try:
                    user = await client.sign_in(password=pwd)
                    break
                except errors.PasswordHashInvalidError:
                    print_fn("⚠️ Неверный облачный 2FA-пароль, попробуйте ещё раз.")
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
    client = _make_desktop_client(
        session=StringSession(),
        api_id=api_id,
        api_hash=api_hash,
        receive_updates=False,
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

    client = _make_desktop_client(
        session=StringSession(),
        api_id=api_id,
        api_hash=api_hash,
        receive_updates=False,
    )
    await client.connect()
    try:
        sent = await client.send_code_request(norm_phone)
        state = {
            "phone": norm_phone,
            "phone_code_hash": sent.phone_code_hash,
            "session": client.session.save(),
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
    client = _make_desktop_client(
        session=StringSession(state["session"]),
        api_id=api_id,
        api_hash=api_hash,
        receive_updates=False,
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
        )
    finally:
        await client.disconnect()
