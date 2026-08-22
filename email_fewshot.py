"""Банк few-shot пар для слотов bridge и offer (Д12 §4).

Девять примеров на язык вывода: столько нужно, чтобы модель не сочиняла форму
письма с нуля, и столько же вместе с системным промптом набирает кэшируемый
префикс (минимум Sonnet — 1024 токена).

Первые три украинских — дословно из Д12 §4, остальные написаны 22.08 по тем же
правилам. Пары пишутся руками и только руками: это единственный образец тона,
который модель видит, и выдуманный пример здесь стоит дороже, чем отсутствующий.
Вычитка основателем — отдельным шагом до первой боевой отправки.
"""
# input — карточка примера в том же виде, в каком её видит модель в
# пользовательском промпте; value=None у типов, где артефакт не текст (скриншот).
FEWSHOT = {
    "uk": [
        {
            "input": {"gap": "slow", "value": "8", "niche": "стоматологія",
                      "draft": "одна сторінка, кнопка запису вгорі, "
                               "вантажиться за секунду"},
            "output": {
                "bridge": "На такому екрані людині простіше повернутись "
                          "у пошук, ніж дочекатись.",
                "offer": "Я зібрав чернетку вашої головної на ваших реальних "
                         "даних, вона відкривається за секунду.",
            },
        },
        {
            "input": {"gap": "no_booking", "value": "тільки телефон",
                      "niche": "салон краси",
                      "draft": "форма запису з вибором дати і майстра"},
            "output": {
                "bridge": "Половина людей обирає час увечері, коли "
                          "адміністратор уже не бере слухавку.",
                "offer": "У чернетці я поставив форму запису з вибором дати "
                         "і майстра, вона працює цілодобово.",
            },
        },
        {
            "input": {"gap": "stale", "value": "Акція діє до 31.12.2021",
                      "niche": "ресторан",
                      "draft": "актуальне меню з цінами, фото залу"},
            "output": {
                "bridge": "Людина бачить позаминулий рік і закриває вкладку, "
                          "не дочитавши до меню.",
                "offer": "Я переніс у чернетку ваше актуальне меню з цінами "
                         "і фото залу з Google Maps.",
            },
        },
        {
            "input": {"gap": "no_mobile", "value": None, "niche": "адвокат",
                      "draft": "одна сторінка, великий шрифт, кнопка дзвінка "
                               "внизу екрана"},
            "output": {
                "bridge": "З телефону таку сторінку закривають раніше, "
                          "ніж знаходять номер.",
                "offer": "Я зібрав чернетку, де текст читається з екрана "
                         "телефону, а кнопка дзвінка завжди під пальцем.",
            },
        },
        {
            "input": {"gap": "no_prices", "value": "імплантація",
                      "niche": "стоматологія",
                      "draft": "таблиця цін на головні послуги, форма запису"},
            "output": {
                "bridge": "Людина, яка не бачить цін, пише тим, у кого вони є.",
                "offer": "У чернетці я поставив таблицю цін на головні послуги "
                         "поруч із формою запису.",
            },
        },
        {
            "input": {"gap": "no_site", "value": "сторінку у Facebook",
                      "niche": "салон краси",
                      "draft": "головна з послугами, цінами і формою запису"},
            "output": {
                "bridge": "Пошук показує конкурентів із сайтами вище, ніж "
                          "сторінку у Facebook.",
                "offer": "Я зібрав чернетку головної з вашими послугами і "
                         "формою запису на ваших даних із Google Maps.",
            },
        },
        {
            "input": {"gap": "no_https", "value": "З'єднання не захищене",
                      "niche": "бухгалтерські послуги",
                      "draft": "захищене з'єднання, контакти вгорі"},
            "output": {
                "bridge": "Після такого попередження людина не залишає "
                          "в формі свій номер.",
                "offer": "Чернетку я зібрав на захищеному з'єднанні, "
                         "попередження браузера там немає.",
            },
        },
        {
            "input": {"gap": "contact_mismatch",
                      "value": "+380501112233, +380671114455",
                      "niche": "автосервіс",
                      "draft": "контакти з Google Maps, однакові на всіх "
                               "сторінках"},
            "output": {
                "bridge": "Людина не знає, який із двох номерів набирати.",
                "offer": "У чернетці я поставив контакти з Google Maps, "
                         "однакові на кожній сторінці.",
            },
        },
        {
            "input": {"gap": "form_broken", "value": "перезавантажилась",
                      "niche": "клініка",
                      "draft": "робоча форма запису, підтвердження після "
                               "відправки"},
            "output": {
                "bridge": "Людина, чия заявка зникла, вдруге форму "
                          "не заповнює.",
                "offer": "Я поставив у чернетку робочу форму, яка показує "
                         "підтвердження одразу після відправки.",
            },
        },
    ],
    "en": [
        {
            "input": {"gap": "slow", "value": "9", "niche": "dental clinic",
                      "draft": "single page, booking button on top, loads in "
                               "under a second"},
            "output": {
                "bridge": "Most people go back to the search results before "
                          "a page like that loads.",
                "offer": "I've built a draft of your homepage on your real "
                         "data, and it loads in under a second.",
            },
        },
        {
            "input": {"gap": "no_mobile", "value": None, "niche": "law firm",
                      "draft": "single page, large type, tap-to-call button"},
            "output": {
                "bridge": "On a phone that means pinching and scrolling "
                          "sideways just to read a paragraph.",
                "offer": "I've put together a draft that reads cleanly on "
                         "a phone, with a tap-to-call button on top.",
            },
        },
        {
            "input": {"gap": "no_booking", "value": "phone only",
                      "niche": "beauty salon",
                      "draft": "booking form with date and staff choice"},
            "output": {
                "bridge": "Plenty of people pick their time at night, after "
                          "the front desk stops answering.",
                "offer": "In the draft I've added a booking form with date "
                         "and staff choice that works around the clock.",
            },
        },
        {
            "input": {"gap": "form_broken", "value": "page reloaded",
                      "niche": "clinic",
                      "draft": "working contact form with a confirmation "
                               "message"},
            "output": {
                "bridge": "Someone whose message disappears once doesn't "
                          "usually try a second time.",
                "offer": "I've set up a working form in the draft that shows "
                         "a confirmation right after you hit send.",
            },
        },
        {
            "input": {"gap": "no_prices", "value": "teeth whitening",
                      "niche": "dental clinic",
                      "draft": "price table for main services next to the "
                               "booking form"},
            "output": {
                "bridge": "People who can't find a price usually message the "
                          "clinic that lists one.",
                "offer": "I've put a price table for your main services right "
                         "next to the booking form in the draft.",
            },
        },
        {
            "input": {"gap": "stale", "value": "Holiday hours 2023",
                      "niche": "restaurant",
                      "draft": "current menu with prices, photos from "
                               "Google Maps"},
            "output": {
                "bridge": "A visitor who sees a two-year-old notice assumes "
                          "the menu is old too.",
                "offer": "I've moved your current menu and your Google Maps "
                         "photos into the draft homepage.",
            },
        },
        {
            "input": {"gap": "no_site", "value": "a Yelp profile",
                      "niche": "roofing company",
                      "draft": "one-page site with services, service area "
                               "and a quote form"},
            "output": {
                "bridge": "Search sends those clicks to the roofers who have "
                          "their own page.",
                "offer": "I've built a one-page draft with your services, "
                         "service area and a quote form on your real data.",
            },
        },
        {
            "input": {"gap": "no_https",
                      "value": "Your connection is not private",
                      "niche": "accounting firm",
                      "draft": "secure connection, contacts on top"},
            "output": {
                "bridge": "Hardly anyone types their number into a form "
                          "behind that warning.",
                "offer": "I've set the draft up on a secure connection, so "
                         "that warning never comes up.",
            },
        },
        {
            "input": {"gap": "contact_mismatch",
                      "value": "(512) 555-0143, (512) 555-0198",
                      "niche": "auto repair shop",
                      "draft": "contacts from Google Maps, identical on "
                               "every page"},
            "output": {
                "bridge": "A caller who can't tell which number is right "
                          "often doesn't dial either.",
                "offer": "In the draft I've used your Google Maps contacts, "
                         "identical on every page.",
            },
        },
    ],
}
