<#
.SYNOPSIS
  Set up the AfterFlight standalone app.

.DESCRIPTION
  Installs in place, in this folder. It deliberately does not create a second
  tree: the sessions, clips, events and EFB package all stay where they are.

  By default this only CHECKS things and reports. The two changes that touch
  the system - starting with Windows, and a Start Menu shortcut - happen only
  when you ask for them by switch.

.PARAMETER EnableAutostart
  Add the tray to HKCU Run so it starts with Windows. Remove with -DisableAutostart.

.PARAMETER DisableAutostart
  Remove the tray from HKCU Run.

.PARAMETER CreateShortcut
  Put an "AfterFlight" shortcut in the Start Menu.

.PARAMETER ResolveDll
  Re-resolve native\SimConnect_internal.dll from the installed sim. Do this
  after a sim update (section 15, risk 2).

.EXAMPLE
  .\install.ps1
  .\install.ps1 -EnableAutostart -CreateShortcut
  .\install.ps1 -ResolveDll
#>
[CmdletBinding()]
param(
  [switch]$EnableAutostart,
  [switch]$DisableAutostart,
  [switch]$CreateShortcut,
  [switch]$ResolveDll
)

$ErrorActionPreference = "Stop"
$Base = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$RunValue = "AfterFlight"
# If this app is ever renamed, put the previous autostart value and shortcut
# name here. Both are migrated below, so a rename neither duplicates the tray
# at logon nor leaves a dead Start Menu entry behind.
$LegacyRunValues = @()
$LegacyShortcuts = @()

function Say($msg)  { Write-Host $msg }
function Ok($msg)   { Write-Host "  [ok]   $msg"   -ForegroundColor Green }
function Warn($msg) { Write-Host "  [warn] $msg"   -ForegroundColor Yellow }
function Bad($msg)  { Write-Host "  [FAIL] $msg"   -ForegroundColor Red }

Say ""
Say "AfterFlight - standalone install"
Say "  tree: $Base"
Say ""

# ---------------------------------------------------------------- python
Say "Python"
$py = $null
foreach ($cand in @("py", "python")) {
  try {
    $v = & $cand -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $v) { $py = $cand; Ok "$cand -> Python $v"; break }
  } catch { }
}
if (-not $py) { Bad "No Python found on PATH. Install Python 3.10+ and re-run."; exit 1 }

$pyExe = & $py -c "import sys; print(sys.executable)"
$pywExe = Join-Path (Split-Path -Parent $pyExe) "pythonw.exe"
if (Test-Path $pywExe) { Ok "pythonw.exe present (tray runs without a console)" }
else { Warn "pythonw.exe not found; the tray will show a console window" }

# ---------------------------------------------------------------- deps
Say ""
Say "Dependencies"
$deps = @{
  "SimConnect" = "required - sampling, clips (pip install SimConnect==0.4.26)";
  "PIL"        = "optional - baked map PNGs (pip install Pillow)";
}
foreach ($mod in $deps.Keys) {
  & $py -c "import $mod" 2>$null
  if ($LASTEXITCODE -eq 0) { Ok "$mod" }
  elseif ($mod -eq "PIL") { Warn "$mod missing - $($deps[$mod]); maps will draw in the page only" }
  else { Bad "$mod missing - $($deps[$mod])" }
}

# ---------------------------------------------------------------- files
Say ""
Say "Application files"
$required = @("watcher.py", "logbook_build.py", "mapbake.py", "passenger.py",
              "tray.py", "logbook.html", "logbook.js", "replay.js")
$missing = @()
foreach ($f in $required) {
  if (Test-Path (Join-Path $Base $f)) { Ok $f } else { Bad $f; $missing += $f }
}
if ($missing.Count) { Bad "Missing files; not safe to continue."; exit 1 }

foreach ($d in @("sessions", "sessions\clips", "sessions\maps", "native")) {
  $p = Join-Path $Base $d
  if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null; Ok "created $d" }
  else { Ok "$d" }
}

# ---------------------------------------------------------------- dll
Say ""
Say "Chase / ghost SimConnect DLL"
if ($ResolveDll) {
  Push-Location $Base
  try {
    & $py -c @"
import importlib.util, sys
spec = importlib.util.spec_from_file_location('w', r'$Base\watcher.py')
w = importlib.util.module_from_spec(spec); sys.modules['w'] = w
spec.loader.exec_module(w)
info = w.load_game_simconnect_dll()
missing = info.get('missing') or []
if info.get('ok'):
    print('RESOLVED', info.get('path'))
elif info.get('path'):
    print('UNRESOLVED', info.get('path'))
    print('  that file is there, but does not export:', ','.join(missing))
    print('  it is the wrong SimConnect_internal.dll - delete it and re-run')
else:
    print('UNRESOLVED - no SimConnect_internal.dll was found anywhere')
    print('  looked in: native, the installed MSFS package, and any running sim')
    print('  START MICROSOFT FLIGHT SIMULATOR, then run this command again.')
    print('  With the sim running the DLL is read out of its own process, which')
    print('  works whatever the install layout is.')
"@
  } finally { Pop-Location }
} else {
  $dll = Join-Path $Base "native\SimConnect_internal.dll"
  if (Test-Path $dll) {
    $sz = [math]::Round((Get-Item $dll).Length / 1KB)
    Ok "native\SimConnect_internal.dll present ($sz KB)"
    Say "         re-run with -ResolveDll after a sim update"
  } else {
    Warn "native\SimConnect_internal.dll missing - ghost and chase will be unavailable"
    Say  "         run: .\install.ps1 -ResolveDll   (with the sim installed)"
  }
}

# ---------------------------------------------------------------- autostart
Say ""
Say "Start with Windows"
$cmd = "`"$pywExe`" `"$(Join-Path $Base 'tray.py')`""
if (-not (Test-Path $pywExe)) { $cmd = "`"$pyExe`" `"$(Join-Path $Base 'tray.py')`"" }

# Carry a logon entry from the old product name across before reading state,
# so "currently ON" reflects the migrated value rather than reporting OFF.
foreach ($legacy in $LegacyRunValues) {
  $old = (Get-ItemProperty -Path $RunKey -Name $legacy -ErrorAction SilentlyContinue).$legacy
  if ($old) {
    New-ItemProperty -Path $RunKey -Name $RunValue -Value $cmd -PropertyType String -Force | Out-Null
    Remove-ItemProperty -Path $RunKey -Name $legacy -ErrorAction SilentlyContinue
    Ok "migrated autostart: $legacy -> $RunValue"
  }
}

if ($EnableAutostart -and $DisableAutostart) {
  Bad "Pick one of -EnableAutostart / -DisableAutostart."; exit 1
}
if ($EnableAutostart) {
  New-ItemProperty -Path $RunKey -Name $RunValue -Value $cmd -PropertyType String -Force | Out-Null
  Ok "enabled: $cmd"
} elseif ($DisableAutostart) {
  Remove-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue
  Ok "disabled"
} else {
  $cur = (Get-ItemProperty -Path $RunKey -Name $RunValue -ErrorAction SilentlyContinue).$RunValue
  if ($cur) { Ok "currently ON  -> $cur" }
  else { Say "  currently OFF - enable with: .\install.ps1 -EnableAutostart" }
  Say  "         (the tray's own menu toggles this too)"
}

# ---------------------------------------------------------------- shortcut
Say ""
Say "Start Menu shortcut"
$lnk = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\AfterFlight.lnk"
foreach ($stale in $LegacyShortcuts) {
  $old = Join-Path ([Environment]::GetFolderPath("StartMenu")) "Programs\$stale"
  if (Test-Path $old) {
    Remove-Item $old -Force -ErrorAction SilentlyContinue
    Ok "removed stale shortcut: $stale"
  }
}
if ($CreateShortcut) {
  $sh = New-Object -ComObject WScript.Shell
  $s = $sh.CreateShortcut($lnk)
  $s.TargetPath = $(if (Test-Path $pywExe) { $pywExe } else { $pyExe })
  $s.Arguments = "`"$(Join-Path $Base 'tray.py')`""
  $s.WorkingDirectory = $Base
  $s.Description = "AfterFlight - tray, logbook, replay and chase"
  $s.Save()
  Ok "created: $lnk"
} elseif (Test-Path $lnk) { Ok "present: $lnk" }
else { Say "  not created - add with: .\install.ps1 -CreateShortcut" }

# ---------------------------------------------------------------- done
Say ""
Say "Ready."
Say "  Start the tray :  `"$pywExe`" `"$(Join-Path $Base 'tray.py')`""
Say "  Logbook        :  http://127.0.0.1:8742/"
Say "  Bound to 127.0.0.1 only. Close the browser and the tray window before flying in VR."
Say ""
