# Админ-панель app.tobisite.com (этап 6, v1 — только чтение)

Тот же образ, что у бота, отдельный процесс: `uvicorn admin.app:app` на порту
8100 **внутри** compose-сети. Наружу панель выходит только через Cloudflare
Tunnel, вход — Cloudflare Access (email OTP). На VPS по-прежнему ноль открытых
портов: у сервисов `admin` и `cloudflared` нет `ports:`.

Панель ничего не пишет: не-GET получает 405, роль в базе — `admin_ro`
(SELECT на всё, кроме `fsm_states`). Всё, что меняет данные, делается в боте.

## Что уже сделано в репозитории

- `admin/` — приложение (экраны: дашборд, лиды, карточка лида, метрики,
  расходы, подписки). Экран «Метрики» — таблица недели, факт-стоимости и
  превью-хиты (13.1, 13.4, 13.5, 20.10); те же цифры отдаёт `/metrics` в боте.
- `compose.yaml` — сервисы `admin` и `cloudflared` под `profiles: ["admin"]`:
  обычный `docker compose up -d` их не трогает, пока в `.env` нет
  `COMPOSE_PROFILES=admin`.
- `admin/sql/admin_ro.sql` — бутстрап роли, идемпотентный.
- htmx вендорён: `admin/static/htmx.min.js` — **htmx 2.0.10**,
  `https://unpkg.com/htmx.org@2.0.10/dist/htmx.min.js`,
  sha256 `71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de`.
  Обновлять — только заменой файла и этой строки: CSP `default-src 'self'`
  ссылок на CDN не пропустит, и это сделано намеренно.

## Серверная часть — руками, один раз

### 1. Туннель

Cloudflare Zero Trust → Networks → Tunnels → **Create a tunnel**, тип
**Cloudflared (remote-managed)**, имя, например, `tobisite`. В настройках
туннеля добавить Public hostname:

- Subdomain `app`, Domain `tobisite.com`
- Service: **HTTP**, URL `admin:8100` (имя сервиса compose, не localhost)

Скопировать **токен туннеля** (длинная строка из команды установки) — он
поедет в `.env` как `CLOUDFLARE_TUNNEL_TOKEN`.

### 2. Access-приложение

Zero Trust → Access → Applications → **Add an application** → Self-hosted:

- Application domain: `app.tobisite.com`
- Policy: Action **Allow**, Include → **Emails** → почта основателя
- Login method: **One-time PIN** (email OTP)
- В настройках приложения включить выдачу **JWT** и скопировать **AUD tag**

Там же посмотреть свой team domain — `<team>.cloudflareaccess.com`.

### 3. Переменные в server-.env

```
COMPOSE_PROFILES=admin
ADMIN_DB_PASSWORD=<длинный случайный пароль роли admin_ro>
CLOUDFLARE_TUNNEL_TOKEN=<токен из шага 1>
ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
ACCESS_AUD=<AUD tag из шага 2>
ADMIN_ALLOWED_EMAILS=<почта основателя, через запятую если несколько>
CLOUDFLARED_TAG=latest
```

`DATABASE_URL`, `BOT_TOKEN` и прочие переменные бота из `.env` не убирать:
панель импортирует его модели и `config.py`, а тот без них не стартует. В базу
панель ходит только по `ADMIN_DATABASE_URL` — его подставляет compose.

Пароль сгенерировать, например, так: `openssl rand -base64 30`.

### 4. Роль в базе

```
docker exec -i tobisite-db psql -U tobisite -d tobisite \
  -v admin_password='<тот же ADMIN_DB_PASSWORD>' -f - < admin/sql/admin_ro.sql
```

Проверка, что роль читает лиды и не видит формы работников:

```
docker exec -it tobisite-db psql -U admin_ro -d tobisite \
  -c 'select count(*) from leads' -c 'select count(*) from fsm_states'
```

Второй запрос обязан ответить `permission denied for table fsm_states`.

### 5. Запуск и проверки

```
docker compose pull && docker compose up -d
docker compose ps                       # admin и cloudflared запущены
ss -tlnp                                # новых открытых портов НЕТ
docker compose exec admin python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8100/').status)"
```

Последняя команда обязана упасть с `HTTP Error 403`: изнутри контейнера токена
Access нет, и панель не отдаёт данные. Живость процесса проверяется отдельно:

```
docker compose exec admin python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8100/healthz').read())"
```

Снаружи: `curl -sI https://app.tobisite.com/` без входа отдаёт редирект на
страницу Cloudflare Access, а не панель. После входа по OTP цифры экранов
должны совпадать с `/costs`, `/subs` и `/metrics` в боте — считаются они тем
же кодом.

Сразу после первого `docker compose pull` зафиксировать тег cloudflared:

```
docker compose exec cloudflared cloudflared --version
```

и вписать в `.env` показанную версию (`CLOUDFLARED_TAG=2026.x.y`), чтобы
следующий `pull` не подменил образ незаметно.

## Обслуживание

- Выключить панель: убрать `COMPOSE_PROFILES=admin` из `.env` и
  `docker compose --profile admin down` — бот и база не затрагиваются.
- Сменить пароль роли: поменять `ADMIN_DB_PASSWORD`, прогнать шаг 4 заново
  (скрипт идемпотентен) и `docker compose up -d admin`.
- Отозвать доступ: убрать почту из политики Access — токены перестают
  выписываться сразу; `ADMIN_ALLOWED_EMAILS` — второй рубеж, его тоже поправить.
- Логи: `docker compose logs -f admin` (отказы пишутся строкой `403 GET /path`).
