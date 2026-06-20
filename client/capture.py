"""画面キャプチャ (複数モニター対応・縮小/ぼかしでプライバシー配慮)."""
import io

from PIL import Image, ImageFilter


def grab_screen() -> Image.Image:
    """全モニターを撮影する。複数あれば横に並べた 1 枚の画像にして返す。

    要ディスプレイ環境。mss.monitors[0] は全画面を結合した仮想領域、[1:] が
    各実モニターなので、各モニターを個別に撮って横に並べる (結合領域だと
    モニター配置のすき間に黒帯が入るため、個別取得して並べる)。
    """
    import mss

    with mss.MSS() as sct:
        mons = sct.monitors[1:] or [sct.monitors[0]]
        imgs = []
        for m in mons:
            shot = sct.grab(m)
            imgs.append(Image.frombytes("RGB", shot.size, shot.rgb))
    if len(imgs) == 1:
        return imgs[0]
    composite = tile_horizontally(imgs)
    composite.n_tiles = len(imgs)   # to_jpeg がモニター枚数に応じて上限幅を決める
    return composite


def tile_horizontally(imgs: list[Image.Image], gap: int = 8,
                      bg=(17, 17, 27)) -> Image.Image:
    """複数画像を共通の高さ (最小の高さ) に揃えて横に並べた 1 枚にする。

    モニターごとに解像度が違っても破綻しないよう高さを揃え、間に細い仕切りを
    入れて区切りを分かりやすくする。
    """
    h = min(im.height for im in imgs)
    scaled = [
        im if im.height == h
        else im.resize((max(1, round(im.width * h / im.height)), h))
        for im in imgs
    ]
    width = sum(im.width for im in scaled) + gap * (len(scaled) - 1)
    canvas = Image.new("RGB", (width, h), bg)
    x = 0
    for im in scaled:
        canvas.paste(im, (x, 0))
        x += im.width + gap
    return canvas


def to_jpeg(img: Image.Image, max_width: int = 1280, blur: int = 0,
            quality: int = 60, tiles: int | None = None) -> bytes:
    # マルチモニター合成は 1 モニターあたり max_width を確保するため、実際の
    # モニター枚数(tiles)に応じて上限幅を広げる。縦横比での推測はしない。
    if tiles is None:
        tiles = getattr(img, "n_tiles", 1)
    cap = max_width * max(1, tiles)
    if img.width > cap:
        img = img.resize((cap, max(1, round(img.height * cap / img.width))))
    if blur > 0:
        img = img.filter(ImageFilter.GaussianBlur(blur))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()
