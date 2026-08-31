# Optional container path. Render's native Python runtime (render.yaml) is the default and
# needs no Docker; this exists for Docker-based deploys, other hosts, or local parity testing.
#
#   docker build -t asr-downloader-backend .
#   docker run --rm -p 8000:8000 asr-downloader-backend

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

# ffmpeg lets yt-dlp inspect/remux awkward containers. curl backs the HEALTHCHECK below.
RUN apt-get update \
 && apt-get install --no-install-recommends -y ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY main.py updater.py ./

# Run unprivileged. /tmp stays writable for the cookie jar and pip upgrades.
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 65"]
