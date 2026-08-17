#!/usr/bin/env bash
# Ручной деплой qDif Handler. Запускать на сервере от пользователя qdif:
#   /opt/qdif-bot/deploy/deploy.sh
#
# Обычные правки кода приезжают сами, из GitHub Actions (deploy/ci-deploy.sh).
# Этот скрипт нужен для одного случая: выкат вместе с миграцией. Автодеплой
# схему не трогает и на неналитой миграции просто откажется ехать — накатывать
# её должен человек, который видит, что именно меняется в базе.
set -euo pipefail

cd /opt/qdif-bot

echo "==> код"
git pull --ff-only

echo "==> зависимости"
# без --upgrade: в requirements.txt стоят нижние границы (>=), и с --upgrade
# любой деплой втягивал бы свежие релизы библиотек без единой правки в коде.
# Обновление приедет тогда, когда границу поднимут в requirements.txt.
.venv/bin/pip install --quiet -r requirements.txt

echo "==> миграции"
.venv/bin/alembic upgrade head

echo "==> перезапуск"
STAMP=$(date '+%Y-%m-%d %H:%M:%S')
sudo systemctl restart qdif-bot
# RestartSec=5, так что за 12 секунд цикл падений успевает проявиться дважды
sleep 12

# Одного is-active мало: с Restart=always и StartLimitIntervalSec=0 падающий
# на старте бот бесконечно перезапускается и почти всё время выглядит живым.
# Считаем строки, которые main.py пишет после успешного get_me().
STARTS=$(journalctl -u qdif-bot --since "$STAMP" --no-pager | grep -c 'bot started' || true)

if systemctl is-active --quiet qdif-bot && [ "${STARTS:-0}" -eq 1 ]; then
    echo "==> бот работает"
    journalctl -u qdif-bot --since "$STAMP" --no-pager | tail -5
else
    echo "==> НЕ поднялся (стартов с $STAMP: ${STARTS:-0}):"
    journalctl -u qdif-bot --since "$STAMP" --no-pager | tail -40
    exit 1
fi
