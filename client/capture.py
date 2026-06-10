"""Screen capture with privacy-friendly downscale / blur."""
import io

from PIL import Image, ImageFilter


def grab_screen() -> Image.Image:
    """Capture the primary monitor. Requires a display."""
    import mss

    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[1])
        return Image.frombytes("RGB", shot.size, shot.rgb)


def to_jpeg(img: Image.Image, max_width: int = 1280, blur: int = 0,
            quality: int = 60) -> bytes:
    if img.width > max_width:
        img = img.resize((max_width, int(img.height * max_width / img.width)))
    if blur > 0:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()
