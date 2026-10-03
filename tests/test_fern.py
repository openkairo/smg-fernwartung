"""Prüft das fern-Skript gegen einen lokal laufenden Dienst (Modus "direkt", ohne SSH, Claude durch Attrappe ersetzt)."""
import asyncio, json, os, subprocess, sys, tempfile
W = os.path.expanduser("~/code/smg-fernwartung")
sys.path.insert(0, W + "/server"); sys.path.insert(0, W + "/smg_fernwartung"); sys.path.insert(0, W + "/tests")
os.environ["FERN_OHNE_INGRESS_PRUEFUNG"] = "1"
import dienst, fernwartung
from test_ablauf import mcp_attrappe, starte, App
import aiohttp

async def haupt():
    tmp = tempfile.mkdtemp(); heim = os.path.join(tmp, "heim"); os.makedirs(heim + "/.config/smg-fern", 0o700)
    z, _ = await dienst.starten(tmp + "/db.sqlite3", 18100, 18101, "127.0.0.1")
    z.db.execute("INSERT INTO techniker VALUES('robert',?,?)", (dienst.sha("tk"), dienst.zeit())); z.db.commit()
    await starte(mcp_attrappe("A", {}), 18110)
    # Claude-Attrappe: schreibt ihre Aufrufdaten weg
    attrappe = tmp + "/claude"; open(attrappe, "w").write(
        "#!/bin/sh\npwd > %s/cwd\nls -A | wc -l > %s/leer\nenv > %s/env\nfor a in \"$@\"; do echo \"$a\"; done > %s/args\n"
        "cat \"$2\" > %s/mcp\necho \"$2\" > %s/mcpweg\n" % ((tmp,) * 6)); os.chmod(attrappe, 0o755)
    k = {"direkt": "http://127.0.0.1:18101", "techniker_schluessel": "tk", "claude": attrappe}
    kp = heim + "/.config/smg-fern/config.json"; open(os.open(kp, os.O_WRONLY | os.O_CREAT, 0o600), "w").write(json.dumps(k))
    env = {**os.environ, "HOME": heim, "CLAUDE_GEERBT": "1", "ANTHROPIC_GEERBT": "1"}
    async def fern(*a):
        p = await asyncio.create_subprocess_exec(W + "/mac/fern", *a, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        aus, _ = await p.communicate(); return p.returncode, aus.decode()
    ok = []
    def pruefe(n, b, zus=""): ok.append(b); print(("  ok      " if b else "  FEHLER  ") + n + ("" if b else "  [" + str(zus)[:300] + "]"))
    code, aus = await fern("neu", "SMG-0100")
    zeilen = dict(l.split(": ", 1) for l in aus.splitlines() if ": " in l and l.startswith("geraete"))
    pruefe("fern neu gibt Kennung und Schlüssel aus", code == 0 and len(zeilen) == 2, aus)
    opt = {"geraetekennung": zeilen["geraetekennung"], "geraeteschluessel": zeilen["geraeteschluessel"],
           "mcp_url": "http://127.0.0.1:18110/geheim/mcp", "dienst_url": "ws://127.0.0.1:18100/geraet"}
    await starte(fernwartung.bauen(opt), 18120)
    async with aiohttp.ClientSession() as http:
        app = App(http, 18120)
        code, aus = await fern("12345678"); pruefe("falsche ID: verständliche Absage", code == 1 and "unbekannt" in aus, aus)
        s = await app.ein()
        code, aus = await fern(s["id"][:4], s["id"][4:])
        pruefe("fern <ID> in zwei Vierergruppen startet die Sitzung", code == 0 and "übernommen" in aus, aus)
        pruefe("App zeigt 'aktiv'", (await app.status())["zustand"] == "aktiv")
        args = open(tmp + "/args").read().split("\n"); mcp = json.load(open(tmp + "/mcp")); umg = open(tmp + "/env").read()
        pruefe("Claude bekommt nur den Kunden-Anschluss", list(mcp["mcpServers"]) == ["kunde"] and "--strict-mcp-config" in args)
        pruefe("Eingebaute Werkzeuge abgeschaltet", args[args.index("--tools") + 1] == "")
        pruefe("Arbeitsregeln werden mitgegeben", "--append-system-prompt" in args)
        pruefe("Eigenes Profil gesetzt, geerbte Claude-Umgebung entfernt", "CLAUDE_CONFIG_DIR=" + heim in umg and "GEERBT" not in umg)
        pruefe("Leeres Arbeitsverzeichnis", open(tmp + "/leer").read().strip() == "0")
        pruefe("Nach Sitzungsende: temporäre Konfiguration gelöscht", not os.path.exists(open(tmp + "/mcpweg").read().strip()))
        code, aus = await fern(s["id"]); pruefe("Zweite Übernahme derselben ID: belegt", code == 1 and "belegt" in aus, aus)
        code, aus = await fern("sperren", "SMG-0100"); pruefe("fern sperren beendet die Sitzung", code == 0 and "beendet" in aus, aus)
        code, aus = await fern("geraete"); pruefe("fern geraete zeigt 'gesperrt'", "gesperrt" in aus, aus)
        code, aus = await fern("protokoll"); pruefe("fern protokoll zeigt Techniker und Grund", "robert" in aus and "gesperrt" in aus, aus)
        os.chmod(kp, 0o644); code, aus = await fern("geraete"); pruefe("Zu offene Rechte werden abgelehnt", code == 1 and "chmod" in aus, aus)
    print(f"{ok.count(True)} von {len(ok)}"); sys.stdout.flush(); os._exit(0 if all(ok) else 1)
asyncio.run(haupt())
