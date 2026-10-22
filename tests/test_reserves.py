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
