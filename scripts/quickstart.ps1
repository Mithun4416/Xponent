<#
Xponent - one-command bring-up for Windows (shared source DB topology).

  .\scripts\quickstart.ps1                               # 50k orders total, 5 tenants (skewed)
  .\scripts\quickstart.ps1 -OrdersPerTenant 1000000      # exactly 1M orders for EVERY tenant
  .\scripts\quickstart.ps1 -OrdersPerTenant 1000000 -Tenants 10

-Rows splits a total across tenants with a power-law skew (a few big restaurants,
a long tail). -OrdersPerTenant gives every tenant the same exact count instead.
#>
param(
    [int]$Rows = 50000,
    [int]$Tenants = 5,
    [int]$OrdersPerTenant = 0
)

# "Continue", not "Stop": in Windows PowerShell 5.1, Docker's normal progress output on
# stderr would otherwise abort the script. Failures are caught by Check / throw instead.
$ErrorActionPreference = "Continue"
Set-Location (Join-Path $PSScriptRoot "..")

function Step($msg) { Write-Host "`n[+] $msg" -ForegroundColor Cyan }
# Native commands don't throw on failure in PowerShell; check the exit code ourselves.
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)." } }

$python = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python venv not found. Run: py -3.12 -m venv .venv; .\.venv\Scripts\pip.exe install -r scripts\requirements.txt"
}

docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Docker is not running. Start Docker Desktop, wait for 'Engine running', then re-run." }

$primaryDsn = "postgresql://postgres:primary_password@localhost:5432/primary_db"
$summaryDsn = "postgresql://agg_user:agg_password@localhost:5433/agg_db"

# --- Idempotent teardown: safe to re-run on a hot stack ---
Step "Stopping CDC worker and API (if running)"
docker compose stop cdc-worker agg-api *> $null

Step "Starting databases (pg-primary, pg-summary, redis)"
cmd /c "docker compose up -d pg-primary pg-summary redis-cache 2>&1"  # via cmd: PS 5.1 shows Docker progress (stderr) as red errors
Check "docker compose up"

for ($i = 0; $i -lt 60; $i++) {
    docker compose exec -T pg-primary pg_isready -U postgres -d primary_db *> $null
    $p = $LASTEXITCODE
    docker compose exec -T pg-summary pg_isready -U agg_user -d agg_db *> $null
    if ($p -eq 0 -and $LASTEXITCODE -eq 0) { Write-Host "    databases ready"; break }
    Start-Sleep -Seconds 2
    if ($i -eq 59) { throw "PostgreSQL containers did not become ready." }
}

# Init scripts only run on a fresh volume; (re)apply the API's writer role so older volumes get it too.
Step "Ensuring the API's app_writer role exists"
Get-Content db\primary\005_app_writer.sql -Raw | docker compose exec -T pg-primary psql -U postgres -d primary_db -q -v ON_ERROR_STOP=1
Check "app_writer role"

# Drop the replication slot so the bulk load isn't decoded/retained, then clear old data.
Step "Dropping replication slot and truncating existing data"
docker compose exec -T pg-primary psql -U postgres -d primary_db -q -c `
    "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name = 'agg_slot';" *> $null
docker compose exec -T pg-primary psql -U postgres -d primary_db -q -c `
    "TRUNCATE order_items, orders, customers, products, tenants CASCADE;"
Check "Truncate"

if ($OrdersPerTenant -gt 0) {
    $total = $OrdersPerTenant * $Tenants
    Step ("Seeding {0:N0} orders per tenant x {1} tenants = {2:N0} orders (this can take a while)" -f $OrdersPerTenant, $Tenants, $total)
    & $python scripts\seed_dataset.py --orders-per-tenant $OrdersPerTenant --tenants $Tenants --seed 42 --batch 500000 --dsn $primaryDsn
} else {
    Step ("Seeding {0:N0} orders across {1} tenants" -f $Rows, $Tenants)
    & $python scripts\seed_dataset.py --rows $Rows --tenants $Tenants --seed 42 --dsn $primaryDsn
}
Check "Seeding"

Step "Refreshing table statistics"
docker compose exec -T pg-primary psql -U postgres -d primary_db -q -c "ANALYZE;"
Check "ANALYZE"

Step "Bootstrapping rollups and creating the CDC slot"
& $python -m worker.bootstrap --primary-dsn $primaryDsn --summary-dsn $summaryDsn --slot agg_slot --force
Check "Bootstrap"

Step "Starting the CDC worker and query API"
cmd /c "docker compose up -d cdc-worker agg-api 2>&1"
Check "docker compose up"

for ($i = 0; $i -lt 30; $i++) {
    try { Invoke-RestMethod http://localhost:8000/api/v1/health -TimeoutSec 3 | Out-Null; break } catch { Start-Sleep -Seconds 2 }
}

docker compose ps
Write-Host ""
Write-Host "Xponent is up  ->  http://localhost:8000" -ForegroundColor Green
Write-Host "Operator key (dev): xpo-admin-dev  (enter it in the sidebar)" -ForegroundColor Green
