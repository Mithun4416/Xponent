# Xponent — Judge Demo Runbook

A step-by-step script for presenting Xponent live, **feature by feature**, mapped to
the judging rubric: *fast · fresh · DB-safe · exact · isolated · scales · self-healing*.

Target length: **~8 minutes**. Everything below is real — no mocked numbers.

---

## 0. Before the judges arrive (bring-up)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r scripts/requirements.txt
./scripts/quickstart.sh
```

`quickstart.sh` starts the two databases + Redis, provisions the per-tenant source
DBs (`tenant_1`, `tenant_2`), seeds synthetic orders, bootstraps the rollups + one
CDC slot per tenant, and starts the worker + API. When it prints the URL:

1. Open **http://localhost:8000** — you land on the **sign-in screen**.
2. Sign in as **Organization** with the operator key `xpo-admin-dev` (pre-filled).
   You land on **Organization overview** with KPIs and a leaderboard filled in.
3. To demo the restaurant role: **Sign out** (sidebar) → **Restaurant** tab → pick a
   restaurant → passkey `restaurant<N>` (e.g. `restaurant1` for Restaurant #1).

**Demo sign-in keys**

| Role | Credential |
|---|---|
| Organization (platform owner) | operator key `xpo-admin-dev` |
| Restaurant #1 | passkey `restaurant1` |
| Restaurant #2 | passkey `restaurant2` |
| Restaurant #N | passkey `restaurant<N>` |

> The sign-in is a demo gate for *routing* each role to its own dashboard. The real
> tenant isolation is enforced **server-side** by PostgreSQL Row-Level Security — a
> restaurant can only ever read its own rows, whatever the client sends.

> Keep a terminal open for the optional live-load step (Stage 6).
> Bigger dataset if asked: `ROWS=1000000 ./scripts/quickstart.sh`.

---

## The pitch, in one sentence (say this first)

> "Every restaurant has its **own** database. Xponent captures their changes with CDC,
> keeps **incremental rollups** in a separate summary store, and answers every dashboard
> question from those rollups — so query cost depends on the **number of time-buckets,
> not the number of rows**. That's how we stay fast, fresh, and never touch the source
> DB under load."

---

## Stage 1 — Scale & the two-database architecture  *(scales, DB-safe)*

1. On **Organization → Overview**, point at **Restaurants** and **Total orders** KPIs.
   "This is the whole platform, read live from the summary store."
2. Go to **Organization → Databases**.
   - Click a **source** DB (`tenant_1`): open the `orders` table. "This is a restaurant's
     *own* OLTP database — raw orders. Dashboards never read this under load."
   - Click the **summary** store: open `agg_orders_daily` and `bucket_digests`.
     "These are the pre-computed rollups the dashboard actually reads — a handful of
     rows per day, per tenant, instead of millions of raw orders."
   - *(The wide tables scroll horizontally inside their panel — scroll to show every column.)*

**Rubric hit:** two physically separate databases; reads hit rollups, not OLTP.

---

## Stage 2 — Speed, made undeniable: Recompute vs Xponent  *(fast — the headline)*

This is the money shot. It proves the value proposition with a measured number.

1. Still in **Organization → Overview**, scroll to **"Recompute vs Xponent — why
   pre-aggregation wins."**
2. Pick a range (top-right), e.g. **All time**, then click **Run comparison**.
3. Two boxes appear:
   - **Recompute from scratch** — scans every raw order across all source DBs (what a
     dashboard *without* rollups must do). Shows the time + rows scanned.
   - **Xponent (rollups)** — reads only pre-aggregated rollup rows. Shows the time.
4. Read the banner: **"Identical result · Xponent was N× faster."**
   "Same numbers — only the time differs. That gap *grows* with data volume."
5. Repeat on a **Restaurant** dashboard (Stage 4) to show it holds per-tenant too.

**Rubric hit:** fast, and *why* it's fast — provably, not by assertion.

---

## Stage 3 — Freshness: watch a change flow through CDC  *(fresh)*

1. Go to **Organization → Live activity**.
2. Click **Simulate orders**. This writes test orders into the restaurants' own source
   DBs; each one flows **source → WAL → CDC worker → rollup**, and appears in the feed.
3. Within a second or two the **feed fills** with INSERT events (restaurant, table,
   order id, LSN, applied-at). "These are real change-data-capture events — the pipeline
   that keeps the numbers fresh."
4. Jump back to **Overview** (or re-run the comparison): the totals have moved.

> Manual alternative: switch to a **Restaurant → Orders** and create/cancel an order —
> same pipeline, one order at a time.

**Rubric hit:** new writes are visible in the rollups within the freshness window.

---

## Stage 4 — Tenant isolation & role separation  *(isolated)*

1. As the owner, click any **leaderboard row** → you drop into that restaurant's
   dashboard. "The platform owner can see every tenant — that's their job."
2. Now show the *restaurant's* own experience: **Sign out** → sign in as a
   **Restaurant** (e.g. #2, passkey `restaurant2`).
3. Point at the sidebar: there's **no Organization view, no restaurant switcher, no
   operator key** — this account is locked to its own data. "A restaurant signs in and
   sees only itself; it can't even navigate to another tenant."
4. "And this isn't just the screen hiding things — it's enforced on the **server** by
   path identity + PostgreSQL Row-Level Security. A restaurant physically cannot read
   another's rows, whatever the client sends."
5. (Optional, Diagnostics) open the gear → Diagnostics to show the scope-locked query.

**Rubric hit:** server-enforced isolation; zero cross-tenant leakage.

---

## Stage 5 — Ask anything (NL query) & exactness  *(exact)*

1. On a **Restaurant** dashboard, use **Ask about your business**. Try:
   - "My sales this week"
   - "Order status breakdown last 30 days"
   - "Sales in July" vs "Sales in September" — **different months, different numbers**.
2. Note the **instant** badge — the deterministic rules parser answers common questions
   with no LLM round-trip, and every answer carries a **freshness** badge and the time.
3. On **Organization**, try "Compare restaurants last 6 months" and "Which restaurant
   sold most this month" — the latter names the **top** restaurant.

**Rubric hit:** exact answers for arbitrary date ranges, instant, with freshness shown.

---

## Stage 6 — (Optional) Live load  *(DB-safe under pressure)*

In the terminal:

```bash
make load            # streams ~500 orders/sec for 60s into the source DBs
```

Watch the dashboard: numbers climb, **freshness stays green**, the API stays fast.
"The source DBs take the write load; the dashboard keeps reading rollups — they don't
compete."

**Rubric hit:** dashboards stay fast and fresh while the OLTP side is under load.

---

## Stage 7 — Data integrity & self-healing  *(correctness, differentiator)*

1. Open **Diagnostics** (gear, top-right) → **Verify** a day. Shows digest health.
2. (If demoing repair) corrupt one bucket with `scripts/inject_fault.py`, re-verify to
   see **Drift detected**, then **Repair day** — it re-reads only that bucket's rows and
   reconciles. "We can detect and repair drift reading a tiny fraction of the data."

**Rubric hit:** verifiable correctness + targeted self-healing.

---

## Closing line

> "Fast because we read buckets, not rows. Fresh because CDC streams every change.
> Safe because dashboards never touch the OLTP databases. Isolated by Row-Level
> Security. Exact to the cent. And it scales — the same design runs 10M orders across
> 100 tenants."

---

## Quick reference

| Thing | Where |
|---|---|
| URL | http://localhost:8000 |
| Sign in — Organization | operator key `xpo-admin-dev` |
| Sign in — Restaurant #N | passkey `restaurant<N>` |
| Bring-up | `./scripts/quickstart.sh` |
| Bigger seed | `ROWS=1000000 ./scripts/quickstart.sh` |
| Live load | `make load` |
| Speed proof | Overview/Dashboard → **Run comparison** |
| Generate activity | Live activity → **Simulate orders** |
| Scale benchmark | `make seed-bench` (10M / 100 tenants) |

### If something looks wrong on stage
- **Leaderboard empty / 403** → operator key not entered in the sidebar.
- **Activity feed empty** → click **Simulate orders** (or create an order in a Restaurant).
- **Comparison shows "results differ"** → that's an honest drift flag; run **Verify/Repair**
  (Stage 7) rather than trusting the numbers. It should normally say *identical*.
