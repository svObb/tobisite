"""Сборка приложения панели: доступ, общие заголовки, 405 на любой не-GET.

Модуль обязан импортироваться без окружения: его дёргает смоук-импорт в CI и
pytest, а .env бота там нет. Поэтому наверху ни config, ни models — движок,
verifier и роуты экранов появляются в create_app() (тесты передают их явно)
или в startup-событии (uvicorn в контейнере, где .env уже прочитан).
"""
import logging
import os
import pathlib
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .auth import CF_HEADER, AccessVerifier, Denied

log = logging.getLogger(__name__)

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"

# Панель не должна попадать в поиск даже теоретически, а CSP без 'unsafe-inline'
# означает: ни одного инлайнового стиля и скрипта в шаблонах — htmx и стили
# лежат в /static (потому же в панели нет CDN-ссылок).
SECURITY_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy":
        "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

# /healthz отвечает без токена: это единственный способ проверить с сервера,
# что процесс жив, — Access-токена внутри контейнера взять негде. Данных он
# не отдаёт.
OPEN_PATHS = {"/healthz"}

# HEAD объявляется явно: FastAPI, в отличие от голого Starlette, сам его к GET
# не добавляет, и curl -I получал бы 405 там, где GET отдаёт страницу
READ_METHODS = ["GET", "HEAD"]


def env_req(name: str) -> str:
    """Как config._req: без переменной панель не поднимется, но скажет почему."""
    value = (os.getenv(name) or "").strip()
    if not value:
        raise SystemExit(f"В .env не заполнена переменная {name}")
    return value


def _attach(app: FastAPI, db_url: str | None, verifier) -> None:
    """Движок, verifier и роуты экранов.

    Импорт views отложен до вызова: он тянет queries → models → config, а config
    без .env завершает процесс. Роутер можно подключать и после старта — Starlette
    перебирает список роутов на каждом запросе.
    """
    from . import db, views

    app.state.engine, app.state.db = db.connect(
        db_url or env_req("ADMIN_DATABASE_URL")
    )
    app.state.verifier = verifier or AccessVerifier(
        team_domain=env_req("ACCESS_TEAM_DOMAIN"),
        aud=env_req("ACCESS_AUD"),
        allowed_emails=env_req("ADMIN_ALLOWED_EMAILS").split(","),
    )
    app.include_router(views.router)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    if not hasattr(app.state, "engine"):
        _attach(app, None, None)
    yield
    await app.state.engine.dispose()


def _refuse(status: int, text: str, **headers) -> PlainTextResponse:
    return PlainTextResponse(text, status_code=status,
                             headers=SECURITY_HEADERS | headers)


def create_app(db_url: str | None = None, verifier=None) -> FastAPI:
    """Приложение панели. Оба аргумента — для тестов, в бою всё берётся из env."""
    app = FastAPI(lifespan=_lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Метод проверяется раньше токена: v1 не пишет ничего и вообще, и ответ
        # на POST не должен зависеть от того, валиден ли токен.
        if request.method not in ("GET", "HEAD"):
            return _refuse(405, "Только чтение", Allow="GET, HEAD")
        if request.url.path not in OPEN_PATHS:
            try:
                request.state.email = await request.app.state.verifier.email(
                    request.headers.get(CF_HEADER, "")
                )
            except Denied as e:
                log.info("403 %s %s: %s", request.method, request.url.path, e)
                return _refuse(403, "Нет доступа")
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.api_route("/healthz", methods=READ_METHODS)
    async def healthz():
        return JSONResponse({"ok": True})

    @app.api_route("/robots.txt", methods=READ_METHODS)
    async def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    if db_url is not None or verifier is not None:
        _attach(app, db_url, verifier)
    return app


# uvicorn admin.app:app — экраны подключатся в startup, когда есть окружение
app = create_app()
