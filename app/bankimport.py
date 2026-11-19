"""Kontoauszüge einlesen: CAMT-XML (camt.052/053/054, alle Versionen, auch als ZIP) und CSV (Sparkasse CSV-CAMT/MT940).

Liefert einheitliche Umsätze (``Txn``) mit IBAN des eigenen Kontos – daran erkennt die App, ob der Auszug zum
Haupt- oder Rücklagenkonto gehört. Bei Sammelbuchungen entsteht je Einzelumsatz (TxDtls) ein Eintrag; fehlen die
Einzelheiten, ein Eintrag mit der Sammler-Referenz und der Gesamtsumme.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
from xml.etree import ElementTree as ET

MAX_BYTES = 20 * 1024 * 1024


class ImportError_(ValueError):
    pass


@dataclass
class Balance:
    account_iban: str
    day: Optional[date]
    kind: str  # OPBD = Anfangssaldo, CLBD = Endsaldo (gebucht)
    amount: float


@dataclass
class Txn:
    account_iban: str
    booking_date: Optional[date]
    amount: float  # + Gutschrift, − Lastschrift/Abbuchung
    purpose: str = ""
    name: str = ""
    iban: str = ""
    e2e: str = ""
    pmtinf: str = ""
    batch_count: int = 0
    info: str = ""
    ref: str = ""  # Bankreferenz (AcctSvcrRef/NtryRef) für Dubletten
    source: str = ""
    extra: dict = field(default_factory=dict)
    pending: bool = False  # vorgemerkt (noch nicht gebucht)

    @property
    def digest(self) -> str:
        key = "|".join([self.account_iban, str(self.booking_date), f"{self.amount:.2f}", self.e2e, self.pmtinf,
                        " ".join(self.purpose.split()), self.ref, self.name])
        return hashlib.sha256(key.encode()).hexdigest()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el, *path):
    """Kind-Element über lokale Namen (Namespace-unabhängig)."""
    for name in path:
        if el is None:
            return None
        el = next((c for c in el if _local(c.tag) == name), None)
    return el


def _children(el, name):
    return [c for c in (el if el is not None else []) if _local(c.tag) == name]


def _text(el, *path) -> str:
    el = _child(el, *path) if path else el
    return (el.text or "").strip() if el is not None and el.text else ""


def _date(el) -> Optional[date]:
    if el is None:
        return None
    raw = _text(el, "Dt") or _text(el, "DtTm") or (el.text or "").strip()
    try:
        return date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        return None


def _party_name(p) -> str:
    return _text(p, "Nm") or _text(p, "Pty", "Nm")


def _iban(acct) -> str:
    return _text(acct, "Id", "IBAN")


def parse_camt(data: bytes, balances: Optional[list] = None, include_pending: bool = False) -> list[Txn]:
    """camt einlesen; vorgemerkte Umsätze (Status PDNG) nur mit ``include_pending`` (FinTS-Abruf)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ImportError_(f"XML nicht lesbar: {e}")
    ns = root.tag.split("}")[0].strip("{") if root.tag.startswith("{") else ""
    source = ns.rsplit(":", 1)[-1] if ns else "camt"
    if not source.startswith("camt."):
        raise ImportError_("Keine CAMT-Datei (erwartet camt.052/053/054).")
    out: list[Txn] = []
    body = next(iter(root), None)
    for stmt in [c for c in (body if body is not None else []) if _local(c.tag) in ("Stmt", "Rpt", "Ntfctn")]:
        own = _iban(_child(stmt, "Acct"))
        for bal in _children(stmt, "Bal"):
            kind = _text(bal, "Tp", "CdOrPrtry", "Cd") or _text(bal, "Tp", "CdOrPrtry", "Prtry")
            amt = _child(bal, "Amt")
            if balances is not None and kind in ("OPBD", "CLBD", "PRCD", "CLAV") and amt is not None and amt.text:
                sign = -1 if _text(bal, "CdtDbtInd") == "DBIT" else 1
                d = _child(bal, "Dt")
                balances.append(Balance(own, _date(d) if d is not None else None, kind, round(float(amt.text) * sign, 2)))
        for ntry in _children(stmt, "Ntry"):
            sts = (_text(ntry, "Sts", "Cd") or _text(ntry, "Sts")).upper()
            pending = sts in ("PDNG", "INFO", "FUTR")
            if sts and sts != "BOOK" and not (include_pending and pending):
                continue  # nur gebuchte Umsätze
            sign = -1 if _text(ntry, "CdtDbtInd") == "DBIT" else 1
            total = float(_text(ntry, "Amt") or 0) * sign
            bdate = _date(_child(ntry, "BookgDt")) or _date(_child(ntry, "ValDt")) or (date.today() if pending else None)
            info = _text(ntry, "AddtlNtryInf")
            nref = _text(ntry, "AcctSvcrRef") or _text(ntry, "NtryRef")
            details = _children(ntry, "NtryDtls")
            txs = [tx for d in details for tx in _children(d, "TxDtls")]
            btch = next((b for d in details for b in _children(d, "Btch")), None)
            pmtinf = (_text(btch, "PmtInfId") or _text(btch, "MsgId")) if btch is not None else ""
            count = int(_text(btch, "NbOfTxs") or 0) if btch is not None else 0
            if len(txs) > 1:
                count = count or len(txs)
            if not txs:
                out.append(Txn(own, bdate, round(total, 2), info, "", "", "", pmtinf, count, info, nref, source,
                               pending=pending))
                continue
            for i, tx in enumerate(txs):
                amt_el = _child(tx, "Amt")
                if amt_el is None:
                    amt_el = _child(tx, "AmtDtls", "TxAmt", "Amt")
                amt = float(amt_el.text) * sign if amt_el is not None and amt_el.text else total
                if len(txs) == 1:
                    amt = total
                refs = _child(tx, "Refs")
                rp = _child(tx, "RltdPties")
                other = ("Dbtr", "DbtrAcct") if sign > 0 else ("Cdtr", "CdtrAcct")
                ustrd = " ".join(_text(u) for u in _children(_child(tx, "RmtInf"), "Ustrd"))
                e2e = _text(refs, "EndToEndId")
                out.append(Txn(
                    own, bdate, round(amt, 2), ustrd or info, _party_name(_child(rp, other[0])),
                    _iban(_child(rp, other[1])), "" if e2e == "NOTPROVIDED" else e2e,
                    pmtinf or _text(refs, "PmtInfId") or (_text(refs, "MsgId") if len(txs) > 1 else ""),
                    count if len(txs) > 1 else 0, info, f"{nref}#{i}" if nref else "", source, pending=pending))
    if not out and not balances:
        raise ImportError_("Keine gebuchten Umsätze in der Datei gefunden.")
    return out


def _norm(h: str) -> str:
    h = h.strip().strip('"').lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        h = h.replace(a, b)
    return re.sub(r"[^a-z0-9]", "", h)


def _de_amount(v: str) -> float:
    v = (v or "").strip().replace(" ", "")
    if "," in v:
        v = v.replace(".", "").replace(",", ".")
    return float(v or 0)


def _de_date(v: str) -> Optional[date]:
    v = (v or "").strip()
    for fmt in ("%d.%m.%y", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_csv(data: bytes) -> list[Txn]:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    first = text.splitlines()[0] if text.strip() else ""
    delim = ";" if first.count(";") >= first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if len(rows) < 2:
        raise ImportError_("CSV enthält keine Umsätze.")
    head = [_norm(h) for h in rows[0]]

    def col(*names):
        for n in names:
            if n in head:
                return head.index(n)
        return None

    c = {"acct": col("auftragskonto"), "date": col("buchungstag", "buchungsdatum"), "amt": col("betrag"),
         "purpose": col("verwendungszweck"), "name": col("beguenstigterzahlungspflichtiger", "name"),
         "iban": col("kontonummeriban", "kontonummer", "iban"), "e2e": col("kundenreferenzendtoend", "endtoendreferenz"),
         "batch": col("sammlerreferenz"), "info": col("info"), "text": col("buchungstext")}
    if c["amt"] is None or c["date"] is None:
        raise ImportError_("CSV-Spalten „Buchungstag“ und „Betrag“ nicht gefunden.")
    out = []
    for r in rows[1:]:
        if not any(x.strip() for x in r):
            continue
        g = lambda k: r[c[k]].strip() if c[k] is not None and c[k] < len(r) else ""  # noqa: E731
        info = g("info")
        if "vorgemerkt" in info.lower():
            continue  # nur gebuchte Umsätze
        e2e = g("e2e")
        out.append(Txn(re.sub(r"\s+", "", g("acct")).upper(), _de_date(g("date")), round(_de_amount(g("amt")), 2),
                       g("purpose"), g("name"), re.sub(r"\s+", "", g("iban")).upper(),
                       "" if e2e == "NOTPROVIDED" else e2e, g("batch"), 0, info or g("text"), "", "csv"))
    if not out:
        raise ImportError_("CSV enthält keine gebuchten Umsätze.")
    return out


def parse(filename: str, data: bytes, balances: Optional[list] = None) -> list[Txn]:
    """Datei (XML, ZIP mit XML-Dateien oder CSV) einlesen; Kontostände (nur CAMT) landen in ``balances``."""
    if len(data) > MAX_BYTES:
        raise ImportError_("Datei ist zu groß.")
    if data[:2] == b"PK":
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ImportError_("ZIP-Datei beschädigt.")
        out = []
        for info in zf.infolist():
            if info.is_dir() or info.file_size > MAX_BYTES:
                continue
            if info.filename.lower().endswith((".xml", ".csv")):
                out += parse(info.filename, zf.read(info), balances)
        if not out:
            raise ImportError_("ZIP enthält keine Kontoauszüge (XML/CSV).")
        return out
    head = data.lstrip()[:200]
    if head.startswith(b"<") or head.startswith(b"\xef\xbb\xbf<"):
        return parse_camt(data.lstrip(b"\xef\xbb\xbf \r\n\t"), balances)
    return parse_csv(data)


# --------------------------------------------------------------------------- vorgemerkte Umsätze
def _alnum(text: str, n: int) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())[:n]


def match_pending(rows: list, t: Txn):
    """Gespeicherten vorgemerkten Umsatz finden, der jetzt (gebucht) wiederkommt: gleicher Betrag und Zweck,
    sonst gleicher Betrag und Empfänger, sonst eindeutig gleicher Betrag innerhalb von 10 Tagen."""
    same = [r for r in rows if round(r.amount, 2) == round(t.amount, 2)]
    for key in (lambda x: _alnum(x.purpose, 24), lambda x: _alnum(x.name, 16)):
        k = key(t)
        if k:
            hit = next((r for r in same if key(r) == k), None)
            if hit is not None:
                return hit
    near = [r for r in same if not (r.booking_date and t.booking_date) or abs((r.booking_date - t.booking_date).days) <= 10]
    return near[0] if len(near) == 1 else None
