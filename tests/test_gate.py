"""Гейт тест/бой в config.py: сравнение баз по сути, а не по строкам.

Дефект: pooled-хост Neon (ep-x-pooler.…) и прямой (ep-x.…) — одна и та же
база, но сырое сравнение строк считало их разными, и тесты могли писать в бой.
"""
import os
import pathlib
import subprocess
import sys

import config

ROOT = pathlib.Path(__file__).resolve().parent.parent

POOLED = "postgresql+asyncpg://u:p@ep-abc-123-pooler.eu-central-1.aws.neon.tech/neondb?ssl=require"
DIRECT = "postgresql://x:y@EP-ABC-123.eu-central-1.aws.neon.tech:5432/neondb"


def test_db_key_sees_through_pooler_case_port_and_query():
    assert config.db_key(POOLED) == config.db_key(DIRECT)


def test_db_key_distinguishes_real_differences():
    other_db = DIRECT.replace("/neondb", "/otherdb")
    other_host = DIRECT.replace("EP-ABC-123", "ep-zzz-999")
    other_port = DIRECT.replace(":5432", ":6432")
    assert config.db_key(DIRECT) != config.db_key(other_db)
    assert config.db_key(DIRECT) != config.db_key(other_host)
    assert config.db_key(DIRECT) != config.db_key(other_port)


def _import_config(test_url: str, prod_url: str) -> subprocess.CompletedProcess:
    env = dict(
        os.environ,
        PYTHONUTF8="1",
        TOBISITE_TEST="1",
        BOT_TEST_TOKEN="0:pytest",
        ADMIN_TG_ID="1",
        ACCESS_CODE="pytest",
        COUNTRIES="Украина|UA",
        LANGUAGES="Украинский",
        TEST_DATABASE_URL=test_url,
        DATABASE_URL=prod_url,
    )
    return subprocess.run(
        [sys.executable, "-c", "import config"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
    )


def test_gate_refuses_same_db_behind_pooler():
    res = _import_config(POOLED, DIRECT)
    assert res.returncode != 0
    assert "ту же базу" in (res.stderr or "")


def test_gate_allows_genuinely_different_db():
    res = _import_config(POOLED, "postgresql://u:p@localhost:5432/tobisite")
    assert res.returncode == 0, res.stderr
