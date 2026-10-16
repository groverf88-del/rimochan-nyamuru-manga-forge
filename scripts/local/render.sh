#!/usr/bin/env bash
# ============================================================
# rimochan-nyamuru-manga-forge / scripts/local/render.sh
#   作画マシン（Apple Silicon + mflux）の上で、作業フォルダの中身を絵にする。
#   ① 立ち絵（Z-Image-Turbo）→ ③ コマ（FLUX.2-klein の編集で立ち絵を参照）→ ④ 組版（compose.py）
#   ふつうは forge-local.sh が呼ぶ。描き済みの PNG は飛ばす（描き直しは消してから）。
# ============================================================
#
# 使い方: render.sh <作業フォルダ>
#   作業フォルダには forge-local.sh / prompts.py が作った
#   work.omny.yaml・refs.tsv・panels.tsv・prompts/・render.env が入っている。
#   計測は stats.tsv（段・id・秒・Peak MLX memory の GB）に書く。
# ============================================================
set -uo pipefail

WD="$1"; cd "$WD" || { echo "ERROR: 作業フォルダが無い: $WD" >&2; exit 1; }
TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source ./render.env

expand() { eval "echo $1"; }  # 設定の $HOME や ~ をこのマシンで展開する
BIN="$(expand "$MFLUX_BIN_DIR")"
ZIT="$BIN/mflux-generate-z-image-turbo"
EDIT="$BIN/mflux-generate-flux2-edit"
KLEIN="$(ls -d $(expand "$KLEIN_MODEL") 2>/dev/null | head -1)"
ZIT_MODEL="$(expand "$ZIT_MODEL")"
PY="$(expand "$COMPOSE_PYTHON")"
FACES="$(expand "$FACES_BIN")"
for x in "$ZIT" "$EDIT" "$PY"; do [[ -x "$x" ]] || { echo "ERROR: 見つからない: $x（設定を見直す）" >&2; exit 1; }; done
[[ -d "$KLEIN" ]] || { echo "ERROR: FLUX.2-klein の snapshot が見つからない: $KLEIN_MODEL" >&2; exit 1; }

LOG="render.log"; STATS="stats.tsv"
touch "$STATS"
mkdir -p refs panels pages
FAIL=0

# 研究などの重い計算と取り合わないよう、空きメモリが少ないときは待つ（macOS の memory_pressure）
wait_memory() {
  [[ -z "${MIN_FREE_PCT:-}" ]] && return
  command -v memory_pressure >/dev/null || return
  local n=0 free
  while :; do
    free=$(memory_pressure 2>/dev/null | awk -F': ' '/free percentage/ {gsub("%","",$2); print $2}')
    [[ -z "$free" || "$free" -ge "$MIN_FREE_PCT" ]] && return
    [[ $n -eq 0 ]] && echo "  空きメモリ ${free}% < ${MIN_FREE_PCT}%。空くまで待つ" | tee -a "$LOG"
    n=$((n + 1)); [[ $n -gt 60 ]] && { echo "ERROR: 30 分待っても空かない" | tee -a "$LOG" >&2; exit 1; }
    sleep 30
  done
}

# 1 枚描く: run <段> <id> <出力> <mflux の引数...>。秒と Peak を stats.tsv に残す
run() {
  local stage="$1" id="$2" out="$3"; shift 3
  wait_memory
  echo "== $stage $id $(date +%T)" | tee -a "$LOG"
  local t0=$SECONDS
  "$@" --output "$out" 2>&1 | tr '\r' '\n' >> "$LOG"
  local sec=$((SECONDS - t0))
  local peak; peak=$(grep -E 'Peak MLX memory' "$LOG" | tail -1 | grep -oE '[0-9.]+' | head -1)
  if [[ -f "$out" ]]; then
    printf '%s\t%s\t%s\t%s\n' "$stage" "$id" "$sec" "${peak:-?}" >> "$STATS"
    echo "   ${sec}s / peak ${peak:-?} GB" | tee -a "$LOG"
  else
    echo "   ⚠️ $out ができなかった（$LOG を見る）" | tee -a "$LOG" >&2
    FAIL=$((FAIL + 1))
  fi
}

# ① 立ち絵（渡された立ち絵があれば refs/ に置いてあるので飛ぶ）
while IFS=$'\t' read -r key name prompt png seed; do
  [[ -z "$key" || -f "$png" ]] && continue
  run ref "$key" "$png" "$ZIT" --model "$ZIT_MODEL" --prompt-file "$prompt" \
    --width "${REF_W:-832}" --height "${REF_H:-1216}" --steps "${ZIT_STEPS:-9}" --seed "$seed"
done < refs.tsv

# ③ コマ。立ち絵の出るコマは FLUX.2-klein の編集、人物のいないコマは Z-Image-Turbo
while IFS=$'\t' read -r id w h seed refs prompt; do
  [[ -z "$id" || -f "panels/$id.png" ]] && continue
  if [[ "$refs" == "-" ]]; then
    run panel "$id" "panels/$id.png" "$ZIT" --model "$ZIT_MODEL" --prompt-file "$prompt" \
      --width "$w" --height "$h" --steps "${ZIT_STEPS:-9}" --seed "$seed"
  else
    IMGS=(); IFS=',' read -r -a IMGS <<< "$refs"
    HF_HUB_OFFLINE=1 run panel "$id" "panels/$id.png" "$EDIT" --model "$KLEIN" --base-model flux2-klein-4b \
      --image-paths "${IMGS[@]}" --prompt-file "$prompt" --width "$w" --height "$h" --seed "$seed" \
      --mlx-cache-limit-gb "${CACHE_GB:-8}" --vae-tiling
  fi
done < panels.tsv

# ④ 組版。顔よけの補助（Vision）は swiftc があれば一度だけ作って使い回す
if [[ -n "$FACES" && ! -x "$FACES" ]] && command -v swiftc >/dev/null; then
  mkdir -p "$(dirname "$FACES")"
  swiftc -O "$TOOL/faces.swift" -o "$FACES" >> "$LOG" 2>&1 || echo "  （faces を作れなかった。ネームの人物枠だけで顔よけする）" | tee -a "$LOG"
fi
echo "== compose $(date +%T)" | tee -a "$LOG"
t0=$SECONDS
FACES_BIN="$FACES" "$PY" "$TOOL/compose.py" work.omny.yaml panels pages 2>&1 | tee -a "$LOG"
printf 'compose\tall\t%s\t-\n' "$((SECONDS - t0))" >> "$STATS"

[[ $FAIL -eq 0 ]] || { echo "ERROR: 描けなかった絵が $FAIL 枚ある" >&2; exit 2; }
echo "== render done $(date +%T)" | tee -a "$LOG"
