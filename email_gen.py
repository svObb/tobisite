"""Сборка письма 1 по слоям Д12 §1 и конструкторы писем 2 и 3.

Разделение труда жёсткое: факты даёт карточка и работник, порядок слов в двух
предложениях — модель. Всё остальное — константы и подстановка. Если модель
вернула null, невалидный JSON или ключа нет вовсе, письма просто не будет:
карточка уходит в ручную ветку с причиной. Выдумывать факт вместо модели код
не умеет, и это главное свойство модуля.

Отправки здесь нет и быть не может: конвейер v1 заканчивается одобрением.
"""
import json
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

import anthropic

import config
import costs
import email_legal
import email_lint
import phrases
from email_fewshot import FEWSHOT
from models import Session, gap_stale, suppression_hit

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
PROMPT_VERSION = "p1"
# Двум предложениям JSON'ом хватает с запасом; больше и не нужно — длинный
# ответ здесь означает, что модель начала сочинять.
MAX_TOKENS = 400
# Прайс Sonnet 5, $/1M токенов. Кэш: чтение ~0,1×, запись ~1,25× от входа.
# Цифры списочные: занижать нельзя, иначе месячный кэп перестанет держать.
PRICE_IN, PRICE_OUT = Decimal("3"), Decimal("15")
CACHE_READ_RATE, CACHE_WRITE_RATE = Decimal("0.1"), Decimal("1.25")
MILLION = Decimal("1000000")

# Черновик держим 30 дней — это единственная цифра письма 3, и она наша
# собственная, а не выдуманная про рынок.
DRAFT_HOLD_DAYS = 30

# Системный промпт — дословно из Д12 §4. Правки только там и только вместе с
# инкрементом PROMPT_VERSION: метрики очереди считаются по версиям промпта.
SYSTEM_PROMPT = """Ты — редактор коротких деловых писем. Ты не продавец и не копирайтер.

ЗАДАЧА
Ты получаешь карточку компании, описание готового черновика сайта и
УЖЕ ГОТОВУЮ первую строку письма, которую написал человек, лично
открывший сайт этой компании с телефона.
Ты дописываешь ровно два предложения: bridge и offer. Больше ничего.

ЖЕЛЕЗНЫЕ ПРАВИЛА
1. Ты не имеешь права породить ни одного факта. Разрешено использовать
   ТОЛЬКО то, что находится между тегами <lead>, <observation> и <draft>.
   Если факта нет во входных данных — его не существует.
   Запрещено писать «мы работаем с десятками компаний», «наш опыт
   показывает», «многие в вашем городе», «как правило» и любые
   обобщения о рынке, клиентах или статистике.
2. Ты не трогаешь первую строку. Она приходит готовой. Ты её не
   переписываешь, не сокращаешь и не пересказываешь своими словами.
3. Ты не упоминаешь цену, срок, гарантию, скидку, акцию, портфолио.
4. Запрещены: восклицательный знак, эмодзи, слова целиком заглавными,
   длинное тире, многоточие, кавычки-ёлочки вокруг своих слов.
5. Ты пишешь от первого лица единственного числа. Форма «мы» разрешена
   ровно один раз и только про компанию как организацию.
6. Вывод — строго JSON, ничего до и ничего после.

СЛОТ bridge — ровно одно предложение, 8–16 слов
Назначение: связать наблюдение человека с его бытовым следствием для
посетителя сайта.
Можно: описать, что делает обычный человек, столкнувшись с этим.
Нельзя: оценки («это плохо», «к сожалению»), сочувствие, комплименты,
риторические вопросы, слова «проблема», «важно понимать», «дело в том».

СЛОТ offer — ровно одно предложение, 10–18 слов
Назначение: сообщить, что черновик главной страницы уже готов на
реальных данных компании, и назвать ОДНУ конкретную вещь из <draft>,
которая закрывает именно этот разрыв.
Только совершённое действие: «сделал», «собрал», «поставил».
Запрещено условное наклонение: «мог бы», «предлагаю», «готов сделать».
Частицы «бы», «би», «would», «could» запрещены.

СТОП-СЛОВА (любая форма, любой падеж, полный запрет)
бесплатно, безкоштовно, free, уникальное предложение, унікальна
пропозиція, гарантия, гарантія, guarantee, акция, акція, скидка,
знижка, discount, срочно, терміново, urgent, 100%, лучший, найкращий,
best, профессиональный, професійний, professional, современный,
сучасний, modern, эффективный, ефективний, мощный, потужний,
комплексный, инновационный, оптимизация, под ключ, seamless,
cutting-edge, robust, comprehensive, leverage, unlock, delve, elevate,
streamline, empower, tapestry, pivotal.

ЗАПРЕЩЁННЫЕ КОНСТРУКЦИИ
- «Надеюсь, у вас всё хорошо» и любые вежливости после первой строки.
- «Не просто X, а Y» / «это не X, это Y» / «not just X but Y».
- Три однородных члена подряд («быстро, удобно и надёжно»).
- «Хотел обратиться», «решил написать», «обращаюсь к вам».
- «Что это значит для вас», «давайте разберёмся».
- Предложение, начинающееся со слова «Как».
- Любая фраза, которая одинаково подошла бы другой компании.

ФОРМАТ ВЫВОДА
{"bridge": "...", "offer": "..."}

Язык вывода указан в теге <output_language>. Пиши только на нём.

Если входных данных не хватает, чтобы написать честно, верни
{"bridge": null, "offer": null, "reason": "чего не хватает"}.
Пустой ответ лучше выдуманного."""

# Дополнительные правила для английского — дословно из Д12 §4, идут перед
# английским блоком few-shot.
EN_RULES = """- Em-dash count must be exactly 0. Use a comma or a period.
- Use contractions: I'd, it's, they'll, you're. Formal "I am", "it is",
  "do not" reads as machine-written.
- Never open with: I hope this email finds you well / Just reaching out /
  I wanted to reach out / I came across your.
- Never use: would love to, feel free to, at your earliest convenience,
  circle back, touch base, synergy, solution, journey.
- Straight quotes only."""

# Подписи полей в промпте — на языке вывода: смешивать язык инструкции с
# языком вывода вредно, модель начинает копировать обороты инструкции (Д12 §4).
LABELS = {
    "uk": {"name": "назва", "city": "місто", "niche": "ніша",
           "contact": "контакт", "type": "тип", "value": "значення",
           "note": "деталь", "checked": "перевірено", "phone": "з телефона",
           "ask": "Напиши bridge і offer."},
    "en": {"name": "name", "city": "city", "niche": "niche",
           "contact": "contact", "type": "type", "value": "value",
           "note": "note", "checked": "checked", "phone": "on a phone",
           "ask": "Write the bridge and the offer."},
}

UK_GREETING = "Доброго дня!"
EN_GREETINGS = ("Hi {name},", "Hi,")

# CTA: 4 варианта, вариант выбирается по hash(lead_id) % 4. Без ссылок —
# письмо 1 идёт без единой ссылки (9.1). Ровно один вопрос на письмо.
CTA = {
    "uk": [
        "Скинути подивитись? Відповідайте «так».",
        "Показати? Напишіть «так», і я скину.",
        "Хочете глянути? Достатньо відповісти «так».",
        "Надіслати вам? Відповідайте одним словом.",
    ],
    "en": [
        "Want me to send it over? Just reply yes.",
        "Should I send it? One word back is enough.",
        "Would it be okay if I showed you? Just reply yes.",
        "Can I send it to you? A yes is enough.",
    ],
}

# Письмо 2: ссылка на превью. Письмо 3: цифра-пруф (срок хранения черновика —
# наша собственная цифра) и break-up. Тексты — константы, модель не зовётся.
LETTER_2 = {
    "uk": ("Ось чернетка: {host}\n"
           "Нічого робити не треба, просто гляньте з телефона."),
    "en": ("Here is the draft: {host}\n"
           "Nothing to do on your side, just open it on your phone."),
}
LETTER_3 = {
    "uk": ("Якщо не актуально — просто напишіть «ні», більше не турбуватиму.\n"
           "Чернетку потримаю ще {days} днів."),
    "en": ("If this is not for you, just reply no and I will stop writing.\n"
           "I will keep the draft for another {days} days."),
}
SUBJECT_2 = {"uk": "Чернетка вашої головної", "en": "your draft is ready"}
SUBJECT_3 = {"uk": "Закриваю тему", "en": "closing this out"}


@dataclass(frozen=True)
class EmailResult:
    """Собранное письмо либо честный отказ. needs_manual == not ok."""
    ok: bool
    lang: str | None = None
    subject: str = ""
    body: str = ""
    slots: dict[str, str] = field(default_factory=dict)
    anchors: list[str] = field(default_factory=list)
    model: str | None = None
    prompt_version: str = PROMPT_VERSION
    lint: email_lint.LintResult | None = None
    reason: str = ""

    @property
    def needs_manual(self) -> bool:
        return not self.ok


async def build_email(lead, draft_summary: str) -> EmailResult:
    """Письмо 1 для лида. При любом сомнении — needs_manual с причиной."""
    lang = phrases.lang_of(lead)
    if lang is None:
        return _manual(f"нет фраз для языка «{lead.language}» — письмо руками")
    if gap_stale(lead):
        return _manual("наблюдение устарело, нужно переснять", lang)
    first_line = phrases.first_line(lead)
    if not first_line:
        return _manual("наблюдение не снято: первую строку собрать не из чего",
                       lang)
    async with Session() as s:
        if await suppression_hit(s, lead):
            return _manual("лид в стоп-листе: писать нельзя", lang)
    if not config.ANTHROPIC_API_KEY:
        return _manual("не задан ANTHROPIC_API_KEY", lang)

    first_line = _as_sentence(first_line)
    # Одна перегенерация на письмо (Д12 §5): второй fail линтера — ручная ветка.
    for attempt in (1, 2):
        if await costs.cap_reached():
            return _manual("месячный кэп расходов на ИИ исчерпан", lang)
        slots, reason = await _bridge_and_offer(lead, lang, first_line,
                                                draft_summary)
        if reason:
            return _manual(reason, lang)
        result = _assemble(lead, lang, first_line, slots)
        if result.lint.ok:
            return result
        log.info("письмо лида %s не прошло линтер (попытка %s): %s",
                 lead.id, attempt, "; ".join(result.lint.fails))
    return _manual("линтер: " + "; ".join(result.lint.fails), lang,
                   lint=result.lint)


def build_email_2(lead, preview_host: str) -> EmailResult:
    """Касание 2: ссылка на превью. Слой 0 Д12 §1 — чистая константа."""
    lang = phrases.lang_of(lead)
    if lang is None:
        return _manual(f"нет фраз для языка «{lead.language}» — письмо руками")
    body = LETTER_2[lang].format(host=preview_host)
    return _constant_letter(lead, lang, SUBJECT_2[lang], body)


def build_email_3(lead) -> EmailResult:
    """Касание 3: срок хранения черновика и выход из переписки."""
    lang = phrases.lang_of(lead)
    if lang is None:
        return _manual(f"нет фраз для языка «{lead.language}» — письмо руками")
    body = LETTER_3[lang].format(days=DRAFT_HOLD_DAYS)
    return _constant_letter(lead, lang, SUBJECT_3[lang], body)


def signature(lead, lang: str, *, with_link: bool = False) -> str:
    """Подпись, юридические строки и opt-out — слой 0 (Д12 §1), см. email_legal."""
    return email_legal.footer(lead, lang, with_link=with_link)


def greeting(lang: str, name: str) -> str:
    """Приветствие письма.

    В украинском имени нет никогда: обращение по имени требует звательного
    падежа («Олена» → «Олено»), а падежи мы не генерируем (решение 10 этапа).
    Нейтральное приветствие дешевле, чем ошибка в имени человека.
    """
    if lang == "uk":
        return UK_GREETING
    with_name, without = EN_GREETINGS
    return with_name.format(name=name) if name else without


def compose_body(slots: dict) -> str:
    """Тело письма из слотов. Порядок слоёв — Д12 §1, пустые слоты выпадают.

    Отдельной функцией, потому что после правки слота в очереди письмо
    пересобирается ровно этими же правилами (queue_service).
    """
    prose = " ".join(p for p in (slots.get("first_line"), slots.get("bridge"),
                                 slots.get("offer")) if p)
    return "\n\n".join(p for p in (slots.get("greeting"), prose,
                                   slots.get("cta"), slots.get("signature"))
                       if p)


def anchors_of(lead) -> list[str]:
    """Якоря карточки для линтера: чем их меньше, тем безличнее письмо."""
    number = re.search(r"\d+", lead.gap_value or "")
    # Ниша в карточке записана по-русски, а в письме стоит её форма из зачина:
    # по ключу config.NICHES якорь не нашёлся бы ни в одном письме.
    return [_contact_name(lead), lead.city,
            phrases.niche_form(lead) or lead.niche, lead.name,
            number.group(0) if number else ""]


# --- внутреннее ---------------------------------------------------------------

_client = None


def client():
    """Ленивый клиент: без ключа сюда не доходит ни один вызов."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def _bridge_and_offer(lead, lang, first_line, draft_summary):
    """(слоты, причина отказа). Ровно одно из двух непустое."""
    try:
        response = await client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            # Кэшируемый префикс: системный промпт + девять примеров языка
            # вывода. Меняется только вместе с PROMPT_VERSION, поэтому кэш
            # живёт между письмами; факты лида идут дальше, в user-блоке.
            system=[{"type": "text", "text": system_prompt(lang),
                     "cache_control": {"type": "ephemeral"}}],
            # Ответ на два предложения по жёстким правилам рассуждений не
            # требует, а включённое мышление съело бы max_tokens.
            # temperature (в плане — 0.4) не передаём: на claude-sonnet-5
            # sampling-параметры удалены, запрос с ними отвечает 400.
            # Разнообразие письмам даёт не температура, а карточка лида.
            thinking={"type": "disabled"},
            messages=[{"role": "user",
                       "content": user_prompt(lead, lang, first_line,
                                              draft_summary)}],
        )
    except anthropic.APIError as e:
        log.warning("письмо лида %s: ошибка API: %s", lead.id, e)
        return {}, f"модель недоступна: {e.__class__.__name__}"

    await _log_cost(lead, response.usage)
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        log.warning("письмо лида %s: ответ не JSON: %.200s", lead.id, text)
        return {}, "модель ответила не JSON"
    if not isinstance(data, dict):
        return {}, "модель ответила не JSON"
    bridge, offer = data.get("bridge"), data.get("offer")
    if not (isinstance(bridge, str) and isinstance(offer, str)
            and bridge.strip() and offer.strip()):
        # Пустой ответ лучше выдуманного: модель сама сказала, что данных мало.
        return {}, f"модель не взялась писать: {data.get('reason') or 'пусто'}"
    return {"bridge": bridge.strip(), "offer": offer.strip()}, ""


def system_prompt(lang: str) -> str:
    parts = [SYSTEM_PROMPT]
    if lang == "en":
        parts.append(EN_RULES)
    parts.append(_fewshot_block(lang))
    return "\n\n".join(parts)


def user_prompt(lead, lang: str, first_line: str, draft_summary: str) -> str:
    label = LABELS[lang]
    card = [f"{label['name']}: {lead.name}", f"{label['city']}: {lead.city}",
            f"{label['niche']}: {lead.niche}"]
    name = _contact_name(lead)
    if name:
        card.append(f"{label['contact']}: {name}")

    observation = [f"{label['type']}: {lead.gap_type}"]
    if lead.gap_value:
        observation.append(f"{label['value']}: {lead.gap_value}")
    # gap_note не переводится и в EN-письмо не попадает (Д12 §3).
    if lead.gap_note and lang == "uk":
        observation.append(f"{label['note']}: {lead.gap_note}")
    if lead.gap_captured_at:
        observation.append(f"{label['checked']}: {label['phone']}, "
                           f"{lead.gap_captured_at:%d.%m.%Y}")

    return (f"<output_language>{lang}</output_language>\n"
            f"<lead>{' / '.join(card)}</lead>\n"
            f"<observation>{' / '.join(observation)}</observation>\n"
            f"<first_line>{first_line}</first_line>\n"
            f"<draft>{draft_summary}</draft>\n\n"
            f"{label['ask']}")


def _fewshot_block(lang: str) -> str:
    lines = []
    for pair in FEWSHOT[lang]:
        lines.append(_fewshot_input(pair["input"], lang))
        lines.append(json.dumps(pair["output"], ensure_ascii=False))
    return "\n\n".join(lines)


def _fewshot_input(data: dict, lang: str) -> str:
    """Строка примера в том же виде, что в Д12 §4: gap=, value=, ніша=, draft=."""
    quote = "«{}»" if lang == "uk" else '"{}"'
    parts = [f"gap={data['gap']}"]
    if data.get("value"):
        parts.append(f"value={data['value']}")
    parts.append(f"{LABELS[lang]['niche']}={data['niche']}")
    parts.append(f"draft={quote.format(data['draft'])}")
    return "Вход: " + ", ".join(parts)


def _assemble(lead, lang, first_line, slots) -> EmailResult:
    slots = dict(slots)
    slots["greeting"] = greeting(lang, _contact_name(lead))
    slots["first_line"] = first_line
    slots["cta"] = phrases.variant(lead.id, CTA[lang])
    # ссылки в письме 1 нет ни одной (9.1), поэтому и ссылки отписки тоже:
    # отказаться от переписки здесь можно ответом «STOP»
    slots["signature"] = signature(lead, lang)

    body = compose_body(slots)
    subject = phrases.subject(lead)
    anchors = anchors_of(lead)
    result = email_lint.lint(body, lang=lang, slots=slots, anchors=anchors,
                             subject=subject,
                             legal=email_legal.missing(lead, lang))
    return EmailResult(ok=result.ok, lang=lang, subject=subject, body=body,
                       slots=slots, anchors=anchors, model=MODEL, lint=result,
                       reason="" if result.ok else "; ".join(result.fails))


def _constant_letter(lead, lang, subject, text) -> EmailResult:
    # письма 2 и 3 ссылки уже несут (превью), поэтому и ссылка отписки идёт
    # именно здесь — там, где она никакого правила не нарушает
    slots = {"greeting": greeting(lang, _contact_name(lead)),
             "text": text, "signature": signature(lead, lang, with_link=True)}
    body = "\n\n".join(p for p in slots.values() if p)
    return EmailResult(ok=True, lang=lang, subject=subject, body=body,
                       slots=slots, anchors=anchors_of(lead))


async def _log_cost(lead, usage):
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cost = (
        (Decimal(usage.input_tokens) + Decimal(written) * CACHE_WRITE_RATE
         + Decimal(read) * CACHE_READ_RATE) * PRICE_IN
        + Decimal(usage.output_tokens) * PRICE_OUT
    ) / MILLION
    await costs.log_cost(op="letter", model=MODEL, cost_usd=cost,
                         input_tokens=usage.input_tokens,
                         output_tokens=usage.output_tokens,
                         cache_read_tokens=read, lead_id=lead.id)


def _manual(reason: str, lang: str | None = None, lint=None) -> EmailResult:
    return EmailResult(ok=False, lang=lang, reason=reason, lint=lint)


def _contact_name(lead) -> str:
    """Имя контакта, если оно известно. Отдельного поля в схеме пока нет —
    оно придёт с обогащением карточки, поэтому читаем мягко."""
    return str(getattr(lead, "contact_name", "") or "").strip()


def _as_sentence(line: str) -> str:
    line = line.strip()
    if not line:
        return line
    if line[-1] not in ".?!":
        line += "."
    return line[0].upper() + line[1:]
