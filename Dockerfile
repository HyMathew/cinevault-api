FROM python:3.13-slim@sha256:09a17cad02668ae61ece136b4fd36ac3849e14f670ebedb637ea3e15580525bc
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_FILE=/data/films.json
WORKDIR /srv/cinevault
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && \
    useradd --system --uid 10001 --create-home cinevault && \
    mkdir /data && chown 10001:10001 /data
COPY --chown=10001:10001 app ./app
COPY --chown=10001:10001 wsgi.py ./
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "--error-logfile", "-", "wsgi:app"]
