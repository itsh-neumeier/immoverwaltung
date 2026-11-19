"""Datenbankmodelle (SQLite via SQLAlchemy)."""

from __future__ import annotations

import os
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, Integer, LargeBinary, String, Text, UniqueConstraint,
                        create_engine, inspect, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import config


class Base(DeclarativeBase):
    pass


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Building(Base):
    """Gebäude/Standort (Multi-Site): Gebäude-ID, Anschrift, Foto, zuständige Verwalter."""

    __tablename__ = "buildings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), default="")  # Gebäude-ID, z. B. GID-01
    name: Mapped[str] = mapped_column(String(200), default="")  # Anzeigename, z. B. „Köttmannsdorfer Hauptstraße 56“
    address: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(String(200), default="Nebenkostenabrechnung")  # Titel der Abrechnung
    photo: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)  # zugeschnitten, quadratisch (JPEG)
    photo_updated: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    # Rücklagen: Überweisung vom Girokonto aufs Rücklagenkonto (SEPA-Datei)
    account_name: Mapped[str] = mapped_column(String(70), default="")  # Kontoinhaber Girokonto
    account_iban: Mapped[str] = mapped_column(String(34), default="")
    account_bic: Mapped[str] = mapped_column(String(11), default="")
    reserve_name: Mapped[str] = mapped_column(String(70), default="")  # Kontoinhaber Rücklagenkonto
    reserve_iban: Mapped[str] = mapped_column(String(34), default="")
    reserve_bic: Mapped[str] = mapped_column(String(11), default="")
    reserve_start: Mapped[str] = mapped_column(String(7), default="")  # JJJJ-MM: Rücklagen ab diesem Abrechnungsmonat
    sepa_format: Mapped[str] = mapped_column(String(20), default="pain.001.001.09")
    reserve_owner: Mapped[bool] = mapped_column(Boolean, default=True)  # Anteil des Eigentümers mit zurücklegen
    # monatlicher Sammelauftrag per E-Mail (XML im Anhang)
    fints_submit: Mapped[bool] = mapped_column(Boolean, default=True)  # neue Aufträge sofort per FinTS einreichen
    reserve_no_file: Mapped[bool] = mapped_column(Boolean, default=False)  # Rücklagenkonto: keine Datei-Übergabe (Sparkonto)
    sepa_mail: Mapped[bool] = mapped_column(Boolean, default=False)
    sepa_mail_to: Mapped[str] = mapped_column(String(300), default="")
    sepa_mail_day: Mapped[int] = mapped_column(Integer, default=25)  # am … des Vormonats
    sepa_mail_last: Mapped[str] = mapped_column(String(7), default="")  # JJJJ-MM des zuletzt versandten Monats
    # Kontostände laut Bank (aus dem CAMT-Kontoauszug)
    bank_main_balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bank_main_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    bank_reserve_balance: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bank_reserve_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    bank_reserve_open: Mapped[Optional[float]] = mapped_column(Float, nullable=True)  # ältester bekannter Stand
    bank_reserve_open_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    @property
    def label(self) -> str:
        return " · ".join(x for x in (self.code, self.name) if x) or f"Gebäude {self.id}"


class Party(Base):
    """Hauspartei / Mieter."""

    __tablename__ = "parties"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    unit: Mapped[str] = mapped_column(String(200), default="")  # Wohnung / Lage
    unit_id: Mapped[str] = mapped_column(String(50), default="")  # Wohneinheiten-ID, z. B. WE-001
    address: Mapped[str] = mapped_column(Text, default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")  # WhatsApp-Nummer
    channel: Mapped[str] = mapped_column(String(20), default="email")  # email | whatsapp | both
    meters: Mapped[list] = mapped_column(JSON, default=list)  # Shelly-Energie-Entitäten
    area: Mapped[float] = mapped_column(Float, default=0.0)  # Wohnfläche in m² (Verteilschlüssel)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    portal: Mapped[bool] = mapped_column(Boolean, default=False)  # Mieterportal: veröffentlichte Abrechnungen sichtbar
    building_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)


class User(Base):
    """Benutzer: Verwalter (admin) oder Mieter (tenant, an eine Partei gebunden)."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(20), default="tenant")  # superadmin | admin | tenant
    building_ids: Mapped[list] = mapped_column(JSON, default=list)  # Verwalter: zugewiesene Gebäude
    party_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)  # erhöhen = alle Sitzungen abmelden
    reset_hash: Mapped[str] = mapped_column(String(64), default="")
    reset_expires: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    # Zwei-Faktor-Anmeldung (TOTP, Authenticator-App)
    totp_secret: Mapped[str] = mapped_column(Text, default="")  # verschlüsselt (secretbox)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_last: Mapped[int] = mapped_column(Integer, default=0)  # zuletzt verwendeter Zeitschritt (keine Wiederverwendung)
    totp_recovery: Mapped[list] = mapped_column(JSON, default=list)  # SHA-256 der Wiederherstellungscodes


class BankAccess(Base):
    """FinTS-Bankzugang (PIN/TAN): Umsatz- und Saldenabruf, Überweisungen mit TAN-Freigabe."""

    __tablename__ = "bank_access"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    name: Mapped[str] = mapped_column(String(100), default="")
    blz: Mapped[str] = mapped_column(String(8), default="")
    server: Mapped[str] = mapped_column(String(255), default="")
    login: Mapped[str] = mapped_column(String(100), default="")  # Anmeldename / Legitimations-ID
    customer_id: Mapped[str] = mapped_column(String(100), default="")  # meist leer (= Anmeldename)
    pin: Mapped[str] = mapped_column(Text, default="")  # verschlüsselt – nur wenn „PIN speichern“
    tan_mechanism: Mapped[str] = mapped_column(String(10), default="")  # Sicherheitsfunktion, z. B. 923 = pushTAN
    tan_medium: Mapped[str] = mapped_column(String(100), default="")
    state: Mapped[str] = mapped_column(Text, default="")  # verschlüsselter Client-Zustand (BPD/UPD, System-ID)
    accounts: Mapped[list] = mapped_column(JSON, default=list)  # Konten mit IBAN, Produkt, erlaubten Geschäftsvorfällen
    mechanisms: Mapped[list] = mapped_column(JSON, default=list)  # angebotene TAN-Verfahren
    auto_sync: Mapped[bool] = mapped_column(Boolean, default=False)
    sync_hour: Mapped[int] = mapped_column(Integer, default=7)  # alt (eine Uhrzeit)
    sync_times: Mapped[str] = mapped_column(String(50), default="7,13,19")  # automatischer Abruf um … Uhr
    last_slot: Mapped[str] = mapped_column(String(20), default="")  # zuletzt erledigter Abruf, z. B. 2026-11-19T13
    sync_from: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # Umsätze ab (erster Abruf)
    last_sync: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_auto: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    last_msg: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(Text, default="")
    log: Mapped[list] = mapped_column(JSON, default=list)  # letzte Vorgänge (Zeit, Text)


class FinMemory(Base):
    """Lernspeicher der Kategorisierung: Empfänger (normalisiert) → Kategorie (bestätigt von Hand, KI oder Lernvorschlag)."""

    __tablename__ = "fin_memory"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(200), default="", index=True)
    label: Mapped[str] = mapped_column(String(200), default="")  # Anzeige (Empfängername)
    category_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(10), default="manuell")  # manuell | ki | lern
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AuditReport(Base):
    """Kassenprüfung: Regelprüfung der App + (optional) Bewertung durch ein KI-Modell mit pseudonymisierten Daten."""

    __tablename__ = "audit_reports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    model: Mapped[str] = mapped_column(String(60), default="")  # leer = nur Regelprüfung
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ok | error | blocked
    verdict: Mapped[str] = mapped_column(String(20), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    findings: Mapped[list] = mapped_column(JSON, default=list)  # mit echten Namen/Beträgen (nur lokal)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)  # genau das, was gesendet wurde (pseudonymisiert)
    error: Mapped[str] = mapped_column(Text, default="")
    done: Mapped[list] = mapped_column(JSON, default=list)  # erledigte Befunde (Index)


class IgnoredInvoice(Base):
    """Gelöschte Rechnung aus dem Postfach – wird beim Abruf nicht erneut importiert."""

    __tablename__ = "ignored_invoices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), default="", index=True)
    invoice_no: Mapped[str] = mapped_column(String(100), default="", index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ApiKey(Base):
    """API-Schlüssel für Webhooks (n8n, Home Assistant): Benachrichtigungen auslösen. Gespeichert wird nur der Hash."""

    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    prefix: Mapped[str] = mapped_column(String(20), default="")  # Anfang des Schlüssels zum Wiedererkennen
    key_hash: Mapped[str] = mapped_column(String(64), index=True)
    building_ids: Mapped[list] = mapped_column(JSON, default=list)  # leer = alle Gebäude
    allow_admins: Mapped[bool] = mapped_column(Boolean, default=True)  # darf auch Verwalter benachrichtigen
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_used: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    uses: Mapped[int] = mapped_column(Integer, default=0)


class ReservePlan(Base):
    """Feste Rücklage außerhalb der Nebenkostenabrechnung, z. B. Grundsteuer oder Wohngebäudeversicherung:
    Jahresbetrag wird monatlich (bzw. viertel-/halbjährlich/jährlich) aufs Rücklagenkonto übertragen."""

    __tablename__ = "reserve_plans"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    building_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    label: Mapped[str] = mapped_column(String(60), default="")  # Zweck im Verwendungszweck, z. B. GRUNDSTEUER
    annual_amount: Mapped[float] = mapped_column(Float, default=0.0)
    interval: Mapped[int] = mapped_column(Integer, default=1)  # Monate je Zuführung: 1, 3, 6, 12
    start: Mapped[str] = mapped_column(String(7), default="")  # JJJJ-MM erste Zuführung
    due_note: Mapped[str] = mapped_column(String(120), default="")  # z. B. „15.02./15.05./15.08./15.11.“
    category: Mapped[str] = mapped_column(String(20), default="immobilie")  # immobilie | privat
    funding: Mapped[str] = mapped_column(String(20), default="sepa")  # sepa | dauerauftrag | manuell (geschützt)
    keywords: Mapped[str] = mapped_column(String(200), default="")  # Stichworte zum Erkennen im Kontoauszug
    standing_order: Mapped[float] = mapped_column(Float, default=0.0)  # aktueller Dauerauftrag €/Monat (Ist)
    payee_rules: Mapped[str] = mapped_column(String(400), default="")  # Rechnung kommt von … (Hauptkonto, für Gutschriften)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    @property
    def rate(self) -> float:
        return round((self.annual_amount or 0) * (self.interval or 1) / 12, 2)


class FinCategory(Base):
    """Kategorie der privaten Finanzplanung (Hauptkonto): Budget je Monat und Regeln zum automatischen Zuordnen."""

    __tablename__ = "fin_categories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), default="")
    kind: Mapped[str] = mapped_column(String(20), default="variabel")  # variabel | fix | einnahme | ignorieren
    budget: Mapped[float] = mapped_column(Float, default=0.0)  # €/Monat (bei variabel)
    rules: Mapped[str] = mapped_column(Text, default="")  # Stichworte je Zeile/Komma; zweck:… / iban:…
    color: Mapped[str] = mapped_column(String(7), default="#64748b")
    sort: Mapped[int] = mapped_column(Integer, default=0)


class FinTxn(Base):
    """Umsatz des Hauptkontos (aus dem CAMT-/CSV-Kontoauszug) für die Finanzplanung."""

    __tablename__ = "fin_txns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_iban: Mapped[str] = mapped_column(String(34), default="", index=True)
    booking_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    name: Mapped[str] = mapped_column(String(200), default="")
    iban: Mapped[str] = mapped_column(String(34), default="")
    purpose: Mapped[str] = mapped_column(Text, default="")
    digest: Mapped[str] = mapped_column(String(64), default="", index=True)
    category_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # von Hand gesetzt (hat Vorrang)
    reserve_done: Mapped[str] = mapped_column(String(20), default="")  # Gutschrift aus Rücklage: erstattet | ignoriert
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    pending: Mapped[bool] = mapped_column(Boolean, default=False)  # vorgemerkt (noch nicht gebucht)


class FinBalance(Base):
    """Kontostand laut Bank (Endsaldo eines Tages) – Grundlage für den Spielraum."""

    __tablename__ = "fin_balances"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_iban: Mapped[str] = mapped_column(String(34), default="", index=True)
    day: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)


class BankLine(Base):
    """Umsatz vom Haupt- oder Rücklagenkonto aus dem Kontoauszug (CAMT-XML oder CSV) – zum Abgleich der Überweisungen."""

    __tablename__ = "bank_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    building_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    account: Mapped[str] = mapped_column(String(10), default="reserve")  # main | reserve
    pmtinf: Mapped[str] = mapped_column(String(100), default="")  # Sammler-Referenz (PmtInfId/MsgId) bei Sammelbuchung
    batch_count: Mapped[int] = mapped_column(Integer, default=0)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    booking_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    purpose: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str] = mapped_column(String(200), default="")
    iban: Mapped[str] = mapped_column(String(34), default="")
    e2e: Mapped[str] = mapped_column(String(100), default="")
    info: Mapped[str] = mapped_column(String(100), default="")  # z. B. „Umsatz gebucht“ / „vorgemerkt“
    digest: Mapped[str] = mapped_column(String(64), default="", index=True)  # gegen doppelten Import
    entry_ids: Mapped[list] = mapped_column(JSON, default=list)  # zugeordnete Rücklagen-Buchungen
    source: Mapped[str] = mapped_column(String(20), default="")  # camt.053.001.08, csv …
    pending: Mapped[bool] = mapped_column(Boolean, default=False)  # vorgemerkt (noch nicht gebucht)


class SepaExport(Base):
    """Erzeugte SEPA-Sammelüberweisung (pain.001) – zum Hochladen ins Online-Banking (Datei-Übergabe)."""

    __tablename__ = "sepa_exports"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    building_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(10), default="in")  # in = Zuführung | out = Entnahme
    msg_id: Mapped[str] = mapped_column(String(35), default="")
    exec_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[float] = mapped_column(Float, default=0.0)
    filename: Mapped[str] = mapped_column(String(100), default="")
    xml: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(10), default="open")  # open = erzeugt | done = Ausführung bestätigt
    manual: Mapped[bool] = mapped_column(Boolean, default=False)  # statt Datei: eine Einzelüberweisung von Hand
    manual_purpose: Mapped[str] = mapped_column(String(140), default="")  # Verwendungszweck mit Kennung
    token: Mapped[str] = mapped_column(String(16), default="", index=True)  # zufällige Kennung, z. B. RG-K7QX9M2T
    confirmed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[str] = mapped_column(String(100), default="")


class ReserveEntry(Base):
    """Buchung im Rücklagenkonto je Zweck: Zuführung (+), Entnahme (−), Anfangsbestand."""

    __tablename__ = "reserve_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    building_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    ref: Mapped[str] = mapped_column(String(20), default="", index=True)  # fc:<id> | al:<id>
    name: Mapped[str] = mapped_column(String(200), default="")
    booking_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    kind: Mapped[str] = mapped_column(String(10), default="in")  # in | out | start
    billing_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    party_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    period: Mapped[str] = mapped_column(String(7), default="")  # JJJJ-MM bei festen Rücklagen (Plan)
    e2e: Mapped[str] = mapped_column(String(35), default="", index=True)  # Kundenreferenz (End-to-End) der Überweisung
    bank_line_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Umsatz Rücklagenkonto (Kontoauszug)
    main_line_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # Umsatz Hauptkonto (Kontoauszug)
    fin_txn_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # erstattete Rechnung (Hauptkonto-Umsatz)
    export_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    note: Mapped[str] = mapped_column(String(140), default="")  # Verwendungszweck bzw. Notiz


class PushSubscription(Base):
    """Web-Push-Abo eines Geräts (PWA/Browser) für einen Benutzer."""

    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)  # None = ohne Login (Verwalter)
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_ok: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)


class Message(Base):
    """Mitteilung des Verwalters an alle Parteien (Broadcast) oder ausgewählte Parteien (Unicast)."""

    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    party_ids: Mapped[list] = mapped_column(JSON, default=list)  # leer = alle Parteien
    sender: Mapped[str] = mapped_column(String(100), default="")
    stats: Mapped[dict] = mapped_column(JSON, default=dict)  # Zustellung: Push-Geräte, E-Mails
    category: Mapped[str] = mapped_column(String(20), default="info")  # info | termin | wartung | abschaltung | ok
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)  # dauerhaft oben anzeigen
    event_start: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)  # geplantes Ereignis
    event_end: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    show_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    remind: Mapped[bool] = mapped_column(Boolean, default=False)  # Erinnerung am Vortag
    reminded_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    priority: Mapped[str] = mapped_column(String(10), default="normal")  # low | normal | high | urgent


class FixedCost(Base):
    """Weitere Fixkosten je Abrechnung, z. B. IPTV-Bereitstellung."""

    __tablename__ = "fixed_costs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    building_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    amount_gross: Mapped[float] = mapped_column(Float, default=0.0)
    party_ids: Mapped[list] = mapped_column(JSON, default=list)  # leer = alle
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    split: Mapped[str] = mapped_column(String(10), default="equal")  # equal (gleichmäßig) | area (nach Wohnfläche)
    reserve: Mapped[bool] = mapped_column(Boolean, default=False)  # zeitversetzt bezahlt → Rücklage bilden
    reserve_label: Mapped[str] = mapped_column(String(60), default="")  # Zweck im Verwendungszweck, z. B. IPTV
    payee_rules: Mapped[str] = mapped_column(String(400), default="")  # Rechnung kommt von … (Hauptkonto, für Gutschriften)


class Allocation(Base):
    """Umlage, z. B. Warmwasserbereitung (Strom eines Shelly) oder Trinkwasser (m³ x Preis)."""

    __tablename__ = "allocations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    building_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(20), default="energy")  # energy | quantity | amount
    source_entity: Mapped[str] = mapped_column(String(255), default="")
    source_unit: Mapped[str] = mapped_column(String(20), default="m³")  # Einheit bei quantity
    price_source: Mapped[str] = mapped_column(String(20), default="custom")  # custom | water (Einstellungen)
    default_amount: Mapped[float] = mapped_column(Float, default=0.0)  # Betrag bzw. Preis je Einheit
    key_type: Mapped[str] = mapped_column(String(20), default="percent")  # entity | percent | equal
    key_unit: Mapped[str] = mapped_column(String(20), default="")
    key: Mapped[dict] = mapped_column(JSON, default=dict)  # {party_id: entity | prozent}
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    reserve: Mapped[bool] = mapped_column(Boolean, default=False)  # zeitversetzt bezahlt → Rücklage bilden
    reserve_label: Mapped[str] = mapped_column(String(60), default="")  # Zweck im Verwendungszweck, z. B. WASSERGELD
    payee_rules: Mapped[str] = mapped_column(String(400), default="")  # Rechnung kommt von … (Hauptkonto, für Gutschriften)


class Billing(Base):
    """Eine Abrechnung = eine Rechnung des Stromanbieters."""

    __tablename__ = "billings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    invoice_no: Mapped[str] = mapped_column(String(100), default="")
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    grid_kwh: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    energy_cost_net: Mapped[float] = mapped_column(Float, default=0.0)
    fixed_cost_net: Mapped[float] = mapped_column(Float, default=0.0)
    vat_rate: Mapped[float] = mapped_column(Float, default=0.19)
    battery_rate_ct: Mapped[float] = mapped_column(Float, default=0.0)  # Batterieverschleißsatz
    spot_price_ct: Mapped[float] = mapped_column(Float, default=0.0)  # Ø Börsenpreis netto lt. Rechnung
    pv_rate_ct: Mapped[float] = mapped_column(Float, default=0.0)  # PV-Bereitstellungssatz
    sent: Mapped[dict] = mapped_column(JSON, default=dict)  # party_id -> Versandzeitpunkt / Fehler
    source_file: Mapped[str] = mapped_column(String(255), default="")  # importierte Original-Rechnung (PDF)
    import_info: Mapped[dict] = mapped_column(JSON, default=dict)  # Positionen / Prüfungen des Imports
    mail_message_id: Mapped[str] = mapped_column(String(255), default="")
    values: Mapped[dict] = mapped_column(JSON, default=dict)  # entity_id -> Verbrauch
    values_meta: Mapped[dict] = mapped_column(JSON, default=dict)  # entity_id -> Methode/Abdeckung aus HA
    amounts: Mapped[dict] = mapped_column(JSON, default=dict)  # allocation_id -> Betrag
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | final
    published: Mapped[bool] = mapped_column(Boolean, default=False)  # im Mieterportal sichtbar (nur wenn final)
    building_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    notified: Mapped[list] = mapped_column(JSON, default=list)  # Benutzer-IDs, die per Push benachrichtigt wurden
    notes: Mapped[str] = mapped_column(Text, default="")
    fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class VictronBucket(Base):
    """Victron direkt: Energie je 15-Minuten-Block und Schlüssel."""

    __tablename__ = "victron_buckets"
    __table_args__ = (UniqueConstraint("start", "key"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    start: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    key: Mapped[str] = mapped_column(String(64), index=True)
    kwh: Mapped[float] = mapped_column(Float, default=0.0)
    seconds: Mapped[float] = mapped_column(Float, default=0.0)  # abgedeckte Zeit


class VictronState(Base):
    """Victron direkt: letzter gelesener Zählerstand je Schlüssel."""

    __tablename__ = "victron_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    raw: Mapped[float] = mapped_column(Float)
    ts: Mapped[datetime] = mapped_column(DateTime)  # UTC


if config.database_url.startswith("sqlite:///"):
    os.makedirs(os.path.dirname(config.database_url.removeprefix("sqlite:///")) or ".", exist_ok=True)

engine = create_engine(config.database_url, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _add_missing_columns()
    _rename_legacy_titles()
    _migrate_multisite()
    _backfill_reserve_e2e()


def _backfill_reserve_e2e() -> None:
    """Rücklagen-Buchungen aus der ersten Version: End-to-End-Referenz nachtragen (für eindeutige Treffer beim Abgleich)."""
    with SessionLocal() as s:
        for e in s.query(ReserveEntry).filter(ReserveEntry.e2e == "", ReserveEntry.kind == "in",
                                              ReserveEntry.billing_id.isnot(None), ReserveEntry.party_id.isnot(None),
                                              ReserveEntry.export_id.isnot(None)):
            e.e2e = f"RL{e.billing_id}P{e.party_id}{e.ref.replace(':', '')}"
        s.commit()


def _migrate_multisite() -> None:
    """Einmalig: erstes Gebäude aus den bisherigen Einstellungen anlegen, alle Parteien/Abrechnungen zuordnen,
    bisherige Verwalter zu Super-Admins machen (sie hatten vorher Zugriff auf alles)."""
    with SessionLocal() as s:
        if s.query(Building).first() is None:
            st = get_settings(s)
            b = Building(code=st.get("building_id", "") or "GID-01",
                         name=(st.get("building_address", "") or "").split(",")[0].strip() or "Mein Gebäude",
                         address=st.get("building_address", ""), title=st.get("building_title") or "Nebenkostenabrechnung")
            s.add(b)
            s.flush()
            s.query(Party).filter(Party.building_id.is_(None)).update({Party.building_id: b.id})
            s.query(Billing).filter(Billing.building_id.is_(None)).update({Billing.building_id: b.id})
        if s.query(User).filter(User.role == "superadmin").first() is None:
            s.query(User).filter(User.role == "admin").update({User.role: "superadmin"})
        first = s.query(Building).order_by(Building.id).first()
        if first is not None:  # Fixkosten/Umlagen ohne Gebäude → erstes Gebäude
            s.query(FixedCost).filter(FixedCost.building_id.is_(None)).update({FixedCost.building_id: first.id})
            s.query(Allocation).filter(Allocation.building_id.is_(None)).update({Allocation.building_id: first.id})
        s.commit()


def _rename_legacy_titles() -> None:
    """Einmalige Umbenennung: „Strom MM/JJJJ“ -> „Nebenkosten MM/JJJJ“ und alte Standard-Mailtexte.
    Selbst geänderte Titel und Texte bleiben unverändert."""
    import re

    with SessionLocal() as s:
        for b in s.query(Billing).all():
            m = re.fullmatch(r"Strom (\d{2}/\d{4})", b.title or "")
            if m:
                b.title = f"Nebenkosten {m.group(1)}"
        old = {
            "mail_subject": "Nebenkostenabrechnung Strom {zeitraum} – {wohneinheit}",
            "mail_body": "Hallo {name},\n\nanbei die Nebenkostenabrechnung Strom für den Zeitraum {zeitraum}.\n"
                         "Betrag: {betrag}\n\nViele Grüße\n{absender}",
        }
        for key, old_value in old.items():
            row = s.get(Setting, key)
            if row is not None and row.value == old_value:
                row.value = SETTING_DEFAULTS[key]
        s.commit()


def _add_missing_columns() -> None:
    """Einfache Migration: neue Spalten in bestehenden Tabellen ergänzen."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if isinstance(default, bool):
                    literal = "1" if default else "0"
                elif isinstance(default, (int, float)):
                    literal = str(default)
                elif isinstance(default, str):
                    literal = "'" + default.replace("'", "''") + "'"
                elif isinstance(col.type, JSON):
                    literal = "'{}'" if col.name in ("sent", "values", "amounts", "result", "key", "import_info", "values_meta", "stats") else "'[]'"
                else:
                    literal = "NULL"
                coltype = col.type.compile(engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {coltype} DEFAULT {literal}'))


def get_session():
    with SessionLocal() as s:
        yield s


SETTING_DEFAULTS = {
    # Entitäten
    "entity_grid": "",
    "entity_total": "",
    "entity_battery": "",  # Batterie entladen
    "entity_battery_charge": "",  # Batterie geladen gesamt
    "entity_battery_charge_grid": "",  # Batterie aus Netz geladen (dyn. ESS)
    "entity_pv_direct": "",  # optional
    # Sätze
    "battery_rate_ct": "8",  # Batterieverschleißsatz
    "pv_rate_ct": "5",  # PV-Bereitstellungssatz
    "water_price_m3": "",  # Trinkwasser €/m³ brutto
    "sewage_price_m3": "",  # Abwasser €/m³ brutto
    "vat_rate": "19",
    "owner_free_own_energy": "1",  # Eigentümer zahlt keinen PV-/Batterie-/Graustrom
    # Objekt
    "building_title": "Nebenkostenabrechnung",
    "building_address": "",
    "building_id": "",
    # E-Mail
    "mail_auto_send": "",
    "mail_subject": "Nebenkostenabrechnung {zeitraum} – {wohneinheit}",
    "mail_body": "Hallo {name},\n\nanbei die Nebenkostenabrechnung für den Zeitraum {zeitraum}.\n"
                 "Betrag: {betrag}\n\nViele Grüße\n{absender}",
    "mail_bcc": "",
    # WhatsApp: n8n oder direkt über die Cloud API
    "wa_mode": "n8n",  # n8n | native
    "wa_verify_token": "",  # Prüf-Token für den Meta-Webhook (wird erzeugt)
    "n8n_webhook_url": "",
    "n8n_app_url": "",  # URL der App aus Sicht von n8n (PDF-Download, Statusmeldung)
    "n8n_secret": "",
    "n8n_pdf_base64": "1",
    "wa_provider": "evolution",  # evolution | cloud
    "wa_message": "Hallo {name},\nanbei die Nebenkostenabrechnung für {zeitraum}.\n"
                  "Betrag: {betrag}, fällig bis {faellig}.\n\nViele Grüße\n{absender}",
    "wa_template_name": "nebenkostenabrechnung",
    "wa_template_lang": "de",
    "n8n_last_test": "",
    # Login / Mieterportal
    "session_secret": "",  # wird erzeugt (oder APP_SECRET_KEY)
    "vapid_private": "",
    # Abfallkalender (ICS)
    "waste_ics_url": "",
    "waste_ics_data": "",
    "waste_fetched_at": "",
    "waste_types": "[]",  # ausgewählte Müllarten (JSON), leer = alle
    "waste_notify_evening": "1",
    "waste_evening_time": "18:00",
    "waste_notify_morning": "1",
    "waste_morning_time": "06:00",
    "waste_include_admins": "1",
    "waste_sent": "[]",  # bereits verschickte Benachrichtigungen (Schlüssel)
    "push_tenant_published": "1",
    "push_admin_import": "1",
    "push_admin_sent": "1",
    "push_admin_errors": "1",
    "push_admin_delivery": "",  # Web-Push-Schlüssel (wird erzeugt, oder VAPID_PRIVATE_KEY)
    "portal_auto_publish": "1",
    "admin_app_name": "ImmoVerwaltung",
    "admin_app_color": "#1e3a8a",  # Farbe der oberen Leiste (installierte App)
    "tenant_app_color": "#16a34a",
    # Finanzplanung (Hauptkonto)
    "fin_iban": "",
    "fints_product_id": "",
    "openai_key": "",  # verschlüsselt (secretbox)
    "openai_model": "",
    "openai_models": "",  # Modelle des Zugangs (beim Schlüsseltest abgerufen)
    "audit_monthly": "",
    "fin_ai_suggestions": "",  # letzte KI-Vorschläge zur Kategorisierung (JSON)  # Kassenprüfung einmal im Monat automatisch  # FinTS-Produktregistrierung (Deutsche Kreditwirtschaft) – nicht ins Repo!
    "totp_admins": "1",  # Verwalter müssen die Zwei-Faktor-Anmeldung einrichten  # Hauptkonto für die Finanzplanung (leer = Hauptkonto des ersten Gebäudes)
    "fin_period": "monat",  # monat (Kalendermonat) | gehalt (vom letzten Gehaltseingang bis zum nächsten)
    "fin_buffer": "100",  # Sicherheitspuffer in €
    "fin_seeded": "",
    "fin_alerts_sent": "",  # bereits gemeldete Budget-Warnungen (JJJJ-MM:Kategorie:Stufe, kommagetrennt)
    "tenant_app_name": "Mein Zuhause",  # Name der App für Mieter  # beim Abschließen im Mieterportal veröffentlichen
    # Rechnungsimport
    "imap_senders": "",  # leer = IMAP_SENDER aus der .env (Standard awattar.de)
    "imap_forwarded": "1",  # weitergeleitete Mails am Original-Absender erkennen
    "import_mode": "review",  # review | auto_if_clean | auto_always
    "notify_email": "",
    # Victron direkt (Modbus TCP, nur lesend)
    "victron_enabled": "",
    "victron_host": "",
    "victron_port": "502",
    "victron_units": "",  # gefundene Unit-IDs (JSON), leer = automatisch suchen
    # VRM (Cloud, optional) – Token kommt aus VRM_TOKEN
    "vrm_site_id": "",  # Hinweis-Mail bei neu importierter Rechnung
    "landlord_name": "",
    "landlord_address": "",
    "landlord_contact": "",
    "landlord_iban": "",
    "payment_days": "14",
    "invoice_text": "",
}


def get_settings(s: Session) -> dict[str, str]:
    out = dict(SETTING_DEFAULTS)
    for row in s.query(Setting).all():
        out[row.key] = row.value
    return out


def save_settings(s: Session, data: dict[str, str]) -> None:
    for k, v in data.items():
        if k not in SETTING_DEFAULTS:
            continue
        row = s.get(Setting, k)
        if row is None:
            s.add(Setting(key=k, value=v))
        else:
            row.value = v
    s.commit()
