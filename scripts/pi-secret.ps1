# Print one value from the git-ignored .env.pi, decrypting *_DPAPI entries.
#
#   .\scripts\pi-secret.ps1 PI_USER
#   .\scripts\pi-secret.ps1 PI_PASSWORD      # reads PI_PASSWORD_DPAPI and decrypts it
#
# DPAPI values only decrypt for the Windows user that wrote them, on this machine.
param([Parameter(Mandatory = $true)][string]$Name)

$ErrorActionPreference = 'Stop'
$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) '.env.pi'
$values = @{}
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*([A-Z0-9_]+)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
}
if ($values.ContainsKey("${Name}_DPAPI")) {
    $secure = ConvertTo-SecureString $values["${Name}_DPAPI"]
    [System.Net.NetworkCredential]::new('', $secure).Password
} elseif ($values.ContainsKey($Name)) {
    $values[$Name]
} else {
    throw "$Name is not in .env.pi"
}
