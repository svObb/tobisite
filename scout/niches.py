"""Ниша бота → OSM-теги для Overpass (пункт 15.3).

Ключи совпадают с config.NICHES: /scout принимает ту же нишу, что и форма
добавления. Один тег — пара (ключ, значение); у ниши их может быть несколько,
запрос объединяет их через union.
"""

NICHE_TAGS: dict[str, list[tuple[str, str]]] = {
    "Стоматология": [("amenity", "dentist")],
    "Автосервис": [("shop", "car_repair")],
    "Кафе/ресторан": [("amenity", "cafe"), ("amenity", "restaurant")],
    "Юрист": [("office", "lawyer")],
    "Салон красоты": [("shop", "beauty"), ("shop", "hairdresser")],
    "Гостиница": [("tourism", "hotel"), ("tourism", "guest_house")],
    "Строительство": [("office", "construction_company"), ("craft", "builder")],
}
