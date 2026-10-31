# Xponent — Progress Tracker

**Purpose:** Living status log so any session (human or AI) can resume without losing context. Update at every integration checkpoint and whenever a task changes state.
**Last updated:** 2026-10-31 — T1 + T2 code landed; pending validation on a Docker host.
**Companion:** `IMPLEMENTATION_PLAN.md` (tasks T1–T17, timeline), `TECHNICAL_GAP_ANALYSIS.md` (defects to fix), `OPEN_DECISIONS.md` (all approved), `SYNTHETIC_DATA_PLAN.md` (team-owned dataset).

---

## Current state

- **Phase:** Implementation started — CP1 foundation (T1, T2) written.
- **Current milestone:** **CP1 — Exact vertical slice at 1M/10 tenants.**
- **Dataset:** No organizer dataset — team generates its own deterministic synthetic data (`SYNTHETIC_DATA_PLAN.md`); our base tables + direct SQL are the oracle. O1–O5 resolved by us (N/A).
- **Decisions:** all D1–D5 **APPROVED** (2026-10-31).
- **Validation (Docker host, 2026-10-31) — PASSED:** `docker compose config` + all 3 containers up; caps enforced (primary 2 CPU/4 GiB, summary 1 CPU/2 GiB, redis 0.5 CPU/256 MiB); primary has 5 tables + `agg_layer_pub` + `orders` REPLICA IDENTITY FULL; summary has 7 tables incl. `agg_orders_minutely`, `bucket_digests`, `cdc_checkpoint`; `agg_reader` is a non-superuser; `make seed-dev` loaded 50,000 orders / 5 tenants / 149,912 items; order-total vs summed-line-items = **0 mismatches**; `agg_orders_daily` as `agg_reader` with no tenant set returned **0 rows** (fails closed).
- **Validation — PENDING (next run):** (a) FORCE-RLS catalog check via `pg_class.relrowsecurity`/`relforcerowsecurity` (an earlier query used a non-existent `pg_tables.forcerowsecurity` column); (b) positive tenant-isolation test (seed two tenants as owner, read as `agg_reader` with `SET LOCAL`, confirm scoped rows + 0 cross-tenant leak).
- **T6 (range decomposer) — DONE + verified here:** `app/services/range_decomposer.py` with 6/6 tests passing (incl. 5,000 random-range coverage checks) via `python3 tests/correctness/test_range_decomposer.py` (no deps needed). Half-open `[frm,to)`; aligned/minute-aligned ranges touch no primary; sub-minute residuals emitted as `edges`.
- **T3 (bootstrap) — code written, unverified:** `worker/bootstrap.py` + `worker/hashing.py` (canonical xxhash digest). Slot-first → L0 (slot plugin now **wal2json**, D6), set-based GROUP BY rollups (daily/hourly/minutely/products) streamed via COPY, per-(tenant,day) digests, checkpoint at L0. Passes `py_compile`; needs a live DB run (worker stopped, D4).
- **T4 (CDC worker) — core verified here; transport unverified:** pure coalescer (Z-set deltas, retraction, digest) passes 6/6 unit tests locally; `worker/flusher.py` (atomic batch + per-event idempotency H1), `worker/cdc_processor.py` (wal2json stream, D6), `worker/events.py`. Transport + atomic flush need a live-DB run.
- **T7 (query API) — code written, unverified:** FastAPI app (`app/`), four endpoints via the shared decomposer + RLS `set_config` (fix C4), sub-minute edges read from primary, freshness watermark, health. No JWT (D3). Compiles; needs a live run.
- **D6 recorded:** wal2json plugin → pg-primary now builds a custom image (`db/primary/Dockerfile`); cdc-worker + agg-api enabled in compose. `agg_layer_pub` (pgoutput) currently unused.
- **Next action (Docker host):** rebuild (`docker compose build pg-primary`), `up`, run the two pending T1 RLS checks, then end-to-end: `python3 -m worker.bootstrap ...` → start `cdc-worker` → `make seed-dev` was already loaded, apply a mutation (`scripts/apply_mutations.py`) → query the API and compare to direct SQL at a quiesced snapshot.

> No application code written, no dependencies installed, source documents unmodified.

---

## Milestone 1 — CP1: Exact vertical slice (1M/10 tenants)

**Goal:** `orders INSERT` → CDC → Z-set delta → atomic upsert into `agg_orders_daily` + checkpoint → `GET /orders/summary` returns an exact, fresh answer. Prove insert→visible→exact before adding any breadth.

**Correctness note:** CP1's first cut answers **aligned ranges only**; this is a *temporary milestone*, not the satisfied correctness requirement. Non-aligned exactness (AC-02) is required before CP1 counts as correctness-complete and before any aligned-only number is reported as "correct per PRD."

| Task | Description | Pri | Owner | Status | Notes |
|------|-------------|-----|-------|--------|-------|
| T1 | Compose + both schemas + RLS + CPU caps | P0 | DevOps | mostly verified — FORCE-RLS + isolation pending | Compose up, caps, both schemas, publication, RI FULL, `agg_reader`, fail-closed RLS all confirmed on Docker host. Pending: FORCE-RLS catalog check + positive tenant-isolation test. |
| T2 | Deterministic synthetic seeder + mutation stream | P0 | DE | verified (dev tier) | `make seed-dev` → 50,000 orders / 5 tenants / 149,912 items; order-total == Σ line items (0 mismatches). Mutation apply path not yet exercised (needs T4 worker). |
| T3 | Bootstrap slot-first + rollups + digests @L0 | P0 | DE | not started | fixes H2; depends T1,T2 |
| T4 | Worker: orders Z-set + per-event idempotency | P0 | DE | not started | fixes H1; depends T3 |
| T3 | Bootstrap slot-first + rollups + digests @L0 | P0 | DE | code written — unverified | `worker/bootstrap.py`, `worker/hashing.py`; fixes H2/H3; slot plugin wal2json; needs live-DB run |
| T4 | Worker: orders+items Z-set + per-event idempotency | P0 | DE | core verified; transport unverified | coalescer 6/6 tests; `flusher.py` (H1), `cdc_processor.py` (wal2json); needs live-DB run |
| T6 | Shared range decomposer + property tests | P0 | BE | **done + verified** | `app/services/range_decomposer.py`; 6/6 tests incl. 5k random ranges; fixes C1/H4 |
| T7 | 4 endpoints via decomposer + RLS txn | P0 | BE | code written — unverified | `app/`; fixes C4; sub-minute edges→primary; no JWT (D3); needs live run |
| T9 | Freshness watermark + idle heartbeat | P0 | BE/DE | not started | fixes H5; depends T4 |

**CP1 exit criteria:** insert a known order → API count increments by exactly 1 within seconds → equals direct `SELECT count(*)` for the range; aligned path exact; non-aligned exactness landing (T6+T7) before sign-off.

---

## Upcoming milestones (not started)

- **CP2 — Breadth + correctness (target ~H24):** T5 (order_items→products), T7 full (all four endpoints, non-aligned exact), T8 (isolation), T10 (correctness oracle). Exit: AC-01/02/04/05/06 + ISO-01..08 green.
- **CP3 — Differentiator + proofs (target ~H36):** T11 (digests + verify + targeted repair, B-2), T12 (benchmark scorecard), T13 (DB-safety pair), T14 (rate limit + /monitor). Exit: FR-08 repair <1% rows; scorecard with manifest.
- **CP4 — Scale + demo (target ~H44):** T16 (10M run + scaling ratio), T15 (`run_demo.sh` + README + DEMO_RUNBOOK), full demo dry-run. Exit: one-command demo proves all 7 rubric items.
- **Harden & buffer (H44–48):** re-run AC + isolation tripwire, backup demo video, freeze at H46.

---

## Status legend

`not started` · `in progress` · `blocked` · `done` · `verified` (passed its acceptance test with evidence).

## Live validation run #2 (2026-10-31, Docker host)

**Passed:** non-aligned summary exactness (643 / 1,501,707.39 == direct SQL — AC-02 ✓); rollups match source; digest row_count sum == 50,000; isolation 403/401; **Receipts detection** (inject → `/admin/verification` flags exactly tenant 1 / 2026-07-02, DRIFT_DETECTED, badge `verified:false`). FORCE RLS confirmed `t/t` on the four agg tables.

**Failed / fixed:**
- **wal2json rejected** even with `shared_preload_libraries`; server HINT names the real GUC `output_plugin_libraries`. → compose now sets `output_plugin_libraries=wal2json`. (CDC/freshness blocked until this takes effect — expected the stale `lag_ms`.)
- **Receipts repair 500** (verify read-path fine, write-path failed): the digest hash can exceed int8 and overflowed asyncpg's int encoder on INSERT. → `receipts.repair_bucket` now writes it as text + `::numeric`. (Worker flusher uses psycopg3, which adapts big ints to numeric automatically — no change needed there.)

**Note:** the summary rollups were populated by an earlier (pgoutput) bootstrap; the current wal2json bootstrap hasn't succeeded yet. Re-bootstrap after the GUC fix (stop the worker first so the stale slot can be dropped).

## Validation issues & fixes (2026-10-31, in progress)

- **API 500 — FIXED (code):** `SUM(bigint)` returns `numeric` → asyncpg `Decimal`, which broke `Decimal/float` arithmetic and JSON encoding. `app/services/query_service.py` now casts every aggregate `::bigint` and coerces to `int`. Pure tests still green. Needs a live re-check.
- **wal2json "may not be used as an output plugin" — FIX PREPARED (needs restart):** added `shared_preload_libraries=wal2json` to pg-primary's command in `docker-compose.yml`. Apply only after confirming the `.so` exports `_PG_output_plugin_init` (see runbook). Requires a pg-primary container recreate; the data volume (50k seeded orders) persists.
- **Slot/data state:** bootstrap failed at slot creation (before `seed_rollups`'s TRUNCATE), so no slot was created and the summary rollups were not truncated by that run. Re-running bootstrap after the fix WILL truncate+rebuild the (derived) summary rollups at a fresh L0 — the primary's 50k orders are never touched. Hold for approval before re-running.

## Live validation run #3 (2026-10-31) — near-complete

Fixes that worked: `output_plugin_libraries=pgoutput,test_decoding,wal2json` (the GUC the server named) unblocked wal2json; bootstrap succeeded at L0 (1,607 daily / 21,201 hourly / 49,048 minutely / 125,005 product rows / 600 digests); worker streams and the freshness heartbeat advances (`lag_ms` sub-second, FRESH).

**PASSED end-to-end:**
- wal2json slot probe + real bootstrap slot (`agg_slot | wal2json`).
- Non-aligned exactness: API `643 / 1,501,707.39` == direct SQL (AC-02).
- Rollups vs source match; digest row_count sum == 50,000.
- Tenant isolation: 403 (cross-tenant) and 401 (no token).
- **Live mutation propagation:** inserted order 99000001 (Aug-15) → LSN advanced 0/60B45C8→0/60E8350 → appeared in the Aug-15 rollup via CDC.
- **Receipts DETECTION:** inject → `/admin/verification` flags tenant 1 / 2026-07-02, badge `verified:false`.

**Receipts repair — PASSED (2026-10-31).** Root cause: repair INSERTed `bucket_digests` without removing the existing row → `UniqueViolationError (bucket_digests_pkey)`; fixed with an upsert (`ON CONFLICT (tenant_id, day_bucket) DO UPDATE`). Secondary blocker: Docker Desktop reused a cached `COPY app` layer; cleared with `docker compose build --no-cache`. Result: `/admin/rebuild` → `reconciled:true`, **rows_scanned 228 / full_rebuild_rows 50,016 = 0.46%**, 114.8 ms; post-repair summary back to **228**, `verified:true`, `corrupted_buckets:0`. The B-2 differentiator is proven end-to-end with a real, measured rows-read ratio.

**STATUS: full core + Receipts validated end-to-end.** All P0 gates (CDC, exactness incl. non-aligned, isolation, freshness, live propagation) and the P1 differentiator (detect + self-heal) pass on the 1M/5-tenant tier (50k orders in this volume).

**Also noted (follow-up, non-blocking):** random-sample verification can miss a specific drifted bucket (a `sample=50` sweep missed 2026-07-02); add a targeted `day_bucket` param to `/admin/verification` for deterministic checks. Small change, no schema impact.

**Worker reconnect hardening (done 2026-10-31):** `worker/cdc_processor.py` `run()` now wraps connect + `_stream()` in a backoff reconnect loop, so a dropped replication connection (e.g. primary restart) recovers gracefully instead of crashing and relying on `restart: on-failure`. The `PGRES_COPY_BOTH` / "database system is starting up" tracebacks seen during validation came from recreating pg-primary under the worker; this removes that noise. Per-event idempotency survives reconnects (checkpoint reloaded each connect). Compiles; needs the worker image rebuilt to take effect.

## Receipts (B-2 differentiator) — built 2026-10-31

Feature: per-`(tenant,day)` verified digests + targeted self-healing repair, with a "verified ✓ / ⚠️ drift" badge on responses. **Code written; pure cores verified here; DB paths unverified pending a Docker-host run.**
- `common/` (new, shared by both images): `hashing.py` (single canonical digest, now re-exported by `worker/hashing.py`), `rollups.py` (pure absolute recompute). Unit-tested: `tests/correctness/test_receipts_core.py` (4/4; digest value checks skip locally without xxhash, run on host).
- `app/services/receipts.py` — `verify_bucket`, `verify_sweep`, `repair_bucket` (recompute one bucket from a bounded source slice, atomic overwrite, reports rows_scanned vs full-table rows).
- `app/routers/admin.py` — `GET /api/v1/admin/verification` (digest health + optional sweep), `POST /api/v1/admin/rebuild` (TARGETED), guarded by `X-Admin-Key`.
- `app/services/verification.py` + badge added to `/summary` and `/freshness` responses.
- `scripts/inject_fault.py` — corrupts one summary bucket (rollup + digest) for the demo.
- Wiring: owner write-pool (`SUMMARY_DB_OWNER_URL`) added to `app/core/db.py`/`config.py`; `admin` router registered; Dockerfiles copy `common/`; compose `agg-api` gets owner DSN + `ADMIN_KEY`; `app` image adds xxhash.
- **No schema change** — reused existing `bucket_digests.is_corrupted`/`verified_at`.
- **Live validation still owed:** bootstrap/CDC/API end-to-end (wal2json restart + numeric-cast fix), then the Receipts demo (inject → `/admin/verification` flags it → `/admin/rebuild` heals, prints rows-read ratio → badge back to ✓).

## Per-tenant-database topology (reviewer model) — built 2026-10-31

Reviewer clarified: each tenant may have its own source database; Xponent is the central platform ingesting from all. **Key design point:** the central summary, API, RLS, decomposer, and Receipts are topology-agnostic (they only read the central `tenant_id`-keyed summary) — so only ingestion + source-reads changed.

- `common/registry.py` — `TENANT_SOURCES` env → `[{tenant_id,dsn,slot}]`; **unset = single shared source** (original behavior preserved). Tests `tests/correctness/test_registry.py` 4/4.
- **Worker** (`worker/cdc_processor.py`): one reconnecting stream **per source**, threaded; events tagged with the registry `tenant_id` (source DB = the tenant). Single-source path unchanged.
- **Bootstrap** (`worker/bootstrap.py`): registry-driven — truncate summary once, then per source create its slot + seed its tenant at L0 (per-slot checkpoint).
- **API** (`app/core/db.py`): one source pool per distinct DSN; `primary_for_tenant()` resolves a tenant to its source DB; edge-scans and Receipts verify/repair read the correct tenant DB. `verify_sweep`/`repair_bucket` take a `pool_for` resolver.
- **Provisioning/seed:** `scripts/provision_tenant_dbs.py` creates per-tenant DBs (schema + publication); `seed_dataset.py --assign-tenant` loads one tenant's data into its DB.
- **Compose:** commented `TENANT_SOURCES` on `cdc-worker` + `agg-api` (flip on to enable; default single-source).
- All modules compile; decomposer/coalescer/receipts-core/registry suites all green.

**Multi-source LIVE RUN — PASSED (2026-10-31):** provisioned `tenant_1`/`tenant_2` databases, seeded each (50k orders), enabled `TENANT_SOURCES`, bootstrapped both (slots `agg_slot_t1`/`agg_slot_t2`, each seeding 360 daily / 6,506 hourly / 46,772 minutely / 85,447 product / 120 digests from its own DB), worker streams both. API serves tenant 1 from `tenant_1` DB and tenant 2 from `tenant_2` DB, both `verified:true`, isolation intact. (Numbers identical across tenants only because both used seed=42 — a seeding artifact, not a bug; use a different `--seed` per tenant for visibly distinct demo data.)
- **Cosmetic fix applied in code, pending an API rebuild:** freshness read a hardcoded `agg_slot`; now resolves per tenant via `slot_for_tenant()` (`app/services/freshness.py`, `app/core/db.py`, routers). After `docker compose build --no-cache agg-api && up -d --force-recreate agg-api`, freshness shows each tenant's own slot LSN and `FRESH`.
- **Cleanup note:** the old single-source slot `agg_slot` on `primary_db` is now unused and retains WAL; drop with `SELECT pg_drop_replication_slot('agg_slot')` when convenient.

**Final multi-tenant-DB demo — PASSED (2026-10-31), distinct data + freshness fix:** re-seeded `tenant_2` (seed=7) → distinct per tenant; re-bootstrapped (t1 L0 `0/12A5D548`, t2 L0 `0/12A5D580`, different rollup cardinalities confirming different data); rebuilt API with the per-slot freshness fix. Live: tenant 1 = 38,307 orders / $87,794,270.89, tenant 2 = 38,458 / $87,247,857.41 — each served from its **own database**, its **own slot**, `FRESH` (sub-second lag, correct per-tenant LSN), `verified:true`, isolated. The per-tenant-database topology is fully demonstrated end-to-end.

## Roles: Organization vs Seller (Zomato model) — 2026-10-31

Clarified the two roles. Both are now first-class:
- **Seller = tenant:** the `/tenants/{id}/...` endpoints (own source DB, RLS-enforced) — a seller queries only its own data.
- **Organization = platform owner:** NEW cross-tenant aggregate — `GET /api/v1/org/overview` (`app/routers/org.py` + `query_service.org_overview`), admin-key guarded, reads via the owner pool (bypasses RLS) and groups across all tenants → platform totals + per-seller leaderboard + fleet verification health. This fills the gap where "the organization as a whole can ask a query" wasn't a feature.
- **Dashboard redesigned** (`frontend/index.html`, served at `/`) into two real roles: an **Organization** view (platform KPIs, seller leaderboard, click a seller to drill in, fleet verified/drift badge) and a **Seller** view (own KPIs, by-status, top products, trend, own freshness+verified badge, Receipts verify/repair). Not a mock — every panel calls the live API.
- Compiles; needs `agg-api` rebuilt to serve the new endpoints + page.

## Operational dashboard + NL query + live proof — built 2026-10-31

Food-delivery themed operational UI (charcoal + warm-orange, no blue), served by the API at `/`. NL query is the hero.
- **NL query** (`app/services/nlq.py`, `POST /api/v1/nlq`, admin-guarded): LLM (Anthropic, `NLQ_MODEL=claude-haiku-5-5`) produces a VALIDATED query *plan* only — never raw SQL; deterministic keyword parser is the offline fallback. Runs through the existing rollup query path; returns interpreted plan + representative SQL + result + exec ms + source + freshness. `ANTHROPIC_API_KEY` passed through compose (empty ⇒ rule parser).
- **Live activity feed**: worker now records one `cdc_activity` row per applied event (coalescer capture + flusher insert, in the atomic txn); table ensured at worker startup; `GET /api/v1/org/activity`. Needs **worker rebuild**.
- **Live pipeline status** (`GET /api/v1/status/pipeline`): real per-source slot (active/LSN) + per-slot checkpoint lag; no fabricated fields.
- **Demo actions** (`/api/v1/demo/order` create/cancel/update, admin-guarded): write test orders into the tenant's OWN source DB so changes flow through CDC → rollup; returns order_id for feed correlation.
- **Isolation + reconciliation** reuse `/org/overview` and `/admin/verification`+`/admin/rebuild`.
- Added `httpx` to the API image; new routers wired in `app/main.py`. All backend compiles; coalescer/decomposer/receipts/registry suites green. **Not yet run live** (needs `agg-api`+`cdc-worker` rebuilt). No re-bootstrap needed (activity table auto-ensured).

## NLQ: local Ollama/Gemma provider — built 2026-10-31

- `NLQ_PROVIDER=ollama|anthropic|rules` (default **ollama**). `app/services/nlq.py` refactored: shared `_system_prompt` + `_plan_from_json` validation; `parse_ollama` (Ollama `/api/chat`, `format:"json"`, no key for local), `parse_anthropic` preserved, deterministic parser as the always-on fallback. Provider + fallback reason logged (no secrets) and returned (`engine`, `provider_requested`, `fallback`).
- **Server-enforced tenant scope:** `/nlq` accepts optional `tenant_id`; when set it pins the plan to that tenant (and downgrades org→seller), so neither the model nor the question text can pick another tenant. UI sends it when Scope = a restaurant; response shows `scope_locked`.
- Config: `OLLAMA_BASE_URL` (default `http://host.docker.internal:11434`), `OLLAMA_MODEL` (default `gemma3`, configurable), separate `OLLAMA_API_KEY` (Cloud only — never the Anthropic key), `NLQ_TIMEOUT_S`. Compose `agg-api` gets these + `extra_hosts: host.docker.internal:host-gateway`.
- **Verified offline (here):** deterministic parser, plan validation (rejects bad metric / accepts good), engine labelling, SQL gen; `py_compile` clean; rollup path + isolation untouched. **NOT verified:** the live Ollama/Gemma round-trip (no Docker/Ollama in build env) — user must run it and confirm `engine:"ollama"` vs `"rules"` fallback.

## Stitch seller dashboard + seller CRUD + NLQ fixes — 2026-11-01

Rebuilt the primary UI to the Stitch reference (light lavender canvas, charcoal 240px sidebar, red #E5383B accent, Inter, 16px glass cards), organized into files: `frontend/index.html`, `frontend/static/app.css`, `frontend/static/app.js`, `frontend/static/logo.png`; served via FastAPI `/` + `StaticFiles` at `/static`.
- **NLQ date fix:** model now returns a `range` enum; server computes dates (`range_dates`). Fixes the "last 30 days → forward/0 results" bug (verified offline: last_30_days → Oct 2–Nov 1).
- **Currency:** `CURRENCY` config (default **INR**) returned by summary/org; UI formats with `Intl.NumberFormat('en-IN')`. No per-value relabeling.
- **Seller CRUD (tenant-safe):** `POST/PATCH/DELETE /api/v1/tenants/{id}/orders[...]` + `GET /orders`, enforced by `get_tenant_context` (seller can only touch its own tenant; writes go to that tenant's source DB → CDC → rollup).
- **Seller NLQ auth:** `/nlq` accepts a dev-mode seller (`X-Dev-Tenant-ID`) and server-pins the tenant (no admin key; model/text can't pick another tenant). Operator admin path preserved.
- Diagnostics (SQL, provider/engine, Receipts verify/repair) moved to a collapsed technical panel with its own admin-key field — out of the seller flow.
- Compiles; HTML/JS validate; coalescer/decomposer/receipts/registry suites green. **Not run live here** (no Docker/Ollama in build env) — needs `agg-api` rebuilt; user verifies the demo flow + Gemma.

## Change log

- 2026-10-31 — Review + roadmap + gap analysis + decisions documented. All tasks `not started`. Awaiting D1–D5 approval and kickoff for O1–O5.
- 2026-10-31 — No organizer dataset confirmed; added `SYNTHETIC_DATA_PLAN.md` (team-owned deterministic generator). O1–O5 reclassified N/A — resolved by us. D2/D4/D5 now decidable with our own oracle/generator; only D1 and D3 still require team approval. All implementation tasks remain `not started`.
- 2026-10-31 — Team **approved all decisions D1–D5** (minute-bucket edges, gross-default semantics, drop JWT, seed-quiesced-then-stream, 1M demo / 10M benchmark). No open decisions remain; implementation unblocked.
- 2026-10-31 — Began implementation. **T1** (docker-compose + primary/summary SQL incl. minute-grain bucket, FORCE RLS + `agg_reader`, publication) and **T2** (deterministic seeder + mutation stream) written. Python passes `py_compile`.
- 2026-10-31 — **T1/T2 validated on a Docker host:** compose up, caps enforced, both schemas (incl. `agg_orders_minutely`), publication, RI FULL, `agg_reader` non-superuser, seed-dev 50k/5-tenant/149,912-item load, 0 order-total mismatches, fail-closed RLS (0 rows without tenant). T2 verified at dev tier. **Pending:** FORCE-RLS catalog check (prior query used a non-existent `pg_tables.forcerowsecurity` column — corrected to `pg_class`) and the positive tenant-isolation test.
- 2026-10-31 — **T6 built and verified** (range decomposer; 6/6 tests incl. 5k random-range coverage, run locally without deps). **T3 written** (bootstrap + canonical digest hash; `py_compile` clean) — needs a live-DB run to verify.
- 2026-10-31 — **T4 + T7 built.** T4 coalescer core verified locally (6/6: insert/cancel-retraction/delete/bucket-move/items/lsn); transport (wal2json, D6) + atomic flush written, unverified. T7 full FastAPI API written (4 endpoints via shared decomposer + RLS, edges→primary, freshness, health; no JWT). All modules `py_compile` clean; both pure suites green. Decision **D6** (wal2json) recorded; pg-primary builds a custom image; cdc-worker + agg-api enabled in compose. Remaining: T5 folded into T4 (items handled), T8–T17 `not started`. Everything DB-touching is unverified pending a Docker-host run.
