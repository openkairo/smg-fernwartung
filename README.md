# SMG Fernwartung

Zeitlich begrenzte Fernwartung für Home-Assistant-Sets: Der Kunde drückt „Fernwartung einschalten“, nennt die angezeigte ID, und Claude arbeitet über MCP auf seinem System. Nach 3 Stunden oder per Knopf ist der Zugang zu.

```
Kunden-HA: App „SMG Fernwartung“  --wss, ausgehend-->  Dienst (VPS)  <--SSH--  Mac: fern + Claude Code
        |
        +--> HA-MCP-App (festes Ziel im selben Gerät)
```

| Ordner | Inhalt |
| --- | --- |
| `server/` | Dienst `dienst.py` (aiohttp, SQLite, ein Prozess), Dockerfile, Compose mit Traefik |
| `smg_fernwartung/` | Home-Assistant-App mit Oberfläche in der Seitenleiste |
| `mac/` | Skript `fern` und die Arbeitsregeln für Kundensitzungen |
| `tests/` | Ende-zu-Ende-Tests |

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install aiohttp 'mcp<2'
.venv/bin/python tests/test_ablauf.py     # Dienst, zwei Geräte, MCP-Attrappen
.venv/bin/python tests/test_fern.py       # fern-Skript gegen lokalen Dienst
.venv/bin/python tests/test_echt_mcp.py   # echter MCP-Server und echter Claude-Code-Client
```

## Dienst auf dem Server

```bash
cd server && docker compose up -d --build
docker compose exec fern python dienst.py techniker-neu robert   # gibt den Techniker-Schlüssel einmal aus
```

Öffentlich ist nur `wss://fern.wattrebellen.de/geraet`. Die Techniker-Seite liegt am Host auf `127.0.0.1:8811` und verlangt bei jeder Anfrage den Techniker-Schlüssel.

## Mac

```bash
fern einrichten     # legt ~/.config/smg-fern/ an (700/600)
fern neu SMG-0001   # Werkstatt: Gerät anlegen
fern 5832 7194      # Kundensitzung
```

Kundensitzungen laufen in einem eigenen Claude-Profil ohne eingebaute Werkzeuge und ohne die sonstigen Anschlüsse, Regeln und Erinnerungen.

## Werkstatt: ein Set einrichten

1. HA OS installieren, Grundeinrichtung.
2. App-Quelle hinzufügen: `https://github.com/openkairo/smg-fernwartung`.
3. „SMG Fernwartung“ installieren, „In Seitenleiste anzeigen“ einschalten.
4. `fern neu <Seriennummer>` und die beiden Werte (Gerätekennung, Geräteschlüssel) in die Einstellungen der App eintragen.
5. App starten, Probelauf: einschalten, `fern <ID>`, ausschalten.

Der MCP-Server (Paket `ha-mcp`, MIT-Lizenz) ist in die App eingebaut und läuft nur, solange die Fernwartung eingeschaltet ist. Eine getrennte HA-MCP-App wird nicht gebraucht.
