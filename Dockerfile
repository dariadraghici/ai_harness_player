# luckydevs bot image. Shared file (owner: Sebi).
# Base image: Python 3.12 slim (open_spiel and the other wheels we bake exist for cp312).
# Build with: python scripts/build.py <tag>   (always linux/amd64)
#
# The harness itself uses only the Python standard library. The libraries below are for the ENGINE SUBSYSTEM
# (bot/libs/): prep may generate an adapter that uses one of them, and the adapter only ever runs inside a
# sandbox child. The bot process never imports them, so an idle specialist still costs almost nothing.
#   requirements-core.txt   installed, so the subsystem works with no network at all
#   requirements-extra.txt  only downloaded to /opt/wheelhouse, installable offline when a game needs it
# Build with --build-arg BAKE_LIBS=0 for a lean image: the subsystem then has nothing baked and falls back to
# installing from the network, or to the standard engines.
FROM python:3.12-slim

ARG BAKE_LIBS=1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MEMORY_DIR=/memory \
    BOT_WHEELHOUSE=/opt/wheelhouse

WORKDIR /app

COPY bot/libs/requirements-core.txt bot/libs/requirements-extra.txt /tmp/
RUN set -eu; \
    mkdir -p /opt/wheelhouse; \
    if [ "$BAKE_LIBS" = "1" ]; then \
        python -m pip install --no-cache-dir --only-binary=:all: --no-input -r /tmp/requirements-core.txt; \
        python -m pip download --no-cache-dir --only-binary=:all: --no-input \
            -d /opt/wheelhouse -r /tmp/requirements-extra.txt; \
    fi; \
    rm -f /tmp/requirements-core.txt /tmp/requirements-extra.txt; \
    find /usr/local/lib/python3.12/site-packages -type d -name tests -prune -exec rm -rf {} + || true; \
    find /usr/local/lib/python3.12/site-packages -type d -name __pycache__ -prune -exec rm -rf {} + || true

COPY bot /app/bot

# Optional logo (at most 1 MB, at most 2048 px per side).
COPY logo.png /logo.png

# The arena runs `docker run -i --rm <image>` with no arguments.
CMD ["python", "-u", "-m", "bot.main"]
