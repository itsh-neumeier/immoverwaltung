import re
from datetime import date

from fastapi.testclient import TestClient

from app import finance
from app.db import Building, FinCategory, FinTxn, SessionLocal
from app.main import app
from tests.test_reserves import GIRO, RESERVE, camt


def tx(day, amount, name, purpose="", other="DE44500105175407324931"):
    return {"date": day, "amount": amount, "purpose": purpose or name, "name": name, "other": other}


def camt_named(own, entries, closing):
    return camt(own, entries, None, closing)


def test_rules_and_matching():
    rules = finance.parse_rules("REWE, GA NR*, zweck:LOHN, iban:DE89 3704 0044 0532 0130 00")
    assert [r.field for r in rules] == ["name", "name", "purpose", "iban"]
    t = FinTxn(name="REWE Markt GmbH", purpose="", amount=-12, iban="")
    assert rules[0].rx.search(finance.norm(t.name))
    assert rules[1].rx.search(finance.norm("GA NR00002317 BLZ77050000"))
    assert not rules[0].rx.search(finance.norm("BREWERY"))  # nur ganze Wörter


def test_spielraum_budgets_recurring_and_rules():
    entries = []
    for m in ("07", "08", "09", "10"):
        entries += [tx(f"2026-{m}-28", 2500.0, "Arbeitgeber GmbH", "LOHN/GEHALT"),
                    tx(f"2026-{m}-01", -850.0, "Sparkasse", "DARLEHEN 4711"),
                    tx(f"2026-{m}-05", -120.0, "REWE Markt GmbH"), tx(f"2026-{m}-15", -80.0, "EDEKA Center"),
                    tx(f"2026-{m}-03", -40.0, "DM DROGERIEMARKT SAGT DANKE"),
                    tx(f"2026-{m}-02", -50.0, "Timo Muster", "RUECKBILDUNG GRUNDSTEUER", other=RESERVE)]
    entries += [tx("2026-11-01", -850.0, "Sparkasse", "DARLEHEN 4711"), tx("2026-11-04", -150.0, "REWE Markt GmbH"),
                tx("2026-11-06", -33.0, "Kiosk Ecke")]
    with TestClient(app) as c:
        c.post("/admin/settings", data={"vat_rate": "19"})
        with SessionLocal() as s:
            b = s.query(Building).first()
            b.account_name = b.reserve_name = "Timo Muster"
            b.account_iban, b.reserve_iban = GIRO, RESERVE
            s.commit()
        r = c.post("/admin/finanzen/import", files={"file": ("h.xml", camt_named(GIRO, entries, ("2026-11-10", 400.0)),
                                                            "application/xml")})
        assert "Finanzen: 27 Umsätze neu" in r.text and "Rücklagen" in r.text
        assert "Finanzen: 0 Umsätze neu, 27 schon vorhanden" in c.post(
            "/admin/finanzen/import", files={"file": ("h.xml", camt_named(GIRO, entries, ("2026-11-10", 400.0)),
                                                       "application/xml")}).text
        with SessionLocal() as s:
            d = finance.overview(s, date(2026, 11, 12))
            cats = {b["cat"].name: b for b in d["budgets"]}
            assert cats["Lebensmittel"]["used"] == 150.0 and cats["Lebensmittel"]["avg"] == 200.0
            assert cats["Drogerie & Apotheke"]["avg"] == 40.0
            # erwartet: Gehalt am 28. (+2500) und Grundsteuer-Dauerauftrag? (am 02. schon vorbei, aber noch nicht gebucht)
            exp = {(e["category"].name if e["category"] else e["name"]): e for e in d["expected"]}
            assert exp["Gehalt"]["amount"] == 2500.0 and exp["Gehalt"]["due"] == date(2026, 11, 28)
            assert exp["Rücklagen (Umbuchung)"]["amount"] == -50.0 and "Kredit & Finanzierung" not in exp  # schon gebucht
            assert d["balance"] == 400.0 and d["low"] == 350.0 and d["room"] == 250.0  # Puffer 100
        assert "Budget(s) aus dem Durchschnitt" in c.post("/admin/finanzen/suggest", data={}).text
        with SessionLocal() as s:
            d = finance.overview(s, date(2026, 11, 12))
            leb = next(b for b in d["budgets"] if b["cat"].name == "Lebensmittel")
            assert leb["cat"].budget == 200.0 and leb["rest"] == 50.0
            assert d["free"] == round(d["room"] - d["budget_rest"], 2)
            kiosk = s.query(FinTxn).filter(FinTxn.name == "Kiosk Ecke").one()
            kid = kiosk.id
            leb_id = leb["cat"].id
        page = c.get("/admin/finanzen?show=offen").text
        assert "Kiosk Ecke" in page and "Spielraum bis 30.11." in page
        r = c.post(f"/admin/finanzen/txn/{kid}", data={"category": leb_id, "remember": "1"})
        assert "Regel „KIOSK ECKE“ gemerkt" in r.text
        with SessionLocal() as s:
            assert "KIOSK ECKE" in s.get(FinCategory, leb_id).rules
            d = finance.overview(s, date(2026, 11, 12))
            assert next(b for b in d["budgets"] if b["cat"].name == "Lebensmittel")["used"] == 183.0


def test_finance_only_for_super_admin():
    with TestClient(app) as c:
        c.post("/setup", data={"username": "admin", "password": "admin-pass-1", "password2": "admin-pass-1"})
        c.post("/admin/users/0", data={"username": "verw", "role": "admin", "password": "verwalter-1", "active": "1"})
        assert "Finanzen" in c.get("/admin").text
        c.get("/logout")
        c.post("/login", data={"username": "verw", "password": "verwalter-1"})
        assert c.get("/admin/finanzen", follow_redirects=False).status_code == 303
        assert ">Finanzen<" not in c.get("/admin").text


def test_refunds_cut_and_monthly_collective_orders():
    import xml.etree.ElementTree as ET
    from app import reserves
    from app.db import ReserveEntry, ReservePlan, SepaExport
    from tests.test_reserves import NS

    main = [tx("2026-10-01", -50.0, "Timo Muster", "RUECKBILDUNG GRUNDSTEUER MUSTERSTR ZYKLUS P.Q.", other=RESERVE),
            tx("2026-08-17", -147.99, "MARKT HIRSCHAID", "GRUNDSTEUER FINANZADRESSE 19379 - 1"),
            tx("2026-11-16", -147.99, "MARKT HIRSCHAID", "GRUNDSTEUER FINANZADRESSE 19379 - 1"),
            tx("2026-11-20", -12.00, "MARKT HIRSCHAID", "Debitk. Wertstoffhof")]
    res = [tx("2026-08-17", -147.00, "Timo Muster", "GUTSCHRIFT GRUNDSTEUER RUECKBILDUNG", other=GIRO)]
    with TestClient(app) as c:
        c.post("/admin/settings", data={"vat_rate": "19"})
        with SessionLocal() as s:
            b = s.query(Building).first()
            bid, b.code = b.id, "GID-134"
            b.account_name = b.reserve_name = "Timo Muster"
            b.account_iban, b.reserve_iban = GIRO, RESERVE
            s.add(ReservePlan(building_id=bid, name="Grundsteuer", label="GRUNDSTEUER", annual_amount=591.96,
                              interval=1, start="2026-01", funding="dauerauftrag", standing_order=50, active=True))
            s.commit()
        c.post("/admin/finanzen/import", files={"file": ("h.xml", camt(GIRO, main, None, ("2026-11-20", 500.0)), "application/xml")})
        c.post("/admin/reserves/bank/import", data={"building": bid},
               files={"file": ("r.xml", camt(RESERVE, res, None, ("2026-11-20", 900.0)), "application/xml")})
        c.post("/admin/reserves/bank/confirm-sure", data={"building": bid})  # Auszahlung 147,00 → Topf Grundsteuer
        with SessionLocal() as s:
            pid = s.query(ReservePlan).one().id
        # Rechnung kommt von … eintragen
        c.post("/admin/reserves/plan", data={"building": bid, "id": pid, "name": "Grundsteuer", "label": "GRUNDSTEUER",
                                             "annual_amount": "591,96", "interval": "1", "start": "2026-01",
                                             "category": "immobilie", "funding": "dauerauftrag", "standing_order": "50",
                                             "payee_rules": "MARKT HIRSCHAID & zweck:GRUNDSTEUER", "active": "1"})
        with SessionLocal() as s:
            rows = reserves.open_refunds(s, s.get(Building, bid))
            assert [(r["amount"], r["bill"], bool(r["probable"])) for r in rows] == [(0.0, 147.99, True), (147.99, 147.99, False)]
            open_bill = rows[1]["txn"].id
            assert rows[1]["purpose"] == ("GUTSCHRIFT RUECKLAGE GRUNDSTEUER GID: 134 RECHNUNG MARKT HIRSCHAID "
                                          "16.11.2026 147,99 EUR")
            done_bill = rows[0]["txn"].id
        page = c.get("/admin/reserves").text
        assert "Offene Gutschriften" in page and "vermutlich erstattet am 17.08.2026" in page and "Wertstoffhof" not in page
        c.post(f"/admin/reserves/refunds/{done_bill}", data={"building": bid, "action": "done"})
        r = c.post("/admin/reserves/refunds/export", data={"building": bid, "bills": [open_bill]})
        assert "Sammelauftrag Gutschriften erzeugt: 1 Überweisung(en), 147,99 €" in r.text
        ex_id = int(re.search(r"dl=(\d+)", str(r.url)).group(1))
        doc = ET.fromstring(c.get(f"/admin/reserves/export/{ex_id}.xml").content)
        assert doc.find(".//p:DbtrAcct//p:IBAN", NS).text == RESERVE and doc.find(".//p:CdtrAcct//p:IBAN", NS).text == GIRO
        assert doc.find(".//p:EndToEndId", NS).text == f"RG{open_bill}"
        with SessionLocal() as s:
            assert not reserves.open_refunds(s, s.get(Building, bid))
            e = s.query(ReserveEntry).filter(ReserveEntry.export_id == ex_id).one()
            assert e.amount == -147.99 and e.fin_txn_id == open_bill
        # Datei verwerfen → Rechnung wieder offen
        c.post(f"/admin/reserves/export/{ex_id}/delete")
        with SessionLocal() as s:
            assert [r["txn"].id for r in reserves.open_refunds(s, s.get(Building, bid))] == [open_bill]
        # Cut: Daueraufträge → monatlicher Sammelauftrag ab 12/2026
        assert "1 Töpfe ab 12/2026 auf monatlichen Sammelauftrag umgestellt" in c.post(
            "/admin/reserves/plans/cut", data={"building": bid, "month": "2026-12"}).text
        with SessionLocal() as s:
            p = s.get(ReservePlan, pid)
            assert (p.funding, p.start, p.standing_order, p.rate) == ("sepa", "2026-12", 0.0, 49.33)
            items = reserves.plan_pending(s, s.get(Building, bid), today=date(2026, 12, 3))
            assert [i.purpose for i in items] == ["RUECKLAGE GRUNDSTEUER 12/2026 GID: 134 49,33 EUR"]


def test_protected_manual_pot_is_never_touched():
    from app import reserves
    from app.db import ReservePlan

    with TestClient(app) as c:
        c.post("/admin/settings", data={"vat_rate": "19"})
        with SessionLocal() as s:
            b = s.query(Building).first()
            bid, b.account_name, b.reserve_name = b.id, "Timo Muster", "Timo Muster"
            b.account_iban, b.reserve_iban = GIRO, RESERVE
            s.commit()
        c.post("/admin/reserves/plan", data={"building": bid, "name": "Urlaub", "label": "URLAUB", "annual_amount": "2400",
                                             "interval": "1", "start": "2026-01", "category": "privat", "funding": "manuell",
                                             "payee_rules": "zweck:URLAUB"})
        c.post("/admin/finanzen/import", files={"file": ("h.xml", camt(GIRO, [tx("2026-11-05", -900.0, "Reisebuero",
                                                                                      "URLAUB TUERKEI")], None,
                                                                            ("2026-11-10", 100.0)), "application/xml")})
        assert "0 Töpfe" in c.post("/admin/reserves/plans/cut", data={"building": bid, "month": "2026-12"}).text
        with SessionLocal() as s:
            bld = s.get(Building, bid)
            p = s.query(ReservePlan).one()
            assert p.funding == "manuell" and p.start == "2026-01"
            assert not reserves.plan_pending(s, bld, today=date(2026, 12, 3))  # keine Zuführung
            assert not reserves.open_refunds(s, bld)  # keine Gutschrift
        assert "🔒 Manuell" in c.get("/admin/reserves").text


def test_monthly_collective_order_mail(monkeypatch):
    import smtplib
    import xml.etree.ElementTree as ET

    from app import reserves
    from app.db import ReservePlan, SepaExport
    from tests.test_reserves import NS

    sent = []

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, **kw):
            pass

        def login(self, *a):
            pass

        def send_message(self, msg, to_addrs=None):
            sent.append((msg, to_addrs))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    with TestClient(app) as c:
        c.post("/admin/settings", data={"vat_rate": "19"})
        with SessionLocal() as s:
            b = s.query(Building).first()
            bid, b.code = b.id, "GID-01"
            b.account_name = b.reserve_name = "Timo Muster"
            b.account_iban, b.reserve_iban = GIRO, RESERVE
            s.add(ReservePlan(building_id=bid, name="Immobilie · Grundsteuer", label="GRUNDSTEUER", annual_amount=600,
                              interval=1, start="2026-12", funding="sepa", category="immobilie", active=True))
            s.add(ReservePlan(building_id=bid, name="Fahrzeug · HU-AU FUER BA-AT9314 (Corsa)", label="HU-AU FUER BA-AT9314",
                              annual_amount=84, interval=1, start="2026-12", funding="sepa", category="privat", active=True))
            s.add(ReservePlan(building_id=bid, name="Privat · Urlaub", label="URLAUB", funding="manuell", active=True,
                              start="2026-11"))
            s.commit()
        c.post("/admin/reserves/accounts", data={"building": bid, "account_name": "Timo Muster", "account_iban": GIRO,
                                                 "reserve_name": "Timo Muster", "reserve_iban": RESERVE,
                                                 "sepa_format": "pain.001.001.09", "reserve_owner": "1", "sepa_mail": "1",
                                                 "sepa_mail_to": "timo@example.test", "sepa_mail_day": "25"})
        with SessionLocal() as s:
            assert reserves.monthly_tick(s, date(2026, 11, 24)) == []  # vor dem 25.
            out = reserves.monthly_tick(s, date(2026, 11, 25))
            assert out and "Sammelauftrag 12/2026 (57,00 €, 2 Überweisungen)" in out[0]
            assert reserves.monthly_tick(s, date(2026, 11, 28)) == []  # nur einmal je Monat
        msg, to = sent[0]
        assert to == ["timo@example.test"] and "SEPA-Sammelauftrag Rücklagen 12/2026: 57,00 €" in msg["Subject"]
        att = [p for p in msg.iter_attachments()][0]
        assert att.get_content_type() == "application/xml" and att.get_filename().endswith(".xml")
        doc = ET.fromstring(att.get_payload(decode=True))
        assert doc.find(".//p:ReqdExctnDt/p:Dt", NS).text == "2026-12-01"  # erster Bankarbeitstag
        texts = sorted(u.text for u in doc.findall(".//p:Ustrd", NS))
        assert texts == ["RUECKLAGE GRUNDSTEUER 12/2026 GID: 01 50,00 EUR", "RUECKLAGE HU-AU FUER BA-AT9314 12/2026 7,00 EUR"]
        # erneut senden (Knopf) → dieselbe Datei, keine zweite
        with SessionLocal() as s:
            assert "per E-Mail" in reserves.monthly_run(s, s.get(Building, bid), today=date(2026, 11, 26), force=True)
            assert s.query(SepaExport).count() == 1
        assert len(sent) == 2
        # Dezember: Zuführungen sind vorgemerkt, nicht mehr offen
        with SessionLocal() as s:
            assert not reserves.plan_pending(s, s.get(Building, bid), today=date(2026, 12, 3))
