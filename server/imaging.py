"""キャプチャ画像の簡易シグネチャと類似度（複数モニター対応）.

直前のスクリーンショットと見比べて「画面がほとんど変化していない（＝離席の
可能性）」を検出するために使う。重い画像処理ライブラリは使わず、Pillow で
小さなグレースケール縮小画像を作り、その画素列を指紋として保存する。

複数モニターは横一列に連結した 1 枚として送られてくる。全体を一括で 1 枚に
縮めて平均すると、モニターが増えるほど 1 台あたりの比重が下がり「1 台だけで
作業している」状態を見逃しやすい（＝働いているのに離席判定）。これを避けるため:

- signature(jpeg, tiles): モニター枚数 tiles に応じて (16*tiles)x16 に縮小して
  指紋化する。各モニターが自分用の 16x16 セルを保持する。
- similarity(a, b): モニター（16 列幅のブロック）ごとに一致率を出し、その
  **最小値**（＝最も動いているモニター）を返す。どれか 1 台でも大きく動けば
  「変化あり」と判定され、停滞カウントがリセットされる。

指紋の長さからモニター枚数を復元できる（256 バイト = 1 台）。tiles=1 のときは
従来どおり 16x16 全体の平均と一致する（後方互換）。
"""
import io

from PIL import Image

# 1 モニターあたりの縮小サムネの一辺（16x16=256セル）
CELL = 16


def signature(jpeg_bytes: bytes, tiles: int = 1) -> str | None:
    """JPEG から指紋 ((16*tiles)x16 グレースケールの hex) を作る。失敗時 None.

    tiles は連結されているモニター枚数。各モニターに 16x16 を割り当てる。
    """
    try:
        tiles = max(1, min(8, int(tiles)))
    except (TypeError, ValueError):
        tiles = 1
    try:
        im = (
            Image.open(io.BytesIO(jpeg_bytes))
            .convert("L")
            .resize((CELL * tiles, CELL))
        )
    except Exception:  # noqa: BLE001  壊れた画像でも撮影自体は止めない
        return None
    return im.tobytes().hex()


def similarity(sig_a: str | None, sig_b: str | None) -> float | None:
    """2つの指紋の一致率 (0.0〜100.0)。比較できなければ None.

    モニター（16列ブロック）ごとに一致率を計算し、最小値を返す。これにより
    1 台でも大きく変化していれば一致率は下がる（停滞と見なされない）。
    指紋の長さ（=モニター枚数）が違う場合は比較不能として None（枚数が変わった
    ＝レイアウト変更なので「変化あり」扱いになる）。
    """
    if not sig_a or not sig_b:
        return None
    try:
        a = bytes.fromhex(sig_a)
        b = bytes.fromhex(sig_b)
    except ValueError:
        return None
    block = CELL * CELL
    if not a or len(a) != len(b) or len(a) % block != 0:
        return None
    tiles = len(a) // block
    width = CELL * tiles
    worst = 100.0
    for t in range(tiles):
        diff = 0
        for r in range(CELL):
            base = r * width + t * CELL
            for c in range(CELL):
                i = base + c
                diff += abs(a[i] - b[i])
        sim_t = 100.0 - (diff / block / 255.0 * 100.0)
        if sim_t < worst:
            worst = sim_t
    return worst
