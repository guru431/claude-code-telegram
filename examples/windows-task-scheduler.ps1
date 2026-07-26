# Bot scheduled task registration.
#
# Security note: Windows Task Scheduler stores user passwords reversibly
# encrypted in the local vault (accessible to admins). There is no API to
# pass a SecureString to Register-ScheduledTask. To completely avoid storing
# a password, use SYSTEM principal or a gMSA. The current script registers
# under the calling user account and prompts for the password interactively,
# so the secret never appears in plain text inside the script source.
#
# -ProjectRoot overrides the auto-detected repository checkout.
# -DryRun prints the action that would be registered and exits without
# prompting for credentials or touching Task Scheduler.
param(
    [string]$ProjectRoot,
    [switch]$DryRun
)

$t_name  = 'claude-code-telegram'
$t_dir   = '\'

# Project root = the repository checkout, i.e. the PARENT of this examples/
# directory (.venv and src/ live there, not next to this script). Using
# $PSScriptRoot directly pointed the task at examples\ and every run failed
# with "Virtualenv not found".
$t_work = $ProjectRoot
if (-not $t_work) {
    if ($PSScriptRoot) { $t_work = Split-Path -Parent $PSScriptRoot }
    else { $t_work = (Get-Location).Path }
}

if (-not (Test-Path (Join-Path $t_work 'pyproject.toml')) -or
    -not (Test-Path (Join-Path $t_work 'src'))) {
    Write-Error "Not a project root: $t_work (expected pyproject.toml and src\). Pass -ProjectRoot <path>."
    exit 1
}

if (-not (Test-Path "$t_work\.venv\Scripts\python.exe")) {
    Write-Error "Virtualenv not found at $t_work\.venv. Run 'poetry install' first."
    exit 1
}

$t_exe = 'cmd'
$t_arg = "/c set CLAUDECODE= && pushd `"$t_work`" && .venv\Scripts\python.exe -m src.main >> `"$t_work\bot.log`" 2>&1 & popd"

$action   = New-ScheduledTaskAction -Execute $t_exe -Argument $t_arg -WorkingDirectory $t_work
$trigger  = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0 -DisallowHardTerminate -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

if ($DryRun) {
    Write-Host "Project root : $t_work"
    Write-Host "Execute      : $t_exe"
    Write-Host "Argument     : $t_arg"
    Write-Host "Working dir  : $($action.WorkingDirectory)"
    Write-Host "(dry run - nothing registered)"
    exit 0
}

$userName = "$env:USERDOMAIN\$env:USERNAME"
$cred = Get-Credential -Message "Password for scheduled task user $userName" -UserName $userName
if (-not $cred) {
    Write-Error "Credential required to register scheduled task."
    exit 1
}

# Register-ScheduledTask requires plain-text password (Windows API limitation).
# The password is stored reversibly encrypted in the Task Scheduler vault by Windows.
# We extract it from the SecureString only at the point of the API call so it does
# not linger in script variables.
try {
    $plainPwd = $cred.GetNetworkCredential().Password
    Register-ScheduledTask `
        -TaskName $t_name `
        -TaskPath $t_dir `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -RunLevel Highest `
        -User $cred.UserName `
        -Password $plainPwd `
        -Force | Out-Null
} finally {
    if ($plainPwd) {
        # Best-effort clear (string still pooled in .NET interner, but
        # we drop our reference at least).
        Remove-Variable -Name plainPwd -ErrorAction SilentlyContinue
    }
}

Start-Sleep -Seconds 5
