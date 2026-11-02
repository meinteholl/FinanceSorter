param(
  [Parameter(Mandatory=$true)] [string] $Version
)

$ErrorActionPreference = 'Stop'

function Update-File($path, $pattern, $replacement) {
  if (-not (Test-Path $path)) { Write-Error "File not found: $path"; exit 1 }
  $text = Get-Content $path -Raw
  # Match first, compare second. Judging by "the replace changed nothing"
  # makes a re-run with the version already in the file look like a broken
  # pattern - which is exactly the state a release is in when the bump was
  # already done by hand.
  if (-not [regex]::IsMatch($text, $pattern)) { Write-Error "Pattern not matched in $path : $pattern"; exit 1 }
  $new = [regex]::Replace($text, $pattern, $replacement, 1)
  if ($new -ne $text) { [System.IO.File]::WriteAllText($path, $new) }
}

Update-File '.\src-tauri\tauri.conf.json' '"version":\s*"[^"]+"' ('"version": "' + $Version + '"')
Update-File '.\src-tauri\Cargo.toml'      '(?m)^version\s*=\s*"[^"]+"' ('version = "' + $Version + '"')

Write-Host "[deploy] version set to $Version"
