# Xponent — developer convenience targets.
# Data volumes follow decision D5: dev inner-loop, 1M demo/functional, 10M benchmark.

COMPOSE ?= docker compose
PY      ?= python3

.PHONY: help up down logs ps verify-caps psql-primary psql-summary \
        seed-dev seed-demo seed-bench clean

help:
	@echo "Targets:"
	@echo "  up            Bring up pg-primary, pg-summary, redis (schemas auto-apply)"
	@echo "  down          Stop containers (keep volumes)"
	@echo "  clean         Stop and DELETE volumes (destroys seeded data)"
	@echo "  ps / logs     Container status / tail logs"
	@echo "  verify-caps   Print enforced CPU/memory limits (DB-safety precondition)"
	@echo "  psql-primary  psql into the primary DB"
	@echo "  psql-summary  psql into the summary DB"
	@echo "  seed-dev      Seed 50k orders / 5 tenants   (fast inner loop)"
	@echo "  seed-demo     Seed 1M orders / 10 tenants    (demo default, D5)"
	@echo "  seed-bench    Seed 10M orders / 100 tenants   (headline benchmark, D5)"

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

clean:
	$(COMPOSE) down -v

ps:
	$(COMPOSE) ps

logs:
	$(COMPOSE) logs -f --tail=100

# DB-safety precondition: confirm caps are actually enforced before any overhead test.
verify-caps:
	@docker inspect xpo-pg-primary xpo-pg-summary xpo-redis \
	  --format '{{.Name}}: NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}' \
	  2>/dev/null || echo "Containers not running; run 'make up' first."

psql-primary:
	$(COMPOSE) exec pg-primary psql -U postgres -d primary_db

psql-summary:
	$(COMPOSE) exec pg-summary psql -U agg_user -d agg_db

# Seeding (task T2). Requires scripts deps:  pip install -r scripts/requirements.txt
seed-dev:
	$(PY) scripts/seed_dataset.py --rows 50000 --tenants 5 --seed 42 \
	  --dsn $${PRIMARY_SUPERUSER_URL:-postgresql://postgres:primary_password@localhost:5432/primary_db}

seed-demo:
	$(PY) scripts/seed_dataset.py --rows 1000000 --tenants 10 --seed 42 \
	  --dsn $${PRIMARY_SUPERUSER_URL:-postgresql://postgres:primary_password@localhost:5432/primary_db}

seed-bench:
	$(PY) scripts/seed_dataset.py --rows 10000000 --tenants 100 --seed 42 \
	  --dsn $${PRIMARY_SUPERUSER_URL:-postgresql://postgres:primary_password@localhost:5432/primary_db}

# Live ingest demo: stream NEW orders into the source DBs so judges can watch the
# rollups stay fresh under load. RATE defaults to 500 orders/sec for 60s. Reads
# TENANT_SOURCES (per-tenant DBs) from the environment when set.
load:
	$(PY) scripts/load_generator.py --rate $${RATE:-500} --duration $${DURATION:-60} \
	  --dsn $${PRIMARY_SUPERUSER_URL:-postgresql://postgres:primary_password@localhost:5432/primary_db} \
	  --tenants $${TENANTS:-1,2}
