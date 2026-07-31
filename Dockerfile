FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: 音訊轉錄；libmagic1: 內容型別偵測；exiftool: 圖片 metadata
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg libmagic1 libimage-exiftool-perl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY app.py entrypoint.sh ./
COPY static ./static

RUN chmod +x entrypoint.sh \
 && useradd -m -u 10001 appuser \
 && chown -R appuser:appuser /app
USER appuser

ENV PORT=8443
EXPOSE 8443

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,ssl,urllib.request;\
ctx=ssl._create_unverified_context();\
s='https' if os.path.isdir('/certs') else 'http';\
urllib.request.urlopen(f'{s}://127.0.0.1:{os.environ[\"PORT\"]}/healthz',context=ctx,timeout=4)" || exit 1

ENTRYPOINT ["./entrypoint.sh"]
