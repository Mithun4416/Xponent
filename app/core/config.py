"""API configuration from the environment (task T7)."""
from __future__ import annotations

import os

# The API MUST use the RLS-constrained reader role (never the owner).
SUMMARY_DB_URL = os.getenv("SUMMARY_DB_URL", "postgresql://agg_reader:agg_reader_pass@localhost:5433/agg_db")
# Primary is read only for sub-minute edge slices of non-aligned ranges (fix C1 / D1)
# and for Receipts verify/repair source reads.
PRIMARY_DB_URL = os.getenv("PRIMARY_DB_URL", "postgresql://replication_reader:rep_pass@localhost:5432/primary_db")
# Owner (write) DSN used ONLY by admin Receipts repair; empty disables repair writes.
SUMMARY_DB_OWNER_URL = os.getenv("SUMMARY_DB_OWNER_URL", "")
# Admin key guarding the Receipts endpoints (no-JWT posture, D3).
ADMIN_KEY = os.getenv("ADMIN_KEY", "xpo-admin-dev")

# Auth posture per decision D3: no JWT. Dev mode accepts X-Dev-Tenant-ID; an
# optional shared key (X-Tenant-Key) may be required when dev mode is off.
AUTH_DEV_MODE = os.getenv("AUTH_DEV_MODE", "true").lower() == "true"
TENANT_API_KEY = os.getenv("TENANT_API_KEY", "")

# Natural-language query → validated plan (never raw SQL).
# Provider: ollama (local Gemma, no key) | anthropic | rules (deterministic only).
# Default to the deterministic rules engine: instant, no external dependency, and
# correct for the dashboard's question set. Set NLQ_PROVIDER=ollama|anthropic to
# enable an LLM for unusual phrasings (the rules fast-path still short-circuits).
NLQ_PROVIDER = os.getenv("NLQ_PROVIDER", "rules").lower()
# Anthropic (optional; only used when NLQ_PROVIDER=anthropic).
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
NLQ_MODEL = os.getenv("NLQ_MODEL", "claude-haiku-5-5")
# Ollama (local by default). Inside a container, localhost is the container — use the host gateway.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3")   # set to whatever tag you `ollama pull`ed
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")     # ONLY for Ollama Cloud; never reuse ANTHROPIC_API_KEY
NLQ_TIMEOUT_S = float(os.getenv("NLQ_TIMEOUT_S", "6"))
# Fast path: when the deterministic parser already understands the question
# (a recognised time phrase + a recognised intent) answer INSTANTLY and skip the
# LLM entirely. The model is then reserved for genuinely unusual phrasing. This
# makes the common demo questions sub-10ms instead of waiting on local inference.
NLQ_FAST_PATH = os.getenv("NLQ_FAST_PATH", "true").lower() == "true"

# Display currency for all money (synthetic *_cents units declared as this currency).
# Default INR for the food-delivery demo; the UI formats via Intl.NumberFormat with this code.
CURRENCY = os.getenv("CURRENCY", "INR")
CURRENCY_LOCALE = os.getenv("CURRENCY_LOCALE", "en-IN")

# B-3: cap how many heavy dashboard queries one tenant may run concurrently.
# Beyond this the API returns 429 (protects the summary store from a single
# tenant's dashboard spamming expensive reads). Per-process, in-memory.
TENANT_MAX_CONCURRENCY = int(os.getenv("TENANT_MAX_CONCURRENCY", "6"))
# B-4: derived metrics are declared in this JSON file, not in code. Edit the file
# (or point this at your own) to add a metric; no redeploy of logic required.
import os.path as _osp
METRICS_CONFIG_PATH = os.getenv(
    "METRICS_CONFIG_PATH", _osp.join(_osp.dirname(_osp.dirname(__file__)), "metrics.json"))
# DB explorer (judge-facing "see the databases") row cap per page.
EXPLORER_MAX_ROWS = int(os.getenv("EXPLORER_MAX_ROWS", "100"))

POOL_MIN = int(os.getenv("API_POOL_MIN", "2"))
POOL_MAX = int(os.getenv("API_POOL_MAX", "20"))
SLOT_NAME = os.getenv("CDC_SLOT_NAME", "agg_slot")
STALE_AFTER_MS = int(os.getenv("API_STALE_AFTER_MS", "30000"))
MAX_WINDOW_DAYS = int(os.getenv("API_MAX_WINDOW_DAYS", "366"))
