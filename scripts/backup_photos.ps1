# Copy the Pi's timelapse photos (and the camera logs) into OneDrive on this PC.
#
# Only photos not already in the backup are copied, so it is quick to run again.
# OneDrive then uploads them, which gives an off-Pi, off-PC copy.
#
# Needs the Pi reachable over SSH from this PC (same network) and the key
# ~/.ssh/smart_garden_pi. Run from the dashboard repo in PowerShell:
#
#   .\scripts\backup_photos.ps1
#   .\scripts\backup_photos.ps1 -PiHost admin@192.168.4.31      # if the Pi's address changed
#   .\scripts\backup_photos.ps1 -Dest "D:\garden-backup"

param(
    [string]$PiHost = "admin@192.168.4.30",
    [string]$Key = "$HOME\.ssh\smart_garden_pi",
    [string]$Repo = "/home/admin/smart-garden-iot-dashboard",
    [string]$Dest = "$env:OneDrive\Smart Garden backup"
)

$ErrorActionPreference = "Stop"
$ssh = @("-i", $Key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
$images = Join-Path $Dest "timelapse photos"
New-Item -ItemType Directory -Force -Path $images | Out-Null

Write-Host "Listing photos on $PiHost ..."
$remote = & ssh @ssh $PiHost "ls $Repo/data/camera/images" | Where-Object { $_ -like "*.jpg" }
if ($LASTEXITCODE -ne 0) { throw "Could not reach $PiHost over SSH (is the PC on the garden's network?)" }
$have = @{}
Get-ChildItem $images -Filter *.jpg | ForEach-Object { $have[$_.Name] = $true }
$missing = @($remote | Where-Object { -not $have.ContainsKey($_) })
Write-Host ("{0} photos on the Pi, {1} already backed up, {2} to copy." -f $remote.Count, $have.Count, $missing.Count)

if ($missing.Count -gt 0) {
    # One tar stream for all missing photos. cmd.exe pipes bytes unchanged
    # (Windows PowerShell 5.1 would re-encode a binary pipe).
    $list = Join-Path $env:TEMP "garden_backup_list.txt"
    # LF line ends and no BOM: the Pi's tar reads the names literally.
    [IO.File]::WriteAllText($list, (($missing -join "`n") + "`n"), (New-Object System.Text.UTF8Encoding $false))
    $sshArgs = ($ssh | ForEach-Object { '"' + $_ + '"' }) -join " "
    $cmd = "ssh $sshArgs $PiHost ""cd $Repo/data/camera/images && tar -cf - -T -"" < ""$list"" | tar -xf - -C ""$images"""
    cmd /c $cmd
    Remove-Item $list
    $still = @($missing | Where-Object { -not (Test-Path (Join-Path $images $_)) })
    if ($still.Count -gt 0) { throw "$($still.Count) photos did not copy (first: $($still[0]))" }
}

# The measurement logs are small: copy them every time.
foreach ($file in @("plants.jsonl", "growth.csv", "layout.json")) {
    & scp @ssh "${PiHost}:$Repo/data/camera/$file" (Join-Path $Dest $file) 2>$null
}
$count = (Get-ChildItem $images -Filter *.jpg).Count
Write-Host "Done: $count photos in $images"
