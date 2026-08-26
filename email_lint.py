"""Линтер письма 1 (Д12 §5): строгость и харизма в проверяемых числах.

Вход — собранное письмо, разложенное по слотам, поэтому линтер точно знает,
какие куски написала модель, а какие пришли из карточки и из констант. Это
важно: стоп-лист продаж и маркеры ИИ применяются ТОЛЬКО к сгенерированным
слотам, иначе «гарантія» в названии компании роняет валидное письмо (Д12 §5).

Ролей у результата две. fails — письмо не идёт в очередь: email_gen один раз
перегенерирует, при повторном fail карточка уходит в ручную ветку. warns —
человеку в очереди видно, но кнопка не блокируется.

TODO: доля глаголов от знаменательных слов ≥18% (Д12 §5, харизма) не считается:
для POS-разметки нужен spaCy с языковыми моделями (~50 МБ на язык) ради одной
warn-метрики — решение 1 этапа. Разброс длин предложений считается без разметки.
"""
import re
import statistics
from dataclasses import dataclass, field

# Слоты, которые пишет модель: только к ним применимы стоп-лист и маркеры ИИ.
GENERATED = ("bridge", "offer")
# Проза письма. Приветствие, CTA и подпись — константы, одинаковые во всех
# письмах: считать по ним ритм бессмысленно (метрика показывала бы одно и то же
# число независимо от текста), поэтому средняя длина, максимум и разброс
# предложений меряются по трём настоящим предложениям письма.
PROSE = ("first_line", "bridge", "offer")

WORDS_MIN, WORDS_MAX = 60, 90
WORDS_HARD_MIN, WORDS_HARD_MAX = 50, 110
AVG_MIN, AVG_MAX = 9, 14
SENTENCE_MAX = 20
STDEV_MIN = 4
ANCHORS_MIN = 4

# Стоп-слова продаж (Д12 §4). Славянские — основами: «любая форма, любой падеж»
# дешевле всего ловится подстрокой, а морфоанализатора у нас нет. Английские —
# по границам слова: подстрокой «best» поймала бы «best practices» внутри
# чужого названия.
STOP_STEMS = (
    "бесплатн", "безкоштовн", "уникальное предложение", "унікальна пропозиція",
    "гаранти", "гаранті", "акци", "акці", "скидк", "знижк", "срочно",
    "терміново", "лучш", "найкращ", "профессиональн", "професійн",
    "современн", "сучасн", "эффективн", "ефективн", "мощн", "потужн",
    "комплексн", "инновацион", "інновацій", "оптимизац", "оптимізац",
    "под ключ", "під ключ", "100%",
)
STOP_WORDS = (
    "free", "guarantee", "discount", "urgent", "best", "professional",
    "modern", "seamless", "cutting-edge", "robust", "comprehensive",
    "leverage", "unlock", "delve", "elevate", "streamline", "empower",
    "tapestry", "pivotal",
)
STOP_WORDS_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(w) for w in STOP_WORDS), re.IGNORECASE
)

# Условное наклонение в offer: обещание вместо совершённого действия (Д12 §4).
CONDITIONAL_RE = re.compile(r"\b(?:бы|би|would|could)\b", re.IGNORECASE)

# Диапазоны эмодзи и пиктограмм явными кодами: печатные символы в классе
# слишком легко потерять при правке файла.
EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF←-⇿☀-➿⬀-⯿️]"
)
LINK_RE = re.compile(
    r"https?://|www\.|\b[\w-]+\.(?:com|net|org|io|ua|sk|co|site|dev|shop|info)\b",
    re.IGNORECASE,
)
WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
SENTENCE_SPLIT_RE = re.compile(r"[.!?…]+")

# Маркеры ИИ (Д12 §5). Вежливости после первой строки, «не просто X, а Y»,
# зачин «Как», ёлочки вокруг своих слов — каждый ловится по отдельности ниже.
POLITENESS = (
    "i hope this email finds you well", "hope this finds you well",
    "hope you are doing well", "hope you're doing well",
    "just reaching out", "i wanted to reach out", "i came across your",
    "надеюсь, у вас всё хорошо", "надеюсь, у вас все хорошо",
    "сподіваюся, у вас все добре", "сподіваюсь, у вас все гаразд",
)
NOT_JUST_RE = re.compile(
    r"не просто\s.+?,\s*а\s|це не\s.+?,\s*це\s|это не\s.+?,\s*это\s"
    r"|not just\s.+?\bbut\b",
    re.IGNORECASE,
)
OPENER_RE = re.compile(r"^(?:Как|Як|How)\b")
GUILLEMETS_RE = re.compile(r"[«»]")
# Три однородных подряд («быстро, удобно и надёжно» / «fast, simple and clear»).
# Без разметки ловится формой: перечисление коротких кусков с союзом в конце
# либо три коротких куска через запятую.
TRIPLE_RE = re.compile(
    r"[\w'’-]+,\s+[\w'’-]+,?\s+(?:and|і|та|й|и)\s+[\w'’-]+"
    r"|[\w'’-]+,\s+[\w'’-]+,\s+[\w'’-]+(?!\s*[,\w])",
    re.IGNORECASE,
)
# Сокращения в английском: их полное отсутствие читается как машинный текст.
CONTRACTION_RE = re.compile(r"\b\w+['’](?:s|t|re|ve|ll|d|m)\b", re.IGNORECASE)
EM_DASH = "—"


@dataclass(frozen=True)
class LintResult:
    ok: bool
    fails: list[str] = field(default_factory=list)
    warns: list[str] = field(default_factory=list)


def lint(body: str, *, lang: str, slots: dict[str, str], anchors=(),
         subject: str = "", legal=()) -> LintResult:
    """Проверяет собранное письмо 1. slots — тот же разбор, что делал email_gen.

    Метрики считаются по телу письма; тема участвует только в поиске якорей —
    название компании чаще всего стоит именно там, и читатель его видит.

    legal — чего не хватает подписи по закону (email_legal.missing, 9.8–9.9).
    Это warn, а не fail: письмо от этого не становится плохим, а отправки в
    конвейере всё равно нет. Отправлять его нельзя, и об этом дежурный читает
    прямо в карточке — жёсткий запрет стоит там, где появится сама отправка.
    """
    fails, warns = [], []
    warns += [f"нельзя отправлять: {gap}" for gap in legal]
    generated = " ".join(slots.get(name, "") for name in GENERATED).strip()
    sentences = [s for name in PROSE for s in _sentences(slots.get(name, ""))]
    lengths = [len(_words(s)) for s in sentences]

    words = len(_words(body))
    if not WORDS_HARD_MIN <= words <= WORDS_HARD_MAX:
        fails.append(f"слов в письме {words}, допустимо "
                     f"{WORDS_HARD_MIN}–{WORDS_HARD_MAX}")
    elif not WORDS_MIN <= words <= WORDS_MAX:
        warns.append(f"слов в письме {words}, норма {WORDS_MIN}–{WORDS_MAX}")

    if lengths:
        longest = max(lengths)
        if longest > SENTENCE_MAX:
            fails.append(f"самое длинное предложение {longest} слов, "
                         f"предел {SENTENCE_MAX}")
        avg = statistics.fmean(lengths)
        if not AVG_MIN <= avg <= AVG_MAX:
            warns.append(f"средняя длина предложения {avg:.1f} слов, "
                         f"норма {AVG_MIN}–{AVG_MAX}")
        stdev = statistics.pstdev(lengths) if len(lengths) > 1 else 0
        if stdev < STDEV_MIN:
            warns.append(f"разброс длин предложений {stdev:.1f} слов, "
                         f"нужно от {STDEV_MIN}: одинаковые предложения — "
                         f"ритм модели")

    # Приветствие и юридический низ — константы слоя 0: украинское «Доброго
    # дня!» пишется со знаком по норме языка, а STOP в строке отписки — слово
    # команды, а не крик. Запреты Д12 §5 — про прозу письма, поэтому оба слота
    # из проверок знака и регистра исключены. Всё, что человек переписал
    # целиком, разбора по слотам не имеет и проверяется без единого исключения.
    prose_only = _without(body, slots.get("greeting"), slots.get("signature"))
    if "!" in prose_only:
        fails.append("восклицательный знак")
    if EMOJI_RE.search(body):
        fails.append("эмодзи")
    caps = [w for w in WORD_RE.findall(prose_only) if w.isupper()]
    if caps:
        fails.append(f"слово заглавными: {caps[0]}")

    questions = body.count("?")
    if questions != 1:
        fails.append(f"вопросительных предложений {questions}, нужно ровно 1")

    # Двойной пробел — след пустой подстановки («вантажилась  секунд»):
    # сам текст такого не порождает, а дыра в цифре превращает факт в ложь
    if "  " in body:
        fails.append("двойной пробел: похоже на пустую подстановку")

    if LINK_RE.search(body):
        fails.append("ссылка в письме 1")

    low = generated.lower()
    hits = [stem for stem in STOP_STEMS if stem in low]
    hits += [m.group(0).lower() for m in STOP_WORDS_RE.finditer(generated)]
    if hits:
        fails.append(f"стоп-слово продаж: {hits[0]}")

    if CONDITIONAL_RE.search(slots.get("offer", "")):
        fails.append("условное наклонение в offer вместо совершённого действия")

    fails += _ai_markers(generated, lang)

    visible = f"{subject}\n{body}".lower()
    found = {a for a in (str(x or "").strip() for x in anchors)
             if a and a.lower() in visible}
    if len(found) < ANCHORS_MIN:
        warns.append(f"якорей из карточки {len(found)}, нужно от {ANCHORS_MIN}: "
                     f"письмо подошло бы другой компании")

    return LintResult(ok=not fails, fails=fails, warns=warns)


def word_count(text: str) -> int:
    """Слов в тексте по тем же правилам, что считает линтер.

    Наружу — потому что порог «прочитал слишком быстро» в очереди считается
    от того же числа, что и метрики письма (Д12 §6.2).
    """
    return len(_words(text))


def _ai_markers(generated: str, lang: str) -> list[str]:
    """Маркеры ИИ ищутся только в том, что написала модель (Д12 §5)."""
    if not generated:
        return []
    found = []
    low = generated.lower()
    hit = next((p for p in POLITENESS if p in low), None)
    if hit:
        found.append(f"вежливость-заготовка: {hit}")
    if NOT_JUST_RE.search(generated):
        found.append("конструкция «не просто X, а Y»")
    if any(OPENER_RE.match(s) for s in _sentences(generated)):
        found.append("предложение начинается с «Как»")
    if GUILLEMETS_RE.search(generated):
        found.append("кавычки-ёлочки вокруг своих слов")
    if TRIPLE_RE.search(generated):
        found.append("три однородных члена подряд")
    if lang == "en":
        if EM_DASH in generated:
            found.append("длинное тире в английском тексте")
        if not CONTRACTION_RE.search(generated):
            found.append("ни одного сокращения в английском тексте")
    return found


def _without(text: str, *parts) -> str:
    """Письмо без перечисленных слотов — по одному вхождению каждого."""
    for part in parts:
        if part:
            text = text.replace(part, "", 1)
    return text


def _sentences(text: str) -> list[str]:
    return [s for s in (p.strip() for p in SENTENCE_SPLIT_RE.split(text or ""))
            if s]


def _words(text: str) -> list[str]:
    return [w for w in (text or "").split() if any(c.isalnum() for c in w)]
