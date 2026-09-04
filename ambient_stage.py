"""Амбиент-кадры лида: пережать, выложить в стейджинг, вписать в карточку.

Картинку рисует человек в подписочной сессии генератора — ни бот, ни этот
модуль за ней никуда не ходят и денег не тратят. Сюда приходят готовые байты,
а модуль делает то, что до сих пор делалось руками: пережимает по контракту
роли (`site_images`), кладёт файл в `_enrich/<lead_id>/img/<роль>.webp` и
дописывает карточку так, чтобы кадр пережил повторное «Обогатить» — за это
отвечает служебный ключ `_ambient` (см. `enrich_service`).

Ролей две породы. `hero_bg` — фон первого экрана: большая картинка на странице
бывает либо фоном секции, либо не бывает вовсе. `ambient-1..3` — кадры общего
пула страницы: галерея, «о компании» и двухполотный первый экран берут снимки
оттуда, и добить пул можно только именем вне NAMED_IMAGES движка. Сколько
кадров лиду не хватает и что на них рисовать, говорит отчёт обогащения
(`enrich_service.ambient_gap`).

Пространство имён своё, не photo-N: те раздаёт скрейп по номерам, и совпадение
стёрло бы настоящий снимок компании. В пуле амбиент стоит после снимков сайта
— снимок компании на странице всегда раньше дорисованного.

Снимка компании тут нет ни в одной роли: есть нарисованный кадр, и людей,
животных, еду и товары на нём не рисуют — этого никто не проверит глазами.

Команда ручная:

    python -m ambient_stage 417 hero.png
    python -m ambient_stage 417 zal.png --role ambient-1
    python -m ambient_stage 417 < hero.png
"""
import argparse
import asyncio
import pathlib
import sys

import config
import draft_service
import enrich_service
import site_images
from models import Lead, Session, log_event
from site_factory.engine import photos, render

# Фон первого экрана и места пула. hero_bg вне пула: секции пула берут кадры
# мимо него, и нехватку он не закрывает (enrich_service.ambient_gap).
POOL_ROLES = ("ambient-1", "ambient-2", "ambient-3")
AMBIENT_ROLES = ("hero_bg", *POOL_ROLES)


def _min_frame_width() -> int:
    """Самый строгий pool_min_width библиотеки, посчитанный по её контрактам.

    Кадр уже этого не берёт ни одна секция пула, и выложить его значило бы
    оставить в бакете файл, которого страница никогда не покажет. Число живёт в
    контрактах секций и здесь не дублируется: разъедутся — и сообщение об
    отказе начнёт врать работнику.
    """
    return photos.strictest_min_width(render.load_library())


def free_pool_roles(occupied=(), count: int = 1) -> list[str]:
    """Первые свободные места пула: под сколько кадров, столько и имён.

    Имя роли — имя файла в стейджинге, и PUT идёт поверх прежнего (см. _put).
    Поэтому второй кадр под тем же ambient-1 не добавляет странице снимок, а
    переписывает уже выложенный: отчёт обязан называть следующее свободное
    место, иначе работник ходит по кругу.
    """
    taken = set(occupied)
    return [role for role in POOL_ROLES if role not in taken][:max(count, 0)]


async def stage_ambient(lead_id: int, data: bytes, role: str = "hero_bg", *,
                        actor_tg_id: int = config.ADMIN_TG_ID) -> str:
    """Выложить амбиент-кадр лида и вписать его в карточку. Отчёт — строкой.

    Порядок шагов такой, чтобы отказ не оставлял следов: сперва картинка (не
    годится — в бакет мы не ходили), потом лид (закрыт — не ходили тоже), и
    только после этого PUT и одна транзакция на карточку.

    Занятость лида общая с «Обогатить»: перескрейп сносит и перекладывает те
    же файлы, и два дела над одним стейджингом одновременно оставили бы в
    бакете половину старых файлов, половину новых.
    """
    if role not in AMBIENT_ROLES:
        raise ValueError(f"роль {role!r} амбиенту не положена: только "
                         f"{', '.join(AMBIENT_ROLES)} — фон секции и пул")
    made = _drawn_frame(data, role)
    if not draft_service.r2_ready():
        raise RuntimeError("не заданы ключи R2 — выкладывать некуда")
    if not enrich_service.hold_lead(lead_id):
        raise RuntimeError(f"лид {lead_id} сейчас обогащается — повторите, "
                           "когда обход закончится")
    key = enrich_service.image_key(lead_id, f"{role}.webp")
    try:
        async with Session() as s:
            _open_lead(await s.get(Lead, lead_id), lead_id)
        await _put(key, made)
        card = await _write(lead_id, made, role, actor_tg_id)
    finally:
        enrich_service.release_lead(lead_id)
    return (f"лид {lead_id}: {role} {made['width']}×{made['height']}, "
            f"{len(made['data']) // 1024} КБ → {key}; фото в карточке "
            f"{card['photo_count']}, амбиент: {', '.join(card['ambient'])}")


# --- внутреннее ---------------------------------------------------------------

def _drawn_frame(data: bytes, role: str) -> dict:
    """Байты → webp по контракту роли. Не годится — ValueError с причиной.

    Пороги те же, по которым фон отбирает скрейп: картинка проходит отбор
    фотографии (не иконка, не полоска карусели) и не уже BACKGROUND_MIN_WIDTH.
    Нарисованный под слот кадр ниже порога не бывает, а пережатый скриншот из
    мессенджера — запросто, и в шапке превью его видно.

    Ширину смотрим дважды, до и после пережатия, и второй раз важнее: узкое
    высокое полотно проходит порог исходником, а пережатие ужимает его по
    длинной стороне, и на страницу приезжает кадр в треть нужной ширины.
    """
    size = site_images.probe_image(data)
    if size is None:
        raise ValueError("это не картинка или файл битый")
    width, height = size["width"], size["height"]
    if (not site_images.fits(size, "photo")
            or width < site_images.BACKGROUND_MIN_WIDTH):
        raise ValueError(f"{width}×{height} для роли {role} не годится: нужна "
                         f"ширина от {site_images.BACKGROUND_MIN_WIDTH}px и "
                         "пропорции фотографии")
    made = site_images.process_image(data, site_images.role_of(role))
    if made is None:
        raise ValueError("картинка не пережалась в webp")
    least = _min_frame_width()
    if made["width"] < least:
        raise ValueError(f"{width}×{height} после пережатия даёт "
                         f"{made['width']}px ширины — секциям нужно от "
                         f"{least}px; кадр нужен не такой вытянутый")
    return made


def _open_lead(lead, lead_id: int):
    """Лид, которому превью ещё показывают. Иначе — ValueError."""
    if lead is None:
        raise ValueError(f"нет лида {lead_id}")
    if (lead.deleted_at or lead.cancelled_at
            or lead.status in draft_service.CLOSED_STATUSES):
        raise ValueError(f"лид {lead_id} закрыт — его стейджинг всё равно "
                         "подметут")
    return lead


async def _put(key: str, made: dict):
    """PUT поверх прежнего файла: имя роли фиксированное.

    Уборки соседей здесь нет намеренно: остальные файлы стейджинга положил
    скрейп, и амбиент им не хозяин.
    """
    s3 = draft_service.s3_client()
    await asyncio.to_thread(
        s3.put_object, Bucket=draft_service.bucket_name(), Key=key,
        Body=made["data"], ContentType=made["content_type"],
        CacheControl="no-cache",
    )


async def _write(lead_id: int, made: dict, role: str, actor_tg_id: int) -> dict:
    """Карточка одной транзакцией: запись картинки, счётчик фото, список амбиента."""
    async with Session() as s, s.begin():
        lead = _open_lead(await s.get(Lead, lead_id), lead_id)
        enrichment = dict(lead.enrichment or {})
        images = dict(enrichment.get("images") or {})
        images[role] = {"src": f"/{enrich_service.IMG_DIR}/{role}.webp",
                        "width": made["width"], "height": made["height"]}
        enrichment["images"] = images
        # photo_count — число контентных имён, а не «сколько фотографий на
        # сайте»: инвариант держится пересчётом, а не прибавлением единицы
        enrichment["photo_count"] = len(site_images.photo_names(images))
        ambient = sorted(set(enrich_service.ambient_names(enrichment)) | {role})
        enrichment[enrich_service.AMBIENT_KEY] = ambient
        # присваиванием: JSONB меняется целым значением, правка вложенного
        # словаря на месте до базы не доедет
        lead.enrichment = enrichment
        log_event(s, lead_id, "ambient_staged", actor_tg_id, field=role,
                  new=f"{made['width']}×{made['height']}")
    return {"photo_count": enrichment["photo_count"], "ambient": ambient}


# --- команда ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("lead_id", type=int, help="номер лида в базе")
    parser.add_argument("path", nargs="?", type=pathlib.Path,
                        help="файл с картинкой; без него байты читаются из stdin")
    parser.add_argument("--role", default=AMBIENT_ROLES[0], choices=AMBIENT_ROLES,
                        help="роль кадра: фон первого экрана или место в пуле")
    args = parser.parse_args()

    if args.path is not None and not args.path.is_file():
        raise SystemExit(f"Нет такого файла: {args.path}")
    data = args.path.read_bytes() if args.path else sys.stdin.buffer.read()
    if not data:
        raise SystemExit("На входе пусто: укажите файл или подайте байты в stdin")
    try:
        print(asyncio.run(stage_ambient(args.lead_id, data, args.role)))
    except (ValueError, RuntimeError) as e:
        raise SystemExit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
