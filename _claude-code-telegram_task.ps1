
$t_dir	= '\'
$t_name	= 'claude-code-telegram'
$t_work	= '\\srvmsk\dd$\Private2\_task\LLM\_VSC\claude-code-telegram'
$t_exe	= 'cmd'
$t_arg	= "/c set CLAUDECODE= && pushd `"$t_work`" && .venv\Scripts\python.exe -m src.main >> `"$t_work\bot.log`" 2>&1 & popd"
# $t_date	= '01/01/2020'
# $t_time	= '12:00'
$t_unit = 'onstart'
# $t_int	= '1'

$action  = New-ScheduledTaskAction -Execute $t_exe -Argument $t_arg -WorkingDirectory $t_work
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -DisallowHardTerminate -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName $t_name -TaskPath $t_dir -Action $action -Trigger $trigger -Settings $settings -RunLevel Highest -Force
Set-ScheduledTask -TaskPath $t_dir -TaskName $t_name -user "$env:userdomain\$env:username" -password $(Get-Credential -Credential $env:userdomain\$env:username).GetNetworkCredential().Password

sleep 5
