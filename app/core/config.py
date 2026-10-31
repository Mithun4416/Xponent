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
NLQ_PROVIDER = os.getenv("NLQ_PROVIDER", "ollama").lower()
# Anthropic (optional; only used when NLQ_PROVIDER=anthropic).
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
NLQ_MODEL = os.getenv("NLQ_MODEL", "claude-haiku-5-5")
# Ollama (local by default). Inside a container, localhost is the container — use the host gateway.
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma3")   # set to whatever tag you `ollama pull`ed
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY", "")     # ONLY for Ollama Cloud; never reuse ANTHROPIC_API_KEY
NLQ_TIMEOUT_S = float(os.getenv("NLQ_TIMEOUT_S", "25"))

# Display currency for all money (synthetic *_cents units declared as this currency).
# Default INR for the food-delivery demo; the UI formats via Intl.NumberFormat with this code.
CURRENCY = os.getenv("CURRENCY", "INR")
CURRENCY_LOCALE = os.getenv("CURRENCY_LOCALE", "en-IN")

POOL_MIN = int(os.getenv("API_POOL_MIN", "2"))
POOL_MAX = int(os.getenv("API_POOL_MAX", "20"))
SLOT_NAME = os.getenv("CDC_SLOT_NAME", "agg_slot")
STALE_AFTER_MS = int(os.getenv("API_STALE_AFTER_MS", "30000"))
MAX_WINDOW_DAYS = int(os.getenv("API_MAX_WINDOW_DAYS", "366"))
