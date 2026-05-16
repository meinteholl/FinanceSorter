param(
  [Parameter(Mandatory=$true)] [string] $Version
)

$ErrorActionPreference = 'Stop'

function Update-File($path, $pattern, $replacement) {
  if (-not (Test-Path $path)) { Write-Error "File not found: $path"; exit 1 }
  $text = Get-Content $path -Raw
  $new  = [regex]::Replace($text, $pattern, $replacement, 1)
  if ($new -eq $text) { Write-Error "Pattern not matched in $path : $pattern"; exit 1 }
  [System.IO.File]::WriteAllText($path, $new)
}

Update-File '.\src-tauri\tauri.conf.json' '"version":\s*"[^"]+"' ('"version": "' + $Version + '"')
Update-File '.\src-tauri\Cargo.toml'      '(?m)^version\s*=\s*"[^"]+"' ('version = "' + $Version + '"')

Write-Host "[deploy] version set to $Version"
