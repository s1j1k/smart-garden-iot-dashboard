# Run the Streamlit dashboard on this PC (not the Raspberry Pi).
#
# Prerequisites:
#   1. MQTT broker listening on localhost:1883
#        docker compose -f docker-compose.local.yml up -d mosquitto
#      or native Mosquitto (winget install EclipseFoundation.Mosquitto)
#   2. ESP32 secrets.h MQTT_BROKER_HOST = this PC's WiFi LAN IP on HOME_WIFI
#
# Usage (from dashboard repo root, PowerShell):
#   .\scripts\run-local-dashboard.ps1

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Get-LanIPv4 {
    $addrs = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object {
            $_.IPAddress -notlike "127.*" -and
            $_.IPAddress -notlike "169.254.*" -and
            $_.PrefixOrigin -ne "WellKnown"
        } |
        Sort-Object {
            if ($_.InterfaceAlias -match "Wi-?Fi|Wireless") { 0 } else { 1 }
        }, IPAddress
    if ($addrs) { return $addrs[0].IPAddress }
    return $null
}

function Test-PortListening([int]$Port) {
    return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Test-LanMqttListener {
    # True if something is listening on :1883 on a non-loopback address (ESP32-reachable).
    $listeners = Get-NetTCPConnection -LocalPort 1883 -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -notin @("127.0.0.1", "::1") }
    return [bool]$listeners
}

function Find-Python {
    foreach ($name in @("python", "python3", "py")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # Skip the Microsoft Store stub that only opens the Store page.
        if ($cmd.Source -like "*WindowsApps*") { continue }
        return $cmd.Source
    }
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher -and $pyLauncher.Source -notlike "*WindowsApps*") {
        return "py -3"
    }
    return $null
}

function Find-Mosquitto {
    $cmd = Get-Command mosquitto -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        "${env:ProgramFiles}\mosquitto\mosquitto.exe",
        "${env:ProgramFiles(x86)}\mosquitto\mosquitto.exe"
    )
    foreach ($path in $candidates) {
        if (Test-Path $path) { return $path }
    }
    return $null
}

function Start-LanMosquitto {
    param([string]$MosquittoExe, [string]$ConfPath)

    # Windows winget Mosquitto installs a service that only accepts localhost.
    # Stop it so we can bind 0.0.0.0 for ESP32 clients on the LAN.
    $svc = Get-Service -Name "mosquitto" -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
        Write-Host "Stopping Windows Mosquitto service (localhost-only default) ..."
        try {
            Stop-Service -Name "mosquitto" -Force -ErrorAction Stop
            Start-Sleep -Seconds 1
        }
        catch {
            Write-Host "Could not stop mosquitto service (need Administrator once)."
            Write-Host "Dashboard will use localhost MQTT; ESP32 on WiFi will not reach this broker yet."
            Write-Host "Fix: open elevated PowerShell and run:"
            Write-Host "  Stop-Service mosquitto; Set-Service mosquitto -StartupType Manual"
            Write-Host "  & '$MosquittoExe' -c '$ConfPath'"
            return $false
        }
    }

    Write-Host "Starting Mosquitto with LAN listener: $MosquittoExe -c $ConfPath"
    Start-Process -FilePath $MosquittoExe -ArgumentList @("-c", $ConfPath) -WindowStyle Minimized
    Start-Sleep -Seconds 2
    return $true
}

$LanIp = Get-LanIPv4

Write-Host "=== Smart Garden dashboard (local PC) ==="
Write-Host "Dashboard MQTT host: localhost:1883"
Write-Host "Subscribe topic:     smart-garden/+/telemetry"
Write-Host "Command topic:       smart-garden/garden-node-01/cmd"
Write-Host ""
Write-Host "This PC LAN IP is: $(if ($LanIp) { $LanIp } else { 'unknown' })"
if ($LanIp) {
    Write-Host "ESP32 MQTT_BROKER_HOST should be: $LanIp"
}
Write-Host ""

$docker = Get-Command docker -ErrorAction SilentlyContinue
$mosquitto = Find-Mosquitto
$conf = Join-Path $Root "docker\mosquitto.conf"

if (-not (Test-PortListening 1883)) {
    Write-Host "No listener on :1883 yet."
    if ($docker) {
        Write-Host "Starting Mosquitto via docker-compose.local.yml ..."
        docker compose -f docker-compose.local.yml up -d mosquitto
        Start-Sleep -Seconds 2
    }
    elseif ($mosquitto) {
        [void](Start-LanMosquitto -MosquittoExe $mosquitto -ConfPath $conf)
    }
    else {
        Write-Host "Install Mosquitto or Docker, then re-run."
        Write-Host "  winget install --source winget EclipseFoundation.Mosquitto"
        Write-Host "  or: winget install --source winget Docker.DockerDesktop"
        Write-Host "  then: docker compose -f docker-compose.local.yml up -d mosquitto"
        exit 1
    }
}
elseif (-not (Test-LanMqttListener)) {
    Write-Host "MQTT is listening on localhost only (ESP32 on WiFi cannot reach it)."
    if ($docker) {
        Write-Host "Restarting Mosquitto via docker-compose.local.yml ..."
        docker compose -f docker-compose.local.yml up -d mosquitto
        Start-Sleep -Seconds 2
    }
    elseif ($mosquitto) {
        [void](Start-LanMosquitto -MosquittoExe $mosquitto -ConfPath $conf)
    }
}

if (-not (Test-PortListening 1883)) {
    Write-Host "WARNING: Still no listener on :1883. Broker may still be starting."
}
elseif (-not (Test-LanMqttListener)) {
    Write-Host "WARNING: :1883 is localhost-only. ESP32 needs this PC's LAN IP with 0.0.0.0 bind."
    Write-Host "  Re-run this script from an elevated PowerShell, or:"
    Write-Host "  Stop-Service mosquitto; & '$mosquitto' -c '$conf'"
}

$python = Find-Python
if (-not $python) {
    Write-Host "Python 3 not found. Install it, then re-run:"
    Write-Host "  winget install Python.Python.3.12"
    Write-Host "Close and reopen PowerShell after install so PATH updates."
    exit 1
}

$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating .venv ..."
    if ($python -eq "py -3") {
        & py -3 -m venv .venv
    }
    else {
        & $python -m venv .venv
    }
}

Write-Host "Installing requirements ..."
& $venvPython -m pip install -q -r requirements.txt

New-Item -ItemType Directory -Force -Path (Join-Path $Root "data") | Out-Null
$env:STREAMLIT_BROWSER_GATHER_USAGE_STATS = "false"

# Skip Streamlit's first-run interactive email prompt on Windows.
$streamlitHome = Join-Path $env:USERPROFILE ".streamlit"
New-Item -ItemType Directory -Force -Path $streamlitHome | Out-Null
$credentials = Join-Path $streamlitHome "credentials.toml"
if (-not (Test-Path $credentials)) {
    Set-Content -Path $credentials -Value "[general]`nemail = `"`"`n" -Encoding ascii
}

Write-Host ""
Write-Host "Opening Streamlit on http://localhost:8501"
Write-Host "Calibration tab -> pH probe + peristaltic pump cup wizard over MQTT."
Write-Host ""

# Always-on MQTT → SQLite ingest (Streamlit only stores while a browser tab is open).
$env:MQTT_HOST = if ($env:MQTT_HOST) { $env:MQTT_HOST } elseif ($LanIp) { $LanIp } else { "127.0.0.1" }
Write-Host "MQTT_HOST for dashboard/ingest: $env:MQTT_HOST"
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'mqtt_to_sqlite' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Process -FilePath $venvPython -ArgumentList @("scripts\mqtt_to_sqlite.py") -WorkingDirectory $Root -WindowStyle Minimized
Write-Host "SQLite ingest started → data\sensor_readings.sqlite"

& $venvPython -m streamlit run src/server.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
