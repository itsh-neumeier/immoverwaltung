"""Bankanbindung per FinTS 3.0 (PIN/TAN, z. B. Sparkasse mit S-pushTAN).

- Bankzugang anlegen → Konten der Bank abrufen (IBAN, Produkt, erlaubte Geschäftsvorfälle je Konto)
- Umsätze (camt) und Salden abrufen → Finanzplanung und Rücklagen-Abgleich (wie der Datei-Import)
- SEPA-Aufträge der App (Sammelauftrag Rücklagen, Gutschriften, manuelle Überweisung vom Sparkonto) und freie
  Überträge zwischen den eigenen Konten direkt einreichen – Freigabe immer mit TAN (pushTAN: in der App bestätigen)
- Empfängerprüfung (Verification of Payee): Ergebnis anzeigen, bei Abweichung bewusst bestätigen

Ein laufender Bankdialog (mit offener TAN-Abfrage) bleibt im Speicher des App-Prozesses (ein Worker) und wird nach
10 Minuten verworfen. Die PIN wird nur gespeichert, wenn ausdrücklich gewünscht – dann verschlüsselt (secretbox).
Die FinTS-Produktregistrierung (Deutsche Kreditwirtschaft) wird in der App bzw. per ``FINTS_PRODUCT_ID`` hinterlegt.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from . import bankimport, scope, secretbox, sepa
from .accounts import _page, _redirect, current
from .db import BankAccess, BankLine, Building, FinTxn, SepaExport, SessionLocal, get_session, get_settings, save_settings

log = logging.getLogger(__name__)
router = APIRouter()

PRODUCT_VERSION = "1.0"
JOB_TTL = 10 * 60  # offene TAN-Abfrage verwerfen nach … Sekunden
POLL_SECONDS = 3  # pushTAN: Status alle … Sekunden abfragen (Bankvorgabe: nicht öfter als alle 2 s)
LOG_KEEP = 30
FIRST_DAYS = 90  # erster Abruf ohne Startdatum: so viele Tage zurück (ohne erneute TAN meist max. 90)
IMMEDIATE = date(1999, 1, 1)  # SEPA-Vorgabe der Kreditwirtschaft für „sofort ausführen“

ACCOUNT_TYPES = {range(1, 10): "Girokonto", range(10, 20): "Sparkonto", range(20, 30): "Festgeld",
                 range(30, 40): "Depot", range(40, 50): "Darlehen", range(50, 60): "Kreditkarte"}
OPS = {"HKCAZ": "Umsätze (camt)", "HKKAZ": "Umsätze (MT940)", "HKSAL": "Saldo", "HKCCS": "Einzelüberweisung",
       "HKCCM": "Sammelüberweisung", "HKCSE": "Terminüberweisung", "HKCME": "Termin-Sammelüberweisung",
       "HKIPZ": "Echtzeitüberweisung", "HKCUM": "Umbuchung"}


class BankError(RuntimeError):
    pass


# --------------------------------------------------------------------------- Konfiguration
def product_id(s: Session) -> str:
    return os.getenv("FINTS_PRODUCT_ID", "").strip() or get_settings(s).get("fints_product_id", "").strip()


def account_type(code) -> str:
    try:
        n = int(code)
    except (TypeError, ValueError):
        return ""
    return next((v for k, v in ACCOUNT_TYPES.items() if n in k), "")


def find_account(acc: BankAccess, iban: str) -> Optional[dict]:
    iban = sepa.normalize_iban(iban)
    return next((a for a in (acc.accounts or []) if a.get("iban") == iban), None)


def access_for_iban(s: Session, iban: str) -> Optional[BankAccess]:
    iban = sepa.normalize_iban(iban)
    if not iban:
        return None
    return next((a for a in s.query(BankAccess).order_by(BankAccess.id) if find_account(a, iban)), None)


def allows(account: dict, op: str) -> Optional[bool]:
    """True/False laut Bank (UPD); None = Bank meldet keine Liste (dann einfach versuchen)."""
    ops = account.get("ops")
    if not ops:
        return None
    return op in ops


def roles(s: Session) -> dict[str, list[str]]:
    """IBAN → Rollen in der App (Hauptkonto/Rücklagenkonto je Gebäude, Finanzplanung)."""
    from . import finance

    out: dict[str, list[str]] = {}
    for b in s.query(Building).order_by(Building.id):
        if b.account_iban:
            out.setdefault(sepa.normalize_iban(b.account_iban), []).append(f"Hauptkonto {b.label}")
        if b.reserve_iban:
            out.setdefault(sepa.normalize_iban(b.reserve_iban), []).append(f"Rücklagenkonto {b.label}")
    if fi := finance.finance_iban(s):
        out.setdefault(fi, []).append("Finanzplanung")
    return out


def _log(acc: BankAccess, text: str, error: bool = False) -> None:
    entries = list(acc.log or [])
    entries.insert(0, {"t": datetime.now().strftime("%d.%m.%Y %H:%M"), "m": text[:500], "e": error})
    acc.log = entries[:LOG_KEEP]
    if error:
        acc.last_error = text[:1000]
    else:
        acc.last_msg, acc.last_error = text[:1000], ""


# --------------------------------------------------------------------------- terminierte Aufträge (fehlen in python-fints)
def _scheduled_segments():
    from fints.fields import DataElementField, DataElementGroupField
    from fints.formals import KTI1, Amount1
    from fints.segments.base import FinTS3Segment

    if "HKCSE1" in globals():
        return globals()["HKCSE1"], globals()["HKCME1"]

    class HKCSE1(FinTS3Segment):  # noqa: N801 – Name = Segmentkennung
        """Terminierte SEPA-Einzelüberweisung einreichen, Version 1"""
        account = DataElementGroupField(type=KTI1, _d="Kontoverbindung international")
        sepa_descriptor = DataElementField(type='an', max_length=256, _d="SEPA Descriptor")
        sepa_pain_message = DataElementField(type='bin', _d="SEPA pain message")

    class HKCME1(FinTS3Segment):  # noqa: N801
        """Terminierte SEPA-Sammelüberweisung einreichen, Version 1"""
        account = DataElementGroupField(type=KTI1, _d="Kontoverbindung international")
        sum_amount = DataElementGroupField(type=Amount1, _d="Summenfeld")
        request_single_booking = DataElementField(type='jn', _d="Einzelbuchung gewünscht")
        sepa_descriptor = DataElementField(type='an', max_length=256, _d="SEPA Descriptor")
        sepa_pain_message = DataElementField(type='bin', _d="SEPA pain message")

    class HKCUM1(FinTS3Segment):  # noqa: N801
        """SEPA-Umbuchung (zwischen eigenen Konten derselben Bank, z. B. Sparkonto → Girokonto), Version 1"""
        account = DataElementGroupField(type=KTI1, _d="Kontoverbindung international")
        sepa_descriptor = DataElementField(type='an', max_length=256, _d="SEPA Descriptor")
        sepa_pain_message = DataElementField(type='bin', _d="SEPA pain message")

    globals().update(HKCSE1=HKCSE1, HKCME1=HKCME1, HKCUM1=HKCUM1)
    return HKCSE1, HKCME1


# --------------------------------------------------------------------------- SEPA-Nachrichten
@dataclass
class Order:
    """Auszuführende Überweisung(en) von einem eigenen Konto (eine pain.001-Nachricht)."""

    debtor: sepa.Account
    transfers: list  # sepa.Transfer mit creditor
    exec_date: date
    msg_id: str = ""
    label: str = ""
    export_id: Optional[int] = None
    segment: str = ""  # leer = automatisch; "HKCUM" = Umbuchung zwischen eigenen Konten

    @property
    def total(self) -> float:
        return round(sum(t.amount for t in self.transfers), 2)


def parse_pain(xml: str) -> Order:
    """Gespeicherte pain.001 der App wieder einlesen (zum Einreichen per FinTS)."""
    root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    loc = lambda el: el.tag.rsplit("}", 1)[-1]  # noqa: E731
    find = lambda el, *path: bankimport._child(el, *path)  # noqa: E731
    text = lambda el, *path: bankimport._text(el, *path)  # noqa: E731
    init = next(c for c in root if loc(c) == "CstmrCdtTrfInitn")
    msg_id = text(init, "GrpHdr", "MsgId")
    pmt = find(init, "PmtInf")
    exec_el = find(pmt, "ReqdExctnDt")
    exec_txt = text(exec_el, "Dt") or text(exec_el)
    debtor = sepa.Account(text(pmt, "Dbtr", "Nm"), text(pmt, "DbtrAcct", "Id", "IBAN"),
                          text(pmt, "DbtrAgt", "FinInstnId", "BICFI") or text(pmt, "DbtrAgt", "FinInstnId", "BIC"))
    transfers = []
    for tx in bankimport._children(pmt, "CdtTrfTxInf"):
        cred = sepa.Account(text(tx, "Cdtr", "Nm"), text(tx, "CdtrAcct", "Id", "IBAN"),
                            text(tx, "CdtrAgt", "FinInstnId", "BICFI") or text(tx, "CdtrAgt", "FinInstnId", "BIC"))
        transfers.append(sepa.Transfer(float(text(tx, "Amt", "InstdAmt")), text(tx, "RmtInf", "Ustrd"),
                                       text(tx, "PmtId", "EndToEndId"), cred))
    return Order(debtor, transfers, date.fromisoformat(exec_txt) if exec_txt else IMMEDIATE, msg_id)


def build_pain(order: Order, fmt: str, exec_date: date) -> bytes:
    _, xml = sepa.credit_transfer(order.debtor, order.transfers[0].creditor, order.transfers, exec_date, fmt,
                                  msg_id=order.msg_id, batch=True)
    return xml


def order_for_export(s: Session, ex: SepaExport) -> Order:
    bld = s.get(Building, ex.building_id)
    if bld is None:
        raise BankError("Gebäude der Datei fehlt.")
    if ex.manual:  # eine Überweisung vom Rücklagenkonto (Sparkonto) mit Kennung
        debtor = sepa.Account(bld.reserve_name or bld.account_name, bld.reserve_iban, bld.reserve_bic)
        cred = sepa.Account(bld.account_name, bld.account_iban, bld.account_bic)
        order = Order(debtor, [sepa.Transfer(ex.total, ex.manual_purpose, ex.token, cred)], date.today(),
                      label=f"Überweisung {ex.token}")
    else:
        order = parse_pain(ex.xml)
        order.label = ex.filename
    order.export_id = ex.id
    return order


# --------------------------------------------------------------------------- Client
def make_client(acc: BankAccess, pin: str, product: str):
    from fints.client import FinTS3PinTanClient

    state = secretbox.decrypt(acc.state) if acc.state else None
    c = FinTS3PinTanClient(acc.blz, acc.login, pin, acc.server, customer_id=acc.customer_id or None,
                           product_id=product, product_version=PRODUCT_VERSION, from_data=state)
    if acc.tan_medium:
        c.selected_tan_medium = acc.tan_medium
    return c


CLIENT_FACTORY: Callable = make_client  # Tests ersetzen das durch eine Attrappe


def _sepa_account(a: dict):
    from fints.models import SEPAAccount

    return SEPAAccount(a.get("iban"), a.get("bic"), a.get("accountnumber"), a.get("subaccount"), a.get("blz"))


def _bank_formats(c) -> list[str]:
    seg = c.bpd.find_segment_first("HISPAS") if getattr(c, "bpd", None) is not None else None
    if not seg:
        return []
    return [re.sub(r"^.*xsd:|\.xsd$", "", str(x)) for x in seg.parameter.supported_sepa_formats]


def _pick_format(c) -> str:
    supported = _bank_formats(c)
    for fmt in ("pain.001.001.09", "pain.001.001.03"):
        if not supported or fmt in supported:
            return fmt
    raise BankError("Die Bank nennt kein passendes SEPA-Format (" + ", ".join(supported) + ").")


def _has_bpd(c, seg: str) -> bool:
    try:
        return bool(c.bpd.find_segment_first(seg))
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- Vorgänge (Jobs)
@dataclass
class Job:
    id: str
    access_id: int
    kind: str  # connect | sync | order
    title: str
    steps: list
    pin: str
    user: str = ""
    params: dict = field(default_factory=dict)
    idx: int = 0
    results: list = field(default_factory=list)
    client: Any = None
    challenge: Any = None
    stage: str = "init"
    started: float = field(default_factory=time.time)
    done: bool = False
    ok: bool = False
    message: str = ""
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def waiting(self) -> bool:
        return self.challenge is not None and not self.done

    def challenge_info(self) -> dict:
        from fints.client import NeedVOPResponse

        ch = self.challenge
        if ch is None:
            return {}
        if isinstance(ch, NeedVOPResponse):
            r = ch.vop_result.vop_single_result
            return {"type": "vop", "result": str(r.result), "close_name": str(getattr(r, "close_match_name", "") or ""),
                    "reason": str(getattr(r, "na_reason", "") or ""), "text": VOP_TEXT.get(str(r.result), str(r.result))}
        info = {"type": "decoupled" if ch.decoupled else "tan", "text": ch.challenge or "",
                "html": ch.challenge_html or ""}
        if ch.challenge_matrix:
            import base64

            mime, data = ch.challenge_matrix
            info["image"] = f"data:{mime};base64,{base64.b64encode(data).decode()}"
        if ch.challenge_hhduc:
            info["hhduc"] = ch.challenge_hhduc
        if getattr(ch, "vop_result", None) is not None:
            r = ch.vop_result.vop_single_result
            info["vop"] = VOP_TEXT.get(str(r.result), str(r.result))
        return info


VOP_TEXT = {"RCVC": "Empfängername passt ✓", "RVMC": "Empfängername passt nur teilweise",
            "RVNM": "Empfängername passt NICHT zur IBAN", "RVNA": "Empfängerprüfung nicht möglich",
            "PDNG": "Empfängerprüfung läuft noch"}

JOBS: dict[int, Job] = {}
_jobs_lock = threading.Lock()


def _close(job: Job) -> None:
    """Dialog beenden; Zustand (System-ID, BPD/UPD, TAN-Verfahren) und Kontodaten für später merken."""
    c, job.client = job.client, None
    if c is None:
        return
    try:
        c.__exit__(None, None, None)
    except Exception:  # noqa: BLE001
        pass
    if job.kind == "connect":
        job.params["upd"] = read_upd(c)
    try:
        job.params["state"] = c.deconstruct(including_private=True)
    except Exception:  # noqa: BLE001
        log.info("FinTS-Zustand nicht lesbar", exc_info=True)


def cleanup(now: Optional[float] = None) -> None:
    now = now or time.time()
    with _jobs_lock:
        for aid, job in list(JOBS.items()):
            if now - job.started > JOB_TTL:
                with job.lock:
                    _close(job)
                    job.challenge, job.pin = None, ""
                    if not job.done:
                        job.done, job.message = True, "Abgebrochen: Zeit für die TAN-Freigabe abgelaufen."
                JOBS.pop(aid, None)


def current_job(access_id: int) -> Optional[Job]:
    cleanup()
    return JOBS.get(access_id)


def cancel(access_id: int) -> None:
    with _jobs_lock:
        job = JOBS.pop(access_id, None)
    if job:
        with job.lock:
            _close(job)
            job.done, job.challenge, job.pin = True, None, ""
            job.message = job.message or "Abgebrochen."


def start(s: Session, acc: BankAccess, kind: str, steps: list, pin: str, title: str, user: str = "",
          params: Optional[dict] = None) -> Job:
    """Bankdialog öffnen und Schritte ausführen, bis eine TAN gebraucht wird oder alles erledigt ist."""
    prod = product_id(s)
    if not prod:
        raise BankError("FinTS-Produktregistrierung fehlt – oben unter „Einstellungen“ eintragen.")
    if not pin:
        raise BankError("PIN fehlt.")
    cancel(acc.id)
    job = Job(secrets.token_urlsafe(8), acc.id, kind, title, steps, pin, user, params or {})
    with _jobs_lock:
        JOBS[acc.id] = job
    with job.lock:
        try:
            c = CLIENT_FACTORY(acc, pin, prod)
            if kind == "connect" or not acc.state:
                _bootstrap(c, acc)
                s.commit()
            elif acc.tan_mechanism:
                try:
                    c.set_tan_mechanism(acc.tan_mechanism)
                except Exception:  # noqa: BLE001
                    pass
            c.__enter__()
            job.client = c
            if getattr(c, "init_tan_response", None) is not None:
                job.challenge, job.stage = c.init_tan_response, "init"
                return job
            job.stage = "run"
            _advance(job)
        except Exception as e:  # noqa: BLE001
            _fail(job, e)
        finally:
            _persist(job)
    return job


def answer(access_id: int, tan: str = "", vop_ok: bool = False, poll: bool = False) -> Job:
    """TAN senden, pushTAN-Status abfragen oder Empfängerprüfung bestätigen."""
    from fints.client import NeedRetryResponse, NeedVOPResponse

    job = current_job(access_id)
    if job is None:
        raise BankError("Kein offener Bankvorgang (abgelaufen?) – bitte neu starten.")
    with job.lock:
        if job.done or job.challenge is None:
            return job
        c, ch = job.client, job.challenge
        try:
            if isinstance(ch, NeedVOPResponse):
                if poll:
                    return job
                if not vop_ok:
                    _close(job)
                    job.done, job.challenge, job.message = True, None, "Abgebrochen (Empfängerprüfung nicht bestätigt)."
                    return job
                res = c.approve_vop_response(ch)
            else:
                if poll and not ch.decoupled:
                    return job
                if not ch.decoupled and not tan.strip():
                    return job
                res = c.send_tan(ch, tan.strip())
            if isinstance(res, NeedRetryResponse):
                job.challenge = res
                return job
            job.challenge = None
            if job.stage == "init":
                job.stage = "run"
            else:
                job.results.append(res)
                job.idx += 1
            _advance(job)
        except Exception as e:  # noqa: BLE001
            _fail(job, e)
        finally:
            _persist(job)
    return job


def _bootstrap(c, acc: BankAccess) -> None:
    """TAN-Verfahren und -Medium ermitteln (vor dem eigentlichen Dialog)."""
    c.fetch_tan_mechanisms()
    mechs = c.get_tan_mechanisms()
    acc.mechanisms = [{"id": k, "name": str(getattr(v, "name", "") or k),
                       "decoupled": "decoupled" in str(getattr(v, "name", "")).lower()
                       or "push" in str(getattr(v, "name", "")).lower()} for k, v in mechs.items()]
    if acc.tan_mechanism and acc.tan_mechanism in mechs:
        c.set_tan_mechanism(acc.tan_mechanism)
    elif mechs:
        push = next((k for k, v in mechs.items() if "push" in str(getattr(v, "name", "")).lower()), None)
        c.set_tan_mechanism(push or next(iter(mechs)))
    acc.tan_mechanism = c.get_current_tan_mechanism() or ""
    try:
        if mechs and c.is_tan_media_required() and not c.selected_tan_medium:
            _, media = c.get_tan_media()
            names = [m.tan_medium_name for m in media if getattr(m, "tan_medium_name", None)]
            if names:
                c.selected_tan_medium = acc.tan_medium = names[0]
    except Exception:  # noqa: BLE001
        log.info("TAN-Medien nicht abrufbar", exc_info=True)


def _advance(job: Job) -> None:
    from fints.client import NeedRetryResponse

    while job.idx < len(job.steps):
        res = _exec(job, job.steps[job.idx])
        if isinstance(res, NeedRetryResponse):
            job.challenge = res
            return
        job.results.append(res)
        job.idx += 1
    _close(job)
    job.done, job.ok = True, True


def _exec(job: Job, step: tuple):
    c = job.client
    kind = step[0]
    if kind == "accounts":
        return c.get_sepa_accounts()
    acc_d = step[1]
    account = _sepa_account(acc_d)
    if kind == "fetch":
        return c.get_transactions_xml(account, step[2], step[3])
    if kind == "balance":
        return c.get_balance(account)
    if kind == "order":
        order: Order = step[2]
        scheduled = order.exec_date > date.today()
        fmt = _pick_format(c)
        xml = build_pain(order, fmt, order.exec_date if scheduled else IMMEDIATE).decode("utf-8")
        desc = f"urn:iso:std:iso:20022:tech:xsd:{fmt}"
        multiple = len(order.transfers) > 1
        if order.segment == "HKCUM":
            from fints.formals import KTI1

            _scheduled_segments()
            with c._get_dialog() as dialog:
                seg = globals()["HKCUM1"](account=KTI1.from_sepa_account(account), sepa_descriptor=desc,
                                          sepa_pain_message=xml.encode())
                return c._send_pay_with_possible_retry(dialog, seg, c._continue_sepa_transfer)
        if not scheduled:
            return c.sepa_transfer(account, xml, multiple=multiple, control_sum=Decimal(f"{order.total:.2f}"),
                                   pain_descriptor=desc)
        from fints.formals import KTI1, Amount1

        hkcse, hkcme = _scheduled_segments()
        seg_cls = hkcme if multiple else hkcse
        with c._get_dialog() as dialog:
            seg = seg_cls(account=KTI1.from_sepa_account(account), sepa_descriptor=desc,
                          sepa_pain_message=xml.encode())
            if multiple:
                seg.sum_amount = Amount1(amount=Decimal(f"{order.total:.2f}"), currency="EUR")
                seg.request_single_booking = False
            return c._send_pay_with_possible_retry(dialog, seg, c._continue_sepa_transfer)
    raise BankError(f"Unbekannter Schritt {kind}")


def _fail(job: Job, e: Exception) -> None:
    _close(job)
    job.done, job.ok, job.challenge = True, False, None
    job.message = explain(e)
    log.info("FinTS-Fehler: %s", e, exc_info=True)


def explain(e: Exception) -> str:
    name = type(e).__name__
    text = str(e)
    if name == "FinTSClientPINError":
        return ("Anmeldung abgelehnt – PIN oder Anmeldename falsch? Zur Sicherheit wird diese PIN nicht erneut "
                "gesendet (sonst droht die Sperre).")
    if name == "FinTSClientTemporaryAuthError":
        return "Zugang vorübergehend gesperrt (zu viele Fehlversuche) – bitte im Online-Banking entsperren."
    if name == "FinTSSCARequiredError":
        return "Die Bank verlangt eine starke Authentifizierung (TAN) – Vorgang bitte erneut starten."
    if name in ("FinTSConnectionError",) or "Connection" in name:
        return f"Bank nicht erreichbar ({text[:200]}). Server-Adresse prüfen."
    if "9010" in text or "BPD" in text:
        return "Bank lehnt die Verbindung ab – Bankleitzahl und FinTS-Server-Adresse prüfen."
    if isinstance(e, (BankError, sepa.SepaError, secretbox.SecretError)):
        return text
    if "product" in text.lower() or "9078" in text or "Produkt" in text:
        return "Produktregistrierung abgelehnt – FinTS-Produkt-ID prüfen (wird evtl. erst in einigen Tagen freigeschaltet)."
    return f"{name}: {text[:300]}"


def _persist(job: Job) -> None:
    """Ergebnis in die Datenbank übernehmen (Konten, Umsätze, Aufträge) und Zustand sichern."""
    with SessionLocal() as s:
        acc = s.get(BankAccess, job.access_id)
        if acc is None:
            return
        if (state := job.params.pop("state", None)) is not None:
            acc.state = secretbox.encrypt(state)
        if job.done and not job.params.get("persisted"):
            job.params["persisted"] = True
            if job.ok:
                try:
                    job.message = PROCESSORS[job.kind](s, acc, job)
                    _log(acc, f"{job.title}: {job.message}")
                except Exception as e:  # noqa: BLE001
                    s.rollback()
                    acc = s.get(BankAccess, job.access_id)
                    job.ok, job.message = False, f"Verarbeitung fehlgeschlagen: {explain(e)}"
                    _log(acc, f"{job.title}: {job.message}", error=True)
            else:
                _log(acc, f"{job.title}: {job.message}", error=True)
            job.pin = ""
        elif job.waiting:
            acc.last_msg = f"{job.title}: wartet auf Freigabe"
        s.commit()


# --------------------------------------------------------------------------- Ergebnisse verarbeiten
def _process_connect(s: Session, acc: BankAccess, job: Job) -> str:
    accounts = job.results[0] if job.results else []
    upd = {}
    c_upd = job.params.get("upd") or []
    for seg in c_upd:
        upd[seg["iban"]] = seg
    out = []
    old = {a.get("iban"): a for a in (acc.accounts or [])}
    for a in accounts:
        iban = sepa.normalize_iban(a.iban)
        u = upd.get(iban, {})
        entry = {"iban": iban, "bic": a.bic, "accountnumber": a.accountnumber, "subaccount": a.subaccount,
                 "blz": a.blz, "owner": u.get("owner", ""), "product": u.get("product", ""),
                 "type": u.get("type", ""), "ops": u.get("ops", [])}
        for k in ("balance", "balance_date"):
            if k in old.get(iban, {}):
                entry[k] = old[iban][k]
        out.append(entry)
    acc.accounts = out
    known = roles(s)
    hits = sum(1 for a in out if a["iban"] in known)
    return f"{len(out)} Konten gefunden" + (f", davon {hits} in der App hinterlegt" if hits else "")


def _soft_key(iban: str, day, amount: float, purpose: str) -> tuple:
    return (sepa.normalize_iban(iban), day, round(amount, 2), re.sub(r"[^A-Z0-9]", "", (purpose or "").upper())[:40])


def _drop_known(s: Session, txns: list) -> list:
    """Umsätze, die schon per Datei-Import da sind (andere camt-Version → anderer Fingerabdruck), auslassen."""
    if not txns:
        return txns
    since = min((t.booking_date for t in txns if t.booking_date), default=None)
    keys = set()
    q = s.query(FinTxn)
    if since:
        q = q.filter(FinTxn.booking_date >= since)
    keys |= {_soft_key(f.account_iban, f.booking_date, f.amount, f.purpose) for f in q}
    ibans = {}
    for b in s.query(Building):
        ibans[(b.id, "main")] = b.account_iban
        ibans[(b.id, "reserve")] = b.reserve_iban
    q = s.query(BankLine)
    if since:
        q = q.filter(BankLine.booking_date >= since)
    keys |= {_soft_key(ibans.get((l.building_id, l.account), ""), l.booking_date, l.amount, l.purpose) for l in q}
    digests = {d for (d,) in s.query(FinTxn.digest)} | {d for (d,) in s.query(BankLine.digest)}
    return [t for t in txns if t.digest in digests or _soft_key(t.account_iban, t.booking_date, t.amount, t.purpose)
            not in keys]


def ingest(s: Session, txns: list, bals: list) -> str:
    """Wie der Datei-Import: Finanzplanung (Hauptkonto) und Rücklagen-Abgleich je Gebäude."""
    from . import finance, reserves

    txns = _drop_known(s, txns)
    parts = []
    finance.seed(s)
    n, _ = finance.ingest(s, txns, bals)
    if n:
        parts.append(f"Finanzen: {n} neu")
        finance.budget_alerts(s)
    seen = {sepa.normalize_iban(t.account_iban) for t in txns} | {sepa.normalize_iban(b.account_iban) for b in bals}
    for bld in s.query(Building):
        ibans = {sepa.normalize_iban(bld.account_iban), sepa.normalize_iban(bld.reserve_iban)} - {""}
        if ibans & seen:
            m = reserves.ingest_bank(s, bld, txns, bals)
            parts.append(f"Rücklagen ({bld.label}): {m}")
    return " · ".join(parts) or "keine neuen Umsätze"


def _process_sync(s: Session, acc: BankAccess, job: Job) -> str:
    txns, bals = [], []
    by_iban = {a["iban"]: dict(a) for a in (acc.accounts or [])}  # Kopie, sonst erkennt SQLAlchemy die Änderung nicht
    for step, res in zip(job.steps, job.results):
        iban = step[1]["iban"] if len(step) > 1 else ""
        if step[0] == "fetch" and res:
            booked = res[0] if isinstance(res, (tuple, list)) else []
            for doc in booked or []:
                data = doc if isinstance(doc, bytes) else str(doc).encode()
                txns += bankimport.parse_camt(data, bals)
        if step[0] == "balance" and res is not None:
            amount = float(res.amount.amount)  # mt940 liefert das Vorzeichen schon (Soll „D“ = negativ, z. B. Darlehen)
            day = getattr(res, "date", None) or date.today()
            bals.append(bankimport.Balance(iban, day, "CLBD", amount))
            if iban in by_iban:
                by_iban[iban]["balance"], by_iban[iban]["balance_date"] = round(amount, 2), day.isoformat()
    acc.accounts = list(by_iban.values())
    acc.last_sync = datetime.now()
    for t in txns:  # Konto-IBAN fehlt in seltenen Fällen im camt → aus dem Abruf ergänzen
        if not t.account_iban and job.params.get("single_iban"):
            t.account_iban = job.params["single_iban"]
    msg = f"{len(txns)} Umsätze abgerufen"
    return msg + " · " + ingest(s, txns, bals)


def _process_order(s: Session, acc: BankAccess, job: Job) -> str:
    from fints.client import ResponseStatus

    notes, ok = [], True
    for res in job.results:
        status = getattr(res, "status", None)
        texts = [f"{r.code} {r.text}" for r in getattr(res, "responses", [])]
        if status == ResponseStatus.ERROR:
            ok = False
        notes += texts
    order: Order = job.params["order"]
    if not ok:
        job.ok = False
        raise BankError("Bank hat den Auftrag abgelehnt: " + "; ".join(notes)[:400])
    msg = f"{len(order.transfers)} Überweisung(en) über {_de(order.total)} € eingereicht"
    if order.exec_date > date.today():
        msg += f" (Ausführung am {order.exec_date:%d.%m.%Y})"
    if order.export_id and (ex := s.get(SepaExport, order.export_id)) is not None and ex.status != "done":
        from .db import ReserveEntry

        when = max(order.exec_date, date.today())
        ex.status, ex.confirmed_at, ex.confirmed_by = "done", datetime.now(), f"FinTS ({job.user or 'App'})"
        s.query(ReserveEntry).filter(ReserveEntry.export_id == ex.id).update({ReserveEntry.booking_date: when})
        msg += " · in den Rücklagen als ausgeführt bestätigt"
    if notes:
        msg += " · Bank: " + "; ".join(n for n in notes if n)[:300]
    return msg


PROCESSORS = {"connect": _process_connect, "sync": _process_sync, "order": _process_order}


def _de(x: float) -> str:
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


# --------------------------------------------------------------------------- Schritte zusammenstellen
def connect_steps() -> list:
    return [("accounts",)]


def read_upd(c) -> list[dict]:
    out = []
    try:
        for seg in c.upd.find_segments("HIUPD"):
            iban = sepa.normalize_iban(getattr(seg, "iban", "") or "")
            if not iban:
                continue
            ops = sorted({str(t.transaction) for t in (seg.allowed_transactions or []) if getattr(t, "transaction", None)})
            owner = " ".join(x for x in [getattr(seg, "name_account_owner_1", ""), getattr(seg, "name_account_owner_2", "")] if x)
            out.append({"iban": iban, "owner": owner, "product": str(getattr(seg, "account_product_name", "") or ""),
                        "type": account_type(getattr(seg, "account_type", None)), "ops": ops})
    except Exception:  # noqa: BLE001
        log.info("UPD nicht lesbar", exc_info=True)
    return out


def sync_steps(acc: BankAccess, ibans: Optional[set] = None, since: Optional[date] = None) -> list:
    steps = []
    start_default = (acc.last_sync.date() - timedelta(days=3)) if acc.last_sync else (
        acc.sync_from or date.today() - timedelta(days=FIRST_DAYS))
    for a in acc.accounts or []:
        if ibans and a["iban"] not in ibans:
            continue
        if allows(a, "HKCAZ") is not False:
            steps.append(("fetch", a, since or start_default, date.today()))
        if allows(a, "HKSAL") is not False:
            steps.append(("balance", a))
    return steps


def order_steps(acc: BankAccess, order: Order) -> list:
    a = find_account(acc, order.debtor.iban)
    if a is None:
        raise BankError(f"Konto {sepa.format_iban(order.debtor.iban)} gehört nicht zu diesem Bankzugang.")
    multiple = len(order.transfers) > 1
    scheduled = order.exec_date > date.today()
    need = ("HKCME" if multiple else "HKCSE") if scheduled else ("HKCCM" if multiple else "HKCCS")
    own = all(find_account(acc, t.creditor.iban) for t in order.transfers)
    if allows(a, need) is False and own and allows(a, "HKCUM") and not scheduled:
        # Sparkonto: keine Überweisung, aber Umbuchung aufs eigene Konto (je Überweisung eine Umbuchung)
        return [("order", a, Order(order.debtor, [t], order.exec_date, f"{order.msg_id[:30]}-{i}"[:35] if multiple
                                   else order.msg_id, order.label, order.export_id, "HKCUM"))
                for i, t in enumerate(order.transfers, 1)]
    if allows(a, need) is False:
        if scheduled:
            raise BankError(f"Die Bank erlaubt für dieses Konto keine Terminüberweisung per FinTS ({OPS[need]}). "
                            "„Sofort ausführen“ wählen oder am Ausführungstag einreichen.")
        if multiple and allows(a, "HKCCS") is not False:
            # Sammelauftrag nicht erlaubt → einzeln (je Überweisung eine TAN)
            return [("order", a, Order(order.debtor, [t], order.exec_date, f"{order.msg_id[:30]}-{i}"[:35],
                                       order.label, order.export_id)) for i, t in enumerate(order.transfers, 1)]
        raise BankError(f"Die Bank erlaubt für dieses Konto keine {OPS[need]} per FinTS – bitte im Online-Banking "
                        "ausführen (Konto-Einstellungen der Bank).")
    return [("order", a, order)]


# --------------------------------------------------------------------------- Hintergrund: täglicher Abruf
def auto_tick(now: Optional[datetime] = None) -> list[str]:
    """Täglicher Umsatzabruf für Zugänge mit gespeicherter PIN. Braucht die Bank eine pushTAN (alle ~90 Tage),
    wird bis zu 4 Minuten auf die Freigabe gewartet, sonst Hinweis per Push."""
    from . import notify

    now = now or datetime.now()
    out = []
    with SessionLocal() as s:
        due = [a.id for a in s.query(BankAccess).filter(BankAccess.auto_sync.is_(True))
               if a.pin and a.accounts and (a.last_auto is None or a.last_auto < now.date()) and now.hour >= (a.sync_hour or 7)]
    for aid in due:
        if (j := JOBS.get(aid)) is not None and not j.done:
            continue  # gerade ein manueller Vorgang
        with SessionLocal() as s:
            acc = s.get(BankAccess, aid)
            acc.last_auto = now.date()
            s.commit()
            pin = secretbox.maybe_decrypt(acc.pin or "")
            if not pin:
                continue
            try:
                job = start(s, acc, "sync", sync_steps(acc), pin, "Automatischer Abruf", "Automatik")
            except BankError as e:
                out.append(str(e))
                continue
        deadline = time.time() + 240
        while job.waiting and job.challenge_info().get("type") == "decoupled" and time.time() < deadline:
            time.sleep(POLL_SECONDS + 2)
            try:
                job = answer(aid, poll=True)
            except BankError:
                break
        if job.waiting:
            cancel(aid)
            with SessionLocal() as s:
                acc = s.get(BankAccess, aid)
                _log(acc, "Automatischer Abruf: Bank verlangt eine TAN-Freigabe – bitte einmal manuell abrufen.", True)
                s.commit()
                try:
                    notify.to_admins(s, "push_admin_errors", "Bankabruf braucht Freigabe",
                                     f"{acc.name}: bitte unter Finanzen → Bankzugang einmal „Umsätze abrufen“ und per TAN freigeben.",
                                     url="/admin/bank", tag=f"fints-{aid}")
                except Exception:  # noqa: BLE001
                    pass
            out.append(f"{aid}: TAN nötig")
        else:
            out.append(f"{aid}: {job.message}")
            if not job.ok:
                with SessionLocal() as s:
                    try:
                        notify.to_admins(s, "push_admin_errors", "Bankabruf fehlgeschlagen", job.message[:200],
                                         url="/admin/bank", tag=f"fints-{aid}")
                    except Exception:  # noqa: BLE001
                        pass
    return out


_auto_lock = threading.Lock()


def auto_tick_background() -> None:
    """Aus der Hintergrundschleife: eigener Thread (Warten auf pushTAN blockiert sonst die Erinnerungen)."""
    if _auto_lock.locked():
        return
    threading.Thread(target=_auto_run, daemon=True, name="fints-auto").start()


def _auto_run() -> None:
    if not _auto_lock.acquire(blocking=False):
        return
    try:
        auto_tick()
    except Exception:  # noqa: BLE001
        log.exception("FinTS-Automatik")
    finally:
        _auto_lock.release()


# --------------------------------------------------------------------------- Web
def _acc(s: Session, aid) -> BankAccess:
    try:
        acc = s.get(BankAccess, int(aid))
    except (TypeError, ValueError):
        acc = None
    if acc is None:
        raise HTTPException(404)
    return acc


def _user(request: Request) -> str:
    me = current(request)
    return me.display if me else ""


def _pin(acc: BankAccess, form) -> str:
    pin = str(form.get("pin", "") or "")
    return pin or (secretbox.maybe_decrypt(acc.pin or "") or "")


def _back(acc: Optional[BankAccess] = None) -> str:
    return "/admin/bank" + (f"#bank-{acc.id}" if acc else "")


def _job_redirect(acc: BankAccess, job: Job):
    if job.waiting:
        return _redirect(f"/admin/bank?job={acc.id}#vorgang", "Freigabe nötig")
    return _redirect(_back(acc), job.message or ("Erledigt" if job.ok else "Fehlgeschlagen"))


def open_exports(s: Session) -> list[dict]:
    """Noch nicht ausgeführte Aufträge der App, die über einen Bankzugang eingereicht werden können."""
    out = []
    q = scope.filter_query(s.query(SepaExport).filter(SepaExport.status == "open"), SepaExport.building_id)
    for ex in q.order_by(SepaExport.id.desc()):
        bld = s.get(Building, ex.building_id)
        if bld is None:
            continue
        debtor = bld.account_iban if ex.kind == "in" else bld.reserve_iban
        acc = access_for_iban(s, debtor)
        a = find_account(acc, debtor) if acc else None
        out.append({"ex": ex, "bld": bld, "access": acc, "debtor": debtor, "account": a,
                    "future": bool(ex.exec_date and ex.exec_date > date.today() and not ex.manual)})
    return out


@router.get("/admin/bank", response_class=HTMLResponse)
def bank_page(request: Request, job: int = 0, s: Session = Depends(get_session)):
    cleanup()
    accesses = s.query(BankAccess).order_by(BankAccess.id).all()
    jobs = {a.id: JOBS.get(a.id) for a in accesses}
    st = get_settings(s)
    own = [(a, x) for a in accesses for x in (a.accounts or [])]
    return _page(request, "bank.html", accesses=accesses, jobs=jobs, open_job=job, st=st,
                 product_env=bool(os.getenv("FINTS_PRODUCT_ID", "").strip()), product=product_id(s),
                 roles=roles(s), exports=open_exports(s), own=own, ops_names=OPS, fmt_iban=sepa.format_iban,
                 de=_de, today=date.today(), key_env=bool(os.getenv("APP_ENCRYPTION_KEY", "").strip()))


@router.post("/admin/bank/settings")
async def bank_settings(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    pid = re.sub(r"[^A-Za-z0-9]", "", str(form.get("fints_product_id", "")))[:30]
    save_settings(s, {"fints_product_id": pid})
    return _redirect("/admin/bank", "Produktregistrierung gespeichert" if pid else "Produktregistrierung entfernt")


@router.post("/admin/bank/access/{aid}")
async def access_save(request: Request, aid: int, s: Session = Depends(get_session)):
    form = await request.form()
    acc = BankAccess() if aid == 0 else _acc(s, aid)
    if form.get("delete"):
        cancel(acc.id)
        s.delete(acc)
        s.commit()
        return _redirect("/admin/bank", "Bankzugang gelöscht (PIN und Zustand entfernt).")
    blz = re.sub(r"\D", "", str(form.get("blz", "")))
    server = str(form.get("server", "")).strip()
    login = str(form.get("login", "")).strip()
    if len(blz) != 8 or not server.startswith("https://") or not login:
        return _redirect(_back(acc if aid else None), "Bitte BLZ (8 Ziffern), FinTS-Adresse (https://…) und Anmeldename angeben.")
    changed = (acc.blz, acc.server, acc.login, acc.customer_id) != (blz, server, login, str(form.get("customer_id", "")).strip())
    acc.name = str(form.get("name", "")).strip() or "Bank"
    acc.blz, acc.server, acc.login = blz, server, login
    acc.customer_id = str(form.get("customer_id", "")).strip()
    acc.auto_sync = bool(form.get("auto_sync"))
    try:
        acc.sync_hour = max(0, min(23, int(form.get("sync_hour") or 7)))
    except ValueError:
        acc.sync_hour = 7
    try:
        acc.sync_from = date.fromisoformat(str(form.get("sync_from"))) if form.get("sync_from") else None
    except ValueError:
        pass
    if form.get("tan_mechanism") is not None and str(form.get("tan_mechanism")) != (acc.tan_mechanism or ""):
        acc.tan_mechanism = str(form.get("tan_mechanism"))
    if form.get("tan_medium") is not None:
        acc.tan_medium = str(form.get("tan_medium")).strip()
    pin = str(form.get("pin", "") or "")
    if form.get("forget_pin"):
        acc.pin = ""
    elif pin and form.get("store_pin"):
        acc.pin = secretbox.encrypt(pin)
    if acc.auto_sync and not acc.pin:
        acc.auto_sync = False
    if changed and aid:
        acc.state, acc.accounts, acc.mechanisms = "", [], []
    if aid == 0:
        s.add(acc)
    s.commit()
    msg = "Gespeichert"
    if form.get("auto_sync") and not acc.auto_sync:
        msg += " – automatischer Abruf nur mit gespeicherter PIN"
    if form.get("connect") and (pin or acc.pin):
        try:
            job = start(s, acc, "connect", connect_steps(), _pin(acc, form), "Verbinden", _user(request))
        except BankError as e:
            return _redirect(_back(acc), str(e))
        return _job_redirect(acc, job)
    return _redirect(_back(acc), msg)


@router.post("/admin/bank/access/{aid}/connect")
async def access_connect(request: Request, aid: int, s: Session = Depends(get_session)):
    form = await request.form()
    acc = _acc(s, aid)
    try:
        job = start(s, acc, "connect", connect_steps(), _pin(acc, form), "Konten abrufen", _user(request))
    except BankError as e:
        return _redirect(_back(acc), str(e))
    return _job_redirect(acc, job)


@router.post("/admin/bank/access/{aid}/sync")
async def access_sync(request: Request, aid: int, s: Session = Depends(get_session)):
    form = await request.form()
    acc = _acc(s, aid)
    if not acc.accounts:
        return _redirect(_back(acc), "Erst „Verbinden / Konten abrufen“.")
    since = None
    if form.get("since"):
        try:
            since = date.fromisoformat(str(form.get("since")))
        except ValueError:
            pass
    ibans = {sepa.normalize_iban(x) for x in form.getlist("iban")} or None
    steps = sync_steps(acc, ibans, since)
    if not steps:
        return _redirect(_back(acc), "Die Bank erlaubt für diese Konten keinen Umsatzabruf per FinTS.")
    try:
        job = start(s, acc, "sync", steps, _pin(acc, form), "Umsätze abrufen", _user(request))
    except BankError as e:
        return _redirect(_back(acc), str(e))
    return _job_redirect(acc, job)


@router.post("/admin/bank/export/{eid}")
async def export_submit(request: Request, eid: int, s: Session = Depends(get_session)):
    """SEPA-Datei der App (Rücklagen/Gutschriften) direkt bei der Bank einreichen."""
    form = await request.form()
    ex = s.get(SepaExport, eid)
    if ex is None or not scope.can(ex.building_id):
        raise HTTPException(404)
    if ex.status == "done":
        return _redirect("/admin/bank#auftraege", "Schon als ausgeführt bestätigt.")
    try:
        order = order_for_export(s, ex)
        if form.get("now"):
            order.exec_date = date.today()
        acc = access_for_iban(s, order.debtor.iban)
        if acc is None:
            raise BankError("Kein Bankzugang mit dem Auftraggeberkonto – erst Bankzugang verbinden.")
        steps = order_steps(acc, order)
        job = start(s, acc, "order", steps, _pin(acc, form), f"Auftrag {order.label}", _user(request),
                    {"order": order})
    except (BankError, sepa.SepaError, ET.ParseError) as e:
        return _redirect("/admin/bank#auftraege", f"Nicht eingereicht: {e}")
    return _job_redirect(acc, job)


@router.post("/admin/bank/transfer")
async def transfer(request: Request, s: Session = Depends(get_session)):
    """Freier Übertrag/Überweisung von einem eigenen Konto (z. B. Giro → Aktivsparen)."""
    form = await request.form()
    src = sepa.normalize_iban(str(form.get("from", "")))
    acc = access_for_iban(s, src)
    back = "/admin/bank#uebertrag"
    if acc is None:
        return _redirect(back, "Auftraggeberkonto unbekannt.")
    a = find_account(acc, src)
    to = str(form.get("to", ""))
    if to and to != "other":
        dst = sepa.normalize_iban(to)
        dst_acc = next((x for b in s.query(BankAccess) for x in (b.accounts or []) if x["iban"] == dst), {})
        cred = sepa.Account(dst_acc.get("owner") or a.get("owner") or acc.name, dst, dst_acc.get("bic", ""))
    else:
        cred = sepa.Account(str(form.get("to_name", "")).strip(), str(form.get("to_iban", "")), str(form.get("to_bic", "")).strip())
    from .render import parse_float

    amount = parse_float(str(form.get("amount", "")), 0.0) or 0.0
    purpose = str(form.get("purpose", "")).strip() or "Uebertrag"
    when = date.today()
    if form.get("exec_date"):
        try:
            when = max(date.today(), date.fromisoformat(str(form.get("exec_date"))))
        except ValueError:
            pass
    if when > date.today() and not sepa.is_business_day(when):
        when = sepa.next_business_day(when)
    debtor = sepa.Account(a.get("owner") or acc.name, src, a.get("bic", ""))
    order = Order(debtor, [sepa.Transfer(round(amount, 2), purpose, f"IMVU{secrets.token_hex(6).upper()}", cred)], when,
                  label=f"Übertrag {_de(amount)} €")
    try:
        if not sepa.iban_ok(cred.iban):
            raise BankError("Empfänger-IBAN ungültig.")
        cred.check("Empfänger")
        if amount <= 0:
            raise BankError("Betrag muss positiv sein.")
        steps = order_steps(acc, order)
        job = start(s, acc, "order", steps, _pin(acc, form), order.label, _user(request), {"order": order})
    except (BankError, sepa.SepaError) as e:
        return _redirect(back, f"Nicht eingereicht: {e}")
    return _job_redirect(acc, job)


@router.post("/admin/bank/job/{aid}")
async def job_answer(request: Request, aid: int, s: Session = Depends(get_session)):
    form = await request.form()
    acc = _acc(s, aid)
    if form.get("cancel"):
        cancel(aid)
        with SessionLocal() as s2:
            a2 = s2.get(BankAccess, aid)
            _log(a2, "Vorgang abgebrochen", error=True)
            s2.commit()
        return _redirect(_back(acc), "Vorgang abgebrochen.")
    try:
        job = answer(aid, tan=str(form.get("tan", "")), vop_ok=bool(form.get("vop_ok")))
    except BankError as e:
        return _redirect(_back(acc), str(e))
    return _job_redirect(acc, job)


@router.get("/admin/bank/job/{aid}/poll")
def job_poll(aid: int):
    """pushTAN: Seite fragt regelmäßig, ob die Freigabe in der App erfolgt ist."""
    try:
        job = answer(aid, poll=True)
    except BankError as e:
        return JSONResponse({"state": "gone", "message": str(e)})
    if job.waiting:
        return JSONResponse({"state": "waiting", "info": job.challenge_info()})
    return JSONResponse({"state": "done" if job.ok else "error", "message": job.message})
