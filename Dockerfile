FROM python:3.12-slim

WORKDIR /app

COPY server/requirements.txt server/requirements.txt
RUN pip install --no-cache-dir -r server/requirements.txt

COPY server/ server/
COPY manage.py .

# 永続データ(DB・スクショ)は /data に。docker compose がボリュームを割り当てる
ENV ZATSUMU_DATA_DIR=/data
RUN useradd --create-home app && mkdir -p /data && chown app:app /data
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

# Render などの PaaS は PORT 環境変数で待ち受けポートを指定してくる
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
