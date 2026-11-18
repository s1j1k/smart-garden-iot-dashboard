# Run the dashboard offline as an archive of the trial: no Raspberry Pi, no ESP32,
# no broker, no network. Readings come from a Pi backup cut off at one moment, and
# the dashboard shows them as they were then (public site, no controls).
#
# Usage (from the dashboard repo root, PowerShell):
#   .\scripts\run-archive-dashboard.ps1
#   .\scripts\run-archive-dashboard.ps1 -At 2026-10-16T13:00:00Z
#
# -At is UTC. The default, 2026-10-18T13:00:00Z, is midnight on 19 Oct AEDT: the
# 24 h view then holds the 18 Oct recovery test with plants and the 3-day view
# reaches back to the 16 Oct run. The first run at a new -At builds its archive from
# the backup (several minutes); later runs reuse it. Opens http://localhost:8502.

param(
    [string]$At = "2026-10-18T13:00:00Z",
    [string]$Backup = "data\pi_backup\db_backup_2026-10-24.sqlite",
    [int]$Port = 8502
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Stamp = ($At -replace "[:\-]", "").Substring(0, 13)   # 2026-10-18T13:00:00Z -> 20261018T1300
$Archive = Join-Path $Root "data\archive\archive_$Stamp.sqlite"

if (-not (Test-Path $Archive)) {
    if (-not (Test-Path $Backup)) { throw "Pi backup not found: $Backup" }
    Write-Host "Building $Archive from $Backup (one-off, several minutes)..."
    & $Python scripts\build_archive_db.py --source $Backup --until $At --out $Archive
    if ($LASTEXITCODE -ne 0) { throw "Archive build failed" }
}

$env:SMART_GARDEN_SITE = "public"
$env:SMART_GARDEN_ARCHIVE_NOW = $At
$env:SMART_GARDEN_SQLITE = $Archive
$env:SMART_GARDEN_HISTORY_ROWS = "100000"      # about three days of readings
$env:SMART_GARDEN_DASHBOARD_PERSIST = "0"      # read only: nothing written back
$env:MQTT_HOST = "127.0.0.1"                    # nothing listens there; the dashboard stays offline

Write-Host "Archive as at $At -> http://localhost:$Port"
# This PC only: not reachable from the rest of the network.
& $Python -m streamlit run src\server.py --server.address 127.0.0.1 --server.port $Port --server.headless false
