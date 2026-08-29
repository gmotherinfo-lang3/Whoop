# Built from the REPOSITORY ROOT, not ./server, because the server also serves
# the laptop bundle (the bridge, tray and Windows scripts) for download.
#   docker build -f server/Dockerfile .
FROM python:3.12-slim

WORKDIR /srv

COPY server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server/app ./app

# The laptop half, shipped so /setup can hand it out pre-configured.
COPY whoop_bridge ./bundle/whoop_bridge
COPY tray ./bundle/tray
COPY windows ./bundle/windows
COPY pyproject.toml requirements.txt ./bundle/

RUN useradd -r -u 10001 whoop && mkdir -p /data && chown whoop:whoop /data
USER whoop

ENV WHOOP_DB=/data/whoop.db
ENV BUNDLE_ROOT=/srv/bundle
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
