FROM --platform=$TARGETPLATFORM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV STREAMLIT_SERVER_HEADLESS=true
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        mosquitto \
        mosquitto-clients \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY docker/mosquitto.conf /etc/mosquitto/conf.d/smart-garden.conf
COPY docker/entrypoint.sh /app/docker/entrypoint.sh
COPY src ./src
COPY scripts/alert_notifier.py ./scripts/alert_notifier.py
COPY .streamlit/config.toml ./.streamlit/config.toml
COPY README.md .

RUN mkdir -p /app/data /tmp/mosquitto \
    && chmod +x /app/docker/entrypoint.sh

EXPOSE 8501 1883

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://127.0.0.1:8501/_stcore/health || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
