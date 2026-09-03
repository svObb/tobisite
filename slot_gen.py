"""Слот-генерация черновиков: модель пишет free-слоты, факты ставит код (Д13 §2).

Разделение труда то же, что в письмах: факты даёт карточка лида, порядок слов
в короткой строке — модель. Модель не видит ни HTML, ни телефона, ни адреса,
ни названий услуг: fact-слоты в промпт не попадают вовсе, их закрывает
site_factory из белого списка профиля. Значит, библиотеку секций можно менять,
не трогая промпт, и наоборот.

Ключ слота — «вариант.слот»: одно и то же имя (lede, section_title) живёт в
разных секциях с разным смыслом и разным лимитом.

max_chars — жёсткий предел. Строка длиннее лимита не режется молча: слот
перегенерируется ровно один раз, а при повторном нарушении остаётся пустым,
и дальше судьбу секции решает лестница деградации site_factory (8.31).

Живёт этот модуль в боте, а не в site_factory: пакет черновиков остаётся без
сети и без ключей от API (решение 9 этапа).
"""
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal

import anthropic

import config
import costs
from site_factory.engine import slots as sf_slots

log = logging.getLogger(__name__)

# Квалити-гейт 28.08.2026 (пилот №2): Haiku систематически (3 из 3 прогонов)
# отдаёт пустой about_text — секция «О нас» выпадает, плюс платная
# перегенерация на каждом черновике. Sonnet прошёл 59/59 слотов с первого раза.
MODEL = "claude-sonnet-5"
# s4: source_note перестал быть free-слотом (подпись источника рейтинга теперь
# факт), из словаря и few-shot он убран.
PROMPT_VERSION = "s4"
# Три десятка коротких строк JSON'ом: к слотам страницы добавились блёрбы
# услуг, по одному на позицию. Больше здесь означает, что модель начала
# сочинять абзацы вместо подписей.
MAX_TOKENS = 2000
# Повторяющийся free-слот, чья заготовка привязана к личности элемента, модели
# не показывается. stat_label — подпись под цифрой из Google: какая именно
# цифра стоит рядом, знает профиль, а модель видит один индекс, и «Відгуків у
# Google» над рейтингом — выдуманный факт. Такие слоты закрывают заготовки
# рецепта; человек может написать свой текст ключом с индексом — дев-дорожка
# зовёт slot_specs с include_fact_bound=True, дорожка модели — никогда.
FACT_BOUND_GROUP_SLOTS = ("stat_label",)

# Прайс Sonnet, $/1M токенов. Кэш: чтение ~0,1×, запись ~1,25× от входа.
PRICE_IN, PRICE_OUT = Decimal("3"), Decimal("15")
CACHE_READ_RATE, CACHE_WRITE_RATE = Decimal("0.1"), Decimal("1.25")
MILLION = Decimal("1000000")

# Системный промпт — по образу мастер-промпта Д12 §4, короче: слот это одна
# строка, а не два предложения. Правки только вместе с PROMPT_VERSION.
SYSTEM_PROMPT = """Ты — редактор коротких строк для одностраничного сайта. Ты не продавец и не копирайтер.

ЗАДАЧА
Ты получаешь карточку компании и список слотов — коротких текстовых полей
страницы. Для каждого слота ты пишешь одну строку. Больше ничего.

ЖЕЛЕЗНЫЕ ПРАВИЛА
1. Ты не имеешь права породить ни одного факта. Разрешено использовать только
   то, что находится между тегами <company>. Если факта нет во входных
   данных — его не существует.
2. Запрещено писать цифры: годы работы, число клиентов и мастеров, проценты,
   цены, сроки, рейтинг, количество отзывов. Телефон, адрес, почту, часы,
   названия услуг и имена людей ставит на страницу код — тебе их писать
   нельзя, даже если ты их угадал.
3. Запрещены обещания и оценки о компании: гарантия, скидка, акция, опыт,
   «лучший», «профессиональный», «современный», «под ключ», «качественно».
4. Запрещены: восклицательный знак, эмодзи, слова целиком заглавными, длинное
   тире, многоточие, риторические вопросы, обращение на «ты».
5. max_chars — предел длины строки В СИМВОЛАХ, вместе с пробелами. Строка
   длиннее предела не попадёт на страницу вообще.
6. Пустая строка лучше выдуманной: если для слота нечего сказать честно,
   верни для него "".
7. Вывод — строго JSON, ничего до и ничего после. Ключи — ровно те, что
   перечислены во входе, без добавленных и без пропущенных.

СЛОВАРЬ СЛОТОВ (поле kind во входе)
eyebrow — надстрочная строка над заголовком: ниша и город
headline — заголовок первого экрана: чем компания занимается
lede — одно предложение под заголовком или над формой: что человек получит,
  если напишет или позвонит
call_label — подпись кнопки звонка
secondary_label — подпись второй кнопки, она ведёт к форме на этой же странице
reassurance — короткая строка рядом с кнопкой: когда отвечают
portrait_alt — alt фотографии компании: что на ней видно
map_alt — alt карты: что на ней видно
address_label, hours_label, contacts_title, hours_title — подписи блоков
  адреса, часов и контактов
company_label — подпись над названием компании в блоке о ней
section_title — заголовок секции: услуг, товаров, часов работы, блока о
  компании или формы — смотри по роли секции
service_blurb — строка под названием услуги: что она включает. Само название
  услуги ставит код, ты его не видишь, — поэтому не начинай строку с него и
  не пересказывай его другими словами
about_text — абзац о компании: чем она занимается и для кого. Это единственный
  слот длиннее строки; два-три предложения, без оценок и без обещаний
name_label, phone_label, message_label, submit_label — подписи полей формы и
  кнопки отправки
honeypot_label — подпись скрытого поля формы: просьба не заполнять его
privacy_note — строка под формой: зачем нужны контакты из неё
legal_line — строка в подвале: это черновик, подготовленный для ознакомления

ФОРМАТ ВЫВОДА
{"вариант.слот": "строка", ...}

Язык вывода указан в теге <output_language>. Пиши только на нём."""

# Дополнительные правила для английского — те же, что в письмах (Д12 §4).
EN_RULES = """- Em-dash count must be exactly 0. Use a comma or a period.
- Straight quotes only.
- Never use: solution, journey, seamless, cutting-edge, comprehensive,
  world-class, trusted, premium, state of the art."""

# Few-shot: два примера на язык. Вход — та же форма, что уходит в user-блок.
# ВЫЧИТКА ОСНОВАТЕЛЕМ ПРЕДСТОИТ: строки написаны 22.08 по стоп-листам Д12 §4,
# на живых лидах не проверялись.
FEWSHOT = {
    "uk": [
        {
            "company": "назва: Стоматологія «Лінія» / місто: Ужгород / ніша: стоматологія",
            "slots": [
                {"slot": "hero_type_only.eyebrow", "kind": "eyebrow",
                 "role": "hero", "max_chars": 28},
                {"slot": "hero_type_only.headline", "kind": "headline",
                 "role": "hero", "max_chars": 64},
                {"slot": "hero_type_only.lede", "kind": "lede",
                 "role": "hero", "max_chars": 180},
                {"slot": "hero_type_only.call_label", "kind": "call_label",
                 "role": "hero", "max_chars": 24},
            ],
            "output": {
                "hero_type_only.eyebrow": "Стоматологія в Ужгороді",
                "hero_type_only.headline": "Запис на прийом у зручний час",
                "hero_type_only.lede": "Зателефонуйте або залиште номер у формі — "
                                       "підберемо час прийому і відповімо на запитання.",
                "hero_type_only.call_label": "Зателефонувати",
            },
        },
        {
            "company": "назва: Автосервіс «Колесо» / місто: Вінниця / ніша: автосервіс",
            "slots": [
                {"slot": "svc_list_icons.section_title", "kind": "section_title",
                 "role": "services", "max_chars": 42},
                {"slot": "cta_form_short.lede", "kind": "lede",
                 "role": "cta", "max_chars": 150},
                {"slot": "cta_form_short.submit_label", "kind": "submit_label",
                 "role": "cta", "max_chars": 24},
                {"slot": "footer_nap.legal_line", "kind": "legal_line",
                 "role": "footer", "max_chars": 90},
            ],
            "output": {
                "svc_list_icons.section_title": "Що ми робимо",
                "cta_form_short.lede": "Опишіть кількома словами, що з автомобілем, "
                                       "і залиште номер — передзвонимо.",
                "cta_form_short.submit_label": "Надіслати",
                "footer_nap.legal_line": "Чернетка сторінки, підготовлена для ознайомлення.",
            },
        },
    ],
    "en": [
        {
            "company": "name: Corner Bakery / city: Kosice / niche: bakery",
            "slots": [
                {"slot": "hero_split_map.eyebrow", "kind": "eyebrow",
                 "role": "hero", "max_chars": 28},
                {"slot": "hero_split_map.headline", "kind": "headline",
                 "role": "hero", "max_chars": 56},
                {"slot": "hero_split_map.lede", "kind": "lede",
                 "role": "hero", "max_chars": 160},
                {"slot": "hero_split_map.address_label", "kind": "address_label",
                 "role": "hero", "max_chars": 20},
            ],
            "output": {
                "hero_split_map.eyebrow": "Bakery in Kosice",
                "hero_split_map.headline": "Baked here, sold here",
                "hero_split_map.lede": "Call ahead for a larger order, or drop by "
                                       "during opening hours and pick what is on the shelf.",
                "hero_split_map.address_label": "Address",
            },
        },
        {
            "company": "name: Riverside Dental / city: Bratislava / niche: dental clinic",
            "slots": [
                {"slot": "svc_cards_3.section_title", "kind": "section_title",
                 "role": "services", "max_chars": 42},
                {"slot": "cta_form_short.privacy_note", "kind": "privacy_note",
                 "role": "cta", "max_chars": 120},
                {"slot": "footer_nap.contacts_title", "kind": "contacts_title",
                 "role": "footer", "max_chars": 20},
                {"slot": "footer_nap.hours_title", "kind": "hours_title",
                 "role": "footer", "max_chars": 20},
            ],
            "output": {
                "svc_cards_3.section_title": "What we do",
                "cta_form_short.privacy_note": "We use the details from this form "
                                               "only to reply to your message.",
                "footer_nap.contacts_title": "Contacts",
                "footer_nap.hours_title": "Opening hours",
            },
        },
    ],
}

# Подписи карточки — на языке вывода: смешивать язык инструкции с языком
# вывода вредно, модель начинает копировать обороты инструкции (Д12 §4).
LABELS = {
    "uk": {"name": "назва", "city": "місто", "niche": "ніша",
           "ask": "Напиши рядок для кожного слота. Відповідай JSON."},
    "en": {"name": "name", "city": "city", "niche": "niche",
           "ask": "Write one line per slot. Answer with JSON."},
}


@dataclass(frozen=True)
class SlotResult:
    """Тексты слотов либо честный отказ. empty — слоты, оставшиеся пустыми."""
    ok: bool
    texts: dict[str, str] = field(default_factory=dict)
    empty: list[str] = field(default_factory=list)
    reason: str = ""
    model: str = MODEL
    prompt_version: str = PROMPT_VERSION


async def fill_slots(profile, sections, lang: str, *,
                     lead_id: int | None = None) -> SlotResult:
    """Заполнить free-слоты композиции. Одна перегенерация на нарушивший слот."""
    specs = slot_specs(sections)
    if not specs:
        return SlotResult(True)
    if lang not in LABELS:
        return SlotResult(False, reason=f"нет промпта для языка {lang!r}")
    if not config.ANTHROPIC_API_KEY:
        return SlotResult(False, reason="не задан ANTHROPIC_API_KEY")

    texts, reason = await _ask(profile, specs, lang, lead_id)
    if reason:
        return SlotResult(False, reason=reason)

    bad = [spec for spec in specs
           if not _fits(spec, texts.get(spec["slot"]))
           and not _blank_ok(spec, texts.get(spec["slot"]))]
    if bad:
        log.info("черновик лида %s: перегенерация слотов %s", lead_id,
                 ", ".join(spec["slot"] for spec in bad))
        again, reason = await _ask(profile, _tighter(bad), lang, lead_id)
        if reason:
            return SlotResult(False, reason=reason)
        texts.update(again)

    final, empty = {}, []
    for spec in specs:
        value = texts.get(spec["slot"])
        if _fits(spec, value):
            final[spec["slot"]] = value.strip()
            continue
        final[spec["slot"]] = ""
        if not _blank_ok(spec, value):
            # второе нарушение подряд: слот пуст, судьбу секции решает
            # лестница деградации site_factory (8.31)
            empty.append(spec["slot"])
    return SlotResult(True, texts=final, empty=empty)


def _blank_ok(spec, value) -> bool:
    """Пустая строка на слот элемента — честный ответ, а не промах.

    Правило 6 промпта велит вернуть "", когда сказать нечего, а названий услуг
    модель не видит вовсе, — для блёрба это рутина. Перегенерация тут лечила бы
    длину, которой нет, вторым платным вызовом, а в empty пустой блёрб не
    значит ничего: карточка услуги без пояснения — карточка. Ключ при этом
    остаётся: apply_free_texts по нему оставит элемент без строки, а не
    подставит заготовку рецепта.
    """
    return bool(spec.get("grouped")) and isinstance(value, str) and not value.strip()


def _tighter(specs) -> list[dict]:
    """Спеки перегенерации: показать модели предел на пятую часть меньше.

    Модель считает символы неточно и на повторе промахивается на те же
    два-три знака. Проверка остаётся по настоящему лимиту — ужимается только
    цель, чтобы запас покрыл ошибку счёта; третьего вызова всё равно нет.
    """
    out = []
    for spec in specs:
        limit = spec.get("max_chars")
        if limit:
            spec = {**spec, "max_chars": max(1, limit - max(2, limit // 5))}
        out.append(spec)
    return out


def slot_specs(sections, include_fact_bound: bool = False) -> list[dict]:
    """Что показать модели: только free-слоты, только имя, роль и лимит.

    Fact-слоты сюда не попадают ни именем, ни значением — телефон и адрес
    модель не видит вовсе.

    Слоты группы идут по одному на элемент, ключом «вариант.слот[индекс]»:
    у трёх услуг три разных блёрба, и одной строкой на всех их не закрыть.
    Сколько элементов в группе, решил уже состав секции, — поэтому индексы
    берутся из готовой композиции, а не из repeat контракта.

    include_fact_bound добавляет к ним FACT_BOUND_GROUP_SLOTS. Это дорожка
    готовых текстов: их пишет человек, который цифру рядом видит. Дорожка
    модели зовёт без флага — она цифры не видит и подписать их не может.
    """
    specs = []
    for section in sections:
        for spec in sf_slots.free_specs(section["contract"]):
            specs.append({"slot": f"{section['variant']}.{spec['name']}",
                          "kind": spec["name"], "role": section["role"],
                          "max_chars": spec.get("max_chars")})
        for spec in sf_slots.group_free_specs(section["contract"]):
            if spec["name"] in FACT_BOUND_GROUP_SLOTS and not include_fact_bound:
                continue
            rows = section["slots"].get(spec["group"]) or []
            for index in range(len(rows)):
                specs.append({"slot": f"{section['variant']}.{spec['name']}[{index}]",
                              "kind": spec["name"], "role": section["role"],
                              "max_chars": spec.get("max_chars"), "grouped": True})
    return specs


def system_prompt(lang: str) -> str:
    parts = [SYSTEM_PROMPT]
    if lang == "en":
        parts.append(EN_RULES)
    parts.append(_fewshot_block(lang))
    return "\n\n".join(parts)


def user_prompt(profile, specs, lang: str) -> str:
    return (f"<output_language>{lang}</output_language>\n"
            f"<company>{_company(profile, lang)}</company>\n"
            f"<slots>{json.dumps(specs, ensure_ascii=False)}</slots>\n\n"
            f"{LABELS[lang]['ask']}")


# --- внутреннее ---------------------------------------------------------------

_client = None


def client():
    """Ленивый клиент: без ключа сюда не доходит ни один вызов."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def _ask(profile, specs, lang, lead_id):
    """(тексты, причина отказа). Ровно одно из двух непустое."""
    if await costs.cap_reached():
        return {}, "месячный кэп расходов на ИИ исчерпан"
    try:
        response = await client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # Кэшируемый префикс: системный промпт со словарём слотов и
            # примеры языка вывода. Карточка лида идёт дальше, в user-блоке.
            system=[{"type": "text", "text": system_prompt(lang),
                     "cache_control": {"type": "ephemeral"}}],
            # Подписи кнопок рассуждений не требуют, а включённое мышление
            # съело бы max_tokens. Sampling-параметры не передаём — как и в
            # письмах: разнообразие даёт карточка, а не температура.
            thinking={"type": "disabled"},
            messages=[{"role": "user",
                       "content": user_prompt(profile, specs, lang)}],
        )
    except anthropic.APIError as e:
        log.warning("черновик лида %s: ошибка API: %s", lead_id, e)
        return {}, f"модель недоступна: {e.__class__.__name__}"

    await _log_cost(response.usage, lead_id)
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    data = parse_model_json(text)
    if data is None:
        log.warning("черновик лида %s: ответ не JSON: %.200s", lead_id, text)
        return {}, "модель ответила не JSON"
    return {k: v for k, v in data.items() if isinstance(v, str)}, ""


def parse_model_json(text: str) -> dict | None:
    """JSON-объект из ответа модели. None — разобрать нечего.

    Модели заворачивают JSON в markdown-фенс вопреки промпту; фенс — это
    упаковка, а не содержимое, поэтому срезается до разбора. Всё, что не
    словарь, отвергается: списку и строке в слотах взяться неоткуда.
    """
    text = (text or "").strip()
    if text.startswith("```") and text.endswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1:-3].strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _fits(spec, value) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    limit = spec.get("max_chars")
    return not limit or len(value.strip()) <= limit


def _company(profile, lang: str) -> str:
    """Карточка для промпта: название, город, ниша — и ничего больше.

    Ни телефона, ни адреса, ни почты, ни списка услуг: всё это fact-слоты,
    и попасть в текст модели они не должны даже случайной перефразировкой.
    """
    label = LABELS[lang]
    fields = (("name", profile.name), ("city", profile.city),
              ("niche", profile.niche))
    return " / ".join(f"{label[key]}: {feature.value}"
                      for key, feature in fields
                      if feature.known and feature.value)


def _fewshot_block(lang: str) -> str:
    lines = []
    for pair in FEWSHOT[lang]:
        lines.append(f"Вход: <company>{pair['company']}</company>\n"
                     f"<slots>{json.dumps(pair['slots'], ensure_ascii=False)}</slots>")
        lines.append(json.dumps(pair["output"], ensure_ascii=False))
    return "\n\n".join(lines)


async def _log_cost(usage, lead_id):
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = (
        (Decimal(usage.input_tokens) + Decimal(written) * CACHE_WRITE_RATE
         + Decimal(read) * CACHE_READ_RATE) * PRICE_IN
        + Decimal(usage.output_tokens) * PRICE_OUT
    ) / MILLION
    await costs.log_cost(op="draft", model=MODEL, cost_usd=cost,
                         input_tokens=usage.input_tokens,
                         output_tokens=usage.output_tokens,
                         cache_read_tokens=read, lead_id=lead_id)
