"""Kassenprüfung für Rücklagen, Konten und Budgets – Regelprüfung in der App plus Bewertung durch ein KI-Modell
(OpenAI) mit pseudonymisierten Daten.

Datenschutz:
- Es gehen keine Bankdaten raus: keine IBAN, keine Namen, keine Verwendungszwecke, keine Gebäude- oder Topfnamen.
- Töpfe, Empfänger, Konten und Gebäude werden zu zufälligen Kennungen (z. B. ``T-418``); die Zuordnung bleibt lokal.
- Beträge werden mit einem geheimen Zufallsfaktor verzerrt („VE“), Zeitangaben sind Tage relativ zum Stichtag.
- Vor dem Versand prüft die App die Anfrage auf echte Namen/IBANs und bricht im Zweifel ab.
- Die Antwort ist strukturiert (JSON-Schema); Kennungen und Beträge werden lokal wieder aufgelöst.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from . import secretbox, sepa
from .accounts import _page, _redirect, current
from .db import (AuditReport, Building, FinTxn, Party, ReservePlan, SepaExport, SessionLocal, User, get_session,
                 get_settings, save_settings)

log = logging.getLogger(__name__)
router = APIRouter()

OPENAI_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.6"
TIMEOUT = 180
SEVERITY = {"hoch": 0, "mittel": 1, "niedrig": 2}
AREAS = ["ruecklagen", "abgleich", "konto", "budget", "auffaelligkeit", "sonstiges"]
AREA_LABEL = {"ruecklagen": "Rücklagen", "abgleich": "Kontoabgleich", "konto": "Konto & Spielraum",
              "budget": "Budgets", "auffaelligkeit": "Auffälligkeit", "sonstiges": "Sonstiges"}
KINDS = {"T": "Rücklagentopf", "E": "Empfänger", "K": "Konto", "G": "Gebäude", "C": "Kategorie", "B": "Buchung"}
GENERIC_AREAS = {"IMMOBILIE", "FAHRZEUG", "PRIVAT", "HAUSHALT", "FAMILIE", "KINDER", "IT", "FREIZEIT", "GESUNDHEIT"}
CLASSES = [  # Stichwort im Topfnamen → allgemeine Art (statt des Namens)
    (r"GRUNDSTEUER", "Grundsteuer"), (r"WOHNGEB|GEBAEUDEVERS|GEBÄUDEVERS", "Gebäudeversicherung"),
    (r"VERSICHER|HAFTPFLICHT|KASKO", "Versicherung"), (r"KFZ.?STEUER|STEUER", "Steuer"),
    (r"HU.?AU|TÜV|TUEV|INSPEKTION|WARTUNG|REIFEN", "Fahrzeug: Prüfung/Wartung"), (r"ANHÄNGER|ANHAENGER", "Anhänger"),
    (r"WASSER|ABWASSER", "Wasser/Abwasser"), (r"KAMIN|SCHORNSTEIN", "Kaminkehrer"), (r"MÜLL|MUELL|ABFALL", "Abfall"),
    (r"IPTV|INTERNET|TELEFON|MOBIL", "Telekommunikation"), (r"DOMAIN|HOSTING|SERVER|HETZNER|STRATO", "IT/Hosting"),
    (r"URLAUB|REISE", "Urlaub"), (r"STROM|GAS|HEIZ|ÖL\b|OEL\b|PELLET", "Energie"), (r"REPARATUR|INSTANDH", "Instandhaltung"),
    (r"FREIES VERMÖGEN|FREIES VERMOEGEN|PUFFER", "Freie Reserve"), (r"GEZ|RUNDFUNK", "Rundfunkbeitrag"),
]


class AuditError(RuntimeError):
    pass


# --------------------------------------------------------------------------- Einstellungen
def api_key(s: Session) -> str:
    return secretbox.maybe_decrypt(get_settings(s).get("openai_key", "")) or ""


def model_name(s: Session) -> str:
    return (get_settings(s).get("openai_model") or DEFAULT_MODEL).strip()


# --------------------------------------------------------------------------- Pseudonymisierung
class Pseudo:
    """Zufällige Kennungen je Prüfung, geheimer Betragsfaktor; Zuordnung bleibt in der App."""

    def __init__(self, today: Optional[date] = None, factor: Optional[float] = None):
        rnd = random.SystemRandom()
        self.today = today or date.today()
        self.k = factor or round(rnd.uniform(0.8, 1.25), 4)
        self.labels: dict[str, str] = {}
        self._keys: dict[tuple, str] = {}
        self._free = {kind: rnd.sample(range(100, 1000), 900) for kind in KINDS}

    def tok(self, kind: str, key, label: str) -> str:
        if (kind, key) not in self._keys:
            t = f"{kind}-{self._free[kind].pop()}"
            self._keys[(kind, key)] = t
            self.labels[t] = label
        return self._keys[(kind, key)]

    def amt(self, x) -> float:
        return round(float(x or 0) * self.k, 4)  # 4 Stellen: Rückrechnung centgenau

    def back(self, y) -> float:
        return round(float(y or 0) / self.k, 2)

    def rel(self, d: Optional[date]) -> Optional[int]:
        return (d - self.today).days if d else None

    def resolve(self, text: str) -> str:
        return re.sub(r"\b([A-Z])-(\d{3})\b", lambda m: self.labels.get(m.group(0), m.group(0)), text or "")


def classify(name: str) -> dict:
    up = (name or "").upper()
    area = up.split("·")[0].strip() if "·" in up else ""
    classes = sorted({c for rx, c in CLASSES if re.search(rx, up)})
    return {"bereich": area.title() if area in GENERIC_AREAS else "", "art": classes}


def _de(x: float) -> str:
    return f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _finding(titel, bereich, schwere, refs, werte, beschreibung, massnahme, quelle="Regel") -> dict:
    return {"titel": titel, "bereich": bereich, "schwere": schwere, "refs": refs, "werte": werte,
            "beschreibung": beschreibung, "massnahme": massnahme, "quelle": quelle}


# --------------------------------------------------------------------------- Daten sammeln + Regelprüfung
def collect(s: Session, p: Pseudo) -> tuple[dict, list[dict]]:
    """→ (pseudonymisierte Daten für die KI, Befunde der Regelprüfung mit echten Namen/Beträgen)."""
    from . import finance, reserves

    today = p.today
    data: dict = {"stichtag": 0, "einheit": "VE", "gebaeude": [], "finanzen": {}}
    rules: list[dict] = []

    for bld in s.query(Building).order_by(Building.id):
        g = p.tok("G", bld.id, bld.label)
        bal = reserves.balances(s, bld)
        stats = {r["plan"].id: r for r in reserves.plan_stats(s, bld, bal)}
        pots = []
        pot_sum = 0.0
        for r in bal:
            pot_sum += r["total"]
            plan = None
            if r["ref"].startswith("rp:"):
                plan = s.get(ReservePlan, int(r["ref"][3:]))
            t = p.tok("T", r["ref"], r["name"])
            item = {"id": t, **classify(r["name"]), "stand": p.amt(r["total"]),
                    "vorgemerkt_zu": p.amt(r["planned_in"]), "vorgemerkt_ab": p.amt(-r["planned_out"]),
                    "zufuehrungen": p.amt(r["in"]), "entnahmen": p.amt(-r["out"])}
            if plan is not None:
                st = stats.get(plan.id, {})
                item.update({"quelle": "fester Topf", "kategorie": plan.category, "finanzierung": plan.funding,
                             "geschuetzt": plan.funding == "manuell", "aktiv": plan.active,
                             "intervall_monate": plan.interval or 1, "soll_jahr": p.amt(plan.annual_amount),
                             "soll_monat": p.amt(st.get("soll", 0)), "ist_monat_12m": p.amt(st.get("ist", 0)),
                             "entnahmen_12m": p.amt(st.get("out12", 0))})
                if plan.active and plan.funding == "sepa" and st.get("soll") and st.get("ist", 0) < st["soll"] * 0.8 \
                        and (plan.created_at or datetime.now()) < datetime.now() - timedelta(days=120):
                    rules.append(_finding("Topf wird zu wenig angespart", "ruecklagen", "mittel", [t],
                                          [{"bezeichnung": "Soll je Monat", "betrag": st["soll"]},
                                           {"bezeichnung": "Ist je Monat (12 Monate)", "betrag": st["ist"]}],
                                          f"{r['name']}: Zuführungen der letzten 12 Monate liegen unter dem Soll.",
                                          "Monatsrate prüfen bzw. fehlende Zuführungen nachholen."))
            else:
                item["quelle"] = "Nebenkosten (zeitversetzte Zahlung)"
            if r["total"] < -0.005:
                rules.append(_finding("Topf im Minus", "ruecklagen", "hoch", [t],
                                      [{"bezeichnung": "Stand", "betrag": r["total"]}],
                                      f"{r['name']} steht im Minus – es wurde mehr entnommen als angespart.",
                                      "Fehlbetrag aus „Freies Vermögen“ umbuchen oder Rate erhöhen."))
            pots.append(item)
        acct = {}
        if bld.account_iban:
            acct["hauptkonto"] = {"id": p.tok("K", sepa.normalize_iban(bld.account_iban), "Hauptkonto " + bld.label),
                                  "saldo": p.amt(bld.bank_main_balance) if bld.bank_main_balance is not None else None,
                                  "stand_tag": p.rel(bld.bank_main_date)}
        if bld.reserve_iban:
            acct["ruecklagenkonto"] = {"id": p.tok("K", sepa.normalize_iban(bld.reserve_iban), "Rücklagenkonto " + bld.label),
                                       "saldo": p.amt(bld.bank_reserve_balance) if bld.bank_reserve_balance is not None else None,
                                       "stand_tag": p.rel(bld.bank_reserve_date)}
        diff = None
        if bld.bank_reserve_balance is not None:
            diff = round(bld.bank_reserve_balance - pot_sum, 2)
            if abs(diff) >= 1:
                rules.append(_finding("Töpfe und Kontostand passen nicht zusammen", "abgleich",
                                      "hoch" if abs(diff) >= 50 else "mittel", [g],
                                      [{"bezeichnung": "Kontostand Rücklagenkonto", "betrag": bld.bank_reserve_balance},
                                       {"bezeichnung": "Summe der Töpfe", "betrag": round(pot_sum, 2)},
                                       {"bezeichnung": "Differenz", "betrag": diff}],
                                      f"{bld.label}: Der Kontostand ({bld.bank_reserve_date:%d.%m.%Y}) weicht von der "
                                      "Summe der virtuellen Töpfe ab." if bld.bank_reserve_date else
                                      f"{bld.label}: Kontostand weicht von der Summe der Töpfe ab.",
                                      "Offene Umsätze im Abgleich zuordnen; Rest einem Topf (z. B. Freies Vermögen) zuweisen."))
        lines = reserves.open_lines(s, bld)
        if lines:
            rules.append(_finding("Nicht zugeordnete Kontoumsätze", "abgleich", "mittel" if len(lines) > 3 else "niedrig",
                                  [g], [{"bezeichnung": "Summe", "betrag": round(sum(l.amount for l in lines), 2)}],
                                  f"{len(lines)} Umsätze des Rücklagen-/Hauptkontos sind keinem Topf zugeordnet.",
                                  "Unter Rücklagen → Kontoabgleich zuordnen oder ausblenden."))
        old = [e for e in s.query(SepaExport).filter(SepaExport.building_id == bld.id, SepaExport.status == "open")
               if e.created_at and e.created_at < datetime.now() - timedelta(days=7)]
        for e in old:
            rules.append(_finding("Auftrag nicht als ausgeführt bestätigt", "abgleich", "mittel",
                                  [p.tok("B", f"ex{e.id}", e.filename or f"Auftrag {e.id}")],
                                  [{"bezeichnung": "Summe", "betrag": e.total}],
                                  f"{e.filename or 'Auftrag'} vom {e.created_at:%d.%m.%Y} ist noch offen.",
                                  "Ausführung prüfen und bestätigen bzw. per FinTS einreichen – oder verwerfen."))
        refunds = [r for r in reserves.open_refunds(s, bld) if not r["probable"] and r["amount"] >= 0.01]
        if refunds:
            rules.append(_finding("Gutschriften fürs Hauptkonto offen", "ruecklagen", "niedrig", [g],
                                  [{"bezeichnung": "Summe", "betrag": round(sum(r["amount"] for r in refunds), 2)}],
                                  f"{len(refunds)} aus dem Hauptkonto bezahlte Rechnungen sind noch nicht aus der Rücklage erstattet.",
                                  "Unter Rücklagen → Offene Gutschriften einreichen."))
        data["gebaeude"].append({
            "id": g, "konten": acct, "toepfe": pots, "summe_toepfe": p.amt(pot_sum),
            "differenz_konto_minus_toepfe": p.amt(diff) if diff is not None else None,
            "offene_umsaetze": {"anzahl": len(lines), "summe": p.amt(sum(l.amount for l in lines))},
            "offene_auftraege": [{"id": p.tok("B", f"ex{e.id}", e.filename), "summe": p.amt(e.total),
                                  "alter_tage": (today - e.created_at.date()).days, "art": e.kind} for e in old],
            "offene_gutschriften": {"anzahl": len(refunds), "summe": p.amt(sum(r["amount"] for r in refunds))},
        })

    # ---- Finanzen (Hauptkonto, Budgets, Spielraum)
    d = finance.overview(s, today)
    if d["iban"]:
        defaults = {c[0] for c in finance.DEFAULTS}
        cat_id = lambda c: c.name if c.name in defaults else p.tok("C", c.id, c.name)  # noqa: E731
        txns = s.query(FinTxn).filter(FinTxn.account_iban == d["iban"],
                                      FinTxn.booking_date >= today - timedelta(days=190)).all()
        matcher = d["matcher"]
        months = defaultdict(lambda: defaultdict(float))
        for t in txns:
            if t.booking_date:
                c = matcher.category(t)
                idx = (t.booking_date.year - today.year) * 12 + t.booking_date.month - today.month
                months[idx][cat_id(c) if c else "unkategorisiert"] += t.amount
        budgets = [{"kategorie": cat_id(b["cat"]), "budget": p.amt(b["cat"].budget) if b["cat"].budget else None,
                    "verbraucht": p.amt(b["used"]), "schnitt_3m": p.amt(b["avg"]),
                    "rest": p.amt(b["rest"]) if b["rest"] is not None else None} for b in d["budgets"]]
        for b in d["budgets"]:
            if b["cat"].budget and b["used"] > b["cat"].budget:
                rules.append(_finding("Budget überschritten", "budget", "mittel", [cat_id(b["cat"])],
                                      [{"bezeichnung": "Budget", "betrag": b["cat"].budget},
                                       {"bezeichnung": "verbraucht", "betrag": b["used"]}],
                                      f"{b['cat'].name}: Budget im laufenden Zeitraum überschritten.",
                                      "Ausgaben bremsen oder Budget anpassen."))
        if d["room"] is not None and d["room"] < 0:
            rules.append(_finding("Konto rutscht voraussichtlich ins Minus", "konto", "hoch", [],
                                  [{"bezeichnung": "Spielraum", "betrag": d["room"]},
                                   {"bezeichnung": "tiefster Stand", "betrag": d["low"]}],
                                  f"Bis {d['end']:%d.%m.%Y} reicht das Geld nach den erwarteten Fixkosten nicht.",
                                  "Ausgaben verschieben oder Geld aus „Freies Vermögen“ holen."))
        # Empfänger als Kennung + Art
        def payee(t):
            c = matcher.category(t)
            return p.tok("E", (t.name or t.purpose[:20]).upper().strip(), t.name or t.purpose[:40]), (cat_id(c) if c else "unkategorisiert")
        seen_before = {(t.name or "").upper() for t in s.query(FinTxn.name).filter(
            FinTxn.account_iban == d["iban"], FinTxn.booking_date < today - timedelta(days=90))}
        avg = defaultdict(list)
        for t in txns:
            c = matcher.category(t)
            avg[c.id if c else 0].append(t.amount)
        anomalies = []
        recent = [t for t in txns if t.booking_date and t.booking_date >= today - timedelta(days=90)]
        for t in recent:
            c = matcher.category(t)
            vals = [v for v in avg[c.id if c else 0] if v < 0]
            mean = sum(vals) / len(vals) if vals else 0
            why = []
            if t.amount < -100 and mean and t.amount < 3 * mean and len(vals) >= 4:
                why.append("deutlich höher als üblich in der Kategorie")
            if t.amount < -200 and (t.name or "").upper() not in seen_before:
                why.append("neuer Empfänger mit größerem Betrag")
            dup = [o for o in recent if o is not t and o.name == t.name and abs(o.amount - t.amount) < 0.005
                   and o.booking_date and abs((o.booking_date - t.booking_date).days) <= 3 and o.id < t.id]
            if dup and t.amount < 0:
                why.append("möglicherweise doppelt (gleicher Empfänger und Betrag innerhalb von 3 Tagen)")
            if why:
                e, cat = payee(t)
                anomalies.append({"id": p.tok("B", f"tx{t.id}", f"{t.booking_date:%d.%m.%Y} {t.name} {_de(t.amount)} €"),
                                  "empfaenger": e, "kategorie": cat, "betrag": p.amt(t.amount),
                                  "tag": p.rel(t.booking_date), "vorgemerkt": bool(t.pending), "hinweis": why})
        recurring = []
        for r in d["recurring"][:40]:
            name = r.get("name", "")
            recurring.append({"empfaenger": p.tok("E", (name or "").upper().strip(), name),
                              "kategorie": cat_id(r["category"]) if r.get("category") else "unkategorisiert",
                              "art": r.get("kind", ""), "betrag": p.amt(r["amount"]), "tag_im_monat": r.get("day"),
                              "monate": r.get("months")})
        data["finanzen"] = {
            "konto": p.tok("K", d["iban"], "Hauptkonto (Finanzplanung)"),
            "saldo": p.amt(d["balance"]) if d["balance"] is not None else None, "stand_tag": p.rel(d["as_of"]),
            "vorgemerkt": {"anzahl": d["pending_count"], "summe": p.amt(d["pending_sum"])},
            "zeitraum": {"von_tag": p.rel(d["start"]), "bis_tag": p.rel(d["end"]), "modus": d["mode"]},
            "spielraum": p.amt(d["room"]) if d["room"] is not None else None,
            "tiefster_stand": p.amt(d["low"]) if d["low"] is not None else None, "puffer": p.amt(d["buffer"]),
            "erwartet_eingang": p.amt(d["exp_in"]), "erwartet_fixkosten": p.amt(d["exp_out"]),
            "budgets": budgets,
            "monatssummen_je_kategorie": {str(k): {c: p.amt(v) for c, v in sorted(cats.items())}
                                          for k, cats in sorted(months.items())},
            "wiederkehrend": recurring, "auffaellige_buchungen": anomalies[:40],
        }
    # Regelbefunde für die KI (pseudonymisiert, Texte ohne Namen)
    data["regelpruefung"] = [{"titel": f["titel"], "bereich": f["bereich"], "schwere": f["schwere"], "refs": f["refs"],
                              "werte": [{"bezeichnung": w["bezeichnung"], "betrag": p.amt(w["betrag"])} for w in f["werte"]]}
                             for f in rules]
    return data, rules


# --------------------------------------------------------------------------- Datenschutz-Prüfung
def sensitive_values(s: Session) -> set[str]:
    out: set[str] = set()
    for b in s.query(Building):
        out |= {b.name, b.address, b.code, b.account_name, b.reserve_name, b.label}
        out |= {sepa.normalize_iban(b.account_iban), sepa.normalize_iban(b.reserve_iban)}
    for p in s.query(Party):
        out |= {p.name, p.address, getattr(p, "email", ""), getattr(p, "phone", "")}
    for u in s.query(User):
        out |= {u.name, u.email, u.username}
    for pl in s.query(ReservePlan):
        out |= {pl.name, pl.label}
    try:
        from .db import BankAccess

        for a in s.query(BankAccess):
            out |= {a.login, a.customer_id}
            for x in a.accounts or []:
                out |= {x.get("iban", ""), x.get("owner", "")}
    except Exception:  # noqa: BLE001
        pass
    for (name,) in s.query(FinTxn.name).distinct():
        out.add(name)
    for (iban,) in s.query(FinTxn.iban).distinct():
        out.add(iban)
    from .finance import DEFAULTS

    generic = {c[0].upper() for c in DEFAULTS} | {c.upper() for _, c in CLASSES} | GENERIC_AREAS
    return {v.strip() for v in out if v and len(v.strip()) >= 5 and v.strip().upper() not in generic}


def leaks(payload: dict, values: set[str]) -> list[str]:
    text = json.dumps(payload, ensure_ascii=False).upper()
    return sorted(v for v in values if v.upper() in text)


# --------------------------------------------------------------------------- KI-Aufruf
SYSTEM = """Du bist ein sorgfältiger Kassenprüfer für einen privaten Haushalt mit vermieteter Immobilie.
Du erhältst pseudonymisierte Daten: Kennungen wie T-123 (Rücklagentopf), E-456 (Empfänger), K-… (Konto), G-… (Gebäude),
C-… (Kategorie), B-… (Buchung/Auftrag). Beträge sind in einer verschleierten Einheit „VE“ angegeben – Verhältnisse und
Vorzeichen stimmen, die absolute Höhe nicht. Zeitangaben sind Tage relativ zum Stichtag (0 = heute, negativ = vergangen),
Monatsschlüssel relativ zum laufenden Monat (0 = dieser Monat, -1 = Vormonat).

Prüfe insbesondere:
- Reichen die Rücklagentöpfe für ihre Zwecke (Intervall, Soll/Ist, Entnahmen)? Unterdeckung, Überdeckung, geschützte Töpfe.
- Stimmen virtuelle Töpfe und Kontostand überein; offene Umsätze, Aufträge, Gutschriften.
- Budgets, Spielraum, erwartete Fixkosten; Trend der Monatssummen je Kategorie.
- Auffälligkeiten: Doppelzahlungen, Ausreißer, neue Empfänger, Preissteigerungen wiederkehrender Zahlungen.
Die Regelprüfung der App ist enthalten – bewerte und ergänze sie, wiederhole sie nicht nur.

Antwortregeln: Nenne Objekte ausschließlich über ihre Kennung. Schreibe in Texten KEINE Beträge und keine Zahlen
mit Währungsbezug – Beträge gehören ins Feld „werte“ (in VE). Rate keine echten Namen. Antworte auf Deutsch,
knapp und konkret, mit umsetzbaren Maßnahmen. Wenn alles in Ordnung ist, sage das."""

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["gesamturteil", "zusammenfassung", "befunde"],
    "properties": {
        "gesamturteil": {"type": "string", "enum": ["in_ordnung", "hinweise", "kritisch"]},
        "zusammenfassung": {"type": "string"},
        "befunde": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["titel", "bereich", "schwere", "refs", "werte", "beschreibung", "massnahme"],
            "properties": {
                "titel": {"type": "string"},
                "bereich": {"type": "string", "enum": AREAS},
                "schwere": {"type": "string", "enum": ["hoch", "mittel", "niedrig"]},
                "refs": {"type": "array", "items": {"type": "string"}},
                "werte": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["bezeichnung", "betrag"],
                    "properties": {"bezeichnung": {"type": "string"}, "betrag": {"type": "number"}}}},
                "beschreibung": {"type": "string"},
                "massnahme": {"type": "string"},
            }}},
    },
}


def call_openai(key: str, model: str, payload: dict) -> dict:
    body = {"model": model, "store": False,
            "input": [{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "text": {"format": {"type": "json_schema", "name": "kassenpruefung", "strict": True, "schema": SCHEMA}}}
    try:
        r = httpx.post(f"{OPENAI_URL}/responses", json=body, timeout=TIMEOUT,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        raise AuditError(f"OpenAI nicht erreichbar: {e}") from e
    try:
        data = r.json()
    except ValueError:
        raise AuditError(f"OpenAI: unerwartete Antwort (HTTP {r.status_code})")
    if r.status_code >= 400:
        msg = (data.get("error") or {}).get("message") if isinstance(data, dict) else ""
        raise AuditError(f"OpenAI-Fehler (HTTP {r.status_code}): {msg or r.text[:200]}")
    for item in data.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") == "refusal":
                raise AuditError(f"Das Modell hat abgelehnt: {c.get('refusal', '')[:200]}")
            if c.get("type") == "output_text":
                try:
                    return json.loads(c["text"])
                except (ValueError, KeyError) as e:
                    raise AuditError("Antwort des Modells ist kein gültiges JSON.") from e
    raise AuditError("Antwort des Modells enthält keinen Text.")


def list_models(key: str) -> list[str]:
    r = httpx.get(f"{OPENAI_URL}/models", headers={"Authorization": f"Bearer {key}"}, timeout=30)
    if r.status_code >= 400:
        raise AuditError(f"Modelle nicht abrufbar (HTTP {r.status_code})")
    ids = [m.get("id", "") for m in r.json().get("data", [])]
    return sorted(i for i in ids if re.match(r"^(gpt|o\d)", i) and not re.search(r"audio|realtime|tts|image|transcribe|search", i))


def resolve(answer: dict, p: Pseudo) -> dict:
    """Kennungen → echte Namen, VE → Euro."""
    out = {"gesamturteil": answer.get("gesamturteil", "hinweise"),
           "zusammenfassung": p.resolve(answer.get("zusammenfassung", "")), "befunde": []}
    for f in answer.get("befunde", []):
        out["befunde"].append({
            "titel": p.resolve(f.get("titel", "")), "bereich": f.get("bereich", "sonstiges"),
            "schwere": f.get("schwere", "niedrig"),
            "refs": [p.labels.get(r, r) for r in f.get("refs", [])],
            "werte": [{"bezeichnung": p.resolve(w.get("bezeichnung", "")), "betrag": p.back(w.get("betrag"))}
                      for w in f.get("werte", [])],
            "beschreibung": p.resolve(f.get("beschreibung", "")), "massnahme": p.resolve(f.get("massnahme", "")),
            "quelle": "KI"})
    return out


# --------------------------------------------------------------------------- Prüfung durchführen
def preview(s: Session) -> tuple[dict, list[str], Pseudo]:
    p = Pseudo()
    payload, _ = collect(s, p)
    return payload, leaks(payload, sensitive_values(s)), p


def run(s: Session, user: str = "", use_ai: bool = True) -> AuditReport:
    p = Pseudo()
    payload, rules = collect(s, p)
    rep = AuditReport(created_by=user, model="", status="ok", payload=payload, findings=[], done=[])
    findings = [dict(f, refs=[p.labels.get(r, r) for r in f["refs"]]) for f in rules]  # lokal: echte Namen
    verdict = "kritisch" if any(f["schwere"] == "hoch" for f in rules) else ("hinweise" if rules else "in_ordnung")
    summary = ""
    key = api_key(s) if use_ai else ""
    if key:
        found = leaks(payload, sensitive_values(s))
        if found:
            rep.status, rep.error = "blocked", ("Datenschutzprüfung: Die Anfrage enthielt echte Angaben ("
                                                + ", ".join(v[:3] + "…" for v in found[:5]) + ") – nicht gesendet.")
        else:
            rep.model = model_name(s)
            try:
                ans = resolve(call_openai(key, rep.model, payload), p)
                summary, verdict = ans["zusammenfassung"], ans["gesamturteil"]
                findings += ans["befunde"]
            except AuditError as e:
                rep.status, rep.error = "error", str(e)
    elif use_ai:
        rep.error = "Kein OpenAI-API-Schlüssel hinterlegt – nur Regelprüfung."
    findings.sort(key=lambda f: (SEVERITY.get(f["schwere"], 3), f["quelle"] != "Regel"))
    rep.findings, rep.verdict, rep.summary = findings, verdict, summary
    s.add(rep)
    s.commit()
    return rep


def monthly_tick(today: Optional[date] = None) -> Optional[int]:
    """Automatisch einmal im Monat (ab dem 2.) prüfen, wenn eingeschaltet; Ergebnis per Push."""
    from . import notify

    today = today or date.today()
    with SessionLocal() as s:
        st = get_settings(s)
        if st.get("audit_monthly") != "1" or today.day < 2:
            return None
        start = datetime(today.year, today.month, 1)
        if s.query(AuditReport).filter(AuditReport.created_at >= start).first() is not None:
            return None
        rep = run(s, "Automatik")
        high = sum(1 for f in rep.findings if f["schwere"] == "hoch")
        try:
            notify.to_admins(s, "push_admin_errors" if high else "push_admin_sent", "Kassenprüfung fertig",
                             f"{len(rep.findings)} Befunde, davon {high} wichtig" + (f" · {rep.error}" if rep.error else ""),
                             url=f"/admin/pruefung/{rep.id}", tag="audit")
        except Exception:  # noqa: BLE001
            pass
        return rep.id


_lock = threading.Lock()


def monthly_background() -> None:
    if _lock.locked():
        return

    def go():
        if not _lock.acquire(blocking=False):
            return
        try:
            monthly_tick()
        except Exception:  # noqa: BLE001
            log.exception("Kassenprüfung")
        finally:
            _lock.release()

    threading.Thread(target=go, daemon=True, name="audit").start()


# --------------------------------------------------------------------------- Web
VERDICT = {"in_ordnung": ("✅", "In Ordnung"), "hinweise": ("ℹ️", "Hinweise"), "kritisch": ("⚠️", "Kritisch")}


def _ctx(s: Session) -> dict:
    st = get_settings(s)
    key = api_key(s)
    return {"st": st, "has_key": bool(key), "key_hint": ("…" + key[-4:]) if key else "", "model": model_name(s),
            "reports": s.query(AuditReport).order_by(AuditReport.id.desc()).limit(20).all(),
            "verdicts": VERDICT, "areas": AREA_LABEL, "de": _de}


@router.get("/admin/pruefung", response_class=HTMLResponse)
def audit_page(request: Request, show: str = "", s: Session = Depends(get_session)):
    ctx = _ctx(s)
    if show == "vorschau":
        payload, found, _ = preview(s)
        ctx.update(preview=json.dumps(payload, ensure_ascii=False, indent=2), leaks=found)
    rep = ctx["reports"][0] if ctx["reports"] else None
    return _page(request, "audit.html", rep=rep, **ctx)


@router.get("/admin/pruefung/{rid}", response_class=HTMLResponse)
def audit_view(request: Request, rid: int, s: Session = Depends(get_session)):
    rep = s.get(AuditReport, rid)
    if rep is None:
        raise HTTPException(404)
    return _page(request, "audit.html", rep=rep, **_ctx(s))


@router.post("/admin/pruefung/run")
async def audit_run(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    me = current(request)
    rep = run(s, me.display if me else "", use_ai=not form.get("nur_regeln"))
    msg = f"Prüfung fertig: {len(rep.findings)} Befunde" + (f" · {rep.error}" if rep.error else "")
    return _redirect(f"/admin/pruefung/{rep.id}", msg)


@router.post("/admin/pruefung/{rid}/done")
async def audit_done(request: Request, rid: int, s: Session = Depends(get_session)):
    rep = s.get(AuditReport, rid)
    if rep is None:
        raise HTTPException(404)
    form = await request.form()
    i = int(form.get("i", -1))
    done = list(rep.done or [])
    done = [x for x in done if x != i] if i in done else done + [i]
    rep.done = done
    s.commit()
    return _redirect(f"/admin/pruefung/{rid}#b{i}")


@router.post("/admin/pruefung/settings")
async def audit_settings(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    data = {"openai_model": re.sub(r"[^A-Za-z0-9._:-]", "", str(form.get("openai_model", "")))[:60] or DEFAULT_MODEL,
            "audit_monthly": "1" if form.get("audit_monthly") else ""}
    key = str(form.get("openai_key", "")).strip()
    if form.get("forget_key"):
        data["openai_key"] = ""
    elif key:
        if not key.startswith("sk-"):
            return _redirect("/admin/pruefung", "Das sieht nicht nach einem OpenAI-API-Schlüssel aus (beginnt mit „sk-“).")
        data["openai_key"] = secretbox.encrypt(key)
    save_settings(s, data)
    msg = "Gespeichert"
    if form.get("test") and (k := api_key(s)):
        try:
            models = list_models(k)
            msg += f" · Schlüssel gültig, {len(models)} Modelle" + ("" if data["openai_model"] in models else
                                                                    f" – „{data['openai_model']}“ nicht in der Liste, bitte prüfen")
        except (AuditError, httpx.HTTPError) as e:
            msg += f" · Test fehlgeschlagen: {e}"
    return _redirect("/admin/pruefung", msg)
