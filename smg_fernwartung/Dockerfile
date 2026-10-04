FROM python:3.13-slim
# ha-mcp (MIT) ist der MCP-Server, über den der Techniker Home Assistant bedient. Feste Version, bewusst geprüft.
RUN pip install --no-cache-dir aiohttp==3.14.3 ha-mcp==8.6.0
# Werkzeuge für Befehle des Technikers im Fehlerfall, dazu die Kommandozeile "ha" von Home Assistant
ARG BUILD_ARCH=aarch64
RUN apt-get update && apt-get install -y --no-install-recommends curl jq less procps nano \
    && rm -rf /var/lib/apt/lists/* \
    && (curl -fsSL -o /usr/local/bin/ha "https://github.com/home-assistant/cli/releases/latest/download/ha_${BUILD_ARCH}" \
        && chmod +x /usr/local/bin/ha || echo "ha-Kommandozeile nicht geladen")
WORKDIR /app
COPY fernwartung.py mcp_start.py seite.html ./
CMD ["python", "-u", "fernwartung.py"]
