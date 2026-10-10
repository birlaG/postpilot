"""Branded image cards and Shorts clips. Pillow draws the card; ffmpeg turns it into a vertical video."""
import shutil
import subprocess
import uuid
from datetime import date

from django.conf import settings
from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]
SQUARE = (1080, 1080)
VERTICAL = (1080, 1920)


class MediaError(Exception):
    pass


def _font(size):
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _hex(value, fallback):
    value = (value or "").lstrip("#")
    if len(value) != 6:
        return fallback
    try:
        return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return fallback


def _wrap(draw, text, font, max_width):
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def _out_path(ext):
    rel_dir = f"{'cards' if ext == 'png' else 'videos'}/{date.today():%Y/%m}"
    (settings.MEDIA_ROOT / rel_dir).mkdir(parents=True, exist_ok=True)
    return f"{rel_dir}/{uuid.uuid4().hex}.{ext}"


def _mix(a, b, t):
    return tuple(int(a[i] * (1 - t) + b[i] * t) for i in range(3))


def draw_card(*, text, category_name, brand, handle="", size=SQUARE):
    """Draw a card image in the brand's theme and return the PIL image."""
    width, height = size
    primary = _hex(brand.primary_color, (79, 70, 229))
    secondary = _hex(brand.secondary_color, (17, 24, 39))
    theme = getattr(brand, "card_theme", "gradient") or "gradient"
    ink, soft_ink, centred = (255, 255, 255), (235, 235, 245), False
    pill = (255, 255, 255)
    img = Image.new("RGB", size, primary)
    px = ImageDraw.Draw(img)
    if theme == "solid":
        px.rectangle([0, 0, width, height], fill=primary)
    elif theme == "dark":
        dark = _mix(secondary, (0, 0, 0), 0.6)
        px.rectangle([0, 0, width, height], fill=dark)
        px.rectangle([0, 0, 44, height], fill=primary)
        pill = primary
    elif theme == "light":
        light = (247, 247, 251)
        px.rectangle([0, 0, width, height], fill=light)
        px.rectangle([0, height - 18, width, height], fill=primary)
        ink, soft_ink, pill = _mix(secondary, (0, 0, 0), 0.5), _mix(secondary, light, 0.35), primary
    else:  # gradient and bold
        for y in range(height):
            px.line([(0, y), (width, y)], fill=_mix(primary, secondary, y / height))
        centred = theme == "bold"
    draw = ImageDraw.Draw(img)

    pad = 90
    label = category_name.upper()
    pill_font = _font(30)
    label_w = draw.textlength(label, font=pill_font)
    px0 = (width - label_w - 20) // 2 if centred else pad
    draw.rounded_rectangle([px0, pad, px0 + 20 + label_w, pad + 62], radius=31, outline=pill, width=3)
    draw.text((px0 + 10, pad + 12), label, font=pill_font, fill=pill)

    available = height - 2 * pad - 260
    size_px = 104 if centred else 84
    while size_px > 40:
        font = _font(size_px)
        lines = _wrap(draw, text.strip() or " ", font, width - 2 * pad)
        if len(lines) * int(size_px * 1.25) <= available:
            break
        size_px -= 6
    block = len(lines) * int(size_px * 1.25)
    # vertical cards and bold cards centre the text block; the others start under the pill
    y = (max(pad + 150, (height - block) // 2)) if (height != width or centred) else pad + 150
    for line in lines:
        x = (width - draw.textlength(line, font=font)) // 2 if centred else pad
        draw.text((x, y), line, font=font, fill=ink)
        y += int(size_px * 1.25)

    footer = brand.watermark or handle
    if footer:
        fx = (width - draw.textlength(footer, font=_font(34))) // 2 if centred else pad
        draw.text((fx, height - pad - 40), footer, font=_font(34), fill=soft_ink)
    return img


def render_card(*, text, category_name, brand, handle="", size=SQUARE):
    """Draw the card and return its path relative to MEDIA_ROOT."""
    img = draw_card(text=text, category_name=category_name, brand=brand, handle=handle, size=size)
    rel = _out_path("png")
    img.save(settings.MEDIA_ROOT / rel, "PNG", optimize=True)
    return rel


def render_video(*, text, category_name, brand, handle="", seconds=None):
    """Render a silent vertical clip (1080x1920) from a freshly drawn vertical card. Needs ffmpeg."""
    if shutil.which("ffmpeg") is None:
        raise MediaError("ffmpeg is not installed on the server")
    card_rel = render_card(text=text, category_name=category_name, brand=brand, handle=handle, size=VERTICAL)
    rel = _out_path("mp4")
    seconds = seconds or settings.VIDEO_SECONDS
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", "30", "-i", str(settings.MEDIA_ROOT / card_rel),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",  # YouTube prefers a file with an audio track
        "-t", str(seconds), "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart",
        str(settings.MEDIA_ROOT / rel),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=120)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", b"") or b""
        raise MediaError(f"ffmpeg failed: {detail.decode(errors='ignore')[:200]}") from exc
    finally:
        try:
            (settings.MEDIA_ROOT / card_rel).unlink()  # the intermediate card is not needed
        except OSError:
            pass
    return rel
