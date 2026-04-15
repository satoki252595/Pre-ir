# Pre-IR スクリーナー

> 直近の日本株決算短信予定を、**上方修正履歴 × 連続増配 × 営業利益率トレンド** で
> 事前にスクリーニングする静的 Web サービス。

GitHub Pages でホスティングし、GitHub Actions が定期的にスクレイピングして
JSON を更新します。**フォールバック・モックデータは一切使用していません**。
スクレイピングに失敗した場合は CI が落ち、ページのデータも更新されません。

## デモ

`https://satoki252595.github.io/pre-ir/`
（GitHub Pages を有効化したあと表示されます）

## 何をするのか

1. **決算予定のスクレイピング**
   - 株探 (Kabutan) / みんかぶ (Minkabu) / TraderWeb の決算スケジュールページから
     直近の発表予定を取得します。複数ソースを順に試し、最初に取れたものを採用。
2. **ファンダメンタルズ取得**
   - 各銘柄について [IR BANK (irbank.net)](https://irbank.net/) から
     「業績予想の修正履歴」「年間配当履歴」「通期営業利益・営業利益率」をスクレイピング。
3. **スクリーニング**
   - 次の3条件をすべて満たす銘柄だけを抽出します（しきい値は `scripts/screen.py` で調整可）。
     - 過去の **上方修正が 2 回以上**
     - **連続増配が 3 年以上**
     - 直近5年で **営業利益率が増加トレンド**（線形回帰の傾き > 0 かつ 直近 > 最古）
   - 重み付けスコア (0–100) を併記してソート可能にしています。
4. **静的サイトとして配信**
   - `docs/index.html` + JSON を GitHub Pages がそのまま配信します。
   - JS でフィルタ／ソート／検索ができます。

## ディレクトリ構成

```
.
├── .github/workflows/update-data.yml   # 1日2回のスクレイピング & Pages デプロイ
├── scripts/
│   ├── common.py                       # HTTP / リトライ
│   ├── scrape_schedule.py              # 決算予定スクレイパ
│   ├── fetch_fundamentals.py           # IR BANK スクレイパ
│   ├── screen.py                       # スクリーニング & スコアリング
│   └── requirements.txt
└── docs/                               # GitHub Pages のドキュメントルート
    ├── index.html
    ├── app.js
    ├── style.css
    └── data/
        ├── schedule.json               # ← scrape_schedule.py が生成
        ├── fundamentals.json           # ← fetch_fundamentals.py が生成
        ├── all_evaluated.json          # ← screen.py が生成（評価済み全銘柄）
        ├── screened.json               # ← screen.py が生成（通過銘柄のみ）
        └── last_updated.json           # ← screen.py が生成（メタ情報）
```

## セットアップ

### 1. リポジトリを準備

このブランチを `main` にマージするか、Pages のソースをこのブランチに変更します。

### 2. GitHub Pages を有効化

リポジトリ Settings → Pages → **Build and deployment**
- Source: **GitHub Actions**

### 3. ワークフローを実行

Actions タブで `Update data and deploy` を選択し **Run workflow** を押すと
すぐにスクレイピング → JSON 更新 → Pages デプロイが走ります。

その後は cron で 1日2回（07:00 JST / 19:00 JST）自動更新されます。

### ローカルで試す

```bash
pip install -r scripts/requirements.txt
python scripts/scrape_schedule.py
python scripts/fetch_fundamentals.py
python scripts/screen.py

# docs/ を任意の静的サーバで配信
python -m http.server 8000 --directory docs
# → http://localhost:8000/
```

## スクリーニング条件のチューニング

`scripts/screen.py` の以下の定数を編集してください。

```python
MIN_UPWARD_REVISIONS = 2          # 上方修正の最小回数
MIN_CONSECUTIVE_DIVIDEND_YEARS = 3 # 連続増配の最低年数
RECENT_YEARS = 5                  # 営業利益率トレンドを見る年数
```

スコアの重みは `score_stock()` で調整できます（修正回数 30 / 増配年数 30 /
傾き 25 / 直近水準 15 = 100点）。

## データソースについて

| 種別 | 主ソース | 副ソース |
|---|---|---|
| 決算予定 | Kabutan `https://kabutan.jp/?mode=market_kessan&market=0` | Minkabu / TraderWeb |
| ファンダメンタルズ | IR BANK `https://irbank.net/{code}/{results,dividend,forecast}` | — |

スクレイピング先サイトの HTML 構造が変わると CI が落ちます。その際は対応する
スクリプトのパース処理を更新してください。**ダミーデータでお茶を濁すことは
仕様上行いません**（real-data only ポリシー）。

各サイトの `robots.txt` と利用規約に従い、リクエスト間隔（1〜2秒）を空けています。

## 注意 / 免責

- 本ツールは情報提供のみを目的としており、特定の銘柄の売買を推奨するもの
  ではありません。投資判断は自己責任で行ってください。
- スクレイピング先サイトのデータ精度・最新性は保証しません。
- スクレイピング先サイトの規約変更等によりサービスが停止する可能性があります。
