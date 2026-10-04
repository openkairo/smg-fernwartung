"""Echter MCP-Server (Streamable HTTP) hinter der Weiterleitung, echter Claude-Code-Client davor."""
import asyncio, json, os, subprocess, sys, tempfile, threading
W = os.path.expanduser("~/code/smg-fernwartung")
sys.path.insert(0, W + "/server"); sys.path.insert(0, W + "/smg_fernwartung"); sys.path.insert(0, W + "/tests")
os.environ["FERN_OHNE_INGRESS_PRUEFUNG"] = "1"
import dienst, fernwartung
from test_ablauf import starte, App, Techniker
import aiohttp
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("kundengeraet", host="127.0.0.1", port=18210)
@mcp.tool()
def zaehlerstand(raum: str) -> str:
    """Liefert den Zählerstand eines Raums."""
    return f"Zählerstand {raum}: 4711,5 kWh (Prüfwort Rhabarber)"
threading.Thread(target=lambda: mcp.run(transport="streamable-http"), daemon=True).start()

async def haupt():
    tmp = tempfile.mkdtemp()
    z, _ = await dienst.starten(tmp + "/db.sqlite3", 18200, 18201, "127.0.0.1")
    z.db.execute("INSERT INTO techniker(name, schluessel_hash, angelegt) VALUES('robert',?,?)", (dienst.sha("tk"), dienst.zeit())); z.db.commit()
    await asyncio.sleep(1.5)
    async with aiohttp.ClientSession() as http:
        t = Techniker.__new__(Techniker); t.http = http; t.kopf = {"Authorization": "Bearer tk"}
        import test_ablauf; test_ablauf.TECH = "http://127.0.0.1:18201"
        _, g, _ = await t.ruf("POST", "/geraete", json={"seriennummer": "ECHT-1"})
        await starte(fernwartung.bauen({"geraetekennung": g["kennung"], "geraeteschluessel": g["schluessel"],
            "mcp_url": "http://127.0.0.1:18210/mcp", "dienst_url": "ws://127.0.0.1:18200/geraet"}), 18220)
        app = App(http, 18220); s = await app.ein(); print("App:", s)
        status, sitzung, _ = await t.uebernehmen(s["id"]); print("Übernahme:", status)
        os.makedirs(tmp + "/arbeit"); json.dump({"mcpServers": {"kunde": {"type": "http",
            "url": f"http://127.0.0.1:18201/s/{sitzung}/mcp", "headers": {"Authorization": "Bearer tk"}}}}, open(tmp + "/mcp.json", "w"))
        p = await asyncio.create_subprocess_exec("claude", "-p", "Rufe das Werkzeug zaehlerstand für den Raum Keller auf und gib nur dessen Antwort wörtlich wieder.",
            "--mcp-config", tmp + "/mcp.json", "--strict-mcp-config", "--tools", "", "--setting-sources", "",
            "--allowedTools", "mcp__kunde__zaehlerstand", "--output-format", "stream-json", "--verbose",
            cwd=tmp + "/arbeit", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        aus, _ = await p.communicate()
        for zeile in aus.decode().splitlines():
            try: m = json.loads(zeile)
            except Exception: continue
            if m.get("type") == "system" and m.get("subtype") == "init": print("Werkzeuge:", m["tools"], "| MCP:", m["mcp_servers"])
            if m.get("type") == "result": print("Antwort:", m.get("result"))
        await app.druecke("aus")
        print("Nach dem Ausschalten:", (await t.mcp(sitzung, "tools/list"))[0])
    sys.stdout.flush(); os._exit(0)
asyncio.run(haupt())
