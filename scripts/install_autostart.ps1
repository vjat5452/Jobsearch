# Starts the JobFlow worker hidden (no windows) every time YOU log in to Windows.
# Run once:   powershell -ExecutionPolicy Bypass -File .\scripts\install_autostart.ps1
# Undo:       Unregister-ScheduledTask -TaskName "JobFlow" -Confirm:$false
$root = Split-Path $PSScriptRoot -Parent
$pythonw = (Get-Command pythonw -ErrorAction Stop).Source
$main = Join-Path $root "main.py"
$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$main`" auto" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = "PT1M"   # one minute after login
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "JobFlow" -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "JobFlow job worker (hidden)" -Force | Out-Null
Write-Host "Done. The worker starts hidden every time you log in to Windows."
Write-Host "Tip: Settings > System > Power > Sleep = Never while plugged in, or it pauses when the PC sleeps."
