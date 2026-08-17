#!/usr/bin/env bash
# Автодеплой из GitHub Actions. Это форсированная команда ssh-ключа CI:
# в authorized_keys прописано command="/usr/local/bin/qdif-deploy", поэтому
# ключ не даёт шелл, а умеет ровно одно — выкатить коммит. Сам коммит
# приезжает в SSH_ORIGINAL_COMMAND.
#
# Ставится root'ом ВНЕ рабочего каталога:
#   install -m 755 -o root -g root deploy/ci-deploy.sh /usr/local/bin/qdif-deploy
#
# Почему не запускать прямо из /opt/qdif-bot: скрипт делает git reset --hard,
# то есть переписал бы файл, который bash в этот момент ещё дочитывает.
# Плюс сломанный скрипт в плохом коммите не должен лишать возможности
# выкатить следующий.
#
# Образ здесь не собирается — он приезжает из GHCR готовым, тем самым, который
# прошёл проверки в CI. Откат поэтому мгновенный: вернуть IMAGE_TAG и поднять.
#
# Миграции скрипт НЕ накатывает — только отказывается ехать на старой схеме.
# Схема меняется вручную через deploy/deploy.sh, см. deploy/README.md.
set -Eeuo pipefail

APP=/opt/qdif-bot
SHA="${SSH_ORIGINAL_COMMAND:-}"

# Единственная защита от «ключ утёк — на сервере выполнили что угодно».
# Всё, что не похоже на коммит, отбрасывается до единого обращения к диску.
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "!! не коммит: ${SHA:0:80}"
    exit 2
fi

cd "$APP"

# IMAGE_TAG в .env — это и есть «что сейчас запущено». Отсюда же берётся точка,
# в которую откатываемся.
PREV=$(grep '^IMAGE_TAG=' .env | head -1 | cut -d= -f2-)
if [ -z "$PREV" ]; then
    echo "!! в .env нет заполненного IMAGE_TAG — первый выкат делается вручную,"
    echo "!! см. deploy/README.md, шаг 14"
    exit 1
fi
echo "==> было $PREV"
echo "==> ставим $SHA"

git fetch --quiet origin main
# коммит действительно доехал в репозиторий на сервере...
git cat-file -e "$SHA^{commit}"
# ...и он из main, а не из чьей-то ветки и не старый уязвимый
git merge-base --is-ancestor "$SHA" origin/main

# .env перезаписывается целиком, поэтому только через временный файл: обрыв
# посреди записи оставил бы сервер без токена бота и пароля к базе.
set_tag() {
    sed "s|^IMAGE_TAG=.*|IMAGE_TAG=$1|" .env > .env.new
    chmod 600 .env.new
    mv .env.new .env
}

rollback() {
    echo "==> откат на $PREV"
    git reset --hard --quiet "$PREV" 2>/dev/null || true
    set_tag "$PREV"
    docker compose up -d || true
}

git reset --hard --quiet "$SHA"

# Скрипт установлен копией, автообновления у него нет. Расхождение видно
# в логе Actions, чтобы копия не устаревала молча.
if ! cmp -s /usr/local/bin/qdif-deploy deploy/ci-deploy.sh; then
    echo "::warning::deploy/ci-deploy.sh в репозитории отличается от /usr/local/bin/qdif-deploy — переустановите его на сервере"
fi

echo "==> образ"
set_tag "$SHA"
if ! docker compose pull bot; then
    echo "!! образ $SHA не скачался — сборка в CI не дошла до реестра или нет доступа"
    rollback
    exit 1
fi

echo "==> схема"
# Миграции автодеплой не накатывает — только отказывается ехать на старой схеме.
# alembic current печатает «(head)», лишь когда база догнала миграции; пустой
# вывод (база вообще без alembic_version) сюда же — ехать нельзя.
if ! docker compose run --rm bot alembic current 2>/dev/null | grep -q '(head)'; then
    echo "!! есть неналитая миграция — контейнер не тронут, код и тег откатываются"
    echo "!! накатите вручную: $APP/deploy/deploy.sh"
    rollback
    exit 1
fi

echo "==> запуск"
STAMP=$(date '+%Y-%m-%dT%H:%M:%S')
docker compose up -d
# у db стоит restart: unless-stopped с интервалом в секунды, так что за 12
# секунд цикл падений успевает проявиться дважды
sleep 12

# docker compose ps здесь недостаточно ровно по той же причине, по которой
# не хватало systemctl is-active: с restart: unless-stopped падающий контейнер
# почти всё время выглядит запущенным. Считаем строки, которые main.py пишет
# после успешного get_me(): 0 — не поднялся, 1 — норма, больше — цикл падений.
STARTS=$(docker compose logs --since "$STAMP" bot 2>/dev/null | grep -c 'bot started' || true)
RUNNING=$(docker inspect --format '{{.State.Running}}' qdif-bot 2>/dev/null || echo false)

if [ "$RUNNING" = "true" ] && [ "${STARTS:-0}" -eq 1 ]; then
    echo "==> бот работает на $SHA"
    # сносит только висячие слои; образ PREV помечен тегом и остаётся
    # доступным для отката
    docker image prune -f >/dev/null
    exit 0
fi

echo "!! не поднялся (запущен: $RUNNING, стартов с $STAMP: ${STARTS:-0})"
docker compose logs --since "$STAMP" bot 2>/dev/null | tail -40
rollback
exit 1
