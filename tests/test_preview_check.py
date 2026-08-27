"""Проверка живого превью: разбор ответа формы и бюджет скорости (10.9, 10.17).

Сети нет: сессия подменяется, PageSpeed разбирается из готового ответа.
Настоящий POST делает только tools/preview_check.py, и в CI он не запускается.
"""
from scout.site_probe import parse_psi_metrics
from site_factory.engine.checks import form_e2e
from tools.preview_check import SPEED_BUDGET_MS, assets_of, speed_problems

URL = "https://pravo-i-dilo.tobisitepreview.com"


class FakePost:
    def __init__(self, owner, payload, status):
        self.owner, self.payload, self.status = owner, payload, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    """Одна заранее заготовленная реакция на POST; вызовы запоминаются."""

    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return FakePost(self, self.payload, self.status)


# --- 10.9: тестовая заявка ----------------------------------------------------

async def test_live_check_posts_to_the_form_with_the_test_header():
    session = FakeSession({"ok": True, "test": True})

    assert await form_e2e.check_live(URL + "/", session) == []

    url, kw = session.calls[0]
    assert url == URL + "/api/lead"
    assert kw["headers"] == {"X-Tobisite-Test": "1"}
    assert set(kw["json"]) == {"name", "phone"}


async def test_answer_without_test_flag_means_the_chat_got_a_lead():
    # заявка без test:true долетела до админ-чата: каждая проверка стала бы
    # сообщением о несуществующем клиенте
    problems = await form_e2e.check_live(URL, FakeSession({"ok": True}))
    assert any("тест-заголовок" in p for p in problems)


async def test_worker_refusal_is_reported():
    session = FakeSession({"ok": False, "error": "invalid"}, status=400)
    assert await form_e2e.check_live(URL, session) == [
        f"{URL}/api/lead: HTTP 400"
    ]


async def test_dead_preview_is_not_an_exception():
    problems = await form_e2e.check_live(URL, FakeSession(ValueError("не JSON")))
    assert problems == [f"{URL}/api/lead: ValueError"]


def test_answer_shapes():
    assert form_e2e.check_answer(200, {"ok": True, "test": True}) == []
    assert form_e2e.check_answer(200, "ok") == ["/api/lead: ответ не JSON"]
    assert form_e2e.check_answer(502, {}) == ["/api/lead: HTTP 502"]


# --- что страница дотягивает сама ---------------------------------------------

SCRIPTS = ('<script defer src="/assets/lenis.js"></script>'
           '<script defer src="/assets/preview.js"></script>')


def test_scripts_come_first_and_in_the_order_of_the_page():
    html = SCRIPTS + '<img src="/img/logo.webp" alt="">'
    assert list(assets_of(html)) == [
        ("javascript", "/assets/lenis.js"),
        ("javascript", "/assets/preview.js"),
        ("image", "/img/logo.webp"),
    ]


def test_only_the_first_frame_is_checked():
    html = ('<img src="/img/logo.webp" alt="">'
            '<img src="/img/hero_bg.webp" alt="">'
            '<img src="/img/photo-2.webp" alt="">')
    # остальные картинки грузятся лениво и на открытие превью не влияют
    assert list(assets_of(html)) == [("image", "/img/logo.webp")]


def test_inline_picture_is_not_a_request():
    html = '<img src="data:image/svg+xml;base64,PHN2Zz4=" alt="">'
    assert list(assets_of(html)) == []


def test_page_without_assets_asks_for_nothing():
    assert list(assets_of("<p>тільки текст</p>")) == []


# --- 10.17: бюджет скорости ---------------------------------------------------

def _psi(lcp=None, si=None, score=None) -> dict:
    return {"lighthouseResult": {
        "categories": {"performance": {"score": score}},
        "audits": {"largest-contentful-paint": {"numericValue": lcp},
                   "speed-index": {"numericValue": si}},
    }}


def test_metrics_come_out_in_milliseconds():
    metrics = parse_psi_metrics(_psi(lcp=1842.7, si=2100.2, score=0.96))
    assert metrics == {"score": 96, "lcp_ms": 1843, "si_ms": 2100}


def test_metrics_of_a_broken_answer_are_not_zeroes():
    assert parse_psi_metrics({}) == {"score": None, "lcp_ms": None,
                                     "si_ms": None}
    assert parse_psi_metrics(_psi())["lcp_ms"] is None


def test_speed_verdict_uses_the_three_second_budget():
    assert speed_problems({"lcp_ms": SPEED_BUDGET_MS - 100}) == []
    assert speed_problems({"lcp_ms": SPEED_BUDGET_MS + 100})
    # не измерено — это не «быстро»
    assert speed_problems({"lcp_ms": None})
