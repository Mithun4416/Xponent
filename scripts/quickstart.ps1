param(
    [int]$Rows = 50000,
    [int]$Tenants = 5
)

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python venv not found. Run: py -3.12 -m venv .venv; .\.venv\Scripts\pip.exe install -r scripts\requirements.txt"
}

# --- Idempotent teardown: safe to re-run on a hot stack ---
Write-Host "[+] Stopping CDC worker and API (if running) ..." -ForegroundColor Cyan
try { docker compose stop cdc-worker agg-api 2>&1 | Out-Null } catch {}

docker compose up -d pg-primary pg-summary redis-cache

for ($i = 0; $i -lt 60; $i++) {
    $primary = docker compose exec -T pg-primary pg_isready -U postgres -d primary_db 2>$null
    $summary = docker compose exec -T pg-summary pg_isready -U agg_user -d agg_db 2>$null
    if ($LASTEXITCODE -eq 0 -and $primary -match "accepting connections" -and $summary -match "accepting connections") {
        break
    }
    Start-Sleep -Seconds 2
    if ($i -eq 59) { throw "PostgreSQL containers did not become ready." }
}

# Drop active replication slot so COPY doesn't block on ObjectInUse
Write-Host "[+] Dropping replication slot 'agg_slot' (if exists) ..." -ForegroundColor Cyan
try {
    docker compose exec -T pg-primary psql -U postgres -d primary_db -c `
        "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = 'agg_slot';" 2>&1 | Out-Null
} catch {}

# Truncate existing data so re-seed doesn't hit duplicate-key violations
Write-Host "[+] Truncating existing data ..." -ForegroundColor Cyan
try {
    docker compose exec -T pg-primary psql -U postgres -d primary_db -c `
        "TRUNCATE order_items, orders, customers, products, tenants CASCADE;" 2>&1 | Out-Null
} catch {}

& $python scripts\seed_dataset.py `
    --rows $Rows `
    --tenants $Tenants `
    --seed 42 `
    --dsn "postgresql://postgres:primary_password@localhost:5432/primary_db"

& $python -m worker.bootstrap `
    --primary-dsn "postgresql://postgres:primary_password@localhost:5432/primary_db" `
    --summary-dsn "postgresql://agg_user:agg_password@localhost:5433/agg_db" `
    --slot agg_slot `
    --force

docker compose up -d cdc-worker agg-api
docker compose ps
Write-Host "Dashboard: http://localhost:8000" -ForegroundColor Green
