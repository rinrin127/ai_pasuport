# Threads自動化システム（投稿作成 → 予約投稿 → 投稿分析）

Meta公式の **Threads API** を使った自動化パイプラインです。GitHub Actionsだけで動くので、サーバー不要・無料で運用できます。

```
┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│ ① 投稿作成       │ →  │ ② 予約投稿        │ →  │ ③ 投稿分析        │
│ create_post.py  │    │ GitHub Actions   │    │ GitHub Actions   │
│ ・手動 or AI生成 │    │ 15分おきにqueue/  │    │ 毎朝7時にインサイト│
│ ・5軸で予測採点  │    │ をチェックして投稿 │    │ 取得→採点→レポート│
└─────────────────┘    └──────────────────┘    └──────────────────┘
```

分析は `threads-post-analysis` スキル（脳育りん仕様）と同じ基準:
**A/B/C型分類・5軸100点採点（フック/具体性/逆張り強度/共感/コメント誘発力）・ファネル診断・Obsidian用トラッキング行**

---

## 初回セットアップ

### 1. Meta開発者アプリを作る

1. https://developers.facebook.com/ で開発者登録 → 「アプリを作成」
2. ユースケースで **「Threads APIにアクセス」** を選択
3. 権限（スコープ）を追加: `threads_basic` / `threads_content_publish` / `threads_manage_insights`
4. 「Threadsアプリテスター」に自分のThreadsアカウントを追加し、Threadsアプリ側（設定 → アカウント → Webサイトのアクセス許可）で招待を承認

### 2. アクセストークンを取得

1. アプリダッシュボードの「Threads API」→ グラフAPIエクスプローラ等で **短期トークン** を取得
2. 短期トークンを **長期トークン（60日有効）** に交換:

```bash
curl -s "https://graph.threads.net/access_token?grant_type=th_exchange_token&client_secret=<アプリシークレット>&access_token=<短期トークン>"
```

3. 自分のユーザーIDを確認:

```bash
curl -s "https://graph.threads.net/v1.0/me?fields=id,username&access_token=<長期トークン>"
```

### 3. GitHubリポジトリにSecretを設定

リポジトリの Settings → Secrets and variables → Actions:

| Secret名 | 値 |
|---|---|
| `THREADS_ACCESS_TOKEN` | 長期アクセストークン（必須） |
| `THREADS_USER_ID` | 手順2-3で確認したID（必須） |
| `GH_PAT` | secrets書き込み権限付きPAT（任意・トークン自動更新用） |

### 4. 楽天アフィリエイト連携（任意）

`--rakuten` で商品リサーチ→アフィリンク自動挿入を使う場合に設定:

1. **アプリID**: https://webservice.rakuten.co.jp/ で「アプリID発行」（無料・即時）
2. **アフィリエイトID**: https://affiliate.rakuten.co.jp/ ログイン後「パラメータ確認」で表示される `xxxx.xxxx.xxxx` 形式のID
3. 環境変数 `RAKUTEN_APP_ID` / `RAKUTEN_AFFILIATE_ID` に設定（投稿作成はローカル実行なのでSecretsは不要）
4. 疎通確認: `python src/rakuten_client.py --test`

商品の選定基準（レビュー100件以上・★4.2以上・1,500〜6,000円等）は `config/rakuten.json`、
投稿生成のペルソナ・6フック型は `config/rakuten_persona.md`、
追加NGワードは `config/ng_words.txt` で管理します（threads-rakuten-affiliスキルと同じ基準）。

### 5. ローカル環境（投稿作成に使う場合）

```bash
cd threads-automation
pip install -r requirements.txt
export THREADS_ACCESS_TOKEN="..."   # 手動テスト投稿する場合
export ANTHROPIC_API_KEY="..."      # AI生成を使う場合
export RAKUTEN_APP_ID="..."         # 楽天連携を使う場合
export RAKUTEN_AFFILIATE_ID="..."
```

---

## 使い方

### ① 投稿作成

**手動で予約:**

```bash
python src/create_post.py \
  --text "3000円の知育玩具より、0円の松ぼっくり。..." \
  --type A \
  --at "2026-08-15 07:00" \
  --reply "【PR】投稿で紹介した図鑑はこちら → https://..."
```

**Claude APIで生成して予約**（脳育りんのアカウント設定・A/B/C型・5軸自己採点込みで生成されます）:

```bash
python src/create_post.py --generate \
  --theme "雨の日の室内遊びネタ" --type B --at "2026-08-15 07:00"
```

**楽天アフィリエイト投稿**（商品検索→本文AI生成→ぶら下げにアフィリンク自動挿入）:

```bash
# キーワードで商品検索し、スコア1位の商品で投稿を生成して予約
python src/create_post.py --generate --rakuten "美顔ローラー" --at "2026-10-21 07:00"

# 検索2位の商品を使う / 本文は手動で書く
python src/create_post.py --rakuten "美顔ローラー" --pick 2 --text "..." --now

# 商品リサーチだけしたい（キーワード or 設定ジャンル巡回）
python src/rakuten_client.py --keyword "美顔ローラー"
python src/rakuten_client.py --genres
```

楽天投稿はキュー投入前に**コンプラ検査**（薬機法・景表法NGワード／#PR自動付与／500字制限／過去投稿との重複）が自動で走り、NGならブロックされます。投稿直前（GitHub Actions側）にも同じ検査が最終ゲートとして入ります。

どちらも `queue/` にJSONが作られるので、**git push すると予約完了**です。JSONを直接書いてもOK（`queue/_example.json.sample` 参照）。

### ② 予約投稿（全自動）

`.github/workflows/threads-scheduler.yml` が15分おきに `queue/` をチェックし、`scheduled_at`（JST）を過ぎた投稿を公開します。

- 本文投稿 → `reply_text` があれば **ぶら下げ（リプライ）** も自動投稿
- 成功した投稿は `published/` に移動（media_id・公開日時を記録）
- 失敗した投稿は `status: "error"` になってqueueに残る（原因を直して `"scheduled"` に戻せば再試行）

### ③ 投稿分析（全自動）

`.github/workflows/threads-analytics.yml` が毎朝7時(JST)に実行:

1. アカウントのフォロワー数などを `data/account_history.csv` に日次記録（フォロー増の把握用）
2. 直近14日の投稿のインサイト（表示・いいね・コメント・リポスト・引用・シェア）を取得
3. **投稿から48時間経過した投稿を実測採点**（5軸100点）し、AI生成時の予測点と比較
4. ボトルネック診断（フック/本文/コメント誘発/プロフィール導線のどこで詰まったか）と次の一手を提案
5. `reports/YYYY-MM-DD.md` に日次レポート、`data/tracking.md` にObsidian用トラッキング行を追記

`data/tracking.md` はそのままObsidianにコピペできる形式です。

---

## ディレクトリ構成

```
threads-automation/
├── src/
│   ├── threads_client.py   # Threads APIクライアント
│   ├── create_post.py      # 投稿作成CLI（手動 / AI生成）
│   ├── publish_due.py      # 予約投稿の実行（Actionsが呼ぶ）
│   ├── analyze.py          # 分析・採点・レポート（Actionsが呼ぶ）
│   ├── rakuten_client.py   # 楽天商品検索（アフィリンク取得・商品スコアリング）
│   ├── compliance.py       # コンプラ検査（NGワード・#PR・重複）
│   └── refresh_token.py    # トークンリフレッシュ
├── config/
│   ├── rakuten.json        # 商品選定フィルタ・巡回ジャンル
│   ├── rakuten_persona.md  # 楽天投稿のペルソナ・6フック型（AI生成プロンプト）
│   └── ng_words.txt        # 追加NGワード（週次で追記更新）
├── queue/                  # 予約待ちの投稿JSON
├── published/              # 公開済み投稿（インサイト・採点結果も蓄積）
├── reports/                # 日次分析レポート
└── data/
    ├── tracking.md         # Obsidian用トラッキング表
    └── account_history.csv # フォロワー数の日次推移
```

## 投稿JSONの形式

```json
{
  "scheduled_at": "2026-08-15T07:00:00+09:00",
  "type": "A",
  "text": "投稿本文（500文字以内）",
  "reply_text": "ぶら下げ本文（不要なら空文字）",
  "image_url": null,
  "predicted_score": {"hook": 16, "concrete": 15, "contrarian": 12, "empathy": 16, "comment": 14, "total": 73},
  "status": "scheduled"
}
```

## 制約・注意

- Threads APIの投稿上限は **24時間で250件**、テキストは **500文字** まで
- 画像投稿は `image_url` に**公開URL**が必要（ローカルファイル不可）
- 長期トークンは60日で失効。月次ワークフローが自動リフレッシュ（`GH_PAT` 設定時はSecretも自動更新）
- 投稿単位の「プロフィールアクセス」「フォロー増」「リンククリック」はThreads APIでは取得不可のため、レポートでは**未計測**と明記し、アカウント全体の日次差分で代替
- 採点の閾値は `src/analyze.py` の `score_post()` にまとまっているので、実測に合わせて調整可
