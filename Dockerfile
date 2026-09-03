# Базовый образ прибит по digest, а не по тегу: тег 3.12-slim перевыпускают
# при каждом обновлении Debian, и «тот же Dockerfile» собирал бы разные образы.
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp

WORKDIR /app

RUN adduser --system --group --no-create-home app

# Зависимости отдельным слоем и до кода: правка хендлера не пересобирает pip.
# Ставится lock, а не requirements.txt — в последнем нижние границы (>=),
# и сборка втягивала бы свежие релизы библиотек без единой правки в коде.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY . .

# Контейнер запускается без прав root; писать в /app процессу не нужно,
# поэтому в compose файловая система смонтирована только на чтение.
USER app

CMD ["python", "main.py"]
