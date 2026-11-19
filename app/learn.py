"""Lernende Kategorisierung der Umsätze (Finanzplanung).

1. Lernspeicher (``FinMemory``): bestätigte Zuordnung Empfänger → Kategorie. Greift nach den Regeln der Kategorie
   automatisch für alle (auch künftigen) Umsätze dieses Empfängers.
2. Lernmodell (Naive Bayes): lernt aus allen bereits kategorisierten Umsätzen (von Hand, per Regel, Lernspeicher)
   über Empfänger, Wörter im Verwendungszweck, IBAN, Richtung und Betragsgröße und schlägt für offene Umsätze eine
   Kategorie mit Wahrscheinlichkeit vor. Bestätigte Vorschläge landen im Lernspeicher.
3. KI-Kategorisierung (OpenAI, optional): nur für Empfänger, die das Lernmodell nicht sicher kennt. Es gehen nur
   Händlernamen raus – keine Beträge, Daten, IBANs, Verwendungszwecke und keine Namen von Personen.
"""

from __future__ import annotations

import json
import math
import re
import secrets
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from . import sepa
from .accounts import _page, _redirect
from .db import FinCategory, FinMemory, FinTxn, get_session, get_settings, save_settings

router = APIRouter()
THRESHOLD = 0.9  # ab dieser Wahrscheinlichkeit „sicherer“ Vorschlag
STOP = {"SAGT", "DANKE", "GMBH", "CO", "KG", "AG", "UND", "DER", "DIE", "DAS", "FUER", "VOM", "BEI", "IBAN", "BIC",
        "EREF", "MREF", "CRED", "SVWZ", "ABWA", "KREF", "DATUM", "UHR", "EUR", "KARTE", "NR", "VISA", "DEBIT",
        "GIROCARD", "ZAHLUNG", "LASTSCHRIFT", "UEBERWEISUNG", "GUTSCHRIFT", "FOLGELASTSCHRIFT", "ERSTLASTSCHRIFT"}
COMPANY = re.compile(r"\b(GMBH|AG|KG|SE|UG|MBH|EV|E V|CO|INC|LTD|LLC|SARL|BV|SAS|SPA|OHG|GBR|MARKT|SHOP|STORE|CENTER|"
                     r"CENTRUM|TANKSTELLE|APOTHEKE|BANK|SPARKASSE|VERSICHERUNG|STADTWERKE|WERKE|VERLAG|SERVICE|"
                     r"SERVICES|HANDEL|ONLINE|DIGITAL|TELEKOM|KASSE|AMT|GEMEINDE|STADT|LANDRATSAMT|FINANZAMT|"
                     r"RESTAURANT|HOTEL|BAECKEREI|METZGEREI|DROGERIE|BAUMARKT|AUTOHAUS|PRAXIS|KLINIK|SCHULE|VEREIN)\b")


def norm(text: str) -> str:
    from .finance import norm as fnorm

    return fnorm(text).strip()


def name_key(t) -> str:
    """Empfänger normalisiert (ohne Nummern) – Schlüssel für Lernspeicher und Gruppen."""
    base = norm(t.name) or " ".join(norm(t.purpose).split()[:3])
    base = re.sub(r"\b\w*\d\w*\b", " ", base)  # Kartennummern, Filial- und Vorgangsnummern
    return " ".join(base.split())[:120]


def _words(text: str, limit: int = 14) -> list[str]:
    out = []
    for w in norm(text).split():
        if len(w) >= 3 and not any(ch.isdigit() for ch in w) and w not in STOP:
            out.append(w)
        if len(out) >= limit:
            break
    return out


def features(t) -> list[tuple[str, float]]:
    f = []
    k = name_key(t)
    if k:
        f.append(("N:" + k, 3.0))
    f += [("W:" + w, 1.0) for w in _words(t.name, 6)]
    f += [("P:" + w, 0.6) for w in _words(t.purpose)]
    if t.iban:
        f.append(("I:" + sepa.normalize_iban(t.iban), 2.0))
    f.append(("S:" + ("+" if (t.amount or 0) > 0 else "-"), 1.0))
    a = abs(t.amount or 0)
    f.append(("A:" + str(int(math.log10(a + 1) * 2)), 0.5))
    return f


# --------------------------------------------------------------------------- Lernspeicher
def memory(s: Session) -> dict[str, int]:
    return {m.key: m.category_id for m in s.query(FinMemory) if m.category_id}


def remember(s: Session, t_or_key, category_id: int, source: str, label: str = "", confidence: float = 1.0) -> None:
    key = t_or_key if isinstance(t_or_key, str) else name_key(t_or_key)
    if not key:
        return
    m = s.query(FinMemory).filter(FinMemory.key == key).first()
    if m is None:
        m = FinMemory(key=key)
        s.add(m)
    m.category_id, m.source, m.confidence = category_id, source, confidence
    m.label = (label or (getattr(t_or_key, "name", "") if not isinstance(t_or_key, str) else "") or key)[:200]


# --------------------------------------------------------------------------- Lernmodell (Naive Bayes)
class Model:
    def __init__(self):
        self.prior: Counter = Counter()
        self.counts: dict[int, Counter] = defaultdict(Counter)
        self.totals: Counter = Counter()
        self.vocab: set = set()
        self.n = 0

    def add(self, cid: int, feats, weight: float = 1.0) -> None:
        self.prior[cid] += weight
        self.n += 1
        for f, w in feats:
            self.counts[cid][f] += w * weight
            self.totals[cid] += w * weight
            self.vocab.add(f)

    def predict(self, feats) -> Optional[tuple[int, float]]:
        if len(self.prior) < 2:
            return None
        v = len(self.vocab) + 1
        total = sum(self.prior.values())
        scores = {}
        for cid, pc in self.prior.items():
            sc = math.log(pc / total)
            for f, w in feats:
                if f not in self.vocab:
                    continue
                sc += w * math.log((self.counts[cid][f] + 0.1) / (self.totals[cid] + 0.1 * v))
            scores[cid] = sc
        best = max(scores, key=scores.get)
        mx = scores[best]
        z = sum(math.exp(x - mx) for x in scores.values())
        return best, 1.0 / z


_cache: dict = {}


def model(s: Session, matcher) -> Model:
    """Aus allen kategorisierten Umsätzen trainieren (zwischengespeichert, bis sich Daten/Regeln ändern)."""
    sig = (s.query(FinTxn).count(), max((t.id for t in s.query(FinTxn.id).order_by(FinTxn.id.desc()).limit(1)), default=0),
           s.query(FinMemory).count(), tuple((c.id, c.rules, c.kind) for c in matcher.cats),
           sum(1 for _ in s.query(FinTxn.id).filter(FinTxn.category_id.isnot(None))))
    if _cache.get("sig") == sig:
        return _cache["model"]
    m = Model()
    for t in s.query(FinTxn).filter(FinTxn.booking_date >= date.today() - timedelta(days=730)):
        c = matcher.category(t)
        if c is not None:
            m.add(c.id, features(t), 2.0 if t.category_id else 1.0)
    _cache.update(sig=sig, model=m)
    return m


def suggestions(s: Session, matcher, txns: list) -> dict[int, tuple]:
    """Vorschläge für noch nicht kategorisierte Umsätze → {txn.id: (Kategorie, Wahrscheinlichkeit)}."""
    m = model(s, matcher)
    out = {}
    for t in txns:
        if matcher.category(t) is not None:
            continue
        p = m.predict(features(t))
        if p and p[0] in matcher.by_id and p[1] >= 0.5:
            out[t.id] = (matcher.by_id[p[0]], round(p[1], 2))
    return out


# --------------------------------------------------------------------------- KI-Kategorisierung
def persons(s: Session) -> set[str]:
    from .db import Building, Party, User

    out = set()
    for b in s.query(Building):
        out |= {b.account_name, b.reserve_name}
    for p in s.query(Party):
        out.add(p.name)
    for u in s.query(User):
        out |= {u.name, u.username}
    try:
        from .db import BankAccess

        for a in s.query(BankAccess):
            out |= {x.get("owner", "") for x in (a.accounts or [])}
    except Exception:  # noqa: BLE001
        pass
    words = set()
    for v in out:
        for w in norm(v or "").split():
            if len(w) >= 4 and w not in {"FAMILIE", "HERR", "FRAU"}:
                words.add(w)
    return words


def is_merchant(name: str, person_words: set[str]) -> bool:
    """Nur Firmen/Händler an die KI – keine Personen (konservativ: im Zweifel nicht senden)."""
    n = norm(name)
    if not n or set(n.split()) & person_words:
        return False
    if COMPANY.search(n):
        return True
    words = [w for w in n.split() if not any(ch.isdigit() for ch in w)]
    return len(words) == 1 and len(words[0]) >= 3  # z. B. AMAZON, PAYPAL, REWE


def sanitize(name: str) -> str:
    n = re.sub(r"\b\w*\d{3,}\w*\b", " ", sepa.clean(name or "", 100, umlauts=False))
    return " ".join(w for w in n.split() if re.search(r"[A-Za-z]", w))[:40]


def candidates(s: Session, matcher, days: int = 180) -> tuple[list[dict], list[str]]:
    """Empfänger-Gruppen ohne Kategorie, die das Lernmodell nicht sicher zuordnet. → (zu senden, nicht gesendet)"""
    from .audit import own_ibans

    own, pw = own_ibans(s), persons(s)
    m = model(s, matcher)
    groups: dict[str, dict] = {}
    for t in s.query(FinTxn).filter(FinTxn.booking_date >= date.today() - timedelta(days=days)):
        if matcher.category(t) is not None or sepa.normalize_iban(t.iban) in own:
            continue
        k = name_key(t)
        if not k:
            continue
        g = groups.setdefault(k, {"key": k, "name": t.name or t.purpose[:40], "n": 0, "out": 0, "in": 0, "p": 0.0})
        g["n"] += 1
        g["out" if t.amount < 0 else "in"] += 1
        p = m.predict(features(t))
        g["p"] = max(g["p"], p[1] if p else 0)
    send, skipped = [], []
    for g in sorted(groups.values(), key=lambda g: -g["n"]):
        if g["p"] >= THRESHOLD:
            continue
        if is_merchant(g["name"], pw):
            send.append(g)
        else:
            skipped.append(g["name"])
    return send[:80], skipped


def ai_categorize(s: Session, matcher) -> tuple[list[dict], list[str]]:
    from . import audit

    key = audit.api_key(s)
    if not key:
        raise audit.AuditError("Kein OpenAI-API-Schlüssel (unter Kassenprüfung → Einstellungen).")
    send, skipped = candidates(s, matcher)
    if not send:
        return [], skipped
    defaults = {c[0] for c in __import__("app.finance", fromlist=["DEFAULTS"]).DEFAULTS}
    pw = persons(s)
    cats = {}
    for c in matcher.cats:
        if c.kind == "ignorieren":
            continue
        label = c.name if (c.name in defaults or not set(norm(c.name).split()) & pw) else f"Kategorie {c.id}"
        cats[label] = c.id
    ids = {f"H-{secrets.randbelow(900) + 100}{i}": g for i, g in enumerate(send)}
    payload = {"kategorien": [{"name": n, "art": next(c.kind for c in matcher.cats if c.id == cid)} for n, cid in cats.items()],
               "empfaenger": [{"id": i, "name": sanitize(g["name"]),
                               "richtung": "ausgabe" if g["out"] >= g["in"] else "eingang", "anzahl": g["n"]}
                              for i, g in ids.items()]}
    leaks = [w for w in pw if w in json.dumps(payload, ensure_ascii=False).upper()]
    if leaks:
        raise audit.AuditError("Datenschutzprüfung: Personennamen in der Anfrage – nicht gesendet.")
    schema = {"type": "object", "additionalProperties": False, "required": ["zuordnungen"],
              "properties": {"zuordnungen": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["id", "kategorie", "sicher"],
                  "properties": {"id": {"type": "string"},
                                 "kategorie": {"type": "string", "enum": list(cats) + ["unklar"]},
                                 "sicher": {"type": "number"}}}}}}
    system = ("Ordne Empfänger von Kontoumsätzen eines deutschen Privathaushalts den vorgegebenen Kategorien zu. "
              "Nutze nur die Kategorienamen aus der Liste; wenn unsicher, „unklar“. „sicher“ = Wahrscheinlichkeit 0–1.")
    body = {"model": audit.model_name(s), "store": False,
            "input": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "text": {"format": {"type": "json_schema", "name": "kategorien", "strict": True, "schema": schema}}}
    try:
        r = httpx.post(f"{audit.OPENAI_URL}/responses", json=body, timeout=audit.TIMEOUT,
                       headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    except httpx.HTTPError as e:
        raise audit.AuditError(f"OpenAI nicht erreichbar: {e}") from e
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise audit.AuditError(audit.explain_error(r.status_code, data, body["model"]))
    text = next((c.get("text") for o in data.get("output", []) for c in (o.get("content") or [])
                 if c.get("type") == "output_text"), None)
    if not text:
        raise audit.AuditError("Antwort des Modells enthält keinen Text.")
    out = []
    for z in json.loads(text).get("zuordnungen", []):
        g = ids.get(z.get("id"))
        if g and z.get("kategorie") in cats:
            out.append({"key": g["key"], "name": g["name"], "n": g["n"], "category_id": cats[z["kategorie"]],
                        "sicher": round(float(z.get("sicher") or 0), 2)})
    return out, skipped


# --------------------------------------------------------------------------- Web
def _matcher(s: Session):
    from .finance import Matcher, seed

    seed(s)
    return Matcher(s)


@router.get("/admin/finanzen/lernen", response_class=HTMLResponse)
def learn_page(request: Request, show: str = "", s: Session = Depends(get_session)):
    matcher = _matcher(s)
    m = model(s, matcher)
    recent = s.query(FinTxn).filter(FinTxn.booking_date >= date.today() - timedelta(days=180)).all()
    sugg = suggestions(s, matcher, recent)
    groups: dict[str, dict] = {}
    for t in recent:
        if t.id in sugg:
            c, p = sugg[t.id]
            g = groups.setdefault(name_key(t), {"key": name_key(t), "name": t.name or t.purpose[:40], "cat": c, "p": p,
                                                "n": 0, "sum": 0.0})
            g["n"] += 1
            g["sum"] += t.amount
            g["p"] = min(g["p"], p) if g["cat"].id == c.id else 0.5
    st = get_settings(s)
    try:
        ai = json.loads(st.get("fin_ai_suggestions") or "[]")
    except ValueError:
        ai = []
    preview = candidates(s, matcher) if show == "vorschau" else None
    mem = s.query(FinMemory).order_by(FinMemory.created_at.desc()).all()
    return _page(request, "learn.html", groups=sorted(groups.values(), key=lambda g: (-g["p"], -g["n"])), ai=ai,
                 cats=matcher.cats, by_id=matcher.by_id, memory=mem, trained=m.n, ncat=len(m.prior), preview=preview,
                 threshold=THRESHOLD, sanitize=sanitize)


@router.post("/admin/finanzen/lernen/accept")
async def learn_accept(request: Request, s: Session = Depends(get_session)):
    """Vorschläge übernehmen (je Empfänger) → Lernspeicher."""
    form = await request.form()
    keys = form.getlist("key")
    n = 0
    for k in keys:
        cid = str(form.get(f"cat_{k}", ""))
        if cid.isdigit() and s.get(FinCategory, int(cid)):
            remember(s, k, int(cid), str(form.get("source", "lern"))[:10], str(form.get(f"label_{k}", "")),
                     float(form.get(f"p_{k}", 1) or 1))
            n += 1
    if form.get("source") == "ki":
        st = get_settings(s)
        try:
            rest = [a for a in json.loads(st.get("fin_ai_suggestions") or "[]") if a["key"] not in set(keys)]
        except ValueError:
            rest = []
        save_settings(s, {"fin_ai_suggestions": json.dumps(rest, ensure_ascii=False)})
    s.commit()
    return _redirect("/admin/finanzen/lernen", f"{n} Empfänger im Lernspeicher – gilt ab sofort für alle ihre Umsätze.")


@router.post("/admin/finanzen/lernen/sure")
async def learn_sure(request: Request, s: Session = Depends(get_session)):
    """Alle sicheren Vorschläge (≥ 90 %) übernehmen."""
    matcher = _matcher(s)
    recent = s.query(FinTxn).filter(FinTxn.booking_date >= date.today() - timedelta(days=180)).all()
    sugg = suggestions(s, matcher, recent)
    best: dict[str, tuple] = {}
    for t in recent:
        if t.id in sugg and sugg[t.id][1] >= THRESHOLD:
            k = name_key(t)
            if k not in best or best[k][1] > sugg[t.id][1]:
                best[k] = (sugg[t.id][0].id, sugg[t.id][1], t.name)
    for k, (cid, p, label) in best.items():
        remember(s, k, cid, "lern", label, p)
    s.commit()
    return _redirect("/admin/finanzen/lernen", f"{len(best)} sichere Vorschläge übernommen.")


@router.post("/admin/finanzen/lernen/forget/{mid}")
def learn_forget(mid: int, s: Session = Depends(get_session)):
    m = s.get(FinMemory, mid)
    if m is not None:
        s.delete(m)
        s.commit()
    return _redirect("/admin/finanzen/lernen#speicher", "Aus dem Lernspeicher entfernt.")


@router.post("/admin/finanzen/lernen/ki")
async def learn_ai(request: Request, s: Session = Depends(get_session)):
    from .audit import AuditError

    matcher = _matcher(s)
    try:
        out, skipped = ai_categorize(s, matcher)
    except AuditError as e:
        return _redirect("/admin/finanzen/lernen", str(e))
    save_settings(s, {"fin_ai_suggestions": json.dumps(out, ensure_ascii=False)})
    msg = f"KI: {len(out)} Vorschläge" + (f" · {len(skipped)} Empfänger nicht gesendet (vermutlich Personen)" if skipped else "")
    return _redirect("/admin/finanzen/lernen#ki", msg)
