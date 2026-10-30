# Restricts this folder to your Windows account (and SYSTEM). It holds your site
# logins (.chrome-profile), API keys (secrets.txt), profile and job database.
# Run:   powershell -ExecutionPolicy Bypass -File .\scripts\lock_folder.ps1
# Undo:  icacls "<this folder>" /reset /T /C /Q
$me = "$env:USERDOMAIN\$env:USERNAME"
$dir = Split-Path $PSScriptRoot -Parent
icacls "$dir" /inheritance:r /grant:r "${me}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" /C /Q | Out-Null
icacls "$dir\*" /reset /T /C /Q | Out-Null
Write-Host "Locked: only $me (and SYSTEM) can open $dir"
Write-Host "An administrator can still take ownership - your Windows password is the real protection."
