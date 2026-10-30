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

# 版の名前 (git のコミット)。/healthz で返し、どの版が動いているかを外から確かめられるようにする。
# 自動更新係 (deploy/auto-update) が渡す。最後の方に置き、依存のキャッシュを壊さない
ARG ZATSUMU_VERSION=
ENV ZATSUMU_VERSION=$ZATSUMU_VERSION

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1

# Render などの PaaS は PORT 環境変数で待ち受けポートを指定してくる
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
