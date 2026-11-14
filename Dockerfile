# Library Doctor: one small container. Python standard library only, plus
#   ffmpeg/ffprobe  - file health checks, Convert to MKV, joining trailer video+audio
#   deno            - the JavaScript runtime yt-dlp needs for YouTube (only used with Agregarr's Coming Soon trailers)
FROM python:3.12-alpine

RUN apk add --no-cache ffmpeg deno tzdata

COPY app/ /app/
# readable by whatever user the container runs as (compose "user:"), writable by nobody
RUN chmod -R a+rX,go-w /app

ENV LD_CONFIG=/config \
    LD_MEDIA_ROOT=/media \
    LD_LISTEN=0.0.0.0 \
    LD_PORT=8790 \
    LD_SCAN_HOUR=3 \
    LD_TRASH_DAYS=7 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runs as a normal user. Use the same user/group that owns your media (compose: user: "1000:1000").
USER 1000:1000
EXPOSE 8790
VOLUME ["/config"]

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('LD_PORT','8790'), timeout=5)" || exit 1

CMD ["python3", "-u", "/app/ldoctor.py"]
