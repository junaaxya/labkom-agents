# LabKom PC Agent - Auto Installer for Windows
# Run as Administrator:
#   powershell -ExecutionPolicy Bypass -File install-windows.ps1
#
# What it does:
#   1. Login Koordinator to backend
#   2. Pick a Lab
#   3. Auto-generate next PC code (PC-LABx-NN) based on existing PCs
#   4. Create PC record + generate agent token via API
#   5. Copy agent files to C:\labkom-agent
#   6. Install Python dependencies
#   7. Write config.json with the new pc_code + token + base_url
#   8. Register Windows scheduled task as SYSTEM (auto-start on boot)
#   9. Run agent, verify it appears Online

[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$BaseUrl = "http://lab-ilkom.my.id",
    [string]$InstallDir = "C:\labkom-agent",
    [string]$PythonVersion = "3.12.6",
    [string]$PythonInstallerUrl = "",
    [switch]$SkipPythonInstall
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host ("[INSTALL] " + $msg) -ForegroundColor Cyan
}

function Write-Info($msg) {
    Write-Host ("    " + $msg) -ForegroundColor Gray
}

function Write-Ok($msg) {
    Write-Host ("    OK  " + $msg) -ForegroundColor Green
}

function Write-Err($msg) {
    Write-Host ("    ERR " + $msg) -ForegroundColor Red
}

function Invoke-Schtasks($Arguments, [switch]$AllowFailure) {
    # Windows PowerShell 5.1 turns native stderr into error records when
    # $ErrorActionPreference is Stop. Missing tasks are normal during a fresh
    # install, so run schtasks with a relaxed local preference and inspect the
    # native exit code ourselves.
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & schtasks @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }

    if ($exitCode -ne 0 -and -not $AllowFailure) {
        $message = ($output | Out-String).Trim()
        if (-not $message) { $message = "schtasks exited with code $exitCode" }
        Write-Err $message
        exit 1
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
    }
}

function Stop-WithUsage($msg) {
    Write-Err $msg
    Write-Info "Correct command:"
    Write-Host '    powershell -ExecutionPolicy Bypass -File install-windows.ps1 -BaseUrl "http://lab-ilkom.my.id"' -ForegroundColor Yellow
    Write-Info "Do not add a trailing backslash after the BaseUrl value."
    exit 1
}

function Normalize-InstallerInputs {
    if ([string]::IsNullOrWhiteSpace($BaseUrl)) {
        Stop-WithUsage "BaseUrl cannot be empty."
    }

    $script:BaseUrl = $BaseUrl.Trim().TrimEnd("/")
    $script:InstallDir = $InstallDir.Trim()

    if ([string]::IsNullOrWhiteSpace($script:InstallDir)) {
        Stop-WithUsage "InstallDir cannot be empty."
    }

    if ($script:InstallDir -eq "\" -or $script:InstallDir -eq "/") {
        Stop-WithUsage "Invalid InstallDir '$script:InstallDir'. This usually happens when the command has an extra trailing backslash after BaseUrl."
    }

    if ($script:InstallDir -match '^[A-Za-z]:\\?$') {
        Stop-WithUsage "Invalid InstallDir '$script:InstallDir'. Refusing to install directly into a drive root. Use C:\labkom-agent."
    }

    if ($script:InstallDir.StartsWith("\\")) {
        Stop-WithUsage "Invalid InstallDir '$script:InstallDir'. UNC/root paths are not supported. Use C:\labkom-agent."
    }

    if (-not [System.IO.Path]::IsPathRooted($script:InstallDir)) {
        Stop-WithUsage "InstallDir must be an absolute Windows path. Use C:\labkom-agent."
    }

    if ($script:InstallDir -match '[<>|?*]') {
        Stop-WithUsage "InstallDir contains invalid path characters. Use C:\labkom-agent."
    }

    try {
        $fullInstallDir = [System.IO.Path]::GetFullPath($script:InstallDir)
    } catch {
        Stop-WithUsage ("Invalid InstallDir: " + $_.Exception.Message)
    }

    if ($fullInstallDir -match '^[A-Za-z]:\\?$') {
        Stop-WithUsage "Invalid InstallDir '$fullInstallDir'. Refusing to install directly into a drive root. Use C:\labkom-agent."
    }

    $script:InstallDir = $fullInstallDir.TrimEnd("\")
}

Normalize-InstallerInputs

# --- 0. Must run as admin ---
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Err "Script must be run as Administrator. Right-click PowerShell -> Run as administrator."
    exit 1
}

Write-Step "LabKom PC Agent - Auto Installer"
Write-Info "Backend: $BaseUrl"
Write-Info "Install dir: $InstallDir"

# Stop an existing install first so old PC code/token cannot keep running during reinstall.
$existingTaskName = "LabKom Agent"
Write-Step "Stopping existing LabKom Agent if present"
$existingTask = Invoke-Schtasks -Arguments @("/query", "/tn", $existingTaskName) -AllowFailure
if ($existingTask.ExitCode -eq 0) {
    Invoke-Schtasks -Arguments @("/end", "/tn", $existingTaskName) -AllowFailure | Out-Null
    Start-Sleep -Seconds 2
    Write-Ok "Existing scheduled task stopped"
} else {
    Write-Info "No existing scheduled task found"
}

$installAgentPath = Join-Path $InstallDir "agent.py"
$runningAgents = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$installAgentPath*" }
foreach ($proc in $runningAgents) {
    try {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction Stop
        Write-Ok ("Stopped old agent process PID " + $proc.ProcessId)
    } catch {
        Write-Err ("Failed to stop old agent PID " + $proc.ProcessId + ": " + $_.Exception.Message)
    }
}

# --- 1. Test backend reachability ---
Write-Step "Checking backend health"
try {
    $health = Invoke-RestMethod -Uri "$BaseUrl/api/v1/health" -Method GET -TimeoutSec 10
    Write-Ok "Backend reachable"
} catch {
    Write-Err "Cannot reach backend at $BaseUrl. Check network or BaseUrl."
    exit 1
}

# --- 2. Login as Koordinator ---
Write-Step "Login Koordinator"
$email = Read-Host "Email Koordinator"
$password = Read-Host "Password" -AsSecureString
$plainPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($password)
)

$loginBody = @{ email = $email; password = $plainPassword } | ConvertTo-Json
try {
    $loginRes = Invoke-RestMethod -Uri "$BaseUrl/api/v1/auth/login" -Method POST -Body $loginBody -ContentType "application/json"
} catch {
    Write-Err "Login failed: $($_.Exception.Message)"
    exit 1
}

$token = $loginRes.data.token
$userRole = $loginRes.data.user.role
if (-not $token) {
    Write-Err "Login response missing token"
    exit 1
}
if ($userRole -ne "KOORDINATOR_LAB") {
    Write-Err "User role is $userRole. Must be KOORDINATOR_LAB."
    exit 1
}
Write-Ok ("Logged in as " + $loginRes.data.user.name)

$authHeaders = @{ Authorization = "Bearer $token" }

# --- 3. Pick a lab ---
Write-Step "Listing labs"
$labsRes = Invoke-RestMethod -Uri "$BaseUrl/api/v1/labs" -Method GET -Headers $authHeaders
$labs = $labsRes.data
if (-not $labs -or $labs.Count -eq 0) {
    Write-Err "No labs found. Create a lab first in dashboard /labs."
    exit 1
}

for ($i = 0; $i -lt $labs.Count; $i++) {
    $l = $labs[$i]
    Write-Host ("  [{0}] {1}  ({2})" -f ($i + 1), $l.name, $l.location)
}
$labChoice = Read-Host "Pilih lab (nomor)"
$labIdx = [int]$labChoice - 1
if ($labIdx -lt 0 -or $labIdx -ge $labs.Count) {
    Write-Err "Invalid lab choice"
    exit 1
}
$lab = $labs[$labIdx]
Write-Ok ("Lab dipilih: " + $lab.name)

# --- 4. Compute next pc_code ---
# Strategy:
#   1. Look at existing PCs in lab. Detect prefix from first existing PC (anything before last "-NN").
#      Examples it learns automatically:
#        PC-LAB1-01  -> prefix "PC-LAB1-"
#        PC-LABM-01  -> prefix "PC-LABM-"
#        PC-LABDS-01 -> prefix "PC-LABDS-"
#   2. If lab has zero PC yet, derive prefix from lab name:
#        "Lab Dasar"      -> "PC-LABD-"
#        "Lab Multimedia" -> "PC-LABM-"   (first letter of suffix)
#      User can override at the prompt.

Write-Step "Detecting next PC code"
$pcsRes = Invoke-RestMethod -Uri "$BaseUrl/api/v1/labs/$($lab.id)/pcs" -Method GET -Headers $authHeaders
$existingPCs = $pcsRes.data
Write-Info ("Existing PCs in this lab: " + $existingPCs.Count)

function Get-PcPrefix($code) {
    # Returns "PREFIX-" from "PREFIX-NN", e.g. "PC-LABM-01" -> "PC-LABM-"
    if ($code -match "^(.*?)-(\d+)$") {
        return $matches[1] + "-"
    }
    return $null
}

$prefix = $null
$maxN = 0

if ($existingPCs.Count -gt 0) {
    foreach ($pc in $existingPCs) {
        $p = Get-PcPrefix $pc.pcCode
        if ($p) {
            if (-not $prefix) { $prefix = $p }
            if ($pc.pcCode -match "^([\s\S]*?)-(\d+)$") {
                $thisPrefix = $matches[1] + "-"
                $thisN = [int]$matches[2]
                if ($thisPrefix -eq $prefix -and $thisN -gt $maxN) { $maxN = $thisN }
            }
        }
    }
}

if (-not $prefix) {
    # Derive from lab name
    $labLower = $lab.name.ToLower()
    if ($labLower -match "multimedia") {
        $prefix = "PC-LABM-"
    } elseif ($labLower -match "dasar") {
        $prefix = "PC-LABD-"
    } elseif ($labLower -match "jaringan|network") {
        $prefix = "PC-LABJ-"
    } else {
        $prefix = "PC-LAB" + ($labIdx + 1) + "-"
    }
}

$nextN = $maxN + 1
$pcCode = ("{0}{1:D2}" -f $prefix, $nextN)
$pcName = ("PC " + ("{0:D2}" -f $nextN))
Write-Ok ("Next PC code: " + $pcCode)
Write-Info ("Hostname Windows lokal: " + (hostname))

$confirm = Read-Host "Pakai kode '$pcCode'? (Y/n)"
if ($confirm -eq "n" -or $confirm -eq "N") {
    $pcCode = Read-Host "Masukkan PC code manual"
    $pcName = Read-Host "Masukkan nama PC manual"
}

# --- 5. Create PC + generate token ---
Write-Step "Create PC record"
$createBody = @{
    labId = $lab.id
    pcCode = $pcCode
    name = $pcName
} | ConvertTo-Json
try {
    $createRes = Invoke-RestMethod -Uri "$BaseUrl/api/v1/labs/pcs" -Method POST -Headers $authHeaders -Body $createBody -ContentType "application/json"
    $pcId = $createRes.data.id
    if (-not $pcId) { $pcId = $createRes.id }
    Write-Ok ("PC created with id " + $pcId)
} catch {
    $msg = $_.ErrorDetails.Message
    if (-not $msg) { $msg = $_.Exception.Message }
    if ($msg -match "sudah digunakan") {
        Write-Info "PC code already exists, fetching its id"
        $pcsRes2 = Invoke-RestMethod -Uri "$BaseUrl/api/v1/labs/$($lab.id)/pcs" -Method GET -Headers $authHeaders
        $found = $pcsRes2.data | Where-Object { $_.pcCode -eq $pcCode } | Select-Object -First 1
        if (-not $found) {
            Write-Err "PC $pcCode exists but cannot retrieve id"
            exit 1
        }
        $pcId = $found.id
        Write-Ok ("Reusing existing PC id " + $pcId)
    } else {
        Write-Err ("Create PC failed: " + $msg)
        exit 1
    }
}

Write-Step "Generate agent token"
try {
    $tokenRes = Invoke-RestMethod -Uri "$BaseUrl/api/v1/pcs/$pcId/generate-token" -Method POST -Headers $authHeaders
} catch {
    Write-Err ("Generate token failed: " + $_.Exception.Message)
    exit 1
}
$agentToken = $tokenRes.data.token
if (-not $agentToken) { $agentToken = $tokenRes.token }
if (-not $agentToken) {
    Write-Err "Token response missing token"
    exit 1
}
Write-Ok "Token generated"

# --- 6. Prepare install dir ---
Write-Step "Prepare install directory"
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir | Out-Null
}
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

$filesToCopy = @("agent.py", "requirements.txt")
foreach ($f in $filesToCopy) {
    $src = Join-Path $scriptDir $f
    if (-not (Test-Path $src)) {
        Write-Err "Missing $f next to install-windows.ps1"
        exit 1
    }
    Copy-Item $src $InstallDir -Force
}
Write-Ok ("Files copied to " + $InstallDir)

# --- 7. Install Python deps ---
Write-Step "Install Python dependencies"

# 7a. Ensure Python is available. Skip if already installed (any 3.x), otherwise download and install silently.
function Test-PythonAvailable {
    try {
        $cmd = Get-Command python -ErrorAction Stop
        $verLine = & $cmd.Source --version 2>&1
        if ($verLine -match "Python\s+3\.") {
            return $cmd.Source
        }
        return $null
    } catch {
        return $null
    }
}

$pythonPath = Test-PythonAvailable
if (-not $pythonPath -and -not $SkipPythonInstall) {
    Write-Info "Python 3 not found. Will download and install $PythonVersion silently."
    if (-not $PythonInstallerUrl) {
        $PythonInstallerUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
    }
    $pyInstaller = Join-Path $env:TEMP ("python-" + $PythonVersion + "-amd64.exe")
    try {
        Invoke-WebRequest -Uri $PythonInstallerUrl -OutFile $pyInstaller -UseBasicParsing
    } catch {
        Write-Err ("Failed to download Python installer: " + $_.Exception.Message)
        exit 1
    }

    # InstallAllUsers + PrependPath so Get-Command python works for SYSTEM scheduled task too.
    $pyArgs = @(
        "/quiet",
        "InstallAllUsers=1",
        "PrependPath=1",
        "Include_pip=1",
        "Include_test=0",
        "Include_launcher=1"
    )
    $proc = Start-Process -FilePath $pyInstaller -ArgumentList $pyArgs -Wait -PassThru
    if ($proc.ExitCode -ne 0) {
        Write-Err ("Python installer exited with code " + $proc.ExitCode)
        exit 1
    }
    Remove-Item $pyInstaller -ErrorAction SilentlyContinue

    # PATH for child processes only refreshes after env update; rebuild for this session.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path", "User")
    $pythonPath = Test-PythonAvailable
}

if (-not $pythonPath) {
    Write-Err "Python 3 still not available after install. Aborting."
    exit 1
}
Write-Ok ("Python: " + $pythonPath)

$python = $pythonPath
Push-Location $InstallDir
& $python -m pip install --upgrade pip | Out-Null
& $python -m pip install -r requirements.txt
$pipExit = $LASTEXITCODE
Pop-Location
if ($pipExit -ne 0) {
    Write-Err "pip install failed with exit code $pipExit"
    exit 1
}
Write-Ok "Dependencies installed"

# --- 8. Write config.json ---
Write-Step "Write config.json"
$config = @{
    pc_code = $pcCode
    agent_token = $agentToken
    base_url = "$BaseUrl/api/v1/pcs"
    heartbeat_interval = 60
    command_poll_interval = 30
}
$configPath = Join-Path $InstallDir "config.json"
$configJson = $config | ConvertTo-Json -Depth 5
# Write BOM-less UTF-8 so Python json.load can parse it on Windows PowerShell 5.1
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($configPath, $configJson, $utf8NoBom)
Write-Ok ("config.json written: " + $configPath)

# --- 9. Register Task Scheduler as SYSTEM ---
Write-Step "Register scheduled task LabKom Agent"
$taskName = "LabKom Agent"
$pythonPath = $python
$cmdArgs = ('/c cd /d "{0}" && "{1}" "{2}\agent.py" >> "{2}\agent-task.log" 2>&1' -f $InstallDir, $pythonPath, $InstallDir)

# Remove old task if exists
$existingTask = Invoke-Schtasks -Arguments @("/query", "/tn", $taskName) -AllowFailure
if ($existingTask.ExitCode -eq 0) {
    Invoke-Schtasks -Arguments @("/delete", "/tn", $taskName, "/f") | Out-Null
}

Invoke-Schtasks -Arguments @("/create", "/tn", $taskName, "/tr", ("cmd " + $cmdArgs), "/sc", "onstart", "/ru", "SYSTEM", "/rl", "HIGHEST", "/f") | Out-Null
Write-Ok "Scheduled task created (runs as SYSTEM at boot)"

# --- 10. Run task now to verify ---
Write-Step "Run agent now"
Invoke-Schtasks -Arguments @("/run", "/tn", $taskName) | Out-Null
Start-Sleep -Seconds 8

$logPath = Join-Path $InstallDir "agent-task.log"
if (Test-Path $logPath) {
    Write-Info "Last log lines:"
    Get-Content $logPath -Tail 8 | ForEach-Object { Write-Host ("    " + $_) -ForegroundColor DarkGray }
} else {
    Write-Info "No log yet. Check Task Scheduler -> $taskName"
}

# --- 11. Verify online via API ---
Write-Step "Verifying agent online via dashboard API"
$online = $false
for ($i = 0; $i -lt 6; $i++) {
    Start-Sleep -Seconds 5
    try {
        $detail = Invoke-RestMethod -Uri "$BaseUrl/api/v1/pcs/$pcId" -Method GET -Headers $authHeaders
        $status = $detail.data.agentStatus
        if (-not $status) { $status = $detail.agentStatus }
        Write-Info ("Try $($i+1): agentStatus=$status")
        if ($status -eq "ONLINE") { $online = $true; break }
    } catch {
        Write-Info ("Try $($i+1): error " + $_.Exception.Message)
    }
}

if ($online) {
    Write-Ok "Agent ONLINE in dashboard"
} else {
    Write-Err "Agent did not become ONLINE. Check $logPath for errors."
}

# --- 12. Configure Wake-on-LAN at OS level (BIOS still manual) ---
Write-Step "Configuring Wake-on-LAN (Windows side)"

# 12a. Disable Fast Startup. Required: Fast Startup leaves NIC half-off and breaks WoL.
try {
    $powerKey = "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power"
    if (-not (Test-Path $powerKey)) { New-Item -Path $powerKey -Force | Out-Null }
    Set-ItemProperty -Path $powerKey -Name "HiberbootEnabled" -Type DWord -Value 0
    Write-Ok "Fast Startup disabled"
} catch {
    Write-Err ("Failed to disable Fast Startup: " + $_.Exception.Message)
}

# 12b. Allow each physical wired NIC to wake the PC and respond to magic packets.
$wolNicCount = 0
$nics = Get-NetAdapter -Physical -ErrorAction SilentlyContinue |
    Where-Object { $_.Status -eq "Up" -and $_.MediaType -eq "802.3" }

foreach ($nic in $nics) {
    try {
        # Power-management: allow wake + wake on magic packet only
        Set-NetAdapterPowerManagement -Name $nic.Name `
            -WakeOnMagicPacket Enabled `
            -WakeOnPattern Enabled `
            -DeviceSleepOnDisconnect Disabled `
            -ErrorAction Stop | Out-Null

        # Vendor-specific advanced properties. Names differ by driver, so try the common keys
        # and ignore failures silently per key.
        $advKeys = @(
            @{ DisplayName = "Wake on Magic Packet"; Value = "Enabled" },
            @{ DisplayName = "Wake on pattern match"; Value = "Enabled" },
            @{ DisplayName = "Energy-Efficient Ethernet"; Value = "Disabled" },
            @{ DisplayName = "Green Ethernet"; Value = "Disabled" }
        )
        foreach ($k in $advKeys) {
            try {
                Set-NetAdapterAdvancedProperty -Name $nic.Name `
                    -DisplayName $k.DisplayName `
                    -DisplayValue $k.Value `
                    -ErrorAction Stop | Out-Null
            } catch {
                # property not supported on this driver, safe to ignore
            }
        }

        $wolNicCount++
        Write-Ok ("WoL enabled on adapter: " + $nic.Name)
    } catch {
        Write-Err ("Cannot configure WoL on " + $nic.Name + ": " + $_.Exception.Message)
    }
}

if ($wolNicCount -eq 0) {
    Write-Err "No active wired adapter found. Connect LAN cable and re-run installer for full WoL setup."
} else {
    Write-Info "BIOS still needs Wake on LAN / ErP off / Deep Sleep off configured manually."
}

Write-Step "Done"
Write-Info ("PC Code        : " + $pcCode)
Write-Info ("Lab            : " + $lab.name)
Write-Info ("Install dir    : " + $InstallDir)
Write-Info ("Config         : " + $configPath)
Write-Info ("Task Scheduler : " + $taskName)
Write-Info ("Log file       : " + $logPath)
Write-Info ("Dashboard      : $BaseUrl/pc-monitoring")
