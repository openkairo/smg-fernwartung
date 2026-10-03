FROM python:3.12-slim
RUN pip install --no-cache-dir aiohttp==3.14.3
WORKDIR /app
COPY dienst.py .
ENV TZ=Europe/Berlin FERN_DB=/data/fern.sqlite3
CMD ["python", "-u", "dienst.py"]
