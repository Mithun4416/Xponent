"""Natural-language → query PLAN → existing rollup query (safe, no arbitrary SQL).

The LLM (Anthropic) translates a plain-English food-delivery question into a
STRICT, validated JSON plan: {scope, tenant_id, metric, from, to, granularity,
limit}. It never emits SQL. We validate the plan, run it through the EXISTING
rollup query path (the summary store, not the source), and show the actual SQL
that the rollup read represents. If no API key or the call fails, a deterministic
keyword parser produces the same plan shape — so the box always works offline.
"""
from __future__ import annotations

import calendar
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core import config

_METRICS = {"summary", "trends", "by_status", "top_products", "org_compare"}
_RANGES = {"today", "yesterday", "last_7_days", "last_30_days", "this_month", "all_time"}
# Earliest seeded data; used for the 'all_time' range so demos always show data.
_DATA_EPOCH = datetime(2026, 7, 1, tzinfo=timezone.utc)


def range_dates(label: str, now: datetime) -> tuple[datetime, datetime]:
    """Server-side date math for a named range (the model never computes dates)."""
    fd = lambda x: x.replace(hour=0, minute=0, second=0, microsecond=0)
    if label == "today":
        return fd(now), now
    if label == "yesterday":
        return fd(now) - timedelta(days=1), fd(now)
    if label == "last_7_days":
        return now - timedelta(days=7), now
    if label == "this_month":
        return fd(now).replace(day=1), now
    if label == "all_time":
        return _DATA_EPOCH, now + timedelta(days=1)
    return now - timedelta(days=30), now   # last_30_days (default)


@dataclass
class QueryPlan:
    scope: str            # "seller" | "org"
    metric: str           # one of _METRICS
    tenant_id: int | None
    frm: datetime
    to: datetime
    granularity: str      # "day" | "hour"
    limit: int
    interpreted: str      # human-readable echo
    engine: str           # "llm" | "rules"


# ----------------------------- deterministic fallback -----------------------------
# Explicit calendar dates the user typed (e.g. "2026-07-01", "2026/07/01",
# "from 2026-07-01 to 2026-07-07"). These MUST win over the vague keyword ranges,
# otherwise every dated question silently collapses to the last-30-days default
# and returns identical numbers regardless of the date.
_ISO_DATE = re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b")
_ISO_MONTH = re.compile(r"\b(\d{4})[-/](\d{1,2})\b")


def _mk_day(y: int, m: int, dd: int) -> datetime | None:
    try:
        return datetime(y, m, dd, tzinfo=timezone.utc)
    except ValueError:
        return None


def _explicit_dates(q: str) -> tuple[datetime, datetime, str] | None:
    """Parse explicit dates/ranges the user typed. A single date -> that whole
    calendar day [00:00, next 00:00); two dates -> the inclusive span; a bare
    YYYY-MM -> that whole month. Returns None if no explicit date is present."""
    days = [d for (y, m, dd) in _ISO_DATE.findall(q) if (d := _mk_day(int(y), int(m), int(dd)))]
    if len(days) >= 2:
        s, e = min(days), max(days)
        return s, e + timedelta(days=1), f"{s.date()} → {e.date()}"
    if len(days) == 1:
        s = days[0]
        return s, s + timedelta(days=1), f"{s.date()}"
    mo = _ISO_MONTH.findall(q)
    if mo:
        y, m = int(mo[0][0]), int(mo[0][1])
        if 1 <= m <= 12:
            s = datetime(y, m, 1, tzinfo=timezone.utc)
            e = datetime(y + 1, 1, 1, tzinfo=timezone.utc) if m == 12 else datetime(y, m + 1, 1, tzinfo=timezone.utc)
            return s, e, f"{y}-{m:02d}"
    return None


# Number words ("last six months") and month names, so the parser understands
# ranges a user actually types, not just the handful of canned phrases.
_WORDNUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
            "twelve": 12, "couple": 2, "few": 3, "several": 3}
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
           "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
           "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
           "aug": 8, "sept": 9, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_ABBR = {i: calendar.month_abbr[i] for i in range(1, 13)}
_MONTH_RE = re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b")
# "last/past 6 months", "two weeks", "3 days", "last year" (N defaults to 1).
_REL_RE = re.compile(
    r"(last|past|previous|recent|this)?\s*"
    r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|couple|few|several)?\s*"
    r"(hour|day|week|month|quarter|year)s?\b")


def _sub_months(dt: datetime, n: int) -> datetime:
    y, m = dt.year, dt.month - n
    while m <= 0:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    return dt.replace(year=y, month=m, day=min(dt.day, calendar.monthrange(y, m)[1]))


def _month_name_range(q: str) -> tuple[datetime, datetime, str] | None:
    """A named month ("sales in July") or a span ("between June and August")."""
    hits = [_MONTHS[m.group(1)] for m in _MONTH_RE.finditer(q)]
    if not hits:
        return None
    yr = re.search(r"\b(20\d{2})\b", q)
    year = int(yr.group(1)) if yr else _DATA_EPOCH.year  # demo data lives in 2026
    lo, hi = (min(hits[0], hits[1]), max(hits[0], hits[1])) if len(hits) >= 2 else (hits[0], hits[0])
    start = datetime(year, lo, 1, tzinfo=timezone.utc)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if hi == 12 else datetime(year, hi + 1, 1, tzinfo=timezone.utc)
    label = f"{_MONTH_ABBR[lo]} {year}" if lo == hi else f"{_MONTH_ABBR[lo]}–{_MONTH_ABBR[hi]} {year}"
    return start, end, label


def _relative_range(q: str, now: datetime) -> tuple[datetime, datetime, str] | None:
    """'last N days/weeks/months/quarters/years' (and singular 'last month')."""
    best = None
    for m in _REL_RE.finditer(q):
        qual, numtok, unit = m.group(1), m.group(2), m.group(3)
        if not qual and not numtok:          # bare "month" with no quantity → ignore
            continue
        if qual == "this":                    # calendar-aligned; handled elsewhere
            continue
        n = int(numtok) if (numtok and numtok.isdigit()) else _WORDNUM.get(numtok, 1)
        best = (max(1, n), unit)
    if not best:
        return None
    n, unit = best
    plural = "" if n == 1 else "s"
    if unit == "hour":
        return now - timedelta(hours=n), now, f"last {n} hour{plural}"
    if unit == "day":
        return now - timedelta(days=n), now, f"last {n} day{plural}"
    if unit == "week":
        return now - timedelta(weeks=n), now, f"last {n} week{plural}"
    if unit == "month":
        return _sub_months(now, n), now, f"last {n} month{plural}"
    if unit == "quarter":
        return _sub_months(now, 3 * n), now, f"last {n} quarter{plural}"
    return _sub_months(now, 12 * n), now, f"last {n} year{plural}"   # year


def _resolve_range(q: str, now: datetime) -> tuple[datetime, datetime, str]:
    def floor_day(x): return x.replace(hour=0, minute=0, second=0, microsecond=0)

    # 1) explicit calendar dates the user typed win outright.
    ex = _explicit_dates(q)
    if ex:
        return ex
    # 2) named months / month spans ("in July", "June to August").
    mn = _month_name_range(q)
    if mn:
        return mn
    # 3) calendar-aligned named periods.
    if "all time" in q:
        return _DATA_EPOCH, now + timedelta(days=1), "all time"
    if "yesterday" in q:
        s = floor_day(now) - timedelta(days=1); return s, floor_day(now), "yesterday"
    if "today" in q:
        return floor_day(now), now, "today"
    if "this week" in q:
        return now - timedelta(days=7), now, "this week"
    if "last month" in q:                      # previous FULL calendar month
        first_this = floor_day(now).replace(day=1)
        start = _sub_months(first_this, 1)
        return start, first_this, f"{_MONTH_ABBR[start.month]} {start.year}"
    if "this month" in q:                      # month-to-date
        return floor_day(now).replace(day=1), now, "this month"
    if "last year" in q:                       # previous FULL calendar year
        return datetime(now.year - 1, 1, 1, tzinfo=timezone.utc), datetime(now.year, 1, 1, tzinfo=timezone.utc), f"{now.year - 1}"
    if "this year" in q:
        return datetime(now.year, 1, 1, tzinfo=timezone.utc), now, "this year"
    # 4) relative "last N units".
    rel = _relative_range(q, now)
    if rel:
        return rel
    # 5) default.
    return now - timedelta(days=30), now, "last 30 days"


def _resolve_tenant(q: str) -> int | None:
    m = re.search(r"(?:restaurant|tenant|seller|store)\s*#?\s*([0-9]+)", q)
    if m:
        return int(m.group(1))
    m = re.search(r"(?:restaurant|seller|store)\s*([a-d])\b", q)
    if m:
        return ord(m.group(1)) - ord("a") + 1   # A->1, B->2
    return None


# Signals that tell us the deterministic parser already understands the question,
# so we can answer instantly and never pay for an LLM round-trip (the fast path).
_TIME_SIGNALS = ("today", "yesterday", "last hour", "past hour", "this week", "last 7",
                 "past week", "last 30", "this month", "past month", "last 24", "all time",
                 "this year", "week", "month", "day", "hour")
_INTENT_SIGNALS = ("sale", "sales", "order", "orders", "revenue", "gross", "net", "count",
                   "trend", "daily", "per day", "over time", "top", "best", "product",
                   "item", "dish", "status", "breakdown", "cancel", "placed", "shipped",
                   "delivered", "compare", "across", "leaderboard", "restaurant", "how many",
                   "total", "average", "avg")


def is_deterministic_confident(question: str) -> bool:
    """True when the rules parser clearly understands the question (a time phrase
    AND an intent). Such questions skip the LLM and answer in well under 10 ms."""
    q = question.lower()
    has_time = (any(t in q for t in _TIME_SIGNALS) or bool(_ISO_DATE.search(q))
                or bool(_ISO_MONTH.search(q)) or bool(_MONTH_RE.search(q)))
    has_intent = any(t in q for t in _INTENT_SIGNALS)
    return has_time and has_intent


_RANK_WORDS = ("top", "best", "most", "highest", "rank", "ranking", "winner",
               "leader", "leading", "sold most", "biggest", "largest")


def parse_deterministic(question: str, now: datetime) -> QueryPlan:
    q = question.lower()
    frm, to, label = _resolve_range(q, now)
    tenant = _resolve_tenant(q)

    is_product = any(w in q for w in ("product", "item", "dish", "menu"))
    rank = any(w in q for w in _RANK_WORDS)
    plural_entity = any(w in q for w in ("restaurants", "tenants", "sellers", "stores"))
    explicit_org = any(w in q for w in ("compare", "across", "all restaurants", "all tenants",
                                        "every restaurant", "which restaurant", "which tenant",
                                        "which seller", "leaderboard"))
    entity = any(w in q for w in ("restaurant", "tenant", "seller", "store"))
    # Organization scope: comparing/ranking RESTAURANTS (not products), with no
    # single tenant pinned. A product ranking stays a seller query.
    is_org = tenant is None and not is_product and (explicit_org or plural_entity or (rank and entity))

    if is_org:
        metric, scope = "org_compare", "org"
    elif any(w in q for w in ("trend", "daily", "per day", "over time", "each day", "by day")):
        metric, scope = "trends", "seller"
    elif is_product or (rank and not entity):
        metric, scope = "top_products", "seller"
    elif any(w in q for w in ("status", "breakdown", "cancel", "placed", "shipped", "delivered")):
        metric, scope = "by_status", "seller"
    else:
        metric, scope = "summary", "seller"

    if scope == "org":
        tenant = None
    elif tenant is None:
        tenant = 1
    gran = "hour" if ("hour" in q or "hourly" in q) else "day"
    lm = re.search(r"top\s*([0-9]+)", q)
    limit = int(lm.group(1)) if lm else 10
    who = "all restaurants" if scope == "org" else f"Restaurant #{tenant}"
    return QueryPlan(scope, metric, tenant, frm, to, gran, limit,
                     f"{who} · {metric.replace('_', ' ')} · {label}", "rules")


# ----------------------------- LLM plan (provider-agnostic) -----------------------------
def _log(msg: str) -> None:
    print(f"[nlq] {msg}", flush=True)


def _system_prompt(tenant_ids: list[int], now: datetime) -> str:
    return (
        "You convert a food-delivery analytics question into a STRICT JSON query plan and nothing else.\n"
        "Output ONLY a JSON object with keys: scope ('seller'|'org'), tenant_id (integer or null), "
        "metric ('summary'|'trends'|'by_status'|'top_products'|'org_compare'), "
        "range ('today'|'yesterday'|'last_7_days'|'last_30_days'|'this_month'|'all_time'), "
        "granularity ('day'|'hour'), limit (integer).\n"
        f"Known restaurant/tenant ids: {tenant_ids or 'unknown'}. 'Restaurant A'=1, 'B'=2, etc.\n"
        f"Now is {now.isoformat()}. Choose the closest 'range' value — DO NOT compute dates yourself.\n"
        "Use scope 'org' + metric 'org_compare' for questions comparing across restaurants; otherwise 'seller' "
        "with a tenant_id. Default range 'last_30_days', granularity 'day', limit 10. No prose, JSON only."
    )


def _plan_from_json(obj: dict, tenant_ids: list[int], now: datetime, engine: str) -> QueryPlan | None:
    """Validate raw model JSON against the allowed schema. Returns None on any violation."""
    try:
        metric = obj["metric"]
        if metric not in _METRICS:
            return None
        scope = "org" if (obj.get("scope") == "org" or metric == "org_compare") else "seller"
        tenant = obj.get("tenant_id")
        tenant = int(tenant) if (tenant is not None and scope == "seller") else None
        if scope == "seller" and tenant is None:
            tenant = (tenant_ids[0] if tenant_ids else 1)
        # Prefer the named range (server computes dates); fall back to explicit from/to.
        rng = obj.get("range")
        if rng in _RANGES:
            frm, to = range_dates(rng, now)
        else:
            frm = datetime.fromisoformat(str(obj["from"]).replace("Z", "+00:00"))
            to = datetime.fromisoformat(str(obj["to"]).replace("Z", "+00:00"))
            frm = frm if frm.tzinfo else frm.replace(tzinfo=timezone.utc)
            to = to if to.tzinfo else to.replace(tzinfo=timezone.utc)
        if frm >= to:
            return None
        gran = "hour" if obj.get("granularity") == "hour" else "day"
        limit = max(1, min(int(obj.get("limit", 10)), 100))
        who = "all restaurants" if scope == "org" else f"Restaurant #{tenant}"
        return QueryPlan(scope, metric, tenant, frm, to, gran, limit,
                         f"{who} · {metric.replace('_', ' ')}", engine)
    except Exception:
        return None


def _extract_json(text: str) -> str:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    return m.group(0) if m else text


async def parse_anthropic(question: str, tenant_ids: list[int], now: datetime) -> QueryPlan | None:
    if not config.ANTHROPIC_API_KEY:
        _log("anthropic selected but ANTHROPIC_API_KEY is empty")
        return None
    try:
        import httpx
        body = {"model": config.NLQ_MODEL, "max_tokens": 400, "temperature": 0,
                "system": _system_prompt(tenant_ids, now),
                "messages": [{"role": "user", "content": question}]}
        async with httpx.AsyncClient(timeout=config.NLQ_TIMEOUT_S) as client:
            r = await client.post("https://api.anthropic.com/v1/messages",
                                  headers={"x-api-key": config.ANTHROPIC_API_KEY,
                                           "anthropic-version": "2023-06-01", "content-type": "application/json"},
                                  json=body)
            r.raise_for_status()
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
        return _plan_from_json(json.loads(_extract_json(text)), tenant_ids, now, "anthropic")
    except Exception as e:
        _log(f"anthropic unavailable/invalid: {type(e).__name__}")
        return None


async def parse_ollama(question: str, tenant_ids: list[int], now: datetime) -> QueryPlan | None:
    """Local Gemma via Ollama; structured JSON output. No API key for local use."""
    try:
        import httpx
        url = config.OLLAMA_BASE_URL.rstrip("/") + "/api/chat"
        headers = {"content-type": "application/json"}
        if config.OLLAMA_API_KEY:   # Ollama Cloud only — NOT the Anthropic key
            headers["Authorization"] = f"Bearer {config.OLLAMA_API_KEY}"
        body = {"model": config.OLLAMA_MODEL, "stream": False, "format": "json",
                "options": {"temperature": 0},
                "messages": [{"role": "system", "content": _system_prompt(tenant_ids, now)},
                             {"role": "user", "content": question}]}
        async with httpx.AsyncClient(timeout=config.NLQ_TIMEOUT_S) as client:
            r = await client.post(url, headers=headers, json=body)
            r.raise_for_status()
            content = (r.json().get("message") or {}).get("content", "")
        return _plan_from_json(json.loads(_extract_json(content)), tenant_ids, now, "ollama")
    except Exception as e:
        _log(f"ollama unavailable/invalid ({config.OLLAMA_BASE_URL}, model={config.OLLAMA_MODEL}): {type(e).__name__}")
        return None


# ----------------------------- representative SQL -----------------------------
def plan_to_sql(p: QueryPlan) -> str:
    d0, d1 = p.frm.date().isoformat(), (p.to - timedelta(microseconds=1)).date().isoformat()
    if p.scope == "org":
        return (f"SELECT tenant_id,\n       SUM(order_count) AS orders,\n"
                f"       SUM(gross_sales_cents)/100.0 AS gross\n"
                f"FROM agg_orders_daily\nWHERE day_bucket BETWEEN '{d0}' AND '{d1}'\n"
                f"GROUP BY tenant_id\nORDER BY gross DESC;  -- central summary, all tenants")
    if p.metric == "trends":
        col = "hour_bucket" if p.granularity == "hour" else "day_bucket"
        tbl = "agg_orders_hourly" if p.granularity == "hour" else "agg_orders_daily"
        return (f"SELECT {col} AS bucket, SUM(order_count) AS orders,\n"
                f"       SUM(gross_sales_cents)/100.0 AS gross\n"
                f"FROM {tbl}\nWHERE tenant_id={p.tenant_id} AND {col} BETWEEN '{d0}' AND '{d1}'\n"
                f"GROUP BY {col} ORDER BY {col};")
    if p.metric == "by_status":
        return (f"SELECT status, SUM(order_count) AS orders,\n"
                f"       SUM(gross_sales_cents)/100.0 AS gross\n"
                f"FROM agg_orders_daily\nWHERE tenant_id={p.tenant_id} AND day_bucket BETWEEN '{d0}' AND '{d1}'\n"
                f"GROUP BY status;")
    if p.metric == "top_products":
        return (f"SELECT product_id, SUM(gross_revenue_cents)/100.0 AS gross, SUM(qty_sold) AS qty\n"
                f"FROM agg_products_daily\nWHERE tenant_id={p.tenant_id} AND day_bucket BETWEEN '{d0}' AND '{d1}'\n"
                f"GROUP BY product_id ORDER BY gross DESC LIMIT {p.limit};")
    return (f"SELECT SUM(order_count) AS orders,\n       SUM(gross_sales_cents)/100.0 AS gross,\n"
            f"       SUM(net_sales_cents)/100.0 AS net\n"
            f"FROM agg_orders_daily\nWHERE tenant_id={p.tenant_id} AND day_bucket BETWEEN '{d0}' AND '{d1}';\n"
            f"-- + boundary hour/minute buckets for exact non-aligned edges")


# ----------------------------- execute via existing rollup path -----------------------------
async def _plan_via_provider(question: str, tenant_ids: list[int], now: datetime) -> tuple[QueryPlan, str, str | None, bool]:
    """Return (plan, provider_requested, fallback_reason|None, fast_path)."""
    provider = config.NLQ_PROVIDER

    # FAST PATH: if the rules parser already understands the question, answer now
    # and skip the LLM. Keeps the common demo questions instant; the model is only
    # consulted for phrasing the rules don't recognise.
    if config.NLQ_FAST_PATH and provider != "rules" and is_deterministic_confident(question):
        plan = parse_deterministic(question, now)
        plan.engine = "rules-fast"
        _log(f"provider_requested={provider} engine=rules-fast (skipped LLM)")
        return plan, provider, None, True

    plan = None
    if provider == "ollama":
        plan = await parse_ollama(question, tenant_ids, now)
    elif provider == "anthropic":
        plan = await parse_anthropic(question, tenant_ids, now)
    # provider == "rules" => skip straight to deterministic
    fallback = None
    if plan is None and provider != "rules":
        fallback = f"{provider} unavailable/invalid/failed-validation → deterministic parser"
        _log(fallback)
    if plan is None:
        plan = parse_deterministic(question, now)
    _log(f"provider_requested={provider} engine={plan.engine}")
    return plan, provider, fallback, False


async def run_nlq(question: str, pinned_tenant: int | None = None) -> dict:
    from app.core.db import pools, primary_for_tenant, slot_for_tenant
    from app.services import query_service
    from app.services.freshness import freshness_block

    now = datetime.now(timezone.utc)
    tenant_ids = sorted(s.tenant_id for s in pools.sources if s.tenant_id is not None)
    plan, provider_requested, fallback_reason, fast_path = await _plan_via_provider(question, tenant_ids, now)

    # Explicit calendar dates always win over whatever range the engine chose, so
    # a dated question answers for exactly that day/span (never the default window).
    ex = _explicit_dates(question.lower())
    if ex:
        plan.frm, plan.to, _label = ex
        who = "all restaurants" if plan.scope == "org" else f"Restaurant #{plan.tenant_id}"
        plan.interpreted = f"{who} · {plan.metric.replace('_', ' ')} · {_label}"

    # Server-enforced tenant scope: a pinned tenant overrides anything the model or
    # the question text chose — the model cannot select another tenant.
    if pinned_tenant is not None:
        plan.scope = "seller"
        plan.tenant_id = int(pinned_tenant)
        if plan.metric == "org_compare":
            plan.metric = "summary"
        plan.interpreted = f"Restaurant #{pinned_tenant} (scope-locked) · {plan.metric.replace('_', ' ')}"

    t0 = time.perf_counter()
    result: dict
    fresh = None
    if plan.scope == "org":
        if pools.summary_owner is None:
            raise RuntimeError("organization query needs the owner pool (SUMMARY_DB_OWNER_URL)")
        result = await query_service.org_overview(pools.summary_owner, plan.frm, plan.to)
        # Ranking questions ("top/best restaurant") get the winner first, trimmed to
        # the requested N; the leaderboard is already sorted by gross descending.
        lb = result.get("leaderboard") or []
        if lb:
            ql = question.lower()
            if any(w in ql for w in _RANK_WORDS):
                n = int(m.group(1)) if (m := re.search(r"top\s+(\d+)", ql)) else 1
                result["top"] = lb[0]
                result["leaderboard"] = lb[: max(1, n)]
                result["ranked"] = True
    else:
        t = plan.tenant_id
        if plan.metric == "trends":
            result = {"series": await query_service.trends(pools.summary, t, plan.frm, plan.to, plan.granularity)}
        elif plan.metric == "by_status":
            result = {"categories": await query_service.by_status(pools.summary, primary_for_tenant(t), t, plan.frm, plan.to)}
        elif plan.metric == "top_products":
            result = {"items": await query_service.top_products(pools.summary, t, plan.frm, plan.to, plan.limit, "gross_sales")}
        else:
            result = {"data": await query_service.summary(pools.summary, primary_for_tenant(t), t, plan.frm, plan.to)}
        fresh = await freshness_block(pools.summary, slot_for_tenant(t))
    ms = round((time.perf_counter() - t0) * 1000, 1)

    return {
        "question": question,
        "engine": plan.engine,                       # actual: ollama | anthropic | rules | rules-fast
        "provider_requested": provider_requested,
        "fast_path": fast_path,                      # true = understood by rules, LLM skipped (instant)
        "fallback": fallback_reason,                 # null unless the provider fell back to rules
        "scope_locked": pinned_tenant is not None,
        "interpreted": {"scope": plan.scope, "tenant_id": plan.tenant_id, "metric": plan.metric,
                        "from": plan.frm.isoformat(), "to": plan.to.isoformat(),
                        "granularity": plan.granularity, "limit": plan.limit, "label": plan.interpreted},
        "sql": plan_to_sql(plan),
        "result": result,
        "execution_ms": ms,
        "source": "Xponent summary store (precomputed rollups)",
        "freshness": fresh,
    }
