FROM python:3.12-alpine
RUN pip install --no-cache-dir aiohttp==3.14.3
WORKDIR /app
COPY fernwartung.py seite.html ./
CMD ["python", "-u", "fernwartung.py"]
