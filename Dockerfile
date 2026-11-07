FROM python:3.15-slim AS builder

WORKDIR /app

# ckzg currently needs a source build with Python 3.15.
RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.15-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends procps ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt && \
    python -c "import ckzg; import hyperliquid" && \
    rm -rf /wheels

COPY hyperliquid-discord-monitor.py hdm.py ./
COPY src ./src

# A named Docker volume mounted on /app/data inherits this ownership.
RUN mkdir -p /app/data && chown -R 65534:65534 /app/data
USER 65534:65534

CMD ["python", "hyperliquid-discord-monitor.py", "addresses.txt"]
