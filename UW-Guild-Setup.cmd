<# : UW Guild addon setup
@echo off
rem Double-click to install the UW_Guild addon for WoW Forever; it then keeps itself up to date.
rem This file is a batch file and a PowerShell script in one: cmd runs these few lines, which start PowerShell on
rem this same file, and PowerShell reads the whole file and skips these lines as a comment.
set "UWSELF=%~f0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:UWSELF))) %*"
exit /b %errorlevel%
#>
# UW Guild addon installer and updater for Windows (WoW Forever). Free, from the guild's own site, no account.
#
# What it does, and nothing else:
#   - finds WoW Forever's AddOns folder (Battle.net's records, the usual folders, or asks once);
#   - downloads version.json and UW_Guild.zip from https://unbotheredwarlords.github.io, checks the zip's size and
#     SHA-256 against version.json and that every file in it is a WoW addon file inside UW_Guild\, then swaps the
#     new UW_Guild folder in (the old one comes back if anything fails);
#   - registers a task for this Windows user (no admin) that checks once an hour, and installs an update only while
#     WoW Forever is closed, so the game never loads half an update.
# It never touches WTF (your settings and progress), never downgrades, never downloads or runs any program, and
# never updates itself: a new version of this setup is a new download. Its own files live in
# %LOCALAPPDATA%\UW-Guild (copy of this script, settings, log). Remove it with the Start menu's
# "UW Guild addon\Stop automatic updates" (the addon stays).
#
# Options: -Quiet (the hourly task: no output, no questions), -Remove, -AddOns <folder> (skip the search),
# -Source <https URL or folder> (tests: where version.json and the zip come from), -StateDir <folder> (tests: instead of
# %LOCALAPPDATA%\UW-Guild), -NoSchedule (install once, register nothing), -TestRun (scripts/Test-Updater.ps1 only:
# needs -AddOns and -StateDir, registers nothing, and runs on the officer PC too, in its temp folders).
# Through the .cmd, arguments ride inside one quoted PowerShell command, so a path with spaces breaks there: for those,
# copy this file to a .ps1 and run it with powershell -File (as scripts/Test-Updater.ps1 does). Members pass none.

param(
  [switch]$Quiet,
  [switch]$Remove,
  [switch]$NoSchedule,
  [switch]$TestRun,
  [string]$AddOns = "",
  [string]$Source = "https://unbotheredwarlords.github.io",
  [string]$StateDir = "",
  [string]$TestPublicKey = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # Windows PowerShell 5.1 downloads many times slower with the progress bar
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }

$ADDON = "UW_Guild"
$TASK = "UW_Guild updater"
# Only file types WoW loads (bot/uwbot/addonzip.py SHIP): nothing else is ever written.
$SHIP = @(".toc", ".lua", ".xml", ".tga", ".blp", ".ttf", ".ogg", ".mp3", ".md", ".txt")
$MAX_ZIP = 20MB
$MAX_UNPACKED = 60MB
# The site's signing key (public half; the private half lives only on the officer PC, where the site is published).
# version.json.sig = base64 RSA PKCS#1 v1.5 SHA-256 signature over version.json's exact bytes, and version.json pins
# the zip's SHA-256, so a push to the site repo without that key can't reach any install (red team, 8 Nov 2026).
# Made 8 Nov 2026 on the officer PC (scripts/New-SiteKey.ps1, 3072-bit). Empty would mean hash checks only, and no task.
$PUBKEY = "<RSAKeyValue><Modulus>rdpU9GcizmAFLkbZiYf8LTWWr1iXzL34zIRT87jyFd+g6NXY7kQz9PjahF8QLdiHOUSa/YH0XOFAD8BF5+1zLSulYsPelTTK8Cxyl4uq37mQI6vCma6tP0sNvoD2i1u+bvmzGq38yOkCojdZby+kumuOCitjWZn20QPJYcW5ftIUS4YUKe8hW5M6KD6EmeZLEH7sVTM96YDG3ynJmJslXS4dJA31GBXE3NcOH22abLHcTqefPRmrZxMGTxB5MOtffVOnGihVyHShVdIAwdyDOgse0D+ZIIfOvnir1NfmuDYFQ38sgNATVCnQSn9pn2V8VrbXGgzbP8cNqO63bYzhfe58DBM/+pNwR6xBLRkNo/8W1+Lmh6yQmHOjRyiFfT0AvN3tcqrhyUxw4Keq3VTWedwJ4b1cap5A8HDkVBtoqpd7ULmLz+tR8b5SF3xsLarweodbtZ6t61WRSsYDjM6D8/hBkMeNH0QafpjWKjVK/vzq6sGKPkLs3udm7Mo9Kb55</Modulus><Exponent>AQAB</Exponent></RSAKeyValue>"
$VERSION_RX = '^(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d{4}))?-([0-9a-f]{7})$'

if ($TestRun) {
  if (-not $AddOns -or -not $StateDir) { Write-Host "-TestRun needs -AddOns and -StateDir."; exit 2 }
  $NoSchedule = $true
  # A key the test made, never honoured outside -TestRun (the environment form is for the cmd entry-point test,
  # where cmd would read the key's < and > as redirections).
  if (-not $TestPublicKey -and $env:UW_TEST_PUBKEY) { $TestPublicKey = $env:UW_TEST_PUBKEY }
  if ($TestPublicKey) { $PUBKEY = $TestPublicKey }
}
if (-not $StateDir) { $StateDir = Join-Path $env:LOCALAPPDATA "UW-Guild" }
$LOG = Join-Path $StateDir "update.log"
$CONFIG = Join-Path $StateDir "config.json"
$SCRIPT_COPY = Join-Path $StateDir "uw-update.ps1"
$MENU = Join-Path ([Environment]::GetFolderPath("Programs")) "UW Guild addon"

function Say([string]$text) { if (-not $Quiet) { Write-Host $text } }

function Log([string]$text) {
  try {
    if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    $line = "{0:yyyy-MM-dd HH:mm:ss}Z {1}" -f [DateTime]::UtcNow, $text
    $old = @()
    if (Test-Path -LiteralPath $LOG) { $old = @(Get-Content -LiteralPath $LOG -Tail 199) }
    Set-Content -LiteralPath $LOG -Value (@($old) + $line) -Encoding UTF8
  } catch { }
}

function Done([int]$code) {
  # Success closes by itself (one click in all: the download); a problem waits, so it can be read.
  if (-not $Quiet) {
    Write-Host ""
    if ($code -eq 0) { Write-Host "This window closes in 20 seconds."; Start-Sleep -Seconds 20 }
    else { Read-Host "Press Enter to close" | Out-Null }
  }
  exit $code
}

function Fail([string]$text) {
  Log "FAILED: $text"
  if (-not $Quiet) { Write-Host ""; Write-Host $text -ForegroundColor Yellow }
  Done 1
}

# 2026.11.08.1553-61841f3 -> 202611081553 (older builds without the time read as 0000). Anything else: 0.
function VersionKey([string]$v) {
  $m = [regex]::Match([string]$v, $VERSION_RX)
  if (-not $m.Success) { return [int64]0 }
  $hm = "0000"
  if ($m.Groups[4].Success) { $hm = $m.Groups[4].Value }
  return [int64]($m.Groups[1].Value + $m.Groups[2].Value + $m.Groups[3].Value + $hm)
}

function TocVersion([string]$toc) {
  if (-not (Test-Path -LiteralPath $toc)) { return "" }
  foreach ($line in [IO.File]::ReadAllLines($toc)) {
    $m = [regex]::Match($line, '^\s*##\s*Version\s*:\s*(\S+)')
    if ($m.Success) { return $m.Groups[1].Value }
  }
  return ""
}

function ReadConfig {
  try { if (Test-Path -LiteralPath $CONFIG) { return (Get-Content -LiteralPath $CONFIG -Raw | ConvertFrom-Json) } } catch { }
  return $null
}

# --- Finding WoW Forever --------------------------------------------------------------------------------------

function ForeverFrom([string]$path) {
  # A folder somewhere inside a World of Warcraft install -> its _forever_ folder, if there is one.
  if (-not $path) { return $null }
  $p = $path.Trim().Trim('"').TrimEnd('\', '/')
  for ($i = 0; $i -lt 4 -and $p; $i++) {
    $leaf = Split-Path -Leaf $p
    if ($leaf -ieq "_forever_" -and (Test-Path -LiteralPath $p -PathType Container)) { return $p }
    $f = Join-Path $p "_forever_"
    if (Test-Path -LiteralPath $f -PathType Container) { return $f }
    $p = Split-Path -Parent $p
  }
  return $null
}

function FindForever {
  $seen = New-Object System.Collections.Generic.List[string]
  $add = { param($x) if ($x -and -not ($seen -contains $x)) { $seen.Add($x) } }
  # 1. What Blizzard's installer wrote to the registry.
  foreach ($key in @("HKLM:\SOFTWARE\WOW6432Node\Blizzard Entertainment\World of Warcraft",
                     "HKLM:\SOFTWARE\Blizzard Entertainment\World of Warcraft")) {
    try { & $add (ForeverFrom (Get-ItemProperty -LiteralPath $key -ErrorAction Stop).InstallPath) } catch { }
  }
  foreach ($root in @("HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                      "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")) {
    try {
      Get-ChildItem -LiteralPath $root -ErrorAction Stop | ForEach-Object {
        $p = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
        if ($p -and [string]$p.DisplayName -like "World of Warcraft*") { & $add (ForeverFrom $p.InstallLocation) }
      }
    } catch { }
  }
  # 2. The Battle.net app's list of installed games (a binary file; the install folders are plain text in it).
  $db = Join-Path $env:ProgramData "Battle.net\Agent\product.db"
  if (Test-Path -LiteralPath $db) {
    try {
      $text = [Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($db))
      foreach ($m in [regex]::Matches($text, '[A-Za-z]:[\\/][^\x00-\x1f"<>|?*]{0,200}?World of Warcraft')) {
        & $add (ForeverFrom ($m.Value -replace '/', '\'))
      }
    } catch { }
  }
  # 3. The usual folders on every local drive.
  foreach ($d in [IO.DriveInfo]::GetDrives()) {
    if ($d.DriveType -ne "Fixed" -or -not $d.IsReady) { continue }
    foreach ($rel in @("Program Files (x86)\World of Warcraft", "Program Files\World of Warcraft", "World of Warcraft",
                       "Games\World of Warcraft", "Battle.net\World of Warcraft", "Blizzard\World of Warcraft")) {
      & $add (ForeverFrom (Join-Path $d.RootDirectory.FullName $rel))
    }
  }
  return ,$seen
}

function AskForFolder {
  if ($Quiet) { return $null }
  Say "I couldn't find WoW Forever. Pick your World of Warcraft folder (the one with _forever_ inside)."
  try {
    Add-Type -AssemblyName System.Windows.Forms
    $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
    $dlg.Description = "Pick your World of Warcraft folder (the one with _forever_ inside)"
    if ($dlg.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { return (ForeverFrom $dlg.SelectedPath) }
  } catch { }
  return $null
}

function ResolveAddOns {
  if ($AddOns) { return $AddOns }
  $cfg = ReadConfig
  if ($cfg -and $cfg.addons -and (Test-Path -LiteralPath (Split-Path -Parent ([string]$cfg.addons)))) { return [string]$cfg.addons }
  $found = FindForever
  $forever = $null
  if ($found.Count -ge 1) { $forever = $found[0] }
  if ($found.Count -gt 1) { Say ("Found more than one WoW Forever; using " + $forever + ".") }
  if (-not $forever) { $forever = AskForFolder }
  if (-not $forever) { return $null }
  return (Join-Path $forever "Interface\AddOns")
}

# --- Guards ---------------------------------------------------------------------------------------------------

function IsOfficerPc([string]$addonsDir) {
  # The officer PC gets UW_Guild from the bot's Update-Bot.cmd, a build ahead of the site: never touch it there.
  if (Test-Path -LiteralPath (Join-Path $addonsDir "$ADDON\.uw-local-tests.txt")) { return $true }
  $docs = [Environment]::GetFolderPath("MyDocuments")
  if ($docs -and (Test-Path -LiteralPath (Join-Path $docs "Unbothered-Warlords\bot\running.txt"))) { return $true }
  return $false
}

function GameRunning([string]$addonsDir) {
  $forever = Split-Path -Parent (Split-Path -Parent $addonsDir)
  try {
    foreach ($p in Get-Process -ErrorAction SilentlyContinue) {
      $path = $null
      try { $path = $p.Path } catch { }
      if ($path -and $path.StartsWith($forever + "\", [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
  } catch { }
  return $false
}

# --- Files ------------------------------------------------------------------------------------------------------

function IsLink([string]$p) {
  try { return [bool]((Get-Item -LiteralPath $p -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) } catch { return $false }
}

function SafeRemove([string]$p) {
  # Windows PowerShell 5.1's Remove-Item -Recurse empties a junction's target: remove a link itself, never descend.
  if (-not (Test-Path -LiteralPath $p)) { return }
  $item = Get-Item -LiteralPath $p -Force
  if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
    if ($item.PSIsContainer) { [IO.Directory]::Delete($p, $false) } else { [IO.File]::Delete($p) }
    return
  }
  if ($item.PSIsContainer) {
    foreach ($c in [IO.Directory]::GetFileSystemEntries($p)) { SafeRemove $c }
    [IO.Directory]::Delete($p, $false)
  } else {
    $item.Attributes = [IO.FileAttributes]::Normal
    [IO.File]::Delete($p)
  }
}

function MarkToc([string]$addonsDir, [bool]$on) {
  # "## X-UW-Updater: <date>" in the installed TOC tells UW_Guild in game that this copy updates itself (Onboarding.lua
  # hasUpdater): its newer-version line then needs no link, and it never suggests this setup.
  $toc = Join-Path $addonsDir "$ADDON\$ADDON.toc"
  if (-not (Test-Path -LiteralPath $toc) -or (IsLink (Join-Path $addonsDir $ADDON))) { return }
  $out = New-Object System.Collections.Generic.List[string]
  $placed = -not $on
  foreach ($line in [IO.File]::ReadAllLines($toc)) {
    if ($line -match '^\s*##\s*X-UW-Updater\s*:') { continue }
    $out.Add($line)
    if (-not $placed -and $line -match '^\s*##\s*Version\s*:') {
      $out.Add("## X-UW-Updater: " + [DateTime]::UtcNow.ToString("yyyy-MM-dd")); $placed = $true
    }
  }
  if (-not $placed) { $out.Insert(0, "## X-UW-Updater: " + [DateTime]::UtcNow.ToString("yyyy-MM-dd")) }
  [IO.File]::WriteAllText($toc, ($out -join "`r`n") + "`r`n", (New-Object Text.UTF8Encoding($false)))
}

# --- Download and check ---------------------------------------------------------------------------------------

function Fetch([string]$name, [string]$dest) {
  # From the site (https only), or from a folder (tests). No cache: a fresh deploy shows up at once.
  if ($Source -match '^https://') {
    $url = $Source.TrimEnd('/') + "/" + $name + "?t=" + [DateTime]::UtcNow.Ticks
    # No redirects: .NET Framework would follow one from https to http.
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $dest -Headers @{ "Cache-Control" = "no-cache" } -TimeoutSec 60 -MaximumRedirection 0
  } elseif ($Source -match '^[a-z]+://') {
    throw "Only https:// sources are allowed, not $Source"
  } else {
    Copy-Item -LiteralPath (Join-Path $Source $name) -Destination $dest -Force
  }
}

function CheckEntries($zip, [string]$want) {
  # Every entry must be a WoW addon file inside UW_Guild/, with no way out of it (zip-slip).
  $total = [int64]0
  $toc = $null
  foreach ($e in $zip.Entries) {
    $n = $e.FullName -replace '\\', '/'
    if ($n -notlike "$ADDON/*") { throw "unexpected entry $n" }
    if ($n -match '(^|/)\.\.(/|$)' -or $n -match ':' -or $n.StartsWith('/')) { throw "unsafe entry $n" }
    if ($n.EndsWith('/')) { continue }
    $ext = [IO.Path]::GetExtension($n).ToLowerInvariant()
    if ($SHIP -notcontains $ext) { throw "not an addon file: $n" }
    $total += $e.Length
    if ($n -eq "$ADDON/$ADDON.toc") { $toc = $e }
  }
  if ($total -gt $MAX_UNPACKED) { throw "the zip unpacks to more than 60 MB" }
  if (-not $toc) { throw "no $ADDON.toc in the zip" }
  $r = New-Object IO.StreamReader($toc.Open())
  try { $text = $r.ReadToEnd() } finally { $r.Dispose() }
  $m = [regex]::Match($text, '(?m)^\s*##\s*Version\s*:\s*(\S+)')
  if (-not $m.Success -or $m.Groups[1].Value -ne $want) { throw "the zip's version doesn't match version.json" }
}

function Unpack($zip, [string]$dest) {
  $root = [IO.Path]::GetFullPath($dest).TrimEnd('\') + "\"
  foreach ($e in $zip.Entries) {
    $rel = ($e.FullName -replace '/', '\').Substring($ADDON.Length + 1)
    if (-not $rel) { continue }
    $target = [IO.Path]::GetFullPath((Join-Path $dest $rel))
    if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw "unsafe entry $($e.FullName)" }
    if ($e.FullName.EndsWith('/')) { New-Item -ItemType Directory -Path $target -Force | Out-Null; continue }
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    $in = $e.Open()
    try {
      $out = [IO.File]::Create($target)
      try { $in.CopyTo($out) } finally { $out.Dispose() }
    } finally { $in.Dispose() }
  }
}

function Update([string]$addonsDir) {
  # Returns "updated", "current" or "newer". Throws with a reason when it can't.
  if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
  $vfile = Join-Path $StateDir "version.json"
  Fetch "version.json" $vfile
  $raw = [IO.File]::ReadAllBytes($vfile)
  if ($PUBKEY) {
    $sfile = Join-Path $StateDir "version.json.sig"
    try { Fetch "version.json.sig" $sfile } catch { throw "the site's version.json has no signature" }
    $ok = $false
    try {
      $sig = [Convert]::FromBase64String(([IO.File]::ReadAllText($sfile)).Trim())
      $rsa = New-Object Security.Cryptography.RSACryptoServiceProvider
      try { $rsa.FromXmlString($PUBKEY); $ok = $rsa.VerifyData($raw, "SHA256", $sig) } finally { $rsa.Dispose() }
    } catch { $ok = $false }
    if (-not $ok) { throw "the site's version.json signature doesn't match the guild's key" }
  }
  $info = [Text.Encoding]::UTF8.GetString($raw) | ConvertFrom-Json
  $want = [string]$info.version
  if (-not [regex]::IsMatch($want, $VERSION_RX)) { throw "version.json has no valid version" }
  if ([string]$info.zip -ne "$ADDON.zip") { throw "version.json names an unexpected file" }
  $sha = ([string]$info.zip_sha256).ToLowerInvariant()
  if ($sha -notmatch '^[0-9a-f]{64}$') { throw "version.json has no valid SHA-256" }
  $bytes = [int64]$info.zip_bytes
  if ($bytes -le 0 -or $bytes -gt $MAX_ZIP) { throw "version.json has an unexpected size" }

  $here = Join-Path $addonsDir $ADDON
  $have = TocVersion (Join-Path $here "$ADDON.toc")
  $haveKey = VersionKey $have
  $wantKey = VersionKey $want
  if ($have -eq $want) { return "current" }
  if ($haveKey -gt $wantKey) { return "newer" }

  $zipFile = Join-Path $StateDir "$ADDON.zip"
  Fetch "$ADDON.zip" $zipFile
  $len = (Get-Item -LiteralPath $zipFile).Length
  if ($len -ne $bytes) { throw "the download is $len bytes, version.json says $bytes" }
  $got = (Get-FileHash -LiteralPath $zipFile -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($got -ne $sha) { throw "the download's SHA-256 doesn't match version.json" }

  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $interface = Split-Path -Parent $addonsDir
  $stage = Join-Path $interface "$ADDON.uw-new"     # outside AddOns, same drive: the swap is two renames
  $backup = Join-Path $interface "$ADDON.uw-old"
  if (IsLink $here) { throw "AddOns\$ADDON is a link to another folder; replace it by hand" }
  foreach ($d in @($stage, $backup)) { SafeRemove $d }
  $zip = [IO.Compression.ZipFile]::OpenRead($zipFile)
  try {
    CheckEntries $zip $want
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    Unpack $zip $stage
  } finally { $zip.Dispose() }
  if (-not (Test-Path -LiteralPath $addonsDir)) { New-Item -ItemType Directory -Path $addonsDir -Force | Out-Null }
  $moved = $false
  try {
    if (Test-Path -LiteralPath $here) { Move-Item -LiteralPath $here -Destination $backup; $moved = $true }
    Move-Item -LiteralPath $stage -Destination $here
  } catch {
    if ($moved -and -not (Test-Path -LiteralPath $here)) { Move-Item -LiteralPath $backup -Destination $here }
    SafeRemove $stage
    throw "couldn't swap the addon folder (is a file in it open?): $($_.Exception.Message)"
  }
  try { SafeRemove $backup } catch { }
  Remove-Item -LiteralPath $zipFile -Force -ErrorAction SilentlyContinue
  return "updated"
}

# --- Automatic updates ------------------------------------------------------------------------------------------

function Shortcut([string]$lnk, [string]$target, [string]$arguments, [string]$what) {
  $sh = New-Object -ComObject WScript.Shell
  $s = $sh.CreateShortcut($lnk)
  $s.TargetPath = $target
  $s.Arguments = $arguments
  $s.WorkingDirectory = $StateDir
  $s.Description = $what
  $s.Save()
}

function Schedule {
  $ps = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
  $run = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$SCRIPT_COPY`" -Quiet"
  # conhost --headless (Windows 10 2004 and later) runs it with no window at all, so a check never pulls
  # focus from a game; older Windows gets a hidden PowerShell window instead.
  $exe = $ps
  $arg = $run
  $conhost = Join-Path $env:SystemRoot "System32\conhost.exe"
  if ([Environment]::OSVersion.Version.Build -ge 19041 -and (Test-Path -LiteralPath $conhost)) {
    $exe = $conhost
    $arg = "--headless `"$ps`" $run"
  }
  try {
    $action = New-ScheduledTaskAction -Execute $exe -Argument $arg -WorkingDirectory $StateDir
    # Daily at midnight, repeated hourly for the day (what Task Scheduler's own "Repeat every 1 hour" makes);
    # StartWhenAvailable runs a check missed while the PC was off as soon as it's back.
    $trigger = New-ScheduledTaskTrigger -Daily -At "00:00"
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At "00:00" -RepetitionInterval (New-TimeSpan -Hours 1) -RepetitionDuration (New-TimeSpan -Days 1)).Repetition
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    Register-ScheduledTask -TaskName $TASK -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description "Keeps the UW_Guild WoW addon up to date from unbotheredwarlords.github.io. Installs only while WoW Forever is closed." -Force | Out-Null
  } catch {
    # Older Windows or a locked-down ScheduledTasks module: the classic tool does the same, without catch-up runs.
    & schtasks.exe /Create /F /SC HOURLY /TN $TASK /TR "`"$exe`" $arg" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Windows didn't accept the hourly update task" }
  }
  if (-not (Test-Path -LiteralPath $MENU)) { New-Item -ItemType Directory -Path $MENU -Force | Out-Null }
  $base = "-NoProfile -ExecutionPolicy Bypass -File `"$SCRIPT_COPY`""
  Shortcut (Join-Path $MENU "Update UW Guild addon now.lnk") $ps $base "Check for a new UW_Guild and install it now"
  Shortcut (Join-Path $MENU "Stop automatic updates.lnk") $ps "$base -Remove" "Stop updating UW_Guild automatically (the addon stays)"
}

function Unschedule {
  try { Unregister-ScheduledTask -TaskName $TASK -Confirm:$false -ErrorAction Stop } catch { & schtasks.exe /Delete /F /TN $TASK 2>$null | Out-Null }
  try { SafeRemove $MENU } catch { }
}

# --- Main -------------------------------------------------------------------------------------------------------

if (-not $Quiet) { Write-Host "UW Guild addon setup" -ForegroundColor Cyan; Write-Host "" }

if ($Remove) {
  $cfg = ReadConfig
  if ($cfg -and $cfg.addons) { try { MarkToc ([string]$cfg.addons) $false } catch { } }
  Unschedule
  foreach ($f in @($SCRIPT_COPY, $CONFIG, (Join-Path $StateDir "version.json"), (Join-Path $StateDir "$ADDON.zip"))) {
    if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
  }
  Log "automatic updates removed"
  Say "Automatic updates are off. The addon stays in WoW; to remove it too, delete the UW_Guild folder in AddOns."
  Done 0
}

$dir = ResolveAddOns
if (-not $dir) { Fail "Couldn't find WoW Forever. Install it with the Battle.net app first, or whisper an officer in game." }
$dir = [IO.Path]::GetFullPath($dir)
if ((Split-Path -Leaf $dir) -ine "AddOns" -or (Split-Path -Leaf (Split-Path -Parent $dir)) -ine "Interface") {
  Fail "$dir isn't an Interface\AddOns folder."
}
if (-not $TestRun -and (IsOfficerPc $dir)) {
  Log "officer PC: skipped (Update-Bot.cmd installs UW_Guild here)"
  Say "This PC gets UW_Guild from the bot's Update-Bot.cmd. Nothing changed."
  Done 0
}
Say "WoW Forever: $(Split-Path -Parent (Split-Path -Parent $dir))"

if ($Quiet -and (GameRunning $dir)) { Log "WoW is running: checked later"; exit 0 }
if (-not $Quiet -and (GameRunning $dir)) {
  Say "WoW Forever is open. Close it first (an update while it runs would load half of it), then run this again."
  Done 1
}

try { $result = Update $dir } catch { Fail ("Couldn't update UW_Guild: " + $_.Exception.Message + ". Nothing changed; it tries again within the hour.") }
$now = TocVersion (Join-Path $dir "$ADDON\$ADDON.toc")
Log "$result $now ($dir)"
switch ($result) {
  "updated" { Say "UW_Guild $now is installed." }
  "current" { Say "UW_Guild $now is already the newest." }
  "newer"   { Say "This PC has a newer UW_Guild ($now) than the site. Nothing changed." }
}

# Automatic updates, on by default (Louis, 8 Nov 2026: one button, zero clicks for updates), said in plain words
# before the task is registered. Only from the downloaded setup: the Start menu's "Update now" runs the installed
# copy and leaves the task as it is. The task runs a static copy of this file made here; nothing ever replaces it
# from the network, so a new setup is always a new download someone chose to run.
$fromCopy = $PSCommandPath -and ([IO.Path]::GetFullPath($PSCommandPath) -eq [IO.Path]::GetFullPath($SCRIPT_COPY))
$cfg = ReadConfig
$auto = [bool]($cfg -and $cfg.auto)
# Only with the signing key pinned: an hourly task without it would carry one bad site push to every install.
if (-not $PUBKEY -and -not $NoSchedule -and -not $Quiet -and -not $fromCopy) {
  Say "To update later, download and run this setup again."
} elseif (-not $NoSchedule -and -not $Quiet -and -not $fromCopy -and $env:UWSELF) {
  Say ""
  Say "Turning on automatic updates: a Windows task named ""$TASK"" checks unbotheredwarlords.github.io"
  Say "once an hour and, while WoW is closed, replaces only the UW_Guild addon folder with the newest one."
  Say "Stop it any time: Start menu, UW Guild addon, Stop automatic updates."
  try {
    if (-not (Test-Path -LiteralPath $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
    # A fresh write, not a copy: the copy carries no "downloaded from the internet" mark, and no other stream.
    [IO.File]::WriteAllText($SCRIPT_COPY, [IO.File]::ReadAllText($env:UWSELF), (New-Object Text.UTF8Encoding($false)))
    Schedule
    @{ addons = $dir; auto = $true } | ConvertTo-Json | Set-Content -LiteralPath $CONFIG -Encoding UTF8
    $auto = $true
    Log "automatic updates on"
    Say "Automatic updates: on."
  } catch {
    Log "schedule failed: $($_.Exception.Message)"
    Say "The addon is installed, but automatic updates couldn't be set up. Run this setup again later to update."
  }
}
if ($auto -and $result -ne "newer") { try { MarkToc $dir $true } catch { Log "couldn't mark the TOC: $($_.Exception.Message)" } }

if ($result -eq "updated" -and -not $Quiet) {
  Say ""
  Say "Next: start WoW. A welcome window opens when you log in."
  Say "Not there? On the character screen, click AddOns, tick UW Guild (and Load out of date AddOns if it asks)."
}
Done 0
