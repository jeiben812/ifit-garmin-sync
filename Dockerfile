FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    INBOX_DIR=/data/inbox \
    SYNC_INTERVAL=900

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./

RUN useradd -u 1000 -m app && mkdir -p /data && chown app /data
USER app
VOLUME /data

CMD ["sh", "-c", "exec python sync.py --watch ${SYNC_INTERVAL}"]
