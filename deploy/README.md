# Развёртывание qDif Handler на VPS

Бот не слушает порты и не принимает входящие соединения — только сам ходит
в Telegram. База лежит на этом же сервере, в контейнере рядом с ботом, и наружу
тоже не смотрит. Поэтому сервер нужен самый маленький, а из открытого наружу
остаётся один SSH.

Расплата за свою базу — резервные копии теперь ваша забота, а не провайдера.
Это шаг 13, пропускать его нельзя.

Все команды выполняются на сервере от `root`, если не указано иное.

## 1. Сервер

Hetzner Cloud → **Add Server**:

- **Location** — Nuremberg или Falkenstein, если работаете по Европе: база
  на самом сервере, так что важна только близость к вам и к вашим работникам.
- **Image** — Ubuntu 24.04 LTS.
- **Type** — самый младший, `CX22`. Нагрузка здесь десятки запросов в час,
  а не в секунду. Порядка €4–5 в месяц вместе с платным IPv4-адресом.
  **Архитектура должна быть x86_64**: образ собирается под `linux/amd64`,
  на ARM-тарифах (`CAX*`) он не запустится.
- **SSH keys** — добавьте свой публичный ключ прямо при создании.

Подключение: `ssh root@<IP>`.

## 2. Базовая настройка

```bash
apt update && apt upgrade -y
apt install -y git
curl -fsSL https://get.docker.com | sh
```

Фаервол — наружу только SSH:

```bash
ufw allow OpenSSH
ufw --force enable
```

Docker умеет обходить ufw, когда контейнер публикует порт: правила он пишет
в iptables напрямую, мимо ufw. У нас портов наружу не публикует никто —
ни бот, ни база, — поэтому конфликта нет. Если когда-нибудь добавите `ports:`,
помните об этом: порт окажется открыт в интернет, хотя ufw будет говорить обратное.

Автоматические обновления безопасности:

```bash
apt install -y unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades
```

## 3. Пользователь бота

```bash
adduser --disabled-password --gecos "" qdif
usermod -aG docker qdif
mkdir -p /opt/qdif-bot
chown qdif:qdif /opt/qdif-bot
```

⚠️ **Группа `docker` — это фактически права root.** Член группы запускает
контейнер с примонтированным `/` и читает или меняет на диске что угодно.
Отдельного правила sudo боту больше не нужно, но и барьера между `qdif` и root
теперь нет. Это осознанная плата за контейнеры; если она смущает, обратно
к systemd можно вернуться по истории git.

## 4. Доступ к приватному репозиторию

Код в образе, но сам репозиторий серверу всё равно нужен: из него берутся
`compose.yaml`, миграции и скрипты деплоя.

```bash
sudo -u qdif mkdir -p /home/qdif/.ssh
sudo -u qdif chmod 700 /home/qdif/.ssh
sudo -u qdif ssh-keygen -t ed25519 -C "qdif-vps" -f /home/qdif/.ssh/id_ed25519 -N ""
sudo -u qdif bash -c 'ssh-keyscan -t ed25519 github.com >> ~/.ssh/known_hosts'
cat /home/qdif/.ssh/id_ed25519.pub
```

Публичный ключ — в репозиторий: **Settings → Deploy keys → Add deploy key**,
галочку *Allow write access* **не ставить**, серверу нужно только читать.

```bash
sudo -u qdif ssh -T git@github.com   # должно ответить: successfully authenticated
sudo -u qdif git clone git@github.com:svObb/qDif-handler.git /opt/qdif-bot
```

## 5. Доступ к реестру образов

Образ собирается в GitHub Actions и лежит в GHCR. Пакет приватного репозитория
приватный, поэтому серверу нужен токен на чтение.

GitHub → Settings → Developer settings → Personal access tokens → **Tokens
(classic)** → Generate new token. Единственная галочка — `read:packages`.

```bash
sudo -iu qdif
echo '<ТОКЕН>' | docker login ghcr.io -u svObb --password-stdin
exit
```

Токен ляжет в `/home/qdif/.docker/config.json` и больше нигде не понадобится.

## 6. База

Отдельно ставить PostgreSQL не нужно — он приедет контейнером из
[compose.yaml](../compose.yaml). Нужен только пароль; сгенерируйте, а не придумывайте:

```bash
openssl rand -hex 24
```

Строку подключения задавать не надо: compose собирает `DATABASE_URL` сам
из `POSTGRES_PASSWORD` и подставляет боту. Пароль хранится в одном месте,
рассинхрона «в базе один, в URL другой» не бывает.

Порт наружу база не публикует. С хоста она доступна так:

```bash
sudo -u qdif bash -c 'cd /opt/qdif-bot && docker compose exec db psql -U qdif -d qdif'
```

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

Права `600` обязательны: внутри токен бота, код регистрации и пароль к базе.

Затем привести файл к серверному виду:

| Переменная | Что с ней делать |
|---|---|
| `POSTGRES_PASSWORD` | **добавить**, значение из шага 6 |
| `IMAGE_TAG` | **добавить** пустой, заполнится на шаге 9 |
| `DATABASE_URL` | можно удалить, compose её перекрывает |
| `LOG_FILE` | можно удалить, compose ставит пустое значение |
| `BOT_TEST_TOKEN`, `TEST_DATABASE_URL` | удалить, боевой запуск в тест уйти не может |

Сохраните боевой `.env` в менеджер паролей. Сервер может умереть, а собирать
конфиг по памяти — занятие на полдня.

### 7.1. Новая переменная

Деплой обновляет код, но не конфигурацию, и порядок здесь жёсткий:
**сначала переменная на сервере, потом код, который её читает.** `config.py`
читает окружение на импорте и завершает процесс, если обязательной переменной
нет, — бот упадёт на старте, а автодеплой увидит это и откатит выкат.

```bash
sudo -u qdif nano /opt/qdif-bot/.env
sudo -u qdif bash -c 'cd /opt/qdif-bot && docker compose up -d'
```

⚠️ Именно `up -d`, а не `restart`: `restart` перезапускает контейнер со старым
окружением, новые переменные из `.env` он не подхватит. `up -d` пересоздаёт
контейнер, увидев изменившийся конфиг.

Посмотреть, что в файле сейчас:

```bash
sudo -u qdif grep -v -e '^#' -e '^$' /opt/qdif-bot/.env
```

И держите [.env.example](../.env.example) в актуальном состоянии: добавили
переменную на сервере — добавьте ключ с пустым значением и в пример.

## 8. Схема базы

```bash
sudo -iu qdif
cd /opt/qdif-bot
docker compose up -d db
docker compose run --rm bot alembic upgrade head
```

Шаг обязательно **до** запуска бота. На пустой базе он стартует зелёным —
до начала polling в базу не ходит, — а потом молча глотает ошибку на каждом
сообщении: контейнер работает, пользователь в Telegram не получает ничего.

Команда идемпотентна: если миграции уже накатаны, alembic ничего не сделает.

## 9. Запуск

`IMAGE_TAG` при первом запуске проставляется руками — деплой берёт из него
точку отката и на пустом значении откажется работать.

```bash
sudo -iu qdif
cd /opt/qdif-bot
sed -i "s|^IMAGE_TAG=.*|IMAGE_TAG=$(git rev-parse HEAD)|" .env
docker compose up -d
docker compose ps
```

Образ для этого коммита должен быть уже собран — то есть коммит должен быть
в `main`, а workflow по нему пройти. Иначе `docker compose pull` не найдёт тег.

Автозапуск после перезагрузки даёт `restart: unless-stopped`, порядок «база
раньше бота» — `depends_on` с `condition: service_healthy`. Отдельного
systemd-юнита нет и не нужно.

Живой лог:

```bash
docker compose logs -f bot
```

## 10. Шпаргалка команд

Все выполняются от `qdif` из `/opt/qdif-bot`.

| Было (systemd) | Стало (docker) |
|---|---|
| `systemctl status qdif-bot` | `docker compose ps` |
| `journalctl -u qdif-bot -f` | `docker compose logs -f bot` |
| `journalctl -u qdif-bot -n 50` | `docker compose logs --tail 50 bot` |
| `systemctl restart qdif-bot` | `docker compose up -d` |
| `systemctl stop qdif-bot` | `docker compose stop` |
| `psql -U qdif qdif` | `docker compose exec db psql -U qdif -d qdif` |
| `.venv/bin/alembic upgrade head` | `docker compose run --rm bot alembic upgrade head` |
| `.venv/bin/python tools/x.py` | `docker compose run --rm bot python tools/x.py` |

Ротация логов у драйвера `json-file` задана в `compose.yaml`: 10 МБ на файл,
пять файлов. Без этих опций логи растут, пока не кончится диск, — у journald
ротация была из коробки, здесь её нужно задавать руками.

## 11. Обновление руками

Обычные правки кода приезжают сами, из GitHub Actions — это шаг 14. Ручной
скрипт нужен для одного случая: **выкат вместе с миграцией**. Автодеплой схему
не трогает и на неналитой миграции просто откажется ехать.

```bash
sudo -u qdif /opt/qdif-bot/deploy/deploy.sh
```

Скрипт подтягивает код, проставляет `IMAGE_TAG`, забирает образ из реестра,
снимает дамп базы, накатывает миграции, поднимает контейнер и проверяет,
что бот действительно стартовал.

Проверка «поднялся» считает строки `bot started` в логе, а не смотрит на то,
запущен ли контейнер. Причина в `restart: unless-stopped`: бот, падающий
на старте, перезапускается бесконечно и почти всё время выглядит живым.
Ноль таких строк — не поднялся, больше одной — цикл падений.

Бит исполнения у скрипта приезжает из git — режим файла хранится в коммите.
Если команда ответит `command not found`, проверьте:
`stat -c '%a %n' /opt/qdif-bot/deploy/deploy.sh` должно показать `755`.
Чинить в репозитории, а не через `chmod` на сервере: ручной `chmod` станет
локальным изменением, из-за которого `git pull --ff-only` внутри самого
скрипта потом откажется обновляться.

### 11.0. Разовые скрипты обслуживания

В `tools/` лежат скрипты, которые не миграции и деплой их не запускает. Каждый
по умолчанию только показывает, что собирается сделать, и меняет данные лишь
с флагом `--apply`:

```bash
sudo -u qdif bash -c 'cd /opt/qdif-bot && docker compose run --rm bot python tools/renorm_phones.py'
```

`renorm_phones.py` пересчитывает `value_norm` у телефонов, записанных по старым
правилам нормализации. Прогнать один раз обязательно, иначе старые номера
не будут дедуплицироваться с новыми.

### 11.1. Когда нужна миграция

Только если структурно поменялся `models.py`: новая колонка, таблица, тип,
индекс или ограничение. Правки в хендлерах, текстах и клавиатурах схему
не трогают.

Файл миграции делается **на своей машине**, после правки моделей:

```powershell
.venv\Scripts\alembic revision --autogenerate -m "добавил колонку X"
```

Сгенерированный файл в `alembic/versions/` обязательно прочитайте.
Автогенерация регулярно ошибается на переименованиях: она видит их как
«удалить старую колонку, создать новую», то есть как потерю данных.

### 11.2. Порядок наката

Порядок «код или схема раньше» зависит от изменения: добавление колонки старый
код просто не заметит, а удаление той, которую он ещё читает, его уронит.

| Изменение | Порядок |
|---|---|
| **Добавление** (колонка, таблица, индекс) | сначала миграция, потом код |
| **Удаление** (колонка, таблица) | сначала код, потом миграция |

`deploy.sh` делает и то и другое в одном заходе с коротким простоем, поэтому
для большинства случаев думать об этом не нужно. Дамп перед миграцией он
снимает сам, в `/tmp`.

Откат схемы:

```bash
sudo -u qdif bash -c 'cd /opt/qdif-bot && docker compose run --rm bot alembic downgrade -1'
```

Он вернёт структуру, но не удалённые данные: за них отвечает только дамп.

## 12. Два процесса на одном токене

Telegram не разрешает двум процессам держать long polling с одним токеном:
второй получает ошибку конфликта, а обновления достаются то одному, то другому.

Правило простое: **боевой токен живёт только на сервере, локально всегда
`--test`**. Локальный запуск `python main.py` без флага при работающем сервере
сломает обоих.

Локальная разработка остаётся на venv, без контейнеров: одному разработчику
на Windows контейнер пользы не добавит.

## 13. Резервные копии

База лежит в docker-volume на том же диске, что и бот: отвалившийся сервер или
неудачный `DELETE` уносят всё. Поэтому — ежедневный дамп на сервере плюс
ежедневная выгрузка копии к себе.

### 13.1. Дамп на сервере

```bash
mkdir -p /var/backups/qdif
chmod 700 /var/backups/qdif
```

```bash
cat > /usr/local/bin/qdif-backup.sh <<'EOF'
#!/bin/sh
set -eu
umask 077
docker exec qdif-db pg_dump -U qdif -Fc qdif > "/var/backups/qdif/qdif-$(date +%F).dump"
find /var/backups/qdif -name 'qdif-*.dump' -mtime +14 -delete
EOF
chmod 755 /usr/local/bin/qdif-backup.sh
```

`pg_dump` берётся из того же контейнера, что и сервер, — версии совпадают
по построению, и рассинхрона «клиент старше сервера» не бывает.

Раз в сутки, от root: доступ к базе теперь даёт docker, а не peer-авторизация.

```bash
echo '30 3 * * * root /usr/local/bin/qdif-backup.sh' > /etc/cron.d/qdif-backup
chmod 644 /etc/cron.d/qdif-backup
```

Проверить сразу, не дожидаясь ночи:

```bash
/usr/local/bin/qdif-backup.sh && ls -lh /var/backups/qdif
```

### 13.2. Копия вне сервера

Дамп на том же диске спасает от испорченных данных, но не от потери сервера.
Ручной еженедельный `scp` — шаг, который не делается, поэтому он заменён
ежедневной задачей на ноутбуке: [fetch-backup.ps1](fetch-backup.ps1) забирает
свежий файл в OneDrive, откуда копия уезжает в облако.

Впишите IP сервера в начало скрипта и зарегистрируйте задачу — PowerShell
от администратора, один раз:

```powershell
$act = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument '-NoProfile -ExecutionPolicy Bypass -File C:\Users\user\Desktop\qDif-handler\deploy\fetch-backup.ps1'
Register-ScheduledTask -TaskName 'qDif backup fetch' -Action $act `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 04:00) `
    -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

`-StartWhenAvailable` обязателен: без него задача, выпавшая на спящий ноутбук,
просто не выполнится, и копии молча перестанут появляться. Проверить руками:

```powershell
Start-ScheduledTask -TaskName 'qDif backup fetch'
```

Скрипт кладёт рядом `last-ok.txt` с датой последнего успеха. Раз в месяц
достаточно взглянуть на неё: отстала — задача не отрабатывает.

### 13.3. Восстановление

Копия, которую ни разу не разворачивали, статистически не работает. Проверьте
на одноразовом контейнере — боевую базу он не трогает:

```bash
docker run --rm -d --name pg-test \
    -e POSTGRES_USER=qdif -e POSTGRES_DB=qdif -e POSTGRES_PASSWORD=x postgres:16
sleep 5
docker exec -i pg-test pg_restore -U qdif -d qdif < /var/backups/qdif/qdif-$(date +%F).dump
docker exec pg-test psql -qAt -U qdif -d qdif -c 'select count(*) from leads'
docker rm -f pg-test
```

Настоящее восстановление — на остановленном боте, иначе он будет писать в базу
прямо во время наката:

```bash
cd /opt/qdif-bot
docker compose stop bot
docker exec -i qdif-db pg_restore -U qdif -d qdif --clean --if-exists < /var/backups/qdif/qdif-2026-08-17.dump
docker compose up -d
```

## 14. Автодеплой из GitHub Actions

Пуш в `main` сам доезжает до сервера. Перед выкатом код проходит проверку
(синтаксис, pyflakes, тесты, миграции на пустой базе, импорт всех модулей),
затем собирается образ и уезжает в GHCR с тегом-коммитом. Сервер этот образ
только забирает: запускается ровно то, что прошло проверки, а не пересобранное
на месте. Откат поэтому мгновенный — вернуть `IMAGE_TAG` и поднять контейнер.

**Миграции автодеплой не накатывает.** Код откатывается одной строкой, схема —
нет, и автооткат кода поверх накатившейся миграции дал бы рассинхрон схемы
и кода. При неналитой миграции выкат отказывается ехать **до** перезапуска:
контейнер остаётся на старом образе, а схему вы накатываете шагом 11.

Ключ, которым Actions ходит на сервер, ограничен одной командой. Даже если
секрет из GitHub утечёт, шелла он не даёт — только «выкати вот этот коммит».

### 14.1. Скрипт выката

Ставится **вне** `/opt/qdif-bot`: скрипт делает `git reset --hard`, то есть
переписал бы файл, который bash в этот момент ещё дочитывает.

```bash
sudo -u qdif git -C /opt/qdif-bot pull --ff-only
install -m 755 -o root -g root /opt/qdif-bot/deploy/ci-deploy.sh /usr/local/bin/qdif-deploy
```

Автообновления у копии нет. Если `deploy/ci-deploy.sh` в репозитории поменяется,
выкат напишет об этом предупреждение в лог Actions — тогда повторите `install`.

### 14.2. Ключ для Actions

Ключ одноразовый по назначению: приватная часть существует в единственном
экземпляре, и если она потерялась — не восстанавливают, а делают новый.

```bash
sudo -iu qdif
sed -i '/github-actions/d' ~/.ssh/authorized_keys   # снести следы прошлых попыток
ssh-keygen -t ed25519 -C "github-actions" -f ~/.ssh/gha -N ""
```

⚠️ **Ключ кладётся в `authorized_keys` только с ограничением.** Простое
`cat gha.pub >> authorized_keys` дало бы полный шелл: утёкший секрет из GitHub
означал бы `.env` с токеном бота, кодом доступа и паролем к базе плюс всю базу
лидов. С `command=` этот ключ умеет ровно одно — «выкати вот этот коммит»,
и присланное достаётся скрипту как текст, а не как команда.

```bash
printf 'command="/usr/local/bin/qdif-deploy",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,no-X11-forwarding %s\n' \
    "$(cat ~/.ssh/gha.pub)" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
grep -c '^command="/usr/local/bin/qdif-deploy"' ~/.ssh/authorized_keys   # должно быть 1
```

Приватный ключ — в секрет `SSH_PRIVATE_KEY`, **целиком**, вместе со строками
`-----BEGIN OPENSSH PRIVATE KEY-----` и `-----END …-----` и переносом в конце;
обрезанный GitHub не примет. Заодно сохраните его в менеджер паролей.

```bash
cat ~/.ssh/gha
```

Скопировали и сохранили — убирайте с сервера:

```bash
rm ~/.ssh/gha ~/.ssh/gha.pub
```

Отпечаток сервера для второго секрета — со своей машины, не с сервера:

```powershell
ssh-keyscan -t ed25519 <IP>
```

Без `SSH_KNOWN_HOSTS` Actions согласится на любой сервер, который ответит
по этому адресу.

### 14.3. Секреты в GitHub

**Settings → Secrets and variables → Actions → New repository secret**:

| Имя | Значение |
|---|---|
| `SERVER_HOST` | IP сервера |
| `SERVER_USER` | `qdif` |
| `SSH_PRIVATE_KEY` | содержимое `gha` целиком, вместе со строками `BEGIN`/`END` |
| `SSH_KNOWN_HOSTS` | строка из `ssh-keyscan` |

Пятого секрета для реестра не нужно: сборка логинится в GHCR встроенным
`GITHUB_TOKEN`, который раннер выдаёт сам.

### 14.4. Проверка

Первый запуск делайте кнопкой, а не пушем: **Actions → deploy → Run workflow**.

Что должно быть в логе шага «выкат»:

```
==> было <старый sha>
==> ставим <новый sha>
==> образ
==> схема
==> запуск
==> бот работает на <новый sha>
```

Проверить, что ключ действительно ограничен, — с машины, где ещё лежит `gha`:

```bash
ssh -i gha qdif@<IP> 'cat /opt/qdif-bot/.env'
```

Ответом должно быть `!! не коммит: cat ...`, а не содержимое файла.

### 14.5. Если выкат откатился

Красная job и письмо от GitHub. В логе — 40 строк лога контейнера с причиной
падения и строка `==> откат на <sha>`. Бот при этом уже работает на прежнем
образе.

Чинить нужно **вперёд**: новый коммит в `main` запустит новый выкат. Не делайте
на сервере `git pull` руками — после отката локальный `main` отстаёт от
`origin/main`, и `pull` вернёт ровно тот код, который только что не поднялся.

## Если что-то не так

```bash
cd /opt/qdif-bot
docker compose ps
docker compose logs --tail 50 bot
```

**Контейнер бота перезапускается по кругу** — почти всегда `.env`: не заполнена
переменная (бот назовёт какую) или у файла не тот владелец.

**`password authentication failed` или `Connection refused` в логе** — дело
в базе. `docker compose ps` должен показывать у `db` состояние `healthy`;
если нет, смотрите `docker compose logs db`. Частая причина — `POSTGRES_PASSWORD`
поменяли в `.env` после того, как volume уже создан: пароль в базе остался
прежним, меняется он через `ALTER ROLE`, а не правкой переменной.

**Бот работает, но молчит** — схема базы не накатана, шаг 8 пропущен.
В логе будет `UndefinedTableError: relation "workers" does not exist`.

**Бот отвечает через раз** — где-то запущен второй процесс с тем же токеном,
см. шаг 12.

**Выкат падает с `Permission denied (publickey)`** — секрет `SSH_PRIVATE_KEY`
скопирован не целиком (строки `BEGIN`/`END` обязательны) или публичный ключ
не попал в `authorized_keys`:
`grep -c qdif-deploy /home/qdif/.ssh/authorized_keys` должно вернуть `1`.

**Выкат падает с `Host key verification failed`** — секрет `SSH_KNOWN_HOSTS`
пуст или снят с другого адреса. Перечитайте `ssh-keyscan -t ed25519 <IP>`.

**Выкат падает на `docker compose pull`** — истёк токен GHCR из шага 5
или сборка не дошла до реестра. Проверьте вкладку Packages в GitHub.

**Новая переменная из `.env` не подхватилась** — использовали `restart` вместо
`up -d`, см. шаг 7.1.
