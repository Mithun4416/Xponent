# Primary OLTP image: PostgreSQL 16 + wal2json logical-decoding plugin (decision D6).
# wal2json emits structured JSON changes with full before-images under
# REPLICA IDENTITY FULL, which the Python CDC worker consumes reliably.
# (Debian-based image so the PGDG wal2json package is available.)
FROM postgres:16

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-wal2json \
    && rm -rf /var/lib/apt/lists/*
