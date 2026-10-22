# m2m 読み取り係。zatsumu 本体 (python:3.12-slim) とは別イメージ・別コンテナで動かす。
# ベースは Playwright 公式イメージ (Chromium と依存ライブラリ入り)。requirements.txt と版をそろえること
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

WORKDIR /app
COPY m2m_sync/requirements.txt m2m_sync/requirements.txt
RUN pip install --no-cache-dir -r m2m_sync/requirements.txt
COPY m2m_sync/ m2m_sync/

# ログイン状態・LAST_STATUS・失敗時スクショは /data (ボリューム) に置く
ENV M2M_DATA_DIR=/data PYTHONUNBUFFERED=1
RUN mkdir -p /data && chown pwuser:pwuser /data
USER pwuser

CMD ["python", "-m", "m2m_sync"]
