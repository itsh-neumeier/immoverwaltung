from fastapi.testclient import TestClient

from app import billing
from app.db import Billing, SessionLocal
from app.main import app
from tests.test_reserves import BILL, VALUES


def test_split_weighted_is_cent_exact():
    parts = billing._split_weighted(100.0, [85.5, 60.0, 64.5])
    assert sum(round(p * 100) for p in parts) == 10000 and parts == [40.72, 28.57, 30.71]
    assert billing._split_weighted(10.0, [0, 0]) == [5.0, 5.0]


def test_costs_by_living_area():
    with TestClient(app) as c:
        c.post("/admin/settings", data={"entity_grid": "sensor.grid", "entity_total": "sensor.total", "vat_rate": "19"})
        c.post("/admin/parties/0", data={"name": "Eigentümer", "is_owner": "1", "active": "1", "meters": "sensor.og",
                                         "area": "120"})
        c.post("/admin/parties/0", data={"name": "Familie Zweig", "unit_id": "WE-1", "meters": "sensor.eg", "active": "1",
                                         "area": "80,00"})
        assert "80,00 m²" in c.get("/admin/parties").text
        c.post("/admin/costs/fixed/0", data={"name": "Grundsteuer", "amount_gross": "49,33", "split": "area", "active": "1"})
        c.post("/admin/costs/alloc/0", data={"name": "Gebäudeversicherung", "source_type": "amount", "default_amount": "100",
                                             "key_type": "area", "keyarea_1": "1", "keyarea_2": "1", "active": "1"})
        page = c.get("/admin/costs").text
        assert "nach Wohnfläche (m²)" in page and "(nach Wohnfläche)" in page
        assert "Nach Wohnfläche (m²)" in c.get("/admin/costs/alloc/1").text and "80,00 m²" in c.get("/admin/costs/alloc/1").text
        r = c.post("/admin/billings", data=BILL, follow_redirects=False)
        url = r.headers["location"].split("?")[0]
        c.post(url, data={"action": "save", **BILL, **VALUES})
        with SessionLocal() as s:
            res = s.query(Billing).one().result
        lines = {(p["name"], l["label"]): l for p in res["parties"] for l in p["lines"]}
        gs_o, gs_z = lines[("Eigentümer", "Grundsteuer")], lines[("Familie Zweig", "Grundsteuer")]
        assert (gs_o["amount"], gs_z["amount"]) == (29.6, 19.73) and round(gs_o["amount"] + gs_z["amount"], 2) == 49.33
        assert "80,00 m² von 200,00 m² (40,0 %) von 49,33 €" in gs_z["note"]
        v = lines[("Familie Zweig", "Gebäudeversicherung")]
        assert v["amount"] == 40.0 and "80,00 m² von 200,00 m² Wohnfläche (40,0 %)" in v["note"]
        # Familie Zweig nimmt an der Umlage nicht mehr teil → Eigentümer trägt alles
        c.post("/admin/costs/alloc/1", data={"name": "Gebäudeversicherung", "source_type": "amount", "default_amount": "100",
                                             "key_type": "area", "keyarea_1": "1", "active": "1"})
        c.post(url, data={"action": "save", **BILL, **VALUES})
        with SessionLocal() as s:
            res = s.query(Billing).one().result
        lines = {(p["name"], l["label"]): l for p in res["parties"] for l in p["lines"]}
        assert lines[("Eigentümer", "Gebäudeversicherung")]["amount"] == 100.0
        assert ("Familie Zweig", "Gebäudeversicherung") not in lines
