# デモ実行マニュアル

実行コマンド(実機 / ドライラン)とヘルプのみをまとめたもの。詳細は `docs/demo_scripts.md`。

- 既定は **ドライラン**(ロボットにコマンドを送らず、送る内容を表示するだけ)。
- 実機へ送るのは `--execute` を付けたときだけ(**ロボットが実際に動く**)。
- `-i` はネットワークインターフェース(例: `Ethenet`, `en7`)。以下は `Ethenet` で記載。
- `pixi run <task>` の後ろの引数はそのままスクリプトへ渡される。

---

## 1. ドライラン(ロボットは動かない)

### 通し

```bash
pixi run demo --source videohub -i Ethenet      # phase1 はロボットの映像を使う(ロボットは動かない)
pixi run demo --no-dashboard                 # ダッシュボードを起動しない
pixi run demo --from grasp --to release      # 途中から / 途中まで
pixi run demo --skip approach                # 特定フェーズを飛ばす
```

### フェーズ単体

```bash
pixi run demo-approach
pixi run demo-read-qr --source videohub -i Ethenet   # 実機の映像でQR読取(ロボットは動かない)
pixi run demo-grasp
pixi run demo-move
pixi run demo-release
```

phase1 は `--execute` を持たず、常にロボットへ動作コマンドを送らない(映像を受信するだけ)。PC のカメラで試す場合は `--source webcam`(既定)。

前フェーズが未完了でも単体確認したいときは `--force` を付ける。

```bash
pixi run demo-grasp --force
```

---

## 2. 実機(ロボットが動く)

事前確認: ロボットと有線接続(dev PC `192.168.123.164/24`、ロボット `192.168.123.161`)/ 周囲の安全確保 / 非常停止を手元に用意。

### 通し

```bash
pixi run demo --source videohub -i Ethenet --execute
pixi run demo --from grasp --to release -i Ethenet --execute    # 途中から(状態JSONを引き継ぐ)
```

### フェーズ単体

```bash
pixi run demo-approach -i Ethenet --execute                     # 未実装(スキップ扱い)
pixi run demo-read-qr  --source videohub -i Ethenet             # 実機の映像でQR読取(--execute なし。ロボットは動かさない)
pixi run demo-grasp    -i Ethenet --execute                     # 腕で把持(プリセット)
pixi run demo-move     -i Ethenet --execute                     # 固定位置へ移動
pixi run demo-move     --track --source videohub -i Ethenet --execute   # 区画マーカーを追って移動
pixi run demo-release  -i Ethenet --execute                     # 解放
```

主なオプション例:

```bash
pixi run demo-grasp   -i Ethenet --execute --arm-mode custom --custom-grab grasp_box
pixi run demo-release -i Ethenet --execute --gesture wave
```

---

## 3. ヘルプ

```bash
pixi run demo --help
pixi run demo-approach --help
pixi run demo-read-qr --help
pixi run demo-grasp --help
pixi run demo-move --help
pixi run demo-release --help
```

### 主な共通オプション(phase0 / 2 / 3 / 4)

| オプション | 内容 |
|---|---|
| `--execute` | 実機へコマンドを送る(無ければ dry-run) |
| `-i, --interface` | ネットワークインターフェース |
| `-d, --domain` | DDS ドメインID |
| `--force` | 前フェーズ未完了でも実行 |
| `--state` | 状態JSONのパス(既定 `logs/state.json`) |
| `--log` | イベントログのパス(既定 `logs/events.jsonl`) |

### `run_demo.py` 専用オプション

| オプション | 内容 |
|---|---|
| `--from <phase>` / `--to <phase>` | 実行範囲(`approach, read_qr, grasp, move, release`) |
| `--skip <phase>` | 飛ばすフェーズ(複数指定可) |
| `--no-dashboard` | ダッシュボードを同時起動しない |

上記以外の引数(`--source` / `-i` / `--execute` / `--zone` など)は各フェーズへ渡される。
