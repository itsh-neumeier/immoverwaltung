"""Login, Passwort-Reset, Benutzerverwaltung und Mieterportal (Router)."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from . import auth, branding, mailer, notify, scope
from .config import config
from .db import Billing, Building, Message, Party, PushSubscription, User, get_session, get_settings, save_settings
from .render import invoice_html, invoice_pdf, party_result, pdf_name, period_text, templates

router = APIRouter()


def _page(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


def _redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url, hash_, frag = url.partition("#")  # Meldung vor den Anker, sonst kommt sie nicht an
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg) + hash_ + frag
    return RedirectResponse(url, status_code=303)


def current(request: Request) -> Optional[auth.CurrentUser]:
    return getattr(request.state, "user", None)


def require_admin(request: Request) -> None:
    u = current(request)
    if getattr(request.state, "auth_enabled", False) and (u is None or not u.is_admin):
        raise HTTPException(403, "Nur für Verwalter")


def _safe_next(url: str) -> str:
    return url if url.startswith("/") and not url.startswith("//") else "/"


def _base_url(request: Request) -> str:
    return config.app_base_url or str(request.base_url).rstrip("/")


def _set_cookie(resp: Response, request: Request, value: str, days: float) -> None:
    resp.set_cookie(auth.COOKIE, value, max_age=int(days * 86400), httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")


# --------------------------------------------------------------------------- Login / Logout / Ersteinrichtung
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return _page(request, "login.html", next=_safe_next(next), mail_ok=mailer.configured())


@router.post("/login")
async def login(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    login_name = str(form.get("username", "")).strip()
    nxt = _safe_next(str(form.get("next", "/")))
    ip = request.client.host if request.client else "?"
    keys = (f"ip:{ip}", f"user:{login_name.lower()}")
    if any(auth.login_throttle.blocked(k) for k in keys):
        return _redirect(f"/login?next={quote(nxt)}", "Zu viele Fehlversuche – bitte in einigen Minuten erneut versuchen.")
    u = auth.find_user(s, login_name)
    if u is None or not u.active or not auth.verify_password(str(form.get("password", "")), u.password_hash):
        for k in keys:
            auth.login_throttle.hit(k)
        return _redirect(f"/login?next={quote(nxt)}", "Benutzername oder Passwort falsch.")
    for k in keys:
        auth.login_throttle.clear(k)
    if u.totp_enabled:  # zweiter Schritt: Code aus der Authenticator-App
        resp = RedirectResponse(f"/login/2fa?next={quote(nxt)}", status_code=303)
        resp.set_cookie(auth.PENDING_COOKIE, auth.make_pending(s, u, bool(form.get("remember"))),
                        max_age=auth.PENDING_MINUTES * 60, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/login")
        return resp
    return _finish_login(request, s, u, bool(form.get("remember")), nxt, bool(form.get("next")))


def _finish_login(request: Request, s: Session, u: User, remember: bool, nxt: str, explicit_next: bool = True):
    u.last_login = datetime.now()
    s.commit()
    days = auth.REMEMBER_DAYS if remember else 0.5
    is_admin = u.role in auth.ADMIN_ROLES
    if not is_admin and (nxt.startswith("/admin") or nxt.startswith("/api/")):
        nxt = "/"  # Mieter: immer „Mein Zuhause“
    elif is_admin and nxt == "/" and not explicit_next:
        nxt = "/admin"
    resp = RedirectResponse(nxt, status_code=303)
    _set_cookie(resp, request, auth.make_cookie(s, u, days), days)
    resp.set_cookie(branding.ROLE_COOKIE, "admin" if is_admin else "tenant", max_age=365 * 86400,
                    samesite="lax", secure=request.url.scheme == "https", path="/")
    resp.delete_cookie(auth.PENDING_COOKIE, path="/login")
    return resp


@router.get("/login/2fa", response_class=HTMLResponse)
def login_2fa_page(request: Request, next: str = "/", s: Session = Depends(get_session)):
    u, _ = auth.read_pending(s, request.cookies.get(auth.PENDING_COOKIE, ""))
    if u is None:
        return _redirect(f"/login?next={quote(_safe_next(next))}", "Bitte erneut anmelden.")
    return _page(request, "login_2fa.html", next=_safe_next(next))


@router.post("/login/2fa")
async def login_2fa(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    nxt = _safe_next(str(form.get("next", "/")))
    u, remember = auth.read_pending(s, request.cookies.get(auth.PENDING_COOKIE, ""))
    if u is None:
        return _redirect(f"/login?next={quote(nxt)}", "Zeit abgelaufen – bitte erneut anmelden.")
    ip = request.client.host if request.client else "?"
    keys = (f"ip:{ip}", f"user:{u.id}")
    if any(auth.totp_throttle.blocked(k) for k in keys):
        return _redirect(f"/login/2fa?next={quote(nxt)}", "Zu viele Fehlversuche – bitte in einigen Minuten erneut versuchen.")
    how = auth.check_second_factor(s, u, str(form.get("code", "")))
    if not how:
        for k in keys:
            auth.totp_throttle.hit(k)
        return _redirect(f"/login/2fa?next={quote(nxt)}", "Code falsch oder abgelaufen.")
    for k in keys:
        auth.totp_throttle.clear(k)
    resp = _finish_login(request, s, u, remember, nxt, nxt != "/")
    if how == "recovery":
        left = len(u.totp_recovery or [])
        resp.headers["location"] = _redirect(resp.headers["location"],
                                             f"Mit Wiederherstellungscode angemeldet – noch {left} übrig. "
                                             "Unter „Mein Konto“ neue Codes erzeugen.").headers["location"]
    return resp


@router.get("/logout")
def logout():
    resp = _redirect("/login", "Abgemeldet.")
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request, s: Session = Depends(get_session)):
    if auth.has_users(s):
        return _redirect("/login")
    return _page(request, "setup.html")


@router.post("/setup")
async def setup(request: Request, s: Session = Depends(get_session)):
    if auth.has_users(s):
        return _redirect("/login")
    form = await request.form()
    pw = str(form.get("password", ""))
    if problem := auth.password_problem(pw):
        return _redirect("/setup", problem)
    if pw != str(form.get("password2", "")):
        return _redirect("/setup", "Passwörter stimmen nicht überein.")
    username = str(form.get("username", "")).strip() or "admin"
    u = User(username=username, name=str(form.get("name", "")).strip(), email=str(form.get("email", "")).strip(),
             role="superadmin", active=True)
    auth.set_password(u, pw)
    s.add(u)
    s.commit()
    resp = _redirect("/admin", f"Verwalter „{username}“ angelegt – Login ist jetzt aktiv.")
    _set_cookie(resp, request, auth.make_cookie(s, u, 0.5), 0.5)
    return resp


# --------------------------------------------------------------------------- Passwort vergessen / zurücksetzen
RESET_SENT = ("Falls ein Konto mit dieser Angabe und E-Mail-Adresse existiert, wurde ein Link zum Zurücksetzen "
              "verschickt (1 Stunde gültig).")


def send_reset_mail(request: Request, s: Session, u: User, invite: bool = False) -> None:
    hours = 24 * 7 if invite else 1
    token = auth.create_reset(s, u, hours=hours)
    link = f"{_base_url(request)}/password/reset?token={token}"
    st = get_settings(s)
    sender = st["landlord_name"] or "Ihre Hausverwaltung"
    hello = f"Hallo {u.name or u.username},"
    app_name = branding.admin_name(st) if u.role in auth.ADMIN_ROLES else branding.for_party(
        st, s.get(Party, u.party_id) if u.party_id else None)
    if invite:
        subject = f"Ihr Zugang zu „{app_name}“"
        body = (f"{hello}\n\nfür Sie wurde ein Zugang zu „{app_name}“ eingerichtet.\n"
                f"Benutzername: {u.username}\n\nBitte legen Sie über diesen Link Ihr Passwort fest (7 Tage gültig):\n"
                f"{link}\n\nViele Grüße\n{sender}")
        html = mailer.render_html(
            title=f"Ihr Zugang zu „{app_name}“", preheader="Passwort festlegen und Abrechnungen online ansehen", brand=app_name,
            paragraphs=[hello, "für Sie wurde ein Zugang eingerichtet. Dort finden Sie Ihre Nebenkostenabrechnungen "
                               "jederzeit als Ansicht und PDF."],
            facts=[("Benutzername", u.username, True)],
            button_url=link, button_label="Passwort festlegen",
            button_hint="Der Link ist 7 Tage gültig. Falls der Knopf nicht funktioniert, diese Adresse öffnen:",
            closing=f"Viele Grüße\n{sender}", footer="Diese E-Mail wurde automatisch versendet.")
    else:
        subject = f"Passwort zurücksetzen – {app_name}"
        body = (f"{hello}\n\nüber diesen Link können Sie ein neues Passwort festlegen "
                f"(1 Stunde gültig):\n{link}\n\nFalls Sie das nicht angefordert haben, ignorieren Sie diese E-Mail.\n\n"
                f"Viele Grüße\n{sender}")
        html = mailer.render_html(
            title="Passwort zurücksetzen", preheader="Link zum Festlegen eines neuen Passworts", brand=app_name,
            paragraphs=[hello, "über den Knopf unten können Sie ein neues Passwort festlegen.",
                        "Falls Sie das nicht angefordert haben, ignorieren Sie diese E-Mail – Ihr Passwort bleibt unverändert."],
            button_url=link, button_label="Neues Passwort festlegen",
            button_hint="Der Link ist 1 Stunde gültig und nur einmal nutzbar. Alternativ diese Adresse öffnen:",
            closing=f"Viele Grüße\n{sender}", footer="Diese E-Mail wurde automatisch versendet.")
    mailer.send_mail([u.email], subject, body, [], html=html)


@router.get("/password/forgot", response_class=HTMLResponse)
def forgot_page(request: Request):
    return _page(request, "forgot.html", mail_ok=mailer.configured())


@router.post("/password/forgot")
async def forgot(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    login_name = str(form.get("login", "")).strip()
    ip = request.client.host if request.client else "?"
    if auth.reset_throttle.blocked(f"ip:{ip}"):
        return _redirect("/login", "Zu viele Anfragen – bitte später erneut versuchen.")
    auth.reset_throttle.hit(f"ip:{ip}")
    u = auth.find_user(s, login_name)
    if u and u.active and u.email and mailer.configured():
        try:
            send_reset_mail(request, s, u)
        except Exception:  # noqa: BLE001 – keine Auskunft, ob das Konto existiert
            pass
    return _redirect("/login", RESET_SENT)


@router.get("/password/reset", response_class=HTMLResponse)
def reset_page(request: Request, token: str = "", s: Session = Depends(get_session)):
    u = auth.user_for_reset(s, token)
    return _page(request, "reset.html", token=token, valid=u is not None, user=u)


@router.post("/password/reset")
async def reset(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    token = str(form.get("token", ""))
    u = auth.user_for_reset(s, token)
    if u is None:
        return _redirect("/password/forgot", "Link ungültig oder abgelaufen – bitte neu anfordern.")
    pw = str(form.get("password", ""))
    if problem := auth.password_problem(pw):
        return _redirect(f"/password/reset?token={quote(token)}", problem)
    if pw != str(form.get("password2", "")):
        return _redirect(f"/password/reset?token={quote(token)}", "Passwörter stimmen nicht überein.")
    auth.set_password(u, pw)
    s.commit()
    return _redirect("/login", "Passwort gespeichert – bitte anmelden.")


# --------------------------------------------------------------------------- Eigenes Konto
@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, s: Session = Depends(get_session)):
    me = current(request)
    if me is None:
        return _redirect("/login")
    return _page(request, "account.html", u=s.get(User, me.id))


@router.post("/account")
async def account_save(request: Request, s: Session = Depends(get_session)):
    me = current(request)
    if me is None:
        return _redirect("/login")
    u = s.get(User, me.id)
    form = await request.form()
    u.name = str(form.get("name", "")).strip()
    u.email = str(form.get("email", "")).strip()
    msg = "Gespeichert"
    new = str(form.get("password", ""))
    if new:
        if not auth.verify_password(str(form.get("current", "")), u.password_hash):
            return _redirect("/account", "Aktuelles Passwort falsch.")
        if problem := auth.password_problem(new):
            return _redirect("/account", problem)
        if new != str(form.get("password2", "")):
            return _redirect("/account", "Passwörter stimmen nicht überein.")
        auth.set_password(u, new)
        msg = "Passwort geändert"
    s.commit()
    resp = _redirect("/account", msg)
    if new:  # Sitzungsversion hat sich geändert → Cookie erneuern (Dauer-Login bleibt erhalten)
        old = request.cookies.get(auth.COOKIE, "")
        exp = auth.cookie_expiry(old) or 0
        days = auth.REMEMBER_DAYS if exp - time.time() > 2 * 86400 else 0.5
        _set_cookie(resp, request, auth.make_cookie(s, u, days), days)
    return resp


# --------------------------------------------------------------------------- Zwei-Faktor-Anmeldung einrichten
def _me_user(request: Request, s: Session) -> Optional[User]:
    me = current(request)
    return s.get(User, me.id) if me is not None else None


def _renew_cookie(resp: Response, request: Request, s: Session, u: User) -> None:
    exp = auth.cookie_expiry(request.cookies.get(auth.COOKIE, "")) or 0
    days = auth.REMEMBER_DAYS if exp - time.time() > 2 * 86400 else 0.5
    _set_cookie(resp, request, auth.make_cookie(s, u, days), days)


@router.get("/account/2fa", response_class=HTMLResponse)
def twofa_page(request: Request, new: int = 0, s: Session = Depends(get_session)):
    from . import secretbox, totp

    u = _me_user(request, s)
    if u is None:
        return _redirect("/login")
    ctx = {"u": u, "required": u.role in auth.ADMIN_ROLES and get_settings(s).get("totp_admins", "1") == "1"}
    if not u.totp_enabled:
        secret = auth.totp_secret(u)
        if not secret or new:
            secret = totp.new_secret()
            u.totp_secret = secretbox.encrypt(secret)
            s.commit()
        brand = getattr(request.state, "brand", None)
        link = totp.uri(secret, u.username, getattr(brand, "name", "") or "ImmoVerwaltung")
        ctx.update(secret=" ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), qr=totp.qr_svg(link), link=link)
    return _page(request, "account_2fa.html", **ctx)


@router.post("/account/2fa/enable")
async def twofa_enable(request: Request, s: Session = Depends(get_session)):
    from . import totp

    u = _me_user(request, s)
    if u is None:
        return _redirect("/login")
    form = await request.form()
    used = totp.verify(auth.totp_secret(u), str(form.get("code", "")))
    if used is None:
        return _redirect("/account/2fa", "Code passt nicht – Uhrzeit am Handy prüfen und den aktuellen Code eingeben.")
    codes, hashes = totp.new_recovery_codes()
    u.totp_enabled, u.totp_last, u.totp_recovery = True, used, hashes
    u.session_version = (u.session_version or 1) + 1  # andere Sitzungen abmelden
    s.commit()
    resp = _page(request, "account_2fa.html", u=u, codes=codes, just_enabled=True, required=False)
    _renew_cookie(resp, request, s, u)
    return resp


@router.post("/account/2fa/recovery")
async def twofa_recovery(request: Request, s: Session = Depends(get_session)):
    from . import totp

    u = _me_user(request, s)
    if u is None or not u.totp_enabled:
        return _redirect("/account/2fa")
    form = await request.form()
    if auth.check_second_factor(s, u, str(form.get("code", ""))) != "totp":
        return _redirect("/account/2fa", "Code aus der App falsch.")
    codes, u.totp_recovery = totp.new_recovery_codes()
    s.commit()
    return _page(request, "account_2fa.html", u=u, codes=codes, required=False)


@router.post("/account/2fa/disable")
async def twofa_disable(request: Request, s: Session = Depends(get_session)):
    u = _me_user(request, s)
    if u is None or not u.totp_enabled:
        return _redirect("/account/2fa")
    if u.role in auth.ADMIN_ROLES and get_settings(s).get("totp_admins", "1") == "1":
        return _redirect("/account/2fa", "Für Verwalter ist die Zwei-Faktor-Anmeldung Pflicht. Neues Handy? → „Neu einrichten“.")
    form = await request.form()
    if not auth.check_second_factor(s, u, str(form.get("code", ""))):
        return _redirect("/account/2fa", "Code falsch.")
    u.totp_enabled, u.totp_secret, u.totp_recovery, u.totp_last = False, "", [], 0
    s.commit()
    return _redirect("/account", "Zwei-Faktor-Anmeldung ausgeschaltet.")


@router.post("/account/2fa/renew")
async def twofa_renew(request: Request, s: Session = Depends(get_session)):
    """Neues Handy: mit aktuellem Code (oder Wiederherstellungscode) neu einrichten."""
    u = _me_user(request, s)
    if u is None or not u.totp_enabled:
        return _redirect("/account/2fa")
    form = await request.form()
    if not auth.check_second_factor(s, u, str(form.get("code", ""))):
        return _redirect("/account/2fa", "Code falsch.")
    u.totp_enabled, u.totp_secret, u.totp_recovery, u.totp_last = False, "", [], 0
    s.commit()
    resp = _redirect("/account/2fa?new=1", "Jetzt den neuen QR-Code mit der App scannen.")
    _renew_cookie(resp, request, s, u)
    return resp


# --------------------------------------------------------------------------- Benutzerverwaltung (Verwalter)
@router.get("/admin/users", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def users_page(request: Request, s: Session = Depends(get_session)):
    users = s.query(User).order_by(User.role, User.username).all()
    parties = {p.id: p for p in s.query(Party).order_by(Party.sort, Party.id).all()}
    devices: dict = {}
    for sub in s.query(PushSubscription).all():
        devices[sub.user_id] = devices.get(sub.user_id, 0) + 1
    return _page(request, "users.html", users=users, parties=parties, roles=auth.ROLES, st=get_settings(s),
                 mail_ok=mailer.configured(), devices=devices, events=notify.EVENTS)


@router.post("/admin/users/settings", dependencies=[Depends(require_admin)])
async def users_settings(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    if form.get("push_form"):  # Formular „Benachrichtigungen (global)“
        save_settings(s, {k: "1" if form.get(k) else "" for k in notify.EVENTS})
    elif form.get("security_form"):
        save_settings(s, {"totp_admins": "1" if form.get("totp_admins") else "0"})
    else:  # Formular „Mieterportal“
        save_settings(s, {"portal_auto_publish": "1" if form.get("portal_auto_publish") else "",
                          "tenant_app_name": branding.clean(str(form.get("tenant_app_name", ""))) or "Mein Zuhause",
                          "admin_app_name": branding.clean(str(form.get("admin_app_name", ""))) or "ImmoVerwaltung",
                          "tenant_app_color": branding.clean_color(str(form.get("tenant_app_color", "")),
                                                                   branding.TENANT_COLOR),
                          "admin_app_color": branding.clean_color(str(form.get("admin_app_color", "")),
                                                                  branding.ADMIN_COLOR)})
        for p in s.query(Party).all():
            p.portal = bool(form.get(f"portal_{p.id}"))
    s.commit()
    return _redirect("/admin/users", "Gespeichert")


@router.get("/admin/users/{uid}", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def user_edit(request: Request, uid: int, party: Optional[int] = None, s: Session = Depends(get_session)):
    u = User(role="tenant", active=True, party_id=party) if uid == 0 else s.get(User, uid)
    if u is None:
        raise HTTPException(404)
    if uid == 0 and party:
        p = s.get(Party, party)
        if p:
            u.name, u.email = p.name, (p.email or "").split(",")[0].strip()
    parties = s.query(Party).order_by(Party.sort, Party.id).all()
    buildings = s.query(Building).order_by(Building.sort, Building.id).all()
    return _page(request, "user_edit.html", u=u, uid=uid, parties=parties, roles=auth.ROLES, buildings=buildings,
                 mail_ok=mailer.configured())


def _admins(s: Session, exclude: int = 0) -> int:
    return s.query(User).filter(User.role == "superadmin", User.active.is_(True), User.id != exclude).count()


@router.post("/admin/users/{uid}", dependencies=[Depends(require_admin)])
async def user_save(request: Request, uid: int, s: Session = Depends(get_session)):
    form = await request.form()
    me = current(request)
    u = User() if uid == 0 else s.get(User, uid)
    if u is None:
        raise HTTPException(404)
    if form.get("delete"):
        if me and u.id == me.id:
            return _redirect(f"/admin/users/{uid}", "Du kannst dich nicht selbst löschen.")
        if u.role == "superadmin" and _admins(s, exclude=u.id) == 0:
            return _redirect(f"/admin/users/{uid}", "Der letzte Super-Admin kann nicht gelöscht werden.")
        s.delete(u)
        s.commit()
        return _redirect("/admin/users", "Benutzer gelöscht")

    username = str(form.get("username", "")).strip()
    if not username:
        return _redirect(f"/admin/users/{uid}", "Benutzername fehlt.")
    clash = s.query(User).filter(User.username == username, User.id != (u.id or 0)).first()
    if clash:
        return _redirect(f"/admin/users/{uid}", f"Benutzername „{username}“ ist schon vergeben.")
    role = str(form.get("role", "tenant"))
    role = role if role in auth.ROLES else "tenant"
    active = bool(form.get("active"))
    if u.id and u.role == "superadmin" and (role != "superadmin" or not active) and _admins(s, exclude=u.id) == 0:
        return _redirect(f"/admin/users/{uid}", "Der letzte aktive Super-Admin muss Super-Admin bleiben.")
    party_id = int(form.get("party_id") or 0) or None
    if role == "tenant" and not party_id:
        return _redirect(f"/admin/users/{uid}" + (f"?party={party_id}" if party_id else ""),
                         "Mieter müssen einer Partei zugeordnet sein.")
    u.username, u.role, u.active = username, role, active
    u.name = str(form.get("name", "")).strip()
    u.email = str(form.get("email", "")).strip()
    u.party_id = party_id if role == "tenant" else None
    u.building_ids = sorted({int(x) for x in form.getlist("building_ids") if str(x).isdigit()}) if role == "admin" else []
    pw = str(form.get("password", ""))
    if pw:
        if problem := auth.password_problem(pw):
            return _redirect(f"/admin/users/{uid}", problem)
        auth.set_password(u, pw)
    elif uid == 0:
        u.password_hash = ""  # Login erst nach Festlegen über den Einladungslink
    if uid == 0:
        s.add(u)
    s.commit()
    msg = "Benutzer gespeichert"
    if form.get("invite") or (uid == 0 and not pw):
        if not (u.email and mailer.configured()):
            msg += " – keine Einladung verschickt (E-Mail-Adresse oder Mailversand fehlt; Passwort selbst setzen)"
        else:
            try:
                send_reset_mail(request, s, u, invite=True)
                msg += f" – Einladung an {u.email} verschickt"
            except Exception as e:  # noqa: BLE001
                msg += f" – Einladung fehlgeschlagen: {e}"
    return _redirect("/admin/users", msg)


@router.post("/admin/users/{uid}/reset", dependencies=[Depends(require_admin)])
def user_reset(request: Request, uid: int, s: Session = Depends(get_session)):
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404)
    if not (u.email and mailer.configured()):
        return _redirect(f"/admin/users/{uid}", "Keine E-Mail-Adresse hinterlegt bzw. Mailversand nicht eingerichtet.")
    try:
        send_reset_mail(request, s, u)
        return _redirect("/admin/users", f"Link zum Zurücksetzen an {u.email} verschickt")
    except Exception as e:  # noqa: BLE001
        return _redirect(f"/admin/users/{uid}", f"E-Mail fehlgeschlagen: {e}")


@router.post("/admin/users/{uid}/2fa-reset", dependencies=[Depends(require_admin)])
def user_2fa_reset(request: Request, uid: int, s: Session = Depends(get_session)):
    """Super-Admin: Zwei-Faktor-Anmeldung eines Benutzers zurücksetzen (z. B. Handy verloren, keine Codes mehr)."""
    u = s.get(User, uid)
    if u is None:
        raise HTTPException(404)
    me = current(request)
    if me is not None and me.id == uid:
        return _redirect(f"/admin/users/{uid}", "Die eigene Zwei-Faktor-Anmeldung unter „Mein Konto“ ändern.")
    u.totp_enabled, u.totp_secret, u.totp_recovery, u.totp_last = False, "", [], 0
    u.session_version = (u.session_version or 1) + 1
    s.commit()
    return _redirect(f"/admin/users/{uid}", f"Zwei-Faktor-Anmeldung von „{u.username}“ zurückgesetzt – "
                     "beim nächsten Login wird sie neu eingerichtet.")


# --------------------------------------------------------------------------- Mieterportal
def _portal_party(request: Request, s: Session, party: Optional[int]) -> Optional[Party]:
    me = current(request)
    if me is not None and not me.is_admin:
        return s.get(Party, me.party_id) if me.party_id else None
    return s.get(Party, party) if party else None  # Verwalter: Vorschau „Ansicht als Mieter“


def visible_billings(s: Session, party: Party) -> list[Billing]:
    if party is None or not party.portal:
        return []
    out = []
    for b in s.query(Billing).filter(Billing.status == "final", Billing.published.is_(True)) \
            .order_by(Billing.period_start.desc()).all():
        if party_result(b, party.id) is not None:
            out.append(b)
    return out


@router.get("/portal")
def portal_legacy(request: Request):
    """Frühere Adresse des Mieterportals → „/“ (Mein Zuhause)."""
    q = request.url.query
    return RedirectResponse("/" + (f"?{q}" if q else ""), status_code=308)


@router.get("/", response_class=HTMLResponse)
def portal(request: Request, party: Optional[int] = None, s: Session = Depends(get_session)):
    me = current(request)
    p = _portal_party(request, s, party)
    rows = [(b, party_result(b, p.id)) for b in visible_billings(s, p)] if p else []
    parties = s.query(Party).order_by(Party.sort, Party.id).all() if (me is None or me.is_admin) else []
    msgs = notify.messages_for_party(s, p.id, limit=30) if p else []
    current_ = sorted([m for m in msgs if notify.is_current(m)], key=notify.sort_key)
    recent = datetime.now() - timedelta(days=14)
    done = [m for m in msgs if notify.is_done(m) and notify.visible_until(m) >= recent]  # kürzlich vorbei
    history = [m for m in msgs if m not in current_ and m not in done and not m.archived][:10]
    from . import waste
    st = get_settings(s)
    pickups = waste.upcoming(waste.parse_ics(st.get("waste_ics_data", "")), json.loads(st.get("waste_types") or "[]"),
                             days=35)[:6] if p else []
    bld = s.get(Building, p.building_id) if (p and p.building_id) else None
    return _page(request, "portal.html", party=p, rows=rows, parties=parties, period_text=period_text, bld=bld,
                 current=current_, history=history, done=done, cats=notify.CATEGORIES, when=notify.when_text,
                 prios=notify.PRIORITIES, is_done=notify.is_done, pickups=pickups, wstyle=waste.style,
                 today=datetime.now().date())


def _portal_billing(request: Request, s: Session, bid: int, party: Optional[int]):
    p = _portal_party(request, s, party)
    b = s.get(Billing, bid)
    if p is None or b is None or b not in visible_billings(s, p):
        raise HTTPException(404, "Abrechnung nicht gefunden")
    return b, party_result(b, p.id)


@router.get("/portal/{bid}.pdf")
def portal_pdf(request: Request, bid: int, party: Optional[int] = None, s: Session = Depends(get_session)):
    b, pr = _portal_billing(request, s, bid, party)
    return Response(invoice_pdf(b, pr), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_name(b, pr)}"', "Cache-Control": "no-store"})


@router.get("/portal/{bid}", response_class=HTMLResponse)
def portal_view(request: Request, bid: int, party: Optional[int] = None, s: Session = Depends(get_session)):
    b, pr = _portal_billing(request, s, bid, party)
    return HTMLResponse(invoice_html(b, pr))


# --------------------------------------------------------------------------- Push-Benachrichtigungen
def _push_user(request: Request) -> tuple[bool, Optional[int]]:
    """(erlaubt, user_id) – ohne Login-System gehört das Abo dem Verwalter (user_id None)."""
    me = current(request)
    if me is not None:
        return True, me.id
    return not getattr(request.state, "auth_enabled", False), None


@router.get("/api/push/config")
def push_config(request: Request, s: Session = Depends(get_session)):
    allowed, uid = _push_user(request)
    if not allowed:
        raise HTTPException(401)
    devices = s.query(PushSubscription).filter(
        PushSubscription.user_id == uid if uid is not None else PushSubscription.user_id.is_(None)).count()
    return {"publicKey": notify.public_key(s), "devices": devices}


@router.post("/api/push/subscribe")
async def push_subscribe(request: Request, s: Session = Depends(get_session)):
    allowed, uid = _push_user(request)
    if not allowed:
        raise HTTPException(401)
    try:
        notify.subscribe(s, uid, await request.json(), request.headers.get("user-agent", ""))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@router.post("/api/push/unsubscribe")
async def push_unsubscribe(request: Request, s: Session = Depends(get_session)):
    allowed, uid = _push_user(request)
    if not allowed:
        raise HTTPException(401)
    data = await request.json()
    sub = s.query(PushSubscription).filter(PushSubscription.endpoint == str(data.get("endpoint", ""))).first()
    if sub is not None and sub.user_id == uid:
        notify.unsubscribe(s, sub.endpoint)
    return {"ok": True}


@router.post("/api/push/test")
def push_test(request: Request, s: Session = Depends(get_session)):
    allowed, uid = _push_user(request)
    if not allowed:
        raise HTTPException(401)
    me = current(request)
    url = "/" if (me is not None and not me.is_admin) else "/admin"
    ok, errors = notify.send_to(s, notify.subs_for_users(s, [uid]), {
        "title": "Test-Benachrichtigung", "body": "Benachrichtigungen auf diesem Gerät funktionieren ✅",
        "url": url, "tag": "test"})
    return {"ok": ok, "errors": errors}


# --------------------------------------------------------------------------- Mitteilungen / Hinweise (Verwalter)
def _dt(value) -> Optional[datetime]:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@router.get("/admin/messages", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def messages_page(request: Request, edit: int = 0, s: Session = Depends(get_session)):
    msgs = s.query(Message).order_by(Message.created_at.desc()).limit(100).all()
    parties = scope.filter_query(s.query(Party).filter(Party.active.is_(True)), Party.building_id) \
        .order_by(Party.sort, Party.id).all()
    if not scope.is_super():  # Verwalter: nur Mitteilungen, die ihre Parteien betreffen
        mine = {p.id for p in parties}
        msgs = [m for m in msgs if not m.party_ids or set(m.party_ids) & mine]
    current_ = sorted([m for m in msgs if notify.is_current(m)], key=notify.sort_key)
    m = s.get(Message, edit) if edit else None
    return _page(request, "messages.html", msgs=msgs, current=current_, parties=parties, cats=notify.CATEGORIES,
                 when=notify.when_text, is_current=notify.is_current, m=m, mail_ok=mailer.configured(),
                 prios=notify.PRIORITIES, status=notify.status)


@router.post("/admin/messages", dependencies=[Depends(require_admin)])
async def message_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    mid = int(form.get("id") or 0)
    m = s.get(Message, mid) if mid else Message()
    if m is None:
        raise HTTPException(404)
    title = str(form.get("title", "")).strip()
    if not title:
        return _redirect("/admin/messages", "Bitte einen Titel eingeben.")
    target = form.getlist("party_ids")
    m.title, m.body = title, str(form.get("body", "")).strip()
    m.category = str(form.get("category", "info")) if form.get("category") in notify.CATEGORIES else "info"
    m.party_ids = [] if (not target or "all" in target) else sorted({int(x) for x in target if str(x).isdigit()})
    if not scope.is_super():  # Verwalter: „alle“ = alle Parteien der eigenen Gebäude
        mine = {p.id for p in scope.filter_query(s.query(Party), Party.building_id)}
        m.party_ids = sorted(mine if not m.party_ids else set(m.party_ids) & mine) or [-1]
    m.priority = str(form.get("priority", "normal")) if form.get("priority") in notify.PRIORITIES else "normal"
    m.pinned = bool(form.get("pinned"))
    m.event_start, m.event_end, m.show_until = _dt(form.get("event_start")), _dt(form.get("event_end")), _dt(form.get("show_until"))
    if m.event_start and m.event_end and m.event_end < m.event_start:
        return _redirect("/admin/messages" + (f"?edit={mid}" if mid else ""), "Ende liegt vor dem Beginn.")
    remind = bool(form.get("remind")) and m.event_start is not None
    if remind and not m.remind:
        m.reminded_at = None
    m.remind = remind
    me = current(request)
    m.sender = me.display if me else "Verwalter"
    if not mid:
        s.add(m)
    s.commit()
    msg = "Hinweis gespeichert"
    if form.get("notify_now"):
        st = notify.send_message(s, m, via_mail=bool(form.get("via_mail")))
        msg += f" · Push an {st['push_ok']} von {st['push_devices']} Gerät(en)"
        if form.get("via_mail"):
            msg += f" · {st['mail_ok']} E-Mail(s)"
            if st["mail_errors"]:
                msg += f" ({len(st['mail_errors'])} fehlgeschlagen)"
    return _redirect("/admin/messages", msg)


@router.post("/admin/messages/{mid}/archive", dependencies=[Depends(require_admin)])
def message_archive(mid: int, s: Session = Depends(get_session)):
    m = s.get(Message, mid)
    if m is None:
        raise HTTPException(404)
    m.archived = not m.archived
    s.commit()
    return _redirect("/admin/messages", "Archiviert" if m.archived else "Wieder aktiv")


@router.post("/admin/messages/{mid}/delete", dependencies=[Depends(require_admin)])
def message_delete(mid: int, s: Session = Depends(get_session)):
    m = s.get(Message, mid)
    if m is not None:
        s.delete(m)
        s.commit()
    return _redirect("/admin/messages", "Gelöscht")


@router.post("/admin/messages/{mid}/send", dependencies=[Depends(require_admin)])
async def message_resend(request: Request, mid: int, s: Session = Depends(get_session)):
    m = s.get(Message, mid)
    if m is None:
        raise HTTPException(404)
    form = await request.form()
    st = notify.send_message(s, m, via_mail=bool(form.get("via_mail")))
    return _redirect("/admin/messages", f"Erneut gesendet · Push an {st['push_ok']} von {st['push_devices']} Gerät(en)")


# --------------------------------------------------------------------------- Abfallkalender (Verwalter)
@router.get("/admin/waste", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def waste_page(request: Request, s: Session = Depends(get_session)):
    from . import waste

    st = get_settings(s)
    events = waste.parse_ics(st.get("waste_ics_data", ""))
    enabled = json.loads(st.get("waste_types") or "[]")
    return _page(request, "waste.html", st=st, kinds=waste.kinds(events), enabled=enabled, style=waste.style,
                 upcoming=waste.upcoming(events, enabled, days=45), total=len(events),
                 recipients=len(notify.subs_for_users(s, notify.waste_recipients(s))))


@router.post("/admin/waste", dependencies=[Depends(require_admin)])
async def waste_save(request: Request, s: Session = Depends(get_session)):
    from . import waste

    form = await request.form()
    data = {k: str(form.get(k, "")).strip() for k in ("waste_ics_url", "waste_evening_time", "waste_morning_time")}
    for flag in ("waste_notify_evening", "waste_notify_morning", "waste_include_admins"):
        data[flag] = "1" if form.get(flag) else ""
    if form.get("types_form"):
        data["waste_types"] = json.dumps(form.getlist("waste_types"), ensure_ascii=False)
    upload = form.get("ics_file")
    msg = "Gespeichert"
    if upload is not None and getattr(upload, "filename", ""):
        text = (await upload.read()).decode("utf-8", errors="replace")
        if "BEGIN:VCALENDAR" not in text:
            return _redirect("/admin/waste", "Die Datei ist keine ICS-Kalenderdatei.")
        data["waste_ics_data"] = text
        data["waste_fetched_at"] = datetime.now().isoformat(timespec="seconds")
        msg += f" · {len(waste.parse_ics(text))} Abholtermine aus der Datei"
    save_settings(s, data)
    s.commit()
    if data["waste_ics_url"] and (form.get("refresh") or not get_settings(s).get("waste_ics_data")):
        try:
            msg += " · " + notify.waste_refresh(s, force=True)
        except Exception as e:  # noqa: BLE001
            msg += f" · Laden fehlgeschlagen: {e}"
    return _redirect("/admin/waste", msg)


@router.post("/admin/waste/test", dependencies=[Depends(require_admin)])
def waste_test(request: Request, s: Session = Depends(get_session)):
    from . import waste

    st = get_settings(s)
    events = waste.parse_ics(st.get("waste_ics_data", ""))
    nxt = waste.upcoming(events, json.loads(st.get("waste_types") or "[]"), days=400)
    if not nxt:
        return _redirect("/admin/waste", "Keine kommenden Abholtermine im Kalender.")
    d, ks = nxt[0]
    _, uid = _push_user(request)
    ok, errors = notify.send_to(s, notify.subs_for_users(s, [uid]), {
        "title": f"🗑️ Test – nächste Abholung {d:%d.%m.}: {', '.join(ks)}",
        "body": "So sieht die Erinnerung für die Mieter aus.", "url": "/#abfall", "tag": "abfall-test"})
    return _redirect("/admin/waste", f"Test an {ok} eigenes Gerät gesendet" if ok else
                     "Kein eigenes Gerät angemeldet – unter „Mein Konto“ Benachrichtigungen aktivieren.")
