"""Webinterface (FastAPI + Jinja2)."""

from __future__ import annotations

import asyncio
import json
import io
import os
import re
import secrets
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import accounts, api, auth, reserves, branding, scope, invoice_import, mailbox, mailer, notify, service, victron, vrm, wa_cloud, whatsapp
from .config import config
from .db import (Allocation, Billing, Building, FixedCost, IgnoredInvoice, Party, User, SessionLocal, get_session, get_settings,
                 init_db, save_settings)
from .ha import HAClient
from .render import BASE, invoice_html, invoice_pdf, parse_float, party_result, pdf_name, templates


async def reminder_loop() -> None:
    """Erinnerungen für geplante Ereignisse (Vortag) und Abfallkalender – alle 5 Minuten prüfen."""
    while True:
        await asyncio.sleep(60)
        try:
            with SessionLocal() as s:
                await asyncio.to_thread(notify.send_reminders, s)
            with SessionLocal() as s:
                await asyncio.to_thread(notify.waste_tick, s)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(4 * 60)


@asynccontextmanager
async def lifespan(_app):
    init_db()
    with SessionLocal() as s:
        if name := auth.bootstrap(s):
            print(f"Login aktiv: Verwalter „{name}“ aus APP_USER/APP_PASSWORD angelegt.")
    tasks = []
    if mailbox.configured():
        tasks.append(asyncio.create_task(mailbox.poll_forever()))
    if config.victron_logger:
        tasks.append(asyncio.create_task(victron.logger.run_forever()))
    tasks.append(asyncio.create_task(reminder_loop()))
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="ImmoVerwaltung", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(accounts.router)
app.include_router(api.router)
app.include_router(reserves.router)


# --------------------------------------------------------------------------- Helfer
def normalize_spec(text: str) -> str:
    """Entitäten-Angabe vereinheitlichen: „a+b , c“ -> „a + b + c“."""
    return " + ".join(e for e in re.split(r"[\s,;+]+", text or "") if e)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("ha_configured", bool(config.ha_url and config.ha_token))
    ctx.setdefault("victron_status", victron.logger.status)
    return templates.TemplateResponse(request, name, ctx)


def redirect(url: str, msg: str = "") -> RedirectResponse:
    if msg:
        url, hash_, frag = url.partition("#")  # Meldung vor den Anker, sonst kommt sie nicht an
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg) + hash_ + frag
    return RedirectResponse(url, status_code=303)


PUBLIC = ("/login", "/logout", "/setup", "/password/", "/static/", "/healthz", "/favicon", "/apple-touch-icon",
          "/api/n8n/", "/api/whatsapp/", "/api/v1/", "/manifest.webmanifest", "/sw.js", "/offline", "/app")
# frühere Adressen des Verwalterbereichs → /admin/… (Lesezeichen, Links in alten Mails)
LEGACY_ADMIN = ("/billings", "/costs", "/mailbox", "/messages", "/parties", "/settings", "/users", "/victron", "/vrm",
                "/waste", "/whatsapp")


def _is_admin_path(path: str) -> bool:
    if path == "/admin" or path.startswith("/admin/"):
        return True
    return path.startswith("/api/") and not path.startswith(("/api/push/", "/api/n8n/", "/api/whatsapp/", "/api/v1/"))


@app.middleware("http")
async def authenticate(request: Request, call_next):
    """Bereiche: „/“ = Mein Zuhause (Mieter), „/admin“ = ImmoVerwaltung (nur Verwalter).
    Ohne angelegte Benutzer ist alles offen (Hinweis zur Einrichtung im Menü)."""
    path = request.url.path
    for old in LEGACY_ADMIN:
        if path == old or path.startswith(old + "/"):
            target = "/admin" + path + (f"?{request.url.query}" if request.url.query else "")
            return RedirectResponse(target, status_code=308)
    with SessionLocal() as s:
        enabled = auth.has_users(s)
        u = None
        if enabled:
            if cookie := request.cookies.get(auth.COOKIE):
                u = auth.user_from_cookie(s, cookie)
            if u is None and request.headers.get("authorization", "").startswith("Basic "):
                u = auth.user_from_basic(s, request.headers["authorization"])
        request.state.user = auth.snapshot(u) if u else None
        request.state.auth_enabled = enabled
        scope.current_user.set(request.state.user)
        scope.auth_enabled.set(enabled)
        request.state.brand = branding.resolve(
            s, request.state.user, get_settings(s), path, request.query_params.get("next", ""),
            request.cookies.get(branding.ROLE_COOKIE, ""))
    if not enabled or path.startswith(PUBLIC):
        return await call_next(request)
    user = request.state.user
    renew = None
    if user is not None and (cookie := request.cookies.get(auth.COOKIE)) and auth.needs_renewal(cookie):
        with SessionLocal() as s:  # Dauer-Login gleitend verlängern
            from .db import User as _U
            if (db_user := s.get(_U, user.id)) is not None:
                renew = auth.make_cookie(s, db_user, auth.REMEMBER_DAYS)
    if user is None:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "Anmeldung erforderlich"}, status_code=401)
        nxt = path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse("/login" + ("" if nxt == "/" else f"?next={quote(nxt)}"), status_code=303)
    if _is_admin_path(path) and not user.is_admin:
        if request.method == "GET" and not path.startswith("/api/"):
            return RedirectResponse("/", status_code=303)
        return Response("Nur für Verwalter", status_code=403)
    if path.startswith(scope.SUPER_ONLY) and not user.is_super:
        if request.method == "GET":
            return RedirectResponse("/admin?msg=" + quote("Nur für Super-Admins"), status_code=303)
        return Response("Nur für Super-Admins", status_code=403)
    response = await call_next(request)
    if renew:
        response.set_cookie(auth.COOKIE, renew, max_age=auth.REMEMBER_DAYS * 86400, httponly=True, samesite="lax",
                            secure=request.url.scheme == "https", path="/")
    return response


@app.get("/favicon.ico")
def favicon():
    return FileResponse(BASE / "static" / "favicon.ico", media_type="image/x-icon")


@app.get("/manifest.webmanifest")
def manifest(app: str = "admin", name: str = "", s: Session = Depends(get_session)):
    """Manifest je Rolle: Verwalter-App (ImmoVerwaltung, Start „/“) bzw. Mieter-App (Mein Zuhause,
    Start „/“). Getrennte ``id`` → beide lassen sich unabhängig installieren."""
    data = json.loads((BASE / "static" / "manifest.webmanifest").read_text())
    st = get_settings(s)
    if app == "tenant":
        title = branding.clean(name) or branding.tenant_default(st)
        data["theme_color"] = branding.tenant_color(st)
        data.update(id="/", start_url="/?source=pwa", scope="/", name=title, short_name=title,
                    description=f"{title} – Abrechnungen, Mitteilungen und Abfuhrtermine",
                    shortcuts=[{"name": "Übersicht", "url": "/", "icons": data["icons"][:1]},
                               {"name": "Mein Konto", "url": "/account", "icons": data["icons"][:1]}])
    else:
        title = branding.admin_name(st)
        data["theme_color"] = branding.admin_color(st)
        data.update(id="/admin", start_url="/admin?source=pwa", scope="/", name=title, short_name=title,
                    description=f"{title} – Abrechnung, Mieterportal und Hausverwaltung",
                    shortcuts=[{"name": "Abrechnungen", "url": "/admin", "icons": data["icons"][:1]},
                               {"name": "Mitteilungen", "url": "/admin/messages", "icons": data["icons"][:1]}])
    return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json",
                    headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    js = (BASE / "static" / "sw.js").read_text().replace("__VERSION__", templates.env.globals["app_version"])
    return Response(js, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/offline", response_class=HTMLResponse)
def offline(request: Request):
    return templates.TemplateResponse(request, "offline.html", {})


@app.get("/app", response_class=HTMLResponse)
def app_info(request: Request):
    return templates.TemplateResponse(request, "app.html", {"https": request.url.scheme == "https",
                                                            "base": config.app_base_url})


@app.get("/apple-touch-icon.png")
def apple_icon():
    return FileResponse(BASE / "static" / "apple-touch-icon.png", media_type="image/png")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# --------------------------------------------------------------------------- Übersicht
@app.get("/admin", response_class=HTMLResponse)
def index(request: Request, s: Session = Depends(get_session)):
    # Verwalter sehen nur ihre Gebäude; nicht zugeordnete Rechnungen sieht (und ordnet zu) der Super-Admin
    billings = scope.filter_query(s.query(Billing), Billing.building_id).order_by(Billing.period_start.desc()).all()
    st = get_settings(s)
    setup_missing = [
        label for key, label in (("entity_total", "Gesamtverbrauch-Entität"), ("entity_grid", "Zähler-Entität"))
        if not st[key]
    ]
    if not service.active_parties(s):
        setup_missing.append("Parteien")
    return render(request, "index.html", billings=billings, setup_missing=setup_missing,
                  buildings={b.id: b for b in _buildings(s)},
                  imap_ok=mailbox.configured(), mbox=mailbox.status, imap=config,
                  senders=mailbox.sender_patterns(st), forwarded=bool(st["imap_forwarded"]))


@app.post("/admin/billings/delete")
async def billings_bulk_delete(request: Request, s: Session = Depends(get_session)):
    """Mehrere Entwürfe auf einmal löschen (abgeschlossene Abrechnungen bleiben unangetastet)."""
    form = await request.form()
    ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
    n = blocked = 0
    for b in s.query(Billing).filter(Billing.id.in_(ids), Billing.status != "final").all() if ids else []:
        blocked += service.delete_billing(s, b)
        n += 1
    if not n:
        return redirect("/admin", "Keine Entwürfe ausgewählt.")
    return redirect("/admin", f"{n} Entwurf/Entwürfe gelöscht" + (f" · {blocked} für den Postfach-Abruf gesperrt" if blocked else ""))


@app.post("/admin/mailbox/unignore/{iid}")
def mailbox_unignore(iid: int, s: Session = Depends(get_session)):
    from .db import IgnoredInvoice

    row = s.get(IgnoredInvoice, iid)
    if row is not None:
        s.delete(row)
        s.commit()
    return redirect("/admin/settings#eingang", "Wieder zugelassen – wird beim nächsten Postfach-Abruf erneut importiert.")


@app.post("/admin/mailbox/check")
async def mailbox_check():
    return redirect("/admin", await mailbox.check_mailbox())


@app.post("/admin/billings/import")
async def billing_import(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    upload = form.get("file")
    if upload is None or not getattr(upload, "filename", ""):
        return redirect("/admin/billings/new", "Bitte eine PDF- oder .eml-Datei auswählen.")
    data = await upload.read()
    try:
        files = invoice_import.load_upload(upload.filename, data)
        last = None
        msgs = []
        for name, pdf, mid in files:
            last, msg = await service.import_invoice(s, pdf, name, mid, source="Upload")
            msgs.append(msg)
    except invoice_import.ImportError_ as e:
        return redirect("/admin/billings/new", f"Import fehlgeschlagen: {e}")
    return redirect(f"/admin/billings/{last.id}" if last else "/", " · ".join(msgs))


# --------------------------------------------------------------------------- Einstellungen
@app.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request, s: Session = Depends(get_session)):
    return render(request, "settings.html", st=get_settings(s), ha_url=config.ha_url,
                  ha_token_set=bool(config.ha_token), house_entities=service.HOUSE_ENTITIES,
                  victron=service.VICTRON_HINTS,
                  smtp=config, mail_ok=mailer.configured(), imap_ok=mailbox.configured(),
                  vrm_ok=vrm.configured(), vrm_mix=vrm.MIX_FIELDS,
                  ignored=s.query(IgnoredInvoice).order_by(IgnoredInvoice.created_at.desc()).all())


@app.post("/admin/settings")
async def settings_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    data = {k: (normalize_spec(str(v)) if k.startswith("entity_") else str(v).strip()) for k, v in form.items()}
    for flag in ("mail_auto_send", "victron_enabled", "owner_free_own_energy", "imap_forwarded"):  # Checkboxen
        data[flag] = "1" if form.get(flag) else ""
    save_settings(s, data)
    # Einzelobjekt: Objektangaben der Einstellungen gelten für das (einzige) Gebäude
    rows = s.query(Building).limit(2).all()
    if len(rows) == 1 and any(k in form for k in ("building_id", "building_address", "building_title")):
        bld = rows[0]
        bld.code = data.get("building_id", bld.code) or bld.code
        bld.address = data.get("building_address", bld.address)
        bld.title = data.get("building_title", bld.title) or bld.title
        if not bld.name or bld.name == "Mein Gebäude":
            bld.name = (bld.address or "").split(",")[0].strip() or bld.name
    s.commit()
    return redirect("/admin/settings", "Gespeichert")


@app.post("/admin/settings/test")
async def settings_test():
    try:
        msg = await HAClient(config.ha_url, config.ha_token).check()
        return redirect("/admin/settings", f"Verbindung OK: {msg}")
    except Exception as e:  # noqa: BLE001
        return redirect("/admin/settings", f"Verbindung fehlgeschlagen: {e}")


@app.post("/admin/settings/testmail")
async def settings_testmail(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    to = str(form.get("test_to", "") or form.get("to", "")).strip()
    if not mailer.configured():
        return redirect("/admin/settings#mail", "SMTP ist nicht eingerichtet (SMTP_HOST / SMTP_FROM in Portainer).")
    if not to:
        return redirect("/admin/settings#mail", "Bitte eine Empfängeradresse für die Test-E-Mail eintragen.")
    try:
        html = mailer.render_html(
            title="Test-E-Mail", preheader="Der E-Mail-Versand funktioniert.",
            paragraphs=[f"Der E-Mail-Versand von {branding.admin_name(get_settings(s))} funktioniert. ✅",
                        "So sehen formatierte Mails aus – Abrechnungen enthalten zusätzlich eine Übersicht mit Betrag, "
                        "Fälligkeit und Konto sowie das PDF im Anhang."],
            facts=[("Server", f"{config.smtp_host}:{config.smtp_port} ({config.smtp_security})", False),
                   ("Absender", config.smtp_from, False)],
            footer=service.mail_footer(get_settings(s)))
        mailer.send_mail([to], f"Test {branding.admin_name(get_settings(s))}",
                         f"Der E-Mail-Versand funktioniert.\n\nServer: {config.smtp_host}:{config.smtp_port} "
                         f"({config.smtp_security})\nAbsender: {config.smtp_from}\n", [], html=html)
        return redirect("/admin/settings#mail", f"Test-E-Mail an {to} verschickt – bitte Posteingang (und Spam) prüfen.")
    except Exception as e:  # noqa: BLE001
        return redirect("/admin/settings#mail", f"E-Mail fehlgeschlagen: {mailer.explain(e)}")


@app.post("/admin/settings/testimap")
async def settings_testimap(request: Request):
    """Postfach prüfen mit den Absender-Angaben aus dem Formular (ohne Speichern, ohne Import)."""
    form = await request.form()
    if not mailbox.configured():
        return redirect("/admin/settings#eingang", "IMAP ist nicht eingerichtet (IMAP_HOST / IMAP_USER / IMAP_PASSWORD).")
    patterns = mailbox.sender_patterns({"imap_senders": str(form.get("imap_senders", ""))})
    try:
        msg = await asyncio.to_thread(mailbox.test_connection, patterns, bool(form.get("imap_forwarded")))
    except Exception as e:  # noqa: BLE001
        msg = f"Postfach-Test fehlgeschlagen: {e}"
    return redirect("/admin/settings#eingang", msg)


# --------------------------------------------------------------------------- WhatsApp über n8n
WA_KEYS = ("wa_mode", "n8n_webhook_url", "n8n_app_url", "wa_provider", "wa_message", "wa_template_name", "wa_template_lang")


@app.get("/admin/whatsapp", response_class=HTMLResponse)
def whatsapp_page(request: Request, s: Session = Depends(get_session)):
    st = get_settings(s)
    secret = whatsapp.ensure_secret(s, st)
    flows = {k: whatsapp.flow_json(k, st, secret) for k in whatsapp.PROVIDERS}
    last = json.loads(st["n8n_last_test"]) if st["n8n_last_test"] else None
    verify = _wa_verify_token(s, st)
    base = config.app_base_url
    return render(request, "whatsapp.html", st=st, secret=secret, flows=flows, providers=whatsapp.PROVIDERS,
                  modes=whatsapp.MODES, native_ok=wa_cloud.configured(), phone_id=config.wa_phone_number_id,
                  app_secret_set=bool(config.wa_app_secret), verify_token=verify,
                  meta_webhook=(base + "/api/whatsapp/webhook") if base else "",
                  app_url=whatsapp.app_url(st), path=whatsapp.WEBHOOK_PATH, header=whatsapp.HEADER, last=last,
                  parties=s.query(Party).order_by(Party.sort, Party.id).all())


@app.post("/admin/whatsapp")
async def whatsapp_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    data = {k: str(form.get(k, "")).strip() for k in WA_KEYS}
    data["n8n_app_url"] = data["n8n_app_url"].rstrip("/")
    if data["wa_mode"] not in whatsapp.MODES:
        data["wa_mode"] = "n8n"
    data["n8n_pdf_base64"] = "1" if form.get("n8n_pdf_base64") else ""
    if form.get("new_secret"):
        data["n8n_secret"] = secrets.token_urlsafe(24)
    save_settings(s, data)
    s.commit()
    return redirect("/admin/whatsapp", "Neues Token erzeugt – Flow in n8n neu kopieren!" if form.get("new_secret") else "Gespeichert")


def _wa_verify_token(s: Session, st: dict) -> str:
    if not st.get("wa_verify_token"):
        st["wa_verify_token"] = secrets.token_urlsafe(18)
        save_settings(s, {"wa_verify_token": st["wa_verify_token"]})
        s.commit()
    return st["wa_verify_token"]


@app.post("/admin/whatsapp/check")
def whatsapp_check():
    try:
        info = wa_cloud.CloudClient().info()
        return redirect("/admin/whatsapp", f"Cloud API OK: {info.get('verified_name', '?')} · {info.get('display_phone_number', '?')}"
                                     f" · Qualität {info.get('quality_rating', '?')}")
    except Exception as e:  # noqa: BLE001
        return redirect("/admin/whatsapp", f"Cloud API: {e}")


@app.post("/admin/whatsapp/test")
async def whatsapp_test(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    st = get_settings(s)
    secret = whatsapp.ensure_secret(s, st)
    phone = str(form.get("phone", "")).strip()
    if whatsapp.native(st):
        to = whatsapp.normalize_phone(phone)
        try:
            wamid = wa_cloud.CloudClient().send_document(
                to, service.test_pdf(), "Test.pdf", st["wa_template_name"], st["wa_template_lang"],
                ["Test", "Testzeitraum", "0,00 €"])
            save_settings(s, {"n8n_last_test": json.dumps({
                "ok": True, "at": datetime.now().isoformat(timespec="seconds"), "status": "gesendet",
                "raw_status": "sent", "message_id": wamid, "provider": "Cloud API direkt", "error": ""})})
            s.commit()
            return redirect("/admin/whatsapp", f"Test an +{to} gesendet – Zustellstatus erscheint unten, sobald Meta ihn meldet.")
        except Exception as e:  # noqa: BLE001
            return redirect("/admin/whatsapp", f"Test fehlgeschlagen: {e}")
    try:
        payload = whatsapp.build_payload(
            st, secret, bid=0, pid=0, name="Test", unit_id="TEST", phone=phone, email="", period="Test",
            total=0.0, total_text="0,00 €", due=date.today().isoformat(),
            message="Test der WhatsApp-Anbindung von ImmoVerwaltung ✅", filename="Test.pdf",
            pdf=service.test_pdf(), test=True)
        save_settings(s, {"n8n_last_test": ""})
        s.commit()
        msg = whatsapp.post(st, secret, payload)
        return redirect("/admin/whatsapp", f"Test an +{payload['phone']} {msg} – Rückmeldung erscheint unten (Seite neu laden).")
    except Exception as e:  # noqa: BLE001
        return redirect("/admin/whatsapp", f"Test fehlgeschlagen: {e}")


@app.get("/admin/whatsapp/flow/{provider}.json")
def whatsapp_flow(provider: str, s: Session = Depends(get_session)):
    if provider not in whatsapp.PROVIDERS:
        raise HTTPException(404)
    st = get_settings(s)
    return Response(whatsapp.flow_json(provider, st, whatsapp.ensure_secret(s, st)), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="n8n-nebenkosten-whatsapp-{provider}.json"'})


def _n8n_secret(s: Session) -> str:
    return get_settings(s)["n8n_secret"]


@app.get("/api/n8n/invoice/{bid}/{pid}.pdf")
def n8n_invoice_pdf(bid: int, pid: int, exp: int = 0, sig: str = "", s: Session = Depends(get_session)):
    if not whatsapp.verify(_n8n_secret(s), bid, pid, exp, sig):
        raise HTTPException(403, "Link ungültig oder abgelaufen")
    b = _get_billing(s, bid)
    p = _party_or_404(b, pid)
    return Response(invoice_pdf(b, p), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_name(b, p)}"', "Cache-Control": "no-store"})


@app.get("/api/n8n/test.pdf")
def n8n_test_pdf(exp: int = 0, sig: str = "", s: Session = Depends(get_session)):
    if not whatsapp.verify(_n8n_secret(s), 0, 0, exp, sig):
        raise HTTPException(403, "Link ungültig oder abgelaufen")
    return Response(service.test_pdf(), media_type="application/pdf")


@app.post("/api/n8n/status")
async def n8n_status(request: Request, s: Session = Depends(get_session)):
    secret = _n8n_secret(s)
    if not secret or not secrets.compare_digest(request.headers.get(whatsapp.HEADER, ""), secret):
        raise HTTPException(403, "Token falsch")
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "JSON erwartet")
    try:
        return {"ok": True, "result": service.wa_status(s, data)}
    except KeyError:
        raise HTTPException(404, "Abrechnung unbekannt")


@app.get("/api/whatsapp/webhook")
def meta_webhook_verify(request: Request, s: Session = Depends(get_session)):
    """Einrichtung des Webhooks in der Meta-App (hub.challenge zurückgeben)."""
    q = request.query_params
    token = get_settings(s)["wa_verify_token"]
    if q.get("hub.mode") == "subscribe" and token and secrets.compare_digest(q.get("hub.verify_token", ""), token):
        return Response(q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403, "Verify-Token falsch")


@app.post("/api/whatsapp/webhook")
async def meta_webhook(request: Request, s: Session = Depends(get_session)):
    """Zustellstatus von Meta (sent / delivered / read / failed)."""
    body = await request.body()
    if not wa_cloud.check_signature(body, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(403, "Signatur ungültig (WA_APP_SECRET prüfen)")
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, "JSON erwartet")
    return {"ok": True, "matched": service.wa_cloud_status(s, wa_cloud.statuses(payload))}


_entity_cache: dict = {"at": 0.0, "data": None}


@app.get("/api/entities")
async def api_entities(refresh: bool = False, s: Session = Depends(get_session)):
    """Zähler-/Leistungssensoren aus Home Assistant + virtuelle Victron-Entitäten des Loggers."""
    st_ = get_settings(s)
    virtual = victron.virtual_entities() if st_["victron_enabled"] else []
    if vrm.configured() and st_["vrm_site_id"]:
        virtual += vrm.virtual_entities()
    if not (config.ha_url and config.ha_token):
        if virtual:
            return virtual
        return JSONResponse({"error": "Home Assistant ist nicht konfiguriert (HA_URL / HA_TOKEN)."}, 503)
    if refresh or _entity_cache["data"] is None or time.time() - _entity_cache["at"] > 60:
        try:
            out = await HAClient(config.ha_url, config.ha_token).entities()
        except Exception as e:  # noqa: BLE001
            if virtual:
                return virtual
            return JSONResponse({"error": f"Home Assistant nicht erreichbar: {e}"}, 502)
        _entity_cache.update(at=time.time(), data=out)
    return virtual + _entity_cache["data"]


@app.get("/api/sources")
async def api_sources(refresh: bool = False, s: Session = Depends(get_session)):
    """Auswahl-Dialog: Entitäten je Datenquelle mit Status (eingerichtet / erreichbar / Hinweis)."""
    st_ = get_settings(s)
    out = {}
    if not (config.ha_url and config.ha_token):
        out["ha"] = {"ok": False, "hint": "HA_URL / HA_TOKEN in der .env bzw. in Portainer setzen.", "entities": []}
    else:
        try:
            if refresh or _entity_cache["data"] is None or time.time() - _entity_cache["at"] > 60:
                _entity_cache.update(at=time.time(), data=await HAClient(config.ha_url, config.ha_token).entities())
            out["ha"] = {"ok": True, "hint": "", "entities": _entity_cache["data"]}
        except Exception as e:  # noqa: BLE001
            out["ha"] = {"ok": False, "hint": f"Home Assistant nicht erreichbar: {e}", "entities": []}
    if st_["victron_enabled"]:
        err = victron.logger.status.get("last_error")
        out["victron"] = {"ok": True, "hint": f"Logger-Fehler: {err}" if err else "",
                          "entities": victron.virtual_entities()}
    else:
        out["victron"] = {"ok": False, "hint": "Unter Einstellungen → „Victron direkt“ den Logger aktivieren.",
                          "entities": []}
    if vrm.configured() and st_["vrm_site_id"]:
        out["vrm"] = {"ok": True, "hint": "", "entities": vrm.virtual_entities()}
    else:
        out["vrm"] = {"ok": False, "hint": "VRM_TOKEN setzen und unter Einstellungen → VRM die Anlage suchen.",
                      "entities": []}
    return out


# --------------------------------------------------------------------------- Victron direkt
@app.post("/admin/victron/discover")
async def victron_discover(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    if "victron_host" in form:  # Knopf sitzt im Einstellungsformular: Eingaben zuerst übernehmen
        save_settings(s, {"victron_host": str(form.get("victron_host", "")).strip(),
                          "victron_port": str(form.get("victron_port", "502")).strip() or "502",
                          "victron_enabled": "1" if form.get("victron_enabled") else ""})
    st = get_settings(s)
    if not st["victron_host"]:
        return redirect("/admin/settings", "Bitte zuerst die IP des GX eintragen und speichern.")
    reader = victron.ModbusReader(st["victron_host"], int(st["victron_port"] or 502))
    try:
        units = await victron.discover(reader)
    except Exception as e:  # noqa: BLE001
        return redirect("/admin/settings", f"Victron: {e}")
    finally:
        await reader.close()
    save_settings(s, {"victron_units": json.dumps(units)})
    victron.logger.units = units
    names = {"system": "System", "vebus": "VE.Bus", "battery": "Batteriewächter", "grid": "Energiezähler"}
    found = ", ".join(f"{names[k]} = Unit {v}" for k, v in units.items() if v is not None) or "nichts"
    missing = ", ".join(names[k] for k, v in units.items() if v is None)
    return redirect("/admin/settings", f"Victron gefunden: {found}" + (f" · nicht gefunden: {missing}" if missing else ""))


@app.get("/admin/victron/compare", response_class=HTMLResponse)
async def victron_compare(request: Request, hours: int = 24, s: Session = Depends(get_session)):
    """Abgleich HA ↔ Victron-Logger für die letzten N vollen Stunden."""
    from zoneinfo import ZoneInfo

    hours = max(1, min(hours, 24 * 62))
    t1 = datetime.now(ZoneInfo(config.timezone)).replace(minute=0, second=0, microsecond=0)
    t0 = t1 - timedelta(hours=hours)
    rows = await service.compare_period(s, t0, t1)
    return render(request, "victron_compare.html", rows=rows, hours=hours, t0=t0, t1=t1, st=get_settings(s))


@app.post("/admin/vrm/sites")
async def vrm_sites(request: Request, s: Session = Depends(get_session)):
    """VRM-Anlagen des Tokens suchen; bei genau einer Anlage wird sie direkt übernommen."""
    form = await request.form()
    if form.get("vrm_site_id"):
        save_settings(s, {"vrm_site_id": str(form.get("vrm_site_id")).strip()})
    try:
        sites = await vrm.VRMClient(config.vrm_token).installations()
    except Exception as e:  # noqa: BLE001
        return redirect("/admin/settings", f"VRM: {e}")
    if len(sites) == 1 and not get_settings(s)["vrm_site_id"]:
        save_settings(s, {"vrm_site_id": str(sites[0]["id"])})
    listing = ", ".join(f"{x['name']} = {x['id']}" for x in sites) or "keine"
    return redirect("/admin/settings", f"VRM-Anlagen: {listing}")


@app.get("/api/victron/status")
def victron_status():
    return victron.logger.status


# --------------------------------------------------------------------------- Gebäude (Multi-Site)
MAX_PHOTO = 2_000_000


def _decode_photo(data_url: str) -> Optional[bytes]:
    """Data-URL aus dem Zuschnitt-Editor → JPEG/PNG-Bytes (geprüft, max. 2 MB)."""
    import base64 as _b64

    m = re.match(r"data:image/(jpeg|png);base64,(.+)$", data_url or "", re.S)
    if not m:
        return None
    try:
        raw = _b64.b64decode(m.group(2), validate=True)
    except ValueError:
        return None
    if len(raw) > MAX_PHOTO or not (raw[:3] == b"\xff\xd8\xff" or raw[:8] == b"\x89PNG\r\n\x1a\n"):
        return None
    return raw


@app.get("/admin/buildings", response_class=HTMLResponse)
def buildings_page(request: Request, s: Session = Depends(get_session)):
    rows = _buildings(s)
    counts = {b.id: s.query(Party).filter(Party.building_id == b.id, Party.active.is_(True)).count() for b in rows}
    managers: dict = {}
    for u in s.query(User).filter(User.role == "admin").all():
        for bid in u.building_ids or []:
            managers.setdefault(int(bid), []).append(u.name or u.username)
    return render(request, "buildings.html", buildings=rows, counts=counts, managers=managers,
                  is_super=scope.is_super())


@app.get("/admin/buildings/{bid}", response_class=HTMLResponse)
def building_edit(request: Request, bid: int, s: Session = Depends(get_session)):
    if bid == 0:
        if not scope.is_super():
            raise HTTPException(403, "Nur Super-Admins legen Gebäude an")
        b = Building(active=True, title="Nebenkostenabrechnung")
    else:
        b = s.get(Building, bid)
        if b is None or not scope.can(b.id):
            raise HTTPException(404)
    admins = s.query(User).filter(User.role == "admin").order_by(User.username).all()
    return render(request, "building_edit.html", b=b, bid=bid, admins=admins, is_super=scope.is_super())


@app.post("/admin/buildings/{bid}")
async def building_save(request: Request, bid: int, s: Session = Depends(get_session)):
    form = await request.form()
    if bid == 0:
        if not scope.is_super():
            raise HTTPException(403)
        b = Building(active=True)
        s.add(b)
    else:
        b = s.get(Building, bid)
        if b is None or not scope.can(b.id):
            raise HTTPException(404)
    if form.get("delete") and scope.is_super() and bid:
        if s.query(Party).filter(Party.building_id == b.id).count() or \
                s.query(Billing).filter(Billing.building_id == b.id).count():
            return redirect(f"/admin/buildings/{bid}", "Gebäude hat noch Parteien oder Abrechnungen – erst umziehen/löschen.")
        s.delete(b)
        s.commit()
        return redirect("/admin/buildings", "Gebäude gelöscht")
    b.code = str(form.get("code", "")).strip()[:50]
    b.name = str(form.get("name", "")).strip()[:200]
    b.address = str(form.get("address", "")).strip()
    b.title = str(form.get("title", "")).strip()[:200] or "Nebenkostenabrechnung"
    if scope.is_super():
        b.active = bool(form.get("active"))
    msg = "Gespeichert"
    if form.get("photo_remove"):
        b.photo, b.photo_updated = None, datetime.now()
    elif form.get("photo_data"):
        raw = _decode_photo(str(form.get("photo_data")))
        if raw is None:
            return redirect(f"/admin/buildings/{bid}", "Foto ungültig oder zu groß (max. 2 MB, JPEG/PNG).")
        b.photo, b.photo_updated = raw, datetime.now()
        msg += " · Foto aktualisiert"
    s.flush()
    if scope.is_super() and form.get("managers_form"):  # zuständige Verwalter
        chosen = {int(x) for x in form.getlist("managers") if str(x).isdigit()}
        for u in s.query(User).filter(User.role == "admin").all():
            ids = {int(x) for x in (u.building_ids or [])}
            ids = ids | {b.id} if u.id in chosen else ids - {b.id}
            u.building_ids = sorted(ids)
    s.commit()
    return redirect(f"/admin/buildings/{b.id}", msg)


@app.get("/photo/building/{bid}.jpg")
def building_photo(request: Request, bid: int, s: Session = Depends(get_session)):
    """Gebäudefoto für Verwalter des Gebäudes und Mieter, deren Partei im Gebäude liegt."""
    b = s.get(Building, bid)
    me = request.state.user
    allowed = scope.can(bid)
    if not allowed and me is not None and me.party_id:
        p = s.get(Party, me.party_id)
        allowed = p is not None and p.building_id == bid
    if b is None or not b.photo or not allowed:
        raise HTTPException(404)
    mime = "image/png" if b.photo[:4] == b"\x89PNG" else "image/jpeg"
    return Response(b.photo, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


# --------------------------------------------------------------------------- Parteien
def _buildings(s: Session) -> list:
    """Gebäude, die der angemeldete Benutzer sehen darf."""
    return scope.filter_query(s.query(Building), Building.id).order_by(Building.sort, Building.id).all()


def _pick_building(s: Session, value) -> Optional[int]:
    """Gebäude aus dem Formular übernehmen (nur erlaubte), sonst das erste erlaubte."""
    allowed = [b.id for b in _buildings(s)]
    try:
        bid = int(value or 0)
    except (TypeError, ValueError):
        bid = 0
    return bid if bid in allowed else (allowed[0] if allowed else None)


@app.get("/admin/parties", response_class=HTMLResponse)
def parties_page(request: Request, s: Session = Depends(get_session)):
    q = scope.filter_query(s.query(Party), Party.building_id)
    parties = q.order_by(Party.active.desc(), Party.sort, Party.id).all()
    return render(request, "parties.html", parties=parties, buildings={b.id: b for b in _buildings(s)})


@app.get("/admin/parties/{pid}", response_class=HTMLResponse)
def party_edit(request: Request, pid: int, s: Session = Depends(get_session)):
    p = Party(name="", meters=[], active=True, is_owner=False, sort=0) if pid == 0 else s.get(Party, pid)
    if p is None or (pid and not scope.can(p.building_id)):
        raise HTTPException(404)
    return render(request, "party_edit.html", p=p, pid=pid, buildings=_buildings(s))


@app.post("/admin/parties/{pid}")
async def party_save(request: Request, pid: int, s: Session = Depends(get_session)):
    form = await request.form()
    if form.get("delete"):
        p = s.get(Party, pid)
        if p and scope.can(p.building_id):
            s.delete(p)
            s.commit()
        return redirect("/admin/parties", "Gelöscht")
    p = Party() if pid == 0 else s.get(Party, pid)
    if p is None or (pid and not scope.can(p.building_id)):
        raise HTTPException(404)
    p.building_id = _pick_building(s, form.get("building_id") or p.building_id)
    p.name = str(form.get("name", "")).strip()
    p.unit = str(form.get("unit", "")).strip()
    p.unit_id = str(form.get("unit_id", "")).strip()
    p.address = str(form.get("address", "")).strip()
    p.email = str(form.get("email", "")).strip()
    p.phone = str(form.get("phone", "")).strip()
    p.portal = bool(form.get("portal"))
    p.channel = str(form.get("channel", "email")) if form.get("channel") in ("email", "whatsapp", "both") else "email"
    p.meters = [m for m in (normalize_spec(str(v)) for v in form.getlist("meters")) if m]
    p.is_owner = bool(form.get("is_owner"))
    p.active = bool(form.get("active"))
    p.sort = int(parse_float(form.get("sort"), 0) or 0)
    if p.is_owner:
        for other in s.query(Party).filter(Party.is_owner.is_(True)).all():
            if other is not p:
                other.is_owner = False
    if pid == 0:
        s.add(p)
    s.commit()
    return redirect("/admin/parties", "Gespeichert")


# --------------------------------------------------------------------------- Fixkosten
@app.get("/admin/costs", response_class=HTMLResponse)
def costs_page(request: Request, s: Session = Depends(get_session)):
    return render(
        request, "costs.html",
        costs=s.query(FixedCost).order_by(FixedCost.id).all(),
        allocations=s.query(Allocation).order_by(Allocation.id).all(),
        parties={p.id: p.name for p in s.query(Party).all()},
    )


@app.get("/admin/costs/fixed/{cid}", response_class=HTMLResponse)
def fixed_edit(request: Request, cid: int, s: Session = Depends(get_session)):
    c = FixedCost(name="", amount_gross=0.0, party_ids=[], active=True) if cid == 0 else s.get(FixedCost, cid)
    if c is None:
        raise HTTPException(404)
    return render(request, "fixed_edit.html", c=c, cid=cid, parties=service.active_parties(s))


@app.post("/admin/costs/fixed/{cid}")
async def fixed_save(request: Request, cid: int, s: Session = Depends(get_session)):
    form = await request.form()
    if form.get("delete"):
        c = s.get(FixedCost, cid)
        if c:
            s.delete(c)
            s.commit()
        return redirect("/admin/costs", "Gelöscht")
    c = FixedCost() if cid == 0 else s.get(FixedCost, cid)
    if c is None:
        raise HTTPException(404)
    c.name = str(form.get("name", "")).strip()
    c.amount_gross = parse_float(form.get("amount_gross")) or 0.0
    c.party_ids = [int(x) for x in form.getlist("party_ids")]
    c.active = bool(form.get("active"))
    c.reserve = bool(form.get("reserve"))
    c.reserve_label = " ".join(str(form.get("reserve_label", "")).split())[:60]
    if cid == 0:
        s.add(c)
    s.commit()
    return redirect("/admin/costs", "Gespeichert")


@app.get("/admin/costs/alloc/{aid}", response_class=HTMLResponse)
def alloc_edit(request: Request, aid: int, s: Session = Depends(get_session)):
    a = (Allocation(name="", source_type="energy", source_entity="", source_unit="m³", price_source="custom", default_amount=0.0,
                    key_type="percent", key_unit="", key={}, active=True)
         if aid == 0 else s.get(Allocation, aid))
    if a is None:
        raise HTTPException(404)
    return render(request, "alloc_edit.html", a=a, aid=aid, parties=service.active_parties(s))


@app.post("/admin/costs/alloc/{aid}")
async def alloc_save(request: Request, aid: int, s: Session = Depends(get_session)):
    form = await request.form()
    if form.get("delete"):
        a = s.get(Allocation, aid)
        if a:
            s.delete(a)
            s.commit()
        return redirect("/admin/costs", "Gelöscht")
    a = Allocation() if aid == 0 else s.get(Allocation, aid)
    if a is None:
        raise HTTPException(404)
    a.name = str(form.get("name", "")).strip()
    a.source_type = str(form.get("source_type", "energy"))
    a.source_entity = normalize_spec(str(form.get("source_entity", "")))
    a.source_unit = str(form.get("source_unit", "")).strip() or "m³"
    a.price_source = str(form.get("price_source", "custom"))
    a.default_amount = parse_float(form.get("default_amount")) or 0.0
    a.key_type = str(form.get("key_type", "entity"))
    a.key_unit = str(form.get("key_unit", "")).strip()
    key = {}
    for p in service.active_parties(s):
        if a.key_type == "percent":
            raw = str(form.get(f"keypct_{p.id}", form.get(f"key_{p.id}", ""))).strip()
            if raw:
                key[str(p.id)] = parse_float(raw)
        else:
            raw = normalize_spec(str(form.get(f"keyent_{p.id}", form.get(f"key_{p.id}", ""))))
            if raw:
                key[str(p.id)] = raw
    a.key = key
    a.reserve = bool(form.get("reserve"))
    a.reserve_label = " ".join(str(form.get("reserve_label", "")).split())[:60]
    a.active = bool(form.get("active"))
    if aid == 0:
        s.add(a)
    s.commit()
    return redirect("/admin/costs", "Gespeichert")


# --------------------------------------------------------------------------- Abrechnungen
def _apply_bill_form(b: Billing, form, st: dict) -> None:
    """Übernimmt die Rechnungsfelder aus dem Formular."""
    b.title = str(form.get("title", b.title or "")).strip()
    b.invoice_no = str(form.get("invoice_no", b.invoice_no or "")).strip()
    b.period_start = date.fromisoformat(str(form.get("period_start")))
    b.period_end = date.fromisoformat(str(form.get("period_end")))
    b.grid_kwh = parse_float(form.get("grid_kwh"), None)
    b.energy_cost_net = parse_float(form.get("energy_cost_net")) or 0.0
    b.fixed_cost_net = parse_float(form.get("fixed_cost_net")) or 0.0
    b.spot_price_ct = parse_float(form.get("spot_price_ct")) or 0.0
    b.vat_rate = (parse_float(form.get("vat_rate"), parse_float(st["vat_rate"])) or 0.0) / 100
    b.battery_rate_ct = parse_float(form.get("battery_rate_ct"), parse_float(st["battery_rate_ct"])) or 0.0
    b.pv_rate_ct = parse_float(form.get("pv_rate_ct"), parse_float(st["pv_rate_ct"])) or 0.0


@app.get("/admin/billings/new", response_class=HTMLResponse)
def billing_new(request: Request, s: Session = Depends(get_session)):
    st = get_settings(s)
    end = date.today().replace(day=1) - timedelta(days=1)
    return render(request, "billing_new.html", start=end.replace(day=1), end=end, st=st, buildings=_buildings(s))


@app.post("/admin/billings")
async def billing_create(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    st = get_settings(s)
    b = Billing(values={}, amounts={}, result={}, sent={})
    _apply_bill_form(b, form, st)
    b.building_id = _pick_building(s, form.get("building_id"))
    if not b.title:
        b.title = f"Nebenkosten {b.period_start.strftime('%m/%Y')}"
    s.add(b)
    s.commit()
    msg = "Angelegt"
    if form.get("fetch") and ((config.ha_url and config.ha_token) or st["victron_enabled"]):
        try:
            msg = service.fetch_message(await service.fetch_values(s, b))
        except Exception as e:  # noqa: BLE001
            msg = f"Home Assistant nicht erreichbar: {e}"
    service.recompute(s, b)
    s.commit()
    return redirect(f"/admin/billings/{b.id}", msg)


def _get_billing(s: Session, bid: int) -> Billing:
    b = s.get(Billing, bid)
    if b is None or not scope.can(b.building_id):  # Verwalter: nur eigene Gebäude
        raise HTTPException(404)
    return b


@app.get("/admin/billings/{bid}", response_class=HTMLResponse)
def billing_view(request: Request, bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    allocs = [a for a in s.query(Allocation).filter(Allocation.active.is_(True)).all()
              if a.source_type == "amount" or (a.source_type == "quantity" and a.price_source != "water")]
    st = get_settings(s)
    contacts = {}
    for p in s.query(Party).all():
        by_mail, by_wa = service.channels(p)
        contacts[p.id] = {"mail": by_mail and bool(p.email), "wa": by_wa and bool(p.phone),
                          "want_mail": by_mail, "want_wa": by_wa}
    return render(request, "billing.html", b=b, r=b.result or {}, buildings=_buildings(s),
                  entities=service.required_entities(s, b.building_id),
                  amount_allocs=allocs, st=st, contacts=contacts, mail_ok=mailer.configured(),
                  wa_ok=whatsapp.configured(st), send_ok=service.can_send(st),
                  compare=service.victron_comparison(s, b))


def _report_msg(report: list[tuple[str, bool, str]]) -> str:
    if not report:
        return "Nichts verschickt (keine offenen Parteien mit E-Mail-Adresse bzw. WhatsApp-Nummer)."
    return "Versand: " + "; ".join(f"{n} {'✔' if ok else '✘ ' + m}" for n, ok, m in report)


@app.post("/admin/billings/{bid}")
async def billing_save(request: Request, bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    form = await request.form()
    action = form.get("action", "save")
    st = get_settings(s)

    if action == "delete":
        blocked = service.delete_billing(s, b, block_reimport=not form.get("allow_reimport"))
        return redirect("/admin", "Abrechnung gelöscht" + (" · wird beim Postfach-Abruf nicht erneut importiert"
                                                           if blocked else ""))
    if action == "reopen":
        b.status = "draft"
        s.commit()
        return redirect(f"/admin/billings/{bid}", "Wieder zur Bearbeitung geöffnet")
    if action in ("publish", "unpublish"):
        b.published = action == "publish"
        s.commit()
        if not b.published:
            return redirect(f"/admin/billings/{bid}", "Aus dem Mieterportal zurückgezogen")
        n = notify.billing_published(s, b)
        return redirect(f"/admin/billings/{bid}", "Im Mieterportal veröffentlicht" + (f" · {n} Mieter per Push benachrichtigt" if n else ""))
    if action.startswith("send"):
        if b.status != "final":
            return redirect(f"/admin/billings/{bid}", "Bitte zuerst abschließen, dann versenden.")
        ids = None if action == "send_all" else [int(action.split(":")[1])]
        report = service.send_invoices(s, b, ids)
        s.commit()
        return redirect(f"/admin/billings/{bid}", _report_msg(report))
    if b.status == "final":
        return redirect(f"/admin/billings/{bid}", "Abrechnung ist abgeschlossen")

    _apply_bill_form(b, form, st)
    if form.get("building_id"):
        b.building_id = _pick_building(s, form.get("building_id"))
    b.notes = str(form.get("notes", "")).strip()
    values = {}
    for k, v in form.items():
        if k.startswith("val__"):
            fv = parse_float(v, None)
            if fv is not None:
                values[k[5:]] = fv
    b.values = values
    b.amounts = {k[8:]: parse_float(v) for k, v in form.items() if k.startswith("amount__")}

    msg = "Gespeichert"
    if action == "fetch":
        try:
            msg = service.fetch_message(await service.fetch_values(s, b))
        except Exception as e:  # noqa: BLE001
            msg = f"Home Assistant: {e}"
    service.recompute(s, b)
    if action == "finalize":
        b.status = "final"
        msg = "Abrechnung abgeschlossen"
        if st["portal_auto_publish"]:
            b.published = True
            msg += " · im Mieterportal veröffentlicht"
        if st["mail_auto_send"] and service.can_send(st):
            s.commit()
            msg += " · " + _report_msg(service.send_invoices(s, b, only_unsent=True))
    s.commit()
    if action == "finalize" and b.published:
        if n := notify.billing_published(s, b):
            msg += f" · {n} Mieter per Push benachrichtigt"
    return redirect(f"/admin/billings/{bid}", msg)


def _party_or_404(b: Billing, pid: int) -> dict:
    p = party_result(b, pid)
    if p is None:
        raise HTTPException(404, "Partei nicht in dieser Abrechnung")
    return p


@app.get("/admin/billings/{bid}/source.pdf")
def billing_source(bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    if not b.source_file or not os.path.exists(b.source_file):
        raise HTTPException(404, "Keine Original-Rechnung gespeichert")
    return FileResponse(b.source_file, media_type="application/pdf")


@app.get("/admin/billings/{bid}/invoice/{pid}.pdf")
def invoice_pdf_view(bid: int, pid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    p = _party_or_404(b, pid)
    return Response(invoice_pdf(b, p), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{pdf_name(b, p)}"',
                             "Cache-Control": "no-store"})


@app.get("/admin/billings/{bid}/invoice/{pid}", response_class=HTMLResponse)
def invoice_view(bid: int, pid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    return HTMLResponse(invoice_html(b, _party_or_404(b, pid)))


@app.get("/admin/billings/{bid}/all.zip")
def invoices_zip(bid: int, s: Session = Depends(get_session)):
    b = _get_billing(s, bid)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in b.result.get("parties", []):
            z.writestr(pdf_name(b, p), invoice_pdf(b, p))
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="Nebenkostenabrechnung_{b.period_start:%Y-%m}.zip"'})
