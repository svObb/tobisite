"""Фабрика черновиков-превью: токены x секции x рецепты (13-шаблоны-сайтов.md §2).

Пакет живёт рядом с ботом, но ботом не импортируется: у него свой вход —
tools/build_css.py, tools/fetch_fonts.py и engine.render.

Профиль лида в HTML — пять строк:

    from site_factory.engine.profile import Profile
    from site_factory.engine.render import render

    profile = Profile.from_dict({"domain_norm": "buro.example", "lang": "uk",
                                 "niche": "Юрист", "name": "...", "phone": "..."})
    html, recipe_json = render(profile)

Ключа в словаре нет — признак неизвестен (unknown != false). Если данных не
хватило, html будет None, а recipe_json["needs_enrichment"] — списком того,
что просить у работника. Перед публикацией страницу прогоняют через
engine.checks.run_all(html, profile, preset["palette"]).
"""
