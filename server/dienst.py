"""smg-fern: Weiterleitungsdienst für die Fernwartung von Kunden-Home-Assistant.

Zwei Seiten in einem Prozess:
- Geräte-Seite (öffentlich hinter Traefik): Kundengeräte melden sich per WebSocket an.
- Techniker-Seite (nur 127.0.0.1 am Host): ID gegen Sitzung tauschen, MCP weiterleiten, Verwaltung.

Aktive Verbindungen liegen im Arbeitsspeicher. Nach einem Neustart sind alle Sitzungen ungültig.
"""
import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time

from aiohttp import WSMsgType, web

FRIST_S = float(os.environ.get("FERN_FRIST_S", "10800"))
PING_S = float(os.environ.get("FERN_PING_S", "15"))
PING_TIMEOUT_S = float(os.environ.get("FERN_PING_TIMEOUT_S", "45"))
TOKEN_S = float(os.environ.get("FERN_TOKEN_S", "60"))
DB_PFAD = os.environ.get("FERN_DB", "data/fern.sqlite3")
GERAETE_PORT = int(os.environ.get("FERN_GERAETE_PORT", "8000"))
TECHNIKER_PORT = int(os.environ.get("FERN_TECHNIKER_PORT", "8001"))
TECHNIKER_HOST = os.environ.get("FERN_TECHNIKER_HOST", "0.0.0.0")

MAX_KOERPER = 4 * 1024 * 1024
MAX_RAHMEN = 256 * 1024
MAX_PARALLEL = 8
MAX_PUFFER = 256
ERSTE_ANTWORT_S = 600
FEHLVERSUCHE_MAX = 10
FEHLVERSUCHE_FENSTER_S = 600
METHODEN = ("POST", "GET", "DELETE")
KOPF_HIN = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
KOPF_ZURUECK = ("content-type", "mcp-session-id", "cache-control")

log = logging.getLogger("smg-fern")


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def zeit():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Sitzung:
    def __init__(self, geraet, ws):
        self.geraet = geraet
        self.ws = ws
        self.id = None
        self.kennung = secrets.token_urlsafe(32)
        self.ref = secrets.token_hex(8)  # fürs Protokoll, nicht zur Anmeldung nutzbar
        self.techniker = None
        self.token = None
        self.frist = None
        self.offen = True
        self.zuletzt = time.monotonic()
        self.anfragen = {}
        self.naechste_nr = 0
        self.sendesperre = asyncio.Lock()
        self.waechter = None

    async def senden(self, nachricht):
        async with self.sendesperre:
            await self.ws.send_str(json.dumps(nachricht))

    def restzeit(self):
        return max(0, int(self.frist - time.time())) if self.frist else 0


class Zustand:
    def __init__(self, db_pfad):
        ordner = os.path.dirname(db_pfad)
        if ordner:
            os.makedirs(ordner, exist_ok=True)
        self.db = sqlite3.connect(db_pfad)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS geraete(
                kennung TEXT PRIMARY KEY, seriennummer TEXT UNIQUE, schluessel_hash TEXT,
                gesperrt INTEGER DEFAULT 0, angelegt TEXT);
            CREATE TABLE IF NOT EXISTS techniker(name TEXT PRIMARY KEY, schluessel_hash TEXT UNIQUE, angelegt TEXT);
            CREATE TABLE IF NOT EXISTS protokoll(
                ref TEXT PRIMARY KEY, geraet TEXT, techniker TEXT, beginn TEXT, ende TEXT, grund TEXT);
            """
        )
        # Sitzungen überleben keinen Neustart
        self.db.execute("UPDATE protokoll SET ende=?, grund='dienst_neustart' WHERE ende IS NULL", (zeit(),))
        self.db.commit()
        self.geheim = secrets.token_bytes(32)  # je Prozess neu: alte Übernahme-Token verfallen beim Neustart
        self.nach_geraet = {}
        self.nach_id = {}
        self.nach_ref = {}
        self.sitzungen = {}
        self.fehlversuche = {}

    def geraet(self, wert):
        return self.db.execute("SELECT * FROM geraete WHERE kennung=? OR seriennummer=?", (wert, wert)).fetchone()

    def techniker_name(self, schluessel):
        zeile = self.db.execute("SELECT name FROM techniker WHERE schluessel_hash=?", (sha(schluessel),)).fetchone()
        return zeile["name"] if zeile else None

    def vermerk(self, geraet, grund):
        self.db.execute(
            "INSERT INTO protokoll(ref, geraet, beginn, ende, grund) VALUES(?,?,?,?,?)",
            (secrets.token_hex(8), geraet, zeit(), zeit(), grund),
        )
        self.db.commit()

    def freigeben(self, s):
        """Sitzung bekommt ID und Frist, sobald das Gerät seinen Funktionstest bestanden hat."""
        while True:
            kandidat = f"{secrets.randbelow(10**8):08d}"
            if kandidat not in self.nach_id:
                break
        s.id = kandidat
        s.frist = time.time() + FRIST_S
        self.nach_id[s.id] = s
        self.nach_ref[s.ref] = s
        self.sitzungen[s.kennung] = s
        self.db.execute("INSERT INTO protokoll(ref, geraet, beginn) VALUES(?,?,?)", (s.ref, s.geraet, zeit()))
        self.db.commit()

    async def beenden(self, s, grund):
        if not s.offen:
            return
        s.offen = False
        if self.nach_geraet.get(s.geraet) is s:
            del self.nach_geraet[s.geraet]
        self.nach_id.pop(s.id, None)
        self.nach_ref.pop(s.ref, None)
        self.sitzungen.pop(s.kennung, None)
        for warteschlange in list(s.anfragen.values()):
            while not warteschlange.empty():
                warteschlange.get_nowait()
            warteschlange.put_nowait({"typ": "abbruch"})
        if s.frist:
            self.db.execute("UPDATE protokoll SET ende=?, grund=? WHERE ref=?", (zeit(), grund, s.ref))
            self.db.commit()
        log.info("Sitzung %s beendet: %s", s.ref, grund)
        if s.waechter and s.waechter is not asyncio.current_task():
            s.waechter.cancel()
        try:
            await asyncio.wait_for(s.senden({"typ": "ende", "grund": grund}), 2)
        except Exception:
            pass
        try:
            await asyncio.wait_for(s.ws.close(), 2)
        except Exception:
            pass

    async def wachen(self, s):
        """Lebenszeichen senden, stille Verbindungen und abgelaufene Fristen beenden."""
        while s.offen:
            warten = PING_S
            if s.frist:
                warten = min(warten, max(0.05, s.frist - time.time()))
            await asyncio.sleep(warten)
            if not s.offen:
                return
            if s.frist and time.time() >= s.frist:
                await self.beenden(s, "frist")
                return
            if time.monotonic() - s.zuletzt > PING_TIMEOUT_S:
                await self.beenden(s, "verbindung_verloren")
                return
            try:
                await asyncio.wait_for(s.senden({"typ": "ping"}), PING_S)
            except Exception:
                await self.beenden(s, "verbindung_verloren")
                return


# ---------- Geräte-Seite ----------

async def geraet_ws(request):
    z = request.app["zustand"]
    ws = web.WebSocketResponse(max_msg_size=MAX_RAHMEN)
    await ws.prepare(request)

    async def ablehnen(grund, code):
        await ws.send_str(json.dumps({"typ": "abgelehnt", "grund": grund}))
        await ws.close(code=code)
        return ws

    try:
        erste = await ws.receive_json(timeout=10)
        kennung = str(erste["kennung"])
        schluessel = str(erste["schluessel"])
        assert erste["typ"] == "anmeldung"
    except Exception:
        await ws.close(code=4400)
        return ws
    g = z.geraet(kennung)
    if not g or g["kennung"] != kennung or not hmac.compare_digest(g["schluessel_hash"], sha(schluessel)):
        return await ablehnen("anmeldung", 4401)
    if g["gesperrt"]:
        return await ablehnen("gesperrt", 4403)
    if kennung in z.nach_geraet:
        # Zweites Gerät mit demselben Schlüssel: bestehende Verbindung bleibt, Konflikt wird vermerkt
        log.warning("Konflikt: zweite Anmeldung für Gerät %s abgelehnt", kennung)
        z.vermerk(kennung, "konflikt_abgelehnt")
        return await ablehnen("konflikt", 4409)

    s = Sitzung(kennung, ws)
    z.nach_geraet[kennung] = s
    s.waechter = asyncio.create_task(z.wachen(s))
    grund = "verbindung_verloren"
    try:
        await s.senden({"typ": "angemeldet"})
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            s.zuletzt = time.monotonic()
            try:
                m = json.loads(msg.data)
                typ = m["typ"]
            except Exception:
                break
            if typ == "pong":
                continue
            if typ == "bereit" and s.id is None:
                z.freigeben(s)
                s.waechter.cancel()  # neu starten, damit die Frist ab jetzt überwacht wird
                s.waechter = asyncio.create_task(z.wachen(s))
                await s.senden({"typ": "sitzung", "id": s.id, "restzeit": s.restzeit()})
            elif typ == "aus":
                grund = "frist" if m.get("grund") == "frist" else "ausgeschaltet"
                break
            elif typ in ("antwort", "teil", "ende", "fehler"):
                warteschlange = s.anfragen.get(m.get("nr"))
                if warteschlange is None:
                    continue
                try:
                    warteschlange.put_nowait(m)
                except asyncio.QueueFull:
                    # Empfänger kommt nicht hinterher: diese eine Anfrage abbrechen
                    s.anfragen.pop(m.get("nr"), None)
                    while not warteschlange.empty():
                        warteschlange.get_nowait()
                    warteschlange.put_nowait({"typ": "abbruch"})
                    await s.senden({"typ": "abbruch", "nr": m.get("nr")})
    finally:
        await z.beenden(s, grund)
    return ws


async def gesund(request):
    return web.Response(text="ok")


# ---------- Techniker-Seite ----------

def fehler(status, text):
    return web.json_response({"fehler": text}, status=status)


@web.middleware
async def techniker_pruefen(request, handler):
    z = request.app["zustand"]
    kopf = request.headers.get("Authorization", "")
    name = z.techniker_name(kopf[7:]) if kopf.startswith("Bearer ") else None
    if not name:
        return fehler(401, "Techniker-Zugang fehlt oder ist ungültig")
    request["techniker"] = name
    return await handler(request)


def token_bauen(z, s, techniker):
    inhalt = json.dumps({"ref": s.ref, "techniker": techniker, "ablauf": time.time() + TOKEN_S}).encode()
    kern = base64.urlsafe_b64encode(inhalt).decode()
    return kern + "." + hmac.new(z.geheim, kern.encode(), hashlib.sha256).hexdigest()


def token_lesen(z, token):
    try:
        kern, signatur = token.split(".")
        if not hmac.compare_digest(signatur, hmac.new(z.geheim, kern.encode(), hashlib.sha256).hexdigest()):
            return None
        return json.loads(base64.urlsafe_b64decode(kern))
    except Exception:
        return None


async def uebernahme_beginn(request):
    """Schritt 1: ID nennen, kurz gültigen Token für genau diese Sitzung und diesen Techniker bekommen."""
    z = request.app["zustand"]
    techniker = request["techniker"]
    jetzt = time.time()
    versuche = [t for t in z.fehlversuche.get(techniker, []) if jetzt - t < FEHLVERSUCHE_FENSTER_S]
    z.fehlversuche[techniker] = versuche
    if len(versuche) >= FEHLVERSUCHE_MAX:
        return fehler(429, "Zu viele Fehlversuche, bitte später erneut")
    try:
        fern_id = "".join(c for c in str((await request.json())["id"]) if c.isdigit())
    except Exception:
        return fehler(400, "ID fehlt")
    s = z.nach_id.get(fern_id)
    if not s or not s.offen:
        versuche.append(jetzt)
        return fehler(404, "ID unbekannt oder abgelaufen")
    if s.techniker:
        return fehler(409, "Sitzung bereits belegt")
    return web.json_response({"token": token_bauen(z, s, techniker)})


async def uebernahme_abschluss(request):
    """Schritt 2: Token einlösen. Wiederholung mit demselben Token liefert dieselbe Sitzung."""
    z = request.app["zustand"]
    techniker = request["techniker"]
    try:
        token = str((await request.json())["token"])
    except Exception:
        return fehler(400, "Token fehlt")
    inhalt = token_lesen(z, token)
    if not inhalt or inhalt["techniker"] != techniker or time.time() > inhalt["ablauf"]:
        return fehler(410, "Übernahme abgelaufen oder ungültig")
    s = z.nach_ref.get(inhalt["ref"])
    if not s or not s.offen:
        return fehler(410, "Sitzung beendet")
    if s.techniker is None:
        s.techniker = techniker
        s.token = token
        z.db.execute("UPDATE protokoll SET techniker=? WHERE ref=?", (techniker, s.ref))
        z.db.commit()
        try:
            await s.senden({"typ": "uebernommen"})
        except Exception:
            pass
    elif s.techniker != techniker or not hmac.compare_digest(s.token, token):
        return fehler(409, "Sitzung bereits belegt")
    return web.json_response({"sitzung": s.kennung, "restzeit": s.restzeit()})


async def weiterleiten(request):
    z = request.app["zustand"]
    s = z.sitzungen.get(request.match_info["kennung"])
    # Bei jeder Anfrage: Zuordnung, Zustand, Frist
    if not s or not s.offen or s.techniker != request["techniker"] or time.time() >= s.frist:
        return fehler(410, "Sitzung beendet oder nicht zugeordnet")
    if request.method not in METHODEN:
        return fehler(405, "Methode nicht erlaubt")
    if len(s.anfragen) >= MAX_PARALLEL:
        return fehler(429, "Zu viele gleichzeitige Anfragen")
    koerper = await request.read()
    nr = s.naechste_nr
    s.naechste_nr += 1
    warteschlange = asyncio.Queue(maxsize=MAX_PUFFER)
    s.anfragen[nr] = warteschlange
    kopf = {k: request.headers[k] for k in KOPF_HIN if k in request.headers}
    fertig = False
    try:
        await s.senden({
            "typ": "anfrage", "nr": nr, "methode": request.method, "kopf": kopf,
            "koerper": base64.b64encode(koerper).decode(),
        })
        try:
            erste = await asyncio.wait_for(warteschlange.get(), ERSTE_ANTWORT_S)
        except asyncio.TimeoutError:
            return fehler(504, "Keine Antwort vom Kundengerät")
        if erste["typ"] != "antwort":
            fertig = True
            return fehler(502, "Weiterleitung abgebrochen: " + str(erste.get("text", erste["typ"])))
        antwort = web.StreamResponse(
            status=int(erste["status"]),
            headers={k: v for k, v in erste.get("kopf", {}).items() if k.lower() in KOPF_ZURUECK},
        )
        await antwort.prepare(request)
        while True:
            m = await warteschlange.get()
            if m["typ"] == "teil":
                await antwort.write(base64.b64decode(m["daten"]))
            elif m["typ"] == "ende":
                fertig = True
                await antwort.write_eof()
                return antwort
            else:
                # mitten in der Antwort abgebrochen: Verbindung hart schließen, damit nichts als vollständig gilt
                fertig = True
                if request.transport:
                    request.transport.close()
                return antwort
    finally:
        s.anfragen.pop(nr, None)
        if not fertig and s.offen:
            try:
                await asyncio.shield(s.senden({"typ": "abbruch", "nr": nr}))
            except Exception:
                pass


async def sitzung_beenden(request):
    """Der Techniker ist fertig: Sitzung sofort schließen, statt sie bis zur Frist offen zu lassen."""
    z = request.app["zustand"]
    s = z.sitzungen.get(request.match_info["kennung"])
    if not s or not s.offen or s.techniker != request["techniker"]:
        return fehler(410, "Sitzung beendet oder nicht zugeordnet")
    await z.beenden(s, "techniker_beendet")
    return web.json_response({"beendet": True})


async def geraet_neu(request):
    z = request.app["zustand"]
    try:
        seriennummer = str((await request.json())["seriennummer"]).strip()
        assert seriennummer
    except Exception:
        return fehler(400, "Seriennummer fehlt")
    if z.geraet(seriennummer):
        return fehler(409, "Seriennummer schon vergeben (erst sperren, dann neue Nummer oder Zusatz verwenden)")
    kennung = secrets.token_hex(8)
    schluessel = secrets.token_urlsafe(32)
    z.db.execute(
        "INSERT INTO geraete(kennung, seriennummer, schluessel_hash, angelegt) VALUES(?,?,?,?)",
        (kennung, seriennummer, sha(schluessel), zeit()),
    )
    z.db.commit()
    return web.json_response({"kennung": kennung, "seriennummer": seriennummer, "schluessel": schluessel})


async def geraet_sperren(request):
    z = request.app["zustand"]
    g = z.geraet(request.match_info["wert"])
    if not g:
        return fehler(404, "Gerät unbekannt")
    z.db.execute("UPDATE geraete SET gesperrt=1 WHERE kennung=?", (g["kennung"],))
    z.db.commit()
    s = z.nach_geraet.get(g["kennung"])
    if s:
        await z.beenden(s, "gesperrt")
    return web.json_response({"kennung": g["kennung"], "gesperrt": True, "sitzung_beendet": bool(s)})


async def geraete_liste(request):
    z = request.app["zustand"]
    zeilen = z.db.execute("SELECT kennung, seriennummer, gesperrt, angelegt FROM geraete ORDER BY angelegt").fetchall()
    return web.json_response([dict(r, verbunden=r["kennung"] in z.nach_geraet) for r in zeilen])


async def protokoll(request):
    z = request.app["zustand"]
    zeilen = z.db.execute("SELECT * FROM protokoll ORDER BY beginn DESC LIMIT 100").fetchall()
    return web.json_response([dict(r) for r in zeilen])


# ---------- Start ----------

async def starten(db_pfad=DB_PFAD, geraete_port=GERAETE_PORT, techniker_port=TECHNIKER_PORT,
                  techniker_host=TECHNIKER_HOST):
    z = Zustand(db_pfad)
    oeffentlich = web.Application()
    oeffentlich["zustand"] = z
    oeffentlich.add_routes([web.get("/geraet", geraet_ws), web.get("/gesund", gesund)])

    techniker = web.Application(middlewares=[techniker_pruefen], client_max_size=MAX_KOERPER)
    techniker["zustand"] = z
    techniker.add_routes([
        web.post("/uebernahme/beginn", uebernahme_beginn),
        web.post("/uebernahme/abschluss", uebernahme_abschluss),
        web.route("*", "/s/{kennung}/mcp", weiterleiten),
        web.post("/s/{kennung}/ende", sitzung_beenden),
        web.post("/geraete", geraet_neu),
        web.get("/geraete", geraete_liste),
        web.post("/geraete/{wert}/sperren", geraet_sperren),
        web.get("/protokoll", protokoll),
    ])
    # Kein Zugriffsprotokoll: die Pfade enthalten Sitzungskennungen
    laeufer = [
        web.AppRunner(oeffentlich, access_log=None),
        web.AppRunner(techniker, access_log=None, handler_cancellation=True),
    ]
    for l in laeufer:
        await l.setup()
    await web.TCPSite(laeufer[0], "0.0.0.0", geraete_port).start()
    await web.TCPSite(laeufer[1], techniker_host, techniker_port).start()
    log.info("smg-fern läuft: Geräte :%s, Techniker %s:%s", geraete_port, techniker_host, techniker_port)
    return z, laeufer


def techniker_neu(name):
    z = Zustand(DB_PFAD)
    schluessel = secrets.token_urlsafe(32)
    z.db.execute("INSERT OR REPLACE INTO techniker(name, schluessel_hash, angelegt) VALUES(?,?,?)",
                 (name, sha(schluessel), zeit()))
    z.db.commit()
    print(schluessel)


def techniker_entfernen(name):
    z = Zustand(DB_PFAD)
    z.db.execute("DELETE FROM techniker WHERE name=?", (name,))
    z.db.commit()


async def _lauf():
    await starten()
    await asyncio.Event().wait()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    p = argparse.ArgumentParser()
    p.add_argument("befehl", nargs="?", default="start", choices=["start", "techniker-neu", "techniker-entfernen"])
    p.add_argument("name", nargs="?")
    a = p.parse_args()
    if a.befehl == "techniker-neu":
        techniker_neu(a.name)
    elif a.befehl == "techniker-entfernen":
        techniker_entfernen(a.name)
    else:
        asyncio.run(_lauf())
