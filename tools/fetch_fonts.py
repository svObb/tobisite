"""Самохостинг шрифтов site_factory: WOFF2-сабсеты + fonts.css с @font-face.

Подключать fonts.googleapis.com нельзя (13-шаблоны-сайтов.md §4: Landgericht
München I, 3 O 17493/20 — динамическая загрузка Google Fonts отдаёт IP
посетителя без согласия, а превью продаются в юрисдикции GDPR). Поэтому
начертания качаются один раз и лежат рядом с бандлом.

Семейства, сабсеты и начертания берутся из site_factory/tokens/presets.yaml —
списка шрифтов в двух местах быть не должно.

Запускать из корня проекта, до tools/build_css.py:

    python tools/fetch_fonts.py
    python tools/fetch_fonts.py --force   # перекачать поверх существующих
"""
import argparse
import json
import pathlib
import sys
import urllib.request

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PRESETS = ROOT / "site_factory" / "tokens" / "presets.yaml"
OUT_DIR = ROOT / "site_factory" / "build" / "fonts"

API = "https://gwfh.mranftl.com/api/fonts/{id}?subsets={subsets}"

# Префикс, под которым Worker отдаёт общие ассеты превью (§4). Должен совпадать
# с site.assets_base в контексте рендера, иначе @font-face укажет в пустоту.
ASSETS_BASE = "/assets"

UA = "tobisite-site-factory/1.0"


def fetch(url, binary=False):
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read()
    return data if binary else json.loads(data)


def variant_url(meta, weight):
    """Ссылка на woff2 нужного начертания в ответе google-webfonts-helper."""
    for variant in meta["variants"]:
        if variant["fontStyle"] == "normal" and str(variant["fontWeight"]) == str(weight):
            return variant["woff2"]
    raise SystemExit(f"{meta['id']}: нет начертания {weight} normal")


def face(family, stack, weight, filename):
    fallback = stack.split(",", 1)[1].strip() if "," in stack else ""
    return "\n".join([
        f"/* {family} {weight} — {fallback or 'без запасных'} */",
        "@font-face {",
        f'  font-family: "{family}";',
        "  font-style: normal;",
        f"  font-weight: {weight};",
        "  font-display: swap;",
        f'  src: url("{ASSETS_BASE}/fonts/{filename}") format("woff2");',
        "}",
    ])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="перекачать уже скачанное")
    args = parser.parse_args()

    tokens = yaml.safe_load(PRESETS.read_text(encoding="utf-8"))
    subsets = ",".join(tokens["subsets"])
    weights = tokens["weights"]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    faces = []

    for family, spec in tokens["fonts"].items():
        meta = fetch(API.format(id=spec["gwfh_id"], subsets=subsets))
        for weight in weights:
            filename = f"{spec['gwfh_id']}-{weight}.woff2"
            path = OUT_DIR / filename
            if args.force or not path.exists():
                path.write_bytes(fetch(variant_url(meta, weight), binary=True))
                print(f"{filename}: {path.stat().st_size // 1024} КБ")
            else:
                print(f"{filename}: уже есть")
            faces.append(face(family, spec["stack"], weight, filename))

    header = (
        "/* Сгенерировано tools/fetch_fonts.py. Руками не править.\n"
        f"   Сабсеты: {subsets}. Источник: google-webfonts-helper. */\n\n"
    )
    (OUT_DIR / "fonts.css").write_text(header + "\n\n".join(faces) + "\n", encoding="utf-8")
    print(f"fonts.css: {len(faces)} @font-face")


if __name__ == "__main__":
    sys.exit(main())
