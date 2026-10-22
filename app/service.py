"""Verbindet Datenbank, Home Assistant, Berechnung und Versand."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Optional
from pathlib import Path

from sqlalchemy.orm import Session

from . import billing as calc
from . import invoice_import, mailer, notify, render, victron, vrm, wa_cloud, whatsapp
from .config import config
from .db import Allocation, Billing, FixedCost, IgnoredInvoice, Party, get_settings
from .ha import HAClient, HAError

# Einstellungs-Schlüssel der Haus-Entitäten und ihre Rolle
HOUSE_ENTITIES = [
    ("entity_grid", "Netzbezug (Stromzähler)"),
    ("entity_total", "Gesamtverbrauch Haus"),
    ("entity_battery", "Batterie entladen"),
    ("entity_battery_charge", "Batterie geladen gesamt"),
    ("entity_battery_charge_grid", "Batterie aus Netz geladen – dyn. ESS"),
    ("entity_pv_direct", "PV-Direktverbrauch (optional)"),
]


def _num(v: str) -> float:
    try:
        return float(str(v).replace(",", ".")) if str(v).strip() else 0.0
    except ValueError:
        return 0.0


def water_price_parts(st: dict[str, str]) -> list[tuple[str, float]]:
    """Wasser- und Abwasserpreis je m³ aus den Einstellungen."""
    return [("Wasser", _num(st["water_price_m3"])), ("Abwasser", _num(st["sewage_price_m3"]))]


# Vorschläge für die lokale Victron-Integration hass-victron (Modbus TCP), Verbraucher am AC-out.
# Sensoren der Cloud-Integration „Victron Remote Monitoring“ (VRM) werden bewusst nie vorgeschlagen.
# "suggest": Teile der Entity-ID; mehrere Teile werden als Summe eingetragen.
VICTRON_HINTS = {
    "entity_grid": {
        "hint": "Nur zur Kontrolle gegen die Rechnung – gerechnet wird immer mit dem Bezug laut Rechnung. "
                "Stromzähler in HA (Bezug in kWh) oder Victron-Logger victron:grid_import. Victron Energy Meter: "
                "grid_energy_forward_total (hass-victron) bzw. victron_netzzaehler_bezug (HA-Modbus, siehe "
                "docs/ha-modbus-victron-netzzaehler.yaml). Leer lassen = kWh aus der Stromrechnung. Nicht die "
                "Leistung system_grid_l1–l3 als 3 Phasen nehmen: durch den Phasenausgleich käme ein Vielfaches heraus.",
        "suggest": ["grid_energy_forward_total|victron_netzzaehler_bezug"],
    },
    "entity_total": {
        "hint": "3 Phasen Leistung: system_consumption_l1 / _l2 / _l3 (W). Robuster als die VE.Bus-Zähler "
                "vebus_acin1toacout + vebus_invertertoacout, die bei Neustarts von GX/MultiPlus zurückgesetzt "
                "werden und dabei Verbrauch verlieren (Praxistest: bis zu 16 % pro Monat).",
        "suggest": ["system_consumption_l1", "system_consumption_l2", "system_consumption_l3"],
    },
    "entity_battery": {
        "hint": "Lokal: battery_history_dischargedenergy (DC-seitig, inkl. Wandlungsverluste). "
                "Mit VRM: vrm:Bc (Batterie → Verbraucher, AC-seitig, ohne Verkauf ins Netz) – genauer.",
        "suggest": ["battery_history_dischargedenergy"],
    },
    "entity_battery_charge": {
        "hint": "battery_history_chargedenergy (SmartShunt/BMV/BMS, Ladung aus Netz + PV).",
        "suggest": ["battery_history_chargedenergy"],
    },
    "entity_battery_charge_grid": {
        "hint": "vebus_acin1toinverter (MultiPlus: Netz → Wechselrichter = Ladung aus dem Netz). "
                "Fehlt er: Victron-Integration → Konfigurieren → „Rescan available devices“. "
                "Hinweis: VE.Bus-Zähler werden bei Neustarts zurückgesetzt – Werte nach Updates prüfen.",
        "suggest": ["vebus_acin1toinverter"],
    },
    "entity_pv_direct": {
        "hint": "Leer lassen – wird berechnet: Gesamtverbrauch − Netz direkt − Batterie entladen.",
        "suggest": [],
    },
}


def active_parties(s: Session, building_id: Optional[int] = None) -> list[Party]:
    """Aktive Parteien – mit ``building_id`` nur die des Gebäudes (Multi-Site)."""
    q = s.query(Party).filter(Party.active.is_(True))
    if building_id is not None:
        q = q.filter(Party.building_id == building_id)
    return q.order_by(Party.sort, Party.id).all()


def default_building(s: Session) -> Optional["Building"]:
    """Gebäude für neue Abrechnungen: das einzige aktive, sonst keins (muss zugeordnet werden)."""
    from .db import Building

    rows = s.query(Building).filter(Building.active.is_(True)).limit(2).all()
    return rows[0] if len(rows) == 1 else None


def building_info(s: Session, b: Billing, st: dict) -> dict:
    """Kopfdaten für die Abrechnung: aus dem zugeordneten Gebäude, sonst aus den Einstellungen."""
    from .db import Building

    bld = s.get(Building, b.building_id) if b.building_id else None
    if bld is None:
        return {k: st[k] for k in ("building_title", "building_address", "building_id")}
    return {"building_title": bld.title or "Nebenkostenabrechnung", "building_address": bld.address or "",
            "building_id": bld.code or "", "building_name": bld.name or ""}


def energy_entities(st: dict[str, str]) -> calc.EnergyEntities:
    return calc.EnergyEntities(
        total=st["entity_total"], grid=st["entity_grid"], battery_discharge=st["entity_battery"],
        battery_charge_total=st["entity_battery_charge"], battery_charge_grid=st["entity_battery_charge_grid"],
        pv_direct=st["entity_pv_direct"],
    )


def _roles(spec: str, role: str) -> list[tuple[str, str]]:
    """Entitäten einer Angabe mit Rolle; bei 3 Teilen als Phasen L1–L3 beschriftet."""
    ids = calc.split_ids(spec)
    if len(ids) == 3:
        return [(e, f"{role} – L{i}") for i, e in enumerate(ids, start=1)]
    if len(ids) > 1:
        return [(e, f"{role} – Teil {i}") for i, e in enumerate(ids, start=1)]
    return [(e, role) for e in ids]


def required_entities(s: Session, building_id: Optional[int] = None) -> list[tuple[str, str]]:
    """Alle Entitäten, deren Verbrauch für eine Abrechnung gebraucht wird: (entity_id, Rolle)."""
    st = get_settings(s)
    out: list[tuple[str, str]] = [x for k, role in HOUSE_ENTITIES for x in _roles(st[k], role)]
    parties = active_parties(s, building_id)
    names = {p.id: p.name for p in parties}
    for p in parties:
        if p.is_owner:
            continue  # Eigentümer trägt den Restverbrauch – seine Zähler werden nicht gebraucht
        meters = p.meters or []
        for i, m in enumerate(meters, start=1):
            out += _roles(m, f"Zähler {p.name}" + (f" #{i}" if len(meters) > 1 else ""))
    for a in s.query(Allocation).filter(Allocation.active.is_(True)).all():
        if a.source_type in ("energy", "quantity") and a.source_entity:
            out += _roles(a.source_entity, f"Umlage {a.name} (Quelle)")
        if a.key_type == "entity":
            for pid, ent in (a.key or {}).items():
                if ent and int(pid) in names:
                    out += _roles(str(ent), f"Umlage {a.name}: {names[int(pid)]}")
    seen: set[str] = set()
    uniq = []
    for e, role in out:
        if e not in seen:
            seen.add(e)
            uniq.append((e, role))
    return uniq


async def fetch_values(s: Session, b: Billing) -> dict[str, str]:
    """Holt Verbrauchswerte aus HA, dem Victron-Logger („victron:…“) und VRM („vrm:…“).

    Jede Quelle wird für sich abgefragt – fällt eine aus, werden die übrigen trotzdem geladen.
    Rückgabe: Entitäten ohne Wert mit Grund.
    """
    ents = [e for e, _ in required_entities(s, b.building_id)]
    ha_ids = [e for e in ents if not victron.is_victron(e) and not vrm.is_vrm(e)]
    vic_ids = [e for e in ents if victron.is_victron(e)]
    vrm_ids = [e for e in ents if vrm.is_vrm(e)]
    st = get_settings(s)
    fetched: dict = {}
    meta: dict = {}
    reasons: dict[str, str] = {}

    if vrm_ids:
        try:
            if not vrm.configured():
                raise vrm.VRMError("VRM_TOKEN ist nicht gesetzt (Portainer / .env)")
            r_vals, r_meta = await vrm.consumption(st["vrm_site_id"], vrm_ids,
                                                   b.period_start, b.period_end, config.timezone)
            fetched.update(r_vals)
            meta.update(r_meta)
            reasons.update({e: "VRM liefert für den Zeitraum keinen Wert" for e in vrm_ids if r_vals.get(e) is None})
        except Exception as e:  # noqa: BLE001
            reasons.update({i: f"VRM: {e}" for i in vrm_ids})
    if ha_ids:
        try:
            if not (config.ha_url and config.ha_token):
                raise HAError("HA_URL / HA_TOKEN nicht gesetzt")
            client = HAClient(config.ha_url, config.ha_token)
            h_vals, h_meta = await client.consumption_detail(ha_ids, b.period_start, b.period_end, config.timezone)
            fetched.update(h_vals)
            meta.update(h_meta)
            no_data = [e for e in ha_ids if h_vals.get(e) is None]
            if no_data:
                try:
                    known = {x["entity_id"]: x for x in await client.entities()}
                except Exception:  # noqa: BLE001
                    known = {}
                for e in no_data:
                    x = known.get(e)
                    if known and (x is None or (x.get("state") in ("", None) and e.startswith("sensor."))):
                        hint = similar_entity(e, known)
                        reasons[e] = ("Entität existiert in HA nicht (mehr) – umbenannt?"
                                      + (f" Vorschlag: {hint}" if hint else ""))
                    else:
                        reasons[e] = "HA-Statistik hat für den Zeitraum keine Daten"
        except Exception as e:  # noqa: BLE001
            reasons.update({i: f"Home Assistant: {e}" for i in ha_ids})
    if vic_ids:
        v_vals, v_meta = victron.consumption(s, vic_ids, b.period_start, b.period_end, config.timezone)
        fetched.update(v_vals)
        meta.update(v_meta)
        reasons.update({e: "Victron-Logger hat für den Zeitraum keine Daten" for e in vic_ids if v_vals.get(e) is None})

    values = dict(b.values or {})
    values_meta = dict(b.values_meta or {})
    for e, v in fetched.items():
        if v is not None:
            values[e] = round(v, 4)
            values_meta[e] = meta.get(e) or {}
    for e, why in reasons.items():
        values_meta[e] = {**(values_meta.get(e) or {}), "missing": why} if values.get(e) is None else values_meta.get(e, {})
    b.values = values
    b.values_meta = values_meta
    grid_val = calc._sum(values, st["entity_grid"]) if st["entity_grid"] else None
    if not b.grid_kwh and grid_val is not None:
        b.grid_kwh = round(grid_val, 3)
    b.fetched_at = datetime.now()
    return {e: why for e, why in reasons.items() if values.get(e) is None}


def similar_entity(old: str, known: dict) -> str:
    """Findet zu einer verschwundenen ID die wahrscheinliche neue (gleiches Ende, aktive Entität)."""
    tail = old.split(".", 1)[-1]
    parts = tail.split("_")
    for n in range(len(parts), 1, -1):
        suffix = "_".join(parts[-n:])
        hits = [k for k, x in known.items() if k != old and k.endswith(suffix) and x.get("state") not in ("", None)]
        if len(hits) == 1:
            return hits[0]
    return ""


def fetch_message(missing: dict[str, str]) -> str:
    if not missing:
        return "Werte geladen"
    return "Werte geladen – fehlend: " + "; ".join(f"{e} ({why})" for e, why in missing.items())


def recompute(s: Session, b: Billing) -> dict:
    st = get_settings(s)
    db_parties = active_parties(s, b.building_id)
    parties = [
        calc.PartyCfg(id=p.id, name=p.name, meters=list(p.meters or []), is_owner=p.is_owner,
                      address=p.address, unit=p.unit)
        for p in db_parties
    ]
    fixed = [
        calc.FixedCostCfg(name=f.name, amount_gross=f.amount_gross, party_ids=[int(x) for x in f.party_ids or []],
                          id=f.id)
        for f in s.query(FixedCost).filter(FixedCost.active.is_(True)).order_by(FixedCost.id).all()
    ]
    allocs = []
    water_parts = water_price_parts(st)
    for a in s.query(Allocation).filter(Allocation.active.is_(True)).order_by(Allocation.id).all():
        amount = (b.amounts or {}).get(str(a.id), a.default_amount)
        parts = water_parts if a.source_type == "quantity" and a.price_source == "water" else []
        allocs.append(
            calc.AllocationCfg(
                id=a.id, name=a.name, source_type=a.source_type, source_entity=a.source_entity,
                amount=float(amount or 0), source_unit=a.source_unit or "", price_parts=parts,
                key_type=a.key_type,
                key_unit=a.key_unit,
                key={int(k): v for k, v in (a.key or {}).items() if v not in ("", None)},
            )
        )
    bill = calc.BillCfg(
        grid_kwh=b.grid_kwh or 0.0,
        energy_cost_net=b.energy_cost_net,
        fixed_cost_net=b.fixed_cost_net,
        vat_rate=b.vat_rate,
        wear_rate_ct=b.battery_rate_ct,
        spot_price_ct=b.spot_price_ct or 0.0,
        owner_free_own_energy=bool(st["owner_free_own_energy"]),
        period_days=(b.period_end - b.period_start).days + 1,
        pv_rate_ct=b.pv_rate_ct or 0.0,
    )
    result = calc.compute(bill, parties, fixed, allocs, b.values or {}, energy_entities(st))
    for e, m in (b.values_meta or {}).items():
        if m.get("glitches") and e in (b.values or {}):
            result["warnings"].append(
                f"{e}: {m['glitches']} Fehlsprung/-sprünge in der HA-Statistik erkannt und herausgerechnet "
                "(Sensor fiel kurz auf 0 / nicht verfügbar). Ggf. einen stabileren Sensor wählen."
            )
        if m.get("method") == "victron" and m.get("coverage", 1) < 0.98 and e in (b.values or {}):
            result["warnings"].append(
                f"{e}: Victron-Logger hat nur {m['coverage']:.0%} des Zeitraums erfasst (Daten ab {m.get('since', '?')}) "
                "– Wert ist unvollständig."
            )
        if m.get("method") == "power" and m.get("coverage", 1) < 0.98 and e in (b.values or {}):
            result["warnings"].append(
                f"{e}: aus Leistung berechnet, aber nur für {m['hours']} von {m['expected_hours']} Stunden Daten "
                f"({m['coverage']:.0%}) – Verbrauch fällt evtl. zu niedrig aus."
            )
    missing = calc.missing_entities([e for e, _ in required_entities(s, b.building_id)], b.values or {})
    if missing:
        vm = b.values_meta or {}
        detail = [f"{e} ({vm[e]['missing']})" if vm.get(e, {}).get("missing") else e for e in missing]
        result["warnings"].insert(0, "Fehlende Messwerte (als 0 gerechnet – „Werte laden“ klicken oder manuell "
                                     f"eintragen): {'; '.join(detail)}")
    extra = {p.id: p for p in db_parties}
    for rp in result["parties"]:
        rp["unit_id"] = extra[rp["id"]].unit_id or ""
    result["daily"] = daily_report(s, b, st, db_parties, allocs)
    result["landlord"] = {
        k: st[k] for k in ("landlord_name", "landlord_address", "landlord_contact", "landlord_iban",
                           "payment_days", "invoice_text")
    }
    result["building"] = building_info(s, b, st)
    result["computed_at"] = datetime.now().isoformat(timespec="seconds")
    b.result = result
    return result


# --------------------------------------------------------------------------- E-Mail
def _split_addr(text: str) -> list[str]:
    return [a.strip() for a in (text or "").replace(";", ",").split(",") if a.strip()]


def channels(party: Optional[Party]) -> tuple[bool, bool]:
    """(per E-Mail, per WhatsApp) laut Partei."""
    ch = (party.channel if party else "") or "email"
    return ch in ("email", "both"), ch in ("whatsapp", "both")


def can_send(st: dict) -> bool:
    return mailer.configured() or whatsapp.configured(st)


def send_invoices(s: Session, b: Billing, party_ids: list[int] | None = None,
                  only_unsent: bool = False) -> list[tuple[str, bool, str]]:
    """Verschickt die PDFs per E-Mail und/oder WhatsApp (über n8n). Ergebnis: (Partei, ok, Meldung).

    ``b.sent[pid]``: E-Mail-Status in ``ok/at/to/error``, WhatsApp-Status unter ``wa``.
    """
    st = get_settings(s)
    parties = {p.id: p for p in s.query(Party).all()}
    sent = {k: dict(v) for k, v in (b.sent or {}).items()}
    report = []
    for rp in (b.result or {}).get("parties", []):
        pid = rp["id"]
        if party_ids is not None and pid not in party_ids:
            continue
        party = parties.get(pid)
        by_mail, by_wa = channels(party)
        entry = sent.get(str(pid), {})
        now = datetime.now().isoformat(timespec="seconds")
        fields = defaultdict(str, {
            "name": rp["name"],
            "wohneinheit": rp["name"],
            "we_id": rp.get("unit_id", ""),
            "zeitraum": render.period_text(b),
            "betrag": render.fmt_eur(rp["total"]),
            "faellig": render.fmt_date(render.due_date(b)),
            "absender": st["landlord_name"],
            "gebaeude": st["building_address"],
        })
        pdf = None

        if by_mail and not (only_unsent and entry.get("ok")):
            to = _split_addr(party.email if party else "")
            if not to:
                if party_ids is not None:
                    report.append((rp["name"], False, "E-Mail: keine Adresse hinterlegt"))
            else:
                try:
                    pdf = render.invoice_pdf(b, rp)
                    body = st["mail_body"].format_map(fields)
                    mailer.send_mail(
                        to=to,
                        subject=st["mail_subject"].format_map(fields),
                        body=body,
                        attachments=[(render.pdf_name(b, rp), pdf)],
                        bcc=_split_addr(st["mail_bcc"]),
                        html=invoice_mail_html(st, b, rp, party, body),
                    )
                    entry.update({"ok": True, "at": now, "to": ", ".join(to)})
                    entry.pop("error", None)
                    report.append((rp["name"], True, "E-Mail " + ", ".join(to)))
                except Exception as e:  # noqa: BLE001
                    entry.update({"ok": False, "at": now, "error": str(e)})
                    report.append((rp["name"], False, f"E-Mail: {e}"))

        if by_wa and not (only_unsent and (entry.get("wa") or {}).get("ok")):
            phone = party.phone if party else ""
            if not phone:
                if party_ids is not None:
                    report.append((rp["name"], False, "WhatsApp: keine Nummer hinterlegt"))
            else:
                try:
                    pdf = pdf or render.invoice_pdf(b, rp)
                    entry["wa"] = _send_whatsapp(s, st, b, rp, party, fields, pdf, now)
                    w = entry["wa"]
                    report.append((rp["name"], True, f"WhatsApp +{w['to']} {w['status']}"))
                except Exception as e:  # noqa: BLE001
                    entry["wa"] = {"ok": False, "at": now, "error": str(e)}
                    report.append((rp["name"], False, f"WhatsApp: {e}"))
        if entry:
            sent[str(pid)] = entry
    b.sent = sent
    try:
        notify.sent_report(s, b, report)
    except Exception:  # noqa: BLE001 – Benachrichtigung darf den Versand nie stören
        pass
    return report


def mail_footer(st: dict) -> str:
    parts = [st.get("landlord_name", ""), (st.get("landlord_address") or "").replace("\n", ", "),
             st.get("landlord_contact", "")]
    line = " · ".join(p for p in parts if p)
    obj = st.get("building_address", "")
    return "\n".join(x for x in (line, f"Objekt: {obj}" if obj else "") if x)


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def invoice_mail_html(st: dict, b: Billing, rp: dict, party: Optional[Party], body: str) -> str:
    """Formatierte Abrechnungs-Mail: Text aus der Vorlage + Übersicht (Betrag, Fälligkeit, Konto) + PDF-Hinweis."""
    period = render.period_text(b)
    unit = rp.get("unit_id", "")
    facts = [("Wohneinheit", rp["name"] + (f" ({unit})" if unit else ""), False),
             ("Zeitraum", period, False),
             ("Betrag", render.fmt_eur(rp["total"]), True),
             ("Zahlbar bis", render.fmt_date(render.due_date(b)), False)]
    if st.get("landlord_iban"):
        facts.append(("Konto (IBAN)", st["landlord_iban"], False))
        facts.append(("Verwendungszweck", f"Nebenkosten {b.period_start:%m/%Y} {unit or rp['name']}", False))
    portal = f"{config.app_base_url}/" if (party is not None and party.portal and config.app_base_url) else ""
    return mailer.render_html(
        title=f"Ihre Nebenkostenabrechnung {b.period_start:%m/%Y}",
        preheader=f"{rp['name']}: {render.fmt_eur(rp['total'])} für {period}",
        brand=st.get("building_title") or "Nebenkostenabrechnung", brand_sub=st.get("building_address", ""),
        paragraphs=paragraphs(body), facts=facts, attachment=render.pdf_name(b, rp),
        button_url=portal, button_label="Alle Abrechnungen in „Mein Zuhause“",
        footer=mail_footer(st))


def _send_whatsapp(s: Session, st: dict, b: Billing, rp: dict, party: Party, fields: dict, pdf: bytes,
                   now: str) -> dict:
    """Eine Abrechnung per WhatsApp – direkt über die Cloud API oder als Webhook an n8n."""
    name, period, filename = rp["name"], render.period_text(b), render.pdf_name(b, rp)
    if whatsapp.native(st):
        to = whatsapp.normalize_phone(party.phone)
        wamid = wa_cloud.CloudClient().send_document(
            to, pdf, filename, st["wa_template_name"], st["wa_template_lang"], [name, period, fields["betrag"]])
        return {"ok": True, "at": now, "to": to, "status": "gesendet", "message_id": wamid,
                "provider": "Cloud API direkt"}
    secret = whatsapp.ensure_secret(s, st)
    payload = whatsapp.build_payload(
        st, secret, bid=b.id, pid=rp["id"], name=name, unit_id=rp.get("unit_id", ""), phone=party.phone,
        email=party.email, period=period, total=rp["total"], total_text=fields["betrag"],
        due=render.due_date(b).isoformat(), message=st["wa_message"].format_map(fields), filename=filename, pdf=pdf)
    state = whatsapp.post(st, secret, payload)
    return {"ok": True, "at": now, "to": payload["phone"], "status": state, "provider": "n8n"}


def wa_cloud_status(s: Session, items: list[dict]) -> int:
    """Statusmeldungen des Meta-Webhooks den gesendeten Nachrichten zuordnen. Ergebnis: Anzahl Treffer."""
    from .db import save_settings

    hits = 0
    pending = {i["id"]: i for i in items if i.get("id")}
    if not pending:
        return 0
    now = datetime.now().isoformat(timespec="seconds")

    def apply(wa: dict, item: dict) -> dict:
        if not wa_cloud.newer(wa.get("raw_status", "sent"), item["status"]):
            return wa
        failed = item["status"] == "failed"
        return {**wa, "ok": not failed, "raw_status": item["status"],
                "status": wa_cloud.STATUS_DE.get(item["status"], item["status"]),
                "error": item["error"] if failed else "", "status_at": now}

    st = get_settings(s)
    if st["n8n_last_test"]:
        last = json.loads(st["n8n_last_test"])
        if last.get("message_id") in pending:
            save_settings(s, {"n8n_last_test": json.dumps(apply(last, pending[last["message_id"]]), ensure_ascii=False)})
            hits += 1
    events = []
    for b in s.query(Billing).order_by(Billing.id.desc()).limit(60).all():
        changed = False
        sent = {k: dict(v) for k, v in (b.sent or {}).items()}
        for pid, entry in sent.items():
            wa = entry.get("wa") or {}
            if wa.get("message_id") in pending:
                new = apply(wa, pending[wa["message_id"]])
                if new.get("raw_status") != wa.get("raw_status"):
                    events.append((b, int(pid), new))
                entry["wa"] = new
                changed = True
                hits += 1
        if changed:
            b.sent = sent
    s.commit()
    for b, pid, wa in events:
        try:
            notify.wa_delivery(s, b, pid, wa["ok"], wa["status"], wa.get("error", ""))
        except Exception:  # noqa: BLE001
            pass
    return hits


def test_pdf() -> bytes:
    return render.render_pdf("<html><body style='font-family:DejaVu Sans'><h1>Testdokument</h1>"
                             "<p>WhatsApp-Versand von ImmoVerwaltung funktioniert.</p></body></html>")


def wa_status(s: Session, data: dict) -> str:
    """Rückmeldung des n8n-Flows (zugestellt / Fehler) an der Abrechnung vermerken."""
    bid, pid = int(data.get("billing_id") or 0), int(data.get("party_id") or 0)
    ok = bool(data.get("ok"))
    info = {"ok": ok, "at": datetime.now().isoformat(timespec="seconds"),
            "status": "zugestellt" if ok else "Fehler", "error": str(data.get("error") or "")[:300],
            "provider": str(data.get("provider") or ""), "message_id": str(data.get("message_id") or "")}
    if bid == 0:  # Testnachricht
        from .db import save_settings

        save_settings(s, {"n8n_last_test": json.dumps(info, ensure_ascii=False)})
        s.commit()
        return "test"
    b = s.get(Billing, bid)
    if b is None:
        raise KeyError(bid)
    sent = {k: dict(v) for k, v in (b.sent or {}).items()}
    entry = sent.setdefault(str(pid), {})
    prev = entry.get("wa") or {}
    entry["wa"] = {**prev, **info, "to": prev.get("to", "")}
    b.sent = sent
    s.commit()
    try:
        notify.wa_delivery(s, b, pid, ok, info["status"], info["error"])
    except Exception:  # noqa: BLE001
        pass
    return "ok"


# --------------------------------------------------------------------------- Rechnungsimport
def invoice_dir() -> Path:
    base = Path(config.data_dir) if config.data_dir else (
        Path(config.database_url.removeprefix("sqlite:///")).parent
        if config.database_url.startswith("sqlite:///") else Path("data"))
    d = base / "invoices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def delete_billing(s: Session, b: Billing, block_reimport: bool = True) -> bool:
    """Abrechnung löschen; stammt sie aus dem Postfach, wird die Mail/Rechnungsnummer gesperrt, damit der
    nächste Abruf sie nicht wieder anlegt. Ergebnis: ob gesperrt wurde."""
    blocked = False
    if block_reimport and (b.mail_message_id or b.invoice_no):
        s.add(IgnoredInvoice(message_id=b.mail_message_id or "", invoice_no=b.invoice_no or "",
                             title=b.title or f"{b.period_start:%m/%Y}"))
        blocked = True
    s.delete(b)
    s.commit()
    return blocked


def is_ignored(s: Session, message_id: str = "", invoice_no: str = "") -> bool:
    q = s.query(IgnoredInvoice)
    if message_id and q.filter(IgnoredInvoice.message_id == message_id).first():
        return True
    return bool(invoice_no and q.filter(IgnoredInvoice.invoice_no == invoice_no).first())


def find_duplicate(s: Session, inv: invoice_import.ParsedInvoice, message_id: str = "") -> Billing | None:
    q = s.query(Billing)
    if inv.invoice_no:
        hit = q.filter(Billing.invoice_no == inv.invoice_no).first()
        if hit:
            return hit
    if message_id:
        return q.filter(Billing.mail_message_id == message_id).first()
    return None


def create_from_invoice(s: Session, inv: invoice_import.ParsedInvoice, pdf: bytes, filename: str,
                        message_id: str = "") -> Billing:
    """Legt einen Abrechnungs-Entwurf aus einer importierten Rechnung an."""
    st = get_settings(s)
    b = Billing(
        title=f"Nebenkosten {inv.period_start:%m/%Y}" if inv.period_start else "Nebenkosten",
        invoice_no=inv.invoice_no,
        period_start=inv.period_start,
        period_end=inv.period_end,
        grid_kwh=inv.grid_kwh,
        energy_cost_net=inv.energy_cost_net,
        fixed_cost_net=inv.fixed_cost_net,
        spot_price_ct=inv.spot_price_ct or 0.0,
        vat_rate=inv.vat_rate if inv.vat_rate is not None else (float(st["vat_rate"] or 19) / 100),
        battery_rate_ct=float(st["battery_rate_ct"].replace(",", ".") or 0),
        pv_rate_ct=float(st["pv_rate_ct"].replace(",", ".") or 0),
        values={}, amounts={}, result={}, sent={},
        import_info={**inv.info(), "filename": filename},
        mail_message_id=message_id,
        notes=f"Automatisch importiert aus {filename}",
        building_id=(default_building(s).id if default_building(s) else None),
    )
    s.add(b)
    s.flush()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename)
    path = invoice_dir() / f"{b.id}_{safe}"
    path.write_bytes(pdf)
    b.source_file = str(path)
    return b


async def import_invoice(s: Session, pdf: bytes, filename: str, message_id: str = "",
                         source: str = "Upload") -> tuple[Billing | None, str]:
    """Kompletter Ablauf: PDF lesen, Entwurf anlegen, HA-Werte laden, berechnen,
    je nach Einstellung „import_mode“ als Entwurf belassen oder automatisch abschließen
    und versenden, anschließend benachrichtigen."""
    inv = invoice_import.parse_pdf(pdf)
    if source != "Upload" and is_ignored(s, message_id, inv.invoice_no):
        return None, f"Rechnung {inv.invoice_no or filename} wurde gelöscht und ist für den Abruf gesperrt."
    dup = find_duplicate(s, inv, message_id)
    if dup:
        return dup, f"Rechnung {inv.invoice_no or filename} ist bereits erfasst."
    b = create_from_invoice(s, inv, pdf, filename, message_id)
    s.commit()
    msgs = [f"Rechnung {inv.invoice_no} ({inv.period_start:%d.%m.%Y} – {inv.period_end:%d.%m.%Y}) importiert"]
    if (config.ha_url and config.ha_token) or get_settings(s)["victron_enabled"]:
        try:
            msgs.append(fetch_message(await fetch_values(s, b)))
        except Exception as e:  # noqa: BLE001
            msgs.append(f"Home Assistant nicht erreichbar: {e}")
    result = recompute(s, b)
    warnings = list(inv.warnings) + list(result.get("warnings", []))
    st = get_settings(s)
    mode = st["import_mode"]
    if mode == "auto_always" or (mode == "auto_if_clean" and not warnings):
        # Vollautomatisch: ohne manuelle Prüfung abschließen und an alle Parteien mit Adresse senden
        b.status = "final"
        msgs.append("automatisch abgeschlossen" + (f" trotz {len(warnings)} Hinweis(en)" if warnings else ""))
        if st["portal_auto_publish"]:
            b.published = True
            msgs.append("im Mieterportal veröffentlicht")
        if can_send(st):
            s.commit()
            report = send_invoices(s, b, only_unsent=True)
            ok = sum(1 for _, good, _ in report if good)
            msgs.append(f"{ok} Abrechnung(en) versendet" + (f", {len(report) - ok} fehlgeschlagen" if len(report) > ok else ""))
        else:
            msgs.append("nicht versendet: weder SMTP noch WhatsApp (n8n) eingerichtet")
    else:
        msgs.append(f"Entwurf – bitte prüfen ({len(warnings)} Hinweis(e))" if warnings else "Entwurf – bitte prüfen")
    s.commit()
    text_msg = " · ".join(msgs)
    if st["notify_email"] and mailer.configured():
        link = f"{config.app_base_url}/admin/billings/{b.id}" if config.app_base_url else f"/admin/billings/{b.id}"
        body = (f"Neue Stromrechnung über {source} eingegangen.\n\n{text_msg}\n\n"
                + ("Hinweise:\n- " + "\n- ".join(warnings) + "\n\n" if warnings else "")
                + f"Abrechnung: {link}\n")
        facts = [("Rechnung", b.invoice_no or "–", False), ("Zeitraum", render.period_text(b), False),
                 ("Netzbezug", f"{render.fmt_num(b.grid_kwh or 0, 1)} kWh", False),
                 ("Rechnungsbetrag brutto", render.fmt_eur(result.get("bill_gross", 0)), True),
                 ("Status", text_msg, False)]
        html = mailer.render_html(
            title="Neue Stromrechnung eingegangen", preheader=text_msg,
            paragraphs=[f"Über {source} ist eine neue Rechnung eingegangen und wurde importiert."],
            facts=facts, notes=warnings, notes_title=f"{len(warnings)} Hinweis(e) – bitte prüfen",
            button_url=link if config.app_base_url else "", button_label="Abrechnung öffnen",
            footer="Automatische Nachricht von ImmoVerwaltung.")
        try:
            mailer.send_mail(_split_addr(st["notify_email"]), f"Stromrechnung importiert: {b.title}", body, [],
                             html=html)
        except Exception:  # noqa: BLE001
            pass
    # Push-Benachrichtigungen (Fehler dürfen den Import nie stören)
    try:
        if not source.startswith("Upload"):
            notify.invoice_imported(s, b, msgs[-1], result.get("bill_gross"), len(warnings))
        notify.billing_published(s, b)
    except Exception:  # noqa: BLE001
        pass
    return b, text_msg


def victron_comparison(s: Session, b: Billing) -> list[dict]:
    """Gegenüberstellung: Wert in der Abrechnung (z. B. aus HA) ↔ Victron-Logger im selben Zeitraum."""
    st = get_settings(s)
    if not st["victron_enabled"]:
        return []
    rows = []
    for field, label, keys in victron.COMPARE:
        ids = [victron.PREFIX + k for k in keys]
        vals, meta = victron.consumption(s, ids, b.period_start, b.period_end, config.timezone)
        own = calc._sum(b.values or {}, st[field]) if st[field] else None
        for vid in ids:
            if vals.get(vid) is None:
                continue
            v = vals[vid]
            dev = (own / v - 1) if own and v else None
            rows.append({"label": label, "field_value": own, "victron_key": vid,
                         "victron_label": victron.KEY_BY_NAME[vid[len(victron.PREFIX):]].label,
                         "victron_value": v, "coverage": meta[vid]["coverage"], "deviation": dev})
    return rows


async def compare_period(s: Session, t0: datetime, t1: datetime) -> list[dict]:
    """Abgleich für einen frei wählbaren Zeitraum: Haus-Felder (HA) ↔ Victron-Logger."""
    st = get_settings(s)
    rows = []
    ha_specs = {f: [e for e in calc.split_ids(st[f]) if not victron.is_victron(e) and not vrm.is_vrm(e)]
                for f, _, _ in victron.COMPARE}
    ha_ids = sorted({e for ids in ha_specs.values() for e in ids})
    ha_vals, ha_meta, ha_error = {}, {}, ""
    if ha_ids and config.ha_url and config.ha_token:
        try:
            ha_vals, ha_meta = await HAClient(config.ha_url, config.ha_token).consumption_detail(
                ha_ids, None, None, config.timezone, bounds=(t0, t1))
        except Exception as e:  # noqa: BLE001
            ha_error = str(e)
    for field, label, keys in victron.COMPARE:
        ids = ha_specs[field]
        ha_value = calc._sum(ha_vals, " + ".join(ids)) if ids else None
        vids = [victron.PREFIX + k for k in keys]
        v_vals, v_meta = victron.consumption(s, vids, None, None, config.timezone, bounds=(t0, t1))
        for vid in vids:
            v = v_vals.get(vid)
            rows.append({
                "label": label, "ha_spec": " + ".join(ids), "ha_value": ha_value,
                "ha_coverage": min((ha_meta.get(e, {}).get("coverage", 1.0) for e in ids), default=None),
                "victron_key": vid, "victron_label": victron.KEY_BY_NAME[vid[len(victron.PREFIX):]].label,
                "victron_value": v, "coverage": (v_meta.get(vid) or {}).get("coverage"),
                "deviation": (v / ha_value - 1) if (v is not None and ha_value) else None,
                "ha_error": ha_error,
            })
    rows += await _vrm_rows(st, ha_specs, ha_vals, (t0, t1), ha_error)
    return rows


async def _vrm_rows(st: dict, ha_specs: dict, ha_vals: dict, bounds, ha_error: str) -> list[dict]:
    """Zusätzliche Vergleichszeilen aus VRM (falls Token und Anlagen-ID gesetzt)."""
    if not (vrm.configured() and st["vrm_site_id"]):
        return []
    ids = [vrm.PREFIX + k for _, _, keys in vrm.COMPARE for k in keys]
    try:
        vals, _ = await vrm.consumption(st["vrm_site_id"], ids, None, None, config.timezone, bounds=bounds)
    except Exception as e:  # noqa: BLE001
        return [{"label": "VRM", "ha_spec": "", "ha_value": None, "ha_coverage": None, "victron_key": "",
                 "victron_label": f"VRM-Fehler: {e}", "victron_value": None, "coverage": None,
                 "deviation": None, "ha_error": ha_error}]
    rows = []
    for field, label, keys in vrm.COMPARE:
        ids_f = ha_specs.get(field, [])
        ha_value = calc._sum(ha_vals, " + ".join(ids_f)) if ids_f else None
        for k in keys:
            v = vals.get(vrm.PREFIX + k)
            rows.append({"label": label, "ha_spec": " + ".join(ids_f), "ha_value": ha_value, "ha_coverage": None,
                         "victron_key": vrm.PREFIX + k, "victron_label": "VRM (Cloud) – " + vrm.label(k),
                         "victron_value": v, "coverage": 1.0 if v is not None else None,
                         "deviation": (v / ha_value - 1) if (v is not None and ha_value) else None,
                         "ha_error": ha_error})
    return rows


def daily_report(s: Session, b: Billing, st: dict, db_parties: list, allocs: list) -> dict:
    """Tageswerte für Seite 2 der Abrechnung: Verbrauch je Partei und Energiemix des Hauses."""
    from datetime import timedelta as _td

    vm = b.values_meta or {}
    days = []
    d = b.period_start
    while d <= b.period_end:
        days.append(d.isoformat())
        d += _td(days=1)

    def daily_of(spec: str) -> Optional[dict[str, float]]:
        ids = calc.split_ids(spec)
        if not ids or any("daily" not in (vm.get(e) or {}) for e in ids):
            return None
        out: dict[str, float] = {}
        for e in ids:
            for day, v in vm[e]["daily"].items():
                out[day] = out.get(day, 0.0) + v
        return out

    owner = next((p for p in db_parties if p.is_owner), None)
    parties: dict[int, list] = {}
    for p in db_parties:
        if owner is not None and p.id == owner.id:
            continue
        series = [daily_of(m) for m in (p.meters or [])]
        if p.meters and all(x is not None for x in series):
            parties[p.id] = [round(sum(x.get(day, 0.0) for x in series), 3) for day in days]
    total = daily_of(st["entity_total"]) if st["entity_total"] else None
    if owner is not None and total is not None and all(
            (p.id in parties) for p in db_parties if p.id != owner.id and p.meters):
        alloc_daily = [daily_of(a.source_entity) for a in allocs if a.source_type == "energy" and a.source_entity]
        if all(x is not None for x in alloc_daily):
            parties[owner.id] = [round(max(0.0, total.get(day, 0.0)
                                           - sum(parties[pid][i] for pid in parties)
                                           - sum(x.get(day, 0.0) for x in alloc_daily)), 3)
                                 for i, day in enumerate(days)]

    mix = None
    ent = energy_entities(st)
    needed = [x for x in (ent.total, ent.grid, ent.battery_discharge) if x]
    if ent.total and ent.grid and all(daily_of(x) is not None for x in needed):
        specs = [x for x in (ent.total, ent.grid, ent.battery_discharge, ent.battery_charge_total,
                             ent.battery_charge_grid, ent.pv_direct) if x]
        per_spec = {x: daily_of(x) or {} for x in specs}
        mix = {k: [] for k, _ in calc.SOURCES}
        for day in days:
            vals = {}
            for spec, series in per_spec.items():
                ids = calc.split_ids(spec)
                vals[ids[0]] = series.get(day, 0.0)  # Summe einer Angabe auf die erste ID legen
                for extra in ids[1:]:
                    vals[extra] = 0.0
            day_bill = calc.BillCfg(grid_kwh=0, energy_cost_net=0, fixed_cost_net=0, vat_rate=0)
            m = calc.energy_mix(day_bill, ent, vals, [])
            for k, _ in calc.SOURCES:
                mix[k].append(round(m["kwh"][k], 3))
    # Umlagen mit Mengen-/Energiequelle (z. B. Trinkwasser m³): Haus je Tag und Anteil je Partei
    alloc_rows = []
    for a in allocs:
        if a.source_type not in ("quantity", "energy") or not a.source_entity:
            continue
        house = daily_of(a.source_entity)
        if house is None:
            continue
        house_list = [round(house.get(day, 0.0), 4) for day in days]
        unit = a.source_unit if a.source_type == "quantity" else "kWh"
        shares: dict[str, list] = {}
        if a.key_type == "entity":
            keyed = {pid: daily_of(str(spec)) for pid, spec in a.key.items()}
            if keyed and all(x is not None for x in keyed.values()):
                for pid, ser in keyed.items():
                    shares[str(pid)] = [round(ser.get(day, 0.0), 4) for day in days]
        else:
            if a.key_type == "percent":
                weights = {pid: float(v) for pid, v in a.key.items() if v}
            else:
                weights = {p.id: 1.0 for p in db_parties}
            wsum = sum(weights.values())
            if wsum > 0:
                for pid, w in weights.items():
                    shares[str(pid)] = [round(v * w / wsum, 4) for v in house_list]
        alloc_rows.append({"id": a.id, "name": a.name, "type": a.source_type, "unit": unit,
                           "house": house_list, "parties": shares})
    return {"days": days, "parties": {str(k): v for k, v in parties.items()}, "mix": mix, "allocs": alloc_rows}
