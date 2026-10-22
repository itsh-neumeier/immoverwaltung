"""Rücklagen für zeitversetzt bezahlte Kosten (z. B. Wassergeld, IPTV, Kaminkehrer).

Die Mieter zahlen diese Posten monatlich mit der Nebenkostenabrechnung, die Rechnung des Versorgers kommt aber erst
später. Je abgeschlossener Abrechnung, Partei und Position wird der Betrag per SEPA-Sammelüberweisung (pain.001) vom
Girokonto aufs Rücklagenkonto übertragen; das Rücklagenbuch zeigt den Stand je Position (Zuführungen − Entnahmen).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.orm import Session

from . import render as rnd, scope, sepa
from .accounts import _page, _redirect, current
from .db import Allocation, Billing, Building, FixedCost, Party, ReserveEntry, SepaExport, get_session

router = APIRouter()
KINDS = {"in": "Zuführung", "out": "Auszahlung / Entnahme", "start": "Anfangsbestand"}


@dataclass
class Position:
    ref: str
    name: str
    label: str  # Zweck im Verwendungszweck (Großbuchstaben)


@dataclass
class Item:
    key: str
    billing: Billing
    party: dict
    pos: Position
    amount: float
    purpose: str


def positions(s: Session) -> dict[str, Position]:
    out = {}
    for f in s.query(FixedCost).filter(FixedCost.reserve.is_(True)).order_by(FixedCost.id):
        out[f"fc:{f.id}"] = Position(f"fc:{f.id}", f.name, (f.reserve_label or f.name).upper())
    for a in s.query(Allocation).filter(Allocation.reserve.is_(True)).order_by(Allocation.id):
        out[f"al:{a.id}"] = Position(f"al:{a.id}", a.name, (a.reserve_label or a.name).upper())
    return out


def all_positions(s: Session) -> dict[str, str]:
    """Namen aller Positionen (auch ohne Rücklage) – für Buchungen, deren Position später abgeschaltet wurde."""
    names = {f"fc:{f.id}": f.name for f in s.query(FixedCost)}
    names.update({f"al:{a.id}": a.name for a in s.query(Allocation)})
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


def pending(s: Session, building: Building) -> list[Item]:
    """Noch nicht zurückgelegte Beträge je Abrechnung × Partei × Position."""
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
                    items.append(Item(f"{b.id}:{rp['id']}:{ref}", b, rp, p, amt, purpose(b, rp, p, amt, building)))
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
    for ref, p in positions(s).items():
        rows.setdefault(ref, blank(ref, p.name))
    for r in rows.values():
        for k in ("in", "out", "total", "planned_in", "planned_out"):
            r[k] = round(r[k], 2)
    return sorted(rows.values(), key=lambda r: r["name"].lower())


def default_exec_date(items: list[Item]) -> date:
    """Ausführung frühestens am nächsten Bankarbeitstag, sonst zur spätesten Fälligkeit der Mieterzahlungen."""
    earliest = sepa.next_business_day()
    due = max((rnd.due_date(i.billing) for i in items), default=earliest)
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
def reserves_page(request: Request, building: int = 0, dl: int = 0, s: Session = Depends(get_session)):
    bld = _building(s, building)
    items = pending(s, bld)
    names = all_positions(s)
    entries = s.query(ReserveEntry).filter(ReserveEntry.building_id == bld.id) \
        .order_by(ReserveEntry.booking_date.desc(), ReserveEntry.id.desc()).limit(300).all()
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
    return _page(request, "reserves.html", open_ids=open_ids, bld=bld, buildings=scope.filter_query(s.query(Building), Building.id)
                 .order_by(Building.sort, Building.id).all(), items=items, total=round(sum(i.amount for i in items), 2),
                 positions=positions(s), names=names, balances=balances(s, bld), entries=entries, exports=exports,
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
    fmt = str(form.get("sepa_format", ""))
    bld.sepa_format = fmt if fmt in sepa.FORMATS else "pain.001.001.09"
    s.commit()
    msg = "Gespeichert" + (" – bitte prüfen: " + "; ".join(errors) if errors else "")
    return _redirect(f"/admin/reserves?building={bld.id}", msg)


def _store_export(s: Session, request: Request, bld: Building, kind: str, xml: bytes, msg_id: str,
                  exec_date: date, count: int, total: float) -> SepaExport:
    code = re.sub(r"[^A-Za-z0-9]+", "", gid(bld.code)) or str(bld.id)
    me = current(request)
    ex = SepaExport(building_id=bld.id, kind=kind, msg_id=msg_id, exec_date=exec_date, count=count,
                    total=round(total, 2), xml=xml.decode("utf-8"), created_by=me.display if me else "")
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


@router.post("/admin/reserves/export")
async def export(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    bld = _building(s, form.get("building"))
    keys = set(form.getlist("items"))
    items = [i for i in pending(s, bld) if i.key in keys]
    if not items:
        return _redirect(f"/admin/reserves?building={bld.id}", "Bitte mindestens einen Posten auswählen.")
    exec_date = _date(form.get("exec_date"), default_exec_date(items))
    debtor, creditor = accounts(bld)
    try:
        msg_id, xml = sepa.credit_transfer(
            debtor, creditor, [sepa.Transfer(i.amount, i.purpose, f"RL{i.billing.id}P{i.party['id']}{i.pos.ref.replace(':', '')}")
                               for i in items], exec_date, bld.sepa_format or "pain.001.001.09")
    except sepa.SepaError as e:
        return _redirect(f"/admin/reserves?building={bld.id}", f"SEPA-Datei nicht erzeugt: {e}")
    ex = _store_export(s, request, bld, "in", xml, msg_id, exec_date, len(items), sum(i.amount for i in items))
    for i in items:
        s.add(ReserveEntry(building_id=bld.id, ref=i.pos.ref, name=i.pos.name, booking_date=exec_date, amount=i.amount,
                           kind="in", billing_id=i.billing.id, party_id=i.party["id"], export_id=ex.id,
                           note=i.purpose))
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
        label = next((p.label for r, p in positions(s).items() if r == ref), names[ref].upper())
        code = f"GID: {gid(bld.code)} " if bld.code else ""
        text = f"AUSZAHLUNG RUECKLAGE {code}{label} {_de(amount)} EUR {note}".upper()
        exec_date = when if when > date.today() and sepa.is_business_day(when) else sepa.next_business_day()
        try:
            msg_id, xml = sepa.credit_transfer(creditor, debtor, [sepa.Transfer(amount, text)], exec_date,
                                               bld.sepa_format or "pain.001.001.09")
        except sepa.SepaError as err:
            s.rollback()
            return _redirect(back, f"SEPA-Datei nicht erzeugt: {err}")
        ex = _store_export(s, request, bld, "out", xml, msg_id, exec_date, 1, amount)
        e.export_id = ex.id
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
