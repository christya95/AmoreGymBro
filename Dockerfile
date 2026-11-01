FROM python:3.12-slim

WORKDIR /app

RUN useradd --create-home --uid 1000 --shell /bin/sh app \
    && mkdir -p /data \
    && chown app:app /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV DATABASE_PATH=/data/fitness.db \
    PYTHONUNBUFFERED=1

EXPOSE 8787
ENTRYPOINT ["/entrypoint.sh"]
