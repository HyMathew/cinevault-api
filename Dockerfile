FROM python:3.13-alpine@sha256:1e1f6334529d1fb26a2ef619d7cffb30a7c7d7cc1cc7338ebee283a58d1aeaca AS python-runtime
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --only-binary=:all: -r /tmp/requirements.txt && \
    rm -rf /usr/local/lib/python3.13/sqlite3 /usr/local/lib/python3.13/lib-dynload/_sqlite3*.so

FROM alpine:3.24@sha256:3f4d6b32e26703c6082839234f43769d5046b3ac7c910033911d1e71d8cd630a
ENV PATH=/usr/local/bin:$PATH LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apk add --no-cache --virtual .cinevault-runtime ca-certificates \
    so:libbz2.so.1 so:libcrypto.so.3 so:libffi.so.8 so:libgdbm.so.6 \
    so:libgdbm_compat.so.4 so:liblzma.so.5 so:libncursesw.so.6 so:libpanelw.so.6 \
    so:libreadline.so.8 so:libssl.so.3 so:libuuid.so.1 zlib=1.4-r0 && \
    addgroup -S -g 10001 cinevault && adduser -S -D -u 10001 -G cinevault cinevault && \
    mkdir /data && chown 10001:10001 /data
COPY --from=python-runtime /usr/local /usr/local
WORKDIR /srv/cinevault
ENV DATA_FILE=/data/films.json
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 wsgi.py ./
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
