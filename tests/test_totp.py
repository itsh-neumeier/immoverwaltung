"""Zwei-Faktor-Anmeldung (TOTP) für Verwalter."""

import base64
import re

import pytest
from fastapi.testclient import TestClient

from app import totp
from app.db import SessionLocal, User
from app.main import app


def test_totp_rfc6238_vector():
    secret = base64.b32encode(b"12345678901234567890").decode()  # RFC 6238, SHA-1
    assert totp.code_at(secret, 59 // 30) == "287082"
    assert totp.code_at(secret, 1111111109 // 30) == "081804"
    assert totp.verify(secret, "287 082", t=59) == 1
    assert totp.verify(secret, "287082", last_counter=1, t=59) is None  # keine Wiederverwendung
    assert totp.verify(secret, "000000", t=59) is None


def test_recovery_codes():
    codes, hashes = totp.new_recovery_codes(3)
    assert len(codes) == 3 and all(re.fullmatch(r"[A-Z2-9]{5}-[A-Z2-9]{5}", c) for c in codes)
    rest = totp.use_recovery(codes[1].lower().replace("-", ""), hashes)
    assert rest is not None and len(rest) == 2
    assert totp.use_recovery(codes[1], rest) is None


def _code(c):
    page = c.get("/account/2fa").text
    secret = re.search(r'class="secret">([A-Z2-7 ]+)<', page).group(1).replace(" ", "")
    return secret, totp.code_at(secret, totp.now_counter())


@pytest.mark.totp
def test_admin_must_set_up_2fa_then_login_needs_code():
    with TestClient(app) as c:
        c.post("/setup", data={"username": "chef", "password": "sehr-geheim-1", "password2": "sehr-geheim-1"})
        # Pflicht: jede Verwalterseite führt zur Einrichtung
        r = c.get("/admin/reserves", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/account/2fa")
        assert c.post("/admin/bank/settings", data={}, follow_redirects=False).status_code == 403
        page = c.get("/account/2fa").text
        assert "<svg" in page and "otpauth://totp/" in page
        secret, code = _code(c)
        assert "Code passt nicht" in c.post("/account/2fa/enable", data={"code": "000000"}).text
        r = c.post("/account/2fa/enable", data={"code": code})
        codes = list(dict.fromkeys(re.findall(r"\b[A-Z2-9]{5}-[A-Z2-9]{5}\b", r.text)))
        assert "Eingerichtet" in r.text and len(codes) == 10
        assert c.get("/admin/reserves").status_code == 200  # Sitzung bleibt gültig

        # Basic-Auth ist mit 2FA gesperrt
        with TestClient(app) as other:
            hdr = {"Authorization": "Basic " + base64.b64encode(b"chef:sehr-geheim-1").decode()}
            assert other.get("/admin/reserves", headers=hdr, follow_redirects=False).status_code == 303

        # neue Anmeldung: Passwort → Code
        c.get("/logout")
        r = c.post("/login", data={"username": "chef", "password": "sehr-geheim-1"}, follow_redirects=False)
        assert r.headers["location"].startswith("/login/2fa")
        assert c.get("/admin", follow_redirects=False).status_code == 303  # noch nicht angemeldet
        assert "Code falsch" in c.post("/login/2fa", data={"code": "123456", "next": "/admin"}).text
        with SessionLocal() as s:  # gleicher Code wie bei der Einrichtung ist verbraucht → nächsten Zeitschritt nehmen
            u = s.query(User).one()
            nxt = totp.code_at(secret, u.totp_last + 1)
        r = c.post("/login/2fa", data={"code": nxt, "next": "/admin"}, follow_redirects=False)
        if r.headers["location"].startswith("/login/2fa"):  # Zeitschritt liegt außerhalb ±1 → Wiederherstellungscode
            r = c.post("/login/2fa", data={"code": codes[0], "next": "/admin"}, follow_redirects=False)
        assert r.headers["location"].startswith("/admin")
        assert c.get("/admin").status_code == 200

        # Wiederherstellungscode funktioniert genau einmal
        c.get("/logout")
        c.post("/login", data={"username": "chef", "password": "sehr-geheim-1"})
        r = c.post("/login/2fa", data={"code": codes[1], "next": "/admin"})
        assert "Wiederherstellungscode" in r.text
        c.get("/logout")
        c.post("/login", data={"username": "chef", "password": "sehr-geheim-1"})
        assert "Code falsch" in c.post("/login/2fa", data={"code": codes[1], "next": "/admin"}).text

        # ohne Zwischenschritt-Cookie kein Code-Login
        with TestClient(app) as fresh:
            r = fresh.post("/login/2fa", data={"code": codes[2]}, follow_redirects=False)
            assert r.headers["location"].startswith("/login")


@pytest.mark.totp
def test_superadmin_resets_2fa_of_other_admin():
    with TestClient(app) as c:
        c.post("/setup", data={"username": "chef", "password": "sehr-geheim-1", "password2": "sehr-geheim-1"})
        _, code = _code(c)
        c.post("/account/2fa/enable", data={"code": code})
        c.post("/admin/users/0", data={"username": "verwalter", "role": "admin", "active": "1", "password": "pass-pass-2"})
        with SessionLocal() as s:
            u = s.query(User).filter(User.username == "verwalter").one()
            u.totp_enabled, u.totp_secret = True, ""
            s.commit()
            uid = u.id
        assert "2FA zurücksetzen" in c.get(f"/admin/users/{uid}").text
        r = c.post(f"/admin/users/{uid}/2fa-reset")
        assert "zurückgesetzt" in r.text
        with SessionLocal() as s:
            assert not s.get(User, uid).totp_enabled
        # Pflicht abschaltbar
        c.post("/admin/users/settings", data={"security_form": "1"})
        with TestClient(app) as v:
            v.post("/login", data={"username": "verwalter", "password": "pass-pass-2"})
            assert v.get("/admin", follow_redirects=False).status_code == 200
