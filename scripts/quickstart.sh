#!/usr/bin/env bash
# Xponent — one-command bring-up for judges.
#
# Brings the whole system up from a clean clone: databases, per-tenant source
# DBs, synthetic data, rollup bootstrap, CDC worker and query API — then prints
# the dashboard URL. Idempotent-ish: safe to re-run (it re-seeds and re-bootstraps).
#
#   ./scripts/quickstart.sh            # ~200k orders across 2 tenants (fast)
#   ROWS=1000000 ./scripts/quickstart.sh   # bigger demo
#
# Prereqs: Docker (compose v2) running, Python 3.10+, and the script deps:
#   python3 -m venv .venv && . .venv/bin/activate && pip install -r scripts/requirements.txt
set -euo pipefail
cd "$(dirname "$0")/.."

PRIMARY_ADMIN="postgresql://postgres:primary_password@localhost:5432/postgres"
SUMMARY_OWNER="postgresql://agg_user:agg_password@localhost:5433/agg_db"
# Host-side source map (localhost ports). The containers use their own in-network
# DSNs defined in docker-compose.yml; these must point to the SAME slots.
export TENANT_SOURCES='[{"tenant_id":1,"dsn":"postgresql://postgres:primary_password@localhost:5432/tenant_1","slot":"agg_slot_t1"},{"tenant_id":2,"dsn":"postgresql://postgres:primary_password@localhost:5432/tenant_2","slot":"agg_slot_t2"}]'
ROWS="${ROWS:-200000}"

say(){ printf "\n\033[1m==> %s\033[0m\n" "$*"; }

command -v docker >/dev/null || { echo "Docker is required."; exit 1; }
python3 -c "import psycopg" 2>/dev/null || {
  echo "Python dep 'psycopg' missing. Run:"; echo "  pip install -r scripts/requirements.txt"; exit 1; }

say "1/7 Stopping CDC worker and API (if running)"
docker compose stop cdc-worker agg-api 2>/dev/null || true

say "2/7 Starting databases (pg-primary, pg-summary, redis)"
docker compose up -d pg-primary pg-summary redis-cache

say "waiting for Postgres to be healthy"
for i in $(seq 1 60); do
  if docker compose exec -T pg-primary pg_isready -U postgres -d primary_db >/dev/null 2>&1 \
  && docker compose exec -T pg-summary pg_isready -U agg_user -d agg_db >/dev/null 2>&1; then
    echo "   databases ready"; break
  fi
  sleep 2
  [ "$i" -eq 60 ] && { echo "databases did not become healthy in time"; exit 1; }
done

say "3/7 Dropping active replication slots and truncating data"
# Drop all agg_slot* replication slots so COPY doesn't block on ObjectInUse
for db in tenant_1 tenant_2; do
  docker compose exec -T pg-primary psql -U postgres -d "$db" -c \
    "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name LIKE 'agg_slot%';" 2>/dev/null || true
  docker compose exec -T pg-primary psql -U postgres -d "$db" -c \
    "TRUNCATE order_items, orders, customers, products, tenants CASCADE;" 2>/dev/null || true
done

say "4/7 Provisioning per-tenant source databases (tenant_1, tenant_2)"
python3 scripts/provision_tenant_dbs.py --admin-dsn "$PRIMARY_ADMIN" --dbs tenant_1,tenant_2

say "5/7 Seeding synthetic data (~${ROWS} orders per tenant, 2 tenants)"
# Per-tenant-DB mode: each DB gets ONE tenant's catalog + ROWS orders. --assign-tenant
# MUST pair with --tenants 1 (otherwise the tenants table is written once per generator
# tenant → duplicate tenant_id PK violation).
python3 scripts/seed_dataset.py --rows "$ROWS" --tenants 1 --seed 42 --assign-tenant 1 \
  --dsn postgresql://postgres:primary_password@localhost:5432/tenant_1 --manifest results/seed_t1.json
python3 scripts/seed_dataset.py --rows "$ROWS" --tenants 1 --seed 42 --assign-tenant 2 \
  --dsn postgresql://postgres:primary_password@localhost:5432/tenant_2 --manifest results/seed_t2.json

say "6/7 Bootstrapping rollups at L0 (one CDC slot per tenant DB)"
python3 -m worker.bootstrap --summary-dsn "$SUMMARY_OWNER" --force

say "7/7 Starting the CDC worker + query API"
docker compose up -d cdc-worker agg-api

say "Waiting for the API"
for i in $(seq 1 30); do
  if curl -fsS http://localhost:8000/api/v1/health >/dev/null 2>&1; then echo "   API up"; break; fi
  sleep 2
done

cat <<'DONE'

────────────────────────────────────────────────────────────
  Xponent is up  →  http://localhost:8000
  Operator key (dev):  xpo-admin-dev   (enter it in the sidebar)

  Try it:
   • Organization view: leaderboard, live activity, Databases
   • Click a restaurant row to drop into its own scoped dashboard
   • Ask a question in plain English (instant)
   • Stream live load:   make load         (500 orders/sec)
────────────────────────────────────────────────────────────
DONE
