# Развёртывание qDif Handler на VPS

Бот не слушает порты и не принимает входящие соединения — только сам ходит
в Telegram. База лежит на этом же сервере и тоже наружу не смотрит. Поэтому
сервер нужен самый маленький, а из открытого наружу остаётся один SSH.

Расплата за локальную базу — резервные копии теперь ваша забота, а не
провайдера. Это шаг 13, пропускать его нельзя.

Все команды ниже выполняются на сервере от `root`, если не указано иное.

## 1. Сервер

Hetzner Cloud → **Add Server**:

- **Location** — Nuremberg или Falkenstein, если работаете по Европе: база
  теперь на самом сервере, так что важна только близость к вам и к вашим
  работникам, а не к внешнему провайдеру БД.
- **Image** — Ubuntu 24.04 LTS.
- **Type** — самый младший: `CAX11` (ARM) дешевле, `CX22` (x86) привычнее.
  Оба тянут бота вместе с PostgreSQL с многократным запасом: нагрузка здесь —
  десятки запросов в час, а не в секунду. Порядка €4–5 в месяц вместе
  с платным IPv4-адресом.
- **SSH keys** — добавьте свой публичный ключ прямо при создании, тогда
  root-пароль не понадобится вовсе.

Подключение: `ssh root@<IP>`.

## 2. Базовая настройка

```bash
apt update && apt upgrade -y
apt install -y git python3-venv postgresql
```

Фаервол — наружу только SSH:

```bash
ufw allow OpenSSH
ufw --force enable
```

Автоматические обновления безопасности:

```bash
apt install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades
```

## 3. Пользователь бота

Бот работает под отдельным непривилегированным пользователем — не под root.

```bash
adduser --disabled-password --gecos "" qdif
mkdir -p /opt/qdif-bot
chown qdif:qdif /opt/qdif-bot
```

## 4. Доступ к приватному репозиторию

Репозиторий приватный, поэтому серверу нужен собственный ключ. Логин и пароль
от GitHub на сервере не нужны и храниться там не должны.

```bash
sudo -u qdif mkdir -p /home/qdif/.ssh
sudo -u qdif chmod 700 /home/qdif/.ssh
sudo -u qdif ssh-keygen -t ed25519 -C "qdif-vps" -f /home/qdif/.ssh/id_ed25519 -N ""
sudo -u qdif bash -c 'ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts'
cat /home/qdif/.ssh/id_ed25519.pub
```

Последняя команда напечатает публичный ключ. Скопируйте его в репозиторий:
**Settings → Deploy keys → Add deploy key**, галочку *Allow write access*
**не ставить** — серверу нужно только читать.

Deploy key действует ровно на один репозиторий, в отличие от ключа аккаунта.
Если сервер скомпрометируют, чужой доступ ограничится этим проектом.

Проверка и клонирование:

```bash
sudo -u qdif ssh -T git@github.com   # должно ответить: successfully authenticated
sudo -u qdif git clone git@github.com:svObb/qDif-handler.git /opt/qdif-bot
```

## 5. Окружение

```bash
sudo -u qdif python3 -m venv /opt/qdif-bot/.venv
sudo -u qdif /opt/qdif-bot/.venv/bin/pip install --upgrade pip
sudo -u qdif /opt/qdif-bot/.venv/bin/pip install -r /opt/qdif-bot/requirements.txt
```

## 6. База

PostgreSQL поставлен на шаге 2. Наружу он не смотрит: из коробки слушает только
localhost, а в ufw открыт один SSH. Пароль всё равно нужен — он отделяет бота
от других пользователей самой машины.

```bash
systemctl enable --now postgresql
```

Роль и база. Пароль сгенерируйте, а не придумывайте:

```bash
openssl rand -hex 24
```

```bash
sudo -u postgres psql -c "CREATE ROLE qdif LOGIN PASSWORD 'ПАРОЛЬ';"
sudo -u postgres psql -c "CREATE DATABASE qdif OWNER qdif;"
```

Проверка, что роль действительно ходит в базу по паролю, а не только по peer:

```bash
psql "postgresql://qdif:ПАРОЛЬ@localhost/qdif" -c "select version();"
```

Строка для `.env` — **без** `ssl=require`: соединение не покидает машину,
шифровать его незачем, а asyncpg на локальном сокете с этим параметром
просто откажется подключаться.

```
DATABASE_URL=postgresql+asyncpg://qdif:ПАРОЛЬ@localhost/qdif
```

Пароль из `openssl rand -hex` безопасен для URL. Если берёте свой — символы
`@ : / ? #` придётся кодировать процентами, иначе развалится разбор строки.

Отдельная переменная `DB_POOLED` нужна только для внешних пулов (pooled-хосты
Neon и Supabase): там приходится гасить кэш prepared statements, иначе asyncpg
получает `DuplicatePreparedStatementError`. Для локальной базы кэш, наоборот,
нужен, и код определяет это сам по хосту — трогать переменную не надо.

## 7. Файл .env

`.env` лежит в `.gitignore` и через git на сервер не попадёт — его переносят
руками. С локальной машины, из папки проекта:

```bash
scp .env root@<IP>:/tmp/qdif.env
```

На сервере:

```bash
mv /tmp/qdif.env /opt/qdif-bot/.env
chown qdif:qdif /opt/qdif-bot/.env
chmod 600 /opt/qdif-bot/.env
```

Права `600` обязательны: внутри лежат токен бота, код регистрации и пароль
к базе. Строки `BOT_TEST_TOKEN` и `TEST_DATABASE_URL` на сервере не нужны —
их можно удалить, боевой запуск идёт без флага `--test` и в тестовый режим
попасть не может.

## 8. Схема базы

```bash
sudo -u qdif bash -c 'cd /opt/qdif-bot && .venv/bin/alembic upgrade head'
```

`cd` обязателен: в `alembic.ini` пути заданы относительно рабочей директории,
из чужой папки alembic не найдёт ни миграции, ни `config.py` с `models.py`.

Шаг обязательно **до** запуска сервиса. Бот на пустой базе стартует зелёным —
до начала polling он в базу не ходит, — а потом молча глотает ошибку на каждом
сообщении: `systemctl status` показывает `active (running)`, пользователь
в Telegram не получает ничего. Такое отлаживать неприятно, поэтому схема
накатывается заранее.

Команда идемпотентна: если миграции уже накатаны, alembic ничего не сделает.

## 9. systemd

```bash
cp /opt/qdif-bot/deploy/qdif-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now qdif-bot
systemctl status qdif-bot
```

`enable` ставит бота в автозапуск после перезагрузки сервера, `--now`
запускает сразу. В юните прописан `Restart=always` с паузой 5 секунд,
а `StartLimitIntervalSec=0` снимает встроенное ограничение systemd, которое
иначе сдалось бы после пяти падений подряд и оставило бота лежать.

Живой лог:

```bash
journalctl -u qdif-bot -f
```

На сервере в `.env` оставьте `LOG_FILE=` пустым. Иначе бот пишет ещё и в
`bot.log` внутри каталога проекта — тот же поток, что уже идёт в journal, но
без ротации: файл растёт, пока не кончится диск. У journald ротация своя,
настраивать ничего не нужно.

## 10. Право на перезапуск для деплоя

Чтобы скрипт деплоя работал от `qdif`, а не от root, разрешаем этому
пользователю ровно четыре команды:

```bash
echo 'qdif ALL=(root) NOPASSWD: /usr/bin/systemctl restart qdif-bot, /usr/bin/systemctl start qdif-bot, /usr/bin/systemctl stop qdif-bot, /usr/bin/systemctl status qdif-bot' > /etc/sudoers.d/qdif-bot
chmod 440 /etc/sudoers.d/qdif-bot
visudo -c
usermod -aG systemd-journal qdif
```

`visudo -c` проверяет синтаксис. Если он ругнётся — удалите файл, иначе
сломается sudo целиком.

## 11. Обновление

Дальше каждый деплой — одна команда, от пользователя `qdif`:

```bash
sudo -u qdif /opt/qdif-bot/deploy/deploy.sh
```

Бит исполнения у скрипта приезжает из git — режим файла хранится в самом
коммите. Если команда вдруг ответит `command not found`, дело именно в нём,
проверяется так: `stat -c '%a %n' /opt/qdif-bot/deploy/deploy.sh` должно
показать `755`. Чинить надо в репозитории, а не через `chmod` на сервере:
на Linux git видит режим файла, и ручной `chmod` станет локальным изменением,
из-за которого `git pull --ff-only` внутри самого скрипта потом откажется
обновляться.

Скрипт подтягивает код, доставляет зависимости, накатывает миграции и
перезапускает сервис, после чего проверяет, что бот действительно поднялся,
и показывает хвост лога. Если не поднялся — печатает 40 строк журнала
и завершается с ошибкой.

Миграции в скрипте не случайны: без `alembic upgrade head` новый код приедет
на старую схему базы и упадёт на первом же запросе.

В `tools/` лежат разовые скрипты обслуживания — не миграции, деплой их не
запускает. Каждый по умолчанию только показывает, что собирается сделать,
и меняет данные лишь с флагом `--apply`:

```bash
sudo -u qdif bash -c 'cd /opt/qdif-bot && .venv/bin/python tools/renorm_phones.py'
```

`renorm_phones.py` пересчитывает `value_norm` у телефонов, записанных по старым
правилам нормализации. Прогнать его нужно один раз после обновления, иначе
старые номера не будут дедуплицироваться с новыми.

## 12. Два процесса на одном токене

Telegram не разрешает двум процессам держать long polling с одним токеном:
второй получает ошибку конфликта, а обновления начинают доставаться то
одному, то другому.

Правило простое: **боевой токен живёт только на сервере, локально всегда
`--test`**. Локальный запуск `python main.py` без флага при работающем
сервере сломает обоих.

## 13. Резервные копии

Пока база была в Neon, снимки делал провайдер. Теперь она лежит на том же диске,
что и бот: отвалившийся сервер или неудачный `DELETE` уносят всё, и
восстанавливать будет не из чего. Поэтому — ежедневный дамп.

```bash
mkdir -p /var/backups/qdif
chown postgres:postgres /var/backups/qdif
chmod 700 /var/backups/qdif
```

Скрипт: дамп в сжатом формате плюс удаление всего, что старше двух недель.

```bash
cat > /usr/local/bin/qdif-backup.sh <<'EOF'
#!/bin/sh
set -eu
umask 077
pg_dump -Fc qdif > "/var/backups/qdif/qdif-$(date +%F).dump"
find /var/backups/qdif -name 'qdif-*.dump' -mtime +14 -delete
EOF
chmod 755 /usr/local/bin/qdif-backup.sh
```

Раз в сутки, от пользователя `postgres` — ему база доступна по peer-авторизации,
пароль в скрипте не нужен:

```bash
echo '30 3 * * * postgres /usr/local/bin/qdif-backup.sh' > /etc/cron.d/qdif-backup
chmod 644 /etc/cron.d/qdif-backup
```

Проверить сразу, не дожидаясь ночи:

```bash
sudo -u postgres /usr/local/bin/qdif-backup.sh && ls -lh /var/backups/qdif
```

Дамп на том же диске спасает от испорченных данных, но не от потери сервера.
Раз в неделю забирайте свежий файл к себе:

```bash
scp root@<IP>:/var/backups/qdif/qdif-$(date +%F).dump .
```

Восстановление — на остановленном боте, иначе он будет писать в базу прямо
во время наката:

```bash
systemctl stop qdif-bot
sudo -u postgres pg_restore -d qdif --clean --if-exists /var/backups/qdif/qdif-2026-08-17.dump
systemctl start qdif-bot
```

Резервная копия, которую ни разу не разворачивали, — это не резервная копия.
Проверьте восстановление хотя бы один раз на тестовой базе.

## Если что-то не так

```bash
systemctl status qdif-bot
journalctl -u qdif-bot -n 50 --no-pager
```

**Сервис не поднимается** — почти всегда `.env`: не заполнена переменная
(бот назовёт какую) или у файла не тот владелец и процесс его не читает.

**В журнале `ConnectionRefusedError` или `password authentication failed`** —
дело в базе, а не в боте. Проверьте `systemctl status postgresql` и то, что
строка подключения работает руками:
`psql "postgresql://qdif:ПАРОЛЬ@localhost/qdif" -c 'select 1;'`.
Частая причина — спецсимвол в пароле, не закодированный процентами.

**Сервис зелёный, но бот молчит** — схема базы не накатана, шаг 8 пропущен.
В журнале будет `UndefinedTableError: relation "workers" does not exist`.
Лечится повторным `alembic upgrade head` и перезапуском.

**Сервис зелёный, но бот отвечает через раз** — где-то запущен второй процесс
с тем же токеном, см. шаг 12.
