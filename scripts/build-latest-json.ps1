param(
  [Parameter(Mandatory=$true)] [string] $Version,
  [Parameter(Mandatory=$true)] [string] $Notes,
  [Parameter(Mandatory=$true)] [string] $SigFile,
  [Parameter(Mandatory=$true)] [string] $DownloadUrl,
  [Parameter(Mandatory=$true)] [string] $OutFile
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $SigFile)) { Write-Error "Signature file not found: $SigFile"; exit 1 }

$sig  = (Get-Content $SigFile -Raw).Trim()
$now  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

$manifest = [ordered]@{
  version   = $Version
  notes     = $Notes
  pub_date  = $now
  platforms = [ordered]@{
    'windows-x86_64' = [ordered]@{
      signature = $sig
      url       = $DownloadUrl
    }
  }
}

$json = $manifest | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText($OutFile, $json)
Write-Host "[deploy] $OutFile written"
