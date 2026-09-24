# claude-session-sync installer for Windows (PowerShell 5.1+).
#
#   Install:    powershell -ExecutionPolicy Bypass -File install.ps1
#               irm https://raw.githubusercontent.com/bdarbaz/claude-session-sync/main/install.ps1 | iex
#   Uninstall:  powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\.claude-session-sync\install.ps1 -Uninstall
#
# Puts claude_session_sync.py in %USERPROFILE%\.claude-session-sync and starts it
# hidden at every login through a shortcut in your Startup folder. No admin rights needed.
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$RepoRaw = if ($env:CLAUDE_SESSION_SYNC_RAW) { $env:CLAUDE_SESSION_SYNC_RAW } else { 'https://raw.githubusercontent.com/bdarbaz/claude-session-sync/main' }
$Dir = Join-Path $env:USERPROFILE '.claude-session-sync'
$Script = Join-Path $Dir 'claude_session_sync.py'
$Shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'claude-session-sync.lnk'

function Stop-Sync {
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*claude_session_sync.py*--loop*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Find-Python {
    # The "python" that ships with Windows may be a Store stub that opens the Store; skip it.
    $candidates = @(@('py', '-3'), @('python'), @('python3'))
    foreach ($c in $candidates) {
        $exe = $c[0]
        $pre = @($c | Select-Object -Skip 1)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $exe @pre -c 'import sys; print(sys.executable if sys.version_info >= (3, 8) else "")' 2>$null
        } catch { continue }
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $python = ($out | Select-Object -Last 1).Trim()
        if ($python -and (Test-Path $python)) {
            $pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
            if (Test-Path $pythonw) { return @{ Python = $python; Pythonw = $pythonw } }
            return @{ Python = $python; Pythonw = $python }
        }
    }
    return $null
}

if ($Uninstall) {
    Stop-Sync
    Remove-Item $Shortcut -ErrorAction SilentlyContinue
    Write-Host "Uninstalled. Backups, trash and logs are still in $Dir (delete it if you don't need them)."
    return
}

$py = Find-Python
if (-not $py) {
    throw "Python 3.8+ not found. Install it (for example: winget install Python.Python.3.12), open a new PowerShell window and run this installer again."
}

New-Item -ItemType Directory -Force -Path $Dir | Out-Null
$tmp = "$Script.new"
$local = if ($PSScriptRoot) { Join-Path $PSScriptRoot 'claude_session_sync.py' } else { $null }
if ($local -and (Test-Path $local)) {
    Copy-Item $local $tmp -Force
} else {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri "$RepoRaw/claude_session_sync.py" -OutFile $tmp
}
& $py.Python -m py_compile $tmp
if ($LASTEXITCODE -ne 0) { throw 'claude_session_sync.py does not compile' }
Stop-Sync
Move-Item $tmp $Script -Force
Remove-Item (Join-Path $Dir '__pycache__') -Recurse -Force -ErrorAction SilentlyContinue
# Keep a copy of this installer so uninstalling works without the download.
$self = Join-Path $Dir 'install.ps1'
if ($PSCommandPath -and ((Resolve-Path $PSCommandPath).Path -ne $self)) {
    Copy-Item $PSCommandPath $self -Force
} elseif (-not $PSCommandPath) {
    try { Invoke-WebRequest -UseBasicParsing -Uri "$RepoRaw/install.ps1" -OutFile $self } catch { }
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($Shortcut)
$lnk.TargetPath = $py.Pythonw
$lnk.Arguments = "`"$Script`" --loop"
$lnk.WorkingDirectory = $Dir
$lnk.WindowStyle = 7
$lnk.Description = 'Mirror Claude desktop Code sessions between accounts'
$lnk.Save()

Start-Process -FilePath $py.Pythonw -ArgumentList @("`"$Script`"", '--loop') -WorkingDirectory $Dir -WindowStyle Hidden

& $py.Python $Script --status
Write-Host ''
Write-Host "Installed (starts at login via $Shortcut)."
Write-Host 'Quit the Claude app completely once (also from the system tray) and reopen it to see the sessions.'
Write-Host "Log: $Dir\sync.log"
