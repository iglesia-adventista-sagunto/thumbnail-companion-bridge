import logging
import os
import re
import sys
from datetime import datetime
from functools import lru_cache
from io import BytesIO

import numpy as np
import pandas as pd
import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger("automatic-thumbnail")


def _resolve_path(path, default_relative=None):
    """Resolve a path relative to cwd or BASE_DIR."""
    target = path or default_relative
    if not target:
        return ""
    if os.path.isabs(target) or os.path.exists(target):
        return target
    candidate = os.path.join(BASE_DIR, target)
    if os.path.exists(candidate):
        return candidate
    return target


def _load_env_file(env_path=None):
    """Load environment variables from a .env file if present (without overriding existing env vars)."""
    target = env_path or _resolve_path(".env")
    try:
        from dotenv import load_dotenv
        load_dotenv(target, override=False)
        return
    except ImportError:
        pass

    if not target or not os.path.exists(target):
        return
    with open(target, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_file()

# Default configuration (customizable via environment variables for Proxmox/Linux/NAS)
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", os.path.join(BASE_DIR, "thumbnails"))
ATTRIBUTIONS_FILE = os.environ.get("ATTRIBUTIONS_FILE", os.path.join(OUTPUT_DIR, "attributions.txt"))
DEFAULT_WEBSITE = os.environ.get("DEFAULT_WEBSITE", "sagunto.adventista.es")
WATERMARK_FILE = _resolve_path(os.environ.get("WATERMARK_FILE", "assets/right_sidebar_watermark.png"))
LOGO_FILE = _resolve_path(os.environ.get("LOGO_FILE", "assets/logo_watermark_bold.png"))

# Font file preferences: Segoe UI Black matches PowerPoint template
FONT_CANDIDATES = [
    _resolve_path(os.environ.get("FONT_FILE", "")),
    _resolve_path("segoe-ui-black.ttf"),
    "C:/Windows/Fonts/seguibl.ttf",
    _resolve_path("segoe-ui-bold.ttf"),
    "C:/Windows/Fonts/segoeuib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
FONT_FILE = next((f for f in FONT_CANDIDATES if f and os.path.exists(f)), None)

# Optional Unsplash API key
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()

# Curated high-res free-to-use Unsplash images per keyword for offline/reliable multi-variation generation
CURATED_UNSPLASH_COLLECTIONS = {
    "trabajo": [
        "https://images.unsplash.com/photo-1522071820081-009f0129c71c?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1497215728101-856f4ea42174?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1531482615713-2afd69097998?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1556761175-5973dc0f32e7?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1542744173-8e7e53415bb0?auto=format&fit=crop&w=1920&h=1080&q=85",
    ],
    "work": [
        "https://images.unsplash.com/photo-1497215728101-856f4ea42174?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1522071820081-009f0129c71c?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1531482615713-2afd69097998?auto=format&fit=crop&w=1920&h=1080&q=85",
    ],
    "oracion": [
        "https://images.unsplash.com/photo-1507692049790-de58290a4334?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1544717305-2782549b5136?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1445445290350-18a3b86e0b5b?auto=format&fit=crop&w=1920&h=1080&q=85",
    ],
    "prayer": [
        "https://images.unsplash.com/photo-1507692049790-de58290a4334?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1544717305-2782549b5136?auto=format&fit=crop&w=1920&h=1080&q=85",
    ],
    "biblia": [
        "https://images.unsplash.com/photo-1504052434569-70ad5836ab65?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1499209974431-9dddcece7f88?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1519817650390-64a93db51149?auto=format&fit=crop&w=1920&h=1080&q=85",
    ],
    "bible": [
        "https://images.unsplash.com/photo-1504052434569-70ad5836ab65?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1499209974431-9dddcece7f88?auto=format&fit=crop&w=1920&h=1080&q=85",
    ],
    "iglesia": [
        "https://images.unsplash.com/photo-1438032005730-c779502df39b?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1548625361-195995006b53?auto=format&fit=crop&w=1920&h=1080&q=85",
    ],
    "church": [
        "https://images.unsplash.com/photo-1438032005730-c779502df39b?auto=format&fit=crop&w=1920&h=1080&q=85",
    ],
    "default": [
        "https://images.unsplash.com/photo-1498050108023-c5249f4df085?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1451187580459-43490279c0fa?auto=format&fit=crop&w=1920&h=1080&q=85",
        "https://images.unsplash.com/photo-1518495973542-4542c06a5843?auto=format&fit=crop&w=1920&h=1080&q=85",
    ],
}


def build_sidebar_watermark(watermark_path=None, logo_path=None):
    """
    Builds the 1920x1080 right sidebar watermark:
      - Gradient begins solid on the bottom of the bar component and fades towards the top.
      - Includes the official logo at (x=1684, y=851).
    """
    wm_path = _resolve_path(watermark_path, WATERMARK_FILE)
    lg_path = _resolve_path(logo_path, LOGO_FILE)

    arr = np.zeros((1080, 1920, 4), dtype=np.uint8)
    bar_start = 1648
    bar_end = 1920

    # Vectorized gradient: solid on bottom (y=1079), smooth fade towards top (y=0)
    alpha_curve = np.round((np.linspace(0.0, 1.0, 1080) ** 0.8) * 255).astype(np.uint8)
    arr[:, bar_start:bar_end, :3] = 255
    arr[:, bar_start:bar_end, 3] = alpha_curve[:, np.newaxis]

    sidebar = Image.fromarray(arr, "RGBA")

    # Composite logo if available
    if lg_path and os.path.exists(lg_path):
        try:
            logo = Image.open(lg_path).convert("RGBA").resize((205, 205), Image.Resampling.LANCZOS)
            sidebar.paste(logo, (1684, 851), mask=logo.split()[3])
        except Exception as e:
            logger.warning("Logo overlay failed: %s", e)

    if wm_path:
        os.makedirs(os.path.dirname(wm_path) or ".", exist_ok=True)
        sidebar.save(wm_path)
    return sidebar


_HTTP_SESSION = requests.Session()
_HTTP_SESSION.headers.update({"User-Agent": "AutomaticThumbnail/1.0 (HomeServer/Proxmox)"})


@lru_cache(maxsize=64)
def _get_font(size, font_path=None):
    """Cached font loader to avoid repeated disk reads during typography sizing and batch rendering."""
    target = font_path or FONT_FILE
    if target:
        try:
            return ImageFont.truetype(target, size)
        except IOError:
            pass
    return ImageFont.load_default()


def _download_image(url, timeout=20):
    """Download an image using the shared HTTP session and return it as RGBA, or None on failure."""
    resp = _HTTP_SESSION.get(url, timeout=timeout)
    if resp.status_code == 200:
        return Image.open(BytesIO(resp.content)).convert("RGBA")
    return None


def get_unsplash_variations(keyword_or_url, count=1, access_key=None):
    """
    Returns a list of `count` (Image, attribution_dict) tuples.
    Supports:
      - Comma-separated list of URLs or direct URLs
      - Keyword search via Unsplash API (if UNSPLASH_ACCESS_KEY is set)
      - Curated free Unsplash collections by topic
    """
    raw = str(keyword_or_url).strip() if pd.notna(keyword_or_url) else ""
    api_key = (access_key if access_key is not None else UNSPLASH_ACCESS_KEY).strip()
    variations = []

    # Case 1: Direct URLs (single or comma-separated)
    if "http://" in raw or "https://" in raw:
        urls = [u.strip() for u in raw.split(",") if u.strip().startswith("http")]
        for u in urls:
            try:
                download_url = u
                if "images.unsplash.com" in u and "w=" not in u:
                    delimiter = "&" if "?" in u else "?"
                    download_url = f"{u}{delimiter}auto=format&fit=crop&w=1920&h=1080&q=85"
                img = _download_image(download_url)
                if img is not None:
                    attr = {
                        "author": "Unsplash Photographer",
                        "photo_url": u,
                        "attribution_text": f"Photo from Unsplash (Free License): {u}",
                    }
                    variations.append((img, attr))
                    if len(variations) >= count:
                        return variations
            except Exception as e:
                logger.warning("Failed to fetch direct URL '%s': %s", u, e)

    # Case 2: Unsplash API Search if key is provided
    if api_key and raw and len(variations) < count:
        needed = count - len(variations)
        try:
            api_resp = _HTTP_SESSION.get(
                "https://api.unsplash.com/search/photos",
                params={"query": raw, "orientation": "landscape", "per_page": min(10, needed)},
                headers={"Authorization": f"Client-ID {api_key}"},
                timeout=15,
            )
            if api_resp.status_code == 200:
                results = api_resp.json().get("results", [])
                for photo in results:
                    try:
                        img_url = photo["urls"]["raw"] + "&auto=format&fit=crop&w=1920&h=1080&q=85"
                        img = _download_image(img_url)
                        if img is None:
                            continue
                        author_name = photo["user"]["name"]
                        photo_page = f"{photo['links']['html']}?utm_source=automatic_thumbnail&utm_medium=referral"
                        attr = {
                            "author": author_name,
                            "photo_url": photo_page,
                            "attribution_text": f"Photo by {author_name} on Unsplash ({photo_page})",
                        }
                        variations.append((img, attr))
                        if len(variations) >= count:
                            return variations
                    except Exception as e:
                        logger.warning("Failed to download Unsplash API photo: %s", e)
        except Exception as e:
            logger.warning("Unsplash API search failed: %s", e)

    # Case 3: Curated Unsplash Free-License Collections
    clean_key = raw.lower().strip()
    pool = CURATED_UNSPLASH_COLLECTIONS.get(clean_key, CURATED_UNSPLASH_COLLECTIONS["default"])

    idx = 0
    while len(variations) < count and idx < len(pool):
        url = pool[idx]
        idx += 1
        try:
            img = _download_image(url)
            if img is not None:
                attr = {
                    "author": "Unsplash Photographer",
                    "photo_url": url,
                    "attribution_text": f"Photo from Unsplash (Free License): {url}",
                }
                variations.append((img, attr))
        except Exception as e:
            logger.warning("Failed to fetch curated URL '%s': %s", url, e)

    # Fallback if still empty
    if not variations:
        bg = Image.new("RGBA", (1920, 1080), (24, 28, 36, 255))
        variations.append((bg, {"attribution_text": "Solid background"}))

    return variations


def wrap_text(text, font, max_width, draw):
    """Wraps text so it fits within max_width pixels without overflowing."""
    words = text.split()
    if not words:
        return []

    lines = []
    current_line = []
    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        line_w = bbox[2] - bbox[0]
        if line_w <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
                current_line = [word]
            else:
                lines.append(word)
                current_line = []
    if current_line:
        lines.append(" ".join(current_line))
    return lines


def calculate_dynamic_typography(title, subtitle, draw, max_width=1500, max_height=520, font_path=None):
    """
    Dynamically sizes fonts and lines according to title length so that the
    composition closely mirrors the PowerPoint template ('plantilla caratula.pptx').
    """
    title_size = 90
    sub_size = 62

    while title_size >= 44:
        f_title = _get_font(title_size, font_path)
        f_sub = _get_font(sub_size, font_path)

        title_lines = wrap_text(title, f_title, max_width, draw)
        sub_lines = wrap_text(subtitle, f_sub, max_width, draw) if subtitle else []

        t_line_h = int(title_size * 1.14)
        s_line_h = int(sub_size * 1.20)
        gap = 20 if subtitle else 0

        total_h = (len(title_lines) * t_line_h) + gap + (len(sub_lines) * s_line_h)
        if total_h <= max_height and len(title_lines) <= 3:
            return f_title, f_sub, title_lines, sub_lines, t_line_h, s_line_h, total_h

        title_size -= 4
        sub_size = max(32, int(title_size * 0.68))

    return f_title, f_sub, title_lines, sub_lines, t_line_h, s_line_h, total_h


def draw_text_with_shadow(draw_img, text, position, font, text_color, shadow_blur=4, shadow_alpha=120):
    """
    Renders text with a soft drop shadow matching PowerPoint's outer shadow effect.
    """
    x, y = position
    shadow_pad = shadow_blur * 3
    bbox = draw_img.textbbox((0, 0), text, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    shadow_img = Image.new("RGBA", (max(1, text_w + shadow_pad * 2), max(1, text_h + shadow_pad * 2)), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow_img)
    s_draw.text((shadow_pad + 2, shadow_pad + 3), text, font=font, fill=(0, 0, 0, shadow_alpha))
    if shadow_blur > 0:
        shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(shadow_blur))

    draw_img._image.paste(shadow_img, (int(x - shadow_pad), int(y - shadow_pad)), mask=shadow_img.split()[3])
    draw_img.text((x, y), text, font=font, fill=text_color)


def render_thumbnail(bg_image, title, subtitle, output_path, watermark_img, site_url=DEFAULT_WEBSITE, font_path=None):
    """Renders a single 1920x1080 thumbnail on a given background."""
    bg = bg_image.copy()

    # Standardize to 1920x1080
    if bg.size != (1920, 1080):
        target_w, target_h = 1920, 1080
        scale = max(target_w / bg.width, target_h / bg.height)
        new_size = (int(bg.width * scale), int(bg.height * scale))
        bg = bg.resize(new_size, Image.Resampling.LANCZOS)
        left = (bg.width - target_w) // 2
        top = (bg.height - target_h) // 2
        bg = bg.crop((left, top, left + target_w, top + target_h))

    # Exposure adjustment (dim slightly for text contrast)
    enhancer = ImageEnhance.Brightness(bg)
    bg = enhancer.enhance(0.68)

    # Composite right sidebar watermark (solid on bottom, fading upward)
    if watermark_img:
        bg.paste(watermark_img, (0, 0), mask=watermark_img.split()[3])

    # Draw Typography
    draw = ImageDraw.Draw(bg)
    draw._image = bg

    left_margin = 40
    max_text_width = 1600

    f_title, f_sub, title_lines, sub_lines, t_h, s_h, total_h = calculate_dynamic_typography(
        str(title).strip(),
        str(subtitle).strip() if pd.notna(subtitle) else "",
        draw,
        max_width=max_text_width,
        font_path=font_path,
    )

    ideal_start_y = 680
    bottom_limit = 960
    if ideal_start_y + total_h > bottom_limit:
        start_y = max(220, bottom_limit - total_h)
    else:
        start_y = ideal_start_y

    current_y = start_y

    # Title: Segoe UI Black, color #F1F1F1
    for line in title_lines:
        draw_text_with_shadow(draw, line, (left_margin, current_y), f_title, "#F1F1F1", shadow_blur=4, shadow_alpha=120)
        current_y += t_h

    # Subtitle: Segoe UI Black, color #FFFFFF
    if sub_lines:
        current_y += 15
        for line in sub_lines:
            draw_text_with_shadow(draw, line, (left_margin, current_y), f_sub, "#FFFFFF", shadow_blur=3, shadow_alpha=110)
            current_y += s_h

    # URL: bottom left
    font_url = _get_font(28, font_path)
    draw_text_with_shadow(draw, site_url or DEFAULT_WEBSITE, (left_margin, 982), font_url, "#FFFFFF", shadow_blur=2, shadow_alpha=100)

    out_dir = os.path.dirname(output_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    bg.convert("RGB").save(output_path, "PNG")
    logger.info("Generated thumbnail: %s", output_path)
    return output_path


def sanitize_filename(raw_name, fallback_prefix="thumb"):
    """Return a filesystem-safe base filename without extension."""
    cleaned = re.sub(r"\.png$", "", str(raw_name or "").strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"[^\w\-_.]", "_", cleaned).strip("._")
    if not cleaned:
        cleaned = f"{fallback_prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    return cleaned


def write_attributions(entries, attributions_file=ATTRIBUTIONS_FILE, append=False):
    """Write or append Unsplash attribution entries to disk."""
    if not entries:
        return
    os.makedirs(os.path.dirname(attributions_file) or ".", exist_ok=True)
    header = "=== UNSPLASH BY-ATTRIBUTION FOR YOUTUBE DESCRIPTIONS ===\n\n"
    mode = "a" if (append and os.path.exists(attributions_file)) else "w"
    with open(attributions_file, mode, encoding="utf-8") as f:
        if mode == "w":
            f.write(header)
        elif os.path.getsize(attributions_file) == 0:
            f.write(header)
        else:
            f.write("\n")
        f.write("\n".join(entries) + "\n")


def generate_thumbnails(
    title,
    subtitle="",
    keyword_or_url="default",
    filename=None,
    variations=1,
    site_url=None,
    output_dir=None,
    attributions_file=None,
    unsplash_access_key=None,
    watermark_file=None,
    logo_file=None,
    font_file=None,
    watermark_img=None,
    append_attributions=True,
):
    """
    Generate one or more 1920x1080 thumbnails for a single title/subtitle pair.
    Returns a summary dictionary with generated filenames, full paths, and attributions.
    """
    target_dir = output_dir or OUTPUT_DIR
    attr_path = attributions_file or os.path.join(target_dir, "attributions.txt")
    os.makedirs(target_dir, exist_ok=True)

    count = max(1, min(10, int(variations or 1)))
    base_filename = sanitize_filename(filename or title)
    wm = watermark_img or build_sidebar_watermark(watermark_file, logo_file)

    fetched = get_unsplash_variations(keyword_or_url, count=count, access_key=unsplash_access_key)
    generated_files = []
    generated_paths = []
    attribution_entries = []

    for v_idx, (bg_img, attr) in enumerate(fetched):
        suffix = f"_var{v_idx + 1}" if len(fetched) > 1 else ""
        out_filename = f"{base_filename}{suffix}.png"
        output_file = os.path.join(target_dir, out_filename)

        render_thumbnail(
            bg_img,
            title,
            subtitle,
            output_file,
            wm,
            site_url=site_url or DEFAULT_WEBSITE,
            font_path=font_file,
        )
        generated_files.append(out_filename)
        generated_paths.append(output_file)

        if attr and attr.get("attribution_text"):
            attribution_entries.append(f"[{out_filename}]\n{attr['attribution_text']}\n")

    if attribution_entries:
        write_attributions(attribution_entries, attr_path, append=append_attributions)

    return {
        "title": str(title).strip(),
        "subtitle": str(subtitle).strip() if subtitle else "",
        "query": str(keyword_or_url or "").strip(),
        "files": generated_files,
        "paths": generated_paths,
        "attributions": attribution_entries,
    }


def batch_process(
    csv_path="miniaturas.csv",
    output_dir=None,
    attributions_file=None,
    site_url=None,
    unsplash_access_key=None,
    watermark_file=None,
    logo_file=None,
    font_file=None,
):
    """Process all rows in a CSV file and generate thumbnails + attributions."""
    resolved_csv = _resolve_path(csv_path)
    if not os.path.exists(resolved_csv):
        logger.error("CSV file not found: %s", resolved_csv)
        return {"error": f"CSV file not found: {resolved_csv}", "files": [], "rows_processed": 0}

    df = pd.read_csv(resolved_csv)
    target_dir = output_dir or OUTPUT_DIR
    attr_path = attributions_file or os.path.join(target_dir, "attributions.txt")
    os.makedirs(target_dir, exist_ok=True)

    watermark_img = build_sidebar_watermark(watermark_file, logo_file)
    all_attributions = []
    all_files = []

    for index, row in df.iterrows():
        raw_filename = str(row.get("Filename", f"thumb_{index}")).strip()
        title = row.get("Title", "")
        subtitle = row.get("Subtitle", "")
        keyword_or_url = row.get("Unsplash_Keyword_or_URL", "")

        variations_count = 1
        for col_name in ["Variations", "variations", "number of variations", "Numero de Variaciones"]:
            if col_name in row and pd.notna(row[col_name]):
                try:
                    variations_count = max(1, int(row[col_name]))
                    break
                except ValueError:
                    pass

        logger.info("Processing [%d/%d]: %s (%d variations)", index + 1, len(df), title, variations_count)
        result = generate_thumbnails(
            title=title,
            subtitle="" if pd.isna(subtitle) else subtitle,
            keyword_or_url=keyword_or_url,
            filename=raw_filename,
            variations=variations_count,
            site_url=site_url,
            output_dir=target_dir,
            attributions_file=attr_path,
            unsplash_access_key=unsplash_access_key,
            font_file=font_file,
            watermark_img=watermark_img,
            append_attributions=False,
        )
        all_files.extend(result["files"])
        all_attributions.extend(result["attributions"])

    if all_attributions:
        write_attributions(all_attributions, attr_path, append=False)
        logger.info("Attributions saved to: %s", attr_path)

    return {
        "rows_processed": len(df),
        "files": all_files,
        "attributions_file": attr_path,
    }


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s in %(module)s: %(message)s",
    )
    csv_file = sys.argv[1] if len(sys.argv) > 1 else "miniaturas.csv"
    batch_process(csv_file)


if __name__ == "__main__":
    main()
