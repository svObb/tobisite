"""Сборка общей статики site_factory: бандл Tailwind v4 плюс скрипты превью.

Standalone-бинарник выбран, чтобы в проекте не появился Node (13-шаблоны §2):
бандл собирается здесь один раз на всю библиотеку, а генерация черновика
остаётся чистым рендером строки в Python без единого подпроцесса.

Бинарник в git не коммитится — качается сюда при первом запуске.

Запускать из корня проекта, после tools/fetch_fonts.py:

    python tools/build_css.py
    python tools/build_css.py --no-minify   # читаемый бандл для отладки
"""
import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "site_factory" / "css" / "source.css"
FONTS = ROOT / "site_factory" / "build" / "fonts" / "fonts.css"
OUT = ROOT / "site_factory" / "build" / "bundle.css"
CLI = ROOT / "tools" / "tailwindcss.exe"

# Скрипты превью лежат рядом с исходником CSS и уезжают в build/ как есть:
# минифицировать нечего (lenis.js уже минифицирован, свои файлы — полсотни
# строк), а воркер отдаёт всю папку build/ по префиксу /assets.
SCRIPTS_DIR = ROOT / "site_factory" / "js"

RELEASE_API = "https://api.github.com/repos/tailwindlabs/tailwindcss/releases/latest"
ASSET = "tailwindcss-windows-x64.exe"

UA = "tobisite-site-factory/1.0"


def download_cli():
    """Последний релиз tailwindcss, ассет под Windows x64."""
    request = urllib.request.Request(RELEASE_API, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=60) as response:
        release = json.load(response)

    if not release["tag_name"].startswith("v4."):
        raise SystemExit(f"последний релиз {release['tag_name']}, а нужен v4 — обновите скрипт")

    url = next((a["browser_download_url"] for a in release["assets"] if a["name"] == ASSET), None)
    if url is None:
        raise SystemExit(f"в релизе {release['tag_name']} нет ассета {ASSET}")

    print(f"качаю tailwindcss {release['tag_name']}…")
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=600) as response:
        CLI.write_bytes(response.read())
    print(f"{CLI.name}: {CLI.stat().st_size // 1024 // 1024} МБ")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-minify", action="store_true", help="не минифицировать")
    args = parser.parse_args()

    if not FONTS.exists():
        raise SystemExit(f"нет {FONTS.relative_to(ROOT)} — сначала python tools/fetch_fonts.py")

    if not CLI.exists():
        download_cli()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    command = [str(CLI), "--input", str(SOURCE), "--output", str(OUT)]
    if not args.no_minify:
        command.append("--minify")

    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    sys.stderr.write(result.stderr)
    if result.returncode:
        return result.returncode

    print(f"bundle.css: {OUT.stat().st_size / 1024:.1f} КБ")
    for script in copy_scripts():
        print(f"{script.name}: {script.stat().st_size / 1024:.1f} КБ")
    return 0


def copy_scripts() -> list[pathlib.Path]:
    """site_factory/js/*.js -> site_factory/build/. Отдаёт скопированное."""
    copied = []
    for source in sorted(SCRIPTS_DIR.glob("*.js")):
        target = OUT.parent / source.name
        shutil.copyfile(source, target)
        copied.append(target)
    return copied


if __name__ == "__main__":
    sys.exit(main())
