#!/usr/bin/env python3
"""Detecta noticias, genera placas 4:5 y publica en Instagram.

El comando scan genera las placas y una cola. El comando publish usa las
imágenes ya subidas al repositorio, publica por la API de Meta y actualiza el
estado local. Las credenciales solo se leen desde variables de entorno.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont, ImageOps


ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "state.json"
QUEUE_PATH = ROOT / "queue.json"
CARDS_DIR = ROOT / "public" / "cards"
LOGO_PATH = ROOT / "assets" / "logo.png"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/126 Safari/537.36 FMVidaAutomation/1.0"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "es-AR,es;q=0.9"})


@dataclass
class Article:
    article_id: str
    url: str
    title: str
    summary: str
    category: str
    image_url: str
    card_path: str = ""


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def fetch(url: str, timeout: int = 45) -> requests.Response:
    last_error = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, timeout=timeout)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                wait_seconds = 10 * (attempt + 1)
                print(f"Servidor demorado. Nuevo intento en {wait_seconds} segundos...")
                time.sleep(wait_seconds)
    raise RuntimeError(f"No se pudo acceder a {url} después de 3 intentos: {last_error}")


def clean_text(value: str) -> str:
    value = html.unescape(str(value or ""))
    if "<" in value and ">" in value:
        value = BeautifulSoup(value, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", value).strip()


def response_html(response: requests.Response) -> str:
    try:
        return response.content.decode("utf-8")
    except UnicodeDecodeError:
        response.encoding = response.apparent_encoding
        return response.text


def normalize_title(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"^FM\s*Vida(?:\s+Fort[ií]n\s+Olmos)?\s*[»|:\-]+\s*", "", value, flags=re.I)
    value = re.sub(r"\s*[|»]\s*FM\s*Vida(?:\s+Fort[ií]n\s+Olmos)?\s*$", "", value, flags=re.I)
    return value.strip()


def article_id_from_url(url: str) -> str:
    match = re.search(r"noticia_(\d+)", url)
    if match:
        return match.group(1)
    return re.sub(r"[^a-zA-Z0-9]+", "-", urlparse(url).path).strip("-")[-80:]


def discover_articles(site_url: str, limit: int) -> list[tuple[str, str]]:
    soup = BeautifulSoup(response_html(fetch(site_url)), "html.parser")
    found: dict[str, tuple[str, str]] = {}
    for anchor in soup.select('a[href*="noticia_"]'):
        url = urljoin(site_url, anchor.get("href", ""))
        article_id = article_id_from_url(url)
        title = clean_text(anchor.get_text(" "))
        if not article_id or not url or len(title) < 8:
            continue
        previous = found.get(article_id)
        if previous is None or len(title) > len(previous[1]):
            found[article_id] = (url, title)

    def numeric_key(item):
        article_id = item[0]
        return int(article_id) if article_id.isdigit() else 0

    ordered = sorted(found.items(), key=numeric_key, reverse=True)
    return [(url, title) for _, (url, title) in ordered[:limit]]


def meta_content(soup: BeautifulSoup, *selectors: str) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if node and node.get("content"):
            value = clean_text(node["content"])
            if value:
                return value
    return ""


def is_probable_content_image(url: str) -> bool:
    lower = url.lower()
    blocked = ("logo", "banner", "icon", "avatar", "weather", "publicidad", "spinner")
    return url.startswith(("http://", "https://")) and not any(word in lower for word in blocked)


def parse_article(url: str, fallback_title: str, default_category: str) -> Article:
    soup = BeautifulSoup(response_html(fetch(url)), "html.parser")
    title = meta_content(soup, 'meta[property="og:title"]', 'meta[name="twitter:title"]')
    if not title:
        h1 = soup.find("h1")
        title = clean_text(h1.get_text(" ") if h1 else fallback_title)
    title = normalize_title(title)

    summary = meta_content(
        soup,
        'meta[property="og:description"]',
        'meta[name="description"]',
        'meta[name="twitter:description"]',
    )
    if not summary:
        paragraphs = []
        for selector in ("article p", ".noticia p", ".contenido p", ".detalle p", ".post p", "p"):
            for node in soup.select(selector):
                text = clean_text(node.get_text(" "))
                if len(text) >= 45 and text not in paragraphs:
                    paragraphs.append(text)
            if paragraphs:
                break
        summary = paragraphs[0] if paragraphs else "Toda la información en nuestro portal de noticias."

    image_url = meta_content(soup, 'meta[property="og:image"]', 'meta[name="twitter:image"]')
    if image_url:
        image_url = urljoin(url, image_url)
    if not image_url or not is_probable_content_image(image_url):
        candidates = []
        for selector in ("article img", ".noticia img", ".contenido img", ".detalle img", ".post img", "img"):
            for node in soup.select(selector):
                src = node.get("data-src") or node.get("data-lazy-src") or node.get("src") or ""
                src = urljoin(url, src)
                if is_probable_content_image(src):
                    width = int(re.sub(r"\D", "", node.get("width", "0")) or 0)
                    height = int(re.sub(r"\D", "", node.get("height", "0")) or 0)
                    candidates.append((width * height, src))
            if candidates:
                break
        image_url = max(candidates, default=(0, ""))[1]

    category = default_category
    for selector in (".categoria", ".category", ".seccion", ".breadcrumb a"):
        node = soup.select_one(selector)
        value = clean_text(node.get_text(" ") if node else "")
        if 3 <= len(value) <= 24:
            category = value.upper()
            break

    return Article(
        article_id=article_id_from_url(url),
        url=url,
        title=title or fallback_title,
        summary=summary,
        category=category,
        image_url=image_url,
    )


def font_path(bold: bool = False) -> str:
    choices = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]
        if bold
        else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"]
    )
    for candidate in choices:
        if Path(candidate).exists():
            return candidate
    raise FileNotFoundError("No se encontró una tipografía compatible")


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(font_path(bold), size=size)


def download_image(url: str) -> Image.Image | None:
    if not url:
        return None
    try:
        response = fetch(url, timeout=45)
        return Image.open(BytesIO(response.content)).convert("RGB")
    except Exception as exc:
        print(f"Aviso: no se pudo descargar la imagen {url}: {exc}", file=sys.stderr)
        return None


def fallback_background(size: tuple[int, int]) -> Image.Image:
    width, height = size
    image = Image.new("RGB", size, "#26313a")
    draw = ImageDraw.Draw(image)
    for y in range(height):
        mix = y / max(height - 1, 1)
        color = tuple(int(a * (1 - mix) + b * mix) for a, b in zip((38, 49, 58), (122, 105, 86)))
        draw.line((0, y, width, y), fill=color)
    draw.ellipse((70, 105, 305, 340), fill="#928c80")
    draw.polygon([(0, 610), (170, 420), (330, 600), (475, 440), (655, 620), (790, 485), (1080, 675), (1080, 840), (0, 840)], fill="#343c39")
    draw.polygon([(430, 840), (782, 575), (895, 840)], fill="#564a3e")
    return image


def cover_crop(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.48))


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = clean_text(text).split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or text_width(draw, candidate, font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def fitted_lines(draw, text: str, max_width: int, max_lines: int, start: int, minimum: int):
    for size in range(start, minimum - 1, -2):
        font = load_font(size, bold=True)
        lines = wrap_text(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
    font = load_font(minimum, bold=True)
    lines = wrap_text(draw, text, font, max_width)[:max_lines]
    if lines:
        while text_width(draw, lines[-1] + "…", font) > max_width and " " in lines[-1]:
            lines[-1] = lines[-1].rsplit(" ", 1)[0]
        lines[-1] = lines[-1].rstrip(".,;:") + "…"
    return font, lines


def rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def generate_card(article: Article, config: dict, destination: Path) -> None:
    canvas = Image.new("RGB", (1080, 1350), "#fbfaf7")
    source = download_image(article.image_url)
    photo = cover_crop(source, (1080, 840)) if source else fallback_background((1080, 840))
    canvas.paste(photo, (0, 0))

    overlay = Image.new("RGBA", (1080, 840), (0, 0, 0, 0))
    overlay_pixels = overlay.load()
    for y in range(840):
        alpha = int(max(0, min(190, (y - 250) / 590 * 190)))
        for x in range(1080):
            overlay_pixels[x, y] = (0, 0, 0, alpha)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), Image.new("RGBA", canvas.size, (0, 0, 0, 0)))
    canvas.alpha_composite(overlay, (0, 0))
    draw = ImageDraw.Draw(canvas)

    burgundy = "#9b241d"
    rounded_rect(draw, (52, 58, 342, 114), 28, burgundy)
    category_font = load_font(25, bold=True)
    category = clean_text(article.category).upper()[:22]
    draw.text((197, 86), category, font=category_font, fill="white", anchor="mm")
    draw.text((54, 143), config["site_name"], font=load_font(25, bold=True), fill="white")
    draw.text((54, 181), config["location"], font=load_font(19), fill="#ece9e4")

    logo = Image.open(LOGO_PATH).convert("RGBA")
    logo.thumbnail((215, 215), Image.Resampling.LANCZOS)
    lx, ly = 805, 36
    draw.ellipse((800, 31, 1025, 256), fill=(255, 255, 255, 245))
    canvas.alpha_composite(logo, (lx + (215 - logo.width) // 2, ly + (215 - logo.height) // 2))
    # alpha_composite reemplaza el búfer interno; se recrea ImageDraw para que
    # los elementos siguientes no sobrescriban parcialmente el logo o textos.
    draw = ImageDraw.Draw(canvas)

    rounded_rect(draw, (54, 704, 385, 752), 24, (255, 255, 255, 235))
    draw.text((219, 728), "INFORMACIÓN REGIONAL", font=load_font(19, bold=True), fill="#84231e", anchor="mm")

    rounded_rect(draw, (0, 786, 1080, 1375), 20, "#fbfaf7")
    draw.rounded_rectangle((54, 842, 146, 849), radius=4, fill=burgundy)

    title_font, title_lines = fitted_lines(draw, article.title, 972, 3, 58, 42)
    line_height = int(title_font.size * 1.23)
    y = 882
    for line in title_lines:
        draw.text((54, y), line, font=title_font, fill="#1f2529")
        y += line_height

    summary_font = load_font(27)
    summary_lines = wrap_text(draw, article.summary, summary_font, 972)[:2]
    sy = 1150
    for line in summary_lines:
        draw.text((54, sy), line, font=summary_font, fill="#596064")
        sy += 40

    draw.line((54, 1260, 1026, 1260), fill="#d9d2c9", width=2)
    draw.ellipse((60, 1296, 76, 1312), fill=burgundy)
    draw.text((90, 1292), config["site_domain"], font=load_font(24, bold=True), fill="#84231e")
    tagline_font = load_font(18, bold=True)
    draw.text((1026, 1295), config["tagline"], font=tagline_font, fill="#737778", anchor="ra")

    destination.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(destination, "JPEG", quality=92, optimize=True, progressive=True)


def build_caption(article: Article, config: dict) -> str:
    summary = clean_text(article.summary)
    if len(summary) > 420:
        summary = summary[:417].rsplit(" ", 1)[0] + "…"
    hashtags = " ".join(config.get("hashtags", []))
    return (
        f"📰 {article.title}\n\n"
        f"{summary}\n\n"
        f"🌐 Leé la noticia completa:\n{article.url}\n\n"
        f"{hashtags}"
    ).strip()


def scan(test_mode: bool = False) -> int:
    config = read_json(CONFIG_PATH, {})
    state = read_json(STATE_PATH, {"initialized": False, "published": []})
    discovered = discover_articles(config["site_url"], int(config.get("check_limit", 20)))
    if not discovered:
        raise RuntimeError("No se encontraron enlaces de noticias en la portada")

    current_ids = [article_id_from_url(url) for url, _ in discovered]
    if not state.get("initialized") and config.get("first_run_initializes_only", True) and not test_mode:
        state["initialized"] = True
        state["published"] = current_ids
        write_json(STATE_PATH, state)
        write_json(QUEUE_PATH, [])
        print(f"Primera ejecución segura: se registraron {len(current_ids)} noticias existentes sin publicarlas.")
        return 0

    published = set(state.get("published", []))
    if test_mode:
        pending_links = [discovered[0]]
    else:
        pending_links = [(url, title) for url, title in reversed(discovered) if article_id_from_url(url) not in published]
        pending_links = pending_links[: int(config.get("max_posts_per_run", 3))]

    queue = []
    for url, fallback_title in pending_links:
        article = parse_article(url, fallback_title, config.get("default_category", "NOTICIAS"))
        filename = f"noticia-{article.article_id}.jpg"
        destination = CARDS_DIR / filename
        generate_card(article, config, destination)
        article.card_path = f"public/cards/{filename}"
        item = asdict(article)
        item["caption"] = build_caption(article, config)
        queue.append(item)
        print(f"Placa generada: {article.title} -> {article.card_path}")

    write_json(QUEUE_PATH, queue)
    print(f"Noticias preparadas: {len(queue)}")
    return 0


def wait_for_public_image(url: str, attempts: int = 8) -> None:
    for attempt in range(attempts):
        try:
            response = SESSION.get(url, timeout=20)
            content_type = response.headers.get("content-type", "")
            if response.ok and content_type.startswith("image/") and len(response.content) > 10_000:
                return
        except requests.RequestException:
            pass
        time.sleep(8 + attempt * 2)
    raise RuntimeError(f"La imagen todavía no está disponible públicamente: {url}")


def meta_post(path: str, data: dict, api_version: str) -> dict:
    response = SESSION.post(f"https://graph.facebook.com/{api_version}/{path.lstrip('/')}", data=data, timeout=60)
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:500]}
    if not response.ok or "error" in payload:
        raise RuntimeError(f"Error de Meta API ({response.status_code}): {payload}")
    return payload


def publish(dry_run: bool = False) -> int:
    queue = read_json(QUEUE_PATH, [])
    if not queue:
        print("No hay noticias en cola.")
        return 0

    repository = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    ig_user_id = os.environ.get("IG_USER_ID", "")
    access_token = os.environ.get("IG_ACCESS_TOKEN", "")
    api_version = os.environ.get("META_API_VERSION", "v26.0")

    if not dry_run and (not repository or not ig_user_id or not access_token):
        raise RuntimeError("Faltan GITHUB_REPOSITORY, IG_USER_ID o IG_ACCESS_TOKEN")

    state = read_json(STATE_PATH, {"initialized": True, "published": []})
    published = list(state.get("published", []))

    for item in queue:
        article_id = item["article_id"]
        if dry_run:
            print(f"MODO PRUEBA: se publicaría {item['title']}")
            continue

        raw_url = f"https://raw.githubusercontent.com/{repository}/{branch}/{item['card_path']}"
        wait_for_public_image(raw_url)
        container = meta_post(
            f"{ig_user_id}/media",
            {"image_url": raw_url, "caption": item["caption"], "access_token": access_token},
            api_version,
        )
        result = meta_post(
            f"{ig_user_id}/media_publish",
            {"creation_id": container["id"], "access_token": access_token},
            api_version,
        )
        print(f"Publicado en Instagram: {item['title']} (media {result.get('id')})")
        if article_id not in published:
            published.append(article_id)

    if not dry_run:
        state["initialized"] = True
        state["published"] = published[-500:]
        write_json(STATE_PATH, state)
        write_json(QUEUE_PATH, [])
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatización FM Vida → Instagram")
    subparsers = parser.add_subparsers(dest="command", required=True)
    scan_parser = subparsers.add_parser("scan", help="Detectar noticias y generar placas")
    scan_parser.add_argument("--test", action="store_true", help="Generar una placa aun sin noticias nuevas")
    publish_parser = subparsers.add_parser("publish", help="Publicar la cola en Instagram")
    publish_parser.add_argument("--dry-run", action="store_true", help="Mostrar sin publicar")
    args = parser.parse_args()
    if args.command == "scan":
        return scan(test_mode=args.test)
    return publish(dry_run=args.dry_run)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
