"""FinTS-Anbindung mit einer simulierten Bank (keine echte Verbindung)."""

import re
from datetime import date, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient
from fints.client import NeedTANResponse, NeedVOPResponse, ResponseStatus
from fints.models import SEPAAccount

from app import banking, sepa
from app.db import BankAccess, Building, FinTxn, SepaExport, SessionLocal
from app.main import app
from tests.test_reserves import camt

GIRO, SPAR = "DE89370400440532013000", "DE02120300000000202051"


class FakeTAN(NeedTANResponse):
    def __init__(self, decoupled=False, text="Bitte Auftrag freigeben"):  # noqa: D107 – ohne Bank-Segmente
        self.decoupled, self.challenge, self.challenge_html = decoupled, text, text
        self.challenge_matrix = self.challenge_hhduc = self.vop_result = None
        self.command_seg = self.tan_request = None


class FakeVOP(NeedVOPResponse):
    def __init__(self, result="RVNM", name=""):  # noqa: D107
        single = SimpleNamespace(result=result, close_match_name=name, na_reason="")
        self.vop_result = SimpleNamespace(vop_single_result=single)
        self.command_seg = None


class Seg(SimpleNamespace):
    pass


class FakeBank:
    """Verhalten je Test einstellbar; merkt sich, was eingereicht wurde."""

    def __init__(self):
        self.init_tan = None  # TAN beim Dialogstart (90-Tage-SCA)
        self.fetch_tan = None  # TAN beim Umsatzabruf
        self.pay_challenges = []  # Abfolge von Antworten vor dem Ergebnis
        self.polls_pending = 0  # pushTAN: so oft „noch nicht freigegeben“
        self.sent = []
        self.ops = {GIRO: ["HKCAZ", "HKSAL", "HKCCS", "HKCCM", "HKCME", "HKCSE"], SPAR: ["HKCAZ", "HKSAL", "HKCUM"]}
        self.camt = {GIRO: camt(GIRO, [{"amount": -42.5, "purpose": "REWE SAGT DANKE", "name": "REWE", "other": SPAR}],
                                None, ("2026-10-30", 1000.0))}
        self.tans = []
        self.giro_debit = False
        self.pending = {}
        self.fetches = 0


BANK = FakeBank()


class FakeClient:
    def __init__(self, acc, pin, product):
        assert product == "TESTPRODUKT" and pin == "geheim"
        self.selected_tan_medium = None
        self.mech = None
        self.init_tan_response = None
        self.upd = SimpleNamespace(find_segments=lambda name: [
            Seg(iban=i, name_account_owner_1="Timo Muster", name_account_owner_2=None,
                account_product_name="Giro" if i == GIRO else "Aktivsparen", account_type=1 if i == GIRO else 10,
                allowed_transactions=[SimpleNamespace(transaction=o) for o in ops])
            for i, ops in BANK.ops.items()])
        self.bpd = SimpleNamespace(find_segment_first=lambda name: Seg(parameter=SimpleNamespace(
            supported_sepa_formats=["urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"])) if name == "HISPAS" else None)
        self._pending = None

    # Einrichtung
    def fetch_tan_mechanisms(self):
        return "923"

    def get_tan_mechanisms(self):
        return {"923": SimpleNamespace(name="pushTAN 2.0"), "920": SimpleNamespace(name="smsTAN")}

    def set_tan_mechanism(self, m):
        self.mech = m

    def get_current_tan_mechanism(self):
        return self.mech

    def is_tan_media_required(self):
        return False

    def __enter__(self):
        self.init_tan_response = BANK.init_tan

    def __exit__(self, *a):
        pass

    def deconstruct(self, including_private=False):
        return b"state"

    # Geschäftsvorfälle
    def get_sepa_accounts(self):
        return [SEPAAccount(i, "BYLADEM1001", "123", None, "37040044") for i in BANK.ops]

    def get_transactions_xml(self, account, start, end):
        BANK.fetches += 1
        if BANK.fetch_tan and self._pending is None:
            self._pending = ("fetch", account)
            return BANK.fetch_tan
        booked = [BANK.camt[account.iban]] if BANK.camt.get(account.iban) else []
        return booked, [BANK.pending[account.iban]] if BANK.pending.get(account.iban) else [None]

    def get_balance(self, account):
        from mt940.models import Balance  # wie python-fints: Soll-Saldo mit Status „D“
        if account.iban == SPAR:
            return Balance("C", "2500.00", date(2026, 11, 18))
        return Balance("D" if BANK.giro_debit else "C", "1234.56", date(2026, 11, 18))

    def _ok(self):
        return SimpleNamespace(status=ResponseStatus.SUCCESS, responses=[SimpleNamespace(code="0010", text="Auftrag angenommen")])

    def sepa_transfer(self, account, xml, multiple=False, control_sum=None, pain_descriptor=""):
        BANK.sent.append({"seg": "HKCCM" if multiple else "HKCCS", "iban": account.iban, "xml": xml,
                          "sum": control_sum, "desc": pain_descriptor})
        self._pending = ("pay", list(BANK.pay_challenges))
        return self._next()

    def _get_dialog(self):
        class D:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                pass
        return D()

    def _send_pay_with_possible_retry(self, dialog, seg, resume):
        BANK.sent.append({"seg": seg.header.type, "iban": seg.account.iban, "xml": seg.sepa_pain_message.decode(),
                          "sum": getattr(seg, "sum_amount", None)})
        self._pending = ("pay", list(BANK.pay_challenges))
        return self._next()

    def _continue_sepa_transfer(self, *a):
        return self._ok()

    def _next(self):
        kind, rest = self._pending
        if rest:
            return rest.pop(0)
        self._pending = None
        return self._ok()

    def approve_vop_response(self, ch):
        BANK.tans.append("VOP-OK")
        return self._next()

    def send_tan(self, ch, tan):
        BANK.tans.append(tan or "push")
        if ch.decoupled and BANK.polls_pending > 0:
            BANK.polls_pending -= 1
            return FakeTAN(decoupled=True)
        if self._pending is None:  # TAN beim Dialogstart
            return "init-ok"
        if self._pending[0] == "fetch":
            account = self._pending[1]
            self._pending = None
            return [BANK.camt.get(account.iban)], []
        return self._next()


def setup_module():
    banking.CLIENT_FACTORY = FakeClient


def teardown_module():
    banking.CLIENT_FACTORY = banking.make_client


def _prepare(c):
    global BANK
    BANK = FakeBank()
    banking.JOBS.clear()
    c.post("/admin/bank/settings", data={"fints_product_id": "TESTPRODUKT"})
    with SessionLocal() as s:
        b = s.query(Building).first()
        b.account_name, b.account_iban, b.account_bic = "Timo Muster", GIRO, "COBADEFFXXX"
        b.reserve_name, b.reserve_iban, b.reserve_bic = "Timo Muster", SPAR, "BYLADEM1001"
        s.commit()
        return b.id


def test_pain_roundtrip_and_formats():
    deb, cred = sepa.Account("Timo Muster", GIRO, "COBADEFFXXX"), sepa.Account("Timo Muster", SPAR)
    _, xml = sepa.credit_transfer(deb, cred, [sepa.Transfer(10.5, "RUECKLAGE A", "RLA1"),
                                              sepa.Transfer(4.5, "RUECKLAGE B", "RLB2")], date(2026, 12, 1))
    order = banking.parse_pain(xml.decode())
    assert order.debtor.iban == GIRO and order.exec_date == date(2026, 12, 1) and order.total == 15.0
    assert [t.end_to_end for t in order.transfers] == ["RLA1", "RLB2"] and order.transfers[0].creditor.iban == SPAR
    old = banking.build_pain(order, "pain.001.001.03", banking.IMMEDIATE).decode()
    assert "pain.001.001.03" in old and "<ReqdExctnDt>1999-01-01</ReqdExctnDt>" in old and "RLB2" in old


def test_connect_sync_with_pushtan_and_dedupe():
    with TestClient(app) as c:
        _prepare(c)
        # ohne Produkt-ID → klarer Hinweis
        c.post("/admin/bank/settings", data={"fints_product_id": ""})
        r = c.post("/admin/bank/access/0", data={"name": "Sparkasse", "blz": "37040044", "login": "user1",
                                                  "server": "https://fints.example/fints30", "pin": "geheim",
                                                  "store_pin": "1", "connect": "1"})
        assert "Produktregistrierung fehlt" in r.text
        c.post("/admin/bank/settings", data={"fints_product_id": "TESTPRODUKT"})
        with SessionLocal() as s:
            acc = s.query(BankAccess).one()
            assert acc.pin and "geheim" not in acc.pin  # verschlüsselt
            aid = acc.id
        r = c.post(f"/admin/bank/access/{aid}/connect", data={})
        assert "2 Konten gefunden, davon 2 in der App hinterlegt" in r.text
        page = c.get("/admin/bank").text
        assert "DE89 3704 0044 0532 0130 00" in page and "Aktivsparen" in page and "Rücklagenkonto" in page
        assert "pushTAN 2.0" in page
        with SessionLocal() as s:
            acc = s.get(BankAccess, aid)
            assert acc.tan_mechanism == "923" and acc.state  # pushTAN bevorzugt, Zustand gespeichert
            spar = banking.find_account(acc, SPAR)
            assert banking.allows(spar, "HKCCS") is False and banking.allows(spar, "HKCAZ") is True

        # Umsatzabruf: Bank verlangt pushTAN beim Dialogstart (90-Tage-Freigabe), erst beim 2. Abfragen freigegeben
        BANK.init_tan, BANK.polls_pending = FakeTAN(decoupled=True), 1
        r = c.post(f"/admin/bank/access/{aid}/sync", data={})
        assert "S-pushTAN-App" in r.text and "data-poll" in r.text
        assert c.get(f"/admin/bank/job/{aid}/poll").json()["state"] == "waiting"
        d = c.get(f"/admin/bank/job/{aid}/poll").json()
        assert d["state"] == "done" and "1 Umsätze abgerufen" in d["message"] and "Finanzen: 1 neu" in d["message"]
        with SessionLocal() as s:
            assert s.query(FinTxn).count() == 1
            acc = s.get(BankAccess, aid)
            assert banking.find_account(acc, GIRO)["balance"] == 1234.56 and acc.last_sync
            assert s.query(Building).first().bank_reserve_balance == 2500.0
        # zweiter Abruf: nichts doppelt; Soll-Saldo (z. B. Darlehen, Dispo) bleibt negativ
        BANK.init_tan, BANK.giro_debit = None, True
        r = c.post(f"/admin/bank/access/{aid}/sync", data={})
        with SessionLocal() as s:
            assert s.query(FinTxn).count() == 1
            assert banking.find_account(s.get(BankAccess, aid), GIRO)["balance"] == -1234.56
        assert "-1.234,56 €" in c.get("/admin/bank").text


def test_submit_export_with_tan_vop_and_confirm():
    with TestClient(app) as c:
        bid = _prepare(c)
        c.post("/admin/bank/access/0", data={"name": "Sparkasse", "blz": "37040044", "login": "user1",
                                              "server": "https://fints.example/fints30", "pin": "geheim",
                                              "store_pin": "1", "connect": "1"})
        with SessionLocal() as s:
            aid = s.query(BankAccess).one().id
            _, xml = sepa.credit_transfer(sepa.Account("Timo Muster", GIRO, "COBADEFFXXX"), sepa.Account("Timo Muster", SPAR),
                                          [sepa.Transfer(20, "RUECKLAGE IPTV", "RL1"), sepa.Transfer(30.34, "RUECKLAGE WASSER", "RL2")],
                                          date.today())
            ex = SepaExport(building_id=bid, kind="in", msg_id="M1", exec_date=date.today(), count=2, total=50.34,
                            xml=xml.decode(), filename="Ruecklage.xml")
            s.add(ex)
            s.commit()
            eid = ex.id
        page = c.get("/admin/bank").text
        assert "Offene Aufträge" in page and f"/admin/bank/export/{eid}" in page

        # ohne ausdrückliche Bestätigung passiert nichts
        n0 = len(BANK.sent)
        r = c.post(f"/admin/bank/export/{eid}", data={"confirm": "ja"})
        assert "AUSFÜHREN" in r.text and len(BANK.sent) == n0
        # Empfängerprüfung weicht ab → bestätigen → TAN eingeben
        BANK.pay_challenges = [FakeVOP("RVMC", "TIMO MUSTERMANN"), FakeTAN(decoupled=False, text="TAN für 50,34 EUR")]
        r = c.post(f"/admin/bank/export/{eid}", data={"confirm": "AUSFÜHREN"})
        assert "passt nur teilweise" in r.text and "TIMO MUSTERMANN" in r.text
        r = c.post(f"/admin/bank/job/{aid}", data={"vop_ok": "1"})
        assert "TAN für 50,34 EUR" in r.text
        r = c.post(f"/admin/bank/job/{aid}", data={"tan": "123456"})
        assert "2 Überweisung(en) über 50,34 € eingereicht" in r.text and "als ausgeführt bestätigt" in r.text
        sent = BANK.sent[-1]
        assert sent["seg"] == "HKCCM" and str(sent["sum"]) == "50.34" and "1999-01-01" in sent["xml"]
        assert "RL2" in sent["xml"] and BANK.tans == ["VOP-OK", "123456"]
        with SessionLocal() as s:
            ex = s.get(SepaExport, eid)
            assert ex.status == "done" and ex.confirmed_by.startswith("FinTS")

        # terminierter Sammelauftrag (Ausführung in der Zukunft) → HKCME mit Datum
        future = sepa.next_business_day(date.today() + timedelta(days=10))
        with SessionLocal() as s:
            _, xml = sepa.credit_transfer(sepa.Account("Timo Muster", GIRO, "COBADEFFXXX"), sepa.Account("Timo Muster", SPAR),
                                          [sepa.Transfer(5, "A", "RL3"), sepa.Transfer(6, "B", "RL4")], future)
            ex = SepaExport(building_id=bid, kind="in", msg_id="M2", exec_date=future, count=2, total=11,
                            xml=xml.decode(), filename="R2.xml")
            s.add(ex)
            s.commit()
            eid2 = ex.id
        BANK.pay_challenges = [FakeTAN(decoupled=True)]
        c.post(f"/admin/bank/export/{eid2}", data={"confirm": "AUSFÜHREN"})
        assert BANK.sent[-1]["seg"] == "HKCME" and future.isoformat() in BANK.sent[-1]["xml"]
        d = c.get(f"/admin/bank/job/{aid}/poll").json()
        assert d["state"] == "done" and f"Ausführung am {future:%d.%m.%Y}" in d["message"]

        # vom Sparkonto erlaubt die Bank keine Überweisung an Fremde → klare Meldung, nichts gesendet
        n = len(BANK.sent)
        r = c.post("/admin/bank/transfer", data={"confirm": "AUSFÜHREN", "from": SPAR, "to": "other", "to_name": "Fremd GmbH",
                                                 "to_iban": "DE44500105175407324931", "amount": "100", "purpose": "TEST"})
        assert "keine Einzelüberweisung per FinTS" in r.text and len(BANK.sent) == n
        # … aber eine Umbuchung aufs eigene Girokonto (HKCUM)
        BANK.pay_challenges = [FakeTAN(decoupled=False)]
        c.post("/admin/bank/transfer", data={"confirm": "AUSFÜHREN", "from": SPAR, "to": GIRO, "amount": "100", "purpose": "AUS RUECKLAGE"})
        r = c.post(f"/admin/bank/job/{aid}", data={"tan": "654321"})
        assert "1 Überweisung(en) über 100,00 € eingereicht" in r.text
        assert BANK.sent[-1]["seg"] == "HKCUM" and BANK.sent[-1]["iban"] == SPAR and GIRO in BANK.sent[-1]["xml"]

        # manuelle Gutschrift vom Rücklagenkonto (Kennung) → Umbuchung mit Kennung als Referenz, danach bestätigt
        with SessionLocal() as s:
            ex = SepaExport(building_id=bid, kind="out", msg_id="M3", exec_date=date.today(), count=1, total=12.34,
                            manual=True, manual_purpose="RG-ABCDEFGH GUTSCHRIFT RUECKLAGE", token="RG-ABCDEFGH",
                            filename="R3.xml")
            s.add(ex)
            s.commit()
            eid3 = ex.id
        BANK.pay_challenges = []
        r = c.post(f"/admin/bank/export/{eid3}", data={"confirm": "AUSFÜHREN"})
        assert "eingereicht" in r.text and BANK.sent[-1]["seg"] == "HKCUM"
        assert "RG-ABCDEFGH GUTSCHRIFT" in BANK.sent[-1]["xml"] and "<EndToEndId>RG-ABCDEFGH" in BANK.sent[-1]["xml"]
        with SessionLocal() as s:
            assert s.get(SepaExport, eid3).status == "done"
        # Übertrag Giro → Aktivsparen
        BANK.pay_challenges = []
        r = c.post("/admin/bank/transfer", data={"confirm": "AUSFÜHREN", "from": GIRO, "to": SPAR, "amount": "250,00", "purpose": "SPAREN"})
        assert "1 Überweisung(en) über 250,00 € eingereicht" in r.text
        assert BANK.sent[-1]["seg"] == "HKCCS" and SPAR in BANK.sent[-1]["xml"] and "SPAREN" in BANK.sent[-1]["xml"]


def test_cancel_and_wrong_pin_message():
    with TestClient(app) as c:
        _prepare(c)
        c.post("/admin/bank/access/0", data={"name": "Sparkasse", "blz": "37040044", "login": "user1",
                                              "server": "https://fints.example/fints30"})
        with SessionLocal() as s:
            aid = s.query(BankAccess).one().id
        r = c.post(f"/admin/bank/access/{aid}/connect", data={"pin": "falsch"})
        assert "AssertionError" in r.text or "fehlgeschlagen" in r.text.lower() or "Verbinden" in r.text
        r = c.post(f"/admin/bank/access/{aid}/connect", data={"pin": "geheim"})
        BANK.init_tan = FakeTAN(decoupled=True)
        BANK.polls_pending = 5
        c.post(f"/admin/bank/access/{aid}/sync", data={"pin": "geheim"})
        assert banking.current_job(aid).waiting
        r = c.post(f"/admin/bank/job/{aid}", data={"cancel": "1"})
        assert "abgebrochen" in r.text and banking.current_job(aid) is None
        assert re.search(r"Vorgang abgebrochen", c.get("/admin/bank").text)


def _pdng(own, entries):
    return camt(own, entries).replace(b"<Cd>BOOK</Cd>", b"<Cd>PDNG</Cd>").replace(b"<BookgDt><Dt>2026-10-30</Dt></BookgDt>", b"")


def _connect(c):
    c.post("/admin/bank/access/0", data={"name": "Sparkasse", "blz": "37040044", "login": "user1",
                                          "server": "https://fints.example/fints30", "pin": "geheim",
                                          "store_pin": "1", "connect": "1"})
    with SessionLocal() as s:
        return s.query(BankAccess).one().id


def test_pending_refresh_and_booking_replaces_pending():
    from app import finance
    from app.db import FinCategory

    with TestClient(app) as c:
        _prepare(c)
        aid = _connect(c)
        amazon = {"amount": -19.99, "purpose": "AMAZON MKTP DE 123", "name": "AMAZON", "other": SPAR, "date": "2026-11-18"}
        BANK.pending[GIRO] = _pdng(GIRO, [amazon])
        assert "Jetzt aktualisieren" in c.get("/admin/finanzen").text
        r = c.post("/admin/bank/refresh", data={"back": "/admin/finanzen"})
        assert r.url.path == "/admin/finanzen" and "1 Umsätze + 1 vorgemerkt abgerufen" in r.text
        assert "vorgemerkt" in r.text
        with SessionLocal() as s:
            p = s.query(FinTxn).filter(FinTxn.pending.is_(True)).one()
            pid = p.id
            cat = s.query(FinCategory).first()
            p.category_id = cat.id  # von Hand zugeordnet → bleibt nach dem Buchen
            s.commit()
            d = finance.overview(s)
            assert d["pending_count"] == 1 and d["pending_sum"] == -19.99
            assert banking.find_account(s.get(BankAccess, aid), GIRO)["pending_count"] == 1
        # jetzt gebucht: Vormerkung wird ersetzt, keine Dublette
        BANK.pending = {}
        BANK.camt[GIRO] = camt(GIRO, [{"amount": -42.5, "purpose": "REWE SAGT DANKE", "name": "REWE", "other": SPAR},
                                      {**amazon, "date": "2026-11-19"}], None, ("2026-11-19", 957.51))
        c.post("/admin/bank/refresh", data={"back": "/admin/finanzen"})
        with SessionLocal() as s:
            rows = s.query(FinTxn).order_by(FinTxn.id).all()
            assert len(rows) == 2 and not any(r.pending for r in rows)
            a = s.get(FinTxn, pid)
            assert a.booking_date == date(2026, 11, 19) and a.category_id == cat.id
        # storniert: Vormerkung verschwindet beim nächsten Abruf
        BANK.pending[GIRO] = _pdng(GIRO, [{"amount": -5.0, "purpose": "KIOSK", "name": "KIOSK", "other": SPAR}])
        c.post("/admin/bank/refresh", data={})
        BANK.pending = {}
        c.post("/admin/bank/refresh", data={})
        with SessionLocal() as s:
            assert s.query(FinTxn).count() == 2


def test_order_then_accounts_refreshed_and_sync_slots():
    with TestClient(app) as c:
        _prepare(c)
        aid = _connect(c)
        before = BANK.fetches
        r = c.post("/admin/bank/transfer", data={"confirm": "AUSFÜHREN", "from": GIRO, "to": SPAR, "amount": "10", "purpose": "SPAREN"})
        assert "eingereicht" in r.text and "Konten aktualisiert" in r.text and BANK.fetches == before + 2
    with SessionLocal() as s:
        acc = s.get(BankAccess, aid)
        acc.sync_times = "7,13,19"
        assert banking.due_slot(acc, datetime(2026, 11, 19, 6, 30)) is None
        assert banking.due_slot(acc, datetime(2026, 11, 19, 14, 5)) == "2026-11-19T13"
        acc.last_slot = "2026-11-19T13"
        assert banking.due_slot(acc, datetime(2026, 11, 19, 18, 59)) is None
        assert banking.due_slot(acc, datetime(2026, 11, 19, 19, 0)) == "2026-11-19T19"


def test_monthly_run_submits_via_fints(monkeypatch):
    import smtplib
    import time

    from app import reserves
    from app.db import ReservePlan

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
            sent.append(msg)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(banking, "BACKGROUND_POLL", 0.05)
    with TestClient(app) as c:
        bid = _prepare(c)
        _connect(c)
        with SessionLocal() as s:
            b = s.get(Building, bid)
            b.sepa_mail, b.sepa_mail_to = True, "timo@example.test"
            s.add(ReservePlan(building_id=bid, name="Immobilie · Grundsteuer", label="GRUNDSTEUER", annual_amount=600,
                              interval=1, start="2026-12", funding="sepa", category="immobilie", active=True))
            s.commit()
        BANK.pay_challenges = [FakeTAN(decoupled=True)]
        BANK.polls_pending = 2  # pushTAN erst nach zwei Abfragen freigegeben
        with SessionLocal() as s:
            msg = reserves.monthly_run(s, s.get(Building, bid), today=date(2026, 11, 25), force=True)
        assert "per FinTS eingereicht" in msg
        for _ in range(100):
            with SessionLocal() as s:
                ex = s.query(SepaExport).one()
                if ex.status == "done":
                    break
            time.sleep(0.05)
        assert ex.status == "done" and ex.confirmed_by.startswith("FinTS")
        assert BANK.sent[-1]["seg"] == "HKCSE" and "GRUNDSTEUER" in BANK.sent[-1]["xml"]  # eine Zuführung → Termin-Einzelüberweisung
        mail = sent[-1]
        assert "per FinTS" in mail["Subject"] and not any((p.get_filename() or "").endswith(".xml") for p in mail.walk())
