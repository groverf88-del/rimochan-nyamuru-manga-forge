#!/usr/bin/env bash
# ============================================================
# rimochan-nyamuru-manga-forge / forge-local.sh
#   OMNY（漫画ネームYAML）→ 完成原稿PNG を、手元の Mac の画像生成だけで焼くドライバ。
#   codex（ChatGPT の画像生成）を使う forge.sh の代わり。詳しくは docs/local.md
# ============================================================
#
# 使い方:
#   ./scripts/forge-local.sh -n <OMNYファイル> [オプション]
#
# 流れ（1 回の実行で全部通る。描き済みの物は飛ばすので、途中で止まっても同じ命令で続きから）:
#   ① キャラごとの立ち絵を描く（materials / characters の文から。Z-Image-Turbo）
#   ② 各コマの英語プロンプトを作る（action・figures・setting から。文字なし・フキダシの場所を空ける）
#   ③ 立ち絵を参照して各コマを描く（FLUX.2-klein-4B の編集）
#   ④ コマ割り・フキダシ・縦書きのセリフを貼って原稿にする（compose.py）
#   ②の英語は PROMPT_LLM（既定 claude -p）がこのマシンで書く。①③④は作画マシン（RENDER_HOST）で動く。
#
# 必須:
#   -n, --omny <path>      OMNYファイル（ネームデータ）
#
# オプション:
#   -o, --out <dir>        出力先（既定: ./out/<OMNY名>-local）。作業の途中物もここに残る
#   -c, --config <path>    設定ファイル（既定: config/local.conf。無ければ config/local.conf.example）
#   -r, --ref <名前=画像>  キャラの立ち絵を渡す（そのキャラは立ち絵を描かない）。複数回指定可
#       --redo <id,...>    そのコマだけ seed を変えて描き直す（例: --redo p1_1,p2_1）。
#                          prompts/<id>.txt を手で直してから --redo すると、直した文面で描く
#       --prompts-only     ②まで（英語プロンプトと作業表）で止める。文面を確かめたいとき
#   -h, --help             このヘルプ
#
# 出力:
#   <out>/page1.png ...    完成原稿
#   <out>/refs/ panels/    立ち絵・コマの絵
#   <out>/prompts/         英語プロンプト（手で直してよい。あるものは作り直さない）
#   <out>/stats.tsv        段ごとの秒と Peak MLX memory（GB）
# ============================================================
set -uo pipefail

OMNY=""; OUT=""; CONF=""; REDO=""; PROMPTS_ONLY=0
REFS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--omny)   OMNY="$2"; shift 2;;
    -o|--out)    OUT="$2"; shift 2;;
    -c|--config) CONF="$2"; shift 2;;
    -r|--ref)    REFS+=("$2"); shift 2;;
    --redo)      REDO="$2"; shift 2;;
    --prompts-only) PROMPTS_ONLY=1; shift;;
    -h|--help)   sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) echo "unknown option: $1" >&2; exit 1;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -z "$OMNY" ]] && { echo "ERROR: -n <OMNYファイル> は必須なのだ" >&2; exit 1; }
[[ -f "$OMNY" ]] || { echo "ERROR: OMNYが見つからない: $OMNY" >&2; exit 1; }
if [[ -z "$CONF" ]]; then
  CONF="$HERE/config/local.conf"; [[ -f "$CONF" ]] || CONF="$HERE/config/local.conf.example"
fi
[[ -f "$CONF" ]] || { echo "ERROR: 設定が見つからない: $CONF" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONF"

BASENAME="$(basename "$OMNY")"; BASENAME="${BASENAME%%.*}"
[[ -z "$OUT" ]] && OUT="$HERE/out/$BASENAME-local"
mkdir -p "$OUT"; OUT="$(cd "$OUT" && pwd)"
LOG="$OUT/forge-local.log"
cp "$OMNY" "$OUT/work.omny.yaml"   # 作業フォルダの中は ASCII の名前だけにする
T0=$SECONDS
say() { echo "$*" | tee -a "$LOG"; }
say "=== forge-local start: $BASENAME $(date '+%F %T') / 設定 $(basename "$CONF") / 作画 ${RENDER_HOST:-このマシン} ==="

# --- 描き直し: seed をずらして、そのコマの絵を消す ---
if [[ -n "$REDO" ]]; then
  touch "$OUT/seed_offsets.tsv"
  IFS=',' read -r -a IDS <<< "$REDO"
  for id in "${IDS[@]}"; do
    cur=$(awk -F'\t' -v k="$id" '$1==k {print $2}' "$OUT/seed_offsets.tsv")
    awk -F'\t' -v k="$id" '$1!=k' "$OUT/seed_offsets.tsv" > "$OUT/seed_offsets.tmp"
    printf '%s\t%s\n' "$id" "$(( ${cur:-0} + 1000 ))" >> "$OUT/seed_offsets.tmp"
    mv "$OUT/seed_offsets.tmp" "$OUT/seed_offsets.tsv"
    rm -f "$OUT/panels/$id.png"
    say "  描き直し: $id（seed +$(( ${cur:-0} + 1000 ))）"
  done
fi

# --- ② 英語プロンプトと作業表（このマシン） ---
PARGS=("$OUT/work.omny.yaml" "$OUT" --llm "$PROMPT_LLM")
for r in "${REFS[@]:-}"; do [[ -n "$r" ]] && PARGS+=(--ref "$r"); done
t=$SECONDS
python3 "$HERE/scripts/local/prompts.py" "${PARGS[@]}" 2>&1 | tee -a "$LOG"
[[ ${PIPESTATUS[0]} -eq 0 ]] || { say "ERROR: プロンプト作りで止まった"; exit 1; }
printf 'prompts\tall\t%s\t-\n' "$((SECONDS - t))" > "$OUT/stats.prompts.tsv"
[[ $PROMPTS_ONLY -eq 1 ]] && { say "=== --prompts-only: $OUT/prompts で止める ==="; exit 0; }

# --- 作画マシンへ渡す設定（作画マシン側で展開する値はそのまま書く） ---
{
  for k in MFLUX_BIN_DIR KLEIN_MODEL ZIT_MODEL ZIT_STEPS COMPOSE_PYTHON FACES_BIN CACHE_GB MIN_FREE_PCT; do
    printf "%s='%s'\n" "$k" "${!k:-}"
  done
} > "$OUT/render.env"
mkdir -p "$OUT/tool"
cp "$HERE/scripts/local/render.sh" "$HERE/scripts/local/compose.py" "$HERE/scripts/local/faces.swift" "$OUT/tool/"
rm -f "$OUT"/page*.png "$OUT/stats.tsv"
export COPYFILE_DISABLE=1  # macOS の tar に ._ ファイルを混ぜさせない

# --- ①③④ 作画と組版 ---
STATUS=0
if [[ -z "${RENDER_HOST:-}" ]]; then
  bash "$OUT/tool/render.sh" "$OUT" 2>&1 | tee -a "$LOG"; STATUS=${PIPESTATUS[0]}
else
  # shellcheck disable=SC2086
  RSSH() { ssh $SSH_OPTS "$RENDER_HOST" "$@"; }
  RUN="run-$(date +%Y%m%d-%H%M%S)-$$"
  RDIR="$RENDER_ROOT/$RUN"
  say "  作画マシンへ送る: $RENDER_HOST:$RDIR"
  # 送るのは作業に要る物だけ（描き済みの絵も送り、描き直しを省く）
  ( cd "$OUT" && tar cf - work.omny.yaml refs.tsv panels.tsv render.env prompts tool \
      $( [[ -d refs ]] && echo refs ) $( [[ -d panels ]] && echo panels ) ) \
    | RSSH "mkdir -p $RDIR && tar xf - -C $RDIR" || { say "ERROR: 作画マシンへ送れなかった"; exit 1; }
  RSSH "bash $RDIR/tool/render.sh $RDIR" < /dev/null 2>&1 | tee -a "$LOG"; STATUS=${PIPESTATUS[0]}
  say "  回収する"
  RSSH "cd $RDIR && tar cf - refs panels pages stats.tsv render.log 2>/dev/null" < /dev/null | ( cd "$OUT" && tar xf - )
  if [[ "${KEEP_REMOTE:-0}" == "1" ]]; then
    say "  作画マシンの作業フォルダを残した: $RDIR"
  else
    RSSH "rm -rf $RDIR" < /dev/null && say "  作画マシンの作業フォルダを消した"
  fi
fi
[[ -f "$OUT/pages/page1.png" ]] && mv "$OUT"/pages/page*.png "$OUT/"
rmdir "$OUT/pages" 2>/dev/null

# --- まとめ ---
cat "$OUT/stats.prompts.tsv" >> "$OUT/stats.tsv"; rm -f "$OUT/stats.prompts.tsv"
say "=== forge-local done: $((SECONDS - T0)) 秒（全体）==="
awk -F'\t' '{n[$1]++; s[$1]+=$3; if ($4+0 > p[$1]) p[$1]=$4+0}
  END {for (k in n) printf "   %-8s %2d 件 %5d 秒  peak %s GB\n", k, n[k], s[k], (p[k] ? p[k] : "-")}' "$OUT/stats.tsv" | tee -a "$LOG"
ls "$OUT"/page*.png 2>/dev/null | sed 's/^/   /' | tee -a "$LOG"
[[ $STATUS -eq 0 ]] || { say "⚠️ 作画で失敗があった（$OUT/render.log）"; exit 2; }
