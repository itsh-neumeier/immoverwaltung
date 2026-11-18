"""Verschlüsselung sensibler Werte in der Datenbank (Bank-PIN, FinTS-Zustand, 2FA-Schlüssel).

Schlüssel: Umgebungsvariable ``APP_ENCRYPTION_KEY`` (empfohlen, z. B. ``openssl rand -base64 32``) – sonst eine
Schlüsseldatei ``.secret.key`` neben der Datenbank (wird beim ersten Bedarf erzeugt, Rechte 0600). Ohne den
Schlüssel lassen sich die Werte nicht entschlüsseln; ein Datenbank-Backup allein verrät also keine PIN.
"""

from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from .config import config


class SecretError(RuntimeError):
    pass


def _key_file() -> str:
    base = config.data_dir or os.path.dirname(config.database_url.removeprefix("sqlite:///")) or "."
    return os.path.join(base, ".secret.key")


@lru_cache(maxsize=1)
def _fernet() -> Fernet:
    env = os.getenv("APP_ENCRYPTION_KEY", "").strip()
    if env:
        return Fernet(base64.urlsafe_b64encode(hashlib.sha256(env.encode()).digest()))
    path = _key_file()
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    with open(path, "rb") as f:
        return Fernet(f.read().strip())


def reset_cache() -> None:
    _fernet.cache_clear()


def encrypt(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return _fernet().encrypt(data).decode()


def decrypt(token: str) -> bytes:
    try:
        return _fernet().decrypt(token.encode())
    except (InvalidToken, ValueError) as e:
        raise SecretError("Gespeicherter Wert lässt sich nicht entschlüsseln (Schlüssel geändert?).") from e


def decrypt_str(token: str) -> str:
    return decrypt(token).decode()


def maybe_decrypt(token: str) -> Optional[str]:
    if not token:
        return None
    try:
        return decrypt_str(token)
    except SecretError:
        return None
