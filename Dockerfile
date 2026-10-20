FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 EXCEL_HOST=0.0.0.0 EXCEL_STORAGE_PATH=/data EXCEL_REQUIRE_AUTH=false
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 libgdk-pixbuf-2.0-0 shared-mime-info fonts-noto-cjk && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home excel && mkdir /data && chown excel:excel /data
COPY app ./app
COPY config.yaml run.py ./
USER excel
LABEL org.opencontainers.image.version="1.2.0-rc.43"
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/api/v1/health', timeout=3)"
CMD ["python", "run.py"]
