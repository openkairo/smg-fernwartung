"""SMG Fernwartung: App auf dem Kundengerät.

Zeigt in der Home-Assistant-Seitenleiste einen Einschaltknopf. Erst der Klick baut eine ausgehende
Verbindung zum Dienst auf. Weitergeleitet wird ausschließlich an den fest eingestellten MCP-Endpunkt.
Nach jedem Start ist die Fernwartung aus.
"""
import asyncio
import base64
import json
import logging
import os
import secrets
import sys
import time

import aiohttp
from aiohttp import WSMsgType, web

OPTIONEN_PFAD = os.environ.get("FERN_OPTIONEN", "/data/options.json")
FRIST_S = float(os.environ.get("FERN_FRIST_S", "10800"))
PING_TIMEOUT_S = float(os.environ.get("FERN_PING_TIMEOUT_S", "45"))
PORT = int(os.environ.get("FERN_APP_PORT", "8099"))
INGRESS_PROXY = "172.30.32.2"
NUR_INGRESS = os.environ.get("FERN_OHNE_INGRESS_PRUEFUNG") != "1"

MCP_PORT = int(os.environ.get("FERN_MCP_PORT", "9584"))
MCP_START_S = float(os.environ.get("FERN_MCP_START_S", "150"))
MAX_RAHMEN = 256 * 1024
TEIL_BYTES = 48 * 1024
METHODEN = ("POST", "GET", "DELETE")
KOPF_HIN = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
KOPF_ZURUECK = ("content-type", "mcp-session-id", "cache-control")

log = logging.getLogger("fernwartung")

ABLEHNUNG = {
    "anmeldung": "Dieses Gerät ist für die Fernwartung nicht angemeldet. Bitte wenden Sie sich an den Support.",
    "gesperrt": "Die Fernwartung ist für dieses Gerät gesperrt. Bitte wenden Sie sich an den Support.",
    "konflikt": "Es besteht noch eine frühere Verbindung. Bitte in einer Minute erneut einschalten.",
}
ENDE = {
    "frist": "Die Zeit ist abgelaufen. Die Fernwartung wurde beendet.",
    "gesperrt": "Die Fernwartung wurde vom Support beendet.",
    "techniker_beendet": "Die Wartung ist abgeschlossen. Die Fernwartung wurde beendet.",
}


class Fernwartung:
    def __init__(self, optionen):
        self.opt = optionen
        self.zustand = "aus"
        self.hinweis = ""
        self.id = None
        self.frist = None  # monotonic
        self.aufgabe = None
        self.anfragen = {}
        self.mcp_url = optionen.get("mcp_url") or ""
        self.mcp_prozess = None

    def status(self):
        rest = max(0, int(self.frist - time.monotonic())) if self.frist and self.zustand in ("bereit", "aktiv") else 0
        return {"zustand": self.zustand, "id": self.id, "restzeit": rest, "hinweis": self.hinweis}

    def setze(self, zustand, hinweis=""):
        self.zustand = zustand
        self.hinweis = hinweis
        if zustand in ("aus", "unterbrochen", "verbindet"):
            self.id = None
        log.info("Zustand: %s", zustand)

    def einschalten(self):
        if self.aufgabe and not self.aufgabe.done():
            return
        self.aufgabe = asyncio.create_task(self.lauf())

    async def ausschalten(self):
        if self.aufgabe and not self.aufgabe.done():
            self.aufgabe.cancel()
            try:
                await self.aufgabe
            except asyncio.CancelledError:
                pass
        self.setze("aus", "Die Fernwartung wurde beendet." if self.zustand != "aus" else self.hinweis)

    async def mcp_starten(self, http):
        """Ohne fremde MCP-Adresse: den eingebauten HA-MCP-Server starten und warten, bis er antwortet."""
        if self.opt.get("mcp_url"):
            self.mcp_url = self.opt["mcp_url"]
            return await self.funktionstest(http)
        pfad = "/private_" + secrets.token_urlsafe(24)
        self.mcp_url = f"http://127.0.0.1:{MCP_PORT}{pfad}"
        self.mcp_prozess = await asyncio.create_subprocess_exec(
            sys.executable, "-u", os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_start.py"),
            env={**os.environ, "FERN_MCP_PORT": str(MCP_PORT), "FERN_MCP_PFAD": pfad})
        ende = time.monotonic() + MCP_START_S
        while time.monotonic() < ende:
            if self.mcp_prozess.returncode is not None:
                return False
            if await self.funktionstest(http):
                return True
            await asyncio.sleep(2)
        return False

    async def mcp_stoppen(self):
        prozess, self.mcp_prozess = self.mcp_prozess, None
        if prozess and prozess.returncode is None:
            prozess.terminate()
            try:
                await asyncio.wait_for(prozess.wait(), 8)
            except asyncio.TimeoutError:
                prozess.kill()

    async def funktionstest(self, http):
        """HA-MCP erreichbar, Anmeldung gültig, MCP-Initialisierung gelingt?"""
        probe = {
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "smg-fernwartung-test", "version": "1"}},
        }
        kopf = self.mcp_kopf({"content-type": "application/json", "accept": "application/json, text/event-stream"})
        try:
            async with http.post(self.mcp_url, json=probe, headers=kopf, allow_redirects=False,
                                 timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
                if r.status != 200 or '"result"' not in text:
                    return False
                sitzung = r.headers.get("mcp-session-id")
            if sitzung:
                kopf["mcp-session-id"] = sitzung
                try:
                    async with http.delete(self.mcp_url, headers=kopf, allow_redirects=False,
                                           timeout=aiohttp.ClientTimeout(total=5)):
                        pass
                except Exception:
                    pass
            return True
        except Exception:
            return False

    def mcp_kopf(self, kopf):
        if self.opt.get("mcp_token"):
            kopf["authorization"] = "Bearer " + self.opt["mcp_token"]
        return kopf

    async def lauf(self):
        self.setze("verbindet")
        self.frist = time.monotonic() + FRIST_S
        ws = None
        sperre = asyncio.Lock()
        self.ende_grund = "ausgeschaltet"

        async def senden(nachricht):
            async with sperre:
                await ws.send_str(json.dumps(nachricht))

        try:
            async with aiohttp.ClientSession() as http:
                if not await self.mcp_starten(http):
                    self.setze("aus", "Die Fernwartung kann gerade nicht gestartet werden "
                                      "(interner Dienst antwortet nicht). Bitte wenden Sie sich an den Support.")
                    return
                try:
                    ws = await http.ws_connect(self.opt["dienst_url"], max_msg_size=MAX_RAHMEN,
                                               timeout=aiohttp.ClientWSTimeout(ws_close=5))
                    await senden({"typ": "anmeldung", "kennung": self.opt["geraetekennung"],
                                  "schluessel": self.opt["geraeteschluessel"]})
                except Exception:
                    self.setze("aus", "Fernwartung derzeit nicht erreichbar. Bitte Internetverbindung prüfen "
                                      "und später erneut versuchen.")
                    return
                try:
                    while True:
                        rest = self.frist - time.monotonic()
                        if rest <= 0:
                            self.ende_grund = "frist"
                            self.setze("aus", ENDE["frist"])
                            return
                        try:
                            msg = await asyncio.wait_for(ws.receive(), min(PING_TIMEOUT_S, rest))
                        except asyncio.TimeoutError:
                            if self.frist - time.monotonic() <= 0:
                                self.ende_grund = "frist"
                                self.setze("aus", ENDE["frist"])
                            else:
                                self.setze("unterbrochen", "Verbindung unterbrochen. Bitte Fernwartung erneut einschalten.")
                            return
                        if msg.type != WSMsgType.TEXT:
                            if self.zustand == "verbindet":
                                self.setze("aus", "Fernwartung derzeit nicht erreichbar. Bitte später erneut versuchen.")
                            else:
                                self.setze("unterbrochen", "Verbindung unterbrochen. Bitte Fernwartung erneut einschalten.")
                            return
                        m = json.loads(msg.data)
                        typ = m.get("typ")
                        if typ == "ping":
                            await senden({"typ": "pong"})
                        elif typ == "angemeldet":
                            await senden({"typ": "bereit"})
                        elif typ == "abgelehnt":
                            self.setze("aus", ABLEHNUNG.get(m.get("grund"), "Die Anmeldung wurde abgelehnt."))
                            return
                        elif typ == "sitzung":
                            self.id = m["id"]
                            self.frist = min(self.frist, time.monotonic() + float(m["restzeit"]))
                            self.setze("bereit")
                        elif typ == "uebernommen":
                            self.setze("aktiv")
                        elif typ == "ende":
                            self.setze("aus", ENDE.get(m.get("grund"), "Die Fernwartung wurde beendet."))
                            return
                        elif typ == "anfrage":
                            nr = m["nr"]
                            self.anfragen[nr] = asyncio.create_task(self.anfrage(http, senden, m))
                            self.anfragen[nr].add_done_callback(lambda _, nr=nr: self.anfragen.pop(nr, None))
                        elif typ == "abbruch":
                            aufgabe = self.anfragen.get(m.get("nr"))
                            if aufgabe:
                                aufgabe.cancel()
                finally:
                    for aufgabe in list(self.anfragen.values()):
                        aufgabe.cancel()
                    try:
                        await asyncio.wait_for(asyncio.shield(self.schliessen(ws, senden)), 3)
                    except BaseException:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Fehler in der Fernwartung")
            self.setze("unterbrochen", "Verbindung unterbrochen. Bitte Fernwartung erneut einschalten.")
        finally:
            # Ohne laufende Fernwartung läuft auch kein MCP-Server
            await asyncio.shield(self.mcp_stoppen())

    async def schliessen(self, ws, senden):
        if not ws.closed:
            try:
                await senden({"typ": "aus", "grund": self.ende_grund})
            except Exception:
                pass
            await ws.close()

    async def anfrage(self, http, senden, m):
        """Eine Anfrage an den festen MCP-Endpunkt reichen und die Antwort stückweise zurückschicken."""
        nr = m["nr"]
        try:
            if m.get("methode") not in METHODEN:
                await senden({"typ": "fehler", "nr": nr, "text": "Methode nicht erlaubt"})
                return
            kopf = self.mcp_kopf({k: v for k, v in m.get("kopf", {}).items() if k.lower() in KOPF_HIN})
            koerper = base64.b64decode(m.get("koerper", ""))
            async with http.request(m["methode"], self.mcp_url, headers=kopf, data=koerper or None,
                                    allow_redirects=False,
                                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=10)) as r:
                await senden({"typ": "antwort", "nr": nr, "status": r.status,
                              "kopf": {k: r.headers[k] for k in KOPF_ZURUECK if k in r.headers}})
                async for stueck in r.content.iter_any():
                    for i in range(0, len(stueck), TEIL_BYTES):
                        await senden({"typ": "teil", "nr": nr,
                                      "daten": base64.b64encode(stueck[i:i + TEIL_BYTES]).decode()})
                await senden({"typ": "ende", "nr": nr})
        except asyncio.CancelledError:
            raise
        except Exception as e:
            try:
                await senden({"typ": "fehler", "nr": nr, "text": type(e).__name__})
            except Exception:
                pass


# ---------- Oberfläche ----------

@web.middleware
async def nur_ingress(request, handler):
    if NUR_INGRESS and request.remote != INGRESS_PROXY:
        return web.Response(status=403, text="Nur über Home Assistant erreichbar.")
    return await handler(request)


async def seite(request):
    return web.FileResponse(os.path.join(os.path.dirname(__file__), "seite.html"),
                            headers={"Cache-Control": "no-store"})


async def api_status(request):
    return web.json_response(request.app["fern"].status())


async def api_ein(request):
    request.app["fern"].einschalten()
    return web.json_response(request.app["fern"].status())


async def api_aus(request):
    await request.app["fern"].ausschalten()
    return web.json_response(request.app["fern"].status())


def bauen(optionen):
    app = web.Application(middlewares=[nur_ingress])
    app["fern"] = Fernwartung(optionen)
    app.add_routes([web.get("/", seite), web.get("/api/status", api_status),
                    web.post("/api/ein", api_ein), web.post("/api/aus", api_aus)])
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    with open(OPTIONEN_PFAD) as f:
        optionen = json.load(f)
    web.run_app(bauen(optionen), host="0.0.0.0", port=PORT, access_log=None, print=None)
