# JugginAI PC bridge installer. Runs in user context: no elevation, netsh, or firewall rules.
# Usage (from the unpacked folder):  powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
# Token files go in %USERPROFILE%\.jugginai\ (pc-bridge-token, cloudflared-token, optional hf-token).
# If a .\secrets\ folder sits next to this script, its token files are moved there first.
# Never touches Card Enhancer (:8000), the 100.83.191.52 forwarder, or PAIR.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Src    = $PSScriptRoot
$JDir   = Join-Path $env:USERPROFILE '.jugginai'
$Bridge = Join-Path $JDir 'bridge'
$Logs   = Join-Path $JDir 'logs'
$Cfd    = Join-Path $JDir 'cloudflared.exe'
$Me     = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Problems = @()

New-Item -ItemType Directory -Force -Path $JDir, $Bridge, $Logs | Out-Null

# 1. copy package files
foreach ($f in 'bridge.py', 'start-bridge.bat', 'README.md') {
    $p = Join-Path $Src $f
    if (Test-Path $p) { Copy-Item -Force $p (Join-Path $Bridge $f) }
}

# 2. token files (moved from .\secrets if shipped alongside; never printed)
$SecretsSrc = Join-Path $Src 'secrets'
foreach ($t in 'pc-bridge-token', 'cloudflared-token', 'hf-token') {
    $s = Join-Path $SecretsSrc $t
    if (Test-Path $s) { Move-Item -Force $s (Join-Path $JDir $t) }
}
foreach ($t in 'pc-bridge-token', 'cloudflared-token', 'hf-token') {
    $p = Join-Path $JDir $t
    if (Test-Path $p) {
        if ((Get-Item $p).Length -lt 8) { $Problems += "$t looks empty" }
        & icacls.exe $p /inheritance:r /grant:r "${Me}:(R,W)" | Out-Null
        if ($LASTEXITCODE -ne 0) { $Problems += "icacls failed on $t" }
    } elseif ($t -ne 'hf-token') {
        $Problems += "missing $p"
    }
}

# 3. cloudflared
if (-not (Test-Path $Cfd)) {
    try {
        Invoke-WebRequest -UseBasicParsing -OutFile $Cfd `
            -Uri 'https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe'
    } catch { $Problems += "cloudflared download failed: $($_.Exception.Message)" }
}

# 4. python check
$py = (Get-Command pythonw.exe -ErrorAction SilentlyContinue) ; if (-not $py) { $py = Get-Command pyw.exe -ErrorAction SilentlyContinue }
if (-not $py) { $Problems += 'pythonw.exe / pyw.exe not found on PATH (install Python 3 from python.org)' }

# 5. autostart
$Startup = [Environment]::GetFolderPath('Startup')
Copy-Item -Force (Join-Path $Bridge 'start-bridge.bat') (Join-Path $Startup 'jugginai-start-bridge.bat')

# 6. stop only our previous instances (command line must contain .jugginai)
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='pyw.exe' OR Name='cloudflared.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*.jugginai*' -and
                   ($_.Name -eq 'cloudflared.exe' -or $_.CommandLine -like '*bridge.py*') } |
    ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
Start-Sleep -Seconds 1

# 7. launch (the .bat relaunches itself hidden)
Start-Process -WindowStyle Hidden -FilePath (Join-Path $Bridge 'start-bridge.bat') -WorkingDirectory $Bridge

# 8. one-line status
$health = 'down'
$tokPath = Join-Path $JDir 'pc-bridge-token'
if (Test-Path $tokPath) {
    $tok = (Get-Content -Raw $tokPath).Trim()
    for ($i = 0; $i -lt 15; $i++) {
        Start-Sleep -Seconds 1
        try {
            $r = Invoke-RestMethod -TimeoutSec 5 -Uri 'http://127.0.0.1:8899/health' -Headers @{ Authorization = "Bearer $tok" }
            $health = "ok (lmstudio:$($r.engines.lmstudio.online) llamacpp:$($r.engines.llamacpp.online) disk:$($r.disk.free_gb)GB)"
            break
        } catch {}
    }
    Remove-Variable tok
}
$cfdUp = [bool](Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" | Where-Object { $_.CommandLine -like '*.jugginai*' })
$extra = if ($Problems.Count) { ' | problems: ' + ($Problems -join '; ') } else { '' }
Write-Output "jugginai-pc-bridge: bridge=$health tunnel=$(if ($cfdUp) {'running'} else {'not running'})$extra"
