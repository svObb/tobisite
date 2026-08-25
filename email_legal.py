"""Юридический низ письма: кто пишет, откуда и как это прекратить (9.8–9.9, 9.30).

Слой 0 Д12 §1 целиком: ни одной строки отсюда модель не пишет и написать не
может. Всё, что здесь есть, — константы и переменные окружения.

Пустая переменная не даёт строки, и это правило, а не оплошность: настоящего
почтового адреса у компании ещё нет (5.43), а вписывать вместо него выдумку в
коммерческое письмо конкретному юрлицу нельзя. Чего в подписи не хватает,
перечисляет missing(): линтер показывает это дежурному, а очередь пишет в
историю лида при одобрении. Заполняется всё одной строкой в .env, когда
основатель решит.

Ссылки отписки в письме 1 нет намеренно: 9.1 запрещает в нём ссылки вообще, а
CAN-SPAM требует не ссылку, а понятный способ отказаться — им и служит «reply
STOP». Ссылка добавляется в письма 2 и 3, и то плейсхолдером: подставлять её
будет Instantly, точное написание тега узнаем при подключении (UNSUBSCRIBE_TAG).
"""
import config

# Пометка рекламного характера (9.9). Юрисдикции, где она обязательна:
# США — CAN-SPAM Act §7704(a)(5): коммерческое письмо обязано быть опознаваемо
# как реклама; Украина — Закон «Про рекламу» ст. 9: реклама має бути чітко
# відокремлена від іншої інформації та позначена як реклама.
# Страны, которых здесь нет, строки не получают: юридический текст без
# основания — такая же выдумка, как выдуманная цифра.
AD_DISCLOSURE_ISO = ("US", "UA")
AD_DISCLOSURE = {
    "uk": "Це рекламний лист.",
    "en": "This email is an advertisement.",
}

# Отказ от переписки (9.30). Слово STOP одно на оба языка: по нему отписка
# ищется в ответах машинально, а «ні»/«no» остаются в CTA и в письме 3 как
# живой ответ человека. Вопросительного знака здесь нет и быть не может:
# вопрос в письме ровно один, и это CTA (Д12 §5).
OPT_OUT = {
    "uk": "Не цікаво — відповідайте «STOP», більше не напишу.",
    "en": "If this is not relevant, reply STOP and I won't write again.",
}
UNSUBSCRIBE = {
    "uk": "Відписатись: {tag}",
    "en": "Unsubscribe: {tag}",
}


def sender() -> str:
    """Кто пишет: имя и компания (9.8, идентификация отправителя)."""
    return ", ".join(p for p in (config.SIGNATURE_NAME,
                                 config.SIGNATURE_COMPANY) if p)


def ad_line(lead, lang: str) -> str:
    """Пометка рекламы, если её требует страна лида (9.9). Иначе пусто."""
    if iso_of(lead) not in AD_DISCLOSURE_ISO:
        return ""
    return AD_DISCLOSURE.get(lang, "")


def unsubscribe_line(lang: str) -> str:
    """Строка со ссылкой отписки. Пусто — тег ещё не задан (Instantly)."""
    if not config.UNSUBSCRIBE_TAG:
        return ""
    return UNSUBSCRIBE[lang].format(tag=config.UNSUBSCRIBE_TAG)


def footer(lead, lang: str, *, with_link: bool = False) -> str:
    """Подпись письма целиком. with_link — письма 2 и 3, где ссылки разрешены."""
    lines = [sender(), config.POSTAL_ADDRESS, ad_line(lead, lang),
             OPT_OUT[lang]]
    if with_link:
        lines.append(unsubscribe_line(lang))
    return "\n".join(p for p in lines if p)


def missing(lead, lang: str) -> list[str]:
    """Чего не хватает, чтобы письмо можно было отправлять наружу.

    Список непустой — письмо остаётся внутренним артефактом: одобрять его
    можно (отправки в конвейере всё равно нет), отправлять нельзя.
    """
    gaps = []
    if not config.SIGNATURE_NAME and not config.SIGNATURE_COMPANY:
        gaps.append("не задан отправитель (SIGNATURE_NAME/SIGNATURE_COMPANY)")
    if not config.POSTAL_ADDRESS:
        gaps.append("не задан физический адрес отправителя (POSTAL_ADDRESS)")
    if not config.UNSUBSCRIBE_TAG:
        gaps.append("не задана ссылка отписки (UNSUBSCRIBE_TAG)")
    if iso_of(lead) in AD_DISCLOSURE_ISO and not AD_DISCLOSURE.get(lang):
        gaps.append(f"нет пометки рекламы на языке «{lang}»")
    return gaps


def iso_of(lead) -> str:
    """Код страны лида. В карточке страна записана словом, ISO даёт config."""
    return config.COUNTRY_ISO.get(lead.country) or (lead.country or "")
