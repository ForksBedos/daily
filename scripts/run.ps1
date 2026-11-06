# Запуск команды UzDigest для Планировщика заданий (docs/plan/stage-8-automation.md).
# Пример: powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run.ps1 run
# Команда выполняется в контейнере uzdigest (docker-compose.yml). Бот живёт там же и этим скриптом не запускается.
# Код завершения команды пишется в logs\scheduler.log и возвращается Планировщику.
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Command,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:PYTHONIOENCODING = 'utf-8'
# Вывод python PowerShell читает в кодировке консоли; у задачи Планировщика это cp866 — русский текст портится
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null
$log = Join-Path $root 'logs\scheduler.log'

function Test-Engine {
    docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
    $LASTEXITCODE -eq 0
}

function Wait-Engine([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Engine) { return $true }
        Start-Sleep -Seconds 5
    }
    Test-Engine
}

# Docker поднимается сам. После нештатной остановки файл-сокет в этой папке не даёт ему стартовать:
# папку отодвигаем, Docker создаст новую. Кнопку «Reset to factory defaults» не нажимать: она стирает образы и тома
function Start-Engine {
    if (Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue) {
        if (Wait-Engine 90) { return $true }
        Get-Process 'Docker Desktop', 'com.docker.backend' -ErrorAction SilentlyContinue | Stop-Process -Force
        Start-Sleep -Seconds 3
    }
    $secrets = Join-Path $env:LOCALAPPDATA 'docker-secrets-engine'
    if (Test-Path $secrets) { Rename-Item $secrets ('docker-secrets-engine.old-{0:yyyyMMddHHmmss}' -f (Get-Date)) }
    Start-Process (Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe')
    Wait-Engine 180
}

$started = Get-Date
# В PowerShell 5.1 при Stop первая строка stderr (WARNING из лога python) обрывает скрипт вместе с командой
$ErrorActionPreference = 'Continue'
if ((Test-Engine) -or (Start-Engine)) {
    docker compose up -d 2>&1 | Out-Null
    $output = & docker compose exec -T uzdigest python -m uzdigest $Command @Rest 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
} else {
    $output = 'Docker не запустился: команда не выполнена'
    $code = 1
}
$ErrorActionPreference = 'Stop'
$finished = Get-Date

$line = '{0:yyyy-MM-dd HH:mm:ss} {1} {2} -> код {3}, {4:N0} с' -f $started, $Command, ($Rest -join ' '), $code, ($finished - $started).TotalSeconds
Add-Content -Path $log -Value $line -Encoding utf8
$output | Out-String | Add-Content -Path $log -Encoding utf8
exit $code
