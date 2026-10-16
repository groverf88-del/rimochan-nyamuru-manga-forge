# ローカル作画（forge-local.sh）

codex（ChatGPT の画像生成）を使わず、**手元の Apple Silicon の Mac だけで**ネームを原稿にします。
`forge.sh` の代わりに `scripts/forge-local.sh` を使います。ネーム（OMNY）はそのまま同じ物が使えます。

```bash
./scripts/forge-local.sh -n examples/sample-2page.omny.yaml
# → out/sample-2page-local/page1.png, page2.png
```

同梱のサンプルネームをこのやり方で焼いた例（[`examples/output-local/`](../examples/output-local/)）：

<p align="center">
  <img src="../examples/output-local/page1.png" alt="ローカル作画の出力 1ページ目" width="49%">
  <img src="../examples/output-local/page2.png" alt="ローカル作画の出力 2ページ目" width="49%">
</p>

1 回の実行（Mac Studio で約 6.5 分・Peak MLX memory 7.0GB）のあと、1 ページ目の 2 コマ目だけ
`prompts/p1_2.txt` を「真横から・両足が浮いて水たまりを跳ぶ」と具体的に直して `--redo p1_2` した（約 1 分）。

## forge.sh との違い

| | forge.sh（codex） | forge-local.sh（ローカル） |
|---|---|---|
| 1 枚の単位 | 1 ページ丸ごと（文字もフキダシも AI が描く） | **1 コマずつ**（絵だけ。文字は描かせない） |
| セリフ・フキダシ・コマ割り | 画像生成 AI が描く（誤字は後で人が直す） | `compose.py` が**ネームどおりに後から貼る**（誤字が出ない） |
| キャラの一貫性 | 同じ会話を続けて保つ | **立ち絵を毎コマ参照**して保つ（FLUX.2-klein の編集） |
| お金 | 画像生成が使えるプラン | かからない（電気代だけ） |
| 速さ | 1 ページ 1〜2 分 | 1 コマ約 1 分（1 ページ 3 コマで約 3 分） |
| 苦手 | 文字のゆらぎ | 細部の指示違い（持ち物の数・「小さく」など）。コマ単位で描き直す |

## 流れ

1 回の実行で ①〜④ が全部通ります。描き済みの物は飛ばすので、途中で止まっても同じ命令で続きからやり直せます。

```
OMNY
 ├─ ② prompts.py      各コマとキャラの英語プロンプトを作る（このマシン・LLM）
 ├─ ① 立ち絵          materials / characters の文から 1 人 1 枚（Z-Image-Turbo）     ┐
 ├─ ③ コマ            立ち絵を参照して 1 コマずつ（FLUX.2-klein-4B の編集）         ├ 作画マシン
 └─ ④ compose.py      コマ割り・枠線・フキダシ・縦書きのセリフを貼る               ┘
page1.png ... pageN.png
```

### ② 英語プロンプトの作り方

英語の文面（場面とキャラの見た目）だけを LLM に書かせ、残りはスクリプトが決めます。

- **スクリプトが決める物**: コマの画素数（コマの縦横比で約 100 万画素）・seed（ページ×100＋コマ番号）・
  背景の描き込み量（`bg`）・人物の写る範囲と位置（`figures` の `size` と `bbox`）・
  **フキダシのために空ける場所**（`bubbles` の `bbox` から「右上と左上を無地に」）・
  「参照の子として描く」前置き・「文字・ロゴ・フキダシを描かない」念押し
- **LLM が書く物**: キャラの英語名と見た目（`characters.role` と `materials.note` から）、各コマの場面（`action` と `setting` から）。
  依頼文に、試作で効いた書き方を規則として入れてある：
  持ち物は数と手を言い切る（「右手に袋を 1 つ・左手は空」）、雨上がりなら「雨は止んでいて降っていない」、
  服の柄は前だけ、店や看板は架空でロゴなし、次のページで見せる物はそのコマで描かない、など

プロンプトは `out/<作品>-local/prompts/` に残ります。**あるファイルは作り直さない**ので、
手で直して `--redo` すれば直した文面で描き直せます。

### 描き直し

```bash
# p1_1 と p2_1 だけ、seed を変えて描き直す（直した prompts/p1_1.txt があればそれで）
./scripts/forge-local.sh -n examples/sample-2page.omny.yaml --redo p1_1,p2_1
```

文言を具体的にして seed を 2〜3 回振ると、たいてい直ります。「小さく描く」だけは文言では効きにくいです。

### 自分の立ち絵を使う

```bash
./scripts/forge-local.sh -n my.omny.yaml -r 'ミオ=chars/mio.png'
```

名前はネームの `figures.name` と同じにします。渡したキャラは立ち絵を描きません。

## 準備

### 呼ぶ側のマシン（forge-local.sh を動かす所）

- `python3` と PyYAML（②のプロンプト作り）
- `PROMPT_LLM` に書く LLM のコマンド（既定 `claude -p`。ログインしておく）
- 作画マシンが別なら、そこへ鍵で ssh できること

### 作画マシン（Apple Silicon の Mac）

1. [mflux](https://github.com/filipstrand/mflux)（0.22.0 で確認）を入れる: `uv tool install mflux`
2. モデルを一度だけ取ってくる（どちらも 4bit）
   - `Runpod/FLUX.2-klein-4B-mflux-4bit`（コマ。立ち絵を参照する編集）
   - `filipstrand/Z-Image-Turbo-mflux-4bit`（立ち絵と、人物のいないコマ）
3. 組版に Pillow と PyYAML が要る。mflux の uv tool の python に両方入っているので、それを使う
4. `swiftc`（Xcode の Command Line Tools）があれば、顔よけの補助（Vision）を自動で作って使う

メモリは 1 コマで約 6GB（Peak MLX memory。64GB の Mac Studio で計測）。

### 設定

```bash
cp config/local.conf.example config/local.conf
```

`config/local.conf` は `.gitignore` 済みです。主な項目：

| 項目 | 中身 |
|---|---|
| `PROMPT_LLM` | ②で英語を書く LLM のコマンド（標準入力→標準出力）。既定 `claude -p` |
| `RENDER_HOST` / `SSH_OPTS` | 別の Mac で描くときの ssh の行き先と踏み台。空ならこのマシンで描く |
| `RENDER_ROOT` / `KEEP_REMOTE` | 作画マシンの作業場所。既定は回収したら消す |
| `MFLUX_BIN_DIR` | mflux の実行ファイルの場所（uv tool なら `$HOME/.local/bin`） |
| `KLEIN_MODEL` | FLUX.2-klein の **HF snapshot の実パス**（glob 可） |
| `ZIT_MODEL` | Z-Image-Turbo（repo 名か実パス） |
| `COMPOSE_PYTHON` | 組版の python（mflux の uv tool の python） |
| `MIN_FREE_PCT` | 空きメモリがこの % 未満なら空くまで待つ |

作画マシン側で展開したい値（`$HOME` など）は `'…'` で囲んで書きます。

## ハマりどころ

- **FLUX.2-klein を HF の repo 名で渡すと、offline でトークナイザの解決に失敗する** → snapshot の実パスを `--model` に、
  `--base-model flux2-klein-4b`、`HF_HUB_OFFLINE=1`（render.sh がやる）
- Qwen-Image-2.1 の q4 は text-only の書き出しで、編集（参照）には使えない
- アニメ絵では Vision の顔検出がほとんど効かない。フキダシの顔よけは**ネームの人物枠**が主で、Vision は補助
- 縦書きは Pillow の raqm 無しでも組めるよう自前で組んでいる。書体は macOS の游明朝・ヒラギノ・筑紫B丸ゴを探す
- 出力の `stats.tsv` に段ごとの秒と Peak MLX memory が残る
