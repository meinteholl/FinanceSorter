# Runs `cargo tauri build` with updater signing configured for a passwordless key.
#
# Windows PowerShell cannot represent an empty environment variable —
# `$env:X = ''` DELETES the variable, and Tauri then falls back to an
# interactive password prompt ("Decrypting updater signing key, expect a
# prompt for password"). The key is generated with `--ci` (empty password),
# so we set a genuinely empty TAURI_SIGNING_PRIVATE_KEY_PASSWORD through the
# Win32 API, which child processes inherit.
param(
    [Parameter(Mandatory = $true)][string]$KeyPath
)

$env:TAURI_SIGNING_PRIVATE_KEY = $KeyPath

Add-Type -Namespace Win32 -Name Env -MemberDefinition '[DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)] public static extern bool SetEnvironmentVariable(string name, string value);'
[Win32.Env]::SetEnvironmentVariable('TAURI_SIGNING_PRIVATE_KEY_PASSWORD', '') | Out-Null

cargo tauri build
exit $LASTEXITCODE
