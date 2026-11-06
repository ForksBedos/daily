# Удаляет задачи UzDigest из Планировщика заданий. Трогает только задачи с именем UzDigest-*.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\uninstall_tasks.ps1
$ErrorActionPreference = 'Stop'
$tasks = @(Get-ScheduledTask -TaskName 'UzDigest-*' -ErrorAction SilentlyContinue)
if ($tasks.Count -eq 0) {
    Write-Output 'Задач UzDigest нет'
    exit 0
}
foreach ($task in $tasks) {
    Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false
    Write-Output "Задача $($task.TaskName) удалена"
}
