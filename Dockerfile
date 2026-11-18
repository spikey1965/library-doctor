# Library Doctor: one small container. Python standard library only, plus
#   ffmpeg/ffprobe  - file health checks and Convert to MKV
FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg tzdata su-exec openssl

COPY app/ /app/
# readable by whatever user the container runs as (compose "user:"), writable by nobody
RUN chmod -R a+rX,go-w /app
COPY --chmod=755 entrypoint.sh /entrypoint.sh

ENV LD_CONFIG=/config \
    LD_MEDIA_ROOT=/media \
    LD_LISTEN=0.0.0.0 \
    LD_PORT=8790 \
    LD_SCAN_HOUR=3 \
    LD_TRASH_DAYS=7 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PUID=1000 \
    PGID=1000

# Starts as root only to give /config to PUID:PGID, then runs as that user (set them to the owner of your media).
EXPOSE 8790
VOLUME ["/config"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('LD_PORT','8790'), timeout=5)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
