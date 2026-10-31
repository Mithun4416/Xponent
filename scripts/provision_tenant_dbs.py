#!/usr/bin/env python3
"""Provision per-tenant source databases (per-tenant-DB topology).

Creates one database per tenant inside the primary Postgres cluster and applies
the source schema (tables, indexes) + the wal2json publication to each. This
represents the reviewer's model — each tenant has its own source database —
without extra containers. (Production: separate instances/hosts; here, separate
databases/catalogs with their own slots is a faithful prototype.)

Usage:
    python3 scripts/provision_tenant_dbs.py \
        --admin-dsn postgresql://postgres:primary_password@localhost:5432/postgres \
        --dbs tenant_1,tenant_2
"""
from __future__ import annotations

import argparse
import os
import sys

import psycopg

_SCHEMA_FILES = ["001_schema.sql", "002_indexes.sql", "004_publication.sql"]  # roles are cluster-global


def _schema_sql(schema_dir: str) -> str:
    parts = []
    for fn in _SCHEMA_FILES:
        with open(os.path.join(schema_dir, fn)) as f:
            parts.append(f.read())
    return "\n".join(parts)


def _admin_dsn_for_db(admin_dsn: str, dbname: str) -> str:
    # Swap the database component of the DSN (…/postgres -> …/<dbname>).
    base, _, _old = admin_dsn.rpartition("/")
    return f"{base}/{dbname}"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Create per-tenant source databases")
    ap.add_argument("--admin-dsn", required=True, help="superuser DSN to the cluster (…/postgres)")
    ap.add_argument("--dbs", required=True, help="comma-separated database names, e.g. tenant_1,tenant_2")
    ap.add_argument("--schema-dir", default="db/primary")
    a = ap.parse_args(argv)
    dbs = [d.strip() for d in a.dbs.split(",") if d.strip()]
    schema = _schema_sql(a.schema_dir)

    # CREATE DATABASE must run outside a transaction.
    with psycopg.connect(a.admin_dsn, autocommit=True) as admin:
        for db in dbs:
            exists = admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone()
            if exists:
                print(f"[provision] database {db} already exists; applying schema (idempotent)")
            else:
                admin.execute(f'CREATE DATABASE "{db}"')
                print(f"[provision] created database {db}")

    for db in dbs:
        with psycopg.connect(_admin_dsn_for_db(a.admin_dsn, db), autocommit=True) as conn:
            conn.execute(schema)   # CREATE TABLE IF NOT EXISTS / publication guarded by DO blocks
            print(f"[provision] {db}: schema + publication applied")

    print(f"[provision] done. Point TENANT_SOURCES at these DBs (one slot each) and bootstrap.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
