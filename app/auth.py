"""Anmeldung: Passwort-Hashes, signierte Sitzungs-Cookies, Passwort-Reset-Tokens, Sperre bei Fehlversuchen.

Ohne Zusatzbibliotheken (hashlib/hmac). Rollen: ``admin`` (Verwalter, alles) und ``tenant`` (Mieter,
nur Mieterportal der eigenen Partei). Solange kein Benutzer existiert, ist die App offen (wie bisher ohne
Login) – beim Start wird aus APP_USER/APP_PASSWORD automatisch der erste Verwalter angelegt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from .config import config
from .db import User, get_settings, save_settings

COOKIE = "nk_session"
ITERATIONS = 600_000
MIN_PASSWORD = 8
ROLES = {"superadmin": "Super-Admin", "admin": "Verwalter", "tenant": "Mieter"}
ADMIN_ROLES = ("superadmin", "admin")


@dataclass(frozen=True)
class CurrentUser:
    """Schnappschuss des angemeldeten Benutzers (ohne DB-Sitzung nutzbar, z. B. in Templates)."""

    id: int
    username: str
    name: str
    email: str
    role: str
    party_id: Optional[int]
    building_ids: tuple = ()
    totp: bool = False  # Zwei-Faktor-Anmeldung eingerichtet

    @property
    def is_admin(self) -> bool:
        """Verwalter oder Super-Admin (Zugang zu /admin)."""
        return self.role in ADMIN_ROLES

    @property
    def is_super(self) -> bool:
        return self.role == "superadmin"

    def can_building(self, building_id) -> bool:
        """Super-Admin: alle Gebäude; Verwalter: nur zugewiesene."""
        return self.is_super or (self.is_admin and building_id in self.building_ids)

    @property
    def display(self) -> str:
        return self.name or self.username


def snapshot(u: User) -> CurrentUser:
    return CurrentUser(u.id, u.username, u.name or "", u.email or "", u.role, u.party_id,
                       tuple(int(x) for x in (u.building_ids or [])), bool(u.totp_enabled))


# --------------------------------------------------------------------------- Passwörter
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, AttributeError):
        return False


def password_problem(password: str) -> str:
    if len(password) < MIN_PASSWORD:
        return f"Passwort muss mindestens {MIN_PASSWORD} Zeichen haben."
    return ""


def set_password(u: User, password: str) -> None:
    u.password_hash = hash_password(password)
    u.session_version = (u.session_version or 1) + 1  # andere Sitzungen abmelden
    u.reset_hash, u.reset_expires = "", None


# --------------------------------------------------------------------------- Sitzungen
def _secret(s: Session) -> bytes:
    if config.app_secret_key:
        return config.app_secret_key.encode()
    st = get_settings(s)
    if not st["session_secret"]:
        st["session_secret"] = secrets.token_urlsafe(32)
        save_settings(s, {"session_secret": st["session_secret"]})
        s.commit()
    return st["session_secret"].encode()


def _sign(secret: bytes, msg: str) -> str:
    return hmac.new(secret, msg.encode(), hashlib.sha256).hexdigest()[:40]


def make_cookie(s: Session, u: User, days: float) -> str:
    msg = f"{u.id}.{int(time.time() + days * 86400)}.{u.session_version or 1}"
    return base64.urlsafe_b64encode(f"{msg}.{_sign(_secret(s), msg)}".encode()).decode()


REMEMBER_DAYS = 400  # Höchstwert, den Chrome/Android für Cookies zulassen – wird bei Nutzung erneuert
RENEW_BELOW_DAYS = 380


def cookie_expiry(value: str) -> Optional[int]:
    try:
        return int(base64.urlsafe_b64decode(value.encode()).decode().split(".")[1])
    except (ValueError, IndexError, UnicodeDecodeError):
        return None


def needs_renewal(value: str) -> bool:
    """Dauer-Login („angemeldet bleiben“) gleitend verlängern: wer die App mindestens einmal im Jahr öffnet,
    bleibt dauerhaft angemeldet. Kurze Sitzungen (ohne Häkchen) werden nicht verlängert."""
    exp = cookie_expiry(value)
    if exp is None:
        return False
    left = (exp - time.time()) / 86400
    return 2 < left < RENEW_BELOW_DAYS


def user_from_cookie(s: Session, value: str) -> Optional[User]:
    try:
        uid, exp, ver, sig = base64.urlsafe_b64decode(value.encode()).decode().split(".")
        msg = f"{uid}.{exp}.{ver}"
        if not hmac.compare_digest(_sign(_secret(s), msg), sig) or int(exp) < time.time():
            return None
        u = s.get(User, int(uid))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    if u is None or not u.active or int(ver) != (u.session_version or 1):
        return None
    return u


def user_from_basic(s: Session, header: str) -> Optional[User]:
    """Basic-Auth bleibt für Skripte möglich (nur Verwalter)."""
    try:
        name, _, pw = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:  # noqa: BLE001
        return None
    u = find_user(s, name)
    if u and u.active and u.role in ADMIN_ROLES and not u.totp_enabled and verify_password(pw, u.password_hash):
        return u  # mit Zwei-Faktor-Anmeldung kein Basic-Auth (Skripte: API-Schlüssel verwenden)
    return None


def find_user(s: Session, login: str) -> Optional[User]:
    login = (login or "").strip()
    if not login:
        return None
    u = s.query(User).filter(User.username == login).first()
    if u is None and "@" in login:
        u = s.query(User).filter(User.email.ilike(login)).first()
    return u


def has_users(s: Session) -> bool:
    return s.query(User.id).first() is not None


def bootstrap(s: Session) -> Optional[str]:
    """Ersten Verwalter aus APP_USER/APP_PASSWORD anlegen (nur wenn noch kein Benutzer existiert)."""
    if has_users(s) or not (config.app_user and config.app_password):
        return None
    u = User(username=config.app_user, name="Super-Admin", role="superadmin", active=True)
    u.password_hash = hash_password(config.app_password)
    s.add(u)
    s.commit()
    return u.username


# --------------------------------------------------------------------------- Zwei-Faktor-Anmeldung (TOTP)
PENDING_COOKIE = "nk_2fa"
PENDING_MINUTES = 5


def make_pending(s: Session, u: User, remember: bool) -> str:
    """Zwischenschritt nach richtigem Passwort: nur für die Code-Eingabe gültig (5 Minuten)."""
    msg = f"2fa.{u.id}.{int(time.time() + PENDING_MINUTES * 60)}.{u.session_version or 1}.{1 if remember else 0}"
    return base64.urlsafe_b64encode(f"{msg}.{_sign(_secret(s), msg)}".encode()).decode()


def read_pending(s: Session, value: str) -> tuple[Optional[User], bool]:
    try:
        tag, uid, exp, ver, rem, sig = base64.urlsafe_b64decode(value.encode()).decode().split(".")
        msg = f"{tag}.{uid}.{exp}.{ver}.{rem}"
        if tag != "2fa" or not hmac.compare_digest(_sign(_secret(s), msg), sig) or int(exp) < time.time():
            return None, False
        u = s.get(User, int(uid))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None, False
    if u is None or not u.active or int(ver) != (u.session_version or 1):
        return None, False
    return u, rem == "1"


def totp_secret(u: User) -> str:
    from . import secretbox

    return secretbox.maybe_decrypt(u.totp_secret or "") or ""


def check_second_factor(s: Session, u: User, code: str) -> str:
    """Code aus der App oder Wiederherstellungscode prüfen → "totp" | "recovery" | "" (falsch)."""
    from . import totp

    used = totp.verify(totp_secret(u), code, u.totp_last or 0)
    if used is not None:
        u.totp_last = used
        s.commit()
        return "totp"
    rest = totp.use_recovery(code, list(u.totp_recovery or []))
    if rest is not None:
        u.totp_recovery = rest
        s.commit()
        return "recovery"
    return ""


def totp_required(s: Session, u: CurrentUser) -> bool:
    """Verwalter ohne eingerichtete Zwei-Faktor-Anmeldung, obwohl sie Pflicht ist."""
    return u.is_admin and not u.totp and get_settings(s).get("totp_admins", "1") == "1"


totp_throttle = None  # wird unten gesetzt (nach Throttle)


# --------------------------------------------------------------------------- Passwort zurücksetzen
def create_reset(s: Session, u: User, hours: float = 1) -> str:
    token = secrets.token_urlsafe(32)
    u.reset_hash = hashlib.sha256(token.encode()).hexdigest()
    u.reset_expires = datetime.now() + timedelta(hours=hours)
    s.commit()
    return token


def user_for_reset(s: Session, token: str) -> Optional[User]:
    if not token:
        return None
    h = hashlib.sha256(token.encode()).hexdigest()
    u = s.query(User).filter(User.reset_hash == h).first()
    if u is None or not u.active or not u.reset_expires or u.reset_expires < datetime.now():
        return None
    return u


# --------------------------------------------------------------------------- Sperre bei Fehlversuchen
class Throttle:
    """Höchstens ``limit`` Versuche je Schlüssel in ``window`` Sekunden (im Speicher)."""

    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: dict[str, list[float]] = {}

    def blocked(self, key: str) -> bool:
        now = time.time()
        hits = [t for t in self.hits.get(key, []) if now - t < self.window]
        self.hits[key] = hits
        return len(hits) >= self.limit

    def hit(self, key: str) -> None:
        self.hits.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        self.hits.pop(key, None)


login_throttle = Throttle(limit=5, window=300)
reset_throttle = Throttle(limit=3, window=900)
totp_throttle = Throttle(limit=5, window=300)
