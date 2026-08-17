# Развёртывание qDif Handler на VPS

Бот не слушает порты и не принимает входящие соединения — только сам ходит
в Telegram и в Neon. Поэтому сервер нужен самый маленький, а из открытого
наружу остаётся один SSH.

Все команды ниже выполняются на сервере от `root`, если не указано иное.

## 1. Сервер

Hetzner Cloud → **Add Server**:

- **Location** — Nuremberg или Falkenstein. База Neon стоит во Франкфурте
  (`eu-central-1`), немецкий дата-центр даёт минимальную задержку до неё.
- **Image** — Ubuntu 24.04 LTS.
- **Type** — самый младший: `CAX11` (ARM) дешевле, `CX22` (x86) привычнее.
  Оба тянут этого бота с многократным запасом. Порядка €4–5 в месяц вместе
  с платным IPv4-адресом.
- **SSH keys** — добавьте свой публичный ключ прямо при создании, тогда
  root-пароль не понадобится вовсе.

Подключение: `ssh root@<IP>`.

## 2. Базовая настройка

```bash
apt update && apt upgrade -y
apt install -y git python3-venv
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

## 6. Файл .env

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

## 7. Схема базы

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

## 8. systemd

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

## 9. Право на перезапуск для деплоя

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

## 10. Обновление

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

## 11. Два процесса на одном токене

Telegram не разрешает двум процессам держать long polling с одним токеном:
второй получает ошибку конфликта, а обновления начинают доставаться то
одному, то другому.

Правило простое: **боевой токен живёт только на сервере, локально всегда
`--test`**. Локальный запуск `python main.py` без флага при работающем
сервере сломает обоих.

## Если что-то не так

```bash
systemctl status qdif-bot
journalctl -u qdif-bot -n 50 --no-pager
```

**Сервис не поднимается** — почти всегда `.env`: не заполнена переменная
(бот назовёт какую) или у файла не тот владелец и процесс его не читает.
Реже — нет сети до Neon.

**Сервис зелёный, но бот молчит** — схема базы не накатана, шаг 7 пропущен.
В журнале будет `UndefinedTableError: relation "workers" does not exist`.
Лечится повторным `alembic upgrade head` и перезапуском.

**Сервис зелёный, но бот отвечает через раз** — где-то запущен второй процесс
с тем же токеном, см. шаг 11.
