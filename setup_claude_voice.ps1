# Pop Locker - setup. Run from the folder it will live in (C:\PopLocker), as administrator (Setup.bat asks).
# Everything it needs comes with it - nothing is downloaded. Safe to run again.
#   1. the Microsoft C++ runtime, if Windows doesn't have it yet (the database needs it)
#   2. the chat database: its own PostgreSQL, run as a Windows service - you choose its password
#   3. the Claude Code hooks (read replies aloud, save every chat, open the window)
#   4. shortcuts: Desktop + Startup, and Pop Locker History
#   5. saves the chats already on this PC, and shows the iPhone address and key
# Everything comes in the package (Python, PostgreSQL, the speech model and voices): the only question is the
# database password
$ErrorActionPreference = 'Stop'
$D = $PSScriptRoot
$py = "$D\python\python.exe"
$pyw = "$D\python\pythonw.exe"
$pg = "$D\runtime\pgsql\bin"
$data = "$D\data"
# installs made before the rename (2026-10-30) keep their ClaudeVoiceDB service on the same data folder
$service = if (Get-Service 'ClaudeVoiceDB' -ErrorAction SilentlyContinue) { 'ClaudeVoiceDB' } else { 'PopLockerDB' }
Write-Host "Setting up Pop Locker in $D" -ForegroundColor Cyan
if (-not (Test-Path $py)) { Write-Host "The python folder is missing - unzip the whole package first." -ForegroundColor Red; exit 1 }

# 1. C++ runtime
$vc = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64' -ErrorAction SilentlyContinue
if (-not $vc -or $vc.Installed -ne 1 -or $vc.Major -lt 14 -or $vc.Minor -lt 40) {
    Write-Host "[1/5] Installing the Microsoft C++ runtime..."
    $p = Start-Process "$D\runtime\vc_redist.x64.exe" -ArgumentList '/install /quiet /norestart' -Wait -PassThru
    if ($p.ExitCode -notin 0, 1638, 3010) { Write-Host "C++ runtime install failed ($($p.ExitCode))." -ForegroundColor Red; exit 1 }
} else { Write-Host "[1/5] Microsoft C++ runtime already here." }

# 2. the chat database
if (-not (Test-Path "$data\PG_VERSION")) {
    Write-Host "[2/5] Setting up the chat database." -ForegroundColor Cyan
    $plain = if ($env:POP_LOCKER_DB_PASSWORD) { $env:POP_LOCKER_DB_PASSWORD } else { $env:CLAUDE_VOICE_DB_PASSWORD }   # unattended installs only
    while (-not $plain) {
        $a = Read-Host "      Choose a password for your chat database" -AsSecureString
        $b = Read-Host "      Type it again" -AsSecureString
        $pa = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
        $pb = [Runtime.InteropServices.Marshal]::PtrToStringBSTR([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
        if ($pa.Length -lt 6) { Write-Host "      At least 6 characters, please." -ForegroundColor Yellow }
        elseif ($pa -ne $pb) { Write-Host "      They didn't match - try again." -ForegroundColor Yellow }
        else { $plain = $pa }
    }
    $port = 5488            # its own port, so it never clashes with another PostgreSQL on this PC
    while (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { $port++ }
    $pwfile = [IO.Path]::GetTempFileName()
    [IO.File]::WriteAllText($pwfile, $plain)
    try {
        & "$pg\initdb.exe" -D $data -U postgres -A scram-sha-256 -E UTF8 --locale=C --pwfile=$pwfile | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "initdb failed" }
    } finally { Remove-Item $pwfile -Force }
    Add-Content "$data\postgresql.conf" "`nport = $port`nlisten_addresses = 'localhost'`n"
    # the service runs as Network Service, which has to be able to write its own data folder
    icacls $data /grant '*S-1-5-20:(OI)(CI)M' /T /Q | Out-Null
    @{ host = '127.0.0.1'; port = $port; user = 'postgres'; password = $plain } | ConvertTo-Json |
        Set-Content "$D\db.json" -Encoding ascii
    icacls "$D\db.json" /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" "${env:USERNAME}:F" /Q | Out-Null
    $plain = $null
}
if (-not (Get-Service $service -ErrorAction SilentlyContinue)) {
    & "$pg\pg_ctl.exe" register -N $service -D $data -S auto -U 'NT AUTHORITY\NetworkService' | Out-Null
}
Start-Service $service
& $py "$D\claude_chat_save.py" --setup
if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't set up the chat database." -ForegroundColor Red; exit 1 }
Write-Host "[2/5] Chat database ready." -ForegroundColor Green

# 3. Claude Code hooks
Write-Host "[3/5] Adding the Claude Code hooks..."
& $py "$D\setup_hooks.py"

# 4. shortcuts
Write-Host "[4/5] Shortcuts on the Desktop and in Startup..."
$ws = New-Object -ComObject WScript.Shell
# the shortcuts' names from before the rename
foreach ($old in "$([Environment]::GetFolderPath('Desktop'))\Claude's voice.lnk", "$([Environment]::GetFolderPath('Startup'))\Claude's voice.lnk",
                 "$([Environment]::GetFolderPath('Desktop'))\Search Claude Chats.lnk") { Remove-Item -LiteralPath $old -ErrorAction SilentlyContinue }
foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Startup'))) {
    $s = $ws.CreateShortcut("$dir\Pop Locker.lnk")
    $s.TargetPath = $pyw
    $s.Arguments = "`"$D\claude_voice_player.pyw`""
    $s.WorkingDirectory = $D
    $s.Description = "Reads Claude's replies aloud (no code) - wake word mic and iPhone door"
    $s.IconLocation = "$D\icon\poplocker.ico,0"
    $s.Save()
}
$s = $ws.CreateShortcut("$([Environment]::GetFolderPath('Desktop'))\Pop Locker History.lnk")
$s.TargetPath = $pyw
$s.Arguments = "`"$D\chat_search.pyw`""
$s.WorkingDirectory = $D
$s.Description = "Search every chat with Claude, by keyword, date and project"
$s.IconLocation = "$D\icon\poplocker.ico,0"
$s.Save()

# 5. chats already on this PC, and the phone's address
Write-Host "[5/5] Saving the Claude chats already on this PC..."
& $py "$D\claude_chat_save.py" --all
$old = (Test-Path "$D\ears_key.txt") -and ((Get-Content "$D\ears_key.txt" -Raw).Trim() -cmatch '^[a-z]+-[a-z]+-[a-z]+-\d{2}$')
if ($old -or -not (Test-Path "$D\ears_key.txt")) {
    $k = (& $py -c "import secrets; print(secrets.token_urlsafe(32))" | Out-String).Trim()
    if ($k.Length -lt 40) { throw "Couldn't make a phone key." }
    Set-Content "$D\ears_key.txt" $k -NoNewline
    if ($old) {
        Write-Host "      The old phone key was too easy to guess, so it has been replaced." -ForegroundColor Yellow
        Write-Host "      Set up your phone again, and copy ears_key.txt to any other PC you use." -ForegroundColor Yellow
    }
}
$k = (Get-Content "$D\ears_key.txt" -Raw).Trim()
$ip = (Get-NetIPAddress -AddressFamily IPv4 | ? { $_.IPAddress -like '192.168.*' -or $_.IPAddress -like '10.*' } | select -First 1).IPAddress
$url = "http://${ip}:5055/say"
Set-Content "$D\iPhone address.txt" "iPhone shortcut, in the Get Contents of URL step (POST, JSON field: text):`r`n  URL: $url`r`n  Headers: add X-Key, with the key below as its value`r`n`r`nNuePost on Android, in Settings:`r`n  Endpoint URL: $url`r`n  JSON Key: text`r`n  Access Key: the key below`r`n`r`nKey (keep it private: anyone who has it and is on your Wi-Fi can type to Claude):`r`n$k" -Encoding utf8
Write-Host "      Phone address and private key are in:" -ForegroundColor Cyan
Write-Host "      $D\iPhone address.txt" -ForegroundColor Yellow
Write-Host ""
Write-Host "Done. Double-click 'Pop Locker' on the Desktop to start it (it also starts with Windows)." -ForegroundColor Green
Write-Host "The first time, Windows may ask to allow it on the network - click Allow (Private networks) so the iPhone can reach it."
