# Xponent — 48-Hour Implementation Plan

**Status:** Approved roadmap. Daily execution guide. No application code written yet; see `PROGRESS.md` for live state.
**Date:** 2026-10-31
**Companion docs:** `TECHNICAL_GAP_ANALYSIS.md` (the bugs to fix first), `OPEN_DECISIONS.md` (decisions awaiting approval), `PROGRESS.md` (live status).
**Confirmed decisions:** Scope = rock-solid core + ONE differentiator (digest verification + targeted repair, B-2). Primary DB = PostgreSQL only (Mongo adapter deferred). Stack = Python end-to-end (FastAPI + asyncpg API, psycopg3 logical-replication worker).
**Dataset:** No organizer-provided dataset — the team generates its own deterministic synthetic data; our base tables + direct SQL are the authoritative oracle. See `SYNTHETIC_DATA_PLAN.md`.

> **Source-document precedence (do not reinterpret):** PRD > SRS > Architecture > DB/API Spec > Test Plan. The five source documents are authoritative and must not be modified. This plan does not change the approved architecture.

---

## Context — why this plan exists

Xponent is a multi-tenant live-aggregation layer for the challenge *"Fast Multi-Tenant Data Aggregation: live rollups at scale without database overload."* The five specs are strong on theory and ship substantial reference code. The dominant risk is **over-scope**, plus concrete correctness bugs in the reference code that would fail the spec's own acceptance tests. This plan keeps the architecture, fixes those defects first, cuts to a correct core + one differentiator, and reserves real integration/debug time.

---

## 1. Overall strategy

Build a **thin, exact vertical slice first**, gate on it, then add breadth, then the one differentiator, then scale + demo. Correctness is the highest-weighted rubric item, so no feature is "done" until it matches a direct SQL oracle at a quiesced snapshot. Keep the approved architecture unchanged.

**MVP definition:** CDC → incremental Z-set rollups → four exact tenant dashboard queries + tenant isolation + freshness watermark, reproducible via `docker compose up`, benchmarked against the PRD "Good"/"Great" targets.

**Critical path:** T1 → T3 → T4 → T7 → T10 (infra → bootstrap → worker → endpoints → correctness oracle).

**Demonstrate first:** insert an order → see the number change in the API within seconds → prove it equals a direct SQL count. That single round-trip proves the whole concept.

**Postpone (P2):** DRR fair scheduling, config-driven metrics, DuckDB vectorized oracle, Mongo adapter, item-aware product retraction.

---

## 2. Feature prioritization

**P0 — must work and be exact:**
- Primary PG schema + seed; `wal_level=logical`, `REPLICA IDENTITY FULL`, publication.
- Summary PG schema (hourly, daily, products_daily, minutely, digests, checkpoint) + RLS done correctly.
- Bootstrap seed of all rollups + digests at snapshot LSN L0.
- CDC worker: orders **and** order_items → Z-set deltas → coalesced atomic upsert + LSN checkpoint with **per-event** idempotency.
- Four endpoints (FR-1 summary, FR-2 trends, FR-3 top-N, FR-4 by-status) via one shared range decomposer, **exact for aligned and non-aligned ranges**.
- Tenant isolation (path + dev header + RLS), reject missing/invalid tenant.
- Freshness watermark on every response, with idle heartbeat.
- Correctness oracle harness (quiesce → compare vs direct SQL).
- One-command `docker compose up` + README.

**P1 — the differentiator + the proofs judges reward:**
- `bucket_digests` populated + incrementally maintained.
- `/admin/verification` + `/admin/rebuild` targeted repair (B-2 flagship).
- Benchmark harness → PRD scorecard (latency per tenant class, freshness, DB-safety pair, 1M→10M ratio).
- Minimal per-tenant rate limit (B-3) + simple `/monitor` HTML (B-5).

**P2 — only if core is bulletproof:** DRR (FR-019), config metrics (FR-020), DuckDB oracle, Mongo adapter, item-aware product retraction.

Justification: P0 is the entire scored rubric; P1 is the one committed differentiator plus the evidence that turns claims into demonstrated results; each P2 adds risk out of proportion to its marginal score.

---

## 3. Technical implementation sequence (component → contract)

Each: what / in→out / deps / done-when.

1. **Repo + Docker Compose + schemas** — bring up pg-primary, pg-summary, redis, api, worker with CPU caps. Done: all healthy, `/health` 200, caps verified via `docker inspect`.
2. **Dataset seeder** — deterministic COPY loader (1M/10M, Pareto skew). Done: counts assert; reproducible md5 per tenant.
3. **Bootstrap** — slot-first; seed rollups + digests at L0. Done: digest(summary) == digest(oracle) for all (tenant, day).
4. **CDC worker (orders)** — pgoutput → Z-set deltas → atomic batch + per-event idempotency. Done: insert/update/cancel reflected < 5 s; AC-07/08/09/11 pass.
5. **CDC worker (order_items)** — product qty/revenue deltas. Done: AC-04 pass.
6. **Shared range decomposer** — one pure module: interior days + boundary hours + minute/edge residuals. Done: property test vs brute-force oracle over 1000 random ranges, **including non-aligned**.
7. **Four query endpoints** — all via the decomposer + RLS transaction. Done: FC-02..06 + AC-01..06 pass.
8. **Isolation layer** — middleware + `agg_reader` role + `FORCE ROW LEVEL SECURITY`. Done: ISO-01..08 pass, 0 bytes leaked.
9. **Freshness + heartbeat** — watermark decorator + idle `as_of` update. Done: FC-09; external lag ≈ self-reported `lag_ms`.
10. **Correctness oracle harness** — quiesce-then-compare. Done: AC suite runs green at the current tier.
11. **Digest verify + targeted repair** — `/admin/verification`, `/admin/rebuild`. Done: FR-08 — corrupt 1 bucket, detect, repair reading < 1% rows, reconcile.
12. **Benchmark harness + scorecard** — HTTP percentile tool + pgbench pair. Done: prints PRD Good/Great scorecard with a manifest.
13. **Demo runbook + `run_demo.sh`** — scripted end-to-end. Done: one command proves all 7 rubric items, fails loudly on mismatch.

---

## 4. End-to-end vertical slice (build FIRST — H0–12, at 1M/10 tenants)

`orders INSERT` → pgoutput WAL → worker computes `+1` delta → atomic upsert into `agg_orders_daily` + checkpoint → `GET /tenants/{id}/orders/summary` reads daily buckets → returns count/sum + freshness.

> **Milestone scope vs correctness requirement (reconciled):** the first cut of the slice (**milestone CP1**) answers **aligned ranges only** (whole-day / whole-hour boundaries) and deliberately omits sub-hour edge handling. **This is a temporary milestone, not a satisfied correctness requirement.** The binding acceptance criterion (§8, AC-02) requires **exact results for arbitrary non-aligned `from`/`to`** via the shared range decomposer. Aligned-only must be clearly labeled as such wherever it is demoed or reported, and it does **not** count toward the correctness Definition of Done until non-aligned exactness lands in CP2 (T6 + T7). Do not present aligned-only numbers as "correct per PRD."

Validation gate for CP1: insert a known order, poll the endpoint, assert the count increments by exactly 1 within seconds and equals a direct `SELECT count(*)` for that **aligned** range. Only then add order_items/products, the decomposer + non-aligned edges, by-status, trends, isolation hardening, and the differentiator.

---

## 5. Repository structure

```
xponent/
├── docker-compose.yml            # 5 services, CPU/mem caps
├── Makefile / run_demo.sh        # one-command bring-up + demo
├── .env.example
├── README.md                     # judge-facing startup + endpoints
├── db/
│   ├── primary/            001_schema.sql, 002_indexes.sql, 003_publication.sql
│   └── summary/            001_schema.sql, 002_rls.sql, 003_indexes.sql
├── app/                          # FastAPI API
│   ├── main.py  core/{database,security,config}.py
│   ├── services/{range_decomposer,query_service,freshness}.py
│   └── routers/{tenants,admin,monitor}.py
├── worker/                       # CDC consumer
│   ├── cdc_processor.py  coalescer.py  zset.py  digests.py
│   ├── bootstrap.py  verifier.py  repair.py
├── scripts/  seed_dataset.py  gen_mutations.py  apply_mutations.py
├── tests/  functional/ correctness/ isolation/ perf/ recovery/ conftest.py
├── benchmarks/  benchmark_harness.py  oltp_mix.sql  dashboard_mix.js
├── frontend/  monitor.html
└── docs/  IMPLEMENTATION_PLAN.md TECHNICAL_GAP_ANALYSIS.md OPEN_DECISIONS.md SYNTHETIC_DATA_PLAN.md DEMO_RUNBOOK.md PROGRESS.md
```
Matches the approved architecture (worker / summary / API / tests / bench / compose).

---

## 6. Task breakdown (role-based ownership; no team members invented)

Roles: **BE** backend/API, **DE** data/CDC/worker, **QA** test/bench, **DevOps**, **FE** frontend/demo.

| ID | Task | Pri | Owner | Est | Deps | Acceptance |
|----|------|-----|-------|-----|------|-----------|
| T1 | Compose + both schemas + RLS + CPU caps | P0 | DevOps | 3h | — | `/health` 200; caps in `docker inspect` |
| T2 | Deterministic seeder (1M/10M, skew) | P0 | DE | 3h | T1 | counts assert; reproducible md5 |
| T3 | Bootstrap slot-first + rollups + digests @L0 | P0 | DE | 3h | T1,T2 | digest(summary)==oracle |
| T4 | Worker: orders Z-set + per-event idempotency (H1) | P0 | DE | 5h | T3 | AC-07/08/09/11 pass |
| T5 | Worker: order_items → products (C2) | P0 | DE | 3h | T4 | AC-04 pass |
| T6 | Shared range decomposer + property tests (C1,H4) | P0 | BE | 4h | — | 1000 random **non-aligned** ranges == brute force |
| T7 | 4 endpoints via decomposer + RLS txn (C4) | P0 | BE | 5h | T3,T6 | FC-02..06, AC-01..06 (incl. non-aligned) |
| T8 | Isolation: middleware + agg_reader + FORCE RLS | P0 | BE | 2h | T7 | ISO-01..08, 0 leak |
| T9 | Freshness watermark + idle heartbeat (H5) | P0 | BE/DE | 2h | T4 | FC-09; lag≈external |
| T10 | Correctness oracle harness (quiesce+compare) | P0 | QA | 4h | T7 | AC suite green |
| T11 | Digest maintain + verify + targeted repair (B-2) | P1 | DE | 5h | T3,T4 | FR-08 <1% rows, reconciles |
| T12 | Benchmark harness + PRD scorecard | P1 | QA | 4h | T7 | scorecard w/ manifest |
| T13 | DB-safety pair (pgbench baseline vs load) | P1 | QA | 3h | T1,T7 | overhead % computed, caps verified |
| T14 | Per-tenant rate limit + `/monitor` HTML (B-3,B-5) | P1 | BE/FE | 3h | T7 | 429 on flood; live metrics |
| T15 | `run_demo.sh` + README + DEMO_RUNBOOK | P0 | DevOps/FE | 3h | most | one-command PASS/FAIL scorecard |
| T16 | 10M run from pre-seeded volume + scaling ratio | P1 | QA/DevOps | 3h | T12 | 1M→10M ratio <5x/<2x |
| T17 | (stretch) DRR / config metrics / Mongo / retraction | P2 | DE | — | core done | per-item AC |

---

## 7. Parallel execution & integration checkpoints

- **Parallel from the start:** T6 (decomposer — pure function) ‖ T1/T2 (infra/seed) ‖ T10 test scaffolding (mocked API).
- **Sequential critical path:** T1→T3→T4→T7→T10.
- **Start-with-mocks:** BE builds T7 against a hand-seeded summary DB before T4 lands; QA writes AC assertions against the OpenAPI contract first.
- **Checkpoints:** CP1 (~H12) vertical slice exact at 1M (aligned milestone, then non-aligned); CP2 (~H24) all four endpoints exact for non-aligned ranges + isolation passing; CP3 (~H36) differentiator + benchmarks; CP4 (~H44) 10M run + demo dry-run; freeze at H46.
- **Avoid collisions:** worker (DE) and API (BE) own disjoint trees; the **decomposer is the one shared module** — lock its interface early (T6).

---

## 8. 48-hour timeline

- **H0–2 — Kickoff & de-risk:** stand up the repo + compose (T1) and build/seed the **team-owned synthetic generator** (T2) early, since there is no organizer dataset — our data + direct SQL is the oracle (`SYNTHETIC_DATA_PLAN.md`). Confirm dataset semantics already settled in `OPEN_DECISIONS.md` (shared-table topology, order_items present, gross default, PostgreSQL). If organizers later publish a reference query/dataset, re-align O1/O3 then.
- **H2–12 — Vertical slice (CP1):** seed 1M (T2), bootstrap (T3), worker orders (T4), decomposer (T6), summary endpoint (T7 partial). Prove insert→visible→exact. Aligned-only is the CP1 milestone; **non-aligned exactness is required before CP1 is considered correctness-complete.** Gate.
- **H12–24 — Breadth + correctness (CP2):** order_items/products (T5), all four endpoints via decomposer with **non-aligned exactness** (T7), isolation (T8), freshness + heartbeat (T9), full AC oracle (T10).
- **H24–36 — Differentiator + proofs (CP3):** digests + verify + targeted repair (T11), benchmark harness + scorecard (T12), DB-safety pair (T13), rate limit + monitor (T14).
- **H36–44 — Scale + demo (CP4):** pre-seed + 10M run + scaling ratio (T16), `run_demo.sh` + README (T15), full demo dry-run on the 10M volume.
- **H44–48 — Harden & buffer:** re-run AC + isolation as tripwire, record a backup demo video, freeze at H46, rehearse. **~4h reserved for integration/debug.**

---

## 9. Testing & verification checkpoints

- **Correctness (highest weight):** AC-01/02 counts aligned **and** non-aligned == oracle at a quiesced snapshot; AC-04/05 Top-N incl. post-cancellation promotion; AC-08 placed→cancelled retraction (hourly + daily); AC-12 digest reconciliation as the after-every-test tripwire.
- **Isolation (pass/fail; any leak fails):** ISO-01 cross-tenant 403 before DB conn; ISO-04 RLS blocks an unscoped buggy query; ISO-08 fuzz sweep 0 bytes.
- **Performance:** per-endpoint p50/p95/p99 at 10M per tenant class (whale/median/small), coordinated-omission-aware tool. Target <1 s / <200 ms.
- **Freshness:** inject insert + cancel, poll 500 ms, measure commit→visible; assert self-reported `lag_ms` tracks external.
- **DB-safety:** pgbench baseline (worker+api stopped) vs load (CDC + 20 dashboard clients + verifier); `(L_load−L_base)/L_base` p95; **verify CPU caps actually enforced** or the number is meaningless.
- **Scaling:** identical suite 1M vs 10M, ratio <5x/<2x, 5M midpoint to catch non-linear inflection.
- **Recovery:** kill worker mid-stream → resume from checkpoint, reconcile; WAL-retention bound test.

> Discipline (from the Test Plan): **no number is a result until a manifested, repeatable run produces it.** No fabricated results.

---

## 10. Demo & judging strategy

Scripted `run_demo.sh`, one command, live PASS/FAIL scorecard mapped to the PRD columns, fails loudly on any mismatch:
1. `compose up` + base-table counts & distinct tenants (**scale**).
2. Fire 4 endpoints for a whale tenant, print p95 live (**speed**).
3. Insert + `placed→cancelled`, poll, print measured lag vs self-reported `lag_ms` (**freshness + honest watermark**).
4. Comparator: 100 random **non-aligned** ranges vs oracle on a quiesced snapshot → "100% exact" or first mismatch (**correctness**).
5. Cross-tenant request → 403 (**isolation**).
6. Corrupt one (tenant, day) bucket → `/admin/verification` detects → `/admin/rebuild` repairs, print rows-read vs full rebuild (**self-healing, B-2 differentiator**).
7. pgbench baseline-vs-loaded → overhead % (**DB safety**).

---

## 11. Risk & contingency register

| Risk | Prob | Impact | Warning sign | Prevention | Recovery / fallback |
|------|------|--------|--------------|------------|---------------------|
| Non-aligned ranges wrong (C1) | High | High | AC-02 fails | Minute-bucket + shared decomposer, property tests early | Restrict demo to aligned ranges; disclose honestly |
| Product retraction on cancel (C3) | High | Med | AC-05 fails | Define Top-N gross for MVP | Carry net columns, switch post-kickoff |
| RLS returns empty (C4) | Med | High | endpoints empty | Txn-wrap SET LOCAL + FORCE RLS + agg_reader | App-layer WHERE as primary, RLS as backstop |
| Over-scope, nothing finished | Med | High | CP2 slips | Hard gate at vertical slice; P2 frozen | Ship P0 only; it is the full rubric |
| 10M seed too slow for judges | Med | Med | seed >20 min | Pre-seeded volume + 1M default demo | Demo at 1M, report 10M from recording |
| Docker CPU caps not enforced | Med | Med | DB-safety number implausible | CPU-burn verify before trusting | Note backend in manifest; mark indicative |
| Exactly-once replay double-count (H1) | Med | High | AC-09 fails | Per-event LSN dedupe | Full rebuild (B-2) resets state |
| WAL fills disk on worker stall | Low | High | disk alarms | `max_slot_wal_keep_size=2GB` + heartbeat | Slot invalidation + rebuild |
| Synthetic data not representative (vacuous tests) | Med | High | AC-02/AC-05 pass but prove little | `--sub-hour-density` + modest catalog + multi-item orders; review distributions early | Tune generator knobs; regenerate |
| Organizers later publish a reference query/schema | Low | Med | new requirements mid-event | Keep gross+net columns; shared-table + order_items already match common shapes | Re-align O1/O3 to theirs; our oracle swaps queries |

---

## 12. Definition of Done

On the pinned hardware, with a manifested run:
- `docker compose up` + README brings the system up from a clean clone; `/health` green.
- All four must-have queries return **exact** matches vs direct SQL at a quiesced snapshot, for **aligned and non-aligned** ranges (AC-01/02/04/06 at 100%). **Aligned-only does not satisfy this.**
- A live insert and a `placed→cancelled` update are reflected within the freshness window, with the watermark tracking externally-measured lag.
- Cross-tenant access is impossible under every tested parameter/token combination — **zero** bytes leaked.
- p95 query latency at 10M/100 tenants ≥ "Good" (<1 s), ideally "Great" (<200 ms); 1M→10M ratio <5x (ideally <2x).
- OLTP overhead under dashboard + CDC load ≥ "Good" (<25%), with CPU-cap enforcement verified.
- Differentiator: a corrupted bucket is detected and repaired reading < 1% of base rows, then reconciles.
- `run_demo.sh` proves all seven rubric items end-to-end and fails loudly on any mismatch.
- A results manifest accompanies every reported number; no fabricated results.
