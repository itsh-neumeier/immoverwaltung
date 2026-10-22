"""Reine Berechnungslogik der Nebenkostenabrechnung (ohne Datenbank / Home Assistant).

Energiemix
----------
Der Gesamtverbrauch des Hauses (Victron) wird für den Abrechnungszeitraum in vier
Quellen zerlegt:

* **Netzstrom direkt**  = Netzbezug laut Rechnung (sonst Zähler) - Netz->Batterie
* **Batterie aus Netz** (Graustrom, dynamisches ESS) = Batterie-Entladung x Anteil Netzladung
* **Batterie aus PV**   = Batterie-Entladung x (1 - Anteil Netzladung)
* **PV direkt**         = Rest (oder eigene Entität)

Anteil Netzladung = Netz->Batterie / Batterie geladen gesamt.

Preise je kWh
-------------
* Netzstrom: Ø Arbeitspreis lt. Rechnung **brutto**
* PV direkt: Ø Börsenpreis netto lt. Rechnung + PV-Bereitstellungssatz (ohne MwSt.)
* Batterie aus Netz: Ø Börsenpreis netto + Batterieverschleißsatz (ohne MwSt.)
* Batterie aus PV: Ø Börsenpreis netto + PV-Bereitstellungssatz + Batterieverschleißsatz
  (ohne MwSt.)

Jede Partei (Summe ihrer Shelly-Zähler) bekommt denselben Mix. Der Eigentümer
bekommt den Restverbrauch. Fixkosten des Anbieters werden gleichmäßig verteilt,
weitere Fixkosten (z. B. IPTV) auf die ausgewählten Parteien. Umlagen verteilen
Strom eines Verbrauchers (z. B. Warmwasserbereitung, kWh zum Hausstrom-Mix), eine
Menge x Preis (z. B. Trinkwasser m³ x €/m³) oder einen Betrag nach Prozent,
Verbrauch je Partei oder gleichmäßig.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional


@dataclass
class PartyCfg:
    id: int
    name: str
    meters: list[str] = field(default_factory=list)
    is_owner: bool = False
    address: str = ""
    unit: str = ""


@dataclass
class FixedCostCfg:
    name: str
    amount_gross: float
    party_ids: list[int] = field(default_factory=list)  # leer = alle Parteien
    id: int = 0


@dataclass
class AllocationCfg:
    id: int
    name: str
    source_type: str  # "energy" (kWh, Hausstrom-Mix) | "quantity" (Menge x Preis, z. B. m³) | "amount" (EUR)
    source_entity: str = ""
    amount: float = 0.0  # bei "amount": Betrag; bei "quantity": Preis je Einheit (brutto)
    source_unit: str = ""  # Einheit der Menge bei "quantity", z. B. m³
    price_parts: list[tuple[str, float]] = field(default_factory=list)  # z. B. [("Wasser", 2.1), ("Abwasser", 2.7)]
    key_type: str = "equal"  # "entity" | "percent" | "equal"
    key: dict[int, str | float] = field(default_factory=dict)
    key_unit: str = ""


@dataclass
class BillCfg:
    grid_kwh: float
    energy_cost_net: float
    fixed_cost_net: float
    vat_rate: float  # z. B. 0.19
    wear_rate_ct: float = 0.0  # Batterieverschleißsatz ct/kWh (ohne MwSt.)
    spot_price_ct: float = 0.0  # Ø Börsenpreis netto ct/kWh lt. Rechnung
    pv_rate_ct: float = 0.0  # PV-Bereitstellungssatz ct/kWh (ohne MwSt.)
    owner_free_own_energy: bool = True  # Eigentümer zahlt keinen PV-/Batterie-/Graustrom (eigene Anlage)
    period_days: int = 0  # Tage im Abrechnungszeitraum (für die Toleranz der Zählerkontrolle)


@dataclass
class EnergyEntities:
    total: str = ""  # Gesamtverbrauch Haus (Victron)
    grid: str = ""  # Netzbezug Stromzähler
    battery_discharge: str = ""  # Batterie entladen
    battery_charge_total: str = ""  # Batterie geladen gesamt
    battery_charge_grid: str = ""  # Batterie aus Netz geladen (dyn. ESS)
    pv_direct: str = ""  # optional: PV -> Verbraucher


# Energiequellen: Schlüssel, Bezeichnung
SOURCES = [
    ("grid", "Netzstrom"),
    ("pv", "PV-Strom direkt"),
    ("bat_pv", "Batteriestrom aus PV"),
    ("bat_grid", "Batteriestrom aus Netz (Graustrom)"),
]


@dataclass
class Line:
    label: str
    amount: float
    quantity: Optional[float] = None
    unit: str = ""
    unit_price: Optional[float] = None
    note: str = ""
    vat_included: bool = False  # nur Posten der Lieferantenrechnung (Netzstrom, Fixkosten) enthalten MwSt.
    group: str = "strom"  # strom | fix | umlage – Gliederung der Abrechnung
    ref: str = ""  # Herkunft: fc:<id> (Fixkosten) bzw. al:<id> (Umlage) – für Rücklagen

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "amount": self.amount,
            "quantity": self.quantity,
            "unit": self.unit,
            "unit_price": self.unit_price,
            "note": self.note,
            "vat_included": self.vat_included,
            "group": self.group,
            "ref": self.ref,
        }


# Gliederung der Abrechnung (Reihenfolge = Reihenfolge auf der Rechnung)
GROUPS = [
    ("strom", "Strom"),
    ("fix", "Fixkosten"),
    ("umlage", "Umlagen"),
]


def _r(x: float, digits: int = 2) -> float:
    """Kaufmännisch runden (0,005 -> 0,01)."""
    q = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(x)).quantize(q, rounding=ROUND_HALF_UP)) + 0.0


def _de(x: float, digits: int = 2) -> str:
    """Zahl im deutschen Format (1.234,56)."""
    return f"{x:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _pct(x: float) -> str:
    return _de(x * 100, 1) + " %"


def split_ids(spec: str) -> list[str]:
    """„sensor.a + sensor.b“ (auch Komma/Leerzeichen) -> Liste der Entitäten."""
    return [e for e in re.split(r"[\s,;+]+", spec or "") if e]


def _sum(values: dict[str, Optional[float]], spec: str) -> Optional[float]:
    """Summe mehrerer Entitäten; None, wenn keine davon einen Wert hat."""
    vals = [values.get(e) for e in split_ids(spec)]
    vals = [float(v) for v in vals if v is not None]
    return sum(vals) if vals else None


def _val(values: dict[str, Optional[float]], entity: str) -> float:
    if not entity:
        return 0.0
    v = values.get(entity)
    return float(v) if v is not None else 0.0


def _split_equal(amount: float, n: int) -> list[float]:
    """Teilt einen Betrag centgenau auf n Teile (Rundungsrest auf die ersten)."""
    if n <= 0:
        return []
    cents = round(amount * 100)
    base, rest = divmod(cents, n)
    return [(base + (1 if i < rest else 0)) / 100 for i in range(n)]


def missing_entities(required: list[str], values: dict[str, Optional[float]]) -> list[str]:
    return [e for e in required if e and values.get(e) is None]


def energy_mix(bill: BillCfg, ent: EnergyEntities, values: dict[str, Optional[float]],
               warnings: list[str]) -> dict:
    """Zerlegt den Gesamtverbrauch in die vier Quellen (kWh und Anteile)."""
    total = _sum(values, ent.total) if ent.total else None
    discharge = _sum(values, ent.battery_discharge) or 0.0
    charge_total = _sum(values, ent.battery_charge_total) or 0.0
    charge_grid = _sum(values, ent.battery_charge_grid) or 0.0
    grid_meter = _sum(values, ent.grid) if ent.grid else None
    # Maßgeblich ist der Netzbezug laut Rechnung (amtlicher Zähler, inkl. Zählerwechsel);
    # ein Zähler aus HA / Victron dient nur zur Kontrolle und als Rückfall ohne Rechnungswert.
    grid_import = bill.grid_kwh if bill.grid_kwh and bill.grid_kwh > 0 else grid_meter

    if charge_total > 0:
        grey_frac = min(1.0, max(0.0, charge_grid / charge_total))
    elif charge_grid > 0:
        grey_frac = 1.0
        warnings.append("„Batterie geladen gesamt“ fehlt – Batteriestrom wird komplett als Graustrom gerechnet.")
    else:
        grey_frac = 0.0

    kwh = {k: 0.0 for k, _ in SOURCES}
    if total is None or total <= 0:
        if ent.total:
            warnings.append("Kein Gesamtverbrauch vorhanden – gesamter Verbrauch wird als Netzstrom gerechnet.")
        return {"total": total, "kwh": kwh, "share": {"grid": 1.0, "pv": 0.0, "bat_pv": 0.0, "bat_grid": 0.0},
                "grey_frac": grey_frac, "grid_import": grid_import, "grid_meter": grid_meter}

    kwh["bat_grid"] = discharge * grey_frac
    kwh["bat_pv"] = discharge * (1 - grey_frac)
    kwh["grid"] = max(0.0, (grid_import or 0.0) - charge_grid)
    if ent.pv_direct:
        kwh["pv"] = _sum(values, ent.pv_direct) or 0.0
    else:
        kwh["pv"] = max(0.0, total - kwh["grid"] - discharge)

    s = sum(kwh.values())
    if s <= 0:
        share = {"grid": 1.0, "pv": 0.0, "bat_pv": 0.0, "bat_grid": 0.0}
    else:
        if abs(s - total) / total > 0.05:
            warnings.append(
                f"Summe der Quellen ({_de(s, 1)} kWh) weicht vom Gesamtverbrauch ({_de(total, 1)} kWh) ab – "
                "Anteile werden normiert. Entitäten prüfen."
            )
        share = {k: v / s for k, v in kwh.items()}
    return {"total": total, "kwh": kwh, "share": share, "grey_frac": grey_frac,
            "grid_import": grid_import, "grid_meter": grid_meter}


def compute(
    bill: BillCfg,
    parties: list[PartyCfg],
    fixed_costs: list[FixedCostCfg],
    allocations: list[AllocationCfg],
    values: dict[str, Optional[float]],
    ent: Optional[EnergyEntities] = None,
) -> dict:
    ent = ent or EnergyEntities()
    warnings: list[str] = []

    # --- Preise aus der Rechnung -------------------------------------------------
    if bill.grid_kwh > 0:
        price_net = bill.energy_cost_net / bill.grid_kwh
    else:
        price_net = 0.0
        warnings.append("Bezogene kWh laut Rechnung fehlen – Durchschnittspreis = 0.")
    price_gross = price_net * (1 + bill.vat_rate)
    spot = bill.spot_price_ct / 100.0
    if not bill.spot_price_ct:
        warnings.append("Ø Börsenpreis lt. Rechnung fehlt – PV- und Batteriestrom ohne Energiepreis gerechnet.")
    pv_rate = bill.pv_rate_ct / 100.0
    wear = bill.wear_rate_ct / 100.0
    bill_gross = (bill.energy_cost_net + bill.fixed_cost_net) * (1 + bill.vat_rate)

    prices = {
        "grid": price_gross,
        "pv": spot + pv_rate,
        "bat_grid": spot + wear,
        "bat_pv": spot + pv_rate + wear,
    }
    price_notes = {
        "grid": "Ø Arbeitspreis lt. Rechnung inkl. MwSt.",
        "pv": f"Ø Börsenpreis {_de(spot*100)} ct + PV-Bereitstellung {_de(pv_rate*100)} ct, ohne MwSt.",
        "bat_grid": f"Ø Börsenpreis {_de(spot*100)} ct + Batterieverschleiß {_de(wear*100)} ct, ohne MwSt.",
        "bat_pv": (f"Ø Börsenpreis {_de(spot*100)} ct + PV-Bereitstellung {_de(pv_rate*100)} ct"
                   f" + Batterieverschleiß {_de(wear*100)} ct, ohne MwSt."),
    }

    # --- Messwerte ---------------------------------------------------------------
    mix = energy_mix(bill, ent, values, warnings)
    total_kwh = mix["total"]
    grid_meter_kwh = mix["grid_meter"]
    share = mix["share"]

    if grid_meter_kwh is not None and bill.grid_kwh > 0:
        diff = abs(grid_meter_kwh - bill.grid_kwh)
        dev = diff / bill.grid_kwh
        # Toleranz: 5 % UND 0,25 kWh je Tag – kleine Dauerabweichungen (z. B. Messung um 0 W bei ESS-Regelung)
        # fallen bei sehr kleinem Netzbezug sonst prozentual groß aus
        if dev > 0.05 and diff > 0.25 * (bill.period_days or 0):
            warnings.append(
                f"Netzbezug laut Zähler ({_de(grid_meter_kwh, 1)} kWh) weicht um {_pct(dev)} "
                f"({_de(diff, 1)} kWh) von der Rechnung ({_de(bill.grid_kwh, 1)} kWh) ab."
            )

    def energy_cost(kwh: float) -> dict[str, tuple[float, float]]:
        """-> {quelle: (kWh, EUR)}"""
        return {k: (kwh * share[k], kwh * share[k] * prices[k]) for k, _ in SOURCES}

    # --- Verbrauch je Partei -----------------------------------------------------
    party_kwh: dict[int, float] = {}
    for p in parties:
        party_kwh[p.id] = sum(_sum(values, m) or 0.0 for m in p.meters)

    energy_alloc_kwh = sum(
        _sum(values, a.source_entity) or 0.0 for a in allocations if a.source_type == "energy"
    )

    owners = [p for p in parties if p.is_owner]
    if len(owners) > 1:
        warnings.append("Mehr als eine Partei ist als Eigentümer markiert – Rest geht an die erste.")
    owner = owners[0] if owners else None
    rest_kwh = None
    if owner is not None and total_kwh is not None:
        others = sum(v for pid, v in party_kwh.items() if pid != owner.id)
        rest_kwh = total_kwh - others - energy_alloc_kwh
        if rest_kwh < 0:
            warnings.append(
                f"Restverbrauch für {owner.name} wäre negativ ({rest_kwh:.1f} kWh) – auf 0 gesetzt. "
                "Zählerzuordnung prüfen."
            )
            rest_kwh = 0.0
        party_kwh[owner.id] = rest_kwh
    elif owner is None and total_kwh is not None:
        unassigned = total_kwh - sum(party_kwh.values()) - energy_alloc_kwh
        if unassigned > 0.5:
            warnings.append(
                f"{unassigned:.1f} kWh sind keiner Partei zugeordnet (keine Eigentümer-Partei festgelegt)."
            )

    lines: dict[int, list[Line]] = {p.id: [] for p in parties}

    for p in parties:
        cost = energy_cost(party_kwh[p.id])
        is_rest = owner is not None and p.id == owner.id and rest_kwh is not None
        own_free = bill.owner_free_own_energy and owner is not None and p.id == owner.id
        for k, label in SOURCES:
            q, eur = cost[k]
            if k != "grid" and share[k] <= 0:
                continue
            if own_free and k != "grid":
                continue  # Eigentümer: eigene Anlage, wird unten als Infozeile ohne Berechnung ausgewiesen
            lines[p.id].append(
                Line(label + (" (Restverbrauch Haus)" if is_rest and k == "grid" else ""), _r(eur), _r(q, 3),
                     "kWh", prices[k], note=price_notes[k], vat_included=(k == "grid"))
            )

        if own_free:
            own_kwh = sum(cost[k][0] for k, _ in SOURCES if k != "grid")
            if own_kwh > 0:
                lines[p.id].append(Line(
                    "Eigenverbrauch aus eigener Anlage (PV direkt, Batterie, Graustrom)", 0.0, _r(own_kwh, 3), "kWh",
                    None, note="Eigentümer – ohne Berechnung", vat_included=False))

    # --- Fixkosten Stromanbieter (Rechnung) --------------------------------------
    if parties and bill.fixed_cost_net:
        fixed_gross = bill.fixed_cost_net * (1 + bill.vat_rate)
        for p, part in zip(parties, _split_equal(fixed_gross, len(parties))):
            lines[p.id].append(
                Line("Fixkosten Stromanbieter (Grundpreis/Messstelle) anteilig", part, group="fix",
                     note=f"{_de(fixed_gross)} € inkl. MwSt. lt. Rechnung / {len(parties)} Parteien",
                     vat_included=True)
            )

    # --- weitere Fixkosten -------------------------------------------------------
    for fc in fixed_costs:
        targets = [p for p in parties if not fc.party_ids or p.id in fc.party_ids]
        if not targets or not fc.amount_gross:
            continue
        for p, part in zip(targets, _split_equal(fc.amount_gross, len(targets))):
            note = f"{_de(fc.amount_gross)} € / {len(targets)} Parteien" if len(targets) > 1 else ""
            lines[p.id].append(Line(fc.name, part, note=note, group="fix", ref=f"fc:{fc.id}" if fc.id else ""))

    # --- Umlagen -----------------------------------------------------------------
    for a in allocations:
        pot_owner = None  # Strom-Umlage: Eigentümer zahlt nur den Netzanteil
        if a.source_type == "energy":
            src_kwh = _sum(values, a.source_entity) or 0.0
            ec = energy_cost(src_kwh)
            pot = sum(eur for _, eur in ec.values())
            pot_desc = f"{_de(src_kwh, 1)} kWh = {_de(pot)} € (Netzanteil inkl. MwSt. lt. Rechnung)"
            if bill.owner_free_own_energy and owner is not None:
                pot_owner = ec["grid"][1]
        elif a.source_type == "quantity":
            qty = _sum(values, a.source_entity) or 0.0
            price = sum(p for _, p in a.price_parts) if a.price_parts else a.amount
            pot = qty * price
            if a.price_parts:
                parts = " + ".join(f"{label} {_de(p)} €" for label, p in a.price_parts)
                pot_desc = f"{_de(qty, 2)} {a.source_unit} × ({parts})/{a.source_unit} = {_de(pot)} €"
            else:
                pot_desc = f"{_de(qty, 2)} {a.source_unit} × {_de(price)} €/{a.source_unit} = {_de(pot)} €"
            if not price:
                warnings.append(f"Umlage „{a.name}“: kein Preis je {a.source_unit} hinterlegt.")
        else:
            pot = a.amount
            pot_desc = f"{_de(pot)} €"
        if not pot:
            continue

        targets = [p for p in parties]
        weights: dict[int, float] = {}
        if a.key_type == "entity":
            targets = [p for p in parties if a.key.get(p.id)]
            weights = {p.id: _sum(values, str(a.key[p.id])) or 0.0 for p in targets}
        elif a.key_type == "percent":
            targets = [p for p in parties if a.key.get(p.id)]
            weights = {p.id: float(a.key[p.id]) for p in targets}
        else:
            weights = {p.id: 1.0 for p in targets}

        wsum = sum(weights.values())
        if not targets:
            warnings.append(f"Umlage „{a.name}“ hat keine Parteien zugeordnet.")
            continue
        if wsum <= 0:
            warnings.append(f"Umlage „{a.name}“: keine Verbrauchswerte – gleichmäßig verteilt.")
            weights = {p.id: 1.0 for p in targets}
            wsum = float(len(targets))

        for p in targets:
            frac = weights[p.id] / wsum
            if a.key_type == "entity":
                qty = f"{_de(weights[p.id])} {a.key_unit} von {_de(wsum)} {a.key_unit}"
                note = f"{qty} ({_pct(frac)}) von {pot_desc}"
            else:
                note = f"{_pct(frac)} von {pot_desc}"
            amount = pot * frac
            if pot_owner is not None and p.id == owner.id:
                amount = pot_owner * frac
                note += f" – Eigentümer: nur Netzanteil {_de(pot_owner)} €"
            lines[p.id].append(Line(a.name, _r(amount), note=note, group="umlage", ref=f"al:{a.id}" if a.id else ""))

    # --- Ergebnis ----------------------------------------------------------------
    result_parties = []
    for p in parties:
        order = {g: i for i, (g, _) in enumerate(GROUPS)}
        pl = sorted(lines[p.id], key=lambda l: order.get(l.group, 99))  # stabil: Reihenfolge innerhalb bleibt
        total = _r(sum(l.amount for l in pl))
        groups = [{"key": g, "label": label, "total": _r(sum(l.amount for l in pl if l.group == g))}
                  for g, label in GROUPS if any(l.group == g for l in pl)]
        result_parties.append(
            {
                "id": p.id,
                "name": p.name,
                "address": p.address,
                "unit": p.unit,
                "is_owner": p.is_owner,
                "kwh": _r(party_kwh[p.id], 3),
                "lines": [l.as_dict() for l in pl],
                "groups": groups,
                "total": total,
            }
        )

    charged = _r(sum(rp["total"] for rp in result_parties))
    return {
        "price_net": price_net,
        "price_gross": price_gross,
        "spot_price": spot,
        "pv_rate": pv_rate,
        "wear_rate": wear,
        "prices": prices,
        "sources": [{"key": k, "label": label, "kwh": _r(mix["kwh"][k], 3), "share": share[k],
                     "price": prices[k]} for k, label in SOURCES],
        "grey_frac": mix["grey_frac"],
        "total_kwh": total_kwh,
        "grid_meter_kwh": grid_meter_kwh,
        "bill_gross": _r(bill_gross),
        "charged_total": charged,
        "difference": _r(charged - bill_gross),
        "parties": result_parties,
        "warnings": warnings,
    }
