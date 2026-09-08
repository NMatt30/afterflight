<# Copy recordings with SHA-256 verification. -RequireQuiet excludes watcher and builder. #>
[CmdletBinding()]
param([string]$To, [string]$From = $PSScriptRoot,
      [switch]$Full, [switch]$Verify, [switch]$RequireQuiet)
$ErrorActionPreference = 'Stop'
function FileHash([string]$Path) {
  $reader = [IO.File]::OpenRead($Path)
  $sha = [Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($sha.ComputeHash($reader))).Replace('-', '') }
  finally { $reader.Dispose(); $sha.Dispose() }
}
$Base = (Resolve-Path -LiteralPath $From).Path
if (-not $To) { $To = Split-Path -Parent $Base }
$destinationRoot = [IO.Path]::GetFullPath($To)
if ($destinationRoot.Equals($Base, [StringComparison]::OrdinalIgnoreCase) -or
    $destinationRoot.StartsWith($Base + '\', [StringComparison]::OrdinalIgnoreCase)) {
  throw 'Choose a backup destination outside the source tree.'
}
$dest = Join-Path $destinationRoot ('afterflight-backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [Guid]::NewGuid().ToString('N').Substring(0,8))
$locks = @()
$quiet = $false
try {
  try {
    foreach ($name in @('watcher.lock', '.maintenance.lock')) {
      $stream = [IO.File]::Open((Join-Path $Base $name), [IO.FileMode]::OpenOrCreate,
                                [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
      try { $stream.Lock(0,1) } catch { $stream.Dispose(); throw }
      $locks += $stream
    }
    $quiet = $true
  } catch {
    foreach ($stream in $locks) { $stream.Dispose() }
    $locks = @()
    if ($RequireQuiet) { throw 'Watcher or maintenance is active. Stop the watcher and retry.' }
    Write-Warning 'Live copy: consistency across files is not guaranteed. HTTP availability does not prove recording stopped.'
  }
  $patterns = @('sessions\*.jsonl', 'sessions\*.meta.json', 'sessions\clips',
                'events.jsonl', 'excluded.json', 'flight_prefs.json', 'settings.json',
                'current.json', 'last_event.json', 'notify_state.json')
  if ($Full) {
    $patterns += @('sessions\maps', 'sessions\tilecache', 'sessions\detail',
                   'sessions\months', 'logbook.json', 'logbook.cache.json')
  }
  function SourceFiles {
    foreach ($pattern in $patterns) {
      foreach ($item in @(Get-ChildItem -Path (Join-Path $Base $pattern) -ErrorAction SilentlyContinue)) {
        if ($item.PSIsContainer) { Get-ChildItem -LiteralPath $item.FullName -Recurse -File }
        else { $item }
      }
    }
  }
  $files = @(SourceFiles | Where-Object { $_.Extension -ne '.tmp' } | Sort-Object FullName -Unique)
  New-Item -ItemType Directory -Path $dest | Out-Null
  $rows = @()
  $stable = $true
  foreach ($item in $files) {
    $rel = $item.FullName.Substring($Base.Length).TrimStart('\')
    $target = Join-Path $dest $rel
    New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
    $before = (FileHash $item.FullName)
    Copy-Item -LiteralPath $item.FullName -Destination $target
    $hash = (FileHash $target)
    $after = (FileHash $item.FullName)
    if ($before -ne $after -or $hash -ne $before) { $stable = $false }
    $rows += [ordered]@{path=$rel; bytes=(Get-Item -LiteralPath $target).Length; sha256=$hash; source_sha256=$before}
  }
  foreach ($row in $rows) {
    $source = Join-Path $Base $row.path
    if (-not (Test-Path -LiteralPath $source) -or
        (FileHash $source) -ne $row.source_sha256) { $stable = $false }
  }
  $afterNames = @(SourceFiles | Where-Object { $_.Extension -ne '.tmp' } | ForEach-Object FullName | Sort-Object -Unique)
  if (($files.FullName -join "`n") -ne ($afterNames -join "`n")) { $stable = $false }
  $status = if ($quiet -and $stable) { 'quiescent' } else { 'live-uncoordinated' }
  $manifest = [ordered]@{schema=1; created_at=[DateTime]::UtcNow.ToString('o');
    consistency=$status; source_stable=$stable; full=[bool]$Full; files=$rows}
  $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $dest 'manifest.json') -Encoding UTF8
  @"
AfterFlight backup
Consistency: $status

Verify before restoring:
  .\verify-backup.ps1 -Backup <this folder>

Restore into a separate, stopped AfterFlight tree first. Copy only the files
listed in manifest.json, preserving relative paths. Follow INSTALL.md to install
dependencies and stage the DLL, then rebuild:
  py -3 logbook_build.py --no-maps --offline --force
Do not copy over an actively recording installation.

Quiescent means the watcher and builder were excluded during copying. An
unrelated editor is outside those locks. A live-uncoordinated copy is not a
snapshot, even if the source hashes happened to remain stable.
"@ | Set-Content -LiteralPath (Join-Path $dest 'RESTORE.txt') -Encoding UTF8
  if ($Verify) {
    & (Join-Path $PSScriptRoot 'verify-backup.ps1') -Backup $dest
    if (-not $stable) { throw "Source changed during backup; see $dest\manifest.json" }
  }
  Write-Host "Backup: $dest"
  Write-Host "$($rows.Count) files; consistency: $status; source stable: $stable"
} finally {
  foreach ($stream in $locks) { $stream.Dispose() }
}
