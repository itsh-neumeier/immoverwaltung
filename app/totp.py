"""Zwei-Faktor-Anmeldung mit Einmalcodes (TOTP, RFC 6238) aus einer Authenticator-App
(z. B. Google Authenticator, Microsoft Authenticator, Aegis, 2FAS, 1Password, Bitwarden).

6 Ziffern, 30 Sekunden, SHA-1 – kompatibel mit allen gängigen Apps. Dazu 10 Wiederherstellungscodes (je einmal
verwendbar), falls das Handy verloren geht.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import secrets
import struct
import time
from typing import Optional
from urllib.parse import quote

DIGITS = 6
STEP = 30
WINDOW = 1  # ± 1 Zeitschritt (Uhrabweichung des Handys)


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def code_at(secret: str, counter: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def now_counter(t: Optional[float] = None) -> int:
    return int((time.time() if t is None else t) // STEP)


def verify(secret: str, code: str, last_counter: int = 0, t: Optional[float] = None) -> Optional[int]:
    """Gültiger Code → verwendeter Zeitschritt (für den Schutz vor Wiederverwendung), sonst None."""
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != DIGITS or not secret:
        return None
    base = now_counter(t)
    for c in range(base - WINDOW, base + WINDOW + 1):
        if c > (last_counter or 0) and hmac.compare_digest(code_at(secret, c), code):
            return c
    return None


def uri(secret: str, account: str, issuer: str = "ImmoVerwaltung") -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits={DIGITS}&period={STEP}"


def qr_svg(text: str) -> str:
    """QR-Code als SVG (ohne Bildbibliothek)."""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.find("<svg"):]


# --------------------------------------------------------------------------- Wiederherstellungscodes
def _norm(code: str) -> str:
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def _hash(code: str) -> str:
    return hashlib.sha256(_norm(code).encode()).hexdigest()


def new_recovery_codes(n: int = 10) -> tuple[list[str], list[str]]:
    """→ (Codes zum Anzeigen, Hashes zum Speichern)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codes = []
    for _ in range(n):
        raw = "".join(secrets.choice(alphabet) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes, [_hash(c) for c in codes]


def use_recovery(code: str, hashes: list) -> Optional[list]:
    """Passender Code → restliche Hashes (Code verbraucht), sonst None."""
    if len(_norm(code)) != 10:
        return None
    h = _hash(code)
    for stored in hashes or []:
        if hmac.compare_digest(stored, h):
            return [x for x in hashes if x != stored]
    return None
