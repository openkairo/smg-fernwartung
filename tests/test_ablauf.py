"""Ende-zu-Ende-Test: Dienst, zwei Geräte-Apps und zwei MCP-Attrappen in einem Prozess.

Aufruf:  .venv/bin/python tests/test_ablauf.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time

import aiohttp
from aiohttp import web

WURZEL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(WURZEL, "server"))
sys.path.insert(0, os.path.join(WURZEL, "smg_fernwartung"))
os.environ["FERN_OHNE_INGRESS_PRUEFUNG"] = "1"
import dienst  # noqa: E402
import fernwartung  # noqa: E402

P_GERAETE, P_TECH, P_MCP_A, P_MCP_B, P_APP_A, P_APP_B, P_STUMM = 18000, 18001, 18010, 18011, 18020, 18021, 18030
TECH = f"http://127.0.0.1:{P_TECH}"
ergebnisse = []


def pruefe(name, ok, zusatz=""):
    ergebnisse.append(ok)
    print(("  ok      " if ok else "  FEHLER  ") + name + (f"  [{zusatz}]" if zusatz and not ok else ""))


# ---------- MCP-Attrappe ----------

def mcp_attrappe(name, merker):
    async def post(request):
        merker["kopf"] = {k.lower(): v for k, v in request.headers.items()}
        m = await request.json()
        methode = m.get("method")
        if methode == "initialize":
            return web.json_response({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": "2025-06-18"}},
                                     headers={"Mcp-Session-Id": "mcp-" + name})
        if methode == "echo":
            await asyncio.sleep(m["params"].get("warten", 0))
            return web.json_response({"jsonrpc": "2.0", "id": m["id"], "result": {"von": name, "wert": m["params"]["wert"]}},
                                     headers={"Mcp-Session-Id": request.headers.get("Mcp-Session-Id", "")})
        if methode == "strom":
            antwort = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await antwort.prepare(request)
            for i in range(3):
                await antwort.write(f"data: {json.dumps({'teil': i})}\n\n".encode())
                await asyncio.sleep(0.3)
            return antwort
        if methode == "langsam":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                merker["abgebrochen"] = True
                raise
        return web.json_response({"error": "unbekannt"}, status=400)

    app = web.Application()
    app.add_routes([web.post("/geheim/mcp", post)])
    return app


async def starte(app, port, **kw):
    laeufer = web.AppRunner(app, access_log=None, **kw)
    await laeufer.setup()
    await web.TCPSite(laeufer, "127.0.0.1", port).start()
    return laeufer


# ---------- Helfer ----------

class Techniker:
    def __init__(self, http, schluessel):
        self.http = http
        self.kopf = {"Authorization": "Bearer " + schluessel} if schluessel else {}

    async def ruf(self, methode, pfad, **kw):
        async with self.http.request(methode, TECH + pfad, headers={**self.kopf, **kw.pop("headers", {})}, **kw) as r:
            try:
                return r.status, await r.json(content_type=None), r.headers
            except Exception:
                return r.status, None, r.headers

    async def uebernehmen(self, fern_id):
        status, j, _ = await self.ruf("POST", "/uebernahme/beginn", json={"id": fern_id})
        if status != 200:
            return status, None, None
        status, j2, _ = await self.ruf("POST", "/uebernahme/abschluss", json={"token": j["token"]})
        return status, (j2 or {}).get("sitzung"), j["token"]

    async def mcp(self, sitzung, methode, params=None, **kw):
        return await self.ruf("POST", f"/s/{sitzung}/mcp",
                              json={"jsonrpc": "2.0", "id": 1, "method": methode, "params": params or {}}, **kw)


class App:
    def __init__(self, http, port):
        self.http = http
        self.basis = f"http://127.0.0.1:{port}"

    async def status(self):
        async with self.http.get(self.basis + "/api/status") as r:
            return await r.json()

    async def druecke(self, was):
        async with self.http.post(self.basis + "/api/" + was) as r:
            return await r.json()

    async def warte_auf(self, zustaende, dauer=5):
        ende = time.monotonic() + dauer
        while time.monotonic() < ende:
            s = await self.status()
            if s["zustand"] in zustaende:
                return s
            await asyncio.sleep(0.05)
        return await self.status()

    async def ein(self):
        await self.druecke("ein")
        return await self.warte_auf(("bereit", "aus", "unterbrochen"))


async def haupt():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "fern.sqlite3")
    dienst.DB_PFAD = db
    z, laeufer = await dienst.starten(db, P_GERAETE, P_TECH, "127.0.0.1")
    for name in ("robert", "zweiter"):
        z.db.execute("INSERT INTO techniker VALUES(?,?,?)", (name, dienst.sha("schluessel-" + name), dienst.zeit()))
    z.db.commit()

    merker_a, merker_b = {}, {}
    await starte(mcp_attrappe("A", merker_a), P_MCP_A, handler_cancellation=True)
    mcp_b = await starte(mcp_attrappe("B", merker_b), P_MCP_B)

    async with aiohttp.ClientSession() as http:
        robert = Techniker(http, "schluessel-robert")
        zweiter = Techniker(http, "schluessel-zweiter")
        fremder = Techniker(http, "falsch")
        ohne = Techniker(http, None)

        print("Verwaltung und Anmeldung")
        status, ga, _ = await robert.ruf("POST", "/geraete", json={"seriennummer": "SMG-0001"})
        pruefe("Gerät A anlegen", status == 200 and len(ga["schluessel"]) >= 43)
        status, gb, _ = await robert.ruf("POST", "/geraete", json={"seriennummer": "SMG-0002"})
        pruefe("Verwaltung ohne Schlüssel abgelehnt", (await ohne.ruf("GET", "/geraete"))[0] == 401)
        pruefe("Verwaltung mit falschem Schlüssel abgelehnt", (await fremder.ruf("GET", "/geraete"))[0] == 401)

        def optionen(g, mcp_port):
            return {"geraetekennung": g["kennung"], "geraeteschluessel": g["schluessel"],
                    "mcp_url": f"http://127.0.0.1:{mcp_port}/geheim/mcp",
                    "dienst_url": f"ws://127.0.0.1:{P_GERAETE}/geraet"}

        app_a_obj = fernwartung.bauen(optionen(ga, P_MCP_A))
        await starte(app_a_obj, P_APP_A)
        await starte(fernwartung.bauen(optionen(gb, P_MCP_B)), P_APP_B)
        a, b = App(http, P_APP_A), App(http, P_APP_B)

        print("Einschalten und Übernahme")
        pruefe("Nach dem Start ist die Fernwartung aus", (await a.status())["zustand"] == "aus")
        pruefe("Unbekannte ID abgelehnt", (await robert.ruf("POST", "/uebernahme/beginn", json={"id": "12345678"}))[0] == 404)
        s = await a.ein()
        id1 = s["id"]
        pruefe("Einschalten liefert achtstellige ID", s["zustand"] == "bereit" and len(id1) == 8 and id1.isdigit(), str(s))
        pruefe("Restzeit läuft", 3590 <= s["restzeit"] <= 3600, str(s["restzeit"]))

        status, j, _ = await robert.ruf("POST", "/uebernahme/beginn", json={"id": id1})
        token1 = j["token"]
        erg = await asyncio.gather(robert.ruf("POST", "/uebernahme/abschluss", json={"token": token1}),
                                   robert.ruf("POST", "/uebernahme/abschluss", json={"token": token1}))
        sitzung1 = erg[0][1]["sitzung"]
        pruefe("Dieselbe Übernahme wiederholt: dieselbe Sitzung", erg[0][1] == erg[1][1] or erg[0][1]["sitzung"] == erg[1][1]["sitzung"])
        pruefe("App zeigt 'aktiv'", (await a.warte_auf(("aktiv",)))["zustand"] == "aktiv")
        pruefe("Zweiter Techniker: Sitzung bereits belegt", (await zweiter.uebernehmen(id1))[0] == 409)
        pruefe("Gleicher Techniker, neue Übernahme: belegt", (await robert.uebernehmen(id1))[0] == 409)
        pruefe("Fremder Token für andere Person ungültig",
               (await zweiter.ruf("POST", "/uebernahme/abschluss", json={"token": token1}))[0] == 410)

        print("Weiterleitung")
        status, j, kopf = await robert.mcp(sitzung1, "echo", {"wert": 42}, headers={"Mcp-Session-Id": "abc123"})
        pruefe("JSON-Antwort kommt durch", status == 200 and j["result"] == {"von": "A", "wert": 42}, str(j))
        pruefe("Mcp-Session-Id hin und zurück", merker_a["kopf"].get("mcp-session-id") == "abc123" and kopf.get("Mcp-Session-Id") == "abc123")
        pruefe("Techniker-Schlüssel erreicht das Kundengerät nicht", "authorization" not in merker_a["kopf"])
        pruefe("Sitzungskennung ohne Schlüssel: kein Zugriff", (await ohne.mcp(sitzung1, "echo", {"wert": 1}))[0] == 401)
        pruefe("Sitzungskennung mit anderem Techniker: kein Zugriff", (await zweiter.mcp(sitzung1, "echo", {"wert": 1}))[0] == 410)
        pruefe("Andere Pfade gibt es nicht", (await robert.ruf("POST", f"/s/{sitzung1}/anderes", json={}))[0] == 404)
        pruefe("Andere Methoden abgelehnt", (await robert.ruf("PUT", f"/s/{sitzung1}/mcp", json={}))[0] == 405)

        zeiten = []
        beginn = time.monotonic()
        async with http.post(f"{TECH}/s/{sitzung1}/mcp", headers=robert.kopf,
                             json={"jsonrpc": "2.0", "id": 1, "method": "strom"}) as r:
            typ = r.headers.get("Content-Type", "")
            async for stueck in r.content.iter_any():
                zeiten.append(time.monotonic() - beginn)
        pruefe("SSE-Strom kommt stückweise an", typ.startswith("text/event-stream") and len(zeiten) >= 3 and zeiten[0] < 0.25 < zeiten[-1], str(zeiten))

        erg = await asyncio.gather(*[robert.mcp(sitzung1, "echo", {"wert": i, "warten": (6 - i) * 0.05}) for i in range(6)])
        pruefe("Gleichzeitige Anfragen werden nicht vertauscht", [e[1]["result"]["wert"] for e in erg] == list(range(6)))

        aufgabe = asyncio.create_task(robert.mcp(sitzung1, "langsam"))
        await asyncio.sleep(0.4)
        aufgabe.cancel()
        await asyncio.sleep(0.6)
        pruefe("Abbruch beim Techniker bricht die Anfrage am Gerät ab", merker_a.get("abgebrochen") is True)

        print("Zwei Geräte")
        sb = await b.ein()
        status, sitzung_b, _ = await zweiter.uebernehmen(sb["id"])
        ja = (await robert.mcp(sitzung1, "echo", {"wert": 1}))[1]
        jb = (await zweiter.mcp(sitzung_b, "echo", {"wert": 1}))[1]
        pruefe("Sitzung A erreicht nur Gerät A, Sitzung B nur Gerät B", ja["result"]["von"] == "A" and jb["result"]["von"] == "B")
        async with http.ws_connect(f"ws://127.0.0.1:{P_GERAETE}/geraet") as ws:
            await ws.send_json({"typ": "anmeldung", "kennung": ga["kennung"], "schluessel": ga["schluessel"]})
            m = await ws.receive_json()
        pruefe("Zweite Anmeldung mit demselben Geräteschlüssel abgelehnt", m == {"typ": "abgelehnt", "grund": "konflikt"}, str(m))
        pruefe("Bestehende Sitzung läuft dabei weiter", (await robert.mcp(sitzung1, "echo", {"wert": 2}))[0] == 200)
        async with http.ws_connect(f"ws://127.0.0.1:{P_GERAETE}/geraet") as ws:
            await ws.send_json({"typ": "anmeldung", "kennung": ga["kennung"], "schluessel": "falsch"})
            m = await ws.receive_json()
        pruefe("Falscher Geräteschlüssel abgelehnt", m.get("grund") == "anmeldung")

        print("Ausschalten")
        await a.druecke("aus")
        pruefe("Nach dem Ausschalten schlägt die nächste Anfrage fehl", (await robert.mcp(sitzung1, "echo", {"wert": 3}))[0] == 410)
        pruefe("Alte ID wertlos", (await robert.ruf("POST", "/uebernahme/beginn", json={"id": id1}))[0] == 404)
        s = await a.ein()
        id2 = s["id"]
        pruefe("Erneutes Einschalten gibt eine andere ID", s["zustand"] == "bereit" and id2 != id1)
        pruefe("Alte Sitzungskennung öffnet die neue Sitzung nicht", (await robert.mcp(sitzung1, "echo", {"wert": 3}))[0] == 410)
        status, j, _ = await robert.ruf("POST", "/uebernahme/beginn", json={"id": id2})
        alter_token = j["token"]
        await a.druecke("aus")
        s = await a.ein()
        pruefe("Token einer beendeten Sitzung gilt nicht für die nächste",
               (await robert.ruf("POST", "/uebernahme/abschluss", json={"token": alter_token}))[0] == 410)
        pruefe("Schließen und erneutes Öffnen der Seite zeigt denselben Stand", (await a.status())["id"] == s["id"])

        print("Sperren")
        status, sitzung_a, _ = await robert.uebernehmen(s["id"])
        status, j, _ = await robert.ruf("POST", "/geraete/SMG-0001/sperren")
        pruefe("Sperren beendet die laufende Sitzung", j["sitzung_beendet"] and (await robert.mcp(sitzung_a, "echo", {"wert": 1}))[0] == 410)
        s = await a.warte_auf(("aus",))
        pruefe("App zeigt das Ende an", s["zustand"] == "aus" and "Support" in s["hinweis"], str(s))
        s = await a.ein()
        pruefe("Gesperrtes Gerät bekommt keine ID mehr", s["zustand"] == "aus" and s["id"] is None and "gesperrt" in s["hinweis"], str(s))

        print("Fristen und Verbindungsverlust")
        await b.druecke("aus")
        dienst.FRIST_S = 1.5
        s = await b.ein()
        status, sitzung_b, _ = await zweiter.uebernehmen(s["id"])
        pruefe("Vor Fristende erreichbar", (await zweiter.mcp(sitzung_b, "echo", {"wert": 1}))[0] == 200)
        await asyncio.sleep(1.8)
        pruefe("Frist im Dienst beendet die Sitzung", (await zweiter.mcp(sitzung_b, "echo", {"wert": 1}))[0] == 410)
        s = await b.warte_auf(("aus",))
        pruefe("App meldet den Zeitablauf", s["zustand"] == "aus" and "Zeit" in s["hinweis"], str(s))
        ws = await http.ws_connect(f"ws://127.0.0.1:{P_GERAETE}/geraet")
        await ws.send_json({"typ": "anmeldung", "kennung": gb["kennung"], "schluessel": gb["schluessel"]})
        await ws.receive_json()
        await ws.send_json({"typ": "bereit"})
        await ws.receive_json()
        try:
            m = await asyncio.wait_for(ws.receive_json(), 3)
        except Exception as e:
            m = {"fehler": repr(e)}
        pruefe("Dienst beendet zur Frist von sich aus", m == {"typ": "ende", "grund": "frist"}, str(m))
        await ws.close()
        await asyncio.sleep(0.2)
        dienst.FRIST_S = 3600
        fernwartung.FRIST_S = 1.5
        s = await b.ein()
        id_b = s["id"]
        await asyncio.sleep(1.8)
        s = await b.status()
        pruefe("Eigene Frist der App beendet unabhängig vom Dienst", s["zustand"] == "aus" and "Zeit" in s["hinweis"], str(s))
        await asyncio.sleep(0.2)
        pruefe("Danach ist die ID im Dienst ungültig", (await zweiter.ruf("POST", "/uebernahme/beginn", json={"id": id_b}))[0] == 404)
        fernwartung.FRIST_S = 3600

        dienst.PING_S, dienst.PING_TIMEOUT_S = 0.2, 0.8
        ws = await http.ws_connect(f"ws://127.0.0.1:{P_GERAETE}/geraet")  # Gerät, das nach der Anmeldung verstummt
        await ws.send_json({"typ": "anmeldung", "kennung": gb["kennung"], "schluessel": gb["schluessel"]})
        await ws.receive_json()
        await ws.send_json({"typ": "bereit"})
        stumme_id = (await ws.receive_json())["id"]
        await asyncio.sleep(1.5)
        pruefe("Verstummtes Gerät: Sitzung endet nach der Erkennungszeit",
               (await zweiter.ruf("POST", "/uebernahme/beginn", json={"id": stumme_id}))[0] == 404)
        await ws.close()
        dienst.PING_S, dienst.PING_TIMEOUT_S = 15, 45

        async def stummer_dienst(request):  # Dienst, der nach der Sitzung verstummt
            w = web.WebSocketResponse()
            await w.prepare(request)
            await w.receive()
            await w.send_json({"typ": "angemeldet"})
            await w.receive()
            await w.send_json({"typ": "sitzung", "id": "11112222", "restzeit": 3600})
            await asyncio.sleep(5)
            return w
        stumm = web.Application()
        stumm.add_routes([web.get("/geraet", stummer_dienst)])
        await starte(stumm, P_STUMM)
        fern_b = fernwartung.Fernwartung({**optionen(gb, P_MCP_B), "dienst_url": f"ws://127.0.0.1:{P_STUMM}/geraet"})
        fernwartung.PING_TIMEOUT_S = 0.8
        fern_b.einschalten()
        await asyncio.sleep(0.4)
        vorher = fern_b.status()["zustand"]
        await asyncio.sleep(1.0)
        s = fern_b.status()
        pruefe("Verstummter Dienst: App zeigt 'unterbrochen' ohne ID", vorher == "bereit" and s["zustand"] == "unterbrochen" and s["id"] is None, f"{vorher} {s}")
        fernwartung.PING_TIMEOUT_S = 45

        print("Fehlerzustände beim Einschalten")
        await mcp_b.cleanup()
        s = await b.ein()
        pruefe("HA-MCP gestoppt: keine ID, verständliche Meldung", s["zustand"] == "aus" and s["id"] is None and "MCP" in s["hinweis"], str(s))
        fern_x = fernwartung.Fernwartung({**optionen(ga, P_MCP_A), "dienst_url": "ws://127.0.0.1:1/geraet"})
        fern_x.einschalten()
        await asyncio.sleep(0.5)
        s = fern_x.status()
        pruefe("Dienst nicht erreichbar: keine ID, verständliche Meldung", s["zustand"] == "aus" and s["id"] is None and "nicht erreichbar" in s["hinweis"], str(s))

        print("Schutzmaßnahmen")
        fernwartung.NUR_INGRESS = True
        async with http.get(f"http://127.0.0.1:{P_APP_A}/api/status") as r:
            pruefe("Oberfläche nur über den Ingress-Proxy", r.status == 403)
        fernwartung.NUR_INGRESS = False
        codes = [(await zweiter.ruf("POST", "/uebernahme/beginn", json={"id": "00000000"}))[0] for _ in range(12)]
        pruefe("Falsche IDs werden gebremst", codes[-1] == 429 and codes[0] == 404, str(codes))
        z.db.execute("DELETE FROM techniker WHERE name='zweiter'")
        z.db.commit()
        pruefe("Schlüsselentzug sperrt den Techniker sofort", (await zweiter.ruf("GET", "/geraete"))[0] == 401)

        print("Neustart des Dienstes")
        status, gc, _ = await robert.ruf("POST", "/geraete", json={"seriennummer": "SMG-0003"})
        app_c = App(http, P_APP_B + 1)
        await starte(fernwartung.bauen(optionen(gc, P_MCP_A)), P_APP_B + 1)
        s = await app_c.ein()
        status, sitzung_c, token_c = await robert.uebernehmen(s["id"])
        status, j, _ = await robert.ruf("POST", "/uebernahme/beginn", json={"id": "99999999"})
        for l in laeufer:
            await l.cleanup()
        z.db.close()
        z2, laeufer2 = await dienst.starten(db, P_GERAETE, P_TECH, "127.0.0.1")
        pruefe("Nach Neustart: alte Sitzungskennung wirkungslos", (await robert.mcp(sitzung_c, "echo", {"wert": 1}))[0] == 410)
        pruefe("Nach Neustart: alter Übernahme-Token wirkungslos",
               (await robert.ruf("POST", "/uebernahme/abschluss", json={"token": token_c}))[0] == 410)
        pruefe("Nach Neustart: alte ID wirkungslos", (await robert.ruf("POST", "/uebernahme/beginn", json={"id": s["id"]}))[0] == 404)
        s2 = await app_c.warte_auf(("unterbrochen", "aus"))
        pruefe("App bleibt nach Neustart des Dienstes aus", s2["zustand"] in ("unterbrochen", "aus") and s2["id"] is None, str(s2))

        print("Protokoll")
        status, zeilen, _ = await robert.ruf("GET", "/protokoll")
        gruende = {r["grund"] for r in zeilen}
        pruefe("Protokoll nennt Beendigungsgründe",
               {"ausgeschaltet", "frist", "gesperrt", "verbindung_verloren", "konflikt_abgelehnt"} <= gruende, str(gruende))
        pruefe("Protokoll nennt den Techniker", any(r["techniker"] == "robert" for r in zeilen))
        z2.db.close()
        roh = open(db, "rb").read()
        geheim = [ga["schluessel"], gb["schluessel"], "schluessel-robert", sitzung1, sitzung_a, token1]
        pruefe("Datenbank enthält keine Schlüssel, Sitzungskennungen oder Token", not any(g.encode() in roh for g in geheim))

    fehler = ergebnisse.count(False)
    print(f"\n{len(ergebnisse) - fehler} von {len(ergebnisse)} Prüfungen bestanden")
    return 1 if fehler else 0


if __name__ == "__main__":
    code = asyncio.run(haupt())
    sys.stdout.flush()
    os._exit(code)
