# Регистрирует задачи UzDigest в Планировщике заданий Windows от текущего пользователя
# (docs/plan/stage-8-automation.md). Права администратора не нужны: задачи работают, пока
# пользователь вошёл в систему. Повторный запуск обновляет задачи.
#
# Время совпадает с блоком schedule в config/settings.yaml: меняя одно, меняйте и другое.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
#   ... -Test   — только пробная задача UzDigest-Test через 2 минуты: проверка, что политики разрешают задачи
#   ... -VoiceOnly — только посредник голосовых UzDigest-Voice, остальные задачи не трогает
param(
    [string]$DigestAt = '09:30',
    [string]$CollectFrom = '08:00',
    [string]$CollectTo = '20:00',
    [int]$CollectEveryHours = 2,
    [string]$Engine = (Join-Path $env:USERPROFILE 'Documents\Transcribe\publish\win-x64\Dictation.Engine.exe'),
    [switch]$Test,
    [switch]$VoiceOnly
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root 'scripts\run.ps1'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$weekdays = 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'

function New-UzAction([string]$Command) {
    New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" $Command" `
        -WorkingDirectory $root
}

# Ноутбук: запуск и от батареи, пропущенный запуск — при пробуждении, не больше часа, без двойных запусков
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

function Register-UzTask([string]$Name, $Trigger, [string]$Command, [string]$Description) {
    $task = New-ScheduledTask -Action (New-UzAction $Command) -Trigger $Trigger -Settings $settings `
        -Principal $principal -Description $Description
    Register-ScheduledTask -TaskName $Name -InputObject $task -Force | Out-Null
    Write-Output "Задача $Name зарегистрирована"
}

# Посредник голосовых работает на Windows, а не в контейнере: транскрибатор — программа Windows.
# Запускается при входе и живёт постоянно; упал — Планировщик поднимет его снова
function Register-UzVoice {
    $python = Join-Path $root '.venv\Scripts\pythonw.exe'
    if (-not (Test-Path $python)) { throw "Нет $python" }
    if (-not (Test-Path $Engine)) { throw "Нет транскрибатора: $Engine" }
    $action = New-ScheduledTaskAction -Execute $python `
        -Argument "-m uzdigest.voice_bridge --engine `"$Engine`"" -WorkingDirectory $root
    $always = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    $task = New-ScheduledTask -Action $action -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $user) `
        -Settings $always -Principal $principal -Description 'UzDigest: голосовые из бота в транскрибатор и обратно'
    Register-ScheduledTask -TaskName 'UzDigest-Voice' -InputObject $task -Force | Out-Null
    Write-Output 'Задача UzDigest-Voice зарегистрирована'
}

if ($VoiceOnly) {
    Register-UzVoice
    Start-ScheduledTask -TaskName 'UzDigest-Voice'
    exit 0
}

if ($Test) {
    $at = (Get-Date).AddMinutes(2)
    Register-UzTask 'UzDigest-Test' (New-ScheduledTaskTrigger -Once -At $at) 'status' `
        'Пробная задача: uzdigest status через 2 минуты. Удаляется scripts\uninstall_tasks.ps1'
    Write-Output ("Проверьте через 2 минуты: в logs\scheduler.log появится строка '{0:HH:mm} status'" -f $at)
    exit 0
}

Register-UzVoice

Register-UzTask 'UzDigest-Digest' `
    (New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $DigestAt) 'run' `
    'UzDigest: сбор новостей и дайджест дня в outputs\ (будни)'

# Сбор каждые N часов с CollectFrom до CollectTo: еженедельный триггер с повтором внутри дня
$collect = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $weekdays -At $CollectFrom
$span = [datetime]::ParseExact($CollectTo, 'HH:mm', $null) - [datetime]::ParseExact($CollectFrom, 'HH:mm', $null)
$repeat = New-ScheduledTaskTrigger -Once -At $CollectFrom `
    -RepetitionInterval (New-TimeSpan -Hours $CollectEveryHours) -RepetitionDuration $span.Add((New-TimeSpan -Minutes 1))
$collect.Repetition = $repeat.Repetition
Register-UzTask 'UzDigest-Collect' $collect 'collect' 'UzDigest: опрос лент между дайджестами (будни)'

# Telegram-бот живёт в контейнере (docker-compose.yml, restart: unless-stopped): отдельная задача ему не нужна
if (Get-ScheduledTask -TaskName 'UzDigest-Bot' -ErrorAction SilentlyContinue) {
    Disable-ScheduledTask -TaskName 'UzDigest-Bot' | Out-Null
    Write-Output 'Задача UzDigest-Bot отключена: бот работает в контейнере'
}

Get-ScheduledTask -TaskName 'UzDigest-*' | Get-ScheduledTaskInfo |
    Select-Object TaskName, NextRunTime | Format-Table -AutoSize
