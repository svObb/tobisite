"""Статика превью: скрипты, бандл и то, что страница у них просит.

Файлов на диске это касается напрямую: имя скрипта в контракте секции и имя
файла в site_factory/js — одно решение, записанное дважды.
"""
import pytest

from site_factory.engine.render import BASE_SCRIPTS, ROOT, load_library

JS_DIR = ROOT / "js"
BUILD = ROOT / "build"

# Кавычки внутри селектора съедает минификатор, поэтому проверяем то, что
# переживает и --minify, и --no-minify.
BUNDLE_MUST_HAVE = (
    ".btn",                        # кнопка компонентом, а не набором утилит
    "--spacing-gutter",            # боковой отступ full-bleed секций
    "data-motion",                 # появление секций включает скрипт
    "is-visible",
    ".scrim",                      # затемнение под текстом на фоновом фото
    "html.lenis",                  # базовые правила плавного скролла
    "nth-of-type",                 # чередование полос paper/surface
    "data-tone",                   # контрастная секция страницы
    "--contrast-bg",
    ".panel",                      # подложка, считающая полосу своей секции
    ".eyebrow",                    # надстрочная подпись одного кегля
    ".nav-collapse",               # меню узкого экрана без единой строки JS
    ".header-overlay",             # шапка на первом экране, а не над ним
    "data-header-stuck",           # она же сплошной полосой после первого экрана
    "--z-header",                  # лестница слоёв: шапка, карточка, кадр
    "--z-raise",
    "--z-under",
    "--text-statement",            # кегль фразы поверх кадра во всю ширину
)


def scripts_asked_by_sections():
    for contract in load_library().values():
        flag = contract.get("js")
        for name in ([flag] if isinstance(flag, str) else flag or []):
            yield contract["id"], name


def test_base_scripts_are_files_on_disk():
    for name in BASE_SCRIPTS:
        assert (JS_DIR / f"{name}.js").is_file(), name


def test_every_script_a_section_asks_for_exists():
    asked = list(scripts_asked_by_sections())
    assert asked, "ни одна секция не просит скрипт — проверять нечего"
    for variant, name in asked:
        assert (JS_DIR / f"{name}.js").is_file(), f"{variant}: нет {name}.js"


def test_vendor_lenis_says_where_it_came_from():
    """Вендорный файл без версии в шапке нечем обновлять и нечем проверить."""
    head = (JS_DIR / "lenis.js").read_text(encoding="utf-8")[:1200]
    assert "lenis" in head.lower()
    assert "MIT" in head
    assert "unpkg.com/lenis@" in head


def test_our_scripts_are_plain_files_without_imports():
    """Превью тянет скрипты как есть: ни сборки, ни модулей, ни внешних адресов."""
    for name in ("preview", "parallax"):
        source = (JS_DIR / f"{name}.js").read_text(encoding="utf-8")
        assert "import " not in source
        assert "http://" not in source and "https://" not in source


def test_reduced_motion_is_checked_before_anything_moves():
    for name in ("preview", "parallax"):
        source = (JS_DIR / f"{name}.js").read_text(encoding="utf-8")
        assert "prefers-reduced-motion: reduce" in source, name


def test_build_holds_a_copy_of_every_script():
    """build/ отдаётся воркером по /assets — там обязаны лежать все скрипты."""
    for path in sorted(JS_DIR.glob("*.js")):
        copied = BUILD / path.name
        if not copied.exists():
            pytest.skip(f"нет build/{path.name}: python tools/build_css.py")
        assert copied.read_bytes() == path.read_bytes(), path.name


def test_bundle_carries_the_component_layer():
    bundle = (BUILD / "bundle.css").read_text(encoding="utf-8")
    for needle in BUNDLE_MUST_HAVE:
        assert needle in bundle, needle
