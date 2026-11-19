"""KI-Kassenprüfung: Pseudonymisierung, Datenschutzprüfung, Rückübersetzung der Antwort (OpenAI gemockt)."""

import json
from datetime import date

import httpx
from fastapi.testclient import TestClient

from app import audit
from app.db import AuditReport, Building, ReserveEntry, ReservePlan, SessionLocal
from app.main import app
from tests.test_finance import camt_named, tx
from tests.test_reserves import GIRO, RESERVE

SECRET_WORDS = ["BA-AT9314", "Timo Muster", "REWE", "DE89370400440532013000", "DE02120300000000202051",
                "RUECKBILDUNG", "Arbeitgeber", "Mein Gebäude"]


def _setup(c):
    entries = []
    for m in ("07", "08", "09", "10"):
        entries += [tx(f"2026-{m}-28", 2500.0, "Arbeitgeber GmbH", "LOHN/GEHALT"),
                    tx(f"2026-{m}-05", -120.0, "REWE Markt GmbH"),
                    tx(f"2026-{m}-02", -50.0, "Timo Muster", "RUECKBILDUNG GRUNDSTEUER", other=RESERVE)]
    entries += [tx("2026-11-04", -1500.0, "Autohaus Beispiel GmbH", "RECHNUNG 4711")]
    c.post("/admin/settings", data={"vat_rate": "19"})
    with SessionLocal() as s:
        b = s.query(Building).first()
        b.account_name = b.reserve_name = "Timo Muster"
        b.account_iban, b.reserve_iban = GIRO, RESERVE
        b.bank_reserve_balance, b.bank_reserve_date = 500.0, date(2026, 11, 18)
        p1 = ReservePlan(building_id=b.id, name="Fahrzeug · HU-AU FUER BA-AT9314 (Corsa)", label="HU-AU FUER BA-AT9314",
                         annual_amount=96, interval=1, start="2026-01", funding="sepa", category="privat", active=True)
        p2 = ReservePlan(building_id=b.id, name="Immobilie · Grundsteuer", label="GRUNDSTEUER", annual_amount=600,
                         interval=1, start="2026-01", funding="sepa", category="immobilie", active=True)
        s.add_all([p1, p2])
        s.flush()
        s.add(ReserveEntry(building_id=b.id, ref=f"rp:{p1.id}", name=p1.name, booking_date=date(2026, 6, 1), amount=-30,
                           kind="out"))
        s.add(ReserveEntry(building_id=b.id, ref=f"rp:{p2.id}", name=p2.name, booking_date=date(2026, 6, 1), amount=420,
                           kind="in"))
        s.commit()
        pid1 = p1.id
    c.post("/admin/finanzen/import", files={"file": ("h.xml", camt_named(GIRO, entries, ("2026-11-10", 400.0)),
                                                    "application/xml")})
    return pid1


def test_preview_is_pseudonymized_and_rules_run():
    with TestClient(app) as c:
        _setup(c)
        page = c.get("/admin/pruefung?show=vorschau").text
        assert "Datenschutzprüfung bestanden" in page
        pre = page[page.index('<pre class="payload">'):]
        for w in SECRET_WORDS:
            assert w not in pre, w
        assert "Prüfung/Wartung" in pre and "&#34;T-" in pre and "&#34;E-" in pre
        # ohne Schlüssel: nur Regelprüfung
        r = c.post("/admin/pruefung/run", data={})
        assert "Kein OpenAI-API-Schlüssel" in r.text and "Topf im Minus" in r.text
        assert "Töpfe und Kontostand passen nicht zusammen" in r.text  # 500 € Konto vs. 390 € Töpfe
        assert "HU-AU FUER BA-AT9314" in r.text  # in der App mit echtem Namen
        assert "betrifft: T-" not in r.text and "betrifft: G-" not in r.text


def test_ai_answer_is_mapped_back(monkeypatch):
    sent = {}

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        sent["url"], sent["body"], sent["auth"] = url, json, headers["Authorization"]
        payload = __import__("json").loads(json["input"][1]["content"])
        pot = next(t for t in payload["gebaeude"][0]["toepfe"] if t["stand"] < 0)  # der Topf im Minus
        answer = {"gesamturteil": "kritisch", "zusammenfassung": f"Topf {pot['id']} ist unterdeckt.",
                  "befunde": [{"titel": f"{pot['id']} auffüllen", "bereich": "ruecklagen", "schwere": "hoch",
                               "refs": [pot["id"]], "werte": [{"bezeichnung": "Stand", "betrag": pot["stand"]}],
                               "beschreibung": f"{pot['id']} wurde für die Hauptuntersuchung entnommen.",
                               "massnahme": "Rate erhöhen."}]}
        out = {"output": [{"type": "message", "content": [{"type": "output_text",
                                                           "text": __import__("json").dumps(answer)}]}]}
        return httpx.Response(200, json=out, request=httpx.Request("POST", url))

    monkeypatch.setattr(audit.httpx, "post", fake_post)
    with TestClient(app) as c:
        _setup(c)
        r = c.post("/admin/pruefung/settings", data={"openai_key": "sk-test-1234567890", "openai_model": "gpt-5.6"})
        assert "Gespeichert" in r.text and "sk-test" not in c.get("/admin/pruefung").text  # Schlüssel nie angezeigt
        r = c.post("/admin/pruefung/run", data={})
        body = json.dumps(sent["body"], ensure_ascii=False)
        assert sent["url"].endswith("/responses") and sent["body"]["store"] is False and sent["auth"] == "Bearer sk-test-1234567890"
        assert sent["body"]["text"]["format"]["type"] == "json_schema"
        for w in SECRET_WORDS:
            assert w not in body, w
        # Antwort: Kennung → echter Topfname, VE → Euro
        assert "Fahrzeug · HU-AU FUER BA-AT9314 (Corsa) auffüllen" in r.text and "🤖 KI" in r.text
        assert "-30,00 €" in r.text and "KI: gpt-5.6" in r.text
        with SessionLocal() as s:
            rep = s.query(AuditReport).one()
            ai = [f for f in rep.findings if f["quelle"] == "KI"][0]
            assert ai["werte"][0]["betrag"] == -30.0 and rep.verdict == "kritisch"
            rid = rep.id
        # erledigt abhaken
        c.post(f"/admin/pruefung/{rid}/done", data={"i": "0"})
        with SessionLocal() as s:
            assert s.get(AuditReport, rid).done == [0]


def test_leak_check_blocks_sending(monkeypatch):
    calls = []
    monkeypatch.setattr(audit.httpx, "post", lambda *a, **k: calls.append(1))
    real_collect = audit.collect

    def leaky(s, p):
        data, rules = real_collect(s, p)
        data["versehentlich"] = GIRO  # z. B. Programmierfehler: IBAN im Datensatz
        return data, rules

    monkeypatch.setattr(audit, "collect", leaky)
    with TestClient(app) as c:
        _setup(c)
        c.post("/admin/pruefung/settings", data={"openai_key": "sk-test-1234567890"})
        r = c.post("/admin/pruefung/run", data={})
        assert "Datenschutzprüfung" in r.text and "nicht gesendet" in r.text and not calls


def test_pseudo_roundtrip():
    p = audit.Pseudo(date(2026, 11, 19), factor=0.9)
    t = p.tok("T", "rp:1", "Immobilie · Grundsteuer")
    assert t == p.tok("T", "rp:1", "x") and t.startswith("T-")
    assert p.back(p.amt(123.45)) == 123.45
    assert p.resolve(f"Bitte {t} prüfen") == "Bitte Immobilie · Grundsteuer prüfen"
    assert p.rel(date(2026, 11, 9)) == -10
    assert audit.classify("Fahrzeug · HU-AU FUER BA-AT9314")["art"] == ["Fahrzeug: Prüfung/Wartung"]


def test_free_tier_model_test_picks_working_cheap_model(monkeypatch):
    models = ["gpt-5.6", "gpt-5.6-mini", "gpt-5.6-nano", "gpt-4.1-mini", "whisper-1"]
    tried = []

    def fake_get(url, headers=None, timeout=None):
        return httpx.Response(200, json={"data": [{"id": m} for m in models]}, request=httpx.Request("GET", url))

    def fake_post(url, json=None, headers=None, timeout=None):  # noqa: A002
        tried.append(json["model"])
        if json["model"] == "gpt-5.6":
            return httpx.Response(403, json={"error": {"code": "model_not_found", "message": "no access"}},
                                  request=httpx.Request("POST", url))
        if json["model"] == "gpt-5.6-mini":
            return httpx.Response(429, json={"error": {"code": "insufficient_quota", "message": "quota"}},
                                  request=httpx.Request("POST", url))
        return httpx.Response(200, json={"output": []}, request=httpx.Request("POST", url))

    monkeypatch.setattr(audit.httpx, "get", fake_get)
    monkeypatch.setattr(audit.httpx, "post", fake_post)
    with TestClient(app) as c:
        r = c.post("/admin/pruefung/settings", data={"openai_key": "sk-free-123456", "openai_model": "gpt-5.6",
                                                     "test": "1"})
        assert "für deinen Zugang nicht freigegeben" in r.text and "„gpt-4.1-mini“ eingestellt" in r.text
        assert tried == ["gpt-5.6", "gpt-5.6-mini", "gpt-4.1-mini"]
        assert 'value="gpt-4.1-mini"' in r.text and '<option value="gpt-5.6-nano">' in r.text
    assert "Kontingent aufgebraucht" in audit.explain_error(429, {"error": {"code": "insufficient_quota"}})
    assert "zu viele Anfragen" in audit.explain_error(429, {"error": {"code": "rate_limit_exceeded"}})


def test_actions_fill_negative_pot_and_assign_difference():
    from app import reserves

    with TestClient(app) as c:
        p_neg = _setup(c)
        with SessionLocal() as s:
            b = s.query(Building).first()
            bid = b.id
            free = ReservePlan(building_id=bid, name="Privat · Freies Vermögen", label="FREIES VERMOEGEN",
                               funding="manuell", category="privat", active=True, start="2026-01")
            s.add(free)
            s.flush()
            s.add(ReserveEntry(building_id=bid, ref=f"rp:{free.id}", name=free.name, booking_date=date(2026, 6, 1),
                               amount=100, kind="start"))
            s.commit()
            fid = free.id
        r = c.post("/admin/pruefung/run", data={})
        assert "Topf im Minus" in r.text and "↔ aus Freies Vermögen" in r.text and "Differenz zuweisen an" in r.text
        assert "vom Hauptkonto auffüllen" not in r.text  # ohne Bankzugang kein FinTS-Knopf
        rid = int(r.url.path.rsplit("/", 1)[1])
        # aus Freies Vermögen umbuchen (nur in der App)
        r = c.post("/admin/reserves/pot-move", data={"building": bid, "from": f"rp:{fid}", "to": f"rp:{p_neg}",
                                                    "amount": "30,00", "back": f"/admin/pruefung/{rid}#b0"})
        assert "Umgebucht: 30,00 €" in r.text and "✓ ausgeglichen" in r.text
        with SessionLocal() as s:
            bal = {x["ref"]: x["total"] for x in reserves.balances(s, s.get(Building, bid))}
            assert bal[f"rp:{p_neg}"] == 0.0 and bal[f"rp:{fid}"] == 70.0
        # Differenz Kontostand – Töpfe zuweisen: 500 € Konto vs. 490 € Töpfe → 10 €
        c.post("/admin/reserves/assign-diff", data={"building": bid, "ref": f"rp:{fid}", "amount": "10"})
        with SessionLocal() as s:
            total = sum(x["total"] for x in reserves.balances(s, s.get(Building, bid)))
            assert round(total, 2) == 500.0
        # Auffüllen vom Hauptkonto: nur mit ausdrücklicher Bestätigung
        r = c.post("/admin/reserves/topup", data={"building": bid, "ref": f"rp:{p_neg}", "amount": "12"})
        assert "AUSFÜHREN" in r.text
        r = c.post("/admin/reserves/topup", data={"building": bid, "ref": f"rp:{p_neg}", "amount": "12",
                                                  "confirm": "AUSFÜHREN"})
        assert "SEPA-Datei über 12,00 € erzeugt" in r.text
        with SessionLocal() as s:
            e = s.query(ReserveEntry).filter(ReserveEntry.e2e.like("RLAUSGL%")).one()
            assert e.amount == 12 and e.kind == "in" and e.export_id and "RUECKLAGE AUSGLEICH" in e.note
        # zweite Prüfung: kein Topf mehr im Minus, keine Differenz-Aktion
        r = c.post("/admin/pruefung/run", data={})
        assert "Topf im Minus" not in r.text
