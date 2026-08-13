"""投稿分析スクリプト（GitHub Actionsから毎朝実行される想定）

やること:
1. アカウントインサイトを取得して data/account_history.csv に日次スナップショット追記
   （フォロワー数の日次差分＝フォロー増の近似値として使う）
2. published/ の直近投稿のインサイト（表示・いいね・コメント・リポスト等）を取得してJSONに保存
3. 投稿から48時間経過した未分析の投稿を「実測採点（5軸100点）」→「ボトルネック診断」し、
   reports/YYYY-MM-DD.md にレポート出力 + data/tracking.md にObsidian用トラッキング行を追記

採点基準は threads-post-analysis スキル（脳育りん仕様）と同一の5軸:
  フック/具体性/逆張り強度/共感/コメント誘発力（各20点）
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from threads_client import ThreadsClient

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parent.parent
PUBLISHED_DIR = ROOT / "published"
REPORTS_DIR = ROOT / "reports"
DATA_DIR = ROOT / "data"
HISTORY_CSV = DATA_DIR / "account_history.csv"
TRACKING_MD = DATA_DIR / "tracking.md"

# インサイトを取り続ける期間（日）と、実測採点を確定するまでの時間（時間）
INSIGHT_WINDOW_DAYS = 14
SCORE_AFTER_HOURS = 48

TRACKING_HEADER = (
    "| 日付 | 型 | 投稿タイトル(冒頭15字) | 表示 | いいね | コメ | リポスト "
    "| フォロー増 | クリック | 成約 | 実測点 | 予測点 | 学び1行 |\n"
    "|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
)


# ---------- 採点ロジック ----------

def views_tier(views: int) -> str:
    if views >= 30000:
        return "バズ"
    if views >= 10000:
        return "バズ圏"
    if views >= 3000:
        return "好調"
    if views >= 500:
        return "平常"
    return "初速不発"


def _score_by_rate(rate: float, table: list[tuple[float, int]], zero: int = 2) -> int:
    for threshold, score in table:
        if rate >= threshold:
            return score
    return zero if rate <= 0 else zero + 2


def score_post(ins: dict) -> dict:
    """実測データから5軸100点採点。閾値は運用しながら調整してよい"""
    views = ins.get("views", 0)
    likes = ins.get("likes", 0)
    replies = ins.get("replies", 0)
    reposts = ins.get("reposts", 0)
    quotes = ins.get("quotes", 0)
    shares = ins.get("shares", 0)

    def rate(n: int) -> float:
        return n / views if views else 0.0

    # フック: 表示数の絶対値（1行目でスクロールが止まったか）
    if views >= 30000:
        hook = 20
    elif views >= 10000:
        hook = 17
    elif views >= 3000:
        hook = 14
    elif views >= 1500:
        hook = 11
    elif views >= 500:
        hook = 8
    elif views >= 200:
        hook = 5
    else:
        hook = 3

    # 具体性: 保存相当（リポスト+シェア）率 — 後で見返したい情報か
    concrete = _score_by_rate(
        rate(reposts + shares),
        [(0.005, 20), (0.003, 16), (0.0015, 12), (0.0005, 8)],
    )
    # 逆張り強度: リポスト+引用率 — 「え？」と共有したくなったか
    contrarian = _score_by_rate(
        rate(reposts + quotes),
        [(0.005, 20), (0.003, 16), (0.0015, 12), (0.0005, 8)],
    )
    # 共感: いいね率 — 自分ごと化されたか
    empathy = _score_by_rate(
        rate(likes),
        [(0.04, 20), (0.025, 16), (0.015, 12), (0.008, 8)],
    )
    # コメント誘発力: コメント率（0.3%以上でアルゴリズム評価が上がりやすい）
    comment = _score_by_rate(
        rate(replies),
        [(0.006, 20), (0.003, 16), (0.0015, 12), (0.0005, 8)],
    )

    engagement = rate(likes + replies + reposts)
    return {
        "hook": hook,
        "concrete": concrete,
        "contrarian": contrarian,
        "empathy": empathy,
        "comment": comment,
        "total": hook + concrete + contrarian + empathy + comment,
        "engagement_rate": round(engagement * 100, 2),
        "comment_rate": round(rate(replies) * 100, 3),
        "views_tier": views_tier(views),
    }


def diagnose_bottleneck(ins: dict, score: dict) -> tuple[str, str]:
    """ファネルのどこで詰まったかを1つに特定 → (診断, 学び1行)"""
    views = ins.get("views", 0)
    eng = score["engagement_rate"]
    if views < 500:
        return (
            "表示が出ていない → フックの問題 or 投稿時間・頻度の問題",
            "フック(1行目)を作り直す。投稿時間の変更も検討",
        )
    if eng < 3.0:
        return (
            "表示は出たがエンゲージが低い → 本文の具体性・共感の問題",
            "本文に具体的な数字・体験談(経歴)を足す",
        )
    if score["comment"] < 12:
        return (
            "エンゲージは良いがコメントが弱い → 問いかけが機能していない",
            "締めの問いかけを答えやすい二択にする",
        )
    return (
        "投稿単体は好調 → プロフィール導線(フォロー転換)を確認。プロフィールアクセス・"
        "フォロー増は投稿単位では未計測のためアカウント全体の日次差分で判断",
        "この型・テーマを横展開する",
    )


# ---------- データ処理 ----------

def append_account_snapshot(client: ThreadsClient) -> dict:
    """アカウントインサイトを取得しCSVに追記。前日との差分も返す"""
    ins = client.get_user_insights()
    today = datetime.now(JST).strftime("%Y-%m-%d")
    fields = ["date", "followers_count", "views", "likes", "replies", "reposts", "quotes"]

    rows: list[dict] = []
    if HISTORY_CSV.exists():
        with HISTORY_CSV.open(encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    prev = rows[-1] if rows else None
    rows = [r for r in rows if r["date"] != today]  # 同日再実行は上書き
    rows.append({k: (today if k == "date" else ins.get(k, 0)) for k in fields})

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with HISTORY_CSV.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    follower_delta = None
    if prev and prev["date"] != today:
        try:
            follower_delta = int(ins.get("followers_count", 0)) - int(prev["followers_count"])
        except (ValueError, TypeError):
            pass
    return {"snapshot": ins, "follower_delta": follower_delta}


def refresh_insights(client: ThreadsClient) -> list[tuple[Path, dict]]:
    """ウィンドウ内の公開済み投稿のインサイトを更新して返す"""
    now = datetime.now(JST)
    updated = []
    for path in sorted(PUBLISHED_DIR.glob("*.json")):
        post = json.loads(path.read_text(encoding="utf-8"))
        if not post.get("media_id"):
            continue
        published_at = datetime.fromisoformat(post["published_at"])
        if now - published_at > timedelta(days=INSIGHT_WINDOW_DAYS):
            continue
        try:
            post["insights"] = client.get_media_insights(post["media_id"])
            post["insights_fetched_at"] = now.isoformat(timespec="seconds")
        except Exception as e:
            print(f"[warn] {path.name}: インサイト取得失敗 {e}", file=sys.stderr)
            continue
        path.write_text(
            json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        updated.append((path, post))
    return updated


def tracking_row(post: dict, score: dict, learning: str) -> str:
    ins = post["insights"]
    date = datetime.fromisoformat(post["published_at"]).strftime("%Y-%m-%d")
    title = post["text"].replace("\n", " ")[:15].replace("|", "｜")
    predicted = (post.get("predicted_score") or {}).get("total", "—")
    return (
        f"| {date} | {post.get('type') or '—'} | {title} | {ins.get('views', 0)} "
        f"| {ins.get('likes', 0)} | {ins.get('replies', 0)} | {ins.get('reposts', 0)} "
        f"| 未計測 | 未計測 | 未計測 | {score['total']} | {predicted} | {learning} |\n"
    )


def append_tracking(rows: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not TRACKING_MD.exists():
        TRACKING_MD.write_text("# Threads投稿トラッキング\n\n" + TRACKING_HEADER, encoding="utf-8")
    with TRACKING_MD.open("a", encoding="utf-8") as f:
        f.writelines(rows)


def render_post_section(post: dict, score: dict, diagnosis: str, learning: str) -> str:
    ins = post["insights"]
    predicted = post.get("predicted_score") or {}
    lines = [
        f"### {post['text'][:30].replace(chr(10), ' ')}...",
        "",
        f"- 型: **{post.get('type') or '不明'}** / 投稿日時: {post['published_at']}",
        f"- 表示 **{ins.get('views', 0)}**（{score['views_tier']}） / "
        f"いいね {ins.get('likes', 0)} / コメント {ins.get('replies', 0)} / "
        f"リポスト {ins.get('reposts', 0)} / 引用 {ins.get('quotes', 0)} / シェア {ins.get('shares', 0)}",
        f"- エンゲージ率 **{score['engagement_rate']}%**（目安3%以上=良） / "
        f"コメント率 {score['comment_rate']}%（目安0.3%以上）",
        "",
        "| 軸 | 実測 | 予測 |",
        "|---|---|---|",
        f"| フック | {score['hook']}/20 | {predicted.get('hook', '—')} |",
        f"| 具体性 | {score['concrete']}/20 | {predicted.get('concrete', '—')} |",
        f"| 逆張り強度 | {score['contrarian']}/20 | {predicted.get('contrarian', '—')} |",
        f"| 共感 | {score['empathy']}/20 | {predicted.get('empathy', '—')} |",
        f"| コメント誘発力 | {score['comment']}/20 | {predicted.get('comment', '—')} |",
        f"| **合計** | **{score['total']}/100** | {predicted.get('total', '—')} |",
        "",
        f"**ボトルネック診断**: {diagnosis}",
        "",
        f"**次の一手**: {learning}",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        print("THREADS_ACCESS_TOKEN が未設定です", file=sys.stderr)
        return 1
    client = ThreadsClient(token, os.environ.get("THREADS_USER_ID", "me"))
    now = datetime.now(JST)

    # 1. アカウント日次スナップショット
    try:
        account = append_account_snapshot(client)
    except Exception as e:
        print(f"[warn] アカウントインサイト取得失敗: {e}", file=sys.stderr)
        account = {"snapshot": {}, "follower_delta": None}

    # 2. 投稿インサイト更新
    updated = refresh_insights(client)

    # 3. 48時間経過した未分析投稿を採点
    sections = []
    rows = []
    for path, post in updated:
        published_at = datetime.fromisoformat(post["published_at"])
        if post.get("analyzed") or now - published_at < timedelta(hours=SCORE_AFTER_HOURS):
            continue
        score = score_post(post["insights"])
        diagnosis, learning = diagnose_bottleneck(post["insights"], score)
        sections.append(render_post_section(post, score, diagnosis, learning))
        rows.append(tracking_row(post, score, learning))
        post["analyzed"] = True
        post["measured_score"] = score
        path.write_text(
            json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    if rows:
        append_tracking(rows)

    # 4. 日次レポート
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"{now.strftime('%Y-%m-%d')}.md"
    snap = account["snapshot"]
    delta = account["follower_delta"]
    header = [
        f"# Threads日次レポート {now.strftime('%Y-%m-%d')}",
        "",
        "## アカウント状況",
        "",
        f"- フォロワー数: **{snap.get('followers_count', '未計測')}**"
        + (f"（前日比 {'+' if delta >= 0 else ''}{delta}）" if delta is not None else ""),
        f"- 直近の表示数（アカウント全体）: {snap.get('views', '未計測')}",
        "",
        f"## 新規分析投稿（{len(sections)}件）",
        "",
    ]
    body = "\n".join(header) + ("\n".join(sections) if sections else "投稿から48時間経過した新規分析対象はありません。\n")
    if sections and len(rows) < 10:
        body += "\n> ⚠️ 分析サンプルが10投稿未満のため、上記は「傾向」であって「結論」ではありません。\n"
    report_path.write_text(body, encoding="utf-8")

    print(f"レポート出力: {report_path.relative_to(ROOT)}（分析 {len(sections)} 件 / インサイト更新 {len(updated)} 件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
