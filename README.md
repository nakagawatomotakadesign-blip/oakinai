# 大商いスクリーナー

米国株の売買代金（株価×出来高）5日平均 上位150 を毎朝 6:30 JST に自動更新し、GitHub Pages で公開します。

## 構成
```
.github/workflows/screen.yml   毎日 21:30 UTC（=06:30 JST）に実行
scripts/screen.py              データ取得・計算・JSON出力
scripts/industry_ja.json       Nasdaq 業種 → 日本語 の対応表
scripts/theme_overrides.json   ティッカー単位の「テーマ反映」上書き（ASML→半導体製造装置 など）
scripts/schedule_report.py     schedule 起動の遅れを集計（手元で実行: python scripts/schedule_report.py --since YYYY-MM-DD）
docs/index.html                フロントエンド（静的・JSONを読むだけ）
docs/data/YYYY-MM-DD.json      日次結果（履歴として蓄積）
cache/daily/YYYY-MM-DD.parquet 日足キャッシュ（1日1ファイル・RS計算用）
```

## セットアップ（初回のみ・10分）
1. このフォルダを GitHub の新規リポジトリに push する（public / private どちらでも可）
2. https://polygon.io で無料アカウントを作り、API キーを取得
3. リポジトリの Settings → Secrets and variables → Actions → New repository secret
   `POLYGON_API_KEY` = 取得したキー
4. Settings → Pages → Source を **GitHub Actions** にする
5. Actions タブ → `daily-screen` → Run workflow → `backfill_days` に **270** を入れて実行
   （無料枠は 5 リクエスト/分のため約 55 分かかります。1回だけ）
6. 完了後 `https://<ユーザー名>.github.io/<リポジトリ名>/` で表示

以後は毎朝 6:30 JST に自動実行され、直近分だけ差分取得します（数十秒で終わります）。

## ローカルで試す
```
pip install -r requirements.txt
POLYGON_API_KEY=xxx BACKFILL_DAYS=10 python scripts/screen.py   # RSなしで動作確認
cd docs && python -m http.server 8000                             # http://localhost:8000
```

## 指標の定義
- 売買代金5日平均: 直近5営業日の 終値×出来高 の平均
- 規模: 全普通株を時価総額で順位付け。大型=1〜150位、中型=151〜750位、小型=751位以下（`screen.py` の `LARGE_CUT` / `MID_CUT` で変更可）
- 騰落: 当日終値 vs 前日終値
- RelVol: 当日出来高 ÷ 過去63営業日の平均出来高
- RS Rating: 3ヶ月リターン×0.4 + 6ヶ月×0.2 + 9ヶ月×0.2 + 12ヶ月×0.2 を全銘柄でパーセンタイル化（1〜99）。キャッシュが252日未満の間は「—」
- 業種RS: 業種ごとの RS 中央値を業種間でパーセンタイル化

## カスタマイズ
- 業種の日本語名: `scripts/industry_ja.json`
- テーマ反映（特定銘柄の業種を上書き）: `scripts/theme_overrides.json`
- 実行時刻: `screen.yml` の cron（UTC）。JST 6:37 = UTC 21:37 前日。毎時00分・30分は GitHub 側が混雑して起動が数十分遅れることがあるため、あえてずらしています
- 上位件数: `screen.py` の `TOP_N`
- キャッシュ対象の絞り込み: `screen.py` の `MIN_DV`（既定 $1M）。小さくすると容量が増えます

## 無料枠の使用量
- GitHub Actions: 通常運転は1回あたり約3分（月60分）。初回 backfill のみ約56分。private リポジトリの無料枠は月2,000分。**public にすれば無制限**
- リポジトリ容量: 日足キャッシュは1日1ファイル（約180KB、既存ファイルは書き換えない）。売買代金 $1M 未満の銘柄は保存しないため、年間の増加は約45MB。保持期間（`CACHE_KEEP=300`）を超えた分は自動削除
- GitHub Pages: 日次JSONは1日約60KB（年15MB）。サイト容量1GB・転送量100GB/月の上限に対して十分小さい
- Polygon 無料枠: 5リクエスト/分。月間の回数制限はなし

## 注意
- Nasdaq screener API は非公式のため、仕様変更時は `fetch_universe()` の調整が必要です
- Polygon 無料枠は前日終値まで（当日リアルタイムは不可）。6:30 JST 実行なら米国当日分が反映されます
- 市場データの正確性は保証されません。投資助言ではありません
