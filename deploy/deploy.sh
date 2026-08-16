#!/usr/bin/env bash
# Деплой qDif Handler. Запускать на сервере от пользователя qdif:
#   /opt/qdif-bot/deploy/deploy.sh
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
sudo systemctl restart qdif-bot

# трёх секунд хватает, чтобы поймать падение на импортах и на разборе .env
sleep 3
if systemctl is-active --quiet qdif-bot; then
    echo "==> бот работает"
    journalctl -u qdif-bot -n 5 --no-pager
else
    echo "==> НЕ поднялся:"
    journalctl -u qdif-bot -n 40 --no-pager
    exit 1
fi
