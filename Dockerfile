FROM python:3.10-slim-buster

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --upgrade setuptools

RUN pip install --no-cache-dir -r requirements.txt

COPY hyperliquid-discord-monitor.py .
COPY addresses.txt .
COPY .env .

CMD ["python", "hyperliquid-discord-monitor.py", "addresses.txt"]
