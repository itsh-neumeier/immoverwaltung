"""Sichtbarkeit je Gebäude (Multi-Site) für die laufende Anfrage.

Super-Admin (oder App ohne Login): alle Gebäude. Verwalter: nur zugewiesene Gebäude. Die Middleware setzt den
angemeldeten Benutzer; Routen fragen hier nach, statt den Benutzer überall durchzureichen.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

current_user: ContextVar = ContextVar("current_user", default=None)
auth_enabled: ContextVar = ContextVar("auth_enabled", default=False)

# nur für Super-Admins (globale Einstellungen, Benutzer, Schnittstellen)
SUPER_ONLY = ("/admin/users", "/admin/settings", "/admin/whatsapp", "/admin/mailbox", "/admin/vrm",
              "/admin/victron", "/admin/api-keys", "/admin/finanzen")


def allowed_buildings() -> Optional[set]:
    """None = alle Gebäude; sonst Menge der erlaubten Gebäude-IDs."""
    u = current_user.get()
    if not auth_enabled.get() or u is None or u.is_super:
        return None
    return set(u.building_ids) if u.is_admin else set()


def can(building_id) -> bool:
    allowed = allowed_buildings()
    return allowed is None or building_id in allowed


def is_super() -> bool:
    u = current_user.get()
    return not auth_enabled.get() or (u is not None and u.is_super)


def filter_query(q, column):
    """SQLAlchemy-Abfrage auf erlaubte Gebäude einschränken."""
    allowed = allowed_buildings()
    if allowed is None:
        return q
    return q.filter(column.in_(allowed or [-1]))
