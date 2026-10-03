FROM python:3.13-slim
# ha-mcp (MIT) ist der MCP-Server, über den der Techniker Home Assistant bedient. Feste Version, bewusst geprüft.
RUN pip install --no-cache-dir aiohttp==3.14.3 ha-mcp==8.6.0
WORKDIR /app
COPY fernwartung.py mcp_start.py seite.html ./
CMD ["python", "-u", "fernwartung.py"]
