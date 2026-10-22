"""SEPA-Sammelüberweisung als XML (ISO 20022 pain.001) für die Datei-Übergabe im Online-Banking.

Vorgaben (z. B. Sparkasse „Datei-Übergabe“): genau eine logische Datei (ein Dokument, ein Sammler), Dateiname nur aus
A–Z, a–z, 0–9 und ``+ - _``, optionales Ausführungsdatum. Zeichensatz nach DK-Spezifikation (SEPA-Basiszeichensatz plus
deutsche Umlaute); Namen bleiben möglichst unverändert, damit die Empfängerprüfung (IBAN ↔ Name) passt.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

FORMATS = {
    "pain.001.001.09": "pain.001.001.09 (aktueller Standard, empfohlen)",
    "pain.001.001.03": "pain.001.001.03 (ältere Banksoftware)",
}
ALLOWED = re.compile(r"[^A-Za-z0-9 /\-?:().,'+ÄÖÜäöüß&*$%]")
TRANSLIT = {"€": "EUR", "„": "", "“": "", "”": "", "‚": "", "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...",
            "·": "-", "×": "x", "@": "(at)", "_": "-", "#": "Nr.", "\n": " ", "\t": " ", '"': "", ";": ",", "!": "."}
UMLAUTS = {"Ä": "AE", "Ö": "OE", "Ü": "UE", "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


class SepaError(ValueError):
    pass


def clean(text: str, max_len: int = 140, umlauts: bool = True) -> str:
    """Auf den SEPA-Zeichensatz (DK) bringen. ``umlauts=False`` schreibt Ä→AE usw. (für den Verwendungszweck)."""
    text = "".join(TRANSLIT.get(c, c) for c in str(text or ""))
    if not umlauts:
        text = "".join(UMLAUTS.get(c, c) for c in text)
    out = []
    for c in text:
        if ALLOWED.match(c):  # nicht erlaubt → Akzente entfernen (é → e), sonst weglassen
            base = unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode()
            c = base if base and not ALLOWED.search(base) else ""
        out.append(c)
    return " ".join("".join(out).split())[:max_len]


def normalize_iban(iban: str) -> str:
    return re.sub(r"\s+", "", str(iban or "")).upper()


def iban_ok(iban: str) -> bool:
    iban = normalize_iban(iban)
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban):
        return False
    if iban.startswith("DE") and len(iban) != 22:
        return False
    num = "".join(str(int(c, 36)) for c in iban[4:] + iban[:4])
    return int(num) % 97 == 1


def bic_ok(bic: str) -> bool:
    return not bic or bool(re.fullmatch(r"[A-Z]{6}[A-Z0-9]{2}([A-Z0-9]{3})?", bic.strip().upper()))


def format_iban(iban: str) -> str:
    iban = normalize_iban(iban)
    return " ".join(iban[i:i + 4] for i in range(0, len(iban), 4))


def safe_filename(stem: str) -> str:
    """Nur A–Z, a–z, 0–9, + - _ (Vorgabe der Datei-Übergabe), Endung .xml."""
    stem = "".join(UMLAUTS.get(c, c) for c in stem)
    stem = re.sub(r"[^A-Za-z0-9+_-]+", "_", stem).strip("_") or "SEPA"
    return stem[:80] + ".xml"


def _holidays(year: int) -> set:
    """TARGET2-Feiertage (an diesen Tagen keine SEPA-Ausführung)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month, day = divmod(h + l_ - 7 * m + 114, 31)
    easter = date(year, month, day + 1)
    return {date(year, 1, 1), easter - timedelta(days=2), easter + timedelta(days=1), date(year, 5, 1),
            date(year, 12, 25), date(year, 12, 26)}


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _holidays(d.year)


def next_business_day(d: Optional[date] = None, min_days: int = 1) -> date:
    d = (d or date.today()) + timedelta(days=min_days)
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


@dataclass
class Account:
    name: str
    iban: str
    bic: str = ""

    def check(self, role: str) -> None:
        if not clean(self.name, 70):
            raise SepaError(f"{role}: Kontoinhaber fehlt.")
        if not iban_ok(self.iban):
            raise SepaError(f"{role}: IBAN „{self.iban}“ ist ungültig (Prüfziffer).")
        if not bic_ok(self.bic):
            raise SepaError(f"{role}: BIC „{self.bic}“ ist ungültig.")


@dataclass
class Transfer:
    amount: float
    purpose: str
    end_to_end: str = ""
    creditor: Optional[Account] = None  # Standard: Empfänger des Auftrags


def _sub(parent, tag, text=None, **attrib):
    el = ET.SubElement(parent, tag, attrib)
    if text is not None:
        el.text = text
    return el


def _agent(parent, tag: str, bic: str, fmt: str, required: bool) -> None:
    bic = (bic or "").strip().upper()
    if not bic and not required:
        return
    fin = _sub(_sub(parent, tag), "FinInstnId")
    if bic:
        _sub(fin, "BICFI" if fmt.endswith(".09") else "BIC", bic)
    else:
        _sub(_sub(fin, "Othr"), "Id", "NOTPROVIDED")


def credit_transfer(debtor: Account, creditor: Account, transfers: list[Transfer], exec_date: date,
                    fmt: str = "pain.001.001.09", msg_id: str = "", batch: bool = True) -> tuple[str, bytes]:
    """SEPA-Sammelüberweisung (ein Sammler) → (MsgId, XML-Bytes)."""
    if fmt not in FORMATS:
        raise SepaError(f"Unbekanntes Format {fmt}")
    debtor.check("Auftraggeber (Girokonto)")
    creditor.check("Empfänger")
    if not transfers:
        raise SepaError("Keine Überweisungen ausgewählt.")
    cents = []
    for t in transfers:
        c = round(t.amount * 100)
        if c <= 0:
            raise SepaError(f"Betrag muss positiv sein ({t.purpose}).")
        if c > 99999999999:
            raise SepaError("Betrag zu hoch.")
        cents.append(c)
    total = sum(cents)
    msg_id = clean(msg_id or f"IMV{datetime.now():%Y%m%d%H%M%S}{secrets.token_hex(3).upper()}", 35, umlauts=False)
    amount = lambda c: f"{c // 100}.{c % 100:02d}"  # noqa: E731

    ns = f"urn:iso:std:iso:20022:tech:xsd:{fmt}"
    doc = ET.Element("Document", {"xmlns": ns, "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance"})
    root = _sub(doc, "CstmrCdtTrfInitn")
    hdr = _sub(root, "GrpHdr")
    _sub(hdr, "MsgId", msg_id)
    _sub(hdr, "CreDtTm", datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    _sub(hdr, "NbOfTxs", str(len(transfers)))
    _sub(hdr, "CtrlSum", amount(total))
    _sub(_sub(hdr, "InitgPty"), "Nm", clean(debtor.name, 70))

    pmt = _sub(root, "PmtInf")
    _sub(pmt, "PmtInfId", clean(f"{msg_id}-1", 35, umlauts=False))
    _sub(pmt, "PmtMtd", "TRF")
    _sub(pmt, "BtchBookg", "true" if batch else "false")
    _sub(pmt, "NbOfTxs", str(len(transfers)))
    _sub(pmt, "CtrlSum", amount(total))
    _sub(_sub(_sub(pmt, "PmtTpInf"), "SvcLvl"), "Cd", "SEPA")
    if fmt.endswith(".09"):
        _sub(_sub(pmt, "ReqdExctnDt"), "Dt", exec_date.isoformat())
    else:
        _sub(pmt, "ReqdExctnDt", exec_date.isoformat())
    _sub(_sub(pmt, "Dbtr"), "Nm", clean(debtor.name, 70))
    _sub(_sub(_sub(pmt, "DbtrAcct"), "Id"), "IBAN", normalize_iban(debtor.iban))
    _agent(pmt, "DbtrAgt", debtor.bic, fmt, required=True)
    _sub(pmt, "ChrgBr", "SLEV")
    for i, (t, c) in enumerate(zip(transfers, cents), 1):
        cred = t.creditor or creditor
        tx = _sub(pmt, "CdtTrfTxInf")
        _sub(_sub(tx, "PmtId"), "EndToEndId", clean(t.end_to_end or f"{msg_id}-{i}", 35, umlauts=False) or "NOTPROVIDED")
        _sub(_sub(tx, "Amt"), "InstdAmt", amount(c), Ccy="EUR")
        _agent(tx, "CdtrAgt", cred.bic, fmt, required=False)
        _sub(_sub(tx, "Cdtr"), "Nm", clean(cred.name, 70))
        _sub(_sub(_sub(tx, "CdtrAcct"), "Id"), "IBAN", normalize_iban(cred.iban))
        _sub(_sub(tx, "RmtInf"), "Ustrd", clean(t.purpose, 140, umlauts=False))
    ET.indent(doc)
    return msg_id, b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(doc, encoding="utf-8", xml_declaration=False)
