"""Push-Benachrichtigungen an Benutzer (über :mod:`webpush`).

Anlässe:
* Mieter: eine Abrechnung ihrer Partei wurde im Mieterportal veröffentlicht (je Benutzer nur einmal).
* Verwalter: eine neue Stromrechnung ist eingegangen (Postfach / Upload).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from email.utils import parseaddr
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from . import branding, render, webpush
from .config import config
from .db import Billing, Party, PushSubscription, User, get_settings, save_settings

log = logging.getLogger("notify")
MAX_FAILURES = 5
# Globale Schalter (Einstellungen → Benutzer): Schlüssel → (Beschriftung, Standard)
EVENTS = {
    "push_tenant_published": ("Mieter: neue Abrechnung im Portal veröffentlicht (mit Betrag)", "1"),
    "push_admin_import": ("Verwalter: neue Stromrechnung eingegangen (mit Betrag, Zeitraum, kWh)", "1"),
    "push_admin_sent": ("Verwalter: Abrechnungen versendet (E-Mail / WhatsApp)", "1"),
    "push_admin_errors": ("Verwalter: Fehler (Versand, Postfach-Abruf, WhatsApp-Zustellung)", "1"),
    "push_admin_delivery": ("Verwalter: WhatsApp zugestellt / gelesen", ""),
    "push_admin_budget": ("Super-Admin: Budget-Limit zu 80 % erreicht bzw. überschritten (Finanzen)", "1"),
}


# Priorität → (Bezeichnung, Symbol, Web-Push-Urgency, Rang für Sortierung)
PRIORITIES = {
    "low": ("Niedrig", "⚪", "low", 0),
    "normal": ("Normal", "🔵", "normal", 1),
    "high": ("Hoch", "🟠", "high", 2),
    "urgent": ("Dringend", "🔴", "high", 3),
}


def prio(key: str) -> tuple:
    return PRIORITIES.get(key or "normal", PRIORITIES["normal"])


def enabled(s: Session, key: str) -> bool:
    return bool(get_settings(s).get(key, EVENTS[key][1]))


def vapid_key(s: Session) -> str:
    if config.vapid_private_key:
        return config.vapid_private_key
    st = get_settings(s)
    if not st["vapid_private"]:
        st["vapid_private"] = webpush.generate_vapid()
        save_settings(s, {"vapid_private": st["vapid_private"]})
        s.commit()
    return st["vapid_private"]


def public_key(s: Session) -> str:
    return webpush.public_key_b64(vapid_key(s))


def subject() -> str:
    """Kontakt für die Push-Dienste (Pflichtangabe in VAPID)."""
    addr = parseaddr(config.smtp_from)[1]
    if addr:
        return f"mailto:{addr}"
    if config.app_base_url.startswith("https://"):
        return config.app_base_url
    return "mailto:admin@localhost"


def subscribe(s: Session, user_id: Optional[int], data: dict, user_agent: str = "") -> PushSubscription:
    endpoint = str(data.get("endpoint", ""))
    keys = data.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("Ungültiges Push-Abo")
    sub = s.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).first() or PushSubscription(
        endpoint=endpoint)
    sub.user_id, sub.p256dh, sub.auth = user_id, keys["p256dh"], keys["auth"]
    sub.user_agent, sub.failures = user_agent[:300], 0
    s.add(sub)
    s.commit()
    return sub


def unsubscribe(s: Session, endpoint: str) -> None:
    s.query(PushSubscription).filter(PushSubscription.endpoint == endpoint).delete()
    s.commit()


def send_to(s: Session, subs: Iterable[PushSubscription], data: dict,
            priority: str = "normal") -> tuple[int, list[str]]:
    """An alle Abos senden; abgelaufene Abos werden entfernt. Ergebnis: (zugestellt, Fehler).

    ``priority`` (low/normal/high/urgent) steuert die Web-Push-Dringlichkeit (Zustellung, auch im Energiesparmodus)
    und – über ``data.priority`` im Service Worker – Ton, Vibration und ob die Meldung stehen bleibt."""
    key, sub_claim = vapid_key(s), subject()
    data = {**data, "priority": priority if priority in PRIORITIES else "normal"}
    urgency = prio(priority)[2]
    ttl = 4 * 3600 if priority == "low" else 86400
    ok, errors = 0, []
    for sub in list(subs):
        try:
            webpush.send({"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}, data,
                         key, sub_claim, ttl=ttl, urgency=urgency)
            sub.last_ok, sub.failures = datetime.now(), 0
            ok += 1
        except webpush.PushError as e:
            sub.failures = (sub.failures or 0) + 1
            if e.gone or sub.failures >= MAX_FAILURES:
                s.delete(sub)
            errors.append(str(e))
        except Exception as e:  # noqa: BLE001 – Benachrichtigung darf nie den eigentlichen Vorgang stören
            log.exception("Push fehlgeschlagen")
            errors.append(str(e))
    s.commit()
    return ok, errors


def subs_for_users(s: Session, user_ids: Iterable[Optional[int]]) -> list[PushSubscription]:
    ids = list(user_ids)
    if not ids:
        return []
    q = s.query(PushSubscription)
    with_none = None in ids
    real = [i for i in ids if i is not None]
    subs = q.filter(PushSubscription.user_id.in_(real)).all() if real else []
    if with_none:
        subs += q.filter(PushSubscription.user_id.is_(None)).all()
    return subs


def admin_ids(s: Session) -> list[Optional[int]]:
    ids: list[Optional[int]] = [u.id for u in s.query(User).filter(User.role.in_(("admin", "superadmin")),
                                                                     User.active.is_(True))]
    return ids + [None]  # Abos aus der Zeit ohne Login


def billing_published(s: Session, b: Billing) -> int:
    """Mieter benachrichtigen, deren Partei die (veröffentlichte, abgeschlossene) Abrechnung sehen darf."""
    if not (b.published and b.status == "final") or not enabled(s, "push_tenant_published"):
        return 0
    already = set(b.notified or [])
    parties = {p.id: p for p in s.query(Party).filter(Party.portal.is_(True)).all()}
    sent_to = []
    for rp in (b.result or {}).get("parties", []):
        if rp["id"] not in parties:
            continue
        users = s.query(User).filter(User.role == "tenant", User.active.is_(True), User.party_id == rp["id"]).all()
        targets = [u.id for u in users if u.id not in already]
        if not targets:
            continue
        data = {"title": f"Neue Nebenkostenabrechnung {b.period_start:%m/%Y}",
                "body": f"{rp['name']}: {render.fmt_eur(rp['total'])} · zahlbar bis {render.fmt_date(render.due_date(b))}",
                "url": f"/portal/{b.id}", "tag": f"billing-{b.id}"}
        send_to(s, subs_for_users(s, targets), data)
        sent_to += targets
    if sent_to:
        b.notified = sorted(already | set(sent_to))
        s.commit()
    return len(sent_to)


def to_admins(s: Session, key: str, title: str, body: str, url: str = "/", tag: str = "",
              priority: str = "") -> int:
    """Benachrichtigung an alle Verwalter-Geräte, wenn der globale Schalter ``key`` an ist."""
    if not enabled(s, key):
        return 0
    priority = priority or {"push_admin_errors": "high", "push_admin_sent": "low",
                            "push_admin_delivery": "low"}.get(key, "normal")
    ok, _ = send_to(s, subs_for_users(s, admin_ids(s)), {"title": title, "body": body[:240], "url": url,
                                                          "tag": tag or key}, priority)
    return ok


def invoice_imported(s: Session, b: Billing, status: str, gross: Optional[float] = None, warnings: int = 0) -> int:
    """Eingang einer Stromrechnung – mit Inhalt: Betrag, Zeitraum, Netzbezug, Status."""
    amount = render.fmt_eur(gross) if gross is not None else ""
    title = f"Neue Stromrechnung{': ' + amount if amount else ''}"
    lines = [f"{render.period_text(b)} · {render.fmt_num(b.grid_kwh or 0, 1)} kWh Netzbezug"
             + (f" · Nr. {b.invoice_no}" if b.invoice_no else "")]
    if warnings:
        lines.append(f"⚠ {warnings} Hinweis(e) – bitte prüfen")
    lines.append(status)
    return to_admins(s, "push_admin_import", title, "\n".join(lines), f"/admin/billings/{b.id}", f"import-{b.id}",
                     "high" if warnings else "normal")


def sent_report(s: Session, b: Billing, report: list[tuple[str, bool, str]]) -> None:
    """Nach dem Versand: Zusammenfassung bzw. Fehler an die Verwalter."""
    if not report:
        return
    bad = [f"{n}: {m}" for n, ok, m in report if not ok]
    good = [n for n, ok, _ in report if ok]
    period = f"{b.period_start:%m/%Y}"
    if bad:
        to_admins(s, "push_admin_errors", f"Versand {period}: {len(bad)} Fehler",
                  "; ".join(bad), f"/admin/billings/{b.id}", f"senderr-{b.id}")
    if good:
        to_admins(s, "push_admin_sent", f"Abrechnung {period} versendet",
                  f"{len(good)} erfolgreich: " + ", ".join(sorted(set(good))), f"/admin/billings/{b.id}", f"sent-{b.id}")


def wa_delivery(s: Session, b: Billing, pid: int, ok: bool, status: str, error: str = "") -> None:
    """WhatsApp-Zustellstatus einer Abrechnung (Fehler → Fehler-Schalter, sonst Zustell-Schalter)."""
    name = next((p["name"] for p in (b.result or {}).get("parties", []) if p["id"] == pid), f"Partei {pid}")
    if ok:
        to_admins(s, "push_admin_delivery", f"WhatsApp {status}: {name}", f"Abrechnung {b.period_start:%m/%Y}",
                  f"/admin/billings/{b.id}", f"wa-{b.id}-{pid}")
    else:
        to_admins(s, "push_admin_errors", f"WhatsApp-Fehler: {name}", f"Abrechnung {b.period_start:%m/%Y}: {error}",
                  f"/admin/billings/{b.id}", f"waerr-{b.id}-{pid}")


# --------------------------------------------------------------------------- Mitteilungen (Broadcast / Unicast)
CATEGORIES = {  # Schlüssel → (Bezeichnung, Symbol, Farbe, heller Hintergrund)
    "info": ("Information", "ℹ️", "#2563eb", "#eff6ff"),
    "termin": ("Termin", "📅", "#7c3aed", "#f5f3ff"),
    "wartung": ("Wartung / geplante Arbeiten", "🔧", "#d97706", "#fff7ed"),
    "abschaltung": ("Abschaltung", "⛔", "#dc2626", "#fef2f2"),
    "ok": ("Erledigt / Entwarnung", "✅", "#16a34a", "#f0fdf4"),
}


def visible_until(msg) -> Optional[datetime]:
    """Bis wann eine Mitteilung als aktueller Hinweis oben steht (None = dauerhaft bzw. nur im Verlauf)."""
    if msg.show_until:
        return msg.show_until
    if msg.event_end:
        return msg.event_end
    if msg.event_start:
        return msg.event_start.replace(hour=23, minute=59, second=59)
    return None


def is_current(msg, now: Optional[datetime] = None) -> bool:
    """Oben als farbiger Hinweis: angeheftet oder geplantes Ereignis, nicht archiviert, noch nicht vorbei."""
    now = now or datetime.now()
    if msg.archived:
        return False
    until = visible_until(msg)
    if until is not None and until < now:
        return False
    return bool(msg.pinned or msg.event_start or msg.show_until)


def is_done(msg, now: Optional[datetime] = None) -> bool:
    """Ereignis/Anzeigezeitraum vorbei, aber noch nicht archiviert → als „abgeschlossen“ darstellen."""
    if msg.archived:
        return False
    until = visible_until(msg)
    return until is not None and until < (now or datetime.now())


def status(msg, now: Optional[datetime] = None) -> str:
    if msg.archived:
        return "archived"
    if is_done(msg, now):
        return "done"
    return "current" if is_current(msg, now) else "history"


def when_text(msg) -> str:
    if not msg.event_start:
        return ""
    a, b = msg.event_start, msg.event_end
    if b is None:
        return a.strftime("%d.%m.%Y %H:%M") if (a.hour or a.minute) else a.strftime("%d.%m.%Y")
    if a.date() == b.date():
        return f"{a:%d.%m.%Y}, {a:%H:%M} – {b:%H:%M} Uhr"
    return f"{a:%d.%m.%Y %H:%M} – {b:%d.%m.%Y %H:%M}"


def _push_title(msg) -> str:
    label, icon, *_ = CATEGORIES.get(msg.category, CATEGORIES["info"])
    return ("‼️ " if msg.priority == "urgent" else "") + f"{icon} {msg.title}"


def sort_key(msg):
    """Aktuelle Hinweise: dringende zuerst, dann nach Termin."""
    return (-prio(msg.priority)[3], msg.event_start or msg.created_at)


def _push_body(msg) -> str:
    w = when_text(msg)
    return (f"{w} · " if w else "") + msg.body
def message_parties(s: Session, msg) -> list[Party]:
    q = s.query(Party).filter(Party.active.is_(True))
    parties = q.all()
    return parties if not msg.party_ids else [p for p in parties if p.id in set(msg.party_ids)]


def messages_for_party(s: Session, party_id: int, limit: int = 20) -> list:
    from .db import Message

    out = []
    for m in s.query(Message).order_by(Message.created_at.desc()).limit(200):
        if not m.party_ids or party_id in m.party_ids:
            out.append(m)
            if len(out) >= limit:
                break
    return out


def send_message(s: Session, msg, via_mail: bool = False, persist: bool = True) -> dict:
    """Mitteilung zustellen: Push an alle Mieter-Geräte der Parteien, optional E-Mail an die Partei-Adressen."""
    from . import mailer

    parties = message_parties(s, msg)
    pids = [p.id for p in parties]
    users = s.query(User).filter(User.role == "tenant", User.active.is_(True), User.party_id.in_(pids)).all() if pids else []
    subs = subs_for_users(s, [u.id for u in users])
    ok, errors = send_to(s, subs, {"title": _push_title(msg), "body": _push_body(msg)[:240],
                                   "url": f"/#m{msg.id}" if persist else "/",
                                   "tag": f"msg-{msg.id}" if persist else f"api-{datetime.now():%H%M%S}"},
                         msg.priority or "normal") if subs else (0, [])
    stats = {"parties": [p.name for p in parties], "push_devices": len(subs), "push_ok": ok,
             "push_errors": errors[:3], "mail_ok": 0, "mail_errors": []}
    if via_mail and mailer.configured():
        st = get_settings(s)
        for p in parties:
            to = [a.strip() for a in (p.email or "").replace(";", ",").split(",") if a.strip()]
            if not to:
                continue
            body = f"Hallo {p.name},\n\n{msg.body}\n\nViele Grüße\n{st.get('landlord_name', '')}"
            label = CATEGORIES.get(msg.category, CATEGORIES["info"])[0]
            facts = [("Art", label, False)] + ([("Wann", when_text(msg), True)] if msg.event_start else [])
            if msg.priority in ("high", "urgent"):
                facts.insert(0, ("Priorität", f"{prio(msg.priority)[1]} {prio(msg.priority)[0]}", True))
            html = mailer.render_html(
                title=msg.title, preheader=_push_body(msg)[:120], facts=facts, brand=branding.for_party(st, p),
                brand_sub=st.get("building_address", ""), paragraphs=[f"Hallo {p.name},"] + [
                    x.strip() for x in msg.body.split("\n\n") if x.strip()],
                closing=f"Viele Grüße\n{st.get('landlord_name', '')}",
                button_url=f"{config.app_base_url}/" if (p.portal and config.app_base_url) else "",
                button_label="Zu „Mein Zuhause“", footer=st.get("building_address", ""))
            try:
                subject = (f"[{prio(msg.priority)[0]}] " if msg.priority in ("high", "urgent") else "") + msg.title
                mailer.send_mail(to, subject, body, [], html=html)
                stats["mail_ok"] += 1
            except Exception as e:  # noqa: BLE001
                stats["mail_errors"].append(f"{p.name}: {e}")
    if persist:
        msg.stats = stats
        s.commit()
    return stats



def tenant_users_for(s: Session, msg) -> list[int]:
    pids = [p.id for p in message_parties(s, msg)]
    if not pids:
        return []
    return [u.id for u in s.query(User).filter(User.role == "tenant", User.active.is_(True), User.party_id.in_(pids))]


def send_reminders(s: Session, now: Optional[datetime] = None) -> int:
    """Erinnerung am Vortag für geplante Ereignisse (läuft im Hintergrund alle 15 Minuten)."""
    from datetime import timedelta

    from .db import Message

    now = now or datetime.now()
    count = 0
    for m in s.query(Message).filter(Message.remind.is_(True), Message.reminded_at.is_(None),
                                     Message.archived.is_(False), Message.event_start.isnot(None)):
        if now < m.event_start <= now + timedelta(hours=24):
            subs = subs_for_users(s, tenant_users_for(s, m))
            send_to(s, subs, {"title": f"Erinnerung: {_push_title(m)}", "body": _push_body(m)[:240],
                              "url": f"/#m{m.id}", "tag": f"msg-{m.id}"}, m.priority or "normal")
            m.reminded_at = now
            count += 1
    s.commit()
    return count



# --------------------------------------------------------------------------- Abfallkalender
def waste_events(s: Session) -> list:
    from . import waste

    return waste.parse_ics(get_settings(s).get("waste_ics_data", ""))


def waste_refresh(s: Session, force: bool = False) -> str:
    """ICS vom Link neu laden (höchstens einmal täglich, außer ``force``)."""
    from . import waste

    st = get_settings(s)
    url = st.get("waste_ics_url", "").strip()
    if not url:
        return "Kein Link hinterlegt."
    last = st.get("waste_fetched_at", "")
    if not force and last and last[:10] == datetime.now().date().isoformat():
        return "Heute schon geladen."
    text = waste.fetch(url)
    n = len(waste.parse_ics(text))
    save_settings(s, {"waste_ics_data": text, "waste_fetched_at": datetime.now().isoformat(timespec="seconds")})
    s.commit()
    return f"{n} Abholtermine geladen."


def waste_recipients(s: Session) -> list[Optional[int]]:
    st = get_settings(s)
    pids = [p.id for p in s.query(Party).filter(Party.active.is_(True))]
    ids: list[Optional[int]] = [u.id for u in s.query(User).filter(
        User.role == "tenant", User.active.is_(True), User.party_id.in_(pids))] if pids else []
    if st.get("waste_include_admins"):
        ids += admin_ids(s)
    return ids


def waste_tick(s: Session, now: Optional[datetime] = None) -> int:
    """Fällige Abfall-Benachrichtigungen verschicken (Hintergrund, alle paar Minuten)."""
    from . import waste

    now = now or datetime.now()
    st = get_settings(s)
    if st.get("waste_ics_url"):
        try:
            waste_refresh(s)
            st = get_settings(s)
        except Exception as e:  # noqa: BLE001 – alter Stand bleibt nutzbar
            log.warning("Abfallkalender nicht geladen: %s", e)
    events = waste.parse_ics(st.get("waste_ics_data", ""))
    if not events:
        return 0
    sent = json.loads(st.get("waste_sent") or "[]")
    due = waste.due_notifications(events, st, now, set(sent))
    for key, title, body, _kinds in due:
        send_to(s, subs_for_users(s, waste_recipients(s)), {"title": title, "body": body, "url": "/#abfall",
                                                              "tag": "abfall-" + key.split("|")[0]})
        sent.append(key)
    if due:
        save_settings(s, {"waste_sent": json.dumps(sent[-60:])})
        s.commit()
    return len(due)
