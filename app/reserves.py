"""Rücklagen für zeitversetzt bezahlte Kosten (z. B. Wassergeld, IPTV, Kaminkehrer) und feste Rücklagen außerhalb
der Abrechnung (z. B. Grundsteuer, Wohngebäudeversicherung).

Die Mieter zahlen diese Posten monatlich mit der Nebenkostenabrechnung, die Rechnung des Versorgers kommt aber erst
später. Je abgeschlossener Abrechnung, Partei und Position wird der Betrag per SEPA-Sammelüberweisung (pain.001) vom
Girokonto aufs Rücklagenkonto übertragen; das Rücklagenbuch zeigt den Stand je Position (Zuführungen − Entnahmen).
"""

from __future__ import annotations

import re
import secrets
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from . import bankimport, render as rnd, scope, sepa
from .accounts import _page, _redirect, current
from .db import (Allocation, BankLine, Billing, Building, FixedCost, Party, ReserveEntry, ReservePlan, SepaExport,
                 get_session)

router = APIRouter()
INTERVALS = {1: "monatlich", 3: "vierteljährlich", 6: "halbjährlich", 12: "jährlich"}
KINDS = {"in": "Zuführung", "out": "Auszahlung / Entnahme", "start": "Anfangsbestand"}


@dataclass
class Position:
    ref: str
    name: str
    label: str  # Zweck im Verwendungszweck (Großbuchstaben)
    groups: list = None  # Stichwort-Gruppen zum Erkennen im Kontoauszug (alle Wörter einer Gruppe müssen vorkommen)


STOP = {"RUECKBILDUNG", "RUECKBILDUNGEN", "RUECKLAGE", "RUECKLAGEN", "GUTSCHRIFT", "AUS", "UND", "DER", "DIE", "DAS",
        "FUER", "MIT", "VON", "BEITRAG", "ABO", "EUR", "DATUM", "UHR", "ALLE", "MONATE", "JAHR", "JAEHRLICH"}


def tokens(text: str) -> list[str]:
    """Bedeutungstragende Wörter (Großbuchstaben, Umlaute ausgeschrieben) – ohne Zyklus-/Datumsangaben."""
    t = sepa.clean(text or "", 500, umlauts=False).upper()
    t = re.sub(r"ZYKLUS.*$", "", t)
    t = re.sub(r"DATUM \d.*$", "", t)
    words = re.findall(r"[A-Z][A-Z0-9]*|\d*[A-Z]+\d+[A-Z0-9]*", t)
    return [w for w in words if len(w) >= 3 and w not in STOP]


def keyword_groups(*texts: str, explicit: str = "") -> list[list[str]]:
    """Stichwort-Gruppen: ausdrücklich gepflegte (Komma-getrennt) oder automatisch aus Name/Zweck – jeweils die
    vollständige Wortliste und ein kurzer Kern (lange Wörter auf 7 Zeichen gekürzt, damit Tippfehler/Varianten
    wie „WOHNGEBAEDE…“ und „WOHNGEBAEUDE…“ beide passen)."""
    if explicit.strip():
        return [g for g in (tokens(x) for x in explicit.split(",")) if g]
    groups = []
    for text in texts:
        words = tokens(text)
        if not words:
            continue
        groups.append(words)
        head = words[0]
        core = [head[:7]] if len(head) >= 10 else words[:2]
        if core != words:
            groups.append(core)
        for w in words[1:]:  # lange, eindeutige Wörter reichen allein (z. B. COOKIDOO); spezifischere Gruppen gewinnen
            if len(w) >= 8 and [w] not in groups:
                groups.append([w[:7]] if len(w) >= 10 else [w])
    return groups


def keyword_score(groups: list, purpose: str) -> int:
    """Anzahl Wörter der am besten passenden Gruppe (0 = passt nicht)."""
    squashed = re.sub(r"[^A-Z0-9]", "", sepa.clean(purpose or "", 500, umlauts=False).upper())
    best = 0
    for g in groups or []:
        if g and all(w in squashed for w in g):
            best = max(best, sum(len(w) for w in g))
    return best


@dataclass
class Item:
    key: str
    billing: Optional[Billing]  # None bei festen Rücklagen
    party: Optional[dict]
    pos: Position
    amount: float
    purpose: str
    month: Optional[date] = None
    period_text: str = ""

    @property
    def end_to_end(self) -> str:
        if self.billing is not None:
            return f"RL{self.billing.id}P{self.party['id']}{self.pos.ref.replace(':', '')}"
        return f"RL{self.pos.ref.replace(':', '').upper()}M{self.month:%Y%m}"


def plans(s: Session, building: Optional[Building] = None, active_only: bool = False) -> list[ReservePlan]:
    q = s.query(ReservePlan)
    if building is not None:
        q = q.filter(ReservePlan.building_id == building.id)
    if active_only:
        q = q.filter(ReservePlan.active.is_(True))
    return q.order_by(ReservePlan.name, ReservePlan.id).all()


def positions(s: Session, building: Optional[Building] = None) -> dict[str, Position]:
    out = {}
    for f in s.query(FixedCost).filter(FixedCost.reserve.is_(True)).order_by(FixedCost.id):
        out[f"fc:{f.id}"] = Position(f"fc:{f.id}", f.name, (f.reserve_label or f.name).upper(),
                                     keyword_groups(f.reserve_label, f.name))
    for a in s.query(Allocation).filter(Allocation.reserve.is_(True)).order_by(Allocation.id):
        out[f"al:{a.id}"] = Position(f"al:{a.id}", a.name, (a.reserve_label or a.name).upper(),
                                     keyword_groups(a.reserve_label, a.name))
    for p in plans(s, building, active_only=True) if building is not None else []:
        out[f"rp:{p.id}"] = Position(f"rp:{p.id}", p.name, (p.label or p.name).upper(),
                                     keyword_groups(p.label, p.name, explicit=p.keywords or ""))
    return out


def all_positions(s: Session) -> dict[str, str]:
    """Namen aller Positionen (auch ohne Rücklage) – für Buchungen, deren Position später abgeschaltet wurde."""
    names = {f"fc:{f.id}": f.name for f in s.query(FixedCost)}
    names.update({f"al:{a.id}": a.name for a in s.query(Allocation)})
    names.update({f"rp:{p.id}": p.name for p in s.query(ReservePlan)})
    return names


def gid(code: str) -> str:
    return re.sub(r"^GID[\s:._-]*", "", (code or "").strip(), flags=re.I) or (code or "").strip()


def _de(x: float) -> str:
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def purpose(b: Billing, party: dict, pos: Position, amount: float, building: Optional[Building]) -> str:
    """z. B. RUECKLAGE AUS NEBENKOSTENABRECHNUNG 09/2026 MIETER FAMILIE ZWEIG GID: 134 WE-1 WASSERGELD 30,34 EUR"""
    role = "EIGENTUEMER" if party.get("is_owner") else "MIETER"
    head = f"RUECKLAGE AUS NEBENKOSTENABRECHNUNG {b.period_start:%m/%Y} {role}"
    tail = " ".join(x for x in (f"GID: {gid(building.code)}" if building and building.code else "",
                                party.get("unit_id") or "", pos.label, f"{_de(amount)} EUR") if x)
    name = sepa.clean(party.get("name", ""), 140, umlauts=False)
    room = 140 - len(head) - len(tail) - 2
    return sepa.clean(f"{head} {name[:max(room, 0)]} {tail}".upper(), 140, umlauts=False)


def _start(building: Building) -> Optional[date]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", building.reserve_start or "")
    return date(int(m[1]), int(m[2]), 1) if m else None


def _month(value: str) -> Optional[date]:
    m = re.fullmatch(r"(\d{4})-(\d{2})", value or "")
    return date(int(m[1]), int(m[2]), 1) if m and 1 <= int(m[2]) <= 12 else None


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def plan_purpose(p: ReservePlan, month: date, amount: float, building: Optional[Building]) -> str:
    """z. B. RUECKLAGE GRUNDSTEUER 11/2026 GID: 134 41,50 EUR"""
    span = f"{month:%m/%Y}" if (p.interval or 1) == 1 else \
        f"{month:%m/%Y}-{_add_months(month, (p.interval or 1) - 1):%m/%Y}"
    code = f"GID: {gid(building.code)} " if building and building.code and p.category != "privat" else ""
    return sepa.clean(f"RUECKLAGE {(p.label or p.name).upper()} {span} {code}{_de(amount)} EUR", 140, umlauts=False)


def plan_pending(s: Session, building: Building, today: Optional[date] = None) -> list[Item]:
    """Fällige Zuführungen fester Rücklagen: je Intervall ab Startmonat bis einschließlich des laufenden Monats."""
    now = (today or date.today()).replace(day=1)
    items = []
    for p in plans(s, building, active_only=True):
        start = _month(p.start) or p.created_at.date().replace(day=1)
        if p.rate < 0.01 or p.funding in ("dauerauftrag", "manuell"):  # Bank bzw. nur von Hand → keine SEPA-Zuführung
            continue
        ref = f"rp:{p.id}"
        done = {e.period for e in s.query(ReserveEntry).filter(ReserveEntry.ref == ref, ReserveEntry.kind == "in")}
        pos = Position(ref, p.name, (p.label or p.name).upper())
        m = start
        while m <= now:
            if m.strftime("%Y-%m") not in done:
                items.append(Item(f"{ref}:{m:%Y-%m}", None, None, pos, p.rate, plan_purpose(p, m, p.rate, building),
                                  month=m, period_text=f"{m:%m/%Y}"))
            m = _add_months(m, p.interval or 1)
    return items


def pending(s: Session, building: Building) -> list[Item]:
    """Offene Zuführungen: aus Abrechnungen (je Abrechnung × Partei × Position) und aus festen Rücklagen."""
    return billing_pending(s, building) + plan_pending(s, building)


def billing_pending(s: Session, building: Building) -> list[Item]:
    pos = positions(s)
    if not pos:
        return []
    q = s.query(Billing).filter(Billing.status == "final", Billing.building_id == building.id)
    if (start := _start(building)) is not None:
        q = q.filter(Billing.period_start >= start)
    bills = q.order_by(Billing.period_start, Billing.id).all()
    booked: dict[tuple, float] = {}
    if bills:
        for e in s.query(ReserveEntry).filter(ReserveEntry.kind == "in",
                                              ReserveEntry.billing_id.in_([b.id for b in bills])):
            k = (e.billing_id, e.party_id, e.ref)
            booked[k] = booked.get(k, 0.0) + e.amount
    items = []
    for b in bills:
        for rp in (b.result or {}).get("parties", []):
            if rp.get("is_owner") and not building.reserve_owner:
                continue
            for ref, p in pos.items():
                amt = sum(float(l.get("amount") or 0) for l in rp.get("lines", [])
                          if l.get("ref") == ref or (not l.get("ref") and l.get("group") in ("fix", "umlage")
                                                     and l.get("label") == p.name))
                amt = round(amt - booked.get((b.id, rp["id"], ref), 0.0), 2)
                if amt >= 0.01:
                    items.append(Item(f"{b.id}:{rp['id']}:{ref}", b, rp, p, amt, purpose(b, rp, p, amt, building),
                                      month=b.period_start, period_text=f"{b.period_start:%m/%Y}"))
    return items


def open_exports(s: Session, building: Building) -> set[int]:
    return {x.id for x in s.query(SepaExport.id).filter(SepaExport.building_id == building.id,
                                                          SepaExport.status != "done")}


def balances(s: Session, building: Building) -> list[dict]:
    """Virtuelles Rücklagenkonto je Position: zählt nur bestätigte Buchungen (Datei ausgeführt bzw. manuell gebucht);
    Buchungen aus noch nicht bestätigten SEPA-Dateien stehen als „vorgemerkt“ daneben."""
    names = all_positions(s)
    pending_ids = open_exports(s, building)
    rows: dict[str, dict] = {}
    blank = lambda ref, name: {"ref": ref, "name": name, "in": 0.0, "out": 0.0, "total": 0.0,  # noqa: E731
                               "planned_in": 0.0, "planned_out": 0.0}
    for e in s.query(ReserveEntry).filter(ReserveEntry.building_id == building.id):
        r = rows.setdefault(e.ref, blank(e.ref, names.get(e.ref, e.name)))
        if e.export_id in pending_ids:
            r["planned_out" if e.amount < 0 else "planned_in"] += e.amount
        else:
            r["out" if e.amount < 0 else "in"] += e.amount
            r["total"] += e.amount
    for ref, p in positions(s, building).items():
        rows.setdefault(ref, blank(ref, p.name))
    for r in rows.values():
        for k in ("in", "out", "total", "planned_in", "planned_out"):
            r[k] = round(r[k], 2)
    return sorted(rows.values(), key=lambda r: r["name"].lower())


def plan_stats(s: Session, building: Building, bal: list[dict]) -> list[dict]:
    """Je Topf: Soll je Monat (Jahresbetrag/12), aktueller Dauerauftrag, Ist der letzten 12 Monate, Stand, Hinweis."""
    stand = {r["ref"]: r["total"] for r in bal}
    year_ago = date.fromordinal(date.today().toordinal() - 365)
    rows = []
    for p in plans(s, building):
        ref = f"rp:{p.id}"
        ins = s.query(ReserveEntry).filter(ReserveEntry.ref == ref, ReserveEntry.amount > 0,
                                           ReserveEntry.kind == "in").all()
        ist = round(sum(e.amount for e in ins if e.booking_date and e.booking_date >= year_ago) / 12, 2)
        outs = sum(-e.amount for e in s.query(ReserveEntry).filter(ReserveEntry.ref == ref, ReserveEntry.amount < 0)
                   if e.booking_date and e.booking_date >= year_ago)
        soll = round((p.annual_amount or 0) / 12, 2)
        hint = ""
        if p.funding == "dauerauftrag" and p.active and soll and abs((p.standing_order or 0) - soll) >= 0.5:
            hint = f"Dauerauftrag von {_de(p.standing_order or 0)} € auf {_de(soll)} € {'erhöhen' if soll > (p.standing_order or 0) else 'senken'}"
        rows.append({"plan": p, "soll": soll, "ist": ist, "out12": round(outs, 2), "stand": stand.get(ref, 0.0),
                     "hint": hint})
    return rows


def default_exec_date(items: list[Item]) -> date:
    """Ausführung frühestens am nächsten Bankarbeitstag, sonst zur spätesten Fälligkeit der Mieterzahlungen."""
    earliest = sepa.next_business_day()
    due = max((rnd.due_date(i.billing) for i in items if i.billing is not None), default=earliest)
    while not sepa.is_business_day(due):
        due = date.fromordinal(due.toordinal() + 1)
    return max(earliest, due)


def accounts(building: Building) -> tuple[sepa.Account, sepa.Account]:
    return (sepa.Account(building.account_name, building.account_iban, building.account_bic),
            sepa.Account(building.reserve_name, building.reserve_iban, building.reserve_bic))


# --------------------------------------------------------------------------- Seiten
def _building(s: Session, bid) -> Building:
    blds = scope.filter_query(s.query(Building), Building.id).order_by(Building.sort, Building.id).all()
    if not blds:
        raise HTTPException(404, "Kein Gebäude")
    try:
        bid = int(bid or 0)
    except (TypeError, ValueError):
        bid = 0
    return next((b for b in blds if b.id == bid), blds[0])


@router.get("/admin/reserves", response_class=HTMLResponse)
def reserves_page(request: Request, building: int = 0, dl: int = 0, plan: int = 0, s: Session = Depends(get_session)):
    bld = _building(s, building)
    lines = open_lines(s, bld)
    cands = {l.id: candidates(s, l) for l in lines}
    sure = sum(1 for l in lines if cands[l.id] and cands[l.id][0]["score"] >= SURE)
    has_bank = s.query(BankLine.id).filter(BankLine.building_id == bld.id).first() is not None
    missing = [e for e in s.query(ReserveEntry).filter(ReserveEntry.building_id == bld.id,
                                                         ReserveEntry.export_id.isnot(None))
               .order_by(ReserveEntry.booking_date, ReserveEntry.id)
               if not (e.bank_line_id or e.main_line_id)] if has_bank else []
    matched_lines = [l for l in s.query(BankLine).filter(BankLine.building_id == bld.id)
                     .order_by(BankLine.booking_date.desc(), BankLine.id.desc()).limit(600) if l.entry_ids or l.ignored]
    entry_refs = {e.id: e.ref for e in s.query(ReserveEntry).filter(ReserveEntry.building_id == bld.id)}
    edit_plan = s.get(ReservePlan, plan) if plan else None
    if edit_plan is not None and edit_plan.building_id != bld.id:
        edit_plan = None
    items = pending(s, bld)
    names = all_positions(s)
    entries = s.query(ReserveEntry).filter(ReserveEntry.building_id == bld.id) \
        .order_by(ReserveEntry.booking_date.desc(), ReserveEntry.id.desc()).limit(1000).all()
    exports = s.query(SepaExport).filter(SepaExport.building_id == bld.id).order_by(SepaExport.id.desc()).limit(50).all()
    debtor, creditor = accounts(bld)
    problems = []
    for acc, role in ((debtor, "Girokonto"), (creditor, "Rücklagenkonto")):
        try:
            acc.check(role)
        except sepa.SepaError as e:
            problems.append(str(e))
    if not problems and sepa.normalize_iban(debtor.iban) == sepa.normalize_iban(creditor.iban):
        problems.append("Girokonto und Rücklagenkonto sind identisch.")
    parties = {p.id: p for p in s.query(Party)}
    open_ids = open_exports(s, bld)
    bal = balances(s, bld)
    pots_total = round(sum(r["total"] for r in bal), 2)
    free = round(bld.bank_reserve_balance - pots_total, 2) if bld.bank_reserve_balance is not None else None
    refunds = open_refunds(s, bld) if scope.is_super() else []
    cycle = cycle_status(s, bld)
    dl_export = s.get(SepaExport, dl) if dl else None
    if dl_export is not None and dl_export.building_id != bld.id:
        dl_export = None
    return _page(request, "reserves.html", refunds=refunds, is_super=scope.is_super(), cycle=cycle, dl_export=dl_export, refunds_total=round(sum(r["amount"] for r in refunds
                 if not r["probable"]), 2), next_month=_add_months(date.today().replace(day=1), 1).strftime("%Y-%m"),
                 open_ids=open_ids, pick=PICK, surescore=SURE, detected=detect_standing_orders(s, bld),
                 plan_rows=plan_stats(s, bld, bal), pots_total=pots_total, free=free, lines=lines, cands=cands, sure=sure, missing=missing,
                 matched_lines=matched_lines, entry_refs=entry_refs, edit_plan=edit_plan, bld=bld, buildings=scope.filter_query(s.query(Building), Building.id)
                 .order_by(Building.sort, Building.id).all(), items=items, total=round(sum(i.amount for i in items), 2),
                 positions=positions(s, bld), names=names, plans=plans(s, bld), intervals=INTERVALS, balances=bal, entries=entries, exports=exports,
                 exec_date=default_exec_date(items), problems=problems, formats=sepa.FORMATS, kinds=KINDS,
                 parties=parties, dl=dl, fmt_iban=sepa.format_iban, today=date.today())


@router.post("/admin/reserves/accounts")
async def save_accounts(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    bld = _building(s, form.get("building"))
    errors = []
    for prefix in ("account", "reserve"):
        name = " ".join(str(form.get(f"{prefix}_name", "")).split())[:70]
        iban = sepa.normalize_iban(str(form.get(f"{prefix}_iban", "")))
        bic = str(form.get(f"{prefix}_bic", "")).strip().upper().replace(" ", "")
        if iban and not sepa.iban_ok(iban):
            errors.append(f"IBAN {iban} ungültig")
        if not sepa.bic_ok(bic):
            errors.append(f"BIC {bic} ungültig")
        setattr(bld, f"{prefix}_name", name)
        setattr(bld, f"{prefix}_iban", iban)
        setattr(bld, f"{prefix}_bic", bic)
    start = str(form.get("reserve_start", "")).strip()
    bld.reserve_start = start if re.fullmatch(r"\d{4}-\d{2}", start) else ""
    bld.reserve_owner = bool(form.get("reserve_owner"))
    bld.reserve_no_file = bool(form.get("reserve_no_file"))
    bld.sepa_mail = bool(form.get("sepa_mail"))
    bld.sepa_mail_to = ", ".join(a.strip() for a in re.split(r"[,;\s]+", str(form.get("sepa_mail_to", ""))) if "@" in a)[:300]
    day = str(form.get("sepa_mail_day", "25")).strip()
    bld.sepa_mail_day = min(max(int(day), 1), 28) if day.isdigit() else 25
    fmt = str(form.get("sepa_format", ""))
    bld.sepa_format = fmt if fmt in sepa.FORMATS else "pain.001.001.09"
    s.commit()
    msg = "Gespeichert" + (" – bitte prüfen: " + "; ".join(errors) if errors else "")
    return _redirect(f"/admin/reserves?building={bld.id}", msg)


def _store_export(s: Session, request: Optional[Request], bld: Building, kind: str, xml: bytes, msg_id: str,
                  exec_date: date, count: int, total: float, created_by: str = "") -> SepaExport:
    code = re.sub(r"[^A-Za-z0-9]+", "", gid(bld.code)) or str(bld.id)
    me = current(request) if request is not None else None
    ex = SepaExport(building_id=bld.id, kind=kind, msg_id=msg_id, exec_date=exec_date, count=count,
                    total=round(total, 2), xml=xml.decode("utf-8"), created_by=me.display if me else created_by)
    s.add(ex)
    s.flush()
    stem = ("Ruecklage" if kind == "in" else "Ruecklage-Entnahme") + f"_GID{code}_{exec_date:%Y-%m-%d}_{ex.id}"
    ex.filename = sepa.safe_filename(stem)
    return ex


def _date(value, fallback: date) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return fallback


def create_in_export(s: Session, request: Optional[Request], bld: Building, items: list[Item], exec_date: date,
                     created_by: str = "") -> SepaExport:
    """Sammelauftrag Rücklagen (Hauptkonto → Rücklagenkonto) erzeugen und die Zuführungen vormerken."""
    debtor, creditor = accounts(bld)
    msg_id, xml = sepa.credit_transfer(debtor, creditor, [sepa.Transfer(i.amount, i.purpose, i.end_to_end) for i in items],
                                       exec_date, bld.sepa_format or "pain.001.001.09")
    ex = _store_export(s, request, bld, "in", xml, msg_id, exec_date, len(items), sum(i.amount for i in items),
                       created_by)
    for i in items:
        s.add(ReserveEntry(building_id=bld.id, ref=i.pos.ref, name=i.pos.name, booking_date=exec_date, amount=i.amount,
                           kind="in", billing_id=i.billing.id if i.billing else None,
                           party_id=i.party["id"] if i.party else None, period=f"{i.month:%Y-%m}" if i.month else "",
                           export_id=ex.id, note=i.purpose, e2e=i.end_to_end))
    return ex


@router.post("/admin/reserves/export")
async def export(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    bld = _building(s, form.get("building"))
    keys = set(form.getlist("items"))
    items = [i for i in pending(s, bld) if i.key in keys]
    if not items:
        return _redirect(f"/admin/reserves?building={bld.id}", "Bitte mindestens einen Posten auswählen.")
    exec_date = _date(form.get("exec_date"), default_exec_date(items))
    try:
        ex = create_in_export(s, request, bld, items, exec_date)
    except sepa.SepaError as e:
        return _redirect(f"/admin/reserves?building={bld.id}", f"SEPA-Datei nicht erzeugt: {e}")
    s.commit()
    return _redirect(f"/admin/reserves?building={bld.id}&dl={ex.id}",
                     f"SEPA-Datei erzeugt: {len(items)} Überweisung(en), {_de(ex.total)} € – jetzt im Online-Banking "
                     "unter „Datei-Übergabe“ hochladen.")


def _export(s: Session, eid: int) -> SepaExport:
    ex = s.get(SepaExport, eid)
    if ex is None or not scope.can(ex.building_id):
        raise HTTPException(404)
    return ex


@router.get("/admin/reserves/export/{eid}.xml")
def export_download(eid: int, s: Session = Depends(get_session)):
    ex = _export(s, eid)
    return Response(ex.xml.encode("utf-8"), media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{ex.filename}"', "Cache-Control": "no-store"})


@router.post("/admin/reserves/export/{eid}/confirm")
async def export_confirm(request: Request, eid: int, s: Session = Depends(get_session)):
    """Verwalter bestätigt: Datei wurde im Online-Banking ausgeführt → zählt im virtuellen Rücklagenkonto."""
    ex = _export(s, eid)
    form = await request.form()
    if form.get("undo"):
        ex.status, ex.confirmed_at, ex.confirmed_by = "open", None, ""
        s.commit()
        return _redirect(f"/admin/reserves?building={ex.building_id}", "Bestätigung zurückgenommen")
    when = _date(form.get("executed_on"), ex.exec_date or date.today())
    me = current(request)
    ex.status, ex.confirmed_at, ex.confirmed_by = "done", datetime.now(), me.display if me else ""
    s.query(ReserveEntry).filter(ReserveEntry.export_id == ex.id).update({ReserveEntry.booking_date: when})
    s.commit()
    what = "Zuführung" if ex.kind == "in" else "Auszahlung"
    return _redirect(f"/admin/reserves?building={ex.building_id}",
                     f"{what} über {_de(ex.total)} € als ausgeführt bestätigt – Rücklagenkonto aktualisiert.")


@router.post("/admin/reserves/export/{eid}/delete")
def export_delete(eid: int, s: Session = Depends(get_session)):
    """Datei verwerfen (z. B. Upload abgelehnt) – die Posten sind danach wieder offen."""
    ex = _export(s, eid)
    bid = ex.building_id
    if ex.status == "done":
        return _redirect(f"/admin/reserves?building={bid}",
                         "Bereits als ausgeführt bestätigt – erst die Bestätigung zurücknehmen.")
    from .db import FinTxn

    for e in s.query(ReserveEntry).filter(ReserveEntry.export_id == ex.id):
        if e.fin_txn_id and (ft := s.get(FinTxn, e.fin_txn_id)) is not None:
            ft.reserve_done = ""  # Rechnung ist wieder offen
    s.query(ReserveEntry).filter(ReserveEntry.export_id == ex.id).delete()
    s.delete(ex)
    s.commit()
    return _redirect(f"/admin/reserves?building={bid}", "SEPA-Datei verworfen – Posten sind wieder offen.")


@router.post("/admin/reserves/entry")
async def entry_add(request: Request, s: Session = Depends(get_session)):
    """Entnahme (Rechnung bezahlt), Anfangsbestand oder manuelle Zuführung buchen."""
    form = await request.form()
    bld = _building(s, form.get("building"))
    back = f"/admin/reserves?building={bld.id}"
    kind = str(form.get("kind", "out"))
    if kind not in KINDS:
        kind = "out"
    ref = str(form.get("ref", ""))
    names = all_positions(s)
    if ref not in names:
        return _redirect(back, "Bitte eine Position wählen.")
    amount = rnd.parse_float(form.get("amount")) or 0.0
    if amount <= 0:
        return _redirect(back, "Bitte einen Betrag größer 0 eingeben.")
    when = _date(form.get("booking_date"), date.today())
    note = " ".join(str(form.get("note", "")).split())[:140]
    signed = -amount if kind == "out" else amount
    e = ReserveEntry(building_id=bld.id, ref=ref, name=names[ref], booking_date=when, amount=round(signed, 2),
                     kind=kind, note=note)
    stand = next((r["total"] for r in balances(s, bld) if r["ref"] == ref), 0.0)
    s.add(e)
    msg = f"{KINDS[kind]} gebucht: {_de(amount)} € ({names[ref]})"
    if kind == "out":
        if amount > stand + 0.005:
            msg += f" – Achtung: übersteigt den bestätigten Rücklagenstand von {_de(stand)} €"
    if kind == "out" and form.get("sepa"):  # Rechnung fällig: Gutschrift Rücklagenkonto → Hauptkonto als SEPA-Datei
        debtor, creditor = accounts(bld)
        label = next((p.label for r, p in positions(s, bld).items() if r == ref), names[ref].upper())
        code = f"GID: {gid(bld.code)} " if bld.code else ""
        text = f"GUTSCHRIFT RUECKLAGE {label} {code}{_de(amount)} EUR {note}".upper()
        exec_date = when if when > date.today() and sepa.is_business_day(when) else sepa.next_business_day()
        s.flush()
        e.e2e = f"RLAUS{e.id}"
        try:
            msg_id, xml = sepa.credit_transfer(creditor, debtor, [sepa.Transfer(amount, text, e.e2e)], exec_date,
                                               bld.sepa_format or "pain.001.001.09")
        except sepa.SepaError as err:
            s.rollback()
            return _redirect(back, f"SEPA-Datei nicht erzeugt: {err}")
        ex = _store_export(s, request, bld, "out", xml, msg_id, exec_date, 1, amount)
        e.export_id = ex.id
        if bld.reserve_no_file:
            ex.manual, ex.token = True, new_token(s)
            ex.manual_purpose = manual_purpose(ex, [(text, amount)])
            s.commit()
            return _redirect(f"{back}&dl={ex.id}", msg.replace("gebucht", "vorgemerkt") + " · Überweisungsvorlage "
                             "aufs Hauptkonto oben – nach Ausführung bitte bestätigen.")
        s.commit()
        return _redirect(f"{back}&dl={ex.id}", msg.replace("gebucht", "vorgemerkt") + " · SEPA-Gutschrift aufs Hauptkonto "
                         "erzeugt – nach Ausführung bitte bestätigen.")
    s.commit()
    return _redirect(back, msg)


@router.post("/admin/reserves/entry/{eid}/delete")
def entry_delete(eid: int, s: Session = Depends(get_session)):
    e = s.get(ReserveEntry, eid)
    if e is None or not scope.can(e.building_id):
        raise HTTPException(404)
    if e.export_id:
        return _redirect(f"/admin/reserves?building={e.building_id}",
                         "Buchungen aus einer SEPA-Datei bitte über „Verwerfen“ der Datei zurücknehmen.")
    bid = e.building_id
    if e.export_id and (ex := s.get(SepaExport, e.export_id)) is not None:
        s.delete(ex)
    s.delete(e)
    s.commit()
    return _redirect(f"/admin/reserves?building={bid}", "Buchung gelöscht")


# --------------------------------------------------------------------------- feste Rücklagen (Grundsteuer, Versicherung …)
@router.post("/admin/reserves/plan")
async def plan_save(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    bld = _building(s, form.get("building"))
    back = f"/admin/reserves?building={bld.id}#feste"
    pid = int(form.get("id") or 0)
    p = s.get(ReservePlan, pid) if pid else ReservePlan(building_id=bld.id)
    if p is None or p.building_id != bld.id:
        raise HTTPException(404)
    name = " ".join(str(form.get("name", "")).split())[:200]
    if not name:
        return _redirect(back, "Bitte eine Bezeichnung eingeben.")
    p.name = name
    p.label = sepa.clean(str(form.get("label", "")), 60, umlauts=False).upper()
    p.annual_amount = round(rnd.parse_float(form.get("annual_amount")) or 0.0, 2)
    interval = int(form.get("interval") or 1) if str(form.get("interval", "1")).isdigit() else 1
    p.interval = interval if interval in INTERVALS else 1
    start = str(form.get("start", "")).strip()
    p.start = start if _month(start) else f"{date.today():%Y-%m}"
    p.due_note = " ".join(str(form.get("due_note", "")).split())[:120]
    p.category = "privat" if form.get("category") == "privat" else "immobilie"
    p.funding = form.get("funding") if form.get("funding") in ("dauerauftrag", "manuell") else "sepa"
    p.keywords = " ".join(str(form.get("keywords", "")).split())[:200]
    p.standing_order = round(rnd.parse_float(form.get("standing_order")) or 0.0, 2)
    p.payee_rules = " ".join(str(form.get("payee_rules", "")).split())[:400]
    p.active = bool(form.get("active")) or not pid
    if not pid:
        s.add(p)
    s.commit()
    return _redirect(back, f"Gespeichert: {p.name} – {_de(p.rate)} € {INTERVALS[p.interval]}")


@router.post("/admin/reserves/plan/{pid}/delete")
def plan_delete(pid: int, s: Session = Depends(get_session)):
    p = s.get(ReservePlan, pid)
    if p is None or not scope.can(p.building_id):
        raise HTTPException(404)
    back = f"/admin/reserves?building={p.building_id}#feste"
    if s.query(ReserveEntry).filter(ReserveEntry.ref == f"rp:{p.id}").first():
        p.active = False
        s.commit()
        return _redirect(back, "Es gibt schon Buchungen – Rücklage wurde deaktiviert statt gelöscht.")
    s.delete(p)
    s.commit()
    return _redirect(back, "Gelöscht")


# --------------------------------------------------------------------------- Kontoabgleich (CAMT/CSV)
def _squash(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", sepa.clean(text or "", 500, umlauts=False).upper())


def _core(text: str) -> str:
    """Verwendungszweck ohne Bank-Zusätze wie „DATUM 19.12.2025, 22.08 UHR“ oder „ANZAHL 5“ (zum Vergleichen)."""
    t = sepa.clean(text or "", 500, umlauts=False).upper()
    t = re.sub(r"DATUM\s*\d\d\.\d\d\.\d{4},?\s*\d\d[.:]\d\d\s*UHR", " ", t)
    t = re.sub(r"ANZAHL\s*\d+", " ", t)
    return re.sub(r"[^A-Z0-9]", "", t)


def _is_mirror(a: BankLine, b: BankLine, bld: Optional[Building], strict: bool = False) -> bool:
    """Gleiche Umbuchung auf beiden eigenen Konten: Betrag gegengleich, zeitnah und gleicher Zweck (``strict``) –
    oder (schwächer) das Gegenkonto ist jeweils das andere eigene Konto."""
    if a.account == b.account or abs(a.amount + b.amount) > 0.005 or not _near(a.booking_date, b.booking_date, 7):
        return False
    if (a.e2e and a.e2e == b.e2e) or (_core(a.purpose) and _core(a.purpose) == _core(b.purpose)):
        return True
    if strict or bld is None:
        return False
    own = {"main": sepa.normalize_iban(bld.account_iban), "reserve": sepa.normalize_iban(bld.reserve_iban)}
    return (_near(a.booking_date, b.booking_date, 3) and sepa.normalize_iban(a.iban) == own[b.account]
            and sepa.normalize_iban(b.iban) == own[a.account])


OTHER = {"bank_line_id": "main_line_id", "main_line_id": "bank_line_id"}
SURE = 95  # ab diesem Trefferwert: „eindeutig“ (gesammelt bestätigen)
PICK = 90  # ab diesem Wert ist der Vorschlag vorausgewählt


def _side(line: BankLine) -> tuple[str, str]:
    """(Art der Buchung, Feld) passend zum Umsatz: Rücklagenkonto + = Zuführung, − = Auszahlung;
    Hauptkonto − = Zuführung (Abbuchung), + = Auszahlung (Gutschrift)."""
    if line.account == "reserve":
        return ("in" if line.amount > 0 else "out"), "bank_line_id"
    return ("in" if line.amount < 0 else "out"), "main_line_id"


def _signed(line: BankLine) -> float:
    """Betrag aus Sicht des Rücklagenkontos (+ Zuführung, − Auszahlung)."""
    return round(line.amount if line.account == "reserve" else -line.amount, 2)


def _near(a: Optional[date], b: Optional[date], days: int = 7) -> bool:
    return a is not None and b is not None and abs((a - b).days) <= days


def bld_cache(s: Session, line: BankLine) -> Building:
    return s.get(Building, line.building_id)


TOKEN_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # ohne leicht verwechselbare Zeichen (0/O, 1/I)


def new_token(s: Session) -> str:
    """Zufällige, eindeutige Kennung für eine Überweisung von Hand, z. B. RG-K7QX9M2T (32^8 Möglichkeiten)."""
    while True:
        token = "RG-" + "".join(secrets.choice(TOKEN_CHARS) for _ in range(8))
        if not s.query(SepaExport.id).filter(SepaExport.token == token).first():
            return token


def manual_purpose(ex: SepaExport, items: list[tuple[str, float]]) -> str:
    """Verwendungszweck der Überweisung von Hand (statt Datei); die Kennung steht vorn, damit sie nie abgeschnitten
    wird – der Kontoabgleich verknüpft nur bei genau dieser Kennung automatisch."""
    if len(items) == 1:
        text, _ = items[0]
        return sepa.clean(f"{ex.token} {text}", 140, umlauts=False)
    return sepa.clean(f"{ex.token} GUTSCHRIFT RUECKLAGE SAMMEL {len(items)} POSTEN {ex.exec_date:%d.%m.%Y} "
                      f"{_de(ex.total)} EUR", 140, umlauts=False)


def candidates(s: Session, line: BankLine, limit: int = 60) -> list[dict]:
    """Vorschläge, beste zuerst: Überweisung aus einer App-Datei (Referenz/Zweck), Gegenbuchung auf dem anderen
    Konto, ganze SEPA-Datei (Sammelbuchung) oder Rücklagen-Topf (Stichworte, z. B. Dauerauftrag).
    Ein gleicher Betrag allein reicht nicht für einen Vorschlag."""
    kind, fld = _side(line)
    other_fld = OTHER[fld]
    amount = round(abs(line.amount), 2)
    purpose = _squash(line.purpose)
    out = []
    q = s.query(ReserveEntry).filter(ReserveEntry.building_id == line.building_id, ReserveEntry.kind == kind,
                                     getattr(ReserveEntry, fld).is_(None))
    others = {}
    for e in q:
        other_id = getattr(e, other_fld)
        if e.export_id is None and other_id is None:
            continue  # manuelle Buchung ohne Kontobezug (z. B. Anfangsbestand)
        same_amount = abs(abs(e.amount) - amount) < 0.005
        score = 0
        if line.e2e and e.e2e and line.e2e == e.e2e:
            score = 100
        elif same_amount and other_id is not None:
            o = others.get(other_id) or s.get(BankLine, other_id)
            others[other_id] = o
            if o is not None and _is_mirror(o, line, None, strict=True):
                score = 98  # Gegenbuchung (z. B. Dauerauftrag: Abbuchung Hauptkonto ↔ Gutschrift Rücklagenkonto)
            elif o is not None and _is_mirror(o, line, s.get(Building, line.building_id)):
                score = 70  # nur Betrag/Konto passen (mehrere gleiche Beträge am Tag möglich) – nicht vorauswählen
            elif o is not None and _near(o.booking_date, line.booking_date):
                score = 60
        elif same_amount and purpose and _squash(e.note) and _squash(e.note) in purpose:
            score = 90
        elif same_amount:
            score = 20
        if score:
            out.append({"value": f"e:{e.id}", "score": score, "amount": abs(e.amount), "date": e.booking_date,
                        "text": e.note or e.name})
    for ex in s.query(SepaExport).filter(SepaExport.building_id == line.building_id,
                                         SepaExport.kind == kind).order_by(SepaExport.id.desc()).limit(50):
        es = [e for e in s.query(ReserveEntry).filter(ReserveEntry.export_id == ex.id) if getattr(e, fld) is None]
        manual = ex.manual or (kind == "out" and bld_cache(s, line).reserve_no_file)
        if not es or (len(es) < 2 and not manual) or abs(sum(abs(e.amount) for e in es) - amount) > 0.005:
            continue
        squashed = _squash(line.purpose)
        if ex.token and ex.token.replace("-", "") in squashed:
            score = 100  # Überweisung von Hand mit genau dieser Kennung
        elif line.pmtinf and line.pmtinf.startswith(ex.msg_id):
            score = 100
        elif manual and "GUTSCHRIFTRUECKLAGE" in squashed:
            score = 80  # passt nur über Betrag/Text – nicht automatisch verknüpfen
        else:
            score = 60
        what = f"Überweisung von Hand ({ex.token})" if ex.token else f"SEPA-Datei {ex.filename}"
        out.append({"value": f"x:{ex.id}", "score": score, "amount": round(sum(abs(e.amount) for e in es), 2),
                    "date": ex.exec_date, "text": f"{what} ({len(es)} Posten)"})
    # Rücklagen-Topf nach Stichworten (nur Umsätze mit Bezug zum Rücklagenkonto)
    bld = s.get(Building, line.building_id)
    if line.account == "reserve" or sepa.normalize_iban(line.iban) == sepa.normalize_iban(bld.reserve_iban if bld else ""):
        what = "Zuführung" if _signed(line) > 0 else "Auszahlung"
        scored = sorted(((keyword_score(p.groups, line.purpose), p) for p in positions(s, bld).values()),
                        key=lambda x: -x[0])
        top = scored[0][0] if scored else 0
        sure = top > 0 and sum(1 for sc, _ in scored if sc == top) == 1
        for sc, p in scored:
            score = (SURE if sure else 80) if sc and sc == top else (70 if sc else 1)
            out.append({"value": f"p:{p.ref}", "score": score, "amount": amount, "date": line.booking_date,
                        "text": f"→ {what} „{p.name}“ buchen"})
    out.sort(key=lambda c: (-c["score"], c["date"] or date.min))
    return out[:limit]


def _finish_exports(s: Session, export_ids: set) -> int:
    """SEPA-Dateien, deren Buchungen alle im Kontoauszug gefunden wurden, gelten als ausgeführt."""
    done = 0
    for ex in s.query(SepaExport).filter(SepaExport.id.in_(export_ids or [-1])):
        es = s.query(ReserveEntry).filter(ReserveEntry.export_id == ex.id).all()
        if ex.status != "done" and es and all(e.bank_line_id or e.main_line_id for e in es):
            ex.status, ex.confirmed_at, ex.confirmed_by = "done", datetime.now(), "Kontoauszug"
            done += 1
    return done


def _pair_mirror(s: Session, e: ReserveEntry, line: BankLine) -> None:
    """Gegenbuchung auf dem anderen Konto gleich mit zuordnen (gleicher Betrag, gleicher Zweck, ±7 Tage)."""
    other_fld = OTHER["bank_line_id" if line.account == "reserve" else "main_line_id"]
    if getattr(e, other_fld) is not None:
        return
    other_acc = "main" if line.account == "reserve" else "reserve"
    bld = s.get(Building, line.building_id)
    free = [o for o in s.query(BankLine).filter(BankLine.building_id == line.building_id, BankLine.account == other_acc,
                                                BankLine.ignored.is_(False)) if not o.entry_ids]
    strict = [o for o in free if _is_mirror(o, line, bld, strict=True)]
    # gleicher Zweck zuerst; nur über Betrag/Gegenkonto, wenn es genau einen Kandidaten gibt (sonst z. B. ADAC ↔ Amazon)
    weak = [o for o in free if _is_mirror(o, line, bld)] if not strict else []
    match = min(strict, key=lambda o: abs((o.booking_date - line.booking_date).days)) if strict else \
        (weak[0] if len(weak) == 1 else None)
    if match is not None:
        setattr(e, other_fld, match.id)
        match.entry_ids = [e.id]


def link(s: Session, line: BankLine, value: str) -> int:
    """Umsatz verknüpfen: Buchung (e:<id>), ganze SEPA-Datei (x:<id>) oder neuer Eintrag im Topf (p:<ref>)."""
    kind, fld = _side(line)
    m = re.fullmatch(r"([exp]):(.+)", value or "")
    if not m:
        return 0
    if m[1] == "p":
        ref = m[2]
        names = all_positions(s)
        if ref not in names:
            return 0
        amount = _signed(line)
        e = ReserveEntry(building_id=line.building_id, ref=ref, name=names[ref], booking_date=line.booking_date,
                         amount=amount, kind="in" if amount > 0 else "out", note=sepa.clean(line.purpose, 140))
        setattr(e, fld, line.id)
        s.add(e)
        s.flush()
        line.entry_ids = [e.id]
        _pair_mirror(s, e, line)
        return 1
    if not m[2].isdigit():
        return 0
    if m[1] == "e":
        es = [s.get(ReserveEntry, int(m[2]))]
    else:
        es = s.query(ReserveEntry).filter(ReserveEntry.export_id == int(m[2])).all()
    es = [e for e in es if e is not None and e.building_id == line.building_id and e.kind == kind
          and getattr(e, fld) is None]
    for e in es:
        setattr(e, fld, line.id)
        if line.booking_date and e.export_id:
            e.booking_date = line.booking_date
    line.entry_ids = sorted(set(line.entry_ids or []) | {e.id for e in es})
    if len(es) == 1:
        _pair_mirror(s, es[0], line)
    _finish_exports(s, {e.export_id for e in es if e.export_id})
    return len(es)


def open_lines(s: Session, building: Building) -> list[BankLine]:
    return [l for l in s.query(BankLine).filter(BankLine.building_id == building.id, BankLine.ignored.is_(False))
            .order_by(BankLine.booking_date.desc(), BankLine.id.desc()) if not l.entry_ids]


def detect_standing_orders(s: Session, building: Building) -> list[dict]:
    """Wiederkehrende Gutschriften auf dem Rücklagenkonto (mind. 3 Monate, gleicher Zweck) als Vorschlag für Töpfe –
    ohne die, die schon zu einem Topf passen und ohne Überweisungen der App."""
    groups: dict[str, list] = {}
    for l in s.query(BankLine).filter(BankLine.building_id == building.id, BankLine.account == "reserve",
                                      BankLine.amount > 0):
        sq = _squash(l.purpose)
        if sq.startswith("RUECKLAGEAUSNEBENKOSTEN") or "SIEHEANLAGE" in sq or not tokens(l.purpose):
            continue  # App-Überweisungen und Rechnungsabschluss (Zinsen/Entgelte) sind keine Daueraufträge
        groups.setdefault(" ".join(tokens(l.purpose)), []).append(l)
    pos = positions(s, building).values()
    out = []
    for key, ls in groups.items():
        months = {l.booking_date.strftime("%Y-%m") for l in ls if l.booking_date}
        if len(months) < 3 or any(keyword_score(p.groups, ls[0].purpose) for p in pos):
            continue
        amounts = sorted((l.amount for l in ls), key=lambda a: -sum(1 for x in ls if x.amount == a))
        monthly = amounts[0]
        if sum(1 for l in ls if l.amount == monthly) < 3:
            continue  # kein fester Betrag
        text = sepa.clean(ls[-1].purpose, 200).upper()
        cyc = re.search(r"ZYKLUS\s*(.*)$", text)
        cyc = (cyc.group(1) if cyc else "").replace(" ", "")
        due = {"P.A.": "jährlich", "PA": "jährlich", "P.A": "jährlich", "P.S.": "jährlich", "2J": "alle 2 Jahre",
               "P.Q.": "vierteljährlich", "PQ": "vierteljährlich", "P.H.": "halbjährlich", "P.M.": "monatlich"}.get(cyc, cyc.lower())
        if "4MONATE" in text.replace(" ", ""):
            due = "alle 4 Monate"
        name = " ".join(w.capitalize() if len(w) > 3 else w for w in tokens(ls[-1].purpose))
        immo = re.search(r"GRUNDSTEUER|WOHNGEB|STROMZ|WASSER|KAMIN|MUELL|ABFALL|HAUS|GEBAEUDE", key)
        out.append({"key": key, "name": name, "monthly": monthly, "count": len(ls), "months": len(months),
                    "first": min(months), "last": max(months), "due": due,
                    "category": "immobilie" if immo else "privat", "total": round(sum(l.amount for l in ls), 2)})
    return sorted(out, key=lambda x: -x["monthly"])


@router.post("/admin/reserves/plans/detect")
async def plans_detect(request: Request, s: Session = Depends(get_session)):
    """Erkannte Daueraufträge als Töpfe anlegen (Dauerauftrag, Jahresbetrag = 12 × Monatsbetrag)."""
    form = await request.form()
    bld = _building(s, form.get("building"))
    chosen = set(form.getlist("keys"))
    n = 0
    for d in detect_standing_orders(s, bld):
        if d["key"] not in chosen:
            continue
        s.add(ReservePlan(building_id=bld.id, name=d["name"][:200], label=d["key"][:60], annual_amount=d["monthly"] * 12,
                          interval=1, start=d["first"], due_note=d["due"][:120], category=d["category"],
                          funding="dauerauftrag", keywords="", standing_order=d["monthly"], active=True))
        n += 1
    s.commit()
    return _redirect(f"/admin/reserves?building={bld.id}#abgleich",
                     f"{n} Töpfe angelegt – jetzt „eindeutige Treffer bestätigen“, um die Kontoumsätze zuzuordnen.")


def _store_balances(bld: Building, balances: list, ibans: dict) -> None:
    for b in balances:
        acc = ibans.get(sepa.normalize_iban(b.account_iban))
        if acc is None or b.day is None:
            continue
        if b.kind == "CLBD":
            if acc == "main" and (bld.bank_main_date is None or b.day >= bld.bank_main_date):
                bld.bank_main_balance, bld.bank_main_date = b.amount, b.day
            if acc == "reserve" and (bld.bank_reserve_date is None or b.day >= bld.bank_reserve_date):
                bld.bank_reserve_balance, bld.bank_reserve_date = b.amount, b.day
        if b.kind == "OPBD" and acc == "reserve" and (bld.bank_reserve_open_date is None
                                                      or b.day < bld.bank_reserve_open_date):
            bld.bank_reserve_open, bld.bank_reserve_open_date = b.amount, b.day


def ingest_bank(s: Session, bld: Building, txns: list, bals: list, since: Optional[date] = None) -> str:
    """Umsätze/Kontostände eines Auszugs für den Rücklagen-Abgleich übernehmen; liefert eine Rückmeldung."""
    main_iban, reserve_iban = sepa.normalize_iban(bld.account_iban), sepa.normalize_iban(bld.reserve_iban)
    accounts_ = {main_iban: "main", reserve_iban: "reserve"}
    accounts_.pop("", None)
    _store_balances(bld, bals, accounts_)
    new = skipped = foreign = irrelevant = old = 0
    seen = {d for (d,) in s.query(BankLine.digest).filter(BankLine.building_id == bld.id)}
    for t in txns:
        acc = accounts_.get(sepa.normalize_iban(t.account_iban))
        if acc is None:
            foreign += 1
            continue
        if since and t.booking_date and t.booking_date < since:
            old += 1
            continue
        if acc == "main" and not (sepa.normalize_iban(t.iban) == reserve_iban or t.e2e.startswith("RL")
                                  or t.pmtinf.startswith("IMV")):
            irrelevant += 1  # Hauptkonto: nur Umsätze mit Bezug zum Rücklagenkonto bzw. aus App-Dateien
            continue
        if t.digest in seen:
            skipped += 1
            continue
        seen.add(t.digest)
        s.add(BankLine(building_id=bld.id, account=acc, booking_date=t.booking_date, amount=t.amount,
                       purpose=t.purpose[:2000], name=t.name[:200], iban=t.iban[:34], e2e=t.e2e[:100],
                       pmtinf=t.pmtinf[:100], batch_count=t.batch_count, info=t.info[:100], digest=t.digest,
                       source=t.source[:20]))
        new += 1
    s.commit()
    msg = f"{new} Umsätze eingelesen"
    if old:
        msg += f", {old} vor dem gewählten Datum übersprungen"
    if irrelevant:
        msg += f", {irrelevant} Umsätze des Hauptkontos ohne Bezug zum Rücklagenkonto übersprungen"
    if skipped:
        msg += f", {skipped} schon vorhanden"
    if foreign:
        msg += f", {foreign} von anderen Konten übersprungen (IBAN passt weder zu Haupt- noch Rücklagenkonto)"
    if bals:
        msg += " · Kontostände aktualisiert"
    sure = sum(1 for l in open_lines(s, bld) if (c := candidates(s, l, 1)) and c[0]["score"] >= SURE)
    if sure:
        msg += f" · {sure} eindeutige Treffer – bitte prüfen und bestätigen"
    return msg


@router.post("/admin/reserves/bank/import")
async def bank_import(request: Request, file: UploadFile, s: Session = Depends(get_session)):
    from . import finance

    form = await request.form()
    bld = _building(s, form.get("building"))
    back = f"/admin/reserves?building={bld.id}#abgleich"
    data = await file.read()
    bals: list = []
    try:
        txns = bankimport.parse(file.filename or "", data, bals)
    except bankimport.ImportError_ as e:
        return _redirect(back, f"Import fehlgeschlagen: {e}")
    since = _date(form.get("since"), date.min) if form.get("since") else None
    msg = ingest_bank(s, bld, txns, bals, since)
    if scope.is_super():  # Hauptkonto-Umsätze auch für die Finanzplanung übernehmen
        finance.seed(s)
        n, _ = finance.ingest(s, txns, bals)
        if n:
            msg += f" · Finanzen: {n} Umsätze"
            finance.budget_alerts(s)
    return _redirect(back, msg)


@router.post("/admin/reserves/bank/confirm-sure")
async def bank_confirm_sure(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    bld = _building(s, form.get("building"))
    lines = n = 0
    for l in open_lines(s, bld):
        if l.entry_ids:  # schon als Gegenbuchung mit zugeordnet
            continue
        c = candidates(s, l, 1)
        if c and c[0]["score"] >= SURE:
            k = link(s, l, c[0]["value"])
            lines += bool(k)
            n += k
    s.commit()
    return _redirect(f"/admin/reserves?building={bld.id}#abgleich", f"{lines} Umsätze zugeordnet ({n} Buchungen)")


@router.post("/admin/reserves/bank/hide-rest")
async def bank_hide_rest(request: Request, s: Session = Depends(get_session)):
    """Alle Umsätze ohne Vorschlag ausblenden (z. B. Zinsen oder private Umbuchungen)."""
    form = await request.form()
    bld = _building(s, form.get("building"))
    n = 0
    for l in open_lines(s, bld):
        c = candidates(s, l, 1)
        if not c or c[0]["score"] < PICK:
            l.ignored = True
            n += 1
    s.commit()
    return _redirect(f"/admin/reserves?building={bld.id}#abgleich", f"{n} Umsätze ohne Vorschlag ausgeblendet")


@router.post("/admin/reserves/bank/unhide-all")
async def bank_unhide_all(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    bld = _building(s, form.get("building"))
    n = s.query(BankLine).filter(BankLine.building_id == bld.id, BankLine.ignored.is_(True)) \
        .update({BankLine.ignored: False})
    s.commit()
    return _redirect(f"/admin/reserves?building={bld.id}#abgleich", f"{n} Umsätze wieder eingeblendet")


@router.post("/admin/reserves/bank/{lid:int}")
async def bank_line_action(request: Request, lid: int, s: Session = Depends(get_session)):
    line = s.get(BankLine, lid)
    if line is None or not scope.can(line.building_id):
        raise HTTPException(404)
    form = await request.form()
    back = f"/admin/reserves?building={line.building_id}#abgleich"
    action = str(form.get("action", "match"))
    if action == "ignore":
        line.ignored = True
        s.commit()
        return _redirect(back, "Umsatz ausgeblendet")
    if action == "unlink":
        kind, fld = _side(line)
        for e in s.query(ReserveEntry).filter(getattr(ReserveEntry, fld) == line.id).all():
            other_id = getattr(e, OTHER[fld])
            if e.export_id is None:  # aus dem Umsatz erzeugt → wieder entfernen, Gegenbuchung freigeben
                if other_id and (o := s.get(BankLine, other_id)) is not None:
                    o.entry_ids = [x for x in (o.entry_ids or []) if x != e.id]
                s.delete(e)
            else:
                setattr(e, fld, None)
        line.entry_ids, line.ignored = [], False
        s.commit()
        return _redirect(back, "Zuordnung aufgehoben")
    if action == "book":  # Umsatz einem Topf zubuchen (z. B. Zinsen, Dauerauftrag, frühere Gutschrift)
        ref = str(form.get("ref", ""))
        names = all_positions(s)
        if ref not in names or not link(s, line, f"p:{ref}"):
            return _redirect(back, "Bitte einen Topf wählen.")
        s.commit()
        return _redirect(back, f"Als {'Zuführung' if _signed(line) > 0 else 'Auszahlung'} gebucht ({names[ref]})")
    n = link(s, line, str(form.get("match", "")))
    s.commit()
    return _redirect(back, f"{n} Buchung(en) zugeordnet" if n else "Bitte eine passende Buchung auswählen.")


# --------------------------------------------------------------------------- Gutschriften: Rechnungen vom Hauptkonto erstatten
def parse_payee(text: str) -> list[list]:
    """„Rechnung kommt von“: Alternativen mit Komma, Bedingungen einer Alternative mit „&“ (alle müssen passen),
    z. B. ``MARKT HIRSCHAID & zweck:GRUNDSTEUER, iban:DE…``."""
    from . import finance

    groups = []
    for alt in re.split(r"[,\n;]", text or ""):
        conds = [r for part in alt.split("&") for r in finance.parse_rules(part)]
        if conds:
            groups.append(conds)
    return groups


def _since(s: Session, bld: Building, ref: str, *dates) -> Optional[date]:
    """Rechnungen zählen ab dem frühesten Zeitpunkt des Topfs: erste Buchung im Topf bzw. Start/„Rücklagen ab“
    (der Start des Sammelauftrags nach einer Umstellung zählt nicht als Beginn des Topfs)."""
    first = s.query(ReserveEntry.booking_date).filter(ReserveEntry.building_id == bld.id, ReserveEntry.ref == ref,
                                                      ReserveEntry.booking_date.isnot(None)) \
        .order_by(ReserveEntry.booking_date).first()
    cands = [d for d in (*dates, first[0].replace(day=1) if first else None) if d]
    return min(cands) if cands else None


def _payee_sources(s: Session, bld: Building) -> list[tuple]:
    """(ref, Position, Regel-Gruppen, ab Datum, privat) für Töpfe/Positionen mit „Rechnung kommt von“."""
    pos = positions(s, bld)
    out = []
    for p in plans(s, bld, active_only=True):
        ref = f"rp:{p.id}"
        if p.funding != "manuell" and p.payee_rules.strip() and ref in pos:  # geschützte Töpfe nie erstatten
            out.append((ref, pos[ref], parse_payee(p.payee_rules), _since(s, bld, ref, _month(p.start)),
                        p.category == "privat"))
    for f in s.query(FixedCost).filter(FixedCost.reserve.is_(True)):
        if (f.payee_rules or "").strip():
            out.append((f"fc:{f.id}", pos.get(f"fc:{f.id}"), parse_payee(f.payee_rules),
                        _since(s, bld, f"fc:{f.id}", _start(bld)), False))
    for a in s.query(Allocation).filter(Allocation.reserve.is_(True)):
        if (a.payee_rules or "").strip():
            out.append((f"al:{a.id}", pos.get(f"al:{a.id}"), parse_payee(a.payee_rules),
                        _since(s, bld, f"al:{a.id}", _start(bld)), False))
    return [x for x in out if x[1] is not None and x[2]]


def _rule_hit(groups, t) -> int:
    """Trefferwert der besten Alternative (Summe der Bedingungen), 0 = passt nicht."""
    from . import finance

    name, purpose, iban = finance.norm(t.name), finance.norm(t.purpose), sepa.normalize_iban(t.iban)
    best = 0
    for conds in groups:
        total = 0
        for r in conds:
            if r.field == "iban":
                hit, size = bool(iban) and iban == r.text, 98
            elif r.field in ("name", "purpose"):
                hit, size = bool(r.rx.search(name if r.field == "name" else purpose)), len(r.text)
            else:
                hit, size = False, 0
            if not hit:
                total = 0
                break
            total += size
        best = max(best, total)
    return best


def refund_purpose(label: str, t, amount: float, building: Optional[Building], private: bool = False) -> str:
    """z. B. GUTSCHRIFT RUECKLAGE GRUNDSTEUER GID: 134 RECHNUNG MARKT HIRSCHAID 16.11.2026 147,99 EUR"""
    code = f"GID: {gid(building.code)} " if building and building.code and not private else ""
    payee = sepa.clean(t.name or t.purpose, 40, umlauts=False).upper()
    date_ = f"{t.booking_date:%d.%m.%Y} " if t.booking_date else ""
    return sepa.clean(f"GUTSCHRIFT RUECKLAGE {label} {code}RECHNUNG {payee} {date_}{_de(amount)} EUR", 140,
                      umlauts=False)


def open_refunds(s: Session, bld: Building) -> list[dict]:
    """Rechnungen, die vom Hauptkonto bezahlt wurden und zu einem Topf gehören, aber noch nicht aus der Rücklage
    erstattet sind. „Vermutlich erstattet“: eine Auszahlung aus dem Topf ±21 Tage mit mind. 95 % des Betrags."""
    from . import finance
    from .db import FinTxn

    sources = _payee_sources(s, bld)
    iban = finance.finance_iban(s)
    if not sources or not iban:
        return []
    pool = defaultdict(list)  # Auszahlungen je Topf mit Restbetrag (eine Gutschrift kann mehrere Rechnungen abdecken)
    for e in s.query(ReserveEntry).filter(ReserveEntry.building_id == bld.id, ReserveEntry.amount < 0,
                                          ReserveEntry.fin_txn_id.is_(None)).order_by(ReserveEntry.booking_date):
        pool[e.ref].append([e, -e.amount])
    rows = []
    own = {sepa.normalize_iban(x) for x in (bld.account_iban, bld.reserve_iban, iban) if x}
    for t in s.query(FinTxn).filter(FinTxn.account_iban == iban, FinTxn.amount < 0, FinTxn.reserve_done == "") \
            .order_by(FinTxn.booking_date, FinTxn.id):
        if sepa.normalize_iban(t.iban) in own:
            continue  # Umbuchung zwischen eigenen Konten ist keine Rechnung
        best = None
        for ref, pos, rules, since, private in sources:
            if since and t.booking_date and t.booking_date < since:
                continue
            score = _rule_hit(rules, t)
            if score and (best is None or score > best[0]):
                best = (score, ref, pos, private)
        if best is None:
            continue
        _, ref, pos, private = best
        amount = round(-t.amount, 2)
        covered, last = 0.0, None
        for item in pool.get(ref, []):  # Auszahlungen aus dem Topf ±21 Tage anrechnen
            e, left = item
            if left < 0.005 or not (t.booking_date and e.booking_date) or abs((e.booking_date - t.booking_date).days) > 21:
                continue
            take = min(left, amount - covered)
            item[1] -= take
            covered += take
            last = e
            if covered >= amount - 0.005:
                break
        rest = round(amount - covered, 2)
        full = rest <= max(0.05 * amount, 0.01)  # Rundungs-/Kleinstdifferenzen (z. B. 147,00 statt 147,99)
        rows.append({"txn": t, "ref": ref, "pos": pos, "amount": amount if not covered else (0.0 if full else rest),
                     "bill": amount, "covered": round(covered, 2), "probable": last if full else None,
                     "partial": last if covered and not full else None,
                     "purpose": refund_purpose(pos.label, t, rest if covered and not full else amount, bld, private)})
    return rows


@router.post("/admin/reserves/refunds/export")
async def refunds_export(request: Request, s: Session = Depends(get_session)):
    """Sammelauftrag Gutschriften: Rücklagenkonto → Hauptkonto, je Rechnung eine Überweisung."""
    from .db import FinTxn

    form = await request.form()
    bld = _building(s, form.get("building"))
    back = f"/admin/reserves?building={bld.id}#gutschriften"
    keys = {int(x) for x in form.getlist("bills") if str(x).isdigit()}
    rows = [r for r in open_refunds(s, bld) if r["txn"].id in keys and r["amount"] >= 0.01]
    if not rows:
        return _redirect(back, "Bitte mindestens eine Rechnung auswählen.")
    exec_date = _date(form.get("exec_date"), sepa.next_business_day())
    debtor, creditor = accounts(bld)
    try:
        msg_id, xml = sepa.credit_transfer(creditor, debtor, [sepa.Transfer(r["amount"], r["purpose"], f"RG{r['txn'].id}")
                                                              for r in rows], exec_date,
                                           bld.sepa_format or "pain.001.001.09")
    except sepa.SepaError as e:
        return _redirect(back, f"SEPA-Datei nicht erzeugt: {e}")
    ex = _store_export(s, request, bld, "out", xml, msg_id, exec_date, len(rows), sum(r["amount"] for r in rows))
    for r in rows:
        s.add(ReserveEntry(building_id=bld.id, ref=r["ref"], name=r["pos"].name, booking_date=exec_date,
                           amount=-r["amount"], kind="out", export_id=ex.id, note=r["purpose"], e2e=f"RG{r['txn'].id}",
                           fin_txn_id=r["txn"].id))
        s.get(FinTxn, r["txn"].id).reserve_done = "erstattet"
    if bld.reserve_no_file:  # Sparkonto ohne Datei-Übergabe → eine Überweisung von Hand
        ex.manual, ex.token = True, new_token(s)
        ex.manual_purpose = manual_purpose(ex, [(r["purpose"], r["amount"]) for r in rows])
        s.commit()
        return _redirect(f"/admin/reserves?building={bld.id}&dl={ex.id}#gutschriften",
                         f"Gutschrift vorbereitet: {len(rows)} Posten, {_de(ex.total)} € – bitte als eine Überweisung "
                         "vom Rücklagenkonto aufs Hauptkonto ausführen (Vorlage oben).")
    s.commit()
    return _redirect(f"/admin/reserves?building={bld.id}&dl={ex.id}#gutschriften",
                     f"Sammelauftrag Gutschriften erzeugt: {len(rows)} Überweisung(en), {_de(ex.total)} € – im "
                     "Online-Banking des Rücklagenkontos hochladen, nach Ausführung bestätigen.")


@router.post("/admin/reserves/refunds/{tid}")
async def refund_mark(request: Request, tid: int, s: Session = Depends(get_session)):
    """Rechnung als bereits erstattet bzw. „nicht erstatten“ markieren (oder zurücksetzen)."""
    from .db import FinTxn

    t = s.get(FinTxn, tid)
    form = await request.form()
    bld = _building(s, form.get("building"))
    if t is None:
        raise HTTPException(404)
    action = str(form.get("action", ""))
    t.reserve_done = {"done": "erstattet", "skip": "ignoriert"}.get(action, "")
    s.commit()
    return _redirect(f"/admin/reserves?building={bld.id}#gutschriften",
                     {"done": "Als bereits erstattet markiert", "skip": "Wird nicht erstattet"}.get(action, "Zurückgesetzt"))


@router.post("/admin/reserves/plans/cut")
async def plans_cut(request: Request, s: Session = Depends(get_session)):
    """Umstellung: Daueraufträge enden, ab Monat X erzeugt die App monatlich den Sammelauftrag (SEPA-Datei)."""
    form = await request.form()
    bld = _building(s, form.get("building"))
    month = str(form.get("month", ""))
    if not _month(month):
        return _redirect(f"/admin/reserves?building={bld.id}#feste", "Bitte einen Monat wählen.")
    n = 0
    for p in plans(s, bld, active_only=True):
        if p.funding == "dauerauftrag":
            p.funding, p.start, p.interval = "sepa", month, 1
            if not p.annual_amount and p.standing_order:
                p.annual_amount = round(p.standing_order * 12, 2)
            p.standing_order = 0.0
            n += 1
    s.commit()
    return _redirect(f"/admin/reserves?building={bld.id}#feste",
                     f"{n} Töpfe ab {month[5:]}/{month[:4]} auf monatlichen Sammelauftrag umgestellt – Daueraufträge "
                     "bei der Bank löschen.")


@router.post("/admin/reserves/entry/{eid}/move")
async def entry_move(request: Request, eid: int, s: Session = Depends(get_session)):
    """Buchung in einen anderen Topf umbuchen (Kontoverknüpfungen bleiben erhalten)."""
    e = s.get(ReserveEntry, eid)
    if e is None or not scope.can(e.building_id):
        raise HTTPException(404)
    form = await request.form()
    ref = str(form.get("ref", ""))
    names = all_positions(s)
    if ref not in names:
        return _redirect(f"/admin/reserves?building={e.building_id}#buchungen", "Bitte einen Topf wählen.")
    old = names.get(e.ref, e.name)
    e.ref, e.name = ref, names[ref]
    s.commit()
    return _redirect(f"/admin/reserves?building={e.building_id}#buchungen", f"Umgebucht: {old} → {names[ref]}")


def repair_mirrors(s: Session, bld: Building) -> int:
    """Gegenbuchungen neu zuordnen: Hauptkonto-Verknüpfungen von Buchungen aus dem Rücklagenkonto lösen und mit der
    Regel „gleicher Zweck zuerst“ neu paaren. Liefert die Zahl geänderter Paarungen."""
    entries = s.query(ReserveEntry).filter(ReserveEntry.building_id == bld.id, ReserveEntry.bank_line_id.isnot(None),
                                           ReserveEntry.main_line_id.isnot(None), ReserveEntry.export_id.is_(None)).all()
    before = {e.id: e.main_line_id for e in entries}
    for e in entries:
        o = s.get(BankLine, e.main_line_id)
        if o is not None:
            o.entry_ids = [x for x in (o.entry_ids or []) if x != e.id]
        e.main_line_id = None
    s.flush()
    for e in sorted(entries, key=lambda x: x.id):
        line = s.get(BankLine, e.bank_line_id)
        if line is not None:
            _pair_mirror(s, e, line)
    s.flush()
    return sum(1 for e in entries if e.main_line_id != before[e.id])


@router.post("/admin/reserves/bank/repair-mirrors")
async def bank_repair_mirrors(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    bld = _building(s, form.get("building"))
    n = repair_mirrors(s, bld)
    s.commit()
    return _redirect(f"/admin/reserves?building={bld.id}#abgleich", f"Gegenbuchungen neu zugeordnet: {n} Paarung(en) geändert")


# --------------------------------------------------------------------------- monatlicher Sammelauftrag per E-Mail
def first_business_day(d: date) -> date:
    while not sepa.is_business_day(d):
        d = date.fromordinal(d.toordinal() + 1)
    return d


def _mail_recipients(s: Session, bld: Building) -> list[str]:
    from .db import User

    if bld.sepa_mail_to.strip():
        return [a.strip() for a in bld.sepa_mail_to.split(",") if a.strip()]
    return [u.email for u in s.query(User).filter(User.role == "superadmin", User.active.is_(True)) if u.email]


def monthly_run(s: Session, bld: Building, today: Optional[date] = None, force: bool = False) -> Optional[str]:
    """Sammelauftrag für den Folgemonat: ab dem eingestellten Tag im Vormonat (Standard 25.) einmal je Monat.
    Enthält alle Töpfe (Monatsbetrag) und offene Nebenkosten-Rücklagen; Ausführung am ersten Bankarbeitstag.
    Liefert eine Rückmeldung oder None (noch nicht fällig / schon versandt)."""
    from . import mailer, notify

    today = today or date.today()
    target = _add_months(today.replace(day=1), 1)
    ym = f"{target:%Y-%m}"
    if not force and (not bld.sepa_mail or today.day < (bld.sepa_mail_day or 25) or bld.sepa_mail_last == ym):
        return None
    to = _mail_recipients(s, bld)
    if not to:
        raise sepa.SepaError("Keine E-Mail-Adresse für den Versand (beim Gebäude oder beim Super-Admin hinterlegen).")
    existing = None
    if bld.sepa_mail_last == ym:  # erneut senden: dieselbe Datei, keine zweite
        existing = s.query(SepaExport).filter(SepaExport.building_id == bld.id, SepaExport.kind == "in",
                                              SepaExport.created_by == f"Monatslauf {ym}").order_by(SepaExport.id.desc()).first()
    if existing is None:
        items = billing_pending(s, bld) + plan_pending(s, bld, today=target)
        if not items:
            bld.sepa_mail_last = ym
            s.commit()
            return f"{target:%m/%Y}: keine Zuführungen fällig – nichts versandt."
        ex = create_in_export(s, None, bld, items, first_business_day(target), created_by=f"Monatslauf {ym}")
        lines = [(i.pos.name + (f" – {i.party['name']}" if i.party else ""), i.amount) for i in items]
    else:
        ex = existing
        lines = [(e.name, e.amount) for e in s.query(ReserveEntry).filter(ReserveEntry.export_id == ex.id)]
    refunds = [r for r in open_refunds(s, bld) if not r["probable"] and r["amount"] >= 0.01]
    base = config_base()
    facts = [("Monat", f"{target:%m/%Y}", True), ("Ausführung", f"{ex.exec_date:%d.%m.%Y}", True),
             ("Überweisungen", str(ex.count), False), ("Summe", f"{_de(ex.total)} €", True),
             ("Datei", ex.filename, False)]
    paragraphs = ["anbei der SEPA-Sammelauftrag Rücklagen (Hauptkonto → Rücklagenkonto) für den kommenden Monat.",
                  "Im Online-Banking des Hauptkontos unter „Datei-Übergabe“ hochladen, Summe und Ausführungsdatum prüfen "
                  "und freigeben. Nach der Ausführung in der App bestätigen – oder einfach den nächsten Kontoauszug "
                  "einlesen, dann wird die Datei automatisch als ausgeführt erkannt.",
                  "Aufstellung: " + "; ".join(f"{n} {_de(a)} €" for n, a in lines)]
    if refunds:
        paragraphs.append(f"Außerdem sind {len(refunds)} Gutschrift(en) über {_de(sum(r['amount'] for r in refunds))} € "
                          "aus der Rücklage aufs Hauptkonto offen – unter „Rücklagen → Offene Gutschriften“ prüfen und "
                          + ("die Überweisungsvorlage erzeugen (eine Überweisung vom Rücklagenkonto)."
                             if bld.reserve_no_file else "den Sammelauftrag Gutschriften erzeugen."))
    html = mailer.render_html(title=f"Sammelauftrag Rücklagen {target:%m/%Y}", preheader=f"{_de(ex.total)} € am "
                              f"{ex.exec_date:%d.%m.%Y}", facts=facts, paragraphs=["Hallo,"] + paragraphs,
                              closing="Viele Grüße\nImmoVerwaltung",
                              button_url=f"{base}/admin/reserves?building={bld.id}" if base else "",
                              button_label="Zu den Rücklagen", footer=bld.label)
    text = "Hallo,\n\n" + "\n\n".join(paragraphs) + "\n\n" + "\n".join(f"{k}: {v}" for k, v, _ in facts)
    mailer.send_mail(to, f"SEPA-Sammelauftrag Rücklagen {target:%m/%Y}: {_de(ex.total)} € ({bld.label})", text,
                     [(ex.filename, ex.xml.encode("utf-8"), "application/xml")], html=html)
    bld.sepa_mail_last = ym
    s.commit()
    try:
        notify.to_admins(s, "push_admin_sent", f"Sammelauftrag Rücklagen {target:%m/%Y}",
                         f"{_de(ex.total)} € · {ex.count} Überweisungen · per E-Mail an {', '.join(to)}",
                         url=f"/admin/reserves?building={bld.id}", tag=f"sepa-{ym}")
    except Exception:  # noqa: BLE001
        pass
    return f"Sammelauftrag {target:%m/%Y} ({_de(ex.total)} €, {ex.count} Überweisungen) per E-Mail an {', '.join(to)} gesendet."


def config_base() -> str:
    from .config import config

    return (config.app_base_url or "").rstrip("/")


def monthly_tick(s: Session, today: Optional[date] = None) -> list[str]:
    """Aus der Hintergrundschleife: für alle Gebäude mit aktivem Versand prüfen."""
    from . import notify

    out = []
    for bld in s.query(Building).filter(Building.sepa_mail.is_(True)):
        try:
            if (m := monthly_run(s, bld, today)):
                out.append(m)
        except Exception as e:  # noqa: BLE001
            s.rollback()
            try:
                notify.to_admins(s, "push_admin_errors", "Sammelauftrag Rücklagen fehlgeschlagen", str(e)[:200],
                                 url="/admin/reserves", tag="sepa-error")
            except Exception:  # noqa: BLE001
                pass
    return out


@router.post("/admin/reserves/monthly/send")
async def monthly_send(request: Request, s: Session = Depends(get_session)):
    """Sammelauftrag für den Folgemonat sofort erzeugen und senden (bzw. erneut senden)."""
    form = await request.form()
    bld = _building(s, form.get("building"))
    back = f"/admin/reserves?building={bld.id}#konten"
    try:
        msg = monthly_run(s, bld, force=True)
    except Exception as e:  # noqa: BLE001
        s.rollback()
        from . import mailer
        return _redirect(back, f"Nicht gesendet: {mailer.explain(e) if not isinstance(e, sepa.SepaError) else e}")
    return _redirect(back, msg or "Nichts zu tun.")


# --------------------------------------------------------------------------- Monatsablauf (Phase)
def cycle_status(s: Session, bld: Building, today: Optional[date] = None) -> dict:
    """Wo steht der Monatsablauf? Sammelauftrag → Ausführung → Kontoabgleich → Gutschriften.
    Bis zum Versandtag zählt der laufende Monat, danach der Folgemonat."""
    today = today or date.today()
    day = bld.sepa_mail_day or 25
    def export_for(m):
        return s.query(SepaExport).filter(SepaExport.building_id == bld.id, SepaExport.kind == "in",
                                          SepaExport.exec_date >= m, SepaExport.exec_date < _add_months(m, 1)) \
            .order_by(SepaExport.id.desc()).first()

    month = today.replace(day=1)
    if today.day >= day or export_for(month) is None:  # laufender Monat ohne Sammelauftrag → Folgemonat
        month = _add_months(month, 1)
    ym = f"{month:%Y-%m}"
    send_on = date(_add_months(month, -1).year, _add_months(month, -1).month, day)
    ex = export_for(month)
    lines_open = len(open_lines(s, bld))
    refunds = [r for r in open_refunds(s, bld) if not r["probable"] and r["amount"] >= 0.01] if scope.is_super() else []
    steps = []
    if ex:
        sent = " · per E-Mail versandt" if bld.sepa_mail_last == ym else ""
        steps.append(("Sammelauftrag erzeugt", "done", f"{_de(ex.total)} € · {ex.count} Überweisungen{sent}"))
    elif bld.sepa_mail:
        steps.append(("Sammelauftrag erzeugt", "todo", f"wird am {send_on:%d.%m.%Y} automatisch per E-Mail versandt"))
    else:
        steps.append(("Sammelauftrag erzeugt", "todo", "unten erzeugen oder Versand per E-Mail einschalten"))
    if ex and ex.status == "done":
        steps.append(("Hochgeladen & ausgeführt", "done", f"bestätigt {ex.confirmed_at:%d.%m.%Y}" if ex.confirmed_at else "bestätigt"))
    elif ex:
        steps.append(("Hochgeladen & ausgeführt", "todo", f"im Online-Banking hochladen · Ausführung {ex.exec_date:%d.%m.%Y}"))
    else:
        steps.append(("Hochgeladen & ausgeführt", "todo", "nach dem Erzeugen"))
    statement_ok = ex is not None and bld.bank_reserve_date is not None and bld.bank_reserve_date >= ex.exec_date
    if statement_ok and not lines_open:
        steps.append(("Kontoabgleich", "done", f"Kontoauszug bis {bld.bank_reserve_date:%d.%m.%Y} zugeordnet"))
    elif statement_ok:
        steps.append(("Kontoabgleich", "todo", f"{lines_open} Umsätze zuordnen"))
    else:
        steps.append(("Kontoabgleich", "todo", "Kontoauszug nach der Ausführung einlesen"
                      + (f" (bisher bis {bld.bank_reserve_date:%d.%m.%Y})" if bld.bank_reserve_date else "")))
    if refunds:
        steps.append(("Gutschriften fürs Hauptkonto", "todo",
                      f"{len(refunds)} offen · {_de(sum(r['amount'] for r in refunds))} € – "
                      + ("Überweisung vorbereiten" if bld.reserve_no_file else "Sammelauftrag Gutschriften erzeugen")))
    else:
        steps.append(("Gutschriften fürs Hauptkonto", "done", "nichts offen"))
    now = next((i for i, st in enumerate(steps) if st[1] != "done"), None)
    if now is not None:
        steps[now] = (steps[now][0], "now", steps[now][2])
        # vor dem Versandtag ist beim ersten Schritt noch nichts zu tun
        waiting = now == 0 and not ex and bld.sepa_mail and today < send_on
        phase = ("Warten auf Versand am " + f"{send_on:%d.%m.}") if waiting else steps[now][0]
    else:
        phase = "Alles erledigt"
    return {"month": month, "steps": steps, "phase": phase, "done": now is None}


@router.post("/admin/reserves/export/{eid}/manual")
def export_to_manual(eid: int, s: Session = Depends(get_session)):
    """Gutschrift-Datei, die sich nicht hochladen lässt (Sparkonto), in eine Überweisung von Hand umwandeln."""
    ex = _export(s, eid)
    back = f"/admin/reserves?building={ex.building_id}"
    if ex.status == "done" or ex.kind != "out":
        return _redirect(back, "Nur noch nicht ausgeführte Gutschriften lassen sich umwandeln.")
    if not ex.manual:
        es = s.query(ReserveEntry).filter(ReserveEntry.export_id == ex.id).all()
        ex.manual, ex.token = True, new_token(s)
        ex.manual_purpose = manual_purpose(ex, [(e.note, -e.amount) for e in es])
        s.commit()
    return _redirect(f"{back}&dl={ex.id}", f"Überweisungsvorlage mit Kennung {ex.token} – bitte so ausführen.")
