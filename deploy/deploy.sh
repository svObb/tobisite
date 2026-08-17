#!/usr/bin/env bash
# Ручной деплой qDif Handler. Запускать на сервере от пользователя qdif:
#   /opt/qdif-bot/deploy/deploy.sh
#
# Обычные правки кода приезжают сами, из GitHub Actions (deploy/ci-deploy.sh).
# Этот скрипт нужен для одного случая: выкат вместе с миграцией. Автодеплой
# схему не трогает и на неналитой миграции просто откажется ехать — накатывать
# её должен человек, который видит, что именно меняется в базе.
#
# Образ берётся из GHCR по текущему origin/main, а не собирается здесь:
# запускать надо ровно то, что прошло проверки в CI.
set -euo pipefail

cd /opt/qdif-bot

echo "==> код"
git pull --ff-only
SHA=$(git rev-parse HEAD)

echo "==> образ $SHA"
sed "s|^IMAGE_TAG=.*|IMAGE_TAG=$SHA|" .env > .env.new
chmod 600 .env.new
mv .env.new .env
docker compose pull bot

echo "==> резервная копия перед миграцией"
# Схему alembic downgrade вернёт, а удалённые колонкой данные — нет.
docker exec qdif-db pg_dump -U qdif -Fc qdif > "/tmp/qdif-before-migration-$(date +%F_%H%M).dump"

echo "==> миграции"
docker compose run --rm bot alembic upgrade head

echo "==> запуск"
STAMP=$(date '+%Y-%m-%dT%H:%M:%S')
docker compose up -d
sleep 12

# Одного «контейнер запущен» мало: с restart: unless-stopped падающий бот
# бесконечно перезапускается и почти всё время выглядит живым. Считаем строки,
# которые main.py пишет после успешного get_me().
STARTS=$(docker compose logs --since "$STAMP" bot 2>/dev/null | grep -c 'bot started' || true)
RUNNING=$(docker inspect --format '{{.State.Running}}' qdif-bot 2>/dev/null || echo false)

if [ "$RUNNING" = "true" ] && [ "${STARTS:-0}" -eq 1 ]; then
    echo "==> бот работает"
    docker compose logs --since "$STAMP" bot | tail -5
else
    echo "==> НЕ поднялся (запущен: $RUNNING, стартов с $STAMP: ${STARTS:-0}):"
    docker compose logs --since "$STAMP" bot | tail -40
    exit 1
fi
