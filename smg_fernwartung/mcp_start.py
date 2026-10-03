"""Startet den eingebauten HA-MCP-Server (Paket ha-mcp, MIT-Lizenz) für die Dauer einer Fernwartung.

Er lauscht nur innerhalb dieser App (127.0.0.1) unter einem Pfad, der für jede Sitzung neu gewürfelt wird,
und spricht Home Assistant über den Supervisor an. Ist die Fernwartung aus, läuft er nicht.
"""
import logging
import os

os.environ.setdefault("HOMEASSISTANT_URL", "http://supervisor/core")
os.environ["HOMEASSISTANT_TOKEN"] = os.environ["SUPERVISOR_TOKEN"]
# Vorgaben wie in der HA-MCP-App
os.environ.setdefault("BACKUP_HINT", "normal")
os.environ.setdefault("ENABLE_MANDATORY_BPS", "true")
os.environ.setdefault("ENABLE_STRICT_MANDATORY_BPS", "true")
os.environ.setdefault("ENABLE_AUTO_BACKUP", "true")
os.environ.setdefault("HA_VERIFY_SSL", "true")

logging.basicConfig(level=logging.WARNING)

from ha_mcp.__main__ import mcp  # noqa: E402

mcp.run(transport="http", host="127.0.0.1", port=int(os.environ["FERN_MCP_PORT"]),
        path=os.environ["FERN_MCP_PFAD"], stateless_http=True, uvicorn_config={"ws": "none"})
