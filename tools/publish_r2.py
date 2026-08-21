"""Публикация превью в R2: папка с файлами -> префикс slug/ в бакете.

Деплоя у превью нет вообще: Worker (worker/src/index.ts) читает файлы прямо
из бакета, так что публикация — это несколько PUT, ~8 на превью.

    python tools/publish_r2.py out/lead-417 --name "Юридична фірма Право і Діло"
    python tools/publish_r2.py out/lead-417 --slug pravo-i-dilo --dry-run

Переменные окружения: R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
R2_BUCKET (по умолчанию tobisite-previews). Ключ создаётся в дашборде
R2 -> Manage API tokens, права Object Read & Write.
"""
import argparse
import os
import pathlib
import sys

from slugify_preview import SLUG_HOST_SUFFIX, unique_slug

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    raise SystemExit(
        "Нужен boto3: pip install boto3 "
        "(и добавить его в requirements.txt И requirements.lock)"
    )

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def env(name, default=None):
    value = os.environ.get(name) or default
    if not value:
        raise SystemExit(f"Не задана переменная окружения {name}")
    return value


def client():
    account = env("R2_ACCOUNT_ID")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
    )


def taken(s3, bucket):
    """Слаг занят, если под ним уже лежит index.html."""
    def check(slug):
        try:
            s3.head_object(Bucket=bucket, Key=f"{slug}/index.html")
            return True
        except ClientError as err:
            if err.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
    return check


def files(folder):
    return sorted(path for path in folder.rglob("*") if path.is_file())


def publish(s3, bucket, folder, slug, dry_run):
    for path in files(folder):
        rel = path.relative_to(folder).as_posix()
        ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
        # повторная публикация того же слага должна показывать новый черновик
        # сразу, а общие шрифты и картинки можно держать в кэше
        cache = "no-cache" if path.suffix.lower() == ".html" else "public, max-age=86400"
        print(f"  {slug}/{rel}  ({ctype}, {path.stat().st_size} Б)")
        if not dry_run:
            s3.put_object(
                Bucket=bucket,
                Key=f"{slug}/{rel}",
                Body=path.read_bytes(),
                ContentType=ctype,
                CacheControl=cache,
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=pathlib.Path, help="папка с файлами превью")
    parser.add_argument("--name", help="название компании, из него делается слаг")
    parser.add_argument("--slug", help="готовый слаг вместо --name (дедуп не выполняется)")
    parser.add_argument("--bucket", default=os.environ.get("R2_BUCKET", "tobisite-previews"))
    parser.add_argument("--dry-run", action="store_true", help="показать план и выйти")
    parser.add_argument(
        "--expire-days", type=int, default=30,
        help="напоминание: срок жизни ставится lifecycle-правилом на бакете, "
             "скрипт его не проставляет (см. worker/README.md)",
    )
    args = parser.parse_args()

    if not args.folder.is_dir():
        raise SystemExit(f"Нет такой папки: {args.folder}")
    if not files(args.folder):
        raise SystemExit(f"В папке нет файлов: {args.folder}")
    if bool(args.name) == bool(args.slug):
        raise SystemExit("Нужен ровно один из --name / --slug")

    s3 = client()
    slug = args.slug or unique_slug(args.name, taken(s3, args.bucket))

    print(f"{'Проверка' if args.dry_run else 'Публикация'}: {args.bucket}/{slug}/")
    publish(s3, args.bucket, args.folder, slug, args.dry_run)

    if args.dry_run:
        print("Сухой прогон, в R2 ничего не записано.")
        return
    print(f"\nГотово: https://{slug}{SLUG_HOST_SUFFIX}/")
    print(f"Слаг {slug} — записать в drafts.r2_prefix.")
    print(f"Удаление через {args.expire_days} дней делает lifecycle-правило "
          f"бакета, а не этот скрипт.")


if __name__ == "__main__":
    sys.exit(main())
