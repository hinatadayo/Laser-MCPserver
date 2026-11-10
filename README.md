# etcher-laser-mcp

Claude Code などの MCP クライアントから **Etcher Laser Pro**（SmartDIYs のダイオードレーザー加工機）を直接操作する MCP サーバーと、
KiCad のペースト層ガーバーから **はんだペースト用ステンシル（カプトンフィルム）を切る**ためのツール一式です。

> English: an MCP server for the Etcher Laser Pro (GRBL 1.2.1 over FTDI USB serial, 921600 bps) plus tools that turn a
> KiCad paste-layer Gerber into laser G-code for cutting solder-paste stencils from Kapton film.

**注意:** レーザーを出す操作を含みます。保護メガネ・換気・目視での監視は必須です。非公式のツールで、メーカーとは関係ありません。
下の切断条件は筆者の機械（カプトン 0.1 mm、3 mm MDF 上、焦点を手で合わせた状態）での試し切り結果です。**自分の環境では必ず試し切りで確認してください。**

## 仕組み

この機種は付属ソフトの同梱ライブラリ（`RDCAM.dll`）ではなく、**FTDI USB シリアル上の GRBL 1.2.1** で動いています。
そのため、このサーバーは `pyserial` で COM ポートに直接 G-code を送ります。

- 通信: FTDI（VID 0403 / PID 6001）、**921600 bps**
- 電源投入直後は XON（`0x11`）を受け取るまで無応答。接続時に自動で送る
- 電源投入直後の機械座標は常に 0,0。位置を信用するには `$H` が必要
- 公式ソフト（SmartDIYsCreator）とは同時に使えない（COM ポートの取り合い）。裏に残っていないか注意

## セットアップ

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
claude mcp add --scope user etcher-laser -- "<このフォルダ>/.venv/Scripts/python" "<このフォルダ>/server.py"
```

環境変数: `ETCHER_PORT`（例 `COM12`。省略時は自動検出）、`ETCHER_BAUD`（既定 921600）

動作確認（機械は動かない）: `python selftest.py`

## ツール

| ツール | 動き | 内容 |
|---|---|---|
| `laser_connect` / `laser_disconnect` | なし | 接続・切断 |
| `laser_status` / `laser_settings` | なし | 状態・座標・`$$` 設定 |
| `laser_analyze_gcode` | なし | G-code の範囲・最大 S を確認 |
| `laser_stencil_from_gerber` | なし | ペースト層ガーバー → ステンシル G-code（位置は現在のヘッド位置など） |
| `laser_stencil_from_3mf` | なし | スライサーで配置した 3MF → ステンシル G-code |
| `laser_home` | ヘッド移動 | `$H` 原点復帰 |
| `laser_unlock` | なし | `$X` アラーム解除 |
| `laser_jog` / `laser_go_origin` | ヘッド移動（レーザー OFF） | ジョグ |
| `laser_frame` / `laser_frame_gcode` | ヘッド移動（レーザー OFF） | 枠表示 |
| `laser_run_gcode` | **レーザー照射** | G-code 送信。`confirm=true` 必須 |
| `laser_pause` / `laser_resume` | — | `!` / `~` |
| `laser_stop` | 停止 | `!` → `0x18` |
| `laser_send` | 内容次第 | 1 行送信。M3/M4 を含むと `confirm=true` 必須 |

照射前の範囲チェックがあり、可動域（450 × 310 mm）の外に出る指示は拒否されます。
AI に操作させる場合は、**照射のたびにユーザー本人の明示的な承認を取る**運用を前提にしています（サーバーの `instructions` にも明記）。

## ステンシルを切る手順

1. KiCad のペースト層ガーバー（`*-F_Paste.gtp` など）を用意する
2. 焦点を手で合わせる（この機種はオートフォーカスではない。`laser_jog` でヘッドを材料の上へ → 高さ調整）
3. `laser_jog` でヘッドを「ステンシルの左上にしたい位置」へ動かす
4. `laser_stencil_from_gerber` でその位置を左上にして G-code を作る
5. `laser_frame_gcode` で枠表示（レーザー OFF）し、フィルム上の位置を目で確認する
6. 承認のうえ `laser_run_gcode`

スライサー（OrcaSlicer / Bambu Studio）で配置したい場合は、`gerber2stencil.py` で STL を作り、スライサーで配置して 3MF で保存し、
`laser_stencil_from_3mf` で変換します。スライサーのベッドは造形範囲を機械の可動域（0,0 〜 450×310）に合わせてください。
3MF に保存されたベッド範囲を読み取り、`機械X = x − ベッドxmin`、`機械Y = ベッドymax − y` に変換します
（機械の原点は左奥で +Y が手前、スライサーは通常 +Y が奥のため）。G-code は機械座標（`G53`）で書くので、加工機に保存された作業原点には依存しません。

### 切り方の工夫（カプトン 0.1 mm で得られた知見）

- 幅 0.45 mm 未満、または隣の穴まで 0.3 mm 未満の穴（USB-C、TSSOP）は、**長手方向の中心線 1 本**で切る。
  輪郭をなぞると 0.2 mm の穴の間が焼け落ちる（`--slot-below` で変更）
- 中心線: 出力 80 % / 3800 mm/min / 8 回（M4）
- 輪郭（0402、タクトスイッチなど）: 片側 0.125 mm 内側 / 2200 mm/min / 4 回（M4）
  - 3800 mm/min では小さな四角で速度が乗らず、M4 が出力を絞って貫通しなかった
  - M3（一定出力）にすると角が焼けて大穴になった
  - 1500 mm/min × 4 回・0.05 mm 寄せでは 0402 の 2 パッドがつながった
- 全部の穴を 1 周ずつ重ねて回し、隣り合う穴は続けて切らない（熱をためない）
- 出力と速度は焦点が合っているかで大きく変わる。焦点が甘い状態の試し切りは参考にならない

試し切り用の生成スクリプト:

- `make_testgrid.py`: 出力 × 速度（または回数）のグリッド
- `make_finetest.py`: ガーバーの USB-C / TSSOP パッドを使った細ピッチの試し切り（輪郭 / 中心線）
- `make_0402test.py`: ガーバーの 0402 パッドだけを使った試し切り

## ファイル

| ファイル | 内容 |
|---|---|
| `server.py` | MCP サーバー本体 |
| `grbl.py` | GRBL ドライバ（接続・状態取得・ストリーミング送信）。冒頭に観測したプロトコルのメモ |
| `gerber2stencil.py` | ペースト層ガーバー → STL / SVG。穴数と断面ループ数を照合して書き出し、未対応の Gerber 命令はエラーで止まる |
| `stencil3mf.py` | 3MF / ガーバー → レーザー G-code（中心線・輪郭の自動切り替え） |
| `runjob.py` | 原点復帰 → 枠表示 → 照射 → 原点復帰をまとめて実行（**照射する**） |
| `framejob.py` | 原点復帰 → 枠表示のみ |
| `selftest.py` / `diag.py` / `baudprobe.py` | 接続確認・生シリアル診断・ボーレート探索（機械は動かない） |
| `motiontest.py` | 原点復帰 → 枠表示 → 停止の動作確認（ヘッドが動く。レーザーは OFF） |

## ライセンス・クレジット

MIT License（`LICENSE`）。

ステンシルの元にした基板データ（flatbox）は別ライセンス（CC BY-SA 4.0、jfedor2/flatbox）です。このリポジトリには含めていません。
