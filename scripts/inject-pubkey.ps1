param(
  [Parameter(Mandatory=$true)] [string] $PubFile,
  [Parameter(Mandatory=$true)] [string] $ConfFile
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $PubFile))  { Write-Error "Public key file not found: $PubFile";  exit 1 }
if (-not (Test-Path $ConfFile)) { Write-Error "Config file not found: $ConfFile";    exit 1 }

# Read the minisign public key, normalise line endings.
$pub = (Get-Content $PubFile -Raw).Trim() -replace "`r`n", "`n" -replace "`r", "`n"

$conf = Get-Content $ConfFile -Raw
if ($conf -notmatch 'REPLACE_ME_VIA_SetupGithub\.bat') {
  Write-Host '[setup] tauri.conf.json already has a pubkey -- leaving it.'
  exit 0
}

# Encode as a JSON string body: escape backslashes and quotes, turn LF into \n.
$jsonEscaped = $pub.Replace('\', '\\').Replace('"', '\"').Replace("`n", '\n')

# Literal string swap (no regex) — placeholder is a fixed string.
$new = $conf.Replace('REPLACE_ME_VIA_SetupGithub.bat', $jsonEscaped)

[System.IO.File]::WriteAllText($ConfFile, $new)
Write-Host '[setup] pubkey written.'
