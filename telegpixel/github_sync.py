"""
Syncs TelegPixel secrets (TELEGPIXEL_ACCOUNTS_JSON, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
directly to GitHub Actions Secrets via `gh` CLI (or GitHub REST API + PyNaCl fallback).
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
from typing import Dict, Optional

import requests
from nacl import encoding, public

import telegpixel.env_loader  # noqa: F401

logger = logging.getLogger("telegpixel.github_sync")


def detect_github_repo() -> Optional[str]:
    env_repo = (os.getenv("GITHUB_REPOSITORY") or "").strip()
    if env_repo:
        return env_repo
    try:
        out = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        if "github.com" in out:
            part = out.split("github.com")[-1].lstrip(":/").removesuffix(".git")
            if "/" in part:
                return part
    except Exception:
        pass
    return None


def set_secret_via_gh_cli(repo: str, name: str, value: str) -> bool:
    if not shutil.which("gh"):
        return False
    try:
        proc = subprocess.run(
            ["gh", "secret", "set", name, "--repo", repo],
            input=value,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=20,
        )
        if proc.returncode == 0:
            return True
        logger.warning(f"gh secret set failed for {name}: {proc.stderr.strip()}")
    except Exception as e:
        logger.warning(f"gh CLI error setting {name}: {e}")
    return False


def set_secret_via_rest_api(repo: str, name: str, value: str, token: str) -> bool:
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "TelegPixel-Secret-Updater",
    }
    pk_resp = requests.get(
        f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
        headers=headers,
        timeout=15,
    )
    pk_resp.raise_for_status()
    pk_data = pk_resp.json()

    pub_key = public.PublicKey(pk_data["key"].encode("utf-8"), encoding.Base64Encoder())
    encrypted = public.SealedBox(pub_key).encrypt(value.encode("utf-8"))
    encrypted_b64 = base64.b64encode(encrypted).decode("utf-8")

    put_resp = requests.put(
        f"https://api.github.com/repos/{repo}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": encrypted_b64, "key_id": pk_data["key_id"]},
        timeout=15,
    )
    return put_resp.status_code in (201, 204)


def sync_secrets_to_github(secrets_map: Dict[str, str], repo: Optional[str] = None) -> Dict[str, bool]:
    target_repo = repo or detect_github_repo()
    if not target_repo:
        raise RuntimeError("Не удалось определить GitHub репозиторий (укажите --repo owner/name).")

    gh_token = (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN") or "").strip()
    outcomes: Dict[str, bool] = {}

    for key, val in secrets_map.items():
        if not val:
            continue
        ok = set_secret_via_gh_cli(target_repo, key, val)
        if not ok and gh_token:
            ok = set_secret_via_rest_api(target_repo, key, val, gh_token)
        outcomes[key] = ok

    return outcomes
