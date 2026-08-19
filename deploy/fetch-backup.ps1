# Забирает свежий дамп с сервера в OneDrive. Запускается Планировщиком заданий
# ежедневно в 04:00 — после серверного дампа в 03:30.
#
# Дамп, лежащий только на сервере, не спасает от потери сервера. Раньше в README
# это был ручной еженедельный scp, то есть шаг, который не делается.
#
# Регистрация задачи (PowerShell от администратора, один раз):
#   $act = New-ScheduledTaskAction -Execute 'powershell.exe' `
#       -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\Users\user\Desktop\tobisite\deploy\fetch-backup.ps1'
#   Register-ScheduledTask -TaskName 'Tobisite backup fetch' -Action $act `
#       -Trigger (New-ScheduledTaskTrigger -Daily -At 04:00) `
#       -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
#
# -StartWhenAvailable обязателен: без него задача, выпавшая на спящий ноутбук,
# просто не выполнится, и копии молча перестанут появляться.
#
# Секретов здесь нет: ходим по тому же ssh-ключу, которым сервер и так
# администрируется.
#
# Ходим под tobisite, а не под root: ключа root на ноутбуке нет, и раньше задача
# из-за этого падала каждый день, а папка копий так и не появилась. Дампы
# снимает крон того же tobisite — pg_dump выполняется внутри контейнера, и права
# root для этого не нужны.
$ErrorActionPreference = 'Stop'

# $Host — встроенная переменная PowerShell, перезаписать её нельзя
$Server = 'tobisite@178.104.114.82'
$Dir    = "$env:USERPROFILE\OneDrive\tobisite-backups"
$Keep   = 14

New-Item -ItemType Directory -Force -Path $Dir | Out-Null

# Имя файла спрашиваем у сервера, а не собираем из даты: часовые пояса ноутбука
# и сервера не совпадают, и в полночь мы бы просили ещё не созданный дамп.
$name = ssh $Server 'ls -1t ~/backups/tobisite-*.dump 2>/dev/null | head -1'
if ($LASTEXITCODE -ne 0) { throw "ssh завершился с кодом $LASTEXITCODE" }
$name = ($name | Select-Object -First 1)
if (-not $name) { throw "на сервере нет ни одного дампа — проверьте crontab -l у tobisite" }
$name = $name.Trim()

$local = Join-Path $Dir (Split-Path $name -Leaf)
scp "${Server}:$name" $local
if ($LASTEXITCODE -ne 0) { throw "scp завершился с кодом $LASTEXITCODE" }

# Пустой или обрезанный файл выглядит как успешная копия — это худший вид
# отказа, потому что обнаруживается в момент восстановления.
if ((Get-Item $local).Length -lt 1024) { throw "дамп подозрительно мал, проверьте сервер" }

Get-ChildItem $Dir -Filter 'tobisite-*.dump' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep |
    Remove-Item -Force

# Дату отсюда достаточно проверять раз в месяц: отстала — задача не отрабатывает
Set-Content -Encoding utf8 -Path (Join-Path $Dir 'last-ok.txt') -Value (Get-Date -Format 'u')
Write-Host "OK $local"
