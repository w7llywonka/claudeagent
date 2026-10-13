#!/bin/sh
set -eu
umask 077

# Container persistence uses one exact location; never chown arbitrary env paths.
if [ "${DB_PATH:-/data/chat.db}" != "/data/chat.db" ]; then
    printf '%s\n' 'Container DB_PATH must be /data/chat.db; mount the volume at /data.' >&2
    exit 1
fi
export DB_PATH=/data/chat.db

if [ -L /data ]; then
    printf '%s\n' 'Refusing a symlink at /data.' >&2
    exit 1
fi
mkdir -p /data

for db_file in /data/chat.db /data/chat.db-wal /data/chat.db-shm /data/chat.db-journal; do
    if [ -L "$db_file" ]; then
        printf '%s\n' 'Refusing a symlink in the database paths.' >&2
        exit 1
    fi
    if [ -e "$db_file" ] && [ ! -f "$db_file" ]; then
        printf '%s\n' 'Database paths must be regular files.' >&2
        exit 1
    fi
done

if [ "$(id -u)" = 0 ]; then
    chown app:app /data
    chmod 0700 /data
    for db_file in /data/chat.db /data/chat.db-wal /data/chat.db-shm /data/chat.db-journal; do
        if [ -f "$db_file" ]; then
            chown app:app "$db_file"
            chmod 0600 "$db_file"
        fi
    done
    exec gosu app:app "$@"
fi

if [ "$(id -u)" != 10001 ]; then
    printf '%s\n' 'Run the entrypoint as root for volume setup, or as app (UID 10001).' >&2
    exit 1
fi
if [ ! -w /data ]; then
    printf '%s\n' 'The app user cannot write /data; allow the entrypoint to prepare the volume.' >&2
    exit 1
fi
exec "$@"
