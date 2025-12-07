FROM python:3.10-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends procps

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --upgrade setuptools

RUN pip install --no-cache-dir -r requirements.txt

RUN getent group nobody || groupadd nobody
RUN chown -R nobody:nobody /app
USER nobody

COPY hyperliquid-discord-monitor.py .
COPY addresses.txt .
COPY .env .

CMD ["python", "hyperliquid-discord-monitor.py", "addresses.txt"]
