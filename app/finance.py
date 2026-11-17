"""Private Finanzplanung fürs Hauptkonto: Budgets je Kategorie und „Spielraum“ – wie viel bis Monatsende (bzw. bis
zum nächsten Gehalt) noch ausgegeben werden kann, ohne ins Minus zu gehen.

Grundlage sind die Umsätze und Kontostände aus dem CAMT-/CSV-Kontoauszug (derselbe Upload wie bei den Rücklagen).
* Kategorien ordnen Umsätze per Regel zu (Empfängername, ``zweck:…``, ``iban:…``); von Hand gesetzte Kategorie hat Vorrang.
* Wiederkehrende Einnahmen/Fixkosten (mind. 3 der letzten 4 Monate, ähnlicher Betrag) werden erkannt und – sofern im
  laufenden Zeitraum noch nicht gebucht – als erwartet eingeplant.
* Spielraum = tiefster voraussichtlicher Kontostand bis Periodenende (Kontostand + erwartete Eingänge − erwartete
  Fixkosten, tagesgenau) minus Puffer. Davon gehen die Rest-Budgets ab → „frei“.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Optional

from sqlalchemy.orm import Session

from . import sepa
from .db import Building, FinBalance, FinCategory, FinTxn, get_settings, save_settings

KINDS = {"variabel": "Budget (variabel)", "fix": "Fixkosten", "einnahme": "Einnahmen", "ignorieren": "nicht zählen"}
OWN_RESERVE = "auto:ruecklagenkonto"

DEFAULTS = [  # Name, Art, Budget, Regeln (Empfängername; zweck:… im Verwendungszweck), Farbe
    ("Lebensmittel", "variabel", 0, "REWE, EDEKA, ALDI, LIDL, NETTO, PENNY, KAUFLAND, NORMA, GLOBUS, MARKTKAUF, "
     "E CENTER, TEGUT, DENNS, BAECKEREI, BAECKER, METZGEREI, METZGER, GETRAENKEMARKT, GETRAENKE, HOFLADEN", "#16a34a"),
    ("Drogerie & Apotheke", "variabel", 0, "DM, DM DROGERIE*, DROGERIEMARKT, ROSSMANN, MUELLER HANDELS, "
     "MULLER HANDELS, APOTHEKE, BUDNI",
     "#db2777"),
    ("Tanken", "variabel", 0, "ARAL, SHELL, ESSO, JET, TOTALENERGIES, AGIP, AVIA, OMV, TANKSTELLE", "#ea580c"),
    ("Online-Shopping", "variabel", 0, "AMAZON, PAYPAL, EBAY, ZALANDO, OTTO, TEMU, SHEIN, ADYEN", "#7c3aed"),
    ("Kleidung & Schuhe", "variabel", 0, "H M, TAKKO, ZARA, DEICHMANN, PRIMARK, KIK, SCHUHMODE, C A MODE", "#0891b2"),
    ("Haus & Garten", "variabel", 0, "OBI, HORNBACH, BAUHAUS, TOOM, HAGEBAU, IKEA, DEHNER, BAUMARKT", "#a16207"),
    ("Restaurant & Freizeit", "variabel", 0, "MCDONALDS, BURGER KING, LIEFERANDO, RESTAURANT, GASTHAUS, GASTHOF, "
     "PIZZERIA, CAFE, KINO", "#f59e0b"),
    ("Kinder", "variabel", 0, "SMYTHS, MYTOYS, BABYONE, SPIELWAREN, KITA, KINDERGARTEN", "#ec4899"),
    ("Bargeld", "variabel", 0, "GA NR*, zweck:BARGELDAUSZAHLUNG, zweck:GELDAUTOMAT, zweck:GA NR*", "#475569"),
    ("Kredit & Finanzierung", "fix", 0, "DARLEHEN, BAUSPARKASSE, SCHWAEBISCH HALL, WUESTENROT, LBS, zweck:DARLEHEN, "
     "zweck:TILGUNG", "#b91c1c"),
    ("Versicherungen", "fix", 0, "VERSICHERUNG, VERSICHERUNGEN, ALLIANZ, HUK, ERGO, DEVK, AXA, GOTHAER, DEBEKA, "
     "SIGNAL IDUNA, ZURICH, WWK", "#9333ea"),
    ("Energie, Telefon & Internet", "fix", 0, "TELEKOM, VODAFONE, TELEFONICA, AWATTAR, STADTWERKE, E ON", "#0d9488"),
    ("Abos & Mitgliedschaften", "fix", 0, "NETFLIX, SPOTIFY, DISNEY, APPLE, GOOGLE, MICROSOFT, HETZNER, STRATO, ADAC, "
     "FITNESS", "#6366f1"),
    ("Gebühren & Abgaben", "fix", 0, "KOMMUNALUNTERNEHMEN, LANDRATSAMT, LANDKREIS, FINANZAMT, GEMEINDE, "
     "BEITRAGSSERVICE, RUNDFUNK", "#64748b"),
    ("Rücklagen (Umbuchung)", "fix", 0, OWN_RESERVE, "#2563eb"),
    ("Gehalt", "einnahme", 0, "zweck:LOHN, zweck:GEHALT, zweck:BEZUEGE, zweck:ENTGELTABRECHNUNG", "#15803d"),
    ("Kindergeld & Elterngeld", "einnahme", 0, "FAMILIENKASSE, ELTERNGELD, ZBFS", "#22c55e"),
    ("Mieteinnahmen", "einnahme", 0, "zweck:MIETE", "#65a30d"),
]


def norm(text: str) -> str:
    """Großbuchstaben, Umlaute ausgeschrieben, Sonderzeichen → Leerzeichen."""
    t = sepa.clean(text or "", 500, umlauts=False).upper()
    return " " + " ".join(re.sub(r"[^A-Z0-9]+", " ", t).split()) + " "


def seed(s: Session) -> None:
    st = get_settings(s)
    if st.get("fin_seeded"):
        return
    if s.query(FinCategory).first() is None:
        for i, (name, kind, budget, rules, color) in enumerate(DEFAULTS):
            s.add(FinCategory(name=name, kind=kind, budget=budget, rules=rules, color=color, sort=i))
    save_settings(s, {"fin_seeded": "1"})
    s.commit()


def finance_iban(s: Session) -> str:
    st = get_settings(s)
    if st.get("fin_iban"):
        return sepa.normalize_iban(st["fin_iban"])
    b = s.query(Building).filter(Building.account_iban != "").order_by(Building.id).first()
    return sepa.normalize_iban(b.account_iban) if b else ""


def reserve_ibans(s: Session) -> set:
    return {sepa.normalize_iban(b.reserve_iban) for b in s.query(Building) if b.reserve_iban}


# --------------------------------------------------------------------------- Zuordnung
@dataclass
class Rule:
    field: str  # name | purpose | iban | reserve
    text: str
    rx: Optional[re.Pattern] = None


def parse_rules(text: str) -> list[Rule]:
    out = []
    for raw in re.split(r"[,\n;]", text or ""):
        raw = raw.strip()
        if not raw:
            continue
        if raw.lower() == OWN_RESERVE:
            out.append(Rule("reserve", raw))
            continue
        field = "name"
        for prefix, f in (("zweck:", "purpose"), ("iban:", "iban"), ("name:", "name")):
            if raw.lower().startswith(prefix):
                field, raw = f, raw[len(prefix):].strip()
        if field == "iban":
            out.append(Rule("iban", sepa.normalize_iban(raw)))
            continue
        prefix = raw.endswith("*")  # „GA NR*“: Wortanfang genügt (z. B. GA NR00002317)
        words = norm(raw.rstrip("*")).strip()
        if words:
            tail = "" if prefix else r"(?![A-Z0-9])"
            out.append(Rule(field, words, re.compile(r"(?<![A-Z0-9])" + re.escape(words) + tail)))
    return out


class Matcher:
    def __init__(self, s: Session):
        self.cats = s.query(FinCategory).order_by(FinCategory.sort, FinCategory.id).all()
        self.by_id = {c.id: c for c in self.cats}
        self.rules = [(c, parse_rules(c.rules)) for c in self.cats]
        self.reserve = reserve_ibans(s)
        self.cache: dict = {}

    def category(self, t: FinTxn) -> Optional[FinCategory]:
        if t.category_id and t.category_id in self.by_id:
            return self.by_id[t.category_id]
        if t.id is not None and t.id in self.cache:
            return self.cache[t.id]
        c = self._match(t)
        if t.id is not None:
            self.cache[t.id] = c
        return c

    def _match(self, t: FinTxn) -> Optional[FinCategory]:
        name, purpose, iban = norm(t.name), norm(t.purpose), sepa.normalize_iban(t.iban)
        best, best_len = None, 0
        for c, rules in self.rules:
            for r in rules:
                if r.field == "reserve":
                    hit, size = iban in self.reserve, 99
                elif r.field == "iban":
                    hit, size = bool(iban) and iban == r.text, 98
                else:
                    hit, size = bool(r.rx.search(name if r.field == "name" else purpose)), len(r.text)
                    if r.field == "name" and not t.name.strip():  # ohne Empfänger: Regel im Zweck suchen
                        hit = bool(r.rx.search(purpose))
                if hit and size > best_len:
                    best, best_len = c, size
        return best


# --------------------------------------------------------------------------- Import
def ingest(s: Session, txns: list, balances: list) -> tuple[int, int]:
    """Umsätze und Kontostände des Hauptkontos übernehmen (Dubletten werden erkannt). → (neu, vorhanden)"""
    iban = finance_iban(s)
    if not iban:
        return 0, 0
    seen = {d for (d,) in s.query(FinTxn.digest).filter(FinTxn.account_iban == iban)}
    new = dup = 0
    for t in txns:
        if sepa.normalize_iban(t.account_iban) != iban:
            continue
        if t.digest in seen:
            dup += 1
            continue
        seen.add(t.digest)
        s.add(FinTxn(account_iban=iban, booking_date=t.booking_date, amount=t.amount, name=t.name[:200],
                     iban=t.iban[:34], purpose=t.purpose[:2000], digest=t.digest))
        new += 1
    known = {(b.day, round(b.amount, 2)) for b in s.query(FinBalance).filter(FinBalance.account_iban == iban)}
    for b in balances:
        if b.kind == "CLBD" and b.day and sepa.normalize_iban(b.account_iban) == iban and (b.day, b.amount) not in known:
            s.query(FinBalance).filter(FinBalance.account_iban == iban, FinBalance.day == b.day).delete()
            s.add(FinBalance(account_iban=iban, day=b.day, amount=b.amount))
            known.add((b.day, b.amount))
    s.commit()
    return new, dup


# --------------------------------------------------------------------------- Zeitraum, wiederkehrende Zahlungen
def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    last = [31, 29 if (d.year + y) % 4 == 0 and ((d.year + y) % 100 or (d.year + y) % 400 == 0) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m]
    return date(d.year + y, m + 1, min(d.day, last))


def period(s: Session, today: date, txns: list, matcher: Matcher) -> tuple[date, date, str]:
    """Kalendermonat oder vom letzten Gehaltseingang bis zum Tag vor dem nächsten."""
    if get_settings(s).get("fin_period") == "gehalt":
        salary = [t.booking_date for t in txns if t.amount > 0 and t.booking_date and t.booking_date <= today
                  and (c := matcher.category(t)) is not None and c.name == "Gehalt"]
        if salary:
            start = max(salary)
            if start > today - timedelta(days=40):
                return start, _add_months(start, 1) - timedelta(days=1), "seit Gehaltseingang"
    start = today.replace(day=1)
    return start, _add_months(start, 1) - timedelta(days=1), "Kalendermonat"


def _key(t: FinTxn) -> str:
    return (sepa.normalize_iban(t.iban) or norm(t.name).strip() or norm(t.purpose)[:30]) + ("+" if t.amount > 0 else "-")


def recurring(txns: list, matcher: Matcher, start: date) -> list[dict]:
    """Wiederkehrende Fixkosten/Einnahmen aus den 4 Monaten vor Periodenbeginn (mind. 3 Monate, ±20 % Betrag)."""
    window_start = _add_months(start.replace(day=1), -4)
    groups = defaultdict(list)
    for t in txns:
        if not t.booking_date or not (window_start <= t.booking_date < start):
            continue
        c = matcher.category(t)
        if c is not None and c.kind in ("variabel", "ignorieren"):
            continue  # variable Ausgaben laufen über Budgets
        groups[_key(t)].append((t, c))
    out = []
    for key, items in groups.items():
        med = median(abs(t.amount) for t, _ in items)
        similar = [(t, c) for t, c in items if med and abs(abs(t.amount) - med) <= 0.2 * med]
        months = {t.booking_date.strftime("%Y-%m") for t, _ in similar}
        if len(months) < 3:
            continue
        last3 = sorted(similar, key=lambda x: x[0].booking_date)[-3:]
        amount = round(median(t.amount for t, _ in last3), 2)
        c = last3[-1][1]
        out.append({"key": key, "name": last3[-1][0].name or last3[-1][0].purpose[:40], "amount": amount,
                    "day": int(median(t.booking_date.day for t, _ in similar)), "category": c,
                    "kind": c.kind if c else ("einnahme" if amount > 0 else "fix"), "months": len(months)})
    return sorted(out, key=lambda r: r["amount"])


def _due(day: int, start: date, end: date) -> Optional[date]:
    d = start
    for _ in range(3):
        try:
            cand = d.replace(day=min(day, 28 if d.month == 2 else 30 if d.month in (4, 6, 9, 11) else 31))
        except ValueError:
            cand = None
        if cand and start <= cand <= end:
            return cand
        d = _add_months(d.replace(day=1), 1)
    return None


# --------------------------------------------------------------------------- Übersicht
def overview(s: Session, today: Optional[date] = None) -> dict:
    seed(s)
    st = get_settings(s)
    today = today or date.today()
    iban = finance_iban(s)
    matcher = Matcher(s)
    txns = s.query(FinTxn).filter(FinTxn.account_iban == iban).order_by(FinTxn.booking_date, FinTxn.id).all() if iban else []
    bal = s.query(FinBalance).filter(FinBalance.account_iban == iban).order_by(FinBalance.day.desc()).first() if iban else None
    start, end, mode = period(s, today, txns, matcher)
    as_of = bal.day if bal else (max((t.booking_date for t in txns if t.booking_date), default=today))
    in_period = [t for t in txns if t.booking_date and start <= t.booking_date <= end]

    # Budgets
    cats = matcher.cats
    spent = defaultdict(float)
    for t in in_period:
        c = matcher.category(t)
        spent[c.id if c else 0] += t.amount
    hist_start = _add_months(start.replace(day=1), -3)
    hist = defaultdict(float)
    for t in txns:
        if t.booking_date and hist_start <= t.booking_date < start.replace(day=1):
            c = matcher.category(t)
            hist[c.id if c else 0] += t.amount
    budgets = []
    for c in cats:
        if c.kind != "variabel":
            continue
        used = round(-spent.get(c.id, 0.0), 2) + 0.0  # + 0.0: keine „-0,00 €“
        avg = round(-hist.get(c.id, 0.0) / 3, 2) + 0.0
        rest = round((c.budget or 0) - used, 2) if c.budget else None
        budgets.append({"cat": c, "used": used, "avg": avg, "rest": rest,
                        "pct": min(100, round(used / c.budget * 100)) if c.budget else 0})

    # erwartete Eingänge/Fixkosten bis Periodenende (ab Stand des Kontoauszugs)
    rec = recurring(txns, matcher, start)
    done_keys = {_key(t) for t in txns if t.booking_date and start <= t.booking_date <= as_of}
    expected = []
    for r in rec:
        if r["key"] in done_keys:
            continue
        due = _due(r["day"], start, end)
        if due is None:
            continue
        expected.append({**r, "due": max(due, as_of + timedelta(days=1))})
    expected.sort(key=lambda r: (r["due"], r["amount"]))

    # Spielraum: tiefster voraussichtlicher Kontostand bis Periodenende
    buffer = float(str(st.get("fin_buffer") or "0").replace(",", ".") or 0)
    balance = bal.amount if bal else None
    low, low_day, proj = None, None, None
    if balance is not None:
        proj = low = balance
        low_day = as_of
        for e in expected:
            proj += e["amount"]
            if proj < low:
                low, low_day = proj, e["due"]
    budget_rest = round(sum(max(b["rest"] or 0, 0) for b in budgets), 2)
    room = round(low - buffer, 2) if low is not None else None
    free = round(room - budget_rest, 2) if room is not None else None
    days_left = max((end - max(today, as_of)).days + 1, 1)
    uncategorized = defaultdict(lambda: {"count": 0, "sum": 0.0, "name": "", "example": None})
    for t in txns:
        if t.booking_date and t.booking_date >= hist_start and matcher.category(t) is None:
            u = uncategorized[norm(t.name).strip() or norm(t.purpose).strip()[:30]]
            u["count"] += 1
            u["sum"] += t.amount
            u["name"] = t.name or t.purpose[:40]
            u["example"] = t
    top_unc = sorted(uncategorized.values(), key=lambda u: u["sum"])[:15]
    return {
        "iban": iban, "balance": balance, "as_of": as_of, "start": start, "end": end, "mode": mode,
        "budgets": budgets, "expected": expected, "exp_in": round(sum(e["amount"] for e in expected if e["amount"] > 0), 2),
        "exp_out": round(sum(e["amount"] for e in expected if e["amount"] < 0), 2), "low": low, "low_day": low_day,
        "proj_end": round(proj - budget_rest, 2) if proj is not None else None, "buffer": buffer, "room": room,
        "free": free, "budget_rest": budget_rest, "per_day": round(room / days_left, 2) if room is not None else None,
        "days_left": days_left, "txns": list(reversed(in_period)), "matcher": matcher, "cats": cats,
        "uncategorized": top_unc, "recurring": rec, "count": len(txns),
        "income": round(sum(t.amount for t in in_period if t.amount > 0), 2),
        "outgo": round(sum(t.amount for t in in_period if t.amount < 0), 2),
    }


def budget_alerts(s: Session, today: Optional[date] = None) -> int:
    """Push an Super-Admins, wenn ein Budget im Zeitraum 80 % erreicht bzw. überschritten ist – je Stufe einmal."""
    from . import notify
    from .db import User

    if not notify.enabled(s, "push_admin_budget"):
        return 0
    data = overview(s, today)
    sent = set(filter(None, (get_settings(s).get("fin_alerts_sent") or "").split(",")))
    period = f"{data['start']:%Y-%m-%d}"
    subs = notify.subs_for_users(s, [u.id for u in s.query(User).filter(User.role == "superadmin", User.active.is_(True))])
    n = 0
    for b in data["budgets"]:
        c = b["cat"]
        if not c.budget:
            continue
        level = 100 if b["used"] > c.budget else 80 if b["used"] >= 0.8 * c.budget else 0
        key = f"{period}:{c.id}:{level}"
        if not level or key in sent:
            continue
        sent.add(key)
        if level == 100:
            title, body = f"Budget überschritten: {c.name}", f"{_eur(b['used'])} von {_eur(c.budget)} ausgegeben"
        else:
            title, body = f"Budget fast aufgebraucht: {c.name}", f"{_eur(b['used'])} von {_eur(c.budget)} – noch {_eur(b['rest'])}"
        if subs:
            notify.send_to(s, subs, {"title": title, "body": body, "url": "/admin/finanzen", "tag": f"budget-{c.id}"},
                           "high" if level == 100 else "normal")
        n += 1
    keep = sorted(k for k in sent if k.startswith(period))  # nur den laufenden Zeitraum merken
    save_settings(s, {"fin_alerts_sent": ",".join(keep)})
    s.commit()
    return n


def _eur(x: float) -> str:
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def suggest_budgets(s: Session, today: Optional[date] = None, only_empty: bool = True) -> int:
    """Budget = Durchschnitt der letzten 3 vollen Monate (auf 10 € aufgerundet)."""
    data = overview(s, today)
    n = 0
    for b in data["budgets"]:
        c = b["cat"]
        if b["avg"] > 0 and (not only_empty or not c.budget):
            c.budget = float(int((b["avg"] + 9.99) // 10 * 10))
            n += 1
    s.commit()
    return n


def remember_rule(cat: FinCategory, t: FinTxn) -> str:
    """Empfänger als Regel übernehmen (Name; ohne Namen die IBAN)."""
    words = norm(t.name).strip()
    if words:
        words = " ".join(words.split()[:3])
        rule = words
    elif t.iban:
        rule = f"iban:{sepa.normalize_iban(t.iban)}"
    else:
        return ""
    existing = {r.text for r in parse_rules(cat.rules)}
    if norm(rule).strip() not in existing and rule not in existing:
        cat.rules = (cat.rules + ", " if cat.rules.strip() else "") + rule
    return rule


# --------------------------------------------------------------------------- Seiten (nur Super-Admin)
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402

from . import bankimport  # noqa: E402
from .accounts import _page, _redirect  # noqa: E402
from .db import get_session  # noqa: E402

router = APIRouter()
COLORS = ["#16a34a", "#db2777", "#ea580c", "#7c3aed", "#0891b2", "#a16207", "#f59e0b", "#2563eb", "#64748b"]


@router.get("/admin/finanzen", response_class=HTMLResponse)
def finance_page(request: Request, show: str = "", s: Session = Depends(get_session)):
    data = overview(s)
    st = get_settings(s)
    txns = data["txns"]
    if show == "offen":
        txns = [t for t in txns if data["matcher"].category(t) is None]
    return _page(request, "finance.html", d=data, txns=txns, show=show, kinds=KINDS, st=st,
                 fmt_iban=sepa.format_iban, colors=COLORS)


@router.post("/admin/finanzen/import")
async def finance_import(request: Request, file: UploadFile, s: Session = Depends(get_session)):
    """Kontoauszug einlesen – füllt Finanzplanung und (passende IBAN) auch den Rücklagen-Abgleich."""
    from . import reserves

    data = await file.read()
    bals: list = []
    try:
        txns = bankimport.parse(file.filename or "", data, bals)
    except bankimport.ImportError_ as e:
        return _redirect("/admin/finanzen", f"Import fehlgeschlagen: {e}")
    seed(s)
    new, dup = ingest(s, txns, bals)
    if new:
        budget_alerts(s)
    msg = f"Finanzen: {new} Umsätze neu" + (f", {dup} schon vorhanden" if dup else "")
    if not finance_iban(s):
        msg = "Bitte zuerst das Hauptkonto (IBAN) eintragen – unten unter Einstellungen."
    for bld in s.query(Building):
        ibans = {sepa.normalize_iban(bld.account_iban), sepa.normalize_iban(bld.reserve_iban)} - {""}
        if ibans & {sepa.normalize_iban(t.account_iban) for t in txns}:
            m = reserves.ingest_bank(s, bld, txns, bals)
            msg += f" · Rücklagen ({bld.label}): {m}"
    return _redirect("/admin/finanzen", msg)


@router.post("/admin/finanzen/txn/{tid}")
async def finance_txn(request: Request, tid: int, s: Session = Depends(get_session)):
    t = s.get(FinTxn, tid)
    if t is None:
        raise HTTPException(404)
    form = await request.form()
    cid = int(form.get("category") or 0) if str(form.get("category", "")).isdigit() else 0
    cat = s.get(FinCategory, cid) if cid else None
    t.category_id = cat.id if cat else None
    msg = f"Zugeordnet: {cat.name}" if cat else "Zuordnung entfernt"
    if cat and form.get("remember"):
        rule = remember_rule(cat, t)
        if rule:
            t.category_id = None  # künftig per Regel
            msg += f" · Regel „{rule}“ gemerkt"
    s.commit()
    back = "/admin/finanzen" + ("?show=offen" if form.get("show") == "offen" else "") + "#umsaetze"
    return _redirect(back, msg)


@router.post("/admin/finanzen/categories")
async def finance_categories(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    from .render import parse_float

    for c in s.query(FinCategory):
        if form.get(f"delete_{c.id}"):
            s.query(FinTxn).filter(FinTxn.category_id == c.id).update({FinTxn.category_id: None})
            s.delete(c)
            continue
        if f"name_{c.id}" not in form:
            continue
        c.name = " ".join(str(form.get(f"name_{c.id}", c.name)).split())[:100] or c.name
        kind = str(form.get(f"kind_{c.id}", c.kind))
        c.kind = kind if kind in KINDS else c.kind
        c.budget = round(parse_float(form.get(f"budget_{c.id}")) or 0.0, 2)
        c.rules = str(form.get(f"rules_{c.id}", c.rules)).strip()[:4000]
    name = " ".join(str(form.get("new_name", "")).split())[:100]
    if name:
        kind = str(form.get("new_kind", "variabel"))
        n = s.query(FinCategory).count()
        s.add(FinCategory(name=name, kind=kind if kind in KINDS else "variabel",
                          budget=round(parse_float(form.get("new_budget")) or 0.0, 2),
                          rules=str(form.get("new_rules", "")).strip()[:4000], color=COLORS[n % len(COLORS)], sort=n))
    s.commit()
    return _redirect("/admin/finanzen#kategorien", "Kategorien gespeichert")


@router.post("/admin/finanzen/budget/{cid}")
async def finance_budget(request: Request, cid: int, s: Session = Depends(get_session)):
    """Limit einer Budget-Kategorie direkt setzen (leer oder 0 = kein Limit)."""
    from .render import parse_float

    c = s.get(FinCategory, cid)
    if c is None:
        raise HTTPException(404)
    form = await request.form()
    c.budget = round(max(parse_float(form.get("budget")) or 0.0, 0.0), 2)
    s.commit()
    msg = f"Limit {c.name}: {_eur(c.budget)} pro Monat" if c.budget else f"Limit {c.name} entfernt"
    return _redirect("/admin/finanzen#budgets", msg)


@router.post("/admin/finanzen/suggest")
async def finance_suggest(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    n = suggest_budgets(s, only_empty=not form.get("all"))
    return _redirect("/admin/finanzen", f"{n} Budget(s) aus dem Durchschnitt der letzten 3 Monate gesetzt")


@router.post("/admin/finanzen/settings")
async def finance_settings(request: Request, s: Session = Depends(get_session)):
    form = await request.form()
    iban = sepa.normalize_iban(str(form.get("fin_iban", "")))
    if iban and not sepa.iban_ok(iban):
        return _redirect("/admin/finanzen#einstellungen", f"IBAN {iban} ist ungültig.")
    buffer = str(form.get("fin_buffer", "0")).replace(",", ".").strip()
    try:
        buffer = f"{max(float(buffer or 0), 0):.2f}"
    except ValueError:
        buffer = "0"
    save_settings(s, {"fin_iban": iban, "fin_period": "gehalt" if form.get("fin_period") == "gehalt" else "monat",
                      "fin_buffer": buffer})
    s.commit()
    return _redirect("/admin/finanzen", "Einstellungen gespeichert")
