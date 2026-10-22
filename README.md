# 🏠 ImmoVerwaltung

Selbst gehostete Hausverwaltung für ein Mehrparteienhaus – mit zwei Oberflächen:

- **ImmoVerwaltung** (Verwalter): monatliche **Nebenkostenabrechnung** je Partei – Strom inkl. PV/Batterie mit Messwerten
  aus **Home Assistant**, **Victron** (Modbus/VRM) und Shelly, **Fixkosten** (Grundpreis, IPTV …), **Umlagen**
  (Warmwasser, Trinkwasser …), automatischer Import der **aWATTar**-Rechnung, Versand per E-Mail/WhatsApp, PDF je Partei,
  **Mitteilungen** mit Prioritäten, **Abfallkalender**, Benutzer- und Rechteverwaltung.
- **Mein Zuhause** (Mieter): eigene Abrechnungen, Hinweise der Hausverwaltung (z. B. geplante Wasserabschaltung),
  nächste Abfuhrtermine und Push-Benachrichtigungen – im Browser oder als installierte App.

Beide lassen sich als eigene App (PWA) installieren – mit eigenem Namen und eigener Startseite.

## Ablauf pro Monat

1. Rechnung des Stromanbieters (aWATTar) kommt per E-Mail → das Tool prüft das Postfach per IMAP
   (nur lesend) und liest das PDF aus: Zeitraum, Bezug kWh, Arbeitspreis netto (alle Cent/kWh-Positionen),
   Fixkosten netto (Grundpreis, Grundentgelt), Ø Börsenpreis (Position „Stromverbrauch (HOURLY)“), MwSt.
   Alternativ PDF oder `.eml` im Webinterface hochladen oder Werte manuell erfassen.
2. Plausibilitätsprüfung: Summe der Positionen = Rechnungssumme, Ø Arbeitspreis = ausgewiesener Arbeitspreis.
   Je nach Einstellung „Nach dem Import“: **erst prüfen** (Entwurf, Standard), **automatisch, wenn keine
   Hinweise** oder **immer automatisch ohne Validierung** abschließen und per E-Mail versenden.
3. Verbrauchswerte werden aus Home Assistant geladen (Langzeitstatistik, auch Monate rückwirkend).
4. Prüfen, ggf. Werte korrigieren → **Abschließen** → PDFs je Partei bzw. alle als ZIP.
5. Versand per E-Mail an alle Parteien mit hinterlegter Adresse – automatisch beim Abschließen
   (Einstellung) oder per Knopf, mit Versandstatus je Partei.

## Berechnung

**Energiemix des Hauses** (je Abrechnungszeitraum, aus Victron + Stromzähler):

| Quelle | Menge | Preis je kWh |
|---|---|---|
| Netzstrom direkt | Netzbezug − Netz→Batterie | Ø Arbeitspreis lt. Rechnung **brutto** |
| PV-Strom direkt | Gesamtverbrauch − Netz direkt − Batterie-Entladung | Ø Börsenpreis netto + PV-Bereitstellungssatz |
| Batteriestrom aus PV | Entladung × (1 − Netzladeanteil) | Ø Börsenpreis netto + PV-Bereitstellungssatz + Batterieverschleißsatz |
| Batteriestrom aus Netz (Graustrom, dyn. ESS) | Entladung × Netzladeanteil | Ø Börsenpreis netto + Batterieverschleißsatz |

Netzladeanteil = Batterie aus Netz geladen ÷ Batterie geladen gesamt. Alles außer Netzstrom ohne MwSt.

**Je Partei**: Verbrauch (Summe der Shelly-Zähler) × Mix-Anteile × Preis. Der Eigentümer bekommt den
Restverbrauch (Gesamt − andere Parteien − Strom-Umlagen).

| Weitere Positionen | Verteilung |
|---|---|
| Fixkosten Anbieter (Grundpreis, Messstelle) | brutto ÷ Anzahl Parteien (centgenau) |
| Weitere Fixkosten (z. B. IPTV) | Betrag ÷ ausgewählte Parteien |
| Warmwasserbereitung | kWh des Shelly (HA) × Hausstrom-Mix, verteilt nach festen Prozenten; wird vom Restverbrauch des Eigentümers abgezogen |
| Trinkwasser | m³ des Wasserzählers (HA-Adapter) × (Wasser- + Abwasserpreis je m³ aus den Einstellungen), verteilt nach festen Prozenten |
| Sonstige Umlagen | fester Betrag; Verteilung auch nach Verbrauch je Partei (HA-Entität) oder gleichmäßig |

## Installation (Docker)

```bash
git clone https://github.com/itsh-neumeier/immoverwaltung.git
cd immoverwaltung
cp .env.example .env      # HA_TOKEN, APP_PASSWORD, SMTP_* und IMAP_* eintragen
docker compose up -d --build
```

- **Mein Zuhause** (Mieter): `http://<host>:8000/`
- **ImmoVerwaltung** (Verwalter): `http://<host>:8000/admin`

Daten (SQLite) liegen in `./data`. Fertige Images baut GitHub Actions nach `ghcr.io/itsh-neumeier/immoverwaltung`.

### Portainer

Fertiger Stack in [`portainer/docker-compose.yml`](portainer/docker-compose.yml), nutzt das Image
`ghcr.io/itsh-neumeier/immoverwaltung` (amd64 + arm64).

1. Portainer → **Stacks → Add stack**, Name `immoverwaltung`.
2. **Repository**: `https://github.com/itsh-neumeier/immoverwaltung`,
   Reference leer lassen (= Standard-Branch), Compose path `portainer/docker-compose.yml`
   – *oder* **Web editor** und den Dateiinhalt einfügen.
3. **Environment variables**: einzeln eintragen oder per *Load variables from .env file* die Vorlage
   [`portainer/stack.env.example`](portainer/stack.env.example) laden und ausfüllen
   (`HA_URL`, `HA_TOKEN`, `APP_PASSWORD`, `SMTP_*`, `IMAP_*` …). Die Compose-Datei setzt sie per
   `${VARIABLE}` ein – eine `stack.env`-Datei wird nicht benötigt.
4. **Deploy the stack** → `http://<host>:8000/` (Mein Zuhause) bzw. `/admin` (ImmoVerwaltung), Port über `APP_PORT`.

Daten (SQLite + Original-Rechnungen) liegen im Volume `stromabrechnung-data` – der Name bleibt aus
Kompatibilitätsgründen, damit bestehende Installationen ihre Daten behalten. Updates: Stack → **Pull and redeploy**.

**Umstellung von „abrechnung_nebenkosten“:** Das Image wird bis auf Weiteres unter beiden Namen veröffentlicht
(`…/immoverwaltung` und `…/abrechnung_nebenkosten`). Im Stack das Image auf `ghcr.io/itsh-neumeier/immoverwaltung`
umstellen (Standard der neuen `portainer/docker-compose.yml`), das neue Package auf **Public** stellen, redeployen.
Frühere Adressen (`/billings/…`, `/settings`, `/portal` …) werden automatisch auf `/admin/…` bzw. `/` umgeleitet.

### Container-Image (GHCR)

GitHub Actions testet bei jedem Push und baut danach das Image für `linux/amd64` und `linux/arm64`:

| Tag | Wann |
|---|---|
| `latest` | Push auf den Standard-Branch |
| `<branch>` | jeder Branch, z. B. `claude-stromabrechnung-hausparteien-qif4vp` |
| `sha-<commit>` | jeder Build, für feste Versionen |
| `1.2.3` | Git-Tag `v1.2.3` |

Manuell starten: *Actions → CI → Run workflow*. Sollte Portainer das Image nicht ziehen dürfen, unter
*GitHub → Profil → Packages → immoverwaltung → Package settings* die Sichtbarkeit auf **Public** stellen
(oder in Portainer die Registry `ghcr.io` mit einem Token mit `read:packages` hinterlegen).

### Home-Assistant-Token

HA → Profil → Sicherheit → *Langlebige Zugriffstoken* → erstellen → in `.env` als `HA_TOKEN`.
Der Token wird nur aus der Umgebung gelesen, nie in der Datenbank oder im Repo gespeichert.

### Anforderungen an die Entitäten

Das Tool liest ausschließlich die **Langzeitstatistik** von Home Assistant (bleibt dauerhaft erhalten,
auch Monate rückwirkend). Zwei Arten von Sensoren funktionieren:

| Sensor | Voraussetzung | Berechnung |
|---|---|---|
| **Zähler** (kWh, Wh, m³) | `state_class: total_increasing` oder `total` | Differenz im Zeitraum – Lücken unkritisch, der Zähler läuft im Gerät weiter |
| **Leistung** (W, kW) | `state_class: measurement` | Σ stündlicher Mittelwert (kW) × 1 h = kWh; negative Stundenmittel (Rückspeisung) zählen als 0 |

Bei Leistungssensoren zeigt die Abrechnung, für wie viel Prozent der Stunden HA Daten hatte, und warnt
unter 98 % (z. B. HA war aus) – fehlende Stunden würden sonst Verbrauch unterschlagen. Für den
Netzbezug und alles, was rechtlich sauber sein soll, sind Zähler vorzuziehen; für Shellys ohne
Energiezähler oder rückwirkende Auswertungen sind Leistungswerte praktisch. Energie wird in kWh,
Leistung in kW, Volumen in m³ abgefragt (HA rechnet um).

## Einrichtung im Webinterface

Jedes Zählerfeld lässt sich als **1 Entität (gesamt)** oder **3 Phasen (L1/L2/L3)** hinterlegen
(z. B. Shelly 3EM je Phase) – die Phasen werden addiert. Alle Entitätsfelder haben einen Knopf **„Aus HA wählen“**: Er lädt die Sensoren live über die
HA-API (Suche, Filter Energie/Wasser, nur mit Langzeitstatistik, aktueller Zählerstand).

1. **Einstellungen**: Entitäten für Netzbezug, Gesamtverbrauch, Batterie entladen/geladen,
   Batterie aus Netz geladen (dyn. ESS), optional PV-Direktverbrauch; PV-Bereitstellungs- und
   Batterieverschleißsatz; Objektanschrift und Gebäude-ID; Absender/IBAN; E-Mail-Vorlage.
2. **Parteien**: Wohneinheit (Name), Wohneinheiten-ID, E-Mail, WhatsApp-Nummer und Versandweg, Shelly-Entitäten; eine Partei als
   *Eigentümer* markieren (bekommt den Restverbrauch).
3. **Fixkosten & Umlagen**: „IPTV-Bereitstellung“ (Betrag, Parteien), „Trinkwasser“ (Menge × Preis,
   Wasserzähler-Entität, €/m³, Prozente) und „Warmwasserbereitung“ (Strom, Shelly-Entität, Prozente).

### WhatsApp-Versand (direkt über die Cloud API oder über n8n)

**Direkt:** offizielle WhatsApp Business Cloud API von Meta, ohne Zusatzdienst. `WA_TOKEN` (dauerhaftes Token eines
Systembenutzers), `WA_PHONE_NUMBER_ID` und `WA_APP_SECRET` in `.env` / Portainer setzen, im Menü **WhatsApp**
„direkt“ wählen. Versand je Partei: PDF hochladen → genehmigte Vorlage (Utility, Kopfzeile *Dokument*, Text mit
{{1}} Name, {{2}} Zeitraum, {{3}} Betrag) senden. Zustellstatus (gesendet → zugestellt → gelesen / Fehler) über den
Meta-Webhook `https://<APP_BASE_URL>/api/whatsapp/webhook` (Prüf-Token steht auf der Seite, Signatur wird mit
`WA_APP_SECRET` geprüft; braucht öffentliches HTTPS – ohne Webhook bleibt der Status „gesendet“).

**Über n8n:** Menü **WhatsApp**: Die App schickt je Partei einen Webhook an n8n (Text, Nummer, PDF als Base64 und
signierter Download-Link, 7 Tage gültig); der n8n-Flow stellt das PDF per WhatsApp zu und meldet das Ergebnis
zurück (💬 *zugestellt* in der Abrechnung). Zwei fertige Flows zum Kopieren bzw. Herunterladen:

- **Evolution API** (selbst gehostet, kostenlos, keine Vorlagen – inoffiziell, Sperrrisiko für die Nummer)
- **WhatsApp Business Cloud API** (Meta, offiziell – braucht eine genehmigte Vorlage mit Dokument-Kopfzeile)

Einrichtung: Flow kopieren → in n8n einfügen (Strg+V) → Knoten *Konfiguration* ausfüllen → aktivieren →
Production-URL des Webhooks und die App-URL (wie n8n die App erreicht) eintragen → „Test senden“.
Bei den Parteien WhatsApp-Nummer und Versandweg (E-Mail / WhatsApp / beides) wählen. Der Versand läuft über
dieselben Knöpfe wie die E-Mail bzw. automatisch beim Abschließen. Die Endpunkte `/api/n8n/…` und `/api/whatsapp/webhook` sind ohne
Basic-Auth erreichbar, aber durch Token, Signatur bzw. signierte Links geschützt.

### Login, Benutzer und Mieterportal

- **Erster Start:** Ist `APP_USER`/`APP_PASSWORD` gesetzt, wird daraus der erste **Super-Admin** angelegt; sonst ist die App
  offen und zeigt einen Hinweis mit Link zur Einrichtung (`/setup`). Danach ist für alle Seiten eine Anmeldung nötig.
- **Rollen** (Menü „Benutzer“): **Super-Admin** (alle Gebäude, Einstellungen, Benutzer, WhatsApp, Postfach),
  **Verwalter** (nur die ihm zugewiesenen Gebäude: Abrechnungen, Parteien, Mitteilungen) und **Mieter** (an eine Partei
  gebunden). Bestehende Verwalter werden beim Update automatisch zu Super-Admins, damit nichts verloren geht. Neue Benutzer ohne Passwort
  bekommen eine **Einladung** per E-Mail (Link 7 Tage gültig).
- **Mieterportal:** je Partei freigeben; Mieter sehen unter „Mein Zuhause“ (Startseite) nur die **abgeschlossenen und
  veröffentlichten** Abrechnungen ihrer Partei (Ansicht + PDF). Veröffentlichen automatisch beim Abschließen
  (abschaltbar) oder je Abrechnung per Knopf. Verwalter können über „Ansicht als Mieter“ prüfen, was sichtbar ist.
- **Gebäude / Multi-Site** (Menü „Gebäude“): jedes Gebäude hat eine Gebäude-ID, Anschrift und Abrechnungstitel;
  Parteien und Abrechnungen (Rechnungen) sind einem Gebäude zugeordnet. Der Super-Admin weist jedem Gebäude die
  zuständigen Verwalter zu. Ein **Foto des Standorts** lässt sich hochladen und im Editor zuschneiden (verschieben,
  zoomen); es erscheint als rundes Avatar in der Übersicht „Mein Zuhause“. Bei nur einem Gebäude werden die Angaben
  aus den Einstellungen („Objekt“) automatisch übernommen.
- **Passwort vergessen:** Link per E-Mail über den eingerichteten Mailversand (SMTP), 1 Stunde gültig, nur einmal nutzbar.
- Sicherheit: Passwörter PBKDF2-SHA256 (600 000 Runden), signierte HttpOnly-Cookies (SameSite=Lax, „dauerhaft angemeldet
  bleiben“ – 400 Tage, bei jeder Nutzung automatisch verlängert), Sperre nach 5 Fehlversuchen, Passwortänderung meldet andere Sitzungen ab. Optional `APP_SECRET_KEY` als
  fester Cookie-Schlüssel (sonst automatisch erzeugt und in der Datenbank gespeichert).

### Darstellung

- **Hell / Dunkel:** im Menü „🌓 Automatisch → ☀️ Hell → 🌙 Dunkel“ umschalten; gilt für das jeweilige Gerät (Cookie),
  „Automatisch“ folgt der Systemeinstellung.
- **Farbe der oberen Leiste:** unter „Benutzer → Mieterportal“ je App (Mieter / Verwalter) wählbar – färbt die
  Statusleiste der installierten App und den Streifen oben auf der Seite.

### Push-Benachrichtigungen (PWA)

Unter „Mein Konto“, im Mieterportal und auf der Seite „📲 App“: **„Auf diesem Gerät aktivieren“**. Mieter werden
benachrichtigt, sobald eine Abrechnung ihrer Partei veröffentlicht wird (je Abrechnung nur einmal), Verwalter bei einer
neu eingegangenen Stromrechnung – mit Inhalt (Betrag, Zeitraum, kWh, Hinweise). Unter „Benutzer → Benachrichtigungen
(global)“ legt der Verwalter fest, welche Ereignisse melden: neue Abrechnung (Mieter), Rechnungseingang, Versand,
Fehler (Versand, Postfach, WhatsApp) und optional WhatsApp zugestellt/gelesen. Ein Tipp öffnet die Abrechnung. Web Push nach RFC 8291/8292
(Ende-zu-Ende verschlüsselt, VAPID-Schlüssel erzeugt die App selbst – kein Firebase-Konto nötig, optional fester
Schlüssel über `VAPID_PRIVATE_KEY`). Voraussetzung: HTTPS; auf dem iPhone nur in der installierten App (iOS 16.4+).

### Mitteilungen & Hinweise (Broadcast / Unicast)

Menü **Mitteilungen**: Nachricht an **alle Parteien** oder **ausgewählte Parteien** – per Push, optional zusätzlich per
E-Mail. Farbige Kategorien: ℹ️ Information, 📅 Termin, 🔧 Wartung / geplante Arbeiten, ⛔ Abschaltung, ✅ Erledigt.
**Geplante Ereignisse** (Beginn/Ende) stehen im Mieterportal oben, bis sie vorbei sind (oder bis „Anzeigen bis“);
📌 angeheftete Hinweise bleiben dauerhaft bis zum Archivieren. Optional **Erinnerung am Vortag** per Push.
Mitteilungen ohne Datum erscheinen im Verlauf. **Priorität** je Mitteilung: ⚪ Niedrig (still), 🔵 Normal, 🟠 Hoch (sofortige
Zustellung, bleibt stehen), 🔴 Dringend (zusätzlich Vibration, ganz oben mit rotem Rahmen, Betreff „[Dringend]“).
Vorbei, aber noch nicht archiviert: im Portal grau unter **„✓ Abgeschlossen“** (Termin durchgestrichen, 14 Tage lang),
danach im Verlauf mit Kennzeichnung – so kann ein vergangener Hinweis nicht mit einem aktuellen verwechselt werden.
Systemmeldungen: Fehler = hoch, Versand/Zustellung = niedrig, Rechnungseingang mit Hinweisen = hoch.

### Rücklagen (zeitversetzte Zahlungen, SEPA-Datei)

Für Kosten, die monatlich abgerechnet, aber erst später bezahlt werden (Wassergeld, IPTV, Kaminkehrer):

1. Unter **Fixkosten & Umlagen** bei der Position „Rücklage bilden“ anhaken, optional den Zweck (z. B. `WASSERGELD`).
2. Unter **Rücklagen → Konten** Hauptkonto und Rücklagenkonto (Inhaber, IBAN, optional BIC) speichern. Der Inhaber muss
   so geschrieben sein wie bei der Bank, weil die Bank beim Hochladen Name und IBAN abgleicht.
3. Nach dem Abschließen einer Abrechnung erscheinen die offenen Beträge **je Partei und Position**, z. B. mit dem
   Verwendungszweck `RUECKLAGE AUS NEBENKOSTENABRECHNUNG 09/2026 MIETER FAMILIE ZWEIG GID: 134 WE-1 WASSERGELD 30,34 EUR`.
   „SEPA-Datei erzeugen“ liefert eine Sammelüberweisung (pain.001.001.09, wahlweise .03) als eine logische Datei mit
   Ausführungsdatum; der Dateiname enthält nur erlaubte Zeichen. Hochladen im Online-Banking unter „Datei-Übergabe“.
4. Nach der Ausführung **„✓ Ausgeführt“ bestätigen**. Erst dann zählt der Betrag im **virtuellen Rücklagenkonto**
   (Stand gesamt und je Position). Eine abgelehnte Datei „Verwerfen“: Die Posten sind dann wieder offen.
5. **Rechnung fällig:** Position und Betrag wählen (der aktuelle Stand ist vorbelegt). Erzeugt wird eine
   SEPA-Gutschrift vom Rücklagenkonto aufs Hauptkonto, die nach der Ausführung ebenfalls bestätigt wird.

Weitere Optionen: Rücklagen erst ab einem Abrechnungsmonat, Anteil des Eigentümers ein- oder ausschließen
(Verwendungszweck „EIGENTUEMER …“), Anfangsbestand buchen.

### Webhook-API (n8n, Home Assistant)

Menü **API** (nur Super-Admin): API-Schlüssel anlegen (optional auf Gebäude beschränkt, optional ohne Recht auf
Verwalter-Benachrichtigungen). Der Schlüssel wird nur einmal angezeigt, gespeichert ist nur sein SHA-256-Hash.

```bash
curl -X POST https://<host>/api/v1/notify -H 'X-Api-Key: imv_…' -H 'Content-Type: application/json' \
  -d '{"title": "Wasser wird abgestellt", "body": "Morgen 9–12 Uhr", "priority": "high", "category": "abschaltung"}'
```

- `audience`: `tenants` (Standard), `admins` oder `all`; `parties`: IDs, Wohnungsnummern oder Namen (leer = alle
  Parteien → Broadcast); `building`: Gebäude-ID oder -Code.
- `persist` (Standard `true`) zeigt die Nachricht zusätzlich als Mitteilung in „Mein Zuhause“, `false` = nur Push;
  `mail: true` zusätzlich per E-Mail. Außerdem `priority`, `category`, `pinned`, `event_start`/`event_end`, `remind`.
- `GET /api/v1/ping` prüft den Schlüssel, `GET /api/v1/parties` liefert die erlaubten Gebäude und Parteien.
- Fertige Beispiele zum Kopieren (curl, Home-Assistant-`rest_command` mit Automation, n8n-Flow) stehen auf der Seite.
  Höchstens 60 Aufrufe pro Minute und Schlüssel.

### Abfallkalender (ICS)

Menü **Abfall**: Link zur ICS-Datei des Entsorgers eintragen (z. B. abfalltermine-bamberg.de → „ICS“) oder Datei
hochladen. Enthält der Link `year=2026`, lädt die App automatisch aktuelles **und** nächstes Jahr (täglich aktualisiert).
Die Müllarten werden aus dem Kalender erkannt und farbig dargestellt; ausgewählt wird, für welche erinnert wird.
Benachrichtigung an alle Mieter (optional auch Verwalter): **am Vortag ab** einer Uhrzeit (Standard 18:00) und **am
Abholtag ab** einer Uhrzeit (Standard 06:00, bis 12 Uhr) – je Termin genau einmal. Im Mieterportal stehen die
nächsten Abholungen.

## Entwicklung

```bash
pip install -r requirements-dev.txt
pytest -q
uvicorn app.main:app --reload
```

Struktur: `app/billing.py` (reine Berechnung), `app/ha.py` (HA REST/WebSocket),
`app/service.py` (DB ↔ Berechnung), `app/main.py` (Web), `app/templates/` (UI + Rechnungsvorlage).

Offene Punkte / Entscheidungen: siehe [`docs/KONZEPT.md`](docs/KONZEPT.md).
Victron-Entitäten (hass-victron) und der eigene Victron-Logger (Modbus TCP, ohne HA): siehe [`docs/VICTRON.md`](docs/VICTRON.md).
