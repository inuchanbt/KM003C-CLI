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

Ctrl+C までキャプチャする場合：

```powershell
.\.venv\Scripts\python.exe km003c_cli.py capture --until-ctrl-c --out-prefix captures/pd02
```

PD 応答に含まれる電圧・電流も保存するには `--scope`、その入力転送データも残すには `--scope-raw` を追加します。

```powershell
.\.venv\Scripts\python.exe km003c_cli.py capture --seconds 10 --scope --scope-raw --out-prefix captures/session01
```

キャプチャは既定で USB vendor interface 0 を使います。Windows ではこのインターフェースを WinUSB で利用できる必要があります。キャプチャから電圧要求や充電プロトコルへの移行を自動実行することはありません。出力フォルダは自動作成し、同名ファイルは上書きします。Ctrl+C で停止しても、それまでのデータを保存します。`--out-prefix` は既存拡張子を置き換えるため、拡張子なしの名前を使ってください。

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
| `capture` | `--seconds` / `--until-ctrl-c`、`--out-prefix`、`--scope`、`--scope-raw`、`--quiet`、`--interval`、`--allow-framing-errors`、`--gui-csv` |
| `export-gui` / `decode` | `--records`、`--out-prefix`、`--scope`、`--allow-framing-errors` |
| `adc` | `--count`、`--interval`、`--csv`、`--jsonl`、`--instant` |

全オプションは `<コマンド> --help` で確認できます。`capture` は常に Utility CSV を生成するため、`--gui-csv` は互換用の指定です。`--scope-raw` には `--scope` が必要です。ADC は既定でメーターの平均済み VBUS/IBUS を使い、`--instant` で瞬時値へ切り替えます。

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
| `.csv` | PD / 接続イベント。CY4500 Utility と同じ 15 列、UTF-8 |
| `.records.jsonl` | 生フレーム、イベント・測定値、未知データ、デコードエラー |
| `.records.bin` | 長さ付き KM003C 応答フレーム |
| `.records.hex.txt` | フレームの HEX 表示 |
| `.xfers.bin` | 長さ付き受信転送データ |
| `.scope.csv` | CY4500 と同じ 19 列の測定 CSV、UTF-8 BOM 付き。`--scope` 指定時 |
| `.scope.xfers.bin` | 測定プリアンブルを含む転送データ。`.xfers.bin` と同じ入力。`--scope-raw` 指定時 |
| `.summary.txt` | 件数、メッセージ別集計、終了状態 |
| `.metadata.json` | 接続条件、データ形式、時刻の出所、精度、未観測項目 |

Live-status CSV は CY4500 と同じ 18 列です。単独の `scope` は `<CSV 名>.metadata.json` も保存します。オフライン変換は CSV、records JSONL、metadata、summary と、指定時の scope CSV を生成します。CY4500 の `.ccgx3` は生成しません。

`0x05` など未知の PD イベントフラグが来ても、キャプチャ・オフライン変換は停止しません。`UNKNOWN_PD_EVENT_0xNN` として表示し、その論理ペイロードの残りを `.records.jsonl` のイベントの `raw_hex` に保持して、次の論理パケット・応答から解析を再開します。未知部分の中でイベント境界を推測しません。CSV の時刻は PD ステータスプリアンブルの観測時刻で、JSONL の `timestamp_source: pd_status_preamble_ms` に出所を記録します。サマリー・メタデータには未知イベント件数を記録します。既知形式のデータ欠落は引き続き framing error とし、`--allow-framing-errors` を指定すると生データを残して読み飛ばします。

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

CDC 経由でメーカーの ASCII コマンドを送信し、出力電圧やプロトコル状態を変更する操作です。`--dry-run` は実際に送らず内容を表示します。`--wait` は応答を読む秒数、`--response-file` は応答バイトの保存先です。送信成功だけでネゴシエーション成功とは判断できません。トリガー操作は dry-run で検証し、実機の電圧変更は行っていません。

```powershell
.\.venv\Scripts\python.exe km003c_cli.py pdm open --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pdm set --type 2 --em 2 --sink 1 --port COM3
.\.venv\Scripts\python.exe km003c_cli.py entry pd --port COM3
.\.venv\Scripts\python.exe km003c_cli.py pd --pdo --port COM3
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
| `pdm` | `open` / `close` / `set`。`set`：`--type` 0–3、`--em` 0–2、`--sink` 0/1 |
| `entry` | `pd`、`ufcs`、`qc`、`fcp`、`scp`、`afc`、`vfcp`、`sfcp`、`bc`、`apple`、`list`、`list+` |
| `pd` | `--pdo`、`--req`、`--cmd`、`--data`、`--drp` のいずれか。要求パラメータは `--volt`、`--cur` |
| `ufcs` | `--pdo`、`--req`、`--cmd` のいずれか。要求時は `--volt` と `--cur` が必須 |
| `qc` | `--voltage` 5/9/12/20 |
| `fcp`、`afc`、`sfcp` | `--voltage` 5/9/12 |
| `qc3` | `--volt`（3600–20000、200 刻み）、`--inc`、`--dec` のいずれか |
| `scp`、`vfcp` | `--volt`、`--cur` |
| `reset` | トリガーモジュールをリセット |

`--voltage` は V、`--volt` は mV、`--cur` はメーカー資料の電流パラメータを整数で送ります。PD の固定 PDO は `--volt` を省略できます。対応する電圧・電流は接続先とファームウェアに依存します。`pd --data` は SOP + 2-byte little-endian ヘッダー + 4-byte オブジェクトを受け取り、CRC は付けません。MessageID や役割はデバイスが書き換える場合があります。各コマンドは個別実行し、PDM 開始・プロトコル移行・電圧要求を自動で連続実行しません。

## 構成と検証

```text
km003c_cli.py          CLI 起動ファイルと出力処理
km003c_modules/       プロトコル解析と USB/HID/CDC 接続処理
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
# ezpd_protocol.py があるフォルダを指定すると、参照互換性テスト 2 件も実行。
$env:CY4500_CLI_ROOT = 'C:\path\to\cy4500-cli\CLI'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

合成 PD パケット・ADC の例示バイト列を使い、分割受信、時刻折り返し、符号・単位、再変換、エラー時の生データ保持、ASCII コマンド生成を検証します。参照テストは CY4500 の列構成と読み取り専用 CSV ローダーを使い、参照ソース未指定時はスキップします。

2026-10-02 の実機確認では USB/HID/CDC の ADC 読み取り、USB PD キャプチャ、単独の HID scope、オフライン再変換を確認しました。検証した個体では HID の PD-only 要求に応答がありませんでした。新 CDC ストリーム・電子負荷・実際のトリガーや電圧変更は未検証です。

実装にはメーカーのインターフェース・CDC・PDM 資料と、公開の [KM003C protocol research](https://github.com/okhsunrog/km003c-protocol-research/blob/main/docs/protocol_reference.md) / [PD event format](https://github.com/okhsunrog/km003c-protocol-research/blob/main/docs/features/pd_analysis.md) を参照しています。公開解析は主に firmware V1.9.9 に基づくため、未知データは生バイトを保持します。ライブラリ資料：[HIDAPI](https://trezor.github.io/cython-hidapi/api.html)、[pySerial](https://pyserial.readthedocs.io/en/latest/pyserial_api.html)。

## ライセンス

[MIT](LICENSE)。メーカー資料はこのリポジトリに含めません。
