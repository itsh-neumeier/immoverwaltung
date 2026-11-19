"""Lernende Kategorisierung: Lernmodell, Lernspeicher, KI-Vorschläge (OpenAI gemockt)."""

import json

import httpx
from fastapi.testclient import TestClient

from app import learn
from app.db import Building, FinCategory, FinMemory, FinTxn, SessionLocal
from app.main import app
from tests.test_finance import camt_named, tx
from tests.test_reserves import GIRO, RESERVE


def _import(c, extra=()):
    entries = []
    for m in ("07", "08", "09", "10"):
        entries += [tx(f"2026-{m}-05", -60.0, "REWE Markt GmbH", "EINKAUF FILIALE KARTENZAHLUNG"),
                    tx(f"2026-{m}-12", -45.0, "EDEKA Center", "EINKAUF FILIALE KARTENZAHLUNG"),
                    tx(f"2026-{m}-20", -38.0, "Naturkost Erdgarten GmbH", "EINKAUF FILIALE KARTENZAHLUNG"),
                    tx(f"2026-{m}-22", -25.0, "Spielwelt Huber GmbH", "SPIELWAREN"),
                    tx(f"2026-{m}-25", -15.0, "Max Mustermann", "GESCHENK"),
                    tx(f"2026-{m}-08", -20.0, "DM DROGERIEMARKT", "DROGERIE KARTE"),
                    tx(f"2026-{m}-09", -30.0, "ARAL Tankstelle", "TANKEN KARTE")]
    entries += list(extra)
    c.post("/admin/settings", data={"vat_rate": "19"})
    with SessionLocal() as s:
        b = s.query(Building).first()
        b.account_name = b.reserve_name = "Timo Muster"
        b.account_iban, b.reserve_iban = GIRO, RESERVE
        s.commit()
    c.post("/admin/finanzen/import", files={"file": ("h.xml", camt_named(GIRO, entries, ("2026-11-10", 900.0)),
                                                    "application/xml")})


def test_model_suggests_and_memory_applies():
    with TestClient(app) as c:
        _import(c)
        page = c.get("/admin/finanzen/lernen").text
        assert "Naturkost Erdgarten GmbH" in page and "Lebensmittel" in page
        with SessionLocal() as s:
            leb = s.query(FinCategory).filter(FinCategory.name == "Lebensmittel").one().id
            key = learn.name_key(s.query(FinTxn).filter(FinTxn.name == "Naturkost Erdgarten GmbH").first())
        r = c.post("/admin/finanzen/lernen/accept", data={"source": "lern", "key": key, f"cat_{key}": str(leb),
                                                          f"label_{key}": "Naturkost Erdgarten GmbH", f"p_{key}": "0.8"})
        assert "1 Empfänger im Lernspeicher" in r.text
        from app.finance import Matcher
        with SessionLocal() as s:
            m = Matcher(s)
            rows = s.query(FinTxn).filter(FinTxn.name == "Naturkost Erdgarten GmbH").all()
            assert len(rows) == 4 and all(m.category(t).name == "Lebensmittel" for t in rows)
            mem = s.query(FinMemory).one()
            mid = mem.id
        # neuer Umsatz desselben Empfängers (andere Filialnummer) wird automatisch zugeordnet
        assert learn.name_key(type("T", (), {"name": "Naturkost Erdgarten GmbH 4711", "purpose": ""})()) == key
        c.post(f"/admin/finanzen/lernen/forget/{mid}")
        with SessionLocal() as s:
            assert s.query(FinMemory).count() == 0


def test_ai_categorization_sends_only_merchants(monkeypatch):
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        payload = __import__("json").loads(json["input"][1]["content"])
        sent["payload"] = payload
        z = [{"id": e["id"], "kategorie": "Kinder" if "SPIELWELT" in e["name"].upper() else "unklar", "sicher": 0.9}
             for e in payload["empfaenger"]]
        out = {"output": [{"type": "message", "content": [{"type": "output_text", "text": __import__("json").dumps({"zuordnungen": z})}]}]}
        return httpx.Response(200, json=out, request=httpx.Request("POST", url))

    monkeypatch.setattr(learn.httpx, "post", fake_post)
    with TestClient(app) as c:
        _import(c)
        c.post("/admin/pruefung/settings", data={"openai_key": "sk-test-1234567890", "openai_model": "gpt-5-mini"})
        page = c.get("/admin/finanzen/lernen?show=vorschau").text
        assert "Spielwelt Huber GmbH" in page and "Max Mustermann" in page.split("Nicht gesendet")[1]
        r = c.post("/admin/finanzen/lernen/ki", data={})
        assert "KI: 1 Vorschläge" in r.text and "nicht gesendet (vermutlich Personen)" in r.text
        body = json.dumps(sent["payload"], ensure_ascii=False)
        for w in ("Mustermann", "Timo", "DE89", "-25", "SPIELWAREN", "2026"):
            assert w not in body, w
        assert "Spielwelt Huber GmbH" in body
        key = learn.name_key(type("T", (), {"name": "Spielwelt Huber GmbH", "purpose": ""})())
        with SessionLocal() as s:
            kid = s.query(FinCategory).filter(FinCategory.name == "Kinder").one().id
        r = c.post("/admin/finanzen/lernen/accept", data={"source": "ki", "key": key, f"cat_{key}": str(kid),
                                                          f"label_{key}": "Spielwelt Huber GmbH", f"p_{key}": "0.9"})
        assert "1 Empfänger im Lernspeicher" in r.text and "🤖 KI" in r.text
        with SessionLocal() as s:
            assert s.query(FinMemory).one().source == "ki"


def test_merchant_detection():
    pw = {"MUSTER", "NEUMEIER"}
    assert learn.is_merchant("REWE", pw) and learn.is_merchant("Autohaus Beispiel GmbH", pw)
    assert not learn.is_merchant("Max Mustermann", pw) and not learn.is_merchant("NEUMEIER TIMO", pw)
    assert not learn.is_merchant("Timo Muster", pw)
    assert learn.sanitize("AMAZON MKTP DE 302-1234567-1234567") == "AMAZON MKTP DE"
