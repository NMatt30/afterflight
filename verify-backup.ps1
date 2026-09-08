[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Backup)
$ErrorActionPreference = 'Stop'
function FileHash([string]$Path) {
  $reader = [IO.File]::OpenRead($Path)
  $sha = [Security.Cryptography.SHA256]::Create()
  try { return ([BitConverter]::ToString($sha.ComputeHash($reader))).Replace('-', '') }
  finally { $reader.Dispose(); $sha.Dispose() }
}
$root = (Resolve-Path -LiteralPath $Backup).Path
$manifest = Get-Content -LiteralPath (Join-Path $root 'manifest.json') -Raw | ConvertFrom-Json
if ($manifest.schema -ne 1) { throw 'Unsupported backup manifest.' }
foreach ($row in $manifest.files) {
  $path = [IO.Path]::GetFullPath((Join-Path $root $row.path))
  if (-not $path.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest path escapes backup.' }
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing backup file: $($row.path)" }
  if ((Get-Item -LiteralPath $path).Length -ne $row.bytes -or
      (FileHash $path) -ne $row.sha256) {
    throw "Backup content differs: $($row.path)"
  }
}
Write-Host "Verified $(@($manifest.files).Count) files by SHA-256. Consistency: $($manifest.consistency)"
