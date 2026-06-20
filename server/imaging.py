"""キャプチャ画像の簡易シグネチャと類似度.

直前のスクリーンショットと見比べて「画面がほとんど変化していない（＝離席の
可能性）」を検出するために使う。重い画像処理ライブラリは使わず、Pillow で
小さなグレースケール縮小画像 (16x16) を作り、その画素列を指紋として保存する。

- signature(jpeg_bytes): 16x16 グレースケールの画素列を hex 文字列で返す
- similarity(a, b): 2つの指紋の一致率 (0.0〜100.0%) を返す

指紋同士の比較は 256 バイトの平均絶対差だけなので非常に軽い。JPEG の圧縮
ノイズや軽微なカーソル移動・時計の更新程度では一致率はほぼ 100% のまま
なので、「同じ画面が続いている」判定に向く。
"""
import io

from PIL import Image

# 指紋に使う縮小サイズ。小さいほどノイズに強く保存も軽い。
THUMB = (16, 16)


def signature(jpeg_bytes: bytes) -> str | None:
    """JPEG バイト列から指紋 (16x16 グレースケールの hex) を作る。失敗時 None."""
    try:
        im = Image.open(io.BytesIO(jpeg_bytes)).convert("L").resize(THUMB)
    except Exception:  # noqa: BLE001  壊れた画像でも撮影自体は止めない
        return None
    return im.tobytes().hex()


def similarity(sig_a: str | None, sig_b: str | None) -> float | None:
    """2つの指紋の一致率 (0.0〜100.0)。比較できなければ None."""
    if not sig_a or not sig_b:
        return None
    try:
        a = bytes.fromhex(sig_a)
        b = bytes.fromhex(sig_b)
    except ValueError:
        return None
    if not a or len(a) != len(b):
        return None
    diff = sum(abs(x - y) for x, y in zip(a, b))
    return 100.0 - (diff / len(a) / 255.0 * 100.0)
