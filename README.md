# azure-token-watchdog

Prüft, ob der lokale `az`-CLI-Token-Cache noch mit der Realität übereinstimmt —
bevor ein Skript mitten im Lauf über ein abgelaufenes Token stolpert.

Das Problem: `~/.azure` cached Subscriptions und Accounts. Läuft ein Refresh-Token
ab oder wird eine Subscription entzogen, bleibt der Cache trotzdem stehen. `az account list`
liefert weiter Einträge, die live nicht mehr auflösen. Wer mehrere Identitäten
parallel nutzt (Kunden-Tenant, Test-Tenant, privat), merkt das erst, wenn ein
Kommando gegen die falsche oder eine tote Subscription läuft.

Der Watchdog vergleicht **Cache gegen Live-Antwort** und meldet die Differenz.

## Was geprüft wird

- **Default-Subscription** — gesetzt? Und löst sie live überhaupt noch auf?
- **Cache-Drift** — Anzahl gecachter vs. live auflösender Subscriptions
- **Pro Identität** — lösen alle, manche oder keine ihrer Subscriptions noch auf
- **Isolierte Projekt-Kontexte** — jedes `~/.azure-contexts/<projekt>` (das
  `AZURE_CONFIG_DIR`-Muster für getrennte Tenants) wird einzeln geprüft und dem
  Projekt zugeordnet, das es benutzt
- **Parallele `az`/`azd`-Prozesse** — laufen gerade welche, besteht Race-Gefahr
  auf dem gemeinsamen Token-Cache

Daraus wird eine Ampel: `green` / `orange` (Drift, keine Default gesetzt, teilweise
tot) / `red` (Identität oder Default-Subscription löst nicht mehr auf).

## Umgang mit Token-Material

Der Watchdog liest `msal_token_cache.json`, greift daraus aber ausschließlich das
Feld `username` ab (`cached_usernames()`). Tokens, Refresh-Tokens und Secrets
werden weder gelesen noch geloggt noch geschrieben.

Die Live-Prüfung nutzt nur lesende Kommandos (`az account list`, `az account show`)
mit 5s Timeout. Nichts wird verändert — kein `az login`, kein `az account set`.

**Die erzeugten Artefakte sind trotzdem sensibel:** `state.json` und
`reports/latest.md` enthalten echte Benutzernamen, Tenant-Domains, ggf.
Subscription-Namen samt `tenantId` und lokale Pfade. Beide sind per `.gitignore`
ausgeschlossen — das bitte so lassen.

## Benutzung

```sh
python3 token_watchdog.py
# severity=orange drift=0 contexts=1
```

Schreibt zwei Dateien neben das Skript:

- `state.json` — maschinenlesbar, für Weiterverarbeitung
- `reports/latest.md` — lesbarer Report

Keine Abhängigkeiten außer Python 3.10+ (`X | None`-Syntax) und der Azure CLI.

## Menüleiste

Die SwiftBar-Anzeige liegt bewusst in einem eigenen Repo:
[dernerl/swiftbar-plugins](https://github.com/dernerl/swiftbar-plugins). Sie liest
die `state.json` und ist rein optional — dieses Tool läuft eigenständig.

## Lizenz

MIT
