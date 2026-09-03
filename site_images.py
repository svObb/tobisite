"""Картинки с сайта лида: отбор, пережатие в webp, роли, санитайзинг SVG.

Модуль чистый: на вход байты, на выход байты. Ни сети, ни диска, ни базы —
файловая система бота read-only, а /tmp это tmpfs, которая умирает вместе с
контейнером. Кто и куда кладёт результат, решает enrich_service.

Имена ролей — контракт со движком (сшивка дорожек): `logo`, `hero_bg`,
`portrait`, `photo-2`…`photo-N`. Именованные роли гейты секций спрашивают
напрямую; `photo-N` галерея берёт пулом свободных (не занятых товарами и
именованными ролями) снимков. Перескрейп кладётся поверх прежних файлов,
потому что имена фиксированные.
"""
import io
import logging
import re

from PIL import Image, ImageOps, UnidentifiedImageError
from lxml import etree

log = logging.getLogger(__name__)

# Файлов на лида: логотип плюс до семи картинок. Потолок стоит здесь, а не в
# движке: платит за лишние килобайты LCP превью, а не композиция.
MAX_STAGED = 8

# Длинная сторона после пережатия и качество webp по ролям. Логотип мелкий, но
# качество ему нужно выше: артефакты на буквах видно сразу.
ROLE_MAX_SIDE = {"logo": 512, "background": 2000, "photo": 1200}
ROLE_QUALITY = {"logo": 95, "background": 85, "photo": 85}

MIN_PHOTO_SIDE = 200
MIN_LOGO_SIDE = 64
# Фото уже 1:4 и шире 4:1 — это баннер, полоска или обрезок карусели, а не
# фотография компании. К логотипу этот порог не применяется: вытянутый
# леттеринг на две строки текста — нормальный логотип, а не брак.
PHOTO_ASPECT = (0.25, 4.0)
# Ландшафт для фона секции: уже — и фон придётся резать по вертикали.
BACKGROUND_ASPECT = 1.5
BACKGROUND_MIN_WIDTH = 900

# Логотип в SVG: всё, что тяжелее, — это растр, зашитый в base64, и место ему
# в обычном конвейере, а не в разметке страницы.
MAX_SVG_BYTES = 200_000
# Элементы, которых в логотипе быть не может; встретили — вырезали.
SVG_DROP_TAGS = {
    "script", "foreignobject", "iframe", "embed", "object", "audio", "video",
    "handler", "set", "animate", "animatemotion", "animatetransform",
}
_SVG_DANGER = re.compile(r"javascript:|<script|\son[a-z]+\s*=", re.I)
_CSS_DANGER = re.compile(r"url\(|@import|expression\(|javascript:", re.I)

_ROLE_OF_NAME = {"logo": "logo", "hero_bg": "background"}
_PHOTO_NUMBERED = "photo-{n}"


def role_of(name: str) -> str:
    """Роль по имени файла: от неё зависят размер и качество пережатия."""
    return _ROLE_OF_NAME.get(name, "photo")


def probe_image(data: bytes) -> dict | None:
    """Размеры и пригодность картинки. None — не картинка или не годится.

    Отдельный шаг перед пережатием: роли раздаются по исходным размерам, а
    фон и фото ужимаются по-разному, и решать это после пережатия было бы
    поздно.
    """
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError,
            ValueError) as e:
        log.info("картинка не разобралась: %s", e.__class__.__name__)
        return None
    if not width or not height:
        return None
    return {"width": width, "height": height, "alpha": alpha}


def fits(size: dict, kind: str) -> bool:
    """Годится ли картинка на роль такого вида: logo или photo."""
    width, height = size["width"], size["height"]
    if kind == "logo":
        return width >= MIN_LOGO_SIDE and height >= MIN_LOGO_SIDE
    if width < MIN_PHOTO_SIDE or height < MIN_PHOTO_SIDE:
        return False
    return PHOTO_ASPECT[0] <= width / height <= PHOTO_ASPECT[1]


def process_image(data: bytes, role: str = "photo") -> dict | None:
    """Байты картинки → webp по правилам роли. None — картинка не годится.

    Прозрачность сохраняется: логотип на цветной шапке без альфа-канала
    получил бы белую подложку.
    """
    size = probe_image(data)
    if size is None:
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            # EXIF-поворот: снятое телефоном фото иначе ляжет боком
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGBA" if size["alpha"] else "RGB")
            side = ROLE_MAX_SIDE.get(role, ROLE_MAX_SIDE["photo"])
            if max(img.size) > side:
                img.thumbnail((side, side), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, "WEBP", quality=ROLE_QUALITY.get(role, 85), method=6)
            width, height = img.size
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError,
            ValueError) as e:
        log.info("картинка не пережалась: %s", e.__class__.__name__)
        return None
    return {"data": out.getvalue(), "width": width, "height": height,
            "content_type": "image/webp"}


def dominant_colors(data: bytes, limit: int = 2) -> list[str]:
    """Цвета логотипа, самый частый первым. Белое, чёрное и серое не в счёт.

    Считается по загрублённой до 32 уровней на канал сетке: точный оттенок
    пикселя нам не нужен, нужен цвет бренда, а он на логотипе занимает пятно.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img).convert("RGBA")
            img.thumbnail((64, 64), Image.Resampling.LANCZOS)
            pixels = img.tobytes()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError,
            ValueError):
        return []
    buckets: dict[tuple, int] = {}
    for start in range(0, len(pixels) - 3, 4):
        r, g, b, a = pixels[start:start + 4]
        if a < 128 or _neutral(r, g, b):
            continue
        key = (r // 8, g // 8, b // 8)
        buckets[key] = buckets.get(key, 0) + 1
    best = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return ["#%02x%02x%02x" % (r * 8, g * 8, b * 8) for (r, g, b), _ in best]


def sanitize_svg(markup: str | bytes) -> str | None:
    """Логотип в SVG без единого способа что-нибудь выполнить. None — отказ.

    CSP превью запрещает инлайновые скрипты, но это вторая линия, а не
    оправдание: разметка чужого сайта попадает на страницу, которую клиент
    откроет со своего телефона. Сомнение решается отказом — админ возьмёт
    логотип руками.
    """
    raw = markup.encode() if isinstance(markup, str) else markup
    if not raw or len(raw) > MAX_SVG_BYTES:
        return None
    parser = etree.XMLParser(resolve_entities=False, no_network=True,
                             huge_tree=False, recover=False)
    try:
        root = etree.fromstring(raw, parser=parser)
    except etree.XMLSyntaxError:
        return None
    if etree.QName(root).localname.lower() != "svg":
        return None
    for node in list(root.iter()):
        if not isinstance(node.tag, str):
            continue  # комментарий или processing instruction
        name = etree.QName(node).localname.lower()
        if name in SVG_DROP_TAGS:
            _drop(node)
            continue
        if name == "style" and _CSS_DANGER.search(node.text or ""):
            _drop(node)
            continue
        _clean_attrs(node)
    text = etree.tostring(root, encoding="unicode")
    if _SVG_DANGER.search(text):
        # что-то уцелело после чистки — значит чистка неполная, и логотип
        # надёжнее не брать вовсе
        return None
    return text


def svg_size(markup: str | bytes) -> dict | None:
    """Размеры SVG-логотипа: {width, height}. None — в разметке их нет.

    Ставит их сама разметка: сначала width/height корня, а если их нет —
    третье и четвёртое числа viewBox. Проценты, `em` и прочие относительные
    единицы размером не считаются: держать место под логотип ими нельзя, а
    движок без числа всё равно выбросит запись (draft_service._clean_image).

    Имена атрибутов сверяются в нижнем регистре: разметку сюда приносит
    site_scrape, а его HTML-парсер приводит `viewBox` к `viewbox`.

    Размеров нет — None, и логотип не берётся вовсе: у sanitize_svg то же
    правило, сомнение решается отказом.
    """
    raw = markup.encode() if isinstance(markup, str) else markup
    parser = etree.XMLParser(resolve_entities=False, no_network=True,
                             huge_tree=False, recover=False)
    try:
        root = etree.fromstring(raw, parser=parser)
    except (etree.XMLSyntaxError, ValueError):
        return None
    attrs = {etree.QName(name).localname.lower(): value
             for name, value in root.attrib.items()}
    width = _svg_length(attrs.get("width"))
    height = _svg_length(attrs.get("height"))
    if width and height:
        return {"width": width, "height": height}
    box = re.split(r"[,\s]+", (attrs.get("viewbox") or "").strip())
    if len(box) != 4:
        return None
    width, height = _svg_length(box[2]), _svg_length(box[3])
    return {"width": width, "height": height} if width and height else None


def _svg_length(value) -> int:
    """Длина из атрибута SVG в целые пиксели. 0 — числа там нет."""
    text = str(value or "").strip().removesuffix("px").strip()
    try:
        number = float(text)
    except ValueError:
        return 0
    return max(1, round(number)) if number > 0 else 0


def assign_roles(candidates: list[dict]) -> dict[str, dict]:
    """Имена файлов для отобранных картинок — контракт со движком.

    candidates: {"kind": "logo"|"photo", "og": bool, "product": bool,
    "width", "height", …}; порядок внутри вида — как его отдал скрейп.

    Логотип — один. Фон отдаём только когда фотографий больше одной: на
    единственной фотографии полезнее портрет, он подходит большему числу
    секций, а фон без второго фото оставил бы страницу без иллюстрации.

    Снимок товара на фон и на портрет не берётся вовсе: крупный план коробки
    в шапке — это витрина, а не компания. Магазин, у которого нетоварных фото
    нет, остаётся без шапки-картинки и без портрета — товары уедут в товарные
    секции под именами photo-N, а шапку движок соберёт без фотографии.
    """
    logos = [c for c in candidates if c.get("kind") == "logo"]
    photos = sorted((c for c in candidates if c.get("kind") != "logo"),
                    key=_photo_rank)
    scene = [c for c in photos if not c.get("product")]
    roles: dict[str, dict] = {}
    if logos:
        roles["logo"] = logos[0]
    if len(photos) > 1:
        wide = next((c for c in scene if _is_background(c)), None)
        if wide is not None:
            roles["hero_bg"] = wide
            photos.remove(wide)
            scene.remove(wide)
    if scene:
        best = min(scene, key=lambda c: (not c.get("og"), _photo_rank(c)))
        roles["portrait"] = best
        photos.remove(best)
    for number, item in enumerate(photos, start=2):
        if len(roles) >= MAX_STAGED:
            break
        roles[_PHOTO_NUMBERED.format(n=number)] = item
    return roles


def photo_names(roles) -> list[str]:
    """Имена контентных фото: всё, кроме логотипа. Их и считает photo_count."""
    return sorted(name for name in roles if name != "logo")


# --- внутреннее ---------------------------------------------------------------

def _photo_rank(item: dict) -> tuple:
    """Чем крупнее, тем раньше; og:image идёт вперёд при равной площади."""
    area = item.get("width", 0) * item.get("height", 0)
    return (-area, not item.get("og"), item.get("url", ""))


def _is_background(item: dict) -> bool:
    width, height = item.get("width", 0), item.get("height", 0)
    if not height or width < BACKGROUND_MIN_WIDTH:
        return False
    return width / height >= BACKGROUND_ASPECT


def _neutral(r: int, g: int, b: int) -> bool:
    return max(r, g, b) - min(r, g, b) < 24


def _drop(node):
    parent = node.getparent()
    if parent is not None:
        parent.remove(node)


def _clean_attrs(node):
    for name, value in list(node.attrib.items()):
        local = etree.QName(name).localname.lower() if "}" in name else name.lower()
        if local.startswith("on"):
            del node.attrib[name]
        elif local in ("href", "src"):
            # ссылка наружу тянула бы чужой ресурс с превью клиента
            if not str(value).lstrip().startswith("#"):
                del node.attrib[name]
        elif local == "style" and _CSS_DANGER.search(str(value)):
            del node.attrib[name]
        elif "javascript:" in str(value).lower():
            del node.attrib[name]
