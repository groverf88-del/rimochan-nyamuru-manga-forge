"""OMNY（ネームYAML）→ ローカル作画用の英語プロンプトと作業表。

使い方: python3 prompts.py <omny.yaml> <workdir> [--llm "claude -p"] [--ref 名前=画像]...

workdir に次を書く（forge-local.sh が作る作業フォルダ）:
  prompts/ref_cN.txt   キャラ N の立ち絵のプロンプト（Z-Image-Turbo 用）
  prompts/pP_I.txt     P ページ I コマ目のプロンプト（FLUX.2-klein の編集用）
  refs.tsv             key, 名前, 立ち絵のプロンプト, 立ち絵の出力先, seed
  panels.tsv           id, 幅, 高さ, seed, 参照する立ち絵（, 区切り / -）, プロンプト

英語の文面（場面・キャラの見た目）だけを LLM に書かせ、それ以外は全部ここで決める:
コマの画素数・seed・背景の描き込み量・人物の大きさと位置・フキダシのために空ける場所・
「参照の子として描く」前置き・文字なしの念押し。
プロンプトのファイルが既にあれば LLM を呼ばずにそれを使う（手で直した文面を残すため）。
"""
import argparse
import json
import math
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

AREA = 1_000_000  # 1 コマの画素数の目安（FLUX.2-klein / Z-Image がきれいに描ける大きさ）
REF_W, REF_H = 832, 1216  # 立ち絵の大きさ
DEFAULT_STYLE = "Japanese anime illustration, full color, clean crisp line art, cel shading."
NO_TEXT = "Clean image without any text, letters, logos, speech bubbles or watermarks."

# bg（背景の描き込み量）→ 文言
BG = {2: "Highly detailed background.", 1: "", 0: "Simple, minimal background with no detailed scenery.",
      -1: "Plain white background."}
# figures.size → 写る範囲
SIZE = {"face": "a close-up of the face", "close-up": "a close-up of the face",
        "bust-up": "shown from the chest up", "waist-up": "shown from the waist up",
        "knee-up": "shown from the knees up", "full-body": "full body", "full": "full body"}


def r16(v):
    return max(256, int(round(v / 16)) * 16)


def panel_px(shape):
    r = shape["w"] / shape["h"]
    return r16(math.sqrt(AREA * r)), r16(math.sqrt(AREA / r))


def where(fx, fy):
    """コマの中の位置（0〜1）を英語の場所の名前にする。"""
    h = "left" if fx < 0.36 else "right" if fx > 0.64 else ""
    v = "upper" if fy < 0.36 else "lower" if fy > 0.64 else ""
    if not h and not v:
        return "center"
    if not v:
        return h + " side"
    if not h:
        return "top middle" if v == "upper" else "bottom middle"
    return f"{v} {h}"


def rel(panel, b):
    s = panel["shape"]
    return ((b["x"] + b["w"] / 2 - s["x"]) / s["w"], (b["y"] + b["h"] / 2 - s["y"]) / s["h"],
            b["w"] * b["h"] / (s["w"] * s["h"]))


def figure_hints(panel):
    out = []
    for f in panel.get("figures", []) or []:
        fx, fy, a = rel(panel, f["bbox"])
        pos = where(fx, fy)
        pos = "in the center of the frame" if pos == "center" else f"at the {pos} of the frame"
        small = ", small in the frame" if a < 0.2 else ""
        out.append(f"{f['name']}: {SIZE.get(f.get('size'), 'shown from the waist up')}{small}, {pos}")
    return out


def keepout(panel):
    """フキダシ・ナレーションの位置（コマの中の場所の名前）。重複は除く。"""
    seen = []
    for b in panel.get("bubbles", []) or []:
        fx, fy, _ = rel(panel, b["bbox"])
        w = where(fx, fy)
        if w != "center" and w not in seen:
            seen.append(w)
    return seen


def keepout_sentence(regions):
    if not regions:
        return ""
    names = " and ".join(regions) if len(regions) <= 2 else ", ".join(regions[:-1]) + " and " + regions[-1]
    return (f"Keep the {names} area of the frame calm and simple, with plain background only "
            "(no faces, people or important objects there).")


def characters(manga):
    """立ち絵が要るキャラ（materials の character と characters の和。コマに出る人だけ）。"""
    used = {f["name"] for pg in manga["pages"] for p in pg["panels"] for f in (p.get("figures") or [])}
    names = []
    for m in manga.get("materials", []) or []:
        if m.get("type") == "character" and m["key"] not in names:
            names.append(m["key"])
    for c in manga.get("characters", []) or []:
        if c["name"] not in names:
            names.append(c["name"])
    return [n for n in names if n in used]


def char_notes(manga, name):
    notes = [c.get("role", "") for c in manga.get("characters", []) or [] if c["name"] == name]
    notes += [m.get("note", "") for m in manga.get("materials", []) or [] if m["key"] == name]
    return " / ".join(n.strip() for n in notes if n)


RULES = """\
You write English prompts for a local image model (FLUX.2-klein, image editing with character reference
sheets) that draws ONE manga panel per image. Speech balloons and all lettering are added later by
another program, so the images must contain no text at all.

Input: a manga name (storyboard) in OMNY YAML (Japanese), plus per-panel hints computed by a script.
Output: JSON only, no prose, no code fence, in exactly this shape:
{
  "style": "<one sentence art style>",
  "characters": {
    "<character name exactly as in the YAML>": {
      "name_en": "<romanized name, e.g. Mio>",
      "noun": "<girl / boy / woman / man / cat / ...>",
      "pronoun": "<her / his / their / its>",
      "appearance": "<one or two sentences: age, hair (style, length, color, any ahoge), eyes, outfit with colors and small details>"
    }
  },
  "panels": {
    "p<page>_<id>": "<the scene description for that panel>"
  }
}

Rules for "style": if meta.format / meta.style_notes ask for no special medium, answer exactly
"Japanese anime illustration, full color, clean crisp line art, cel shading." Otherwise describe the
requested medium and paper in English in one sentence (monochrome manga, watercolor picture book, ...).

Rules for "appearance": use only what the YAML says (characters.role, materials.note); invent missing
concrete details (age, eye color, small outfit details) once, plausibly, and keep them consistent.
Clothing must fit the season and weather in setting.

Rules for each panel description (this is the most important part):
- Start with "Scene: " and describe only what is visible in this one panel, in present tense.
- Use the character's English name, and right after the action restate the appearance in short:
  "<Name>: <appearance>." so every panel carries the same look.
- Follow the framing hint for each character exactly and make it a camera instruction: "shown from the
  waist up" becomes "medium shot cropped at the waist, legs not visible"; "chest up" becomes "cropped
  at the chest"; "small, at the lower left of the frame" stays small.
- Describe every action as a concrete body pose and camera angle, not as a mood: e.g. for stepping
  over a puddle, "seen from the side, mid-jump over a puddle with both feet off the ground"; for
  looking up, "head tilted back, eyes open looking up". Keep the eyes open unless the action says otherwise.
- Be concrete and countable: "exactly one white plastic bag in her right hand; her left hand is empty".
  Say where every held object is. Do not add objects that the action does not mention.
- Weather and time must match setting. If it is after rain, say "the rain has already stopped and no
  rain is falling" in panels that show the sky or the street.
- Clothing prints or icons appear only where the appearance says (e.g. "on the front of the chest
  only"); never put prints on the back.
- Shops, signs and products are fictional: describe shapes and colors, never brand names or logos,
  and signs carry simple icons instead of letters. Shop windows and walls are plain glass or plain
  walls with no posters, no menus and no printed notices (the model turns those into fake letters).
- If the action says something must NOT be shown in this panel (it is revealed later), do not mention
  that thing at all; describe only what the character does (e.g. looking up at the sky out of frame).
- For each area listed under "keep empty", describe a calm plain background that fills it
  (e.g. "calm empty sky at the upper right and a plain wall at the upper left").
- Describe the background concretely according to setting and the bg level hint.
- Never mention speech balloons, dialogue, sound effects, captions, panel borders or Japanese text.
- 50 to 130 words per panel.
"""


def build_request(manga, chars, hints):
    lines = [RULES, "", "== Characters that need a reference sheet =="]
    for n in chars:
        lines.append(f"- {n}: {char_notes(manga, n)}")
    lines += ["", "== Per-panel hints (computed) =="]
    for pid, h in hints.items():
        lines.append(f"- {pid}: bg level {h['bg']} ({h['bg_text'] or 'normal background'}); "
                     f"framing: {'; '.join(h['figures']) or 'no characters'}; "
                     f"keep empty: {', '.join(h['keep']) or 'nothing'}")
    lines += ["", "== OMNY YAML ==", yaml.safe_dump({"manga": manga}, allow_unicode=True, sort_keys=False)]
    return "\n".join(lines)


def ask_llm(cmd, request):
    print(f"  LLM に英語の文面を頼む: {cmd}", flush=True)
    res = subprocess.run(shlex.split(cmd), input=request, capture_output=True, text=True, timeout=900)
    if res.returncode != 0:
        sys.exit(f"ERROR: LLM が失敗した（{res.returncode}）: {res.stderr[-800:]}")
    out = res.stdout
    try:
        return json.loads(out[out.index("{"): out.rindex("}") + 1])
    except ValueError as e:
        sys.exit(f"ERROR: LLM の答えが JSON として読めない: {e}\n{out[:800]}")


def edit_prefix(info):
    """立ち絵を参照させる前置き（試作で効いた文面を一般化したもの）。"""
    if len(info) == 1:
        c = info[0]
        p = c["pronoun"]
        return (f"Draw the {c['noun']} from the reference image as {c['name_en']}, keeping exactly {p} face, "
                f"eye shape, hairstyle and hair color, and {p} outfit. "
                "Do not copy the white background or the standing pose of the reference.")
    who = ", ".join(f"reference image {i + 1} shows {c['name_en']} ({c['noun']})" for i, c in enumerate(info))
    return (f"Use the reference images as character sheets: {who}. Draw each of them keeping exactly their "
            "face, eye shape, hairstyle, hair color and outfit. "
            "Do not copy the white backgrounds or the standing poses of the references.")


def ref_prompt(style, c):
    p = c["pronoun"]
    return (f"{style} Character reference sheet of {c['name_en']}: {c['appearance']} "
            f"Full body, standing straight, facing the viewer, arms relaxed at {p} sides, empty hands, "
            "carrying nothing (no bag, no backpack, no accessories), centered. "
            "Plain pure white background, even soft lighting, no shadows on the background. " + NO_TEXT)


def join(*parts):
    return " ".join(p.strip() for p in parts if p and p.strip())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("omny")
    ap.add_argument("workdir")
    ap.add_argument("--llm", default="claude -p")
    ap.add_argument("--ref", action="append", default=[], help="名前=立ち絵の画像（渡すと立ち絵を描かない）")
    a = ap.parse_args()

    manga = yaml.safe_load(Path(a.omny).read_text())["manga"]
    wd = Path(a.workdir)
    (wd / "prompts").mkdir(parents=True, exist_ok=True)
    given = dict(r.split("=", 1) for r in a.ref)

    chars = characters(manga)
    keys = {n: f"c{i + 1}" for i, n in enumerate(chars)}
    offsets = {}
    so = wd / "seed_offsets.tsv"
    if so.exists():
        for line in so.read_text().splitlines():
            if line.strip():
                k, v = line.split("\t")
                offsets[k] = int(v)

    hints, panels = {}, []
    for pg in manga["pages"]:
        for p in pg["panels"]:
            pid = f"p{pg['page']}_{p['id']}"
            bg = p.get("bg", 1)
            hints[pid] = dict(bg=bg, bg_text=BG.get(bg, ""), figures=figure_hints(p), keep=keepout(p))
            names = []
            for f in p.get("figures", []) or []:
                if f["name"] in keys and f["name"] not in names:
                    names.append(f["name"])
            w, h = panel_px(p["shape"])
            panels.append((pid, w, h, pg["page"] * 100 + p["id"] + offsets.get(pid, 0), names))

    missing = [n for n in chars if n not in given and not (wd / f"prompts/ref_{keys[n]}.txt").exists()]
    missing += [pid for pid, *_ in panels if not (wd / f"prompts/{pid}.txt").exists()]
    meta_f = wd / "prompts/llm.json"  # LLM の答え（キャラの英語名・代名詞は前置きで毎回使う）
    if missing or not meta_f.exists():
        ans = ask_llm(a.llm, build_request(manga, chars, hints))
        meta_f.write_text(json.dumps(ans, ensure_ascii=False, indent=2))
    ans = json.loads(meta_f.read_text())
    style = ans.get("style") or DEFAULT_STYLE
    cinfo = ans.get("characters", {})
    for n in chars:
        if n not in cinfo:
            sys.exit(f"ERROR: LLM の答えにキャラ {n} が無い（{meta_f} を直すか消して再実行）")

    with open(wd / "refs.tsv", "w") as rt:
        for i, n in enumerate(chars):
            k = keys[n]
            f = wd / f"prompts/ref_{k}.txt"
            if not f.exists():
                f.write_text(ref_prompt(style, cinfo[n]))
            png = f"refs/{k}.png"
            if n in given:  # 渡された立ち絵は作業フォルダへ写して、描かずに使う
                (wd / "refs").mkdir(exist_ok=True)
                (wd / png).write_bytes(Path(given[n]).read_bytes())
            rt.write(f"{k}\t{n}\tprompts/ref_{k}.txt\t{png}\t{11 + i}\n")

    with open(wd / "panels.tsv", "w") as pt:
        for pid, w, h, seed, names in panels:
            f = wd / f"prompts/{pid}.txt"
            if not f.exists():
                scene = ans.get("panels", {}).get(pid)
                if not scene:
                    sys.exit(f"ERROR: LLM の答えにコマ {pid} が無い")
                pre = edit_prefix([cinfo[n] for n in names]) if names else ""
                h_ = hints[pid]
                f.write_text(join(pre, style, h_["bg_text"], scene, keepout_sentence(h_["keep"]), NO_TEXT))
            refs = ",".join(f"refs/{keys[n]}.png" for n in names) or "-"
            pt.write(f"{pid}\t{w}\t{h}\t{seed}\t{refs}\tprompts/{pid}.txt\n")
    print(f"  プロンプト: キャラ {len(chars)} 人・コマ {len(panels)} 枚 → {wd / 'prompts'}")


if __name__ == "__main__":
    main()
