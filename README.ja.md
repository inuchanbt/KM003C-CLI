# KM003C CLI

日本語 | [English](README.md)

POWER-Z KM003C の測定・USB-PD キャプチャ・急速充電トリガー用 Python CLI です。[cy4500-cli](https://github.com/inuchanbt/cy4500-cli) の共通コマンドラインオプションと CSV の列名・列順に合わせています。KM003C にない機能は省き、KM003C 固有の操作を追加しています。

## インストール

Python 3.10 以上が必要です。以下は Windows / PowerShell の例です。

```powershell
git clone https://github.com/inuchanbt/KM003C-CLI.git
cd KM003C-CLI
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe km003c_cli.py --help
.\.venv\Scripts\python.exe km003c_cli.py usb-info
```

`km003c_cli.py` と `km003c_modules/` は同じフォルダに置いてください。オフライン変換・テスト・`--dry-run` は Python 標準ライブラリだけで動作します。実機接続では接続方法に応じて `hidapi`、`libusb1`、`pyserial` を使います。

## まず PD をキャプチャする

KM003C の USB データ・制御用インターフェースを PC へ接続し、測定対象の USB-C 接続にメーターを挿入します。10 秒間キャプチャするには次を実行します。

```powershell
.\.venv\Scripts\python.exe km003c_cli.py capture --seconds 10 --out-prefix captures/pd01
```

Ctrl+C までキャプチャする場合（既定動作）：

```powershell
.\.venv\Scripts\python.exe km003c_cli.py capture --until-ctrl-c --out-prefix captures/pd02
```

`--seconds` を省略すると Ctrl+C まで取得します。`--out-prefix` を省略すると `captures/km003c_<ローカルの年月日_時分秒_マイクロ秒>` を生成します。CSV・`.ccgx3`・元データのログ・測定値は既定で保存します。scope・ccgx3 有効、GoodCRC 表示省略、解析・VBUS 推定・scope 生転送保存・quiet・上書きは無効です。状態表示は既定で 1 秒ごと（`--status-interval 1`）で、`--quiet` はイベント・状態表示を省きます。単独 `scope` の既定の取得時間は 5 秒を維持します。`--scope` / `--ccgx3` は既定動作の明示指定です。測定の入力転送データも残すには `--scope-raw` を追加します。

```powershell
.\.venv\Scripts\python.exe km003c_cli.py capture --seconds 10 --scope --scope-raw --out-prefix captures/session01
```

キャプチャは既定で USB vendor interface 0 を使います。Windows ではこのインターフェースを WinUSB で利用できる必要があります。キャプチャから電圧要求や充電プロトコルへの移行を自動実行することはありません。出力フォルダは自動作成します。既存ファイルとの衝突は実機接続前に拒否し、上書きには `--force` が必要です。Ctrl+C で停止しても、それまでのデータを保存します。`--out-prefix` は既存拡張子を置き換えるため、拡張子なしの名前を使ってください。

## EZ-PD Protocol Analyzer Utility 4.2.0 用出力

PD の `.csv` は **File > Import**、`.ccgx3` セッションは **File > Open** で読み込みます。PD の行を選ぶと詳細と、測定有効時には電圧・電流・CC の波形を表示します。CSV の Import は PD 一覧だけなので、波形には ccgx3 を使います。生成に Java やメーカーのファイルは不要です。

```powershell
# CY4500 形式の明示指定。CSV と ccgx3 は既定で有効。
python km003c_cli.py capture --until-ctrl-c --scope --ccgx3 --out-prefix captures/ezpd01

# 波形とセッションを省き、CSV・元データのログを保存。
python km003c_cli.py capture --seconds 10 --no-scope --no-ccgx3 --out-prefix captures/pd_only

# TI 形式の出力選択。
python km003c_cli.py capture --seconds 10 --formats all --out-prefix captures/all01
python km003c_cli.py capture --seconds 10 --formats csv ccgx3 --out-prefix captures/gui_only

# 既存の KM003C 記録を、測定値も含めてオフライン変換。
python km003c_cli.py export-gui --records captures/session01.records.bin --out-prefix captures/ezpd_converted
python km003c_cli.py convert --input captures/session01.records.bin --out-prefix captures/ezpd_converted --force
```

`capture` と `export-gui` / `convert` / `decode` は `--scope` / `--no-scope`、`--ccgx3` / `--no-ccgx3`、`--formats all|original|csv|ccgx3`（複数指定可）、`--force`、互換用の `--gui-csv` に対応します。既定は全形式・測定有効です。`--[no-]ccgx3` の明示指定は `--formats` の選択に優先します。`--formats original` は KM003C の JSONL・生データと scope CSV を選び、CSV・ccgx3 は個別に選択します。metadata と summary は常に保存します。オフライン入力は `--records` / `--input` で KM003C の長さ付きフレームを指定し、CY4500 records や TI `.pda` は入力できません。

GUI CSV・ccgx3 は実際に観測した PD メッセージを `SOP`、`SOP_PRIME`、`SOP_DPRIME` と `v1` / `v2` / `v3` の表記で保存します。PD ヘッダーのない状態・未知イベントと未検証の SOP 種別は GUI ファイルから除外し、metadata に件数を記録します。**既定の original 形式の JSONL・生データには元のバイト列を含めて残します。** GoodCRC は保存します。GUI 形式だけを選ぶと元データのログは生成しません。

実際に観測した PD メッセージでは、KM003C に CRC / EOP 判定や物理的な通信時間がないため、`Ok`、`Duration`、`Delta` は空欄、開始・終了は同じ観測時刻です。セッションの `pktData` は GUI デコーダー用に組み立てたデータで、OK / CRC / EOP ビットを立てず、CY4500 の実キャプチャとしては扱いません。波形の `GraphData` は物理値を整数 mV / mA に丸めて保存し、電流の符号も保持します。元の精度の値は scope CSV・JSONL に残します。VBUS は unsigned 16-bit の mV（上限 65.535 V）、CC・電流は signed 16-bit です。範囲外の値は範囲内に収め、`ccgx3_graph_clipped` に件数を記録します。メーカー版 4.2.0 の GUI は 32.767 V を超える VBUS を負数として表示しますが、EPR 改造版 Build 155 は正しく表示します。GUI の軸範囲によって負電流が見えない場合があります。測定点の補間・追加はせず、KM003C の 1 ms 分解能・ポーリング頻度を保持します。

### GoodCRC 表示と VBUS イベント推定

`capture --hide-goodcrc` は `GOODCRC` のコンソール表示だけを省きます。CY4500/TI と同じく既定で有効です。`--show-goodcrc` で表示し、`--quiet` で全イベントの表示を省きます。選択した保存形式には GoodCRC を残し、未知イベントや framing error はこのフィルターで隠しません。KM003C は CRC / EOP / idle error 判定を提供しないため、CY4500 の「正常な GoodCRC だけを隠す」という区別までは再現できません。

```powershell
python km003c_cli.py capture --until-ctrl-c --hide-goodcrc --infer-vbus-events --out-prefix captures/vbus01
python km003c_cli.py capture --seconds 10 --show-goodcrc --out-prefix captures/goodcrc01
python km003c_cli.py convert --input captures/session01.records.bin --infer-vbus-events --out-prefix captures/vbus_converted
```

`--infer-vbus-events` は**既定で無効**です。capture とオフライン変換で使え、測定（既定で有効な `--scope`）を必要とします。デバイス時刻付きの PD ステータスプリアンブルだけから、TI と同じしきい値で推定します。低状態を確認した後の 4,000 mV 以上で `VBUS_UP`、高状態を確認した後の 800 mV 以下で `VBUS_DN` を生成します。中間の電圧では状態を保持します。初回の測定では生成せず、100,000 µs を超える測定間隔・同一時刻・時刻逆行では基準をリセットして、その測定点からイベントを生成しません。

時刻はしきい値を超えた最初の測定点で、1 ms のデバイス分解能とポーリング間隔（既定 40 ms）による不確かさがあります。KM003C のハードウェアイベントではなくソフトウェア推定で、測定点の間の変化は見逃す場合があります。コンソールには `[VBUS inferred]` と表示します。選択した GUI 出力には電圧イベント行（`Ok=VBUS_UP/DN`、Utility の `VOLT_PKT` 形式）を追加し、PD メッセージは捏造しません。GUI の行だけでは実機 VBUS イベントと区別できないため、共有時は `.vbus_events.jsonl` と metadata も添付してください。この JSONL は `--formats original` でも必ず生成し、`estimated`・出所・しきい値・測定区間・GUI 行との対応を記録します。metadata の `vbus_event_inference` はしきい値・件数・間隔リセット数を記録し、`gui_pd_messages` は推定を除外、`gui_rows` は推定行を含めます。生フレーム・元の JSONL・測定点・実機 PD 件数は変更しません。

### AVS 遷移解析

```powershell
# Ctrl+C まで取得。出力名は captures/km003c_<日時> を自動生成。
python km003c_cli.py capture --analyze-transitions

# 10 秒で終了。VBUS UP/DN 推定は独立した任意機能。
python km003c_cli.py capture --seconds 10 --analyze-transitions --infer-vbus-events --out-prefix captures/avs01

# 保存済みの生データを、実機なしで再解析。入力は変更しない。
python km003c_cli.py convert --input captures/avs01.records.bin --analyze-transitions --out-prefix captures/avs_reanalyzed

# 同じ KM003C キャプチャの PD/scope CSV を CY4500 と同じ形式で解析。
python km003c_cli.py analyze-sync --pd-csv captures/avs01.csv --scope-csv captures/avs01.scope.csv --out-prefix captures/avs_csv
```

`--analyze-transitions` は既定で無効、scope が必要です。取得終了時（Ctrl+C を含む）またはオフライン変換後に解析します。RDO と選択された EPR AVS PDO を含む `EPR_REQUEST` だけを対象とし、次の要求までの SOP ACCEPT/PS_RDY を対応付け、基準電圧・変化方向と開始点・目標電圧通過・目標帯への整定・観測した平坦部・平均スルーレートを推定します。固定電圧/PPS/SPR-AVS 要求は対象外です。空データや AVS 要求がない記録でも、列名付きのレポートと対象なしの説明を生成します。解析用入力を一時ファイルへ保存するため、どの `--formats` 選択でも使え、生データ・測定値は変更しません。`analyze-sync` には同じ取得のデバイス時刻付き PD/scope CSV が必要です。単独 `scope` のホスト時刻 CSV は対応付けできません。

出力は CY4500 と同じ列名・順序の `.transitions.csv`、`.transitions.txt`、`.transition_summary.csv`、`.transition_summary.txt` です。capture・変換の metadata に設定・解析状態・件数・要求番号の基準を記録します。単独 analyze-sync はテキストレポートに設定を記録します。要約では要求目標と観測した平坦部を別々に示します。CSV の flags に KM003C の点時刻・離散測定・データ不足・中断/失敗を記録します。デバイス分解能は 1 ms、既定ポーリングは 40 ms なので、遅延やスルーレートは推定値であり、速い変化は見逃す場合があります。物理的な USB-PD タイミング規格適合の判定には使えません。測定点や時刻オフセットは捏造しません。長い空きや重複時刻では連続した変化・整定の判定を区切り、安定したデータが足りなければ平坦部・整定の値は空欄にします。要求/PS_RDY の近傍測定が許容間隔より遠ければ、対応電圧を未取得とします。

共通のしきい値・オプション名は CY4500 に揃え、測定頻度に依存する既定値は次のようにします。

| オプション | KM003C の既定値 | CY4500 の既定値 |
| --- | --- | --- |
| `--movement-sustain-samples` | 2 | 5 |
| `--settle-hold-ms` / `--observed-settle-hold-ms` | 80 | 20 |
| `--settle-max-gap-ms` | 100 | 5 |
| `--plateau-lookback-ms` | 400 | 150 |
| `--plateau-min-samples` | 6 | 50 |

その他の既定値は、基準窓/直前除外 100/5 ms、変化しきい値 0.05 V・MAD 係数 6、目標帯 ±1%、観測帯 ±0.5%、平坦部の許容幅 max(0.10 V, 0.25%)、目標との妥当性範囲 10% です。すべて `--help` にある CY4500 と同名のオプションで変更できます。解析設定を変えても機器のポーリングや測定設定は変更しません。

## 測定とオフライン変換

```powershell
# 電圧・電流を表示。volt-amp は同じコマンドの別名。
.\.venv\Scripts\python.exe km003c_cli.py live-status --count 20 --interval 0.1 --median 5 --csv captures/live.csv

# 5 秒間の ADC 測定。
.\.venv\Scripts\python.exe km003c_cli.py scope --seconds 5 --csv captures/scope.csv --raw captures/scope.xfers.bin

# D+/D-/VDD と温度の生値を含む詳細 ADC。
.\.venv\Scripts\python.exe km003c_cli.py adc --count 10 --csv captures/adc.csv --jsonl captures/adc.jsonl

# 実機を接続せず、保存済み KM003C データを変換。decode は別名。
.\.venv\Scripts\python.exe km003c_cli.py export-gui --records captures/session01.records.bin --out-prefix captures/converted01 --scope
```

| コマンド | 主なオプション |
| --- | --- |
| `usb-info` | `--json`、デバイス列挙 |
| `live-status` / `volt-amp` | `--count`、`--interval`、`--median`、`--csv`、`--instant` |
| `scope` | `--seconds` / `--until-ctrl-c`、`--csv`、`--raw`、`--quiet`、`--interval`、`--instant`、`--stream`、`--rate` |
| `capture` | `--seconds` / `--until-ctrl-c`、`--out-prefix`、`--scope` / `--no-scope`、`--scope-raw`、`--ccgx3` / `--no-ccgx3`、`--formats`、`--force`、`--quiet`、`--hide-goodcrc` / `--show-goodcrc`、`--infer-vbus-events`、`--analyze-transitions`、`--status-interval`、`--interval`、`--allow-framing-errors`、`--gui-csv` |
| `export-gui` / `convert` / `decode` | `--records` / `--input`、`--out-prefix`、`--scope` / `--no-scope`、`--ccgx3` / `--no-ccgx3`、`--formats`、`--force`、`--infer-vbus-events`、`--analyze-transitions`、`--allow-framing-errors` |
| `analyze-sync` | `--pd-csv`、`--scope-csv`、`--out-prefix`、`--force`、遷移解析オプション |
| `adc` | `--count`、`--interval`、`--csv`、`--jsonl`、`--instant` |

全オプションは `<コマンド> --help` で確認できます。`capture` は既定で Utility CSV を生成し、`--gui-csv` は互換用の指定です。`--scope-raw` には `--scope` が必要です。ADC は既定でメーターの平均済み VBUS/IBUS を使い、`--instant` で瞬時値へ切り替えます。

## 接続方法

接続オプションはコマンドの後に指定します。`--transport auto|hid|usb|cdc`、`--serial`、`--vid`、`--pid`、`--timeout`、`--port` / `-p` / `--com`、`--baud` に対応します。HID には `--hid-path` もあります。既定値は VID `0x5FC9`、PID `0x0063`、I/O タイムアウト 2 秒、115200 baud です。

- `auto` はキャプチャに USB interface 0、ADC 測定に HID interface 3 を使います。`--port` を指定すると CDC を使います。
- USB は `libusb1` を使います。Windows では **vendor interface 0** を WinUSB で利用できるようにし、HID と仮想 COM は利用可能な状態に保ちます。
- HID は `hidapi` を使います。候補が複数ある場合は `usb-info`、`--serial`、`--hid-path` で選択します。
- ASCII トリガー操作と新 CDC ストリームは仮想 COM を使います。一致する KM003C が 1 台ならポート指定を省略できます。以下の `COM3` は例示です。

## 出力ファイルと CY4500 互換性

`capture --out-prefix captures/session01 --scope --scope-raw` は次を生成します。

| 拡張子 | 内容 |
| --- | --- |
| `.csv` | PD メッセージ。CY4500 Utility と同じ 15 列、UTF-8 |
| `.ccgx3` | PD と波形の Java シリアライズリストを含む EZ-PD 4.2 セッション ZIP |
| `.records.jsonl` | 生フレーム、イベント・測定値、未知データ、デコードエラー |
| `.records.bin` | 長さ付き KM003C 応答フレーム |
| `.records.hex.txt` | フレームの HEX 表示 |
| `.xfers.bin` | 長さ付き受信転送データ |
| `.scope.csv` | CY4500 と同じ 19 列の測定 CSV、UTF-8 BOM 付き。`--scope` 指定時 |
| `.scope.xfers.bin` | 測定プリアンブルを含む転送データ。`.xfers.bin` と同じ入力。`--scope-raw` 指定時 |
| `.summary.txt` | 件数、メッセージ別集計、終了状態 |
| `.metadata.json` | 接続条件、データ形式、時刻の出所、精度、未観測項目 |
| `.transitions.csv` / `.transitions.txt` | CY4500 と同形式の AVS 解析。`--analyze-transitions` 指定時 |
| `.transition_summary.csv` / `.transition_summary.txt` | 遷移ごとの読みやすい要約。解析時だけ生成 |
| `.vbus_events.jsonl` | ソフトウェア VBUS 推定と根拠の測定区間。`--infer-vbus-events` 指定時だけ生成 |

Live-status CSV は CY4500 と同じ 18 列です。単独の `scope` は `<CSV 名>.metadata.json` も保存します。オフライン変換は選択した CSV・ccgx3・records JSONL・scope CSV と metadata・summary を生成し、入力の生バイナリは書き換えません。セッションの波形は保存済みの測定プリアンブルから再構成します。

`0x05` など未知の PD イベントフラグが来ても、キャプチャ・オフライン変換は停止しません。コンソール・JSONL では `UNKNOWN_PD_EVENT_0xNN` として表示し、残りの論理ペイロードを `raw_hex` に保持します。境界を推測せず次の論理パケット・応答から再開します。PD ヘッダーがないため GUI CSV・ccgx3 からは除外します。JSONL の時刻の出所は `pd_status_preamble_ms` です。サマリー・メタデータには未知イベント・GUI 除外件数を記録します。既知形式のデータ欠落は framing error とし、`--allow-framing-errors` 指定時は original 形式に生データを残して読み飛ばします。

### 単位と時刻

- **PD CSV の `Vbus(V)` は整数の mV** です。現行 CY4500 Utility 形式に合わせています。Scope CSV の `Vbus(V)` と Live CSV の `vbus_V` は V です。
- Scope の Raw 列は KM003C の単位です。VBUS は µV、IBUS は µA。CC は ADC で 0.1 mV/count、PD ステータス・新 CDC で 1 mV/count です。CY4500 の ADC 換算式は適用しないでください。電流の符号は受信値を保持します。
- `capture --scope` は PD 応答の 12-byte ステータスプリアンブルを読みます。デバイス時刻は **1 ms 分解能**で、µs に換算し、32-bit / 24-bit の折り返しを処理します。既定の `--interval 0.04` は約 25 回/秒のポーリングです。CY4500 の高速波形測定と同じ精度ではありません。
- PD イベントは一点の観測値です。`Start Time` と `End Time` に同じ時刻を格納します。物理的な開始・終了時刻や CRC / EOP 判定がないため、`Duration`、`Delta`、`Ok` は空欄です。この表現は既存の CY4500 CSV 読み込み処理で読めます。
- 単独の `scope` は実行開始からのホスト受信時刻を使います。ADC にデバイス時刻がないため、`Timestamp Raw` は空欄です。別実行の scope と capture は時刻原点が一致しません。
- 生バイナリは **4-byte little-endian 長さ + KM003C の生バイト列**の繰り返しです。CY4500 の 64-byte records とは異なります。この CLI の `export-gui` / `decode` で変換してください。
- 温度は `temp_raw` として保持します。提供されたメーカー資料で単位が未定義のため、換算しません。

## KM003C 固有の操作

### 新 CDC の連続 ADC

```powershell
.\.venv\Scripts\python.exe km003c_cli.py scope --port COM3 --stream --rate 50 --seconds 5 --csv captures/cdc.csv --raw captures/cdc.xfers.bin
```

`--rate` は 4 / 10 / 50 / 1000。省略時はメーターの設定を使います。`02` / `02 rate` で開始し、終了時は `03`、30 分ごとに `01` を送信して資料記載の 1 時間停止を防ぎます。20-byte ADC サンプルをまとめて受信する形式に対応します。

資料に `Time` の単位と CRC8 計算法がないため、CRC は未検証で保持し、Scope はホスト受信時刻を使います。同じバッチのサンプルは同じ時刻になります。資料の一例には宣言長と実データ長の不一致があります。非対応フレームではデコードを停止するため、生データを残すには `--raw` を指定してください。**新 CDC ストリームは実機未検証です。**

### 急速充電トリガー

CDC 経由でメーカーの ASCII コマンドを送信し、出力電圧やプロトコル状態を変更する操作です。 メーカーの SSCOM 例に合わせ、CR/LF を付けずに1コマンドずつ送ります。改行なしで PDM 設定を明示した PD3.1 初期化と PDO 取得は実機確認済みです。再初期化時の機器状態・設定も影響するため、改行の修正だけで準備完了を保証するわけではありません。`--dry-run` は実際に送らず内容を表示します。`--wait` は応答を読む秒数、`--response-file` は応答バイトの保存先です。送信成功だけでネゴシエーション成功とは判断できません。

**電源の PDO 取得には `pdo --port COM14` を使ってください。** COM ポートを一度だけ開き、sweep と共通の `pdm open` → PDM 設定 → `entry pd` → `pd pdo` を実行します。busy 時の再起動、起動待ち、ready 待ちも共有します。これらを別々の CLI プロセスで実行すると毎回 COM を開き直し、今回の接続では最初のコマンド以降に応答が返らないことが報告されました。専用コマンドは取得から終了処理まで同じ接続を維持します。既定は PD3.1／EPR e-marker／5 A Sink 能力（`type=2,em=2,sink=1`）です。取得中は外部電子負荷を OFF または低電流にしてください。

既定では初回の PDO 問い合わせ後、20 V 超の PDO が現れるまで最大 `--entry-timeout` 秒追加で問い合わせます。EPR の AVS 範囲、または 20 V 超の固定 PDO を検出すれば待機を終了します。SPR の PDO だけ取得できた場合は警告とともに最後の有効な応答を表示し、有効な PDO 応答が一度もなければエラー終了します。`--no-epr` は追加の EPR 待ちを省き、最初の有効な応答を表示します。PD3.0／PPS 設定にするなら `pdo --type 1 --em 1`。EPR 以外の e-marker 設定でも EPR 待ちは省きます。`--wait`、`--entry-timeout`、`--pdm-startup-wait` の既定値は sweep と共通です。掃引の電圧要求は送らず、ADC 接続も開きません。正常終了・Ctrl+C・エラー時は同じ COM 接続で `reset` → `pdm close` を行ってから接続を閉じます。`--keep-trigger` を明示した場合だけ後処理を省きます。ADC を使わないため、後処理の応答確認は電圧復帰の実測確認ではありません。

`--quiet` でも最終 PDO 応答を表示します。`--response-file captures/source_pdo.txt` は応答の生バイトを保存し、`--force` で上書きできます。`--dry-run` は手順表示だけです。表示行は予約 PDO 番号を省略する場合があるため、自動で番号を振り直しません。既存の `pd --pdo` は初期化なしの単発問い合わせです。初期化から取得する場合は `pdo` を使ってください。

```powershell
.\.venv\Scripts\python.exe km003c_cli.py pdo --port COM14
.\.venv\Scripts\python.exe km003c_cli.py pd --req 2 --cur 3000 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pd --req 5 --volt 12000 --cur 3000 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pd --cmd 18 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pd --data 008F1201A800FF --dry-run
.\.venv\Scripts\python.exe km003c_cli.py pd --drp --port COM3
.\.venv\Scripts\python.exe km003c_cli.py qc --voltage 9 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py qc3 --volt 5000 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py qc3 --inc 8 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py ufcs --pdo --port COM3
.\.venv\Scripts\python.exe km003c_cli.py ufcs --req 1 --volt 11000 --cur 4000 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py reset --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pdm close --port COM3
```

| コマンド | オプション / 値 |
| --- | --- |
| `pdo` | 同じ COM 接続で初期化から PDO 取得まで実行。`--no-epr`、`--type`、`--em`、`--sink`、待機時間、`--response-file`、`--force`、`--keep-trigger` |
| `pdm` | `open` / `close` / `set`。`set`：`--type` 0–3、`--em` 0–2、`--sink` 0/1 |
| `entry` | `pd`、`ufcs`、`qc`、`fcp`、`scp`、`afc`、`vfcp`、`sfcp`、`bc`、`apple`、`list`、`list+` |
| `pd` | `--pdo`、`--req`、`--cmd`、`--data`、`--drp` のいずれか。要求パラメータは `--volt`、`--cur` |
| `ufcs` | `--pdo`、`--req`、`--cmd` のいずれか。要求時は `--volt` と `--cur` が必須 |
| `qc` | `--voltage` 5/9/12/20 |
| `fcp`、`afc`、`sfcp` | `--voltage` 5/9/12 |
| `qc3` | `--volt`（3600–20000、200 刻み）、`--inc`、`--dec` のいずれか |
| `scp`、`vfcp` | `--volt`、`--cur` |
| `reset` | トリガーモジュールをリセット |

`--voltage` は V、`--volt` は mV、`--cur` はメーカー資料の電流パラメータを整数で送ります。PD の固定 PDO は `--volt` を省略できます。対応する電圧・電流は接続先とファームウェアに依存します。`pd --data` は SOP + 2-byte little-endian ヘッダー + 4-byte オブジェクトを受け取り、CRC は付けません。MessageID や役割はデバイスが書き換える場合があります。上記の単発コマンドは個別に実行します。以下の掃引コマンドは専用の初期化手順を持ちます。

## PPS／AVS 電圧掃引（ASD 形式）

`asd_pd31_cli.py` と同じ `--mode avs --sweep start:end:step[:current]`、`--pps-sweep`、`--round-trip-sweep` を使えます。電圧は **V**、電流は **A**。オプションから始める書き方に加え、`sweep` サブコマンドと `load` エイリアスも使えます。

```powershell
# AVS：15 → 48 → 15 V、1 V 刻み、要求 5 A、67 点
# --pdo-index は接続先の PPS/AVS PDO 番号に合わせて変更
python km003c_cli.py --port COM3 --mode avs --sweep 15:48:1:5 --pdo-index 11 --continuous-sweep --round-trip-sweep --apdo-voltage-hold 2 --csv captures/avs_sweep.csv

# PPS：5 → 21 → 5 V、要求 3 A
python km003c_cli.py --port COM3 --pps-sweep 5:21:1:3 --pdo-index 6 --continuous-sweep --round-trip-sweep --apdo-voltage-hold 2 --csv captures/pps_sweep.csv

# 実機に接続せず、全要求と往復順序を確認
python km003c_cli.py --mode avs --sweep 15:48:1:5 --pdo-index 11 --round-trip-sweep --dry-run
```

AVS の初期化は `pdm open` → `pdm set type=2,em=2,sink=1` → `entry pd` → `pd pdo` の順です。PPS は `type=1,em=1,sink=1` を使います。`pdm open` が `pdm busy` を返した場合は、トリガーを終了・再起動してから設定します。この操作はネゴシエーションをやり直します。`entry pd` の `ready` 応答を最大10秒待ち、受信後すぐに先へ進みます。掃引前に PDO を別途取得する必要はありません。 20 V 超の AVS 掃引では、最初の ready が SPR の準備完了だけを示す場合があります。掃引範囲をカバーする AVS 能力が pd pdo に現れるまで、さらに最大 --entry-timeout 秒待ちます。PDO 番号の自動選択や要求電流の検証は行いません。`--entry-timeout` で初期化待ち時間を変更でき、各電圧要求の待ち時間には影響しません。`ready` が返らない場合は電源・CC 接続と PDM 設定を確認してください。すでに準備済みなら `--no-initialize`。`--type`、`--em`、`--sink` で設定を変更でき、`--em 0` で e-marker 模擬を無効にできます。`--no-initialize` では設定送信と busy 時の復帰処理も省略します。

CDC の読み取り中は COM 設定を固定します。以前の処理は読み取りごとにタイムアウトを設定し、Windows が COM 状態を再適用することで、確認した構成では PDM 動作を妨げていました。測定用インターフェースは PD 初期化後に開きます。応答中のバイナリ PDO は画面ではエスケープ表示し、生バイトはメタデータに保持します。COM14 で PD3.1 初期化・AVS 15～48 V／240 W の能力取得・HID ADC 読み取りの併用を確認しました。電圧掃引そのものは未検証です。

KM003C では **`--pdo-index` が必要**です。メーカー資料に `pd pdo` の機械処理用の応答形式がないため、PDO の自動選択や指定番号・要求電流の検証は行いません。20 V 超の AVS 掃引は、範囲をカバーする AVS 能力の取得を待ってから開始します。既存の `pd --pdo` で接続先を確認し、範囲全体をカバーする PPS/AVS PDO を選んでください。Fixed PDO ではメーカー仕様上 `volt` が無視されるため、掃引には使えません。実際の要求は `pd req=N,volt=mV,cur=mA` に変換します。

| オプション | 動作・既定値 |
| --- | --- |
| `--sweep start:end:step[:current]` | AVS。電流省略時は `--request-current` が必要 |
| `--pps-sweep start:end:step[:current]` | PPS。電流省略時は 1 A |
| `--request-current` | A。式の電流を上書き |
| `--round-trip-sweep` | 往復。折り返し点は重複しない。下降は負の step |
| `--continuous-sweep` | PDO・要求電流を維持し、各点で ADC を測定 |
| `--continuous-settle` | 各要求後の測定前待ち。既定 0.5 秒 |
| `--apdo-voltage-hold` | 各点の最低保持時間。既定 0 秒。応答待ち・測定時間を含む |
| `--measure` / `--measure-loop` | 1 回 / 指定回数の ADC 読み取り。既定 off / 0。continuous 時は最低 1 回 |
| `--delay` | 測定前の待ち。既定 0.5 秒。continuous の初回は continuous-settle を使う |
| `--wait` | entry pd 以外の KM 固有 ASCII 応答読み取り時間。既定 1 秒 |
| `--entry-timeout` | 初期化時の entry pd ready 待ち上限。既定 10 秒 |
| `--pdm-startup-wait` | PDM 起動時の読み取り時間の下限。既定 2 秒。--wait も考慮 |
| `--type` / `--em` / `--sink` | PDM のプロトコル・e-marker 模擬・Sink 能力を変更。AVS の既定は 2/2/1、PPS は 1/1/1 |
| `--measurement-transport` | ADC 読み取り用の HID（既定）/ USB。トリガーの CDC 接続と併用 |
| `--csv` / `--no-csv` | 省略時は日時入りの captures/km003c_*_sweep_*.csv を作成 / 保存なし |
| `--force` / `--csv-overwrite` / `--csv-append` | 前二つは指定 CSV とメタデータを上書き。append は列を検証して追記し、既存メタデータを保持。既定は既存ファイルを拒否 |
| `--source-name` / `--cable-name` / `--test-note` | ASD 形式の記録用ラベル |
| `--keep-trigger` | 終了後も最後の PD 要求を維持。既定は無効（reset + pdm close） |
| `--pause-before-sweep` | 初期化・ADC 接続準備後、最初の電圧要求前に Enter 待ち。既定は無効 |
| `--quiet` / `--dry-run` | 進捗表示を省略 / 全計画のみ表示 |

PDO 取得中は外部負荷を OFF または低電流にし、本測定の直前に設定したい場合は `--pause-before-sweep` を追加してください。初期化（EPR 能力取得の待機を含む）と ADC 接続の準備が完了した後、最初の掃引要求を送る前に停止します。電子負荷を CC 5 A など目的の電流に設定し、Enter で開始します。`--quiet` でも待機案内は表示します。待機中の Ctrl+C や入力 EOF でも既存の終了処理を実行し、掃引要求は送りません。`--no-initialize` の場合も最初の要求前に停止します。`--dry-run` では停止位置の表示のみで、入力待ちはしません。`--force` は `--csv-overwrite` と同じ指定で、`--csv` が必要です。指定 CSV と `<CSV>.metadata.json` を両方上書きします。

```powershell
# 低電流／無負荷で PDO を取得 → 待機中に CC 5 A に設定 → Enter で開始。既存ログは上書き
python km003c_cli.py --port COM14 --mode avs --sweep 15:48:1:5 --pdo-index 11 --continuous-sweep --round-trip-sweep --apdo-voltage-hold 2 --pause-before-sweep --csv captures/avs07.csv --force
```

`--continuous-sweep` は **KM003C の電子負荷を制御しません**。式の電流は PD 要求電流です。実際の消費電流は接続した外部負荷で決まります。ASD の電子負荷 ON/OFF、初回の負荷電流ランプ、プリチェック関連のオプションは、このコマンドには含めません。

CSV は ASD と共通の `target_voltage_v`、`request_current_a`、`actual_voltage_v`、`actual_current_a`、`sweep_leg`、`sweep_pass` などに、KM の要求文・応答生バイト・状態を追加した**掃引用の列構成**です。電子負荷の目標値は空欄で、ASD の全 CSV 列との完全一致ではありません。`capture` の EZ-PD 用 CSV とは別形式です。メタデータは `<CSV>.metadata.json`、追記時は既存情報を残すため `<CSV>.run_<日時>.metadata.json` に保存します。

正常終了・Ctrl+C・エラー時は、接続を閉じる前に `reset` → `pdm close` を送ります。実機では `pdm close` 単独で24 Vが残る場合があり、その後の reset/close 試験では15 Vの AVS 要求から約5.12 Vへ戻ることを確認しました。この試験時の外部負荷電流はほぼ0 Aです。各後処理コマンドの応答待ちは2秒です。後処理中の追加 Ctrl+C は一時的に保留し、終了後に元のハンドラーを戻します。応答生バイト・失敗はメタデータの `cleanup` に別途記録し、元の掃引エラーを置き換えません。正常完了した掃引でも後処理に失敗すればエラー終了します。測定用接続がある場合は最後に ADC を1回読み、5.5 V以下か確認します。この測定は掃引 CSV の行に加えず、メタデータに保存します。測定用接続がない場合はコマンド応答のみを記録し、電圧復帰の実測確認はしません。`--keep-trigger` を明示した場合だけ最後の要求を維持します。既定は無効です。**外部電子負荷の ON/OFF や電流設定は変更しません。**空の応答は成功扱いにせず、CSV は `sent_unverified` と記録します。行頭の error / failed / false / reject 応答では停止し、その他の応答形式は未確認として保持します。ADC は保持中の測定値で、PD 遷移波形の時間解析には `capture` を使ってください。**掃引の電圧変更と CDC＋USB ADC の併用は実機未検証です。CDC の初期化・PDO 取得と HID ADC の併用は確認済みです。**

## 構成と検証

```text
km003c_cli.py          CLI 起動ファイルと出力処理
km003c_modules/       プロトコル解析、USB/HID/CDC 接続処理、GUI セッション出力
tests/                オフラインのプロトコル・接続・互換性テスト
README.md             英語の説明
README.ja.md          日本語の説明
requirements.txt      実機接続ライブラリ
LICENSE               MIT ライセンス
```

メーカーの説明ファイル・デモバイナリと測定ファイルは**配布しません**。ローカル専用の `local/`、`data/`、`captures/` と、測定出力の拡張子を Git の対象から除外しています。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 任意：別途取得した CY4500 ソースのうち、cy4500_cli.py と
# ezpd_protocol.py があるフォルダを指定すると、参照互換性テスト 3 件も実行。
$env:CY4500_CLI_ROOT = 'C:\path\to\cy4500-cli\CLI'
# 任意：ASD の掃引経路・既定値・共通列を比較。
$env:ASD_PD31_CLI_ROOT = 'D:\ASD-PD31\src\current'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

合成 PD パケット・ADC の例示バイト列を使い、分割受信、時刻折り返し、符号・単位、再変換、エラー時の生データ保持、ASCII コマンド生成、キャプチャの既定動作、AVS 遷移解析を検証します。参照テストは CY4500 の列構成・読み取り専用 CSV ローダー・解析結果と比較し、参照ソース未指定時はスキップします。

2026-10-07 に ASD/CY 参照チェックを含む 86 件のテストが通りました。追加の掃引テストは往復順序、十進刻み、共通設定、ドライラン、中断・拒否・ADC 失敗時の生データ保持、CSV の衝突防止と追記を検証します。

2026-10-06 に CY4500 参照チェックを含む 65 件のテストが通りました。ローカルの EZ-PD 4.2.0 Build 155（EPR 改造版 v1.0p）で CSV Import、ccgx3 Open、PD 詳細、波形を確認しました。メーカー版 4.2.0 の CSV パーサーも既存キャプチャの PD 1,881 件をすべて受理し、メーカーの Java クラスでセッションの PD 1,881 件・波形 5,674 点を読み込めました。全波形の値は整数 mV / mA の丸め幅以内で scope CSV と一致しました。追加の合成データでは、推定 UP/DN の CSV Import・ccgx3 Open・行選択と波形位置の一致も確認しました。メーカー版パーサーは全 4 行を受理し、Java クラスは 4 行・6 測定点を読み込めました。検証用の測定データ・メーカーのクラスは Git に含めません。

2026-10-02 の実機確認では USB/HID/CDC の ADC 読み取り、USB PD キャプチャ、単独の HID scope、オフライン再変換を確認しました。検証した個体では HID の PD-only 要求に応答がありませんでした。新 CDC ストリーム・電子負荷・実際のトリガーや電圧変更は未検証です。

実装にはメーカーのインターフェース・CDC・PDM 資料と、公開の [KM003C protocol research](https://github.com/okhsunrog/km003c-protocol-research/blob/main/docs/protocol_reference.md) / [PD event format](https://github.com/okhsunrog/km003c-protocol-research/blob/main/docs/features/pd_analysis.md) を参照しています。公開解析は主に firmware V1.9.9 に基づくため、未知データは生バイトを保持します。ライブラリ資料：[HIDAPI](https://trezor.github.io/cython-hidapi/api.html)、[pySerial](https://pyserial.readthedocs.io/en/latest/pyserial_api.html)。

## ライセンス

[MIT](LICENSE)。メーカー資料はこのリポジトリに含めません。

Java シリアライズ形式は MIT ライセンスの [CY4500 CLI](https://github.com/inuchanbt/cy4500-cli) と同系列の TI CLI に基づきます。メーカーのクラスはローカルでの互換性検証にだけ使い、配布しません。

AVS 遷移解析・レポート形式は MIT ライセンスの CY4500 CLI（copyright 2026 inuchanbt）に基づき、KM003C の時刻・測定頻度と、測定の空きを考慮する判定へ適応しています。
