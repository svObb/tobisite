"""Роль второго админа (6.16): админов бывает двое, работниками они не становятся.

EXTRA_ADMINS разбирается на импорте config, поэтому разбор проверяется отдельным
процессом — тем же приёмом, что гейт баз в test_gate.py.
"""
import json
import os
import pathlib
import subprocess
import sys
from types import SimpleNamespace

from aiogram import F
from sqlalchemy import func, select

import config
from conftest import TEST_TG_BASE
from handlers_admin import NOT_ADMIN, guard_admin_row
from models import Session, Worker, ensure_admin_worker

ROOT = pathlib.Path(__file__).resolve().parent.parent
SECOND = TEST_TG_BASE + 777_001


class FakeCb:
    def __init__(self):
        self.alerts = []

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


def _run(extra: str) -> subprocess.CompletedProcess:
    env = dict(
        os.environ, PYTHONUTF8="1", TOBISITE_TEST="1",
        BOT_TEST_TOKEN="0:pytest", ADMIN_TG_ID="1", ACCESS_CODE="pytest",
        ADMIN_NAME="Основной", COUNTRIES="Украина|UA", LANGUAGES="Украинский",
        TEST_DATABASE_URL="postgresql://u:p@localhost:5432/tobisite_test",
        DATABASE_URL="postgresql://u:p@localhost:5432/tobisite",
        EXTRA_ADMINS=extra,
    )
    code = ("import json, config; print(json.dumps({'ids': config.ADMIN_IDS, "
            "'names': {str(k): v for k, v in config.ADMIN_NAMES.items()}}))")
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                          capture_output=True, text=True, encoding="utf-8")


def _admins(extra: str) -> dict:
    res = _run(extra)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_without_extra_admins_there_is_still_one():
    assert _admins("") == {"ids": [1], "names": {"1": "Основной"}}


def test_extra_admin_is_parsed_with_and_without_name():
    out = _admins("42|Оля, 43")
    assert out["ids"] == [1, 42, 43]
    assert out["names"]["42"] == "Оля"
    # без имени человек всё равно подписан: под этим именем он виден в CSV
    assert out["names"]["43"] == "Администратор 3"


def test_main_admin_listed_twice_stays_one():
    out = _admins("1|Он же")
    assert out["ids"] == [1]
    assert out["names"]["1"] == "Основной"


def test_broken_extra_admin_stops_the_start():
    res = _run("не_число|Оля")
    assert res.returncode != 0
    assert "EXTRA_ADMINS" in (res.stderr or "")


def test_is_admin_knows_both(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [config.ADMIN_TG_ID, SECOND])
    assert config.is_admin(config.ADMIN_TG_ID) and config.is_admin(SECOND)
    assert not config.is_admin(SECOND + 1)


def test_router_filter_matches_every_admin():
    """Одна строка в main.py решает, пустят ли админа в панель вообще."""
    flt = F.from_user.id.in_([1, SECOND])
    assert flt.resolve(SimpleNamespace(from_user=SimpleNamespace(id=SECOND)))
    assert not flt.resolve(SimpleNamespace(from_user=SimpleNamespace(id=7)))


def test_workers_list_excludes_every_admin():
    """NOT_ADMIN собран из ADMIN_IDS целиком, а не из одного основного."""
    excluded, = NOT_ADMIN.compile().params.values()
    assert sorted(excluded) == sorted(config.ADMIN_IDS)


async def test_second_admin_gets_his_own_worker_row(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [config.ADMIN_TG_ID, SECOND])
    monkeypatch.setattr(config, "ADMIN_NAMES",
                        config.ADMIN_NAMES | {SECOND: "Второй админ"})

    worker = await ensure_admin_worker(SECOND)
    assert (worker.tg_id, worker.name) == (SECOND, "Второй админ")
    # его компании числятся за ним, а не за основным админом
    assert worker.tg_id != config.ADMIN_TG_ID

    again = await ensure_admin_worker(SECOND)
    assert again.id == worker.id
    async with Session() as s:
        assert await s.scalar(
            select(func.count()).select_from(Worker).where(Worker.tg_id == SECOND)
        ) == 1


async def test_second_admin_row_is_not_managed_as_worker(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [config.ADMIN_TG_ID, SECOND])
    monkeypatch.setattr(config, "ADMIN_NAMES",
                        config.ADMIN_NAMES | {SECOND: "Второй админ"})
    worker = await ensure_admin_worker(SECOND)

    cb = FakeCb()
    assert await guard_admin_row(cb, worker.id) is False
    assert cb.alerts == ["Это строка админа, а не работник"]
