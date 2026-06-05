# ポケカ抽選 通知Bot 🎴

ポケカBOX/パックの**抽選販売が新しく出るたびに、Discordチャンネルへ自動通知**するBotです。
[入荷Now](https://nyuka-now.com/archives/2459) を15分ごとに監視し、新着抽選（ポケセン公式・Tokyo Otaku Mode・主要店舗など）を検知して投稿します。

- **無料・PC不要**: GitHub Actions の無料枠で常時稼働
- **重複なし**: 通知済みの抽選は `state.json` で記憶し再通知しない
- **Bot名義で投稿**: Bot Token を使い REST API で投稿（Gateway常駐サーバー不要）

```
スクレイプ(入荷Now) → 既知分と差分 → 新着だけDiscord通知 → state.json更新(コミット)
```

## ファイル構成

| ファイル | 役割 |
|---|---|
| `sources.py` | 入荷Nowの抽選テーブルをスクレイプ・構造化 |
| `notifier.py` | Discord REST API へ embed 投稿（レート制限対応） |
| `main.py` | 差分検知・状態管理・通知のオーケストレーション |
| `state.json` | 通知済み抽選ID（CIが自動コミットして永続化） |
| `.github/workflows/check.yml` | 15分ごとの定期実行 |

---

## セットアップ

### 1. Discord Bot を作る

1. https://discord.com/developers/applications → **New Application**
2. 左メニュー **Bot** → **Reset Token** でトークンを取得（＝`DISCORD_BOT_TOKEN`）
3. **Installation**（または OAuth2 → URL Generator）で `bot` スコープ、権限は
   **Send Messages** / **Embed Links** を付けた招待URLを生成し、自分のサーバーに追加
4. 通知したいチャンネルを右クリック → **チャンネルIDをコピー**（＝`DISCORD_CHANNEL_ID`）
   - 出ない場合: ユーザー設定 → 詳細設定 → **開発者モード** をON

### 2. GitHub に置く（無料常時稼働）

```bash
# このフォルダで
git init && git add -A && git commit -m "init: pokeca chusen bot"
gh repo create pokeca-chusen-bot --public --source=. --push
```

> 💡 **public リポジトリ推奨**。Actions実行時間が無料無制限になります（privateは月2000分の枠を消費）。
> トークンはコード内に書かず Secrets に入れるため、publicでも安全です。

### 3. Secrets を登録

GitHubリポジトリ → **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `DISCORD_BOT_TOKEN` | 手順1のトークン |
| `DISCORD_CHANNEL_ID` | 手順1のチャンネルID |

### 4. 動かす

- **Actions** タブ → 「ポケカ抽選チェック」→ **Run workflow** で手動実行してテスト
- 初回は「監視を開始しました（現在N件）」だけ通知し、既存抽選は既知として記録
- 以降15分ごとに自動チェックし、**新着が出たときだけ**個別に通知

---

## ローカルでの動作確認

```bash
pip install -r requirements.txt

# 通知せず差分だけ表示（Discord不要）
python main.py --dry-run

# 実際に通知（要トークン）
cp .env.example .env   # 値を編集
source .env
python main.py
```

---

## カスタマイズ

- **チェック間隔**: `.github/workflows/check.yml` の `cron` を変更（例 `*/10 * * * *`）
- **監視ソース追加**: `sources.py` に `fetch_xxx()` を足して `fetch_all()` に追加
- **通知の見た目**: `notifier.py` の `lottery_embed()`

## 既知の制約 / 今後

- **torecamap / ポケセン公式直**: torecamapは恒常ガイド記事（締切なし）のためトリガーには不向き、
  ポケセン公式直はJSレンダリングで脆い。両者の内容は入荷Now経由でカバーされるため未実装。
- **`/list` 等の対話コマンド**: Discord Gatewayの常時接続が必要なため、このGitHub Actions構成では非対応。
  欲しい場合は discord.py 常駐Bot を VPS/Railway 等に置く拡張が必要（別途）。

## 抽選サイト 当たりやすさメモ（参考）

| 当たりやすさ | サイト | 条件 |
|---|---|---|
| ◎ | トイザらス | 過去1年で1,000円以上購入＋5pt以上 |
| ◎ | ふるいち | アプリ登録のみ（当選率約25%・穴場） |
| ○ | ゲオ / ヨドバシ | 店舗発行Ponta / 購入履歴 |
| △ | ポケセン公式 | 弾により20〜30%（変動大） |
| ▲ | ヤマダ電機 / ファミマ | 条件ゆるく高倍率（当たりにくい） |
