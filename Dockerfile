FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Render sets $PORT automatically
CMD gunicorn main:app --workers 1 --threads 2 --timeout 120 --bind 0.0.0.0:${PORT:-10000}
