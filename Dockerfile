# luckydevs bot image. Shared file (owner: Sebi).
# No starter kit was provided, so the base image is our choice: Python 3.12 slim.
# The bot uses only the Python standard library (no pip install needed).
# Build with: python scripts/build.py <tag>   (always linux/amd64)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MEMORY_DIR=/memory

WORKDIR /app
COPY bot /app/bot

# Optional logo (at most 1 MB, at most 2048 px per side).
COPY logo.png /logo.png

# The arena runs `docker run -i --rm <image>` with no arguments.
CMD ["python", "-u", "-m", "bot.main"]
