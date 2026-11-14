# filemanager.py — single-file web file manager
#
# Build:
#   docker build -t filemanager .
#
# Run (serves the current host directory as /data):
#   docker run --rm -p 8000:8000 -v "$PWD:/data" filemanager
#
# There is no authentication and no HTTPS, and the Run/Shell features execute
# arbitrary commands. Only expose this on a trusted network or behind an
# authenticating reverse proxy.
#
# The served folder is also a WebDAV share, at /dav/, so it can be mounted
# instead of browsed:
#   sudo mount -t davfs http://127.0.0.1:8000/dav/ /mnt/data
# Pass --no-dav to leave that out, or --dav-path PREFIX to move it.

FROM python:3.12-alpine

# Only two tiny helpers are added on top of the Python base image:
#   bash — used by the Shell feature
#   make — used by the Tools menu's make items and by automatic make
# Pillow is deliberately omitted; thumbnails fall back to a generic icon.
# To enable them, add `py3-pillow` to the apk line below.
RUN apk add --no-cache bash make \
    && adduser -D -u 1000 -h /home/app app \
    && mkdir -p /data \
    && chown app:app /data

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --chown=app:app filemanager.py /app/filemanager.py

# Unprivileged by default. To edit files owned by another UID, run with
# --user "$(id -u):$(id -g)" and make sure the mounted directory is writable.
USER app

EXPOSE 8000
VOLUME ["/data"]

# /health is the liveness endpoint: it never touches the served tree and is
# left out of the access log, so polling it every 30s stays cheap and quiet.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

ENTRYPOINT ["python3", "/app/filemanager.py"]
CMD ["/data", "--host", "0.0.0.0", "--port", "8000"]
