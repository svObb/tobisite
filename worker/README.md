# tobisite-preview — раздача превью

Один Worker на все черновики: `<slug>.tobisitepreview.com` → префикс `<slug>/`
в бакете R2. Публикация превью — не деплой, а PUT в бакет: его делает бот
сразу после сборки черновика (`docs/preview-pipeline.md`), а папку целиком —
`python tools/publish_r2.py`. Деплой воркера нужен один раз и потом только
при правке кода.

Локального `node_modules` тут нет: всё через `npx wrangler`.

## Разовая настройка

1. **Вход:** `npx wrangler login`

2. **Бакет:** `npx wrangler r2 bucket create tobisite-previews`

3. **Автоудаление.** Срок жизни превью — 60 дней, и держит его бот
   (`draft_service.expire_previews`, см. `docs/preview-pipeline.md`): он один
   знает, что превью проданного лида сносить нельзя. Правила на бакете
   (дашборд → R2 → `tobisite-previews` → Settings → Object lifecycle rules) —
   только страховка:

   | Префикс | Срок | Зачем |
   |---|---|---|
   | весь бакет | 180 дней | мусор от сбоев; ДЛИННЕЕ 60 дней намеренно — иначе правило снесёт страницу, из которой делается боевой сайт |
   | `_hits/` | 7 дней | хвосты, если поллер бота стоял: разобранное он удаляет сам |

4. **DNS зоны `tobisitepreview.com`:** одна запись `*` типа A на `192.0.2.1`
   (адрес-заглушка из TEST-NET, трафик до него не доходит), **proxied**,
   оранжевое облако. Запись нужна только чтобы имя резолвилось в Cloudflare —
   отвечает по нему воркер.

5. **Деплой:** из папки `worker/` → `npx wrangler deploy`
   Перед этим должна существовать папка `../site_factory/build` (общий
   `bundle.css`, `fonts/` и скрипты превью — всё это делает
   `python tools/build_css.py`). Если её ещё нет — временно закомментируйте
   секцию `[assets]` в `wrangler.toml`.

6. **Секреты** (в `wrangler.toml` их нет намеренно):

   ```
   npx wrangler secret put TG_BOT_TOKEN
   npx wrangler secret put TG_ADMIN_CHAT_ID
   ```

   Без них форма превью вернёт `502`, а в логах будет `lead dropped`.

## Проверка после деплоя

Положить любую страницу под слаг `test` и запросить её:

```
python tools/publish_r2.py <папка> --slug test
curl -I https://test.tobisitepreview.com
```

Ожидаем `200` и заголовок `X-Robots-Tag: noindex, nofollow, noarchive`
(плюс `Content-Security-Policy` и `X-Content-Type-Options`). Это та самая
проверка из плана на неделю 2: связка «wildcard proxied DNS + Worker route на
Free-плане» задокументирована, но никем не проверена вживую. Если не
взлетит — запасной путь: Pages с одним доменом и путями `/{slug}` вместо
поддоменов.

Остальное тем же curl:

```
curl https://test.tobisitepreview.com/robots.txt            # User-agent: * / Disallow: /
curl https://test.tobisitepreview.com/nope                  # 404, страница без брендинга
curl -X POST https://test.tobisitepreview.com/api/lead \
     -H "content-type: application/json" -H "X-Tobisite-Test: 1" \
     -d '{"name":"Test","phone":"+380000000000"}'           # {"ok":true,"test":true}
```

Заголовок `X-Tobisite-Test: 1` проходит всю валидацию, но не пишет в
админ-чат и не тратит лимит запросов — им можно дёргать форму сколько угодно.

## Что стоит знать

- **Слаг без точек.** Universal SSL Free покрывает ровно один уровень
  wildcard: сертификат на `*.tobisitepreview.com` действителен для
  `pravo-i-dilo.tobisitepreview.com` и **не** действителен для
  `pravo.i.dilo.tobisitepreview.com` — клиент увидит предупреждение браузера.
  Поэтому слаг только `[a-z0-9-]`, это гарантирует `tools/slugify_preview.py`.
- **Rate-limit на `/api/lead` — 5 запросов в минуту с адреса — живёт в памяти
  изолята.** Изолятов много, они перезапускаются: это заслон от простого
  флуда, а не гарантия. Честный глобальный счётчик требует Durable Object или
  KV, оба лишние на Free.
- **`/api/hit`** пишет только слаг и тип события, без куки и без адреса —
  в Analytics Engine (агрегаты) и пустым объектом
  `_hits/<slug>/<event>/<мс>-<нонс>` в бакет (его забирает бот: наружных
  портов у VPS нет, позвать его воркер не может). Запись в бакет —
  `waitUntil` с проглоченной ошибкой: из-за счётчика вовлечённости посетитель
  ошибки не увидит. Нет Analytics Engine на аккаунте — закомментируйте
  `[[analytics_engine_datasets]]`, ручка продолжит отвечать `204`.
  Префикс `_hits/` слагом стать не может: подчёркивание не проходит `SLUG_RE`.
- **Скрипты превью раздаются из `/assets`, отдельными файлами.** CSP не
  разрешает инлайн (`default-src 'self'`), поэтому `lenis.js` (плавный скролл),
  `preview.js` (появление секций и счётчики `/api/hit`) и `parallax.js` (фон
  первого экрана) лежат в `site_factory/js/` и копируются в `build/` тем же
  `tools/build_css.py`, что собирает бандл. Каждая страница подключает их
  через `defer`. Скрипт не доехал или заблокирован расширением — страница
  остаётся полностью рабочей: движение это надстройка, а не условие.
  `prefers-reduced-motion` выключает всё движение, счётчики продолжают
  работать: это измерение, а не анимация.
- **Лимиты Free:** 100 000 запросов воркера в день, R2 — 10 ГБ и 1 млн записей
  в месяц. При 1 760 превью по ~8 файлов это ~14 000 записей в месяц.
