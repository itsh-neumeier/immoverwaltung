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
