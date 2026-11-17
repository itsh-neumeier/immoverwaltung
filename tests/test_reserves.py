import re
import xml.etree.ElementTree as ET
from datetime import date

from fastapi.testclient import TestClient

from app import sepa
from app.db import Building, ReserveEntry, SepaExport, SessionLocal
from app.main import app

BILL = {"period_start": "2026-09-01", "period_end": "2026-09-30", "grid_kwh": "100", "energy_cost_net": "25,00",
        "fixed_cost_net": "10", "spot_price_ct": "10", "vat_rate": "19", "battery_rate_ct": "8", "pv_rate_ct": "5"}
VALUES = {"val__sensor.grid": "100", "val__sensor.total": "300", "val__sensor.eg": "120", "val__sensor.og": "80"}
NS = {"p": "urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"}
GIRO, RESERVE = "DE89370400440532013000", "DE02120300000000202051"


def test_sepa_helpers():
    assert sepa.iban_ok("DE89 3704 0044 0532 0130 00") and not sepa.iban_ok("DE89370400440532013001")
    assert sepa.safe_filename("Rücklage GID:134 / 09.2026") == "Ruecklage_GID_134_09_2026.xml"
    assert sepa.clean("Wasser „Süd“ – 5 €", umlauts=False) == "Wasser Sued - 5 EUR"
    assert sepa.clean("Müller & Söhne") == "Müller & Söhne"
    assert sepa.next_business_day(date(2026, 12, 24)) == date(2026, 12, 28)  # Weihnachten + Wochenende
    assert sepa.next_business_day(date(2026, 4, 2)) == date(2026, 4, 7)  # Karfreitag bis Ostermontag


def test_reserve_flow_export_confirm_payout():
    with TestClient(app) as c:
        c.post("/admin/settings", data={"entity_grid": "sensor.grid", "entity_total": "sensor.total", "vat_rate": "19"})
        c.post("/admin/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1", "meters": "sensor.og"})
        c.post("/admin/parties/0", data={"name": "Familie Zweig", "unit_id": "WE-1", "meters": "sensor.eg", "active": "1"})
        c.post("/admin/costs/fixed/0", data={"name": "IPTV-Bereitstellung", "amount_gross": "20", "active": "1",
                                             "reserve": "1", "reserve_label": "IPTV"})
        c.post("/admin/costs/alloc/0", data={"name": "Trinkwasser", "source_type": "amount", "default_amount": "60,68",
                                             "key_type": "equal", "active": "1", "reserve": "1",
                                             "reserve_label": "Wassergeld"})
        with SessionLocal() as s:
            b = s.query(Building).first()
            b.code = "GID-134"
            s.commit()
            bid = b.id
        # Konten fehlen → Hinweis, Export gesperrt
        assert "Konten unvollständig" in c.get("/admin/reserves").text
        r = c.post("/admin/reserves/accounts", data={"building": bid, "account_name": "Timo Muster",
                                                     "account_iban": "DE89 3704 0044 0532 0130 00",
                                                     "reserve_name": "Timo Muster", "reserve_iban": RESERVE,
                                                     "reserve_owner": "1", "sepa_format": "pain.001.001.09"})
        assert "Gespeichert" in r.text and "Konten unvollständig" not in r.text

        r = c.post("/admin/billings", data=BILL, follow_redirects=False)
        url = r.headers["location"].split("?")[0]
        c.post(url, data={"action": "finalize", **BILL, **VALUES})

        page = c.get("/admin/reserves").text
        purpose = "RUECKLAGE AUS NEBENKOSTENABRECHNUNG 09/2026 MIETER FAMILIE ZWEIG GID: 134 WE-1 WASSERGELD 30,34 EUR"
        assert purpose in page and "EIGENTUEMER" in page
        keys = re.findall(r'name="items" value="([^"]+)"', page)
        assert len(keys) == 4  # 2 Parteien × 2 Positionen

        # Eigentümer-Anteil abgewählt → nur Familie Zweig
        zweig = [k for k in keys if ":2:" in k]
        r = c.post("/admin/reserves/export", data={"building": bid, "items": zweig, "exec_date": "2026-10-30"})
        dl = re.search(r"dl=(\d+)", str(r.url)).group(1)
        xml = c.get(f"/admin/reserves/export/{dl}.xml")
        assert xml.headers["content-disposition"].endswith('.xml"')
        fname = re.search(r'filename="([^"]+)"', xml.headers["content-disposition"]).group(1)
        assert re.fullmatch(r"[A-Za-z0-9+_-]+\.xml", fname)
        doc = ET.fromstring(xml.content)
        assert len(doc.findall(".//p:PmtInf", NS)) == 1  # eine logische Datei / ein Sammler
        assert doc.find(".//p:GrpHdr/p:CtrlSum", NS).text == "40.34"  # 30,34 Wasser + 10,00 IPTV
        assert doc.find(".//p:ReqdExctnDt/p:Dt", NS).text == "2026-10-30"
        assert doc.find(".//p:DbtrAcct//p:IBAN", NS).text == GIRO
        assert purpose in [u.text for u in doc.findall(".//p:Ustrd", NS)]

        # noch nicht bestätigt → vorgemerkt, Stand 0, nicht mehr offen
        page = c.get("/admin/reserves").text
        assert "wartet auf Ausführung" in page and len(re.findall(r'name="items"', page)) == 2
        with SessionLocal() as s:
            assert s.query(ReserveEntry).count() == 2
            ex = s.get(SepaExport, int(dl))
            assert ex.status == "open"
        # bestätigen
        r = c.post(f"/admin/reserves/export/{dl}/confirm", data={"executed_on": "2026-10-30"})
        assert "als ausgeführt bestätigt" in r.text and "✓ ausgeführt" in r.text
        # verwerfen nach Bestätigung nicht möglich
        assert "erst die Bestätigung zurücknehmen" in c.post(f"/admin/reserves/export/{dl}/delete").text

        # Wasserabrechnung fällig → Auszahlung aufs Hauptkonto per SEPA
        with SessionLocal() as s:
            ref = s.query(ReserveEntry).filter(ReserveEntry.name == "Trinkwasser").first().ref
        r = c.post("/admin/reserves/entry", data={"building": bid, "kind": "out", "ref": ref, "amount": "30,34",
                                                  "note": "Wasserabrechnung 2026", "sepa": "1"})
        assert "SEPA-Gutschrift aufs Hauptkonto" in r.text
        out_id = re.search(r"dl=(\d+)", str(r.url)).group(1)
        doc = ET.fromstring(c.get(f"/admin/reserves/export/{out_id}.xml").content)
        assert doc.find(".//p:DbtrAcct//p:IBAN", NS).text == RESERVE
        assert doc.find(".//p:CdtrAcct//p:IBAN", NS).text == GIRO
        assert doc.find(".//p:Ustrd", NS).text.startswith("AUSZAHLUNG RUECKLAGE GID: 134 WASSERGELD 30,34 EUR")

        def stand():
            from app import reserves
            with SessionLocal() as s:
                return {r["name"]: r["total"] for r in reserves.balances(s, s.get(Building, bid))}
        assert stand()["Trinkwasser"] == 30.34  # Auszahlung noch vorgemerkt
        c.post(f"/admin/reserves/export/{out_id}/confirm", data={})
        assert stand() == {"IPTV-Bereitstellung": 10.0, "Trinkwasser": 0.0}

        # verworfene Datei → Posten wieder offen
        page = c.get("/admin/reserves").text
        own = re.findall(r'name="items" value="([^"]+)"', page)
        r = c.post("/admin/reserves/export", data={"building": bid, "items": own})
        dl2 = re.search(r"dl=(\d+)", str(r.url)).group(1)
        assert not re.findall(r'name="items"', c.get("/admin/reserves").text)
        c.post(f"/admin/reserves/export/{dl2}/delete")
        assert len(re.findall(r'name="items"', c.get("/admin/reserves").text)) == 2


def camt(own_iban, entries, opening=None, closing=None):
    """Minimaler camt.052.001.08-Auszug wie aus dem Sparkassen-Export (je Buchung genau ein TxDtls)."""
    ntry = ""
    for i, e in enumerate(entries):
        cd = "CRDT" if e["amount"] > 0 else "DBIT"
        refs = (f"<EndToEndId>{e['e2e']}</EndToEndId>" if e.get("e2e") else "") + \
            (f"<PmtInfId>{e['pmtinf']}</PmtInfId>" if e.get("pmtinf") else "")
        ntry += f"""<Ntry><Amt Ccy="EUR">{abs(e['amount']):.2f}</Amt><CdtDbtInd>{cd}</CdtDbtInd><Sts><Cd>BOOK</Cd></Sts>
<BookgDt><Dt>{e.get('date', '2026-10-30')}</Dt></BookgDt><ValDt><Dt>2026-10-30</Dt></ValDt><AcctSvcrRef>R{i}</AcctSvcrRef>
<AddtlNtryInf>{e.get('info', 'GUTSCHRIFT')}</AddtlNtryInf><NtryDtls><TxDtls><Refs>{refs}<Prtry><Tp>A</Tp><Ref>x</Ref></Prtry></Refs>
<Amt Ccy="EUR">{abs(e['amount']):.2f}</Amt><RltdPties><Dbtr><Pty><Nm>{e.get('name', 'Timo Muster')}</Nm></Pty></Dbtr><DbtrAcct><Id><IBAN>{e.get('other', GIRO)}</IBAN></Id></DbtrAcct>
<Cdtr><Pty><Nm>{e.get('name', 'Timo Muster')}</Nm></Pty></Cdtr><CdtrAcct><Id><IBAN>{e.get('other', GIRO)}</IBAN></Id></CdtrAcct></RltdPties>
<RmtInf><Ustrd>{e.get('purpose', '')}</Ustrd></RmtInf></TxDtls></NtryDtls></Ntry>"""
    return f"""<?xml version="1.0" encoding="UTF-8"?><Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.052.001.08">
<BkToCstmrAcctRpt><GrpHdr><MsgId>1</MsgId><CreDtTm>2026-10-31T00:00:00</CreDtTm></GrpHdr><Rpt><Id>1</Id>
<Acct><Id><IBAN>{own_iban}</IBAN></Id></Acct>{bal(opening, "OPBD")}{bal(closing, "CLBD")}{ntry}</Rpt></BkToCstmrAcctRpt></Document>""".encode()


def bal(value, kind):
    if value is None:
        return ""
    day, amount = value
    return (f'<Bal><Tp><CdOrPrtry><Cd>{kind}</Cd></CdOrPrtry></Tp><Amt Ccy="EUR">{abs(amount):.2f}</Amt>'
            f'<CdtDbtInd>{"DBIT" if amount < 0 else "CRDT"}</CdtDbtInd><Dt><Dt>{day}</Dt></Dt></Bal>')


def test_camt_parse_zip_and_csv():
    import io
    import zipfile

    from app import bankimport
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026.10.30.xml", camt(RESERVE, [{"amount": 30.34, "e2e": "RLX1", "purpose": "RUECKLAGE A"}]))
        z.writestr("2026.10.31.xml", camt(RESERVE, [{"amount": -120, "pmtinf": "P1", "purpose": "GUTSCHRIFT"}]))
    t = bankimport.parse("a.ZIP", buf.getvalue())
    assert [(x.account_iban, x.amount, x.e2e, x.name, x.iban) for x in t] == [
        (RESERVE, 30.34, "RLX1", "Timo Muster", GIRO), (RESERVE, -120.0, "", "Timo Muster", GIRO)]
    assert t[0].digest != t[1].digest
    csv_data = ('"Auftragskonto";"Buchungstag";"Valutadatum";"Buchungstext";"Verwendungszweck";'
                '"Kundenreferenz (End-to-End)";"Beguenstigter/Zahlungspflichtiger";"Kontonummer/IBAN";"Betrag";"Info"\n'
                f'"{RESERVE}";"30.10.26";"30.10.26";"GUTSCHR";"RUECKLAGE Ü";"RLX1";"Timo";"{GIRO}";"30,34";"Umsatz gebucht"\n'
                f'"{RESERVE}";"31.10.26";"31.10.26";"x";"y";"";"";"";"-1,00";"Umsatz vorgemerkt"\n').encode("cp1252")
    t = bankimport.parse("u.csv", csv_data)
    assert len(t) == 1 and t[0].amount == 30.34 and t[0].booking_date == date(2026, 10, 30) and t[0].purpose == "RUECKLAGE Ü"


def test_fixed_reserves_and_bank_reconciliation():
    from app import reserves
    from app.db import BankLine, ReservePlan

    with TestClient(app) as c:
        c.post("/admin/settings", data={"vat_rate": "19"})
        with SessionLocal() as s:
            b = s.query(Building).first()
            b.code, bid = "GID-134", b.id
            b.account_name = b.reserve_name = "Timo Muster"
            b.account_iban, b.reserve_iban = GIRO, RESERVE
            s.commit()
        start = date.today().replace(day=1)
        start = date(start.year - (start.month <= 2), (start.month - 3) % 12 + 1, 1)  # vor 2 Monaten
        r = c.post("/admin/reserves/plan", data={"building": bid, "name": "Grundsteuer", "label": "Grundsteuer",
                                                 "annual_amount": "498,00", "interval": "1",
                                                 "start": start.strftime("%Y-%m"), "due_note": "15.02./15.05."})
        assert "41,50" in r.text
        c.post("/admin/reserves/plan", data={"building": bid, "name": "Wohngebäudeversicherung", "label": "",
                                             "annual_amount": "1235", "interval": "12",
                                             "start": start.strftime("%Y-%m")})
        page = c.get("/admin/reserves").text
        assert f"RUECKLAGE GRUNDSTEUER {start:%m/%Y} GID: 134 41,50 EUR" in page
        assert "RUECKLAGE WOHNGEBAEUDEVERSICHERUNG" in page and "1.235,00" in page
        keys = re.findall(r'name="items" value="([^"]+)"', page)
        assert len(keys) == 4  # 3 Monate Grundsteuer + 1× Versicherung (jährlich)

        r = c.post("/admin/reserves/export", data={"building": bid, "items": keys})
        ex_id = int(re.search(r"dl=(\d+)", str(r.url)).group(1))
        assert not re.findall(r'name="items"', c.get("/admin/reserves").text)  # nichts mehr offen
        with SessionLocal() as s:
            es = s.query(ReserveEntry).filter(ReserveEntry.export_id == ex_id).all()
            assert sorted(e.period for e in es)[0] == f"{start:%Y-%m}" and all(e.e2e.startswith("RL") for e in es)
            msg_id = s.get(SepaExport, ex_id).msg_id
            e2es = [(e.e2e, e.amount) for e in es]

        # Kontoauszug Hauptkonto: eine Sammelbuchung (PmtInfId der Datei) + fremde Umsätze (werden übersprungen)
        total = round(sum(a for _, a in e2es), 2)
        main = camt(GIRO, [{"amount": -total, "pmtinf": f"{msg_id}-1", "other": RESERVE, "purpose": "SAMMLER"},
                           {"amount": -55.0, "other": "DE44500105175407324931", "purpose": "Strom"}])
        r = c.post("/admin/reserves/bank/import", data={"building": bid},
                   files={"file": ("haupt.xml", main, "application/xml")})
        assert "1 Umsätze eingelesen" in r.text and "1 Umsätze des Hauptkontos ohne Bezug" in r.text
        # Rücklagenkonto: Einzelgutschriften mit End-to-End + Zinsen
        res = camt(RESERVE, [{"amount": a, "e2e": e} for e, a in e2es] + [{"amount": 0.12, "purpose": "Zinsen"}])
        r = c.post("/admin/reserves/bank/import", data={"building": bid},
                   files={"file": ("ruecklage.xml", res, "application/xml")})
        assert "5 Umsätze eingelesen" in r.text and "5 eindeutige Treffer" in r.text
        page = c.get("/admin/reserves").text
        assert page.count('class="bank-line"') == 6 and "Noch nicht im Kontoauszug gefunden (4)" in page

        # Vorschlag für die Sammelbuchung: ganze Datei
        with SessionLocal() as s:
            line = s.query(BankLine).filter(BankLine.account == "main").one()
            best = reserves.candidates(s, line)[0]
            assert best["value"] == f"x:{ex_id}" and best["score"] == 100
            zins = s.query(BankLine).filter(BankLine.amount == 0.12).one().id
            plan_ref = f"rp:{s.query(ReservePlan).filter(ReservePlan.name == 'Grundsteuer').one().id}"
        r = c.post("/admin/reserves/bank/confirm-sure", data={"building": bid})
        assert "5 Umsätze zugeordnet (8 Buchungen)" in r.text  # 4 Gutschriften Rücklagenkonto + Sammelbuchung (4 Buchungen)
        with SessionLocal() as s:
            assert s.get(SepaExport, ex_id).status == "done" and s.get(SepaExport, ex_id).confirmed_by == "Kontoauszug"
            assert all(e.bank_line_id and e.main_line_id for e in s.query(ReserveEntry).filter(ReserveEntry.export_id == ex_id))
        # Zinsen von Hand einer Position zubuchen
        r = c.post(f"/admin/reserves/bank/{zins}", data={"action": "book", "ref": plan_ref})
        assert "Als Zuführung gebucht (Grundsteuer)" in r.text
        page = c.get("/admin/reserves").text
        assert 'class="bank-line"' not in page and "Noch nicht im Kontoauszug" not in page
        with SessionLocal() as s:
            stand = {r["name"]: r["total"] for r in reserves.balances(s, s.get(Building, bid))}
        assert stand == {"Grundsteuer": round(3 * 41.5 + 0.12, 2), "Wohngebäudeversicherung": 1235.0}


def test_keywords_and_mirror_rules():
    from app import reserves
    g = reserves.keyword_groups("", "RÜCKBILDUNG WOHNGEBAEDEVERSICHERUNGZYKLUS P.A.")
    assert reserves.keyword_score(g, "WOHNGEBÄUDEVERSICHERUNG DATUM 13.07.2026, 10.05 UHR")  # Tippfehler-tolerant
    opel = reserves.keyword_groups("", "KFZ Steuer Opel Insignia")
    anh = reserves.keyword_groups("", "KFZ-Steuer Anhaehner")
    p = "RÜCKBILDUNG KFZ-STEUER ANHAEHNER ZYKLUS P.A."
    assert reserves.keyword_score(anh, p) > reserves.keyword_score(opel, p)
    assert reserves.keyword_score(opel, "KFZ STEUER OPEL INSIGNIA GUTSCHRIFT") > \
        reserves.keyword_score(anh, "KFZ STEUER OPEL INSIGNIA GUTSCHRIFT")
    tuev = reserves.keyword_groups("", "TÜV Opel Insignia")
    assert reserves.keyword_score(tuev, "GUTSCHRIFT TÜV OPEL INSIGNIA") > reserves.keyword_score(opel, "GUTSCHRIFT TÜV OPEL INSIGNIA")
    assert reserves.keyword_score(reserves.keyword_groups("", "Vorwerk Cookidoo"), "AUSZAHLUNG RÜCKBILDUNG COOKIDOO")
    assert reserves._core("HÜHNERFUTTER          DATUM 19.12.2025, 22.08 UHR") == reserves._core("HÜHNERFUTTER")


def test_standing_orders_detect_balances_and_pairing():
    from app import reserves
    from app.db import BankLine, ReservePlan

    months = ["2026-07", "2026-08", "2026-09", "2026-10"]
    res_lines, main_lines = [], []
    for m in months:
        for text, amt in (("RÜCKBILDUNG GRUNDSTEUER MUSTERSTR 1 ZYKLUS P.Q.", 50.0),
                          ("RÜCKBILDUNG KFZ STEUER OPEL INSIGNIA B ZYKLUS P.A.", 10.0)):
            res_lines.append({"amount": amt, "purpose": text, "date": f"{m}-01"})
            main_lines.append({"amount": -amt, "purpose": text + " DATUM 01.07.2026, 08.00 UHR", "date": f"{m}-01",
                               "other": RESERVE})
        res_lines.append({"amount": 0.37 + len(res_lines) / 100, "purpose": f"Abrechnung {m} siehe Anlage", "date": f"{m}-28"})
    res_lines.append({"amount": -147.99, "purpose": "GUTSCHRIFT AUS RÜCKBILDUNG GRUNDSTEUER 02.2026 DATUM 02.10.2026, 09.24 UHR",
                      "date": "2026-10-02"})
    main_lines.append({"amount": 147.99, "purpose": "GUTSCHRIFT AUS RÜCKBILDUNG GRUNDSTEUER 02.2026", "date": "2026-10-02",
                       "other": RESERVE})
    main_lines.append({"amount": 10.0, "purpose": "Erstattung Shop", "date": "2026-10-03", "other": "DE44500105175407324931"})

    with TestClient(app) as c:
        c.post("/admin/settings", data={"vat_rate": "19"})
        with SessionLocal() as s:
            b = s.query(Building).first()
            bid, b.account_name, b.reserve_name = b.id, "Timo Muster", "Timo Muster"
            b.account_iban, b.reserve_iban = GIRO, RESERVE
            s.commit()
        c.post("/admin/reserves/bank/import", data={"building": bid},
               files={"file": ("r.xml", camt(RESERVE, res_lines, ("2026-07-01", 1000.0), ("2026-10-31", 1000.0 + 240 - 147.99 + 1.74)), "application/xml")})
        r = c.post("/admin/reserves/bank/import", data={"building": bid},
                   files={"file": ("h.xml", camt(GIRO, main_lines, ("2026-07-01", 50.0), ("2026-11-02", -73.01)), "application/xml")})
        assert "1 Umsätze des Hauptkontos ohne Bezug" in r.text  # „Erstattung Shop“
        with SessionLocal() as s:
            bld = s.get(Building, bid)
            assert (bld.bank_main_balance, bld.bank_reserve_open) == (-73.01, 1000.0)
            det = reserves.detect_standing_orders(s, bld)
        assert [(d["monthly"], d["category"]) for d in det] == [(50.0, "immobilie"), (10.0, "privat")]  # ohne Zinsen
        page = c.get("/admin/reserves").text
        assert "Erkannte Daueraufträge (2)" in page
        c.post("/admin/reserves/plans/detect", data={"building": bid, "keys": [d["key"] for d in det]})
        r = c.post("/admin/reserves/bank/confirm-sure", data={"building": bid})
        assert "9 Umsätze zugeordnet" in r.text  # 8 Daueraufträge + 1 Auszahlung; Gegenbuchungen automatisch
        with SessionLocal() as s:
            bld = s.get(Building, bid)
            stand = {r["name"]: r["total"] for r in reserves.balances(s, bld)}
            assert stand == {"Grundsteuer Musterstr": round(200 - 147.99, 2), "KFZ Steuer Opel Insignia": 40.0}
            assert all(e.bank_line_id and e.main_line_id for e in s.query(ReserveEntry))  # beide Konten gepaart
            assert s.query(ReserveEntry).count() == 9  # keine Doppelzählung
            assert {l.account for l in reserves.open_lines(s, bld)} == {"reserve"}  # nur Zinsen offen
            plan = s.query(ReservePlan).filter(ReservePlan.standing_order == 50).one()
            pid = plan.id
        page = c.get("/admin/reserves").text
        assert "— bitte wählen —" in page  # Zinsen: kein sicherer Vorschlag, nichts vorausgewählt
        assert "4 Umsätze ohne Vorschlag ausgeblendet" in c.post("/admin/reserves/bank/hide-rest", data={"building": bid}).text
        assert "4 Umsätze wieder eingeblendet" in c.post("/admin/reserves/bank/unhide-all", data={"building": bid}).text
        assert "frei / nicht zugeordnet" in page and "1.093,75" in page  # 1.093,75 Bank − 92,01 Töpfe = 1.001,74
        assert "1.001,74" in page
        # Jahresbetrag laut Bescheid eintragen → Hinweis zum Dauerauftrag
        c.post("/admin/reserves/plan", data={"building": bid, "id": pid, "name": "Grundsteuer", "annual_amount": "591,96",
                                             "interval": "1", "start": "2026-07", "category": "immobilie",
                                             "funding": "dauerauftrag", "standing_order": "50", "active": "1"})
        assert "Dauerauftrag von 50,00 € auf 49,33 € senken" in c.get("/admin/reserves").text
        # Zuordnung aufheben → erzeugte Buchung weg, Gegenbuchung wieder frei
        with SessionLocal() as s:
            line = s.query(BankLine).filter(BankLine.account == "reserve", BankLine.amount == -147.99).one()
            lid = line.id
        c.post(f"/admin/reserves/bank/{lid}", data={"action": "unlink"})
        with SessionLocal() as s:
            assert s.query(ReserveEntry).count() == 8
            assert not s.query(BankLine).filter(BankLine.account == "main", BankLine.amount == 147.99).one().entry_ids
