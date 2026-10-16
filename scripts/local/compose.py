"""OMNY（ネームYAML）＋コマ画像 → 原稿PNG。
コマ割り・枠線・フキダシ・縦書きの文字は全部ここで描く（画像生成AIには文字を描かせない）。

使い方: python compose.py <omny.yaml> <panels_dir> <out_dir>
コマ画像は <panels_dir>/p{page}_{id}.png を読む。無いコマは灰色の箱になる（配置の下見に使える）。
顔よけの補助（Vision）は環境変数 FACES_BIN の実行ファイル（faces.swift を swiftc で作ったもの）を使う。
書体は macOS の游明朝・ヒラギノ・筑紫B丸ゴを探す（ローカル作画は macOS 前提）。
"""
import glob
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

S = 5  # OMNY の 1 単位（mm）あたりのピクセル数。A4 210x297 → 1050x1485
PAGE_W, PAGE_H = 210, 297
FONTS = "/System/Library/Fonts/"
_ASSET = "/System/Library/AssetsV2/com_apple_MobileAsset_Font*/*/AssetData/"


def _asset(name, index, fallback):
    hit = glob.glob(_ASSET + name)
    return (hit[0], index) if hit else fallback


F_MINCHO_L = (FONTS + "ヒラギノ明朝 ProN.ttc", 0)  # W3
F_SERIF = _asset("YuMincho.ttc", 4, F_MINCHO_L)  # 游明朝 +36p かな Demibold（セリフ）
F_SERIF_L = _asset("YuMincho.ttc", 3, F_MINCHO_L)  # 游明朝 +36p かな Medium（ナレーション）
F_GOTHIC_B = (FONTS + "ヒラギノ角ゴシック W8.ttc", 0)
F_GOTHIC_M = (FONTS + "ヒラギノ角ゴシック W6.ttc", 0)
F_MARU = _asset("TsukushiBMaruGothic.ttc", 1, (FONTS + "ヒラギノ丸ゴ ProN W4.ttc", 0))

LINE = 3  # フキダシの線の太さ(px)
BORDER = 4  # コマ枠の太さ(px)

# 縦書きで 90° 回す文字・右上に寄せる句読点・少し右上に寄せる小書き文字
ROTATE = set("ー―〜～…‥「」『』（）()【】-")
PUNCT = set("、。，．,.")
SMALL = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ")
NO_HEAD = PUNCT | SMALL | set("ー！？!?」』）")  # 行頭禁則
INK = (20, 20, 20)


def font(spec, size):
    return ImageFont.truetype(spec[0], size, index=spec[1])


def px(v):
    return int(round(v * S))


# ---------------------------------------------------------------- 文字組み
def split_phrases(text):
    """読点・句点・！？の直後で区切る（区切り文字は前の句に付ける）。"""
    out, cur = [], ""
    for ch in text:
        cur += ch
        if ch in "、。！？!?":
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    # 「…」で始まる句は前に付けない（そのまま）
    return out


MAXCOL = 9  # 1 列の字数の上限（漫画のセリフは長い列を嫌う）


def kind(ch):
    o = ord(ch)
    if 0x3040 <= o <= 0x309F:
        return "h"
    if 0x30A0 <= o <= 0x30FF:
        return "k"
    if 0x4E00 <= o <= 0x9FFF:
        return "c"
    return "o"


def eff(t):
    """句読点のぶら下げを許すので、末尾の句読点は字数に数えない。"""
    n = len(t)
    while n and t[n - 1] in PUNCT:
        n -= 1
    return n


PARTICLE = set("てではをがにもへと")


def soft_cut(ph, cap):
    """長い句を割る位置。字数が均等になる点の近くで、文字種の切り替わりや助詞の後を選ぶ。"""
    n = math.ceil(eff(ph) / cap)
    ideal = math.ceil(len(ph) / n)
    best, best_score = None, 1e9
    for cut in range(2, min(cap, len(ph) - 1) + 1):
        a, b = ph[cut - 1], ph[cut]
        if b in NO_HEAD:
            continue
        score = abs(cut - ideal)
        if (kind(a), kind(b)) in (("k", "c"), ("k", "h"), ("h", "c"), ("h", "k"), ("o", "c")):
            score -= 2.5
        if a in PARTICLE and kind(b) != "o":
            score -= 2
        if a in SMALL:  # 「っ」「ゃ」で列を終えると語が割れやすい
            score += 2
        if score < best_score:
            best, best_score = cut, score
    if best:
        return best
    cut = cap
    while cut > 1 and ph[cut] in NO_HEAD:
        cut -= 1
    return cut


def pack_columns(text, cap):
    """句単位で列に詰める。句が 1 列に収まらなければ文字で割る。禁則は行頭だけ見る。"""
    cols, cur = [], ""
    for ph in split_phrases(text):
        if eff(cur + ph) <= cap:
            cur += ph
            continue
        if cur:
            cols.append(cur)
            cur = ""
        while eff(ph) > cap:
            cut = soft_cut(ph, cap)
            cols.append(ph[:cut])
            ph = ph[cut:]
        cur = ph
    if cur:
        cols.append(cur)
    # 行頭禁則の後始末（前の列にぶら下げる）
    for i in range(1, len(cols)):
        while cols[i] and cols[i][0] in NO_HEAD:
            cols[i - 1] += cols[i][0]
            cols[i] = cols[i][1:]
    return [c for c in cols if c]


def layout_cols(text, fs, max_h, allow_split):
    """字の大きさ fs・列の高さ max_h で列割りする。句を割れないなら None。"""
    cap = min(MAXCOL, max(1, int(max_h / (fs * 1.04))))
    if eff(text) > 6:  # 長めのセリフは 1 列にせず、2 列以上に組む
        cap = min(cap, max(5, math.ceil(eff(text) / 2) + 1))
    if not allow_split and max(eff(ph) for ph in split_phrases(text)) > cap:
        return None
    return pack_columns(text, cap)


def text_size(cols, fs):
    return (len(cols) - 1) * fs * 1.3 + fs, max(len(c) for c in cols) * fs * 1.04


def draw_char_v(img, ch, cx, y, fs, fnt, fill, stroke=0, stroke_fill=None):
    """縦書きの 1 文字を (中心x, 上端y) のセルに置く。"""
    if ch in ROTATE:
        tile = Image.new("RGBA", (fs * 2, fs * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(tile)
        d.text((fs, fs), ch, font=fnt, fill=fill, anchor="mm",
               stroke_width=stroke, stroke_fill=stroke_fill)
        tile = tile.rotate(-90, resample=Image.BICUBIC)
        img.alpha_composite(tile, (int(cx - fs), int(y + fs * 0.52 - fs)))
        return
    d = ImageDraw.Draw(img)
    dx, dy = 0, 0
    if ch in PUNCT:
        dx, dy = fs * 0.6, -fs * 0.6
    elif ch in SMALL:
        dx, dy = fs * 0.1, -fs * 0.08
    d.text((cx + dx, y + fs * 0.52 + dy), ch, font=fnt, fill=fill, anchor="mm",
           stroke_width=stroke, stroke_fill=stroke_fill)


def draw_cols(img, cols, fs, center, fspec, fill=INK, stroke=0, stroke_fill=None):
    fnt = font(fspec, fs)
    tw, th = text_size(cols, fs)
    right = center[0] + tw / 2 - fs / 2  # 1 列目（右端）の中心
    top = center[1] - th / 2
    for i, col in enumerate(cols):
        cx = right - i * fs * 1.3
        for j, ch in enumerate(col):
            draw_char_v(img, ch, cx, top + j * fs * 1.04, fs, fnt, fill, stroke, stroke_fill)


def draw_horizontal(img, text, box, fspec, fill, stroke, stroke_fill):
    x0, y0, x1, y1 = box
    fs = int(min((y1 - y0) * 0.95, (x1 - x0) / max(1, len(text)) * 1.15))
    d = ImageDraw.Draw(img)
    d.text(((x0 + x1) / 2, (y0 + y1) / 2), text, font=font(fspec, fs), fill=fill,
           anchor="mm", stroke_width=stroke, stroke_fill=stroke_fill)


# ---------------------------------------------------------------- フキダシ
# 形ごとの「文字が入る内側の割合」と、文字の大きさの上限・下限(px)
SHAPES = {
    "normal": dict(rx=0.70, ry=0.76, fmax=28, fmin=16),
    "thought": dict(rx=0.62, ry=0.66, fmax=24, fmin=14),
    "flash": dict(rx=0.60, ry=0.64, fmax=40, fmin=18),
}
GROW = 1.35  # ネームの枠より最大でどこまで膨らませてよいか


def plan_bubble(text, bbox, panel, shape):
    """字の大きさ・列割り・実際のフキダシ枠を決める。枠はネームの枠を中心に、
    文字が入るまで膨らませ、コマからはみ出さないように寄せる。"""
    k = SHAPES[shape]
    bx0, by0, bx1, by1 = bbox
    bw, bh = bx1 - bx0, by1 - by0
    pw, ph = panel[2] - panel[0], panel[3] - panel[1]
    lim_w, lim_h = min(bw * GROW, pw * 0.9), min(bh * GROW, ph * 0.9)
    best = None
    for allow_split in (False, True):
        for fs in range(k["fmax"], k["fmin"] - 1, -1):
            cols = layout_cols(text, fs, lim_h * k["ry"], allow_split)
            if not cols:
                continue
            tw, th = text_size(cols, fs)
            need_w, need_h = max(bw, tw / k["rx"]), max(bh, th / k["ry"])
            if need_w <= lim_w and need_h <= lim_h:
                best = (fs, cols, need_w, need_h)
                break
        if best:
            break
    if not best:
        fs = k["fmin"]
        cols = layout_cols(text, fs, lim_h * k["ry"], True)
        tw, th = text_size(cols, fs)
        best = (fs, cols, tw / k["rx"], th / k["ry"])
    fs, cols, w, h = best
    cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
    m = 2 * S
    cx = min(max(cx, panel[0] + m + w / 2), panel[2] - m - w / 2)
    cy = min(max(cy, panel[1] + m + h / 2), panel[3] - m - h / 2)
    return fs, cols, (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


# ---------------------------------------------------------------- 顔よけ
# ネームの人物枠のうち「顔」とみなす割合（上から高さ・中央の幅）。size ごと。
HEAD = {"full-body": (0.20, 0.45), "full": (0.20, 0.45), "waist-up": (0.42, 0.55),
        "bust-up": (0.50, 0.60), "face": (0.65, 0.60), "close-up": (0.65, 0.60)}
KEEP_M = 1.5 * S  # 避ける範囲のまわりに取る余白(px)
FACES_BIN = Path(os.environ.get("FACES_BIN") or Path(__file__).resolve().parent / "faces")


def overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0


def name_heads(panel):
    """ネームの人物枠から顔のあたりの枠（px）を出す。"""
    out = []
    for f in panel.get("figures", []) or []:
        b = f["bbox"]
        fh, fw = HEAD.get(f.get("size", "waist-up"), HEAD["waist-up"])
        cx = b["x"] + b["w"] / 2
        out.append((px(cx - b["w"] * fw / 2), px(b["y"]), px(cx + b["w"] * fw / 2), px(b["y"] + b["h"] * fh)))
    return out


def seen_heads(art, box):
    """コマの絵から Vision（tools/faces）で顔・頭を探す。アニメ絵では取れないことが多いので補助。"""
    if not FACES_BIN.exists():
        return []
    with tempfile.NamedTemporaryFile(suffix=".png") as tf:
        art.convert("RGB").save(tf.name)
        try:
            res = json.loads(subprocess.run([str(FACES_BIN), tf.name], capture_output=True,
                                            text=True, timeout=60).stdout)
        except Exception:
            return []
    r = next(iter(res.values()), {}) if res else {}
    W, H = box[2] - box[0], box[3] - box[1]
    out = []
    for x0, y0, x1, y1, c in r.get("face", []):
        if c >= 0.4:  # 顔の枠は髪を含まないので上と横に広げる
            fw, fh = x1 - x0, y1 - y0
            out.append((x0 - fw * 0.15, y0 - fh * 0.3, x1 + fw * 0.15, y1 + fh * 0.05))
    for x0, y0, x1, y1, c in r.get("head", []):  # 骨格（頭の点と首）から出した頭の枠
        if c >= 0.3:
            out.append((x0, y0, x1, y1))
    return [(box[0] + a * W, box[1] + b * H, box[0] + c * W, box[1] + d * H) for a, b, c, d in out]


def place(w, h, center, panel, keep):
    """幅 w・高さ h の箱を、keep（顔や置き済みの文字）に重ならず、コマに収まり、
    ネームの位置 center にいちばん近い所に置く。無理なら重なりが最小の所。"""
    m = 2 * S
    xs = (panel[0] + m + w / 2, panel[2] - m - w / 2)
    ys = (panel[1] + m + h / 2, panel[3] - m - h / 2)
    if xs[0] > xs[1] or ys[0] > ys[1]:
        return center, 1.0
    keep = [(k[0] - KEEP_M, k[1] - KEEP_M, k[2] + KEEP_M, k[3] + KEEP_M) for k in keep]
    step = S
    best = None
    x = xs[0]
    while x <= xs[1] + 0.1:
        y = ys[0]
        while y <= ys[1] + 0.1:
            r = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
            ov = sum(overlap(r, k) for k in keep) / (w * h)
            key = (round(ov, 3), math.hypot(x - center[0], y - center[1]))
            if best is None or key < best[0]:
                best = (key, (x, y))
            y += step
        x += step
    return best[1], best[0][0]


# 重なるときに試す、ネームの枠の縮め方（幅・高さの倍率）。字はなるべく大きいまま、枠を細長く・小さくする。
SQUEEZE = [(1.0, 1.0), (0.85, 1.0), (0.7, 1.15), (0.85, 0.85), (0.6, 1.3), (0.7, 0.75), (0.55, 0.6)]


def place_bubble(text, box, pbox, shape, keep, label):
    """フキダシを顔よけしながら置く。重なるときは枠の形を変えて試す。"""
    c0 = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
    bw, bh = box[2] - box[0], box[3] - box[1]
    tried = None
    for sx, sy in SQUEEZE:
        sb = (c0[0] - bw * sx / 2, c0[1] - bh * sy / 2, c0[0] + bw * sx / 2, c0[1] + bh * sy / 2)
        fs, cols, bub = plan_bubble(text, sb, pbox, shape)
        w, h = bub[2] - bub[0], bub[3] - bub[1]
        (cx, cy), ov = place(w, h, c0, pbox, keep)
        if tried is None or ov < tried[0] - 1e-6:
            tried = (ov, fs, cols, (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2))
        if ov == 0:
            break
    ov, fs, cols, bub = tried
    moved = math.hypot((bub[0] + bub[2]) / 2 - c0[0], (bub[1] + bub[3]) / 2 - c0[1]) / S
    if moved > 0.5 or ov > 0:
        print(f"  {label}: {moved:.0f}mm 動かした・字 {fs}px" + (f"（重なり {ov:.0%} が残る）" if ov > 0 else ""))
    return fs, cols, bub


def ellipse_point(cx, cy, rx, ry, ang):
    return cx + rx * math.cos(ang), cy + ry * math.sin(ang)


def exit_point(cx, cy, rx, ry, a, b):
    """点 a（楕円の内側）から b（外側）へ向かう線分が楕円を横切る点。"""
    lo, hi = 0.0, 1.0
    for _ in range(30):
        t = (lo + hi) / 2
        x, y = a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        if ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 < 1:
            lo = t
        else:
            hi = t
    return a[0] + (b[0] - a[0]) * lo, a[1] + (b[1] - a[1]) * lo


def bubble_normal(img, box, target):
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    tail = None
    if target:
        tx, ty = target
        ang = math.atan2(ty - cy, tx - cx)
        half = 0.10 * math.pi
        b1 = ellipse_point(cx, cy, rx * 0.85, ry * 0.85, ang - half)
        b2 = ellipse_point(cx, cy, rx * 0.85, ry * 0.85, ang + half)
        ex, ey = ellipse_point(cx, cy, rx, ry, ang)
        dist = math.hypot(tx - ex, ty - ey)
        L = max(3 * S, min(dist * 0.5, 9 * S))
        tip = (ex + L * math.cos(ang), ey + L * math.sin(ang))
        tail = (b1, tip, b2)
        d.polygon(tail, fill="white")
    d.ellipse(box, fill="white", outline="black", width=LINE)
    if tail:
        b1, tip, b2 = tail
        d.polygon(tail, fill="white")  # 付け根の楕円の線を消す
        for b in (b1, b2):
            d.line([exit_point(cx, cy, rx, ry, b, tip), tip], fill="black", width=LINE)
    return (cx, cy)


def bubble_thought(img, box, target):
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    n = 14
    r = min(rx, ry) * 0.30
    pts = [ellipse_point(cx, cy, rx - r * 0.7, ry - r * 0.7, 2 * math.pi * k / n) for k in range(n)]
    for (qx, qy) in pts:
        d.ellipse((qx - r, qy - r, qx + r, qy + r), fill="white", outline="black", width=LINE)
    for (qx, qy) in pts:
        d.ellipse((qx - r + LINE, qy - r + LINE, qx + r - LINE, qy + r - LINE), fill="white")
    d.ellipse((cx - rx + r * 0.7, cy - ry + r * 0.7, cx + rx - r * 0.7, cy + ry - r * 0.7), fill="white")
    if target:  # 小さな泡を 2 つ、話者のほうへ
        ang = math.atan2(target[1] - cy, target[0] - cx)
        ex, ey = ellipse_point(cx, cy, rx, ry, ang)
        for k, rr in ((1, r * 0.45), (2, r * 0.28)):
            bx, by = ex + math.cos(ang) * r * 1.1 * k, ey + math.sin(ang) * r * 1.1 * k
            d.ellipse((bx - rr, by - rr, bx + rr, by + rr), fill="white", outline="black", width=LINE)
    return (cx, cy)


def bubble_flash(img, box, target):
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = box
    cx, cy, rx, ry = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2, (y1 - y0) / 2
    n = 22
    pts = []
    for k in range(n * 2):
        a = math.pi * k / n
        f = 1.0 if k % 2 == 0 else 0.80
        f *= 1.0 + 0.05 * math.sin(k * 2.7)
        pts.append(ellipse_point(cx, cy, rx * f, ry * f, a))
    d.polygon(pts, fill="white", outline="black", width=LINE)
    return (cx, cy)


def caption(img, text, bbox, panel, keep=(), label="caption"):
    """ナレーションの箱。ネームの枠が横長なら横書き、縦長なら縦書き。顔よけして置き、箱を返す。"""
    d = ImageDraw.Draw(img)
    bx0, by0, bx1, by1 = bbox
    bw, bh = bx1 - bx0, by1 - by0
    pad = 2.2 * S
    if bw > bh * 1.2:  # 横書き
        for fs in range(24, 13, -1):
            cap = max(1, int((bw * GROW - pad * 2) / fs))
            lines = pack_columns(text, cap)
            tw = max(len(l) for l in lines) * fs
            th = len(lines) * fs * 1.45
            if th + pad * 2 <= bh * GROW:
                break
        w, h = max(bw * 0.6, tw + pad * 2), th + pad * 2
    else:
        fs = 24
        cols = layout_cols(text, fs, bh * GROW - pad * 2, False) or layout_cols(text, fs, bh * GROW - pad * 2, True)
        tw, th = text_size(cols, fs)
        w, h = tw + pad * 2, th + pad * 2
    c0 = ((bx0 + bx1) / 2, (by0 + by1) / 2)
    (cx, cy), ov = place(w, h, c0, panel, keep)
    moved = math.hypot(cx - c0[0], cy - c0[1]) / S
    if moved > 0.5 or ov > 0:
        print(f"  {label}: {moved:.0f}mm 動かした" + (f"（重なり {ov:.0%} が残る）" if ov > 0 else ""))
    d.rectangle((cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), fill="white", outline="black", width=LINE)
    if bw > bh * 1.2:
        fnt = font(F_SERIF_L, fs)
        y = cy - th / 2 + fs * 0.72
        for l in lines:
            d.text((cx - tw / 2, y), l, font=fnt, fill=INK, anchor="lm")
            y += fs * 1.45
    else:
        draw_cols(img, cols, fs, (cx, cy), F_SERIF_L)
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


# ---------------------------------------------------------------- 本体
def cover(im, w, h):
    """縦横比を保って w×h を覆うように拡縮し、中央で切り抜く。"""
    sw, sh = im.size
    k = max(w / sw, h / sh)
    im = im.resize((max(w, round(sw * k)), max(h, round(sh * k))), Image.LANCZOS)
    l, t = (im.width - w) // 2, (im.height - h) // 2
    return im.crop((l, t, l + w, t + h))


def figure_target(panel, speaker):
    for f in panel.get("figures", []) or []:
        if f.get("name") == speaker:
            b = f["bbox"]
            # 顔のあたり（人物枠の上 1/3）をしっぽの向け先にする
            return px(b["x"] + b["w"] / 2), px(b["y"] + b["h"] * 0.3)
    return None


def compose_page(manga, page, panels_dir):
    W, H = px(PAGE_W), px(PAGE_H)
    img = Image.new("RGBA", (W, H), "white")
    d = ImageDraw.Draw(img)
    keeps = {}  # コマごとの「文字を置かない所」（顔のあたり）
    for p in page["panels"]:
        sh = p["shape"]
        box = (px(sh["x"]), px(sh["y"]), px(sh["x"] + sh["w"]), px(sh["y"] + sh["h"]))
        keeps[p["id"]] = name_heads(p)
        src = panels_dir / f"p{page['page']}_{p['id']}.png"
        if src.exists():
            art = cover(Image.open(src).convert("RGBA"), box[2] - box[0], box[3] - box[1])
            img.alpha_composite(art, (box[0], box[1]))
            keeps[p["id"]] += seen_heads(art, box)
        else:
            d.rectangle(box, fill=(235, 235, 235))
        d.rectangle(box, outline="black", width=BORDER)

    for p in page["panels"]:  # フキダシは全コマの絵の上に描く
        sh = p["shape"]
        pbox = (px(sh["x"]), px(sh["y"]), px(sh["x"] + sh["w"]), px(sh["y"] + sh["h"]))
        keep = list(keeps[p["id"]])
        # ナレーションを先に置き、フキダシはそれも避ける
        bubbles = sorted(p.get("bubbles", []) or [], key=lambda b: b.get("shape") != "caption")
        for b in bubbles:
            bb = b["bbox"]
            box = (px(bb["x"]), px(bb["y"]), px(bb["x"] + bb["w"]), px(bb["y"] + bb["h"]))
            shape = b.get("shape", "normal")
            target = figure_target(p, b.get("speaker")) if b.get("speaker") else None
            text = b["text"]
            label = f"p{page['page']}_{p['id']}「{text[:8]}」"
            if shape == "caption":
                keep.append(caption(img, text, box, pbox, keep, label))
            elif shape == "handwritten":
                if bb["w"] > bb["h"] * 1.5:
                    draw_horizontal(img, text, box, F_MARU, (90, 60, 40), 4, "white")
                else:
                    fs, cols, _ = plan_bubble(text, box, pbox, "flash")
                    draw_cols(img, cols, fs, ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2),
                              F_MARU, (90, 60, 40), 4, "white")
            else:
                shape = shape if shape in SHAPES else "normal"
                fs, cols, bub = place_bubble(text, box, pbox, shape, keep, label)
                keep.append(bub)
                draw = {"normal": bubble_normal, "thought": bubble_thought, "flash": bubble_flash}[shape]
                center = draw(img, bub, target)
                if shape == "flash":
                    draw_cols(img, cols, fs, center, F_GOTHIC_B)
                else:
                    draw_cols(img, cols, fs, center, F_SERIF)

    meta = manga["meta"]
    hf = font(F_GOTHIC_M, 20)
    d = ImageDraw.Draw(img)
    d.text((px(10), px(5)), meta["title"], font=hf, fill="black", anchor="lm")
    d.text((px(200), px(5)), f"{page['page']}/{meta['page_count']}", font=hf, fill="black", anchor="rm")
    d.text((W / 2, px(292)), meta.get("author", ""), font=font(F_GOTHIC_M, 18), fill="black", anchor="mm")
    return img.convert("RGB")


def main():
    omny, panels_dir, out_dir = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    manga = yaml.safe_load(omny.read_text())["manga"]
    out_dir.mkdir(parents=True, exist_ok=True)
    for page in manga["pages"]:
        out = out_dir / f"page{page['page']}.png"
        compose_page(manga, page, panels_dir).save(out)
        print(out)


if __name__ == "__main__":
    main()
