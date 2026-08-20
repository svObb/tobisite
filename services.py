"""Каталог доп-услуг и рекомендатор «Что допродать» (16.A/16.B). Без ИИ.

Файл-истина — services.yml. Валидатор гоняется при импорте: битый каталог
роняет бота на старте внятной ошибкой, а не молча прячет услуги. CI ловит то
же самое шагом «импорт всех модулей».

Рекомендатор детерминированный: triggers услуги сверяются с фактами лида
(ниша, страна, has_ads, наличие сайта), топ-3 по баллам. ИИ-однострочник
«почему именно им» — отдельный пункт 16.9, волна 2+.
"""
import pathlib

import yaml

import config

STATUSES = ("ready", "pilot", "idea")
COMPLEXITY = ("low", "mid", "high")
TRIGGERS = ("always", "has_ads", "no_website", "has_website")
REQUIRED = (
    "id", "name", "status", "cogs", "price", "margin", "complexity",
    "niches", "countries", "deps", "triggers", "pitch_en", "pitch_ua",
)

# Баллы за совпавший триггер. has_ads выше всех: компания уже платит за
# клики — тёплый сегмент для missed-call и голоса (вердикт раздела 15).
TRIGGER_POINTS = {"always": 8, "has_ads": 40, "no_website": 30, "has_website": 15}
# Услуга со списком ниш — точнее универсальной: бонус за попадание в нишу
NICHE_MATCH_POINTS = 20

_PATH = pathlib.Path(__file__).with_name("services.yml")


def _err(sid: str, msg: str):
    raise SystemExit(f"services.yml, услуга «{sid}»: {msg}")


def _load() -> list[dict]:
    try:
        data = yaml.safe_load(_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Не найден каталог услуг: {_PATH}")
    except yaml.YAMLError as e:
        raise SystemExit(f"services.yml не разбирается: {e}")
    if not isinstance(data, list) or not data:
        raise SystemExit("services.yml: ожидается непустой список услуг")

    seen = set()
    for svc in data:
        sid = svc.get("id") or "<без id>"
        for f in REQUIRED:
            if f not in svc:
                _err(sid, f"нет поля {f}")
        if sid in seen:
            _err(sid, "id повторяется")
        seen.add(sid)
        if svc["status"] not in STATUSES:
            _err(sid, f"status «{svc['status']}» не из {STATUSES}")
        if svc["complexity"] not in COMPLEXITY:
            _err(sid, f"complexity «{svc['complexity']}» не из {COMPLEXITY}")
        if not svc["triggers"]:
            _err(sid, "пустые triggers — услугу никогда не порекомендует")
        for t in svc["triggers"]:
            if t not in TRIGGERS:
                _err(sid, f"неизвестный триггер «{t}» (можно: {TRIGGERS})")
        for field, known in (("niches", config.NICHES),
                             ("countries", [n for n, _ in config.COUNTRIES])):
            v = svc[field]
            if v == "all":
                continue
            if not isinstance(v, list) or not v:
                _err(sid, f"{field}: либо all, либо непустой список")
            # страны сверяем только на тип: список стран живёт в .env и на
            # разных окружениях разный, а ниши — константа в config.py
            if field == "niches":
                for x in v:
                    if x not in known:
                        _err(sid, f"ниша «{x}» не из config.NICHES")
        if svc["status"] in ("ready", "pilot"):
            if not (svc["pitch_en"] or "").strip() or not (svc["pitch_ua"] or "").strip():
                _err(sid, "у ready/pilot услуги pitch_en и pitch_ua обязательны")
    return data


SERVICES: list[dict] = _load()


def recommend(lead, limit: int = 3) -> list[dict]:
    """Топ услуг под лида: [{svc, score, why}], баллы по убыванию.

    idea не показывается никому (16.3); pilot проходит — хендлер помечает его
    «только дружественным». Услуга без единого совпавшего триггера не выводится.
    """
    scored = []
    for pos, svc in enumerate(SERVICES):
        if svc["status"] == "idea":
            continue
        if svc["countries"] != "all" and lead.country not in svc["countries"]:
            continue
        if svc["niches"] != "all" and lead.niche not in svc["niches"]:
            continue

        pts, why = 0, []
        facts = {
            "always": (True, "универсальная"),
            "has_ads": (bool(lead.has_ads), "уже платит за рекламу"),
            "no_website": (not lead.website_url, "сайта нет"),
            "has_website": (bool(lead.website_url), "есть сайт — можно улучшать"),
        }
        for t in svc["triggers"]:
            hit, reason = facts[t]
            if hit:
                pts += TRIGGER_POINTS[t]
                why.append(reason)
        if svc["niches"] != "all":
            pts += NICHE_MATCH_POINTS
            why.append(f"под нишу «{lead.niche}»")
        if pts:
            scored.append({"svc": svc, "score": pts, "why": why, "pos": pos})

    # при равных баллах побеждает порядок в services.yml — он ручной и осознанный
    scored.sort(key=lambda r: (-r["score"], r["pos"]))
    return scored[:limit]
