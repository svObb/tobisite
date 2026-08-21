"""Доступ в панель: без валидного токена Cloudflare Access — 403 на всё.

Токены подписываются локальным RSA-ключом (фикстура access_key), JWKS ему отдан
в конструктор — в сеть тесты не ходят. Поэтому тест с чужим kid заодно и
доказывает, что refetch JWKS в тестах не случается: сходив за ним, панель
уронила бы запрос сетевой ошибкой, а не отдала 403.
"""
import pytest

from admin.app import SECURITY_HEADERS

SCREENS = ["/", "/leads", "/costs", "/subs", "/robots.txt"]


@pytest.mark.parametrize("path", SCREENS)
async def test_no_token_forbidden(admin, path):
    assert (await admin.get(path, token=None)).status_code == 403


async def test_healthz_open_without_token(admin):
    response = await admin.get("/healthz", token=None)
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize("token_kw", [
    {"aud": "чужой-aud"},
    {"issuer": "https://чужой.cloudflareaccess.com"},
    {"email": "stranger@example.com"},
    {"kid": "ключ-после-ротации"},
    {"lifetime": -60},
])
async def test_broken_token_forbidden(admin, token_kw):
    assert (await admin.get("/", token=admin.token(**token_kw))).status_code == 403


async def test_garbage_token_forbidden(admin):
    # значение заголовка всегда ascii, поэтому мусор тоже ascii
    assert (await admin.get("/", token="not.a.token")).status_code == 403


async def test_valid_token_opens_panel(admin):
    response = await admin.get("/")
    assert response.status_code == 200
    assert "Дашборд" in response.text
    # почта из клеймов видна в шапке: понятно, чьим доступом открыто
    assert admin.email in response.text


async def test_robots_disallows_everything(admin):
    response = await admin.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /" in response.text


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", SCREENS + ["/healthz", "/leads/1"])
async def test_writes_are_refused(admin, method, path):
    """v1 только читает: не-GET получает 405 и с токеном, и без него."""
    for token in (..., None):
        response = await admin.request(method, path, token)
        assert response.status_code == 405
        assert response.headers["allow"] == "GET, HEAD"


@pytest.mark.parametrize("path", SCREENS + ["/healthz"])
async def test_head_allowed(admin, path):
    """HEAD — то же чтение: curl -I из ранбука не должен получать 405."""
    assert (await admin.request("HEAD", path)).status_code == 200


async def test_security_headers_on_every_answer(admin):
    answers = [
        await admin.get("/healthz", token=None),
        await admin.get("/", token=None),
        await admin.get("/"),
        await admin.get("/leads/999999999"),
        await admin.get("/static/app.css"),
        await admin.request("POST", "/"),
    ]
    assert [r.status_code for r in answers] == [200, 403, 200, 404, 200, 405]
    for response in answers:
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value
