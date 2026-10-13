FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/chat.db

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

COPY bot /app/bot
COPY entrypoint.sh /usr/local/bin/bot-entrypoint
RUN sed -i 's/\r$//' /usr/local/bin/bot-entrypoint \
    && chmod 0755 /usr/local/bin/bot-entrypoint

# The entrypoint prepares the mounted volume, then runs Python as UID 10001.
ENTRYPOINT ["/usr/local/bin/bot-entrypoint"]
CMD ["python", "-m", "bot"]
