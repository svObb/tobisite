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
# Миграции скрипт НЕ накатывает — только отказывается ехать на старой схеме.
# Схема меняется вручную через deploy/deploy.sh, см. deploy/README.md.
set -Eeuo pipefail

APP=/opt/qdif-bot
SERVICE=qdif-bot
SHA="${SSH_ORIGINAL_COMMAND:-}"

# Единственная защита от «ключ утёк — на сервере выполнили что угодно».
# Всё, что не похоже на коммит, отбрасывается до единого обращения к диску.
if [[ ! "$SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "!! не коммит: ${SHA:0:80}"
    exit 2
fi

cd "$APP"
PREV=$(git rev-parse HEAD)
echo "==> было $PREV"
echo "==> ставим $SHA"

git fetch --quiet origin main
# коммит действительно доехал в репозиторий на сервере...
git cat-file -e "$SHA^{commit}"
# ...и он из main, а не из чьей-то ветки и не старый уязвимый
git merge-base --is-ancestor "$SHA" origin/main

rollback() {
    echo "==> откат на $PREV"
    git reset --hard --quiet "$PREV"
    .venv/bin/pip install --quiet -r requirements.txt || true
    sudo systemctl restart "$SERVICE" || true
}

git reset --hard --quiet "$SHA"

# Скрипт установлен копией, автообновления у него нет. Расхождение видно
# в логе Actions, чтобы копия не устаревала молча.
if ! cmp -s /usr/local/bin/qdif-deploy deploy/ci-deploy.sh; then
    echo "::warning::deploy/ci-deploy.sh в репозитории отличается от /usr/local/bin/qdif-deploy — переустановите его на сервере"
fi

echo "==> зависимости"
if ! .venv/bin/pip install --quiet -r requirements.txt; then
    rollback
    exit 1
fi

echo "==> схема"
# alembic current печатает «(head)» только когда база догнала миграции.
# Пустой вывод (база вообще без alembic_version) сюда же — ехать нельзя.
if ! .venv/bin/alembic current 2>/dev/null | grep -q '(head)'; then
    echo "!! есть неналитая миграция — служба не тронута, код откатывается"
    echo "!! накатите вручную: sudo -u qdif bash -c 'cd $APP && .venv/bin/alembic upgrade head'"
    rollback
    exit 1
fi

echo "==> перезапуск"
STAMP=$(date '+%Y-%m-%d %H:%M:%S')
sudo systemctl restart "$SERVICE"
# RestartSec=5, так что за 12 секунд цикл падений успевает проявиться дважды
sleep 12

# is-active тут недостаточно: с Restart=always и StartLimitIntervalSec=0
# падающий на старте бот бесконечно перезапускается и почти всё время
# выглядит живым. Считаем строки, которые main.py пишет после успешного
# get_me(): 0 — не поднялся, 1 — норма, больше — цикл падений.
STARTS=$(journalctl -u "$SERVICE" --since "$STAMP" --no-pager | grep -c 'bot started' || true)

if systemctl is-active --quiet "$SERVICE" && [ "${STARTS:-0}" -eq 1 ]; then
    echo "==> бот работает на $SHA"
    exit 0
fi

echo "!! не поднялся (стартов с $STAMP: ${STARTS:-0})"
journalctl -u "$SERVICE" --since "$STAMP" --no-pager | tail -40
rollback
exit 1
