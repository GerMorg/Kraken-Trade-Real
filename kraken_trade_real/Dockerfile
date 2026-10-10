FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION=0.1.52

LABEL \
    io.hass.name="Kraken Trade Real" \
    io.hass.description="Autonomous Kraken trading application for Home Assistant" \
    io.hass.type="app" \
    io.hass.version="$BUILD_VERSION"

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY run.sh ./
RUN apk add --no-cache python3 py3-pip \
    && pip3 install --no-cache-dir --break-system-packages . \
    && chmod a+x /app/run.sh

CMD ["/app/run.sh"]
