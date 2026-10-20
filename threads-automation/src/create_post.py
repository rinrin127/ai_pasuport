"""投稿作成CLI — 手動作成 or Claude APIによるAI生成で予約キューに追加する

使い方:
  # 手動で本文を指定して予約
  python create_post.py --text "投稿本文" --at "2026-08-15 07:00" --type A \
      --reply "ぶら下げ本文（アフィリンク等・任意）"

  # Claude APIでバズ投稿を生成して予約（要 ANTHROPIC_API_KEY）
  python create_post.py --generate --theme "雨の日の室内遊びネタ" --type B \
      --at "2026-08-15 07:00"

  # 楽天商品を検索→アフィリンク付きで投稿生成（要 RAKUTEN_APP_ID / RAKUTEN_AFFILIATE_ID）
  #   本文はAI生成（6フック型・コンプラルール込み）、ぶら下げにアフィリンク自動挿入、
  #   #PR自動付与、NGワード機械検査を通ったものだけキューに入る
  python create_post.py --generate --rakuten "美顔ローラー" --at "2026-10-21 07:00"

  # 楽天商品のリンクだけ使って本文は手動
  python create_post.py --rakuten "美顔ローラー" --pick 2 --text "..." --now

  # 予約せず即時投稿キューへ（scheduled_at = 今）
  python create_post.py --text "..." --now
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = ROOT / "queue"

# ---- AI生成用の設定（脳育りん仕様） ----

ACCOUNT_CONTEXT = """\
アカウント: 脳育りん（@nouiku_rin）
テーマ: 0〜3歳の子の脳育・ことばの発達×自然体験
差別化: 元・国立青少年施設指導員（自然体験の現場経験）。この経歴が入ると信頼度が上がる
収益導線: 投稿→ぶら下げ（スレッドに追加）のアフィリンク（楽天など、PR表記必須）／LINE誘導

投稿の型（3分類）:
- A = 価格ギャップ型（「〇〇円のおもちゃより、△△円の□□」のような価格対比でフック）
- B = 時短・手軽型（忙しいママでも今日からできる手軽さでフック）
- C = 逆張り・共感型（常識への逆張り+「わかる」の共感でフック）

制約:
- 本文は500文字以内（Threadsの上限）
- 1行目でスクロールが止まるフックを最優先
- 最後は問いかけ or 保存を促す一言で締めてコメント/保存を誘発
- ぶら下げにアフィリンクを入れる場合は必ず【PR】表記を入れる
"""

SCORING_AXES = """\
5軸採点（各20点・計100点）で自己採点すること:
- hook: フック（1行目でスクロールが止まるか）
- concrete: 具体性（後で見返したい情報か＝保存・リポスト誘発）
- contrarian: 逆張り強度（「え？」と共有したくなるか）
- empathy: 共感（自分ごと化されるか＝いいね誘発）
- comment: コメント誘発力（問いかけが機能するか）
"""

POST_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "投稿本文（500文字以内）"},
        "reply_text": {
            "type": "string",
            "description": "ぶら下げ投稿の本文。不要なら空文字",
        },
        "type": {"type": "string", "enum": ["A", "B", "C"]},
        "predicted_score": {
            "type": "object",
            "properties": {
                "hook": {"type": "integer"},
                "concrete": {"type": "integer"},
                "contrarian": {"type": "integer"},
                "empathy": {"type": "integer"},
                "comment": {"type": "integer"},
                "total": {"type": "integer"},
            },
            "required": ["hook", "concrete", "contrarian", "empathy", "comment", "total"],
            "additionalProperties": False,
        },
        "rationale": {"type": "string", "description": "採点理由の要約（2文以内）"},
    },
    "required": ["text", "reply_text", "type", "predicted_score", "rationale"],
    "additionalProperties": False,
}


# ---- 楽天アフィリエイト連携 ----

RAKUTEN_PERSONA_PATH = ROOT / "config" / "rakuten_persona.md"

HOOK_TYPES = ["価格ショック", "社会的証明", "悩み言い当て", "時短・簡便", "意外性", "ビフォアフ示唆"]

RAKUTEN_POST_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "description": "投稿本文（フック1行+本文2〜5行+CTA1行、500文字以内）"},
        "hook_type": {"type": "string", "enum": HOOK_TYPES},
        "predicted_score": POST_SCHEMA["properties"]["predicted_score"],
        "rationale": {"type": "string", "description": "採点理由の要約（2文以内）"},
    },
    "required": ["text", "hook_type", "predicted_score", "rationale"],
    "additionalProperties": False,
}


def build_affiliate_reply(item: dict) -> str:
    """ぶら下げ投稿（アフィリンク）の定型文を作る"""
    name = item["itemName"][:50]
    return (
        f"【PR】投稿で紹介したのはこちら\n"
        f"{name}\n"
        f"★{item['reviewAverage']}（レビュー{item['reviewCount']:,}件）/ {item['itemPrice']:,}円\n"
        f"{item['affiliateUrl']}"
    )


def generate_rakuten_post(item: dict, model: str, theme: str | None = None) -> dict:
    """楽天商品の事実情報だけを素材に、6フック型で投稿文を生成する"""
    import anthropic

    client = anthropic.Anthropic()
    persona = RAKUTEN_PERSONA_PATH.read_text(encoding="utf-8")
    product_facts = (
        f"商品名: {item['itemName']}\n"
        f"価格: {item['itemPrice']:,}円\n"
        f"レビュー: ★{item['reviewAverage']}（{item['reviewCount']:,}件）\n"
        f"ショップ: {item.get('shopName') or '不明'}\n"
        f"商品説明（商品ページより抜粋）:\n{item.get('itemCaption') or '（なし）'}"
    )
    theme_hint = f"切り口の指定: {theme}\n" if theme else ""
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=persona + "\n\n" + SCORING_AXES,
        output_config={"format": {"type": "json_schema", "schema": RAKUTEN_POST_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": (
                    "以下の楽天商品を紹介するThreads投稿を1本作成してください。\n"
                    "使ってよい情報はこの商品情報に書かれている事実のみです。"
                    "体験談の捏造・断定的な効果効能は禁止。"
                    "リンクはぶら下げ（リプライ）に付くので、CTAは「リンクはリプ欄に👇」等にしてください。\n"
                    f"{theme_hint}\n{product_facts}\n\n"
                    "作成後、5軸で自己採点し predicted_score に入れてください。"
                ),
            }
        ],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude が生成を拒否しました。商品やテーマを変えて再実行してください。")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def generate_with_claude(theme: str, post_type: str | None, model: str) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    type_hint = f"型は必ず {post_type} を使うこと。" if post_type else "テーマに最適な型（A/B/C）を選ぶこと。"
    response = client.messages.create(
        model=model,
        max_tokens=16000,
        system=ACCOUNT_CONTEXT + "\n" + SCORING_AXES,
        output_config={"format": {"type": "json_schema", "schema": POST_SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": (
                    f"テーマ「{theme}」でThreadsのバズ投稿を1本作成してください。"
                    f"{type_hint}"
                    "作成後、5軸で自己採点し predicted_score に入れてください。"
                ),
            }
        ],
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude が生成を拒否しました。テーマを変えて再実行してください。")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def parse_when(value: str) -> datetime:
    """'2026-08-15 07:00' / ISO形式 をJSTのdatetimeにする"""
    value = value.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=JST)
        except ValueError:
            continue
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=JST)


def queue_post(post: dict) -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    sched = datetime.fromisoformat(post["scheduled_at"])
    slug = re.sub(r"[^\w]", "", post["text"][:8]) or "post"
    base = sched.strftime("%Y%m%d-%H%M")
    path = QUEUE_DIR / f"{base}-{slug}.json"
    n = 1
    while path.exists():
        n += 1
        path = QUEUE_DIR / f"{base}-{slug}-{n}.json"
    path.write_text(
        json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def main() -> int:
    p = argparse.ArgumentParser(description="Threads投稿を予約キューに追加")
    p.add_argument("--text", help="投稿本文（手動作成）")
    p.add_argument("--reply", help="ぶら下げ投稿の本文（任意）")
    p.add_argument("--type", choices=["A", "B", "C"], help="投稿の型")
    p.add_argument("--at", help="投稿日時（JST） 例: '2026-08-15 07:00'")
    p.add_argument("--now", action="store_true", help="即時投稿（次のスケジューラ実行時）")
    p.add_argument("--image-url", help="画像URL（任意・公開URLである必要あり)")
    p.add_argument("--generate", action="store_true", help="Claude APIで生成する")
    p.add_argument("--theme", help="生成テーマ（--generate 時に必須。--rakuten併用時は任意）")
    p.add_argument("--model", default="claude-opus-5", help="生成に使うClaudeモデル")
    p.add_argument("--rakuten", metavar="KEYWORD", help="楽天商品を検索してアフィリンクをぶら下げに自動挿入")
    p.add_argument("--pick", type=int, default=1, help="楽天検索結果の何位の商品を使うか（デフォルト1位）")
    args = p.parse_args()

    if args.now:
        sched = datetime.now(JST)
    elif args.at:
        sched = parse_when(args.at)
    else:
        p.error("--at か --now のどちらかを指定してください")

    # 楽天商品リサーチ（--rakuten指定時）
    rakuten_item = None
    if args.rakuten:
        from rakuten_client import search_keyword

        candidates = search_keyword(args.rakuten, top=max(args.pick, 5))
        if not candidates:
            p.error(
                f"「{args.rakuten}」で条件（レビュー件数・評価・除外ワード）を満たす商品が"
                "見つかりませんでした。config/rakuten.json のフィルタ緩和も検討してください"
            )
        if args.pick > len(candidates):
            p.error(f"候補は{len(candidates)}件しかありません（--pick {args.pick}）")
        rakuten_item = candidates[args.pick - 1]
        print(
            f"対象商品（{args.pick}位 / スコア{rakuten_item['score']}）: "
            f"{rakuten_item['itemName'][:40]}… "
            f"¥{rakuten_item['itemPrice']:,}（★{rakuten_item['reviewAverage']}/"
            f"{rakuten_item['reviewCount']:,}件）"
        )

    if args.generate:
        if rakuten_item:
            result = generate_rakuten_post(rakuten_item, args.model, theme=args.theme)
            post_type = f"R:{result['hook_type']}"
            reply_text = args.reply or build_affiliate_reply(rakuten_item)
        else:
            if not args.theme:
                p.error("--generate には --theme が必要です（--rakuten併用時を除く）")
            result = generate_with_claude(args.theme, args.type, args.model)
            post_type = result["type"]
            reply_text = result.get("reply_text") or (args.reply or "")
        post = {
            "scheduled_at": sched.isoformat(timespec="seconds"),
            "type": post_type,
            "text": result["text"],
            "reply_text": reply_text,
            "image_url": args.image_url,
            "predicted_score": result["predicted_score"],
            "status": "scheduled",
        }
        print(f"--- 生成結果（予測 {result['predicted_score']['total']}点 / 型 {post_type}）---")
        print(result["text"])
        if reply_text:
            print("--- ぶら下げ ---")
            print(reply_text)
        print(f"--- 採点理由: {result['rationale']}")
    else:
        if not args.text:
            p.error("--text か --generate のどちらかを指定してください")
        reply_text = args.reply or ""
        if rakuten_item and not reply_text:
            reply_text = build_affiliate_reply(rakuten_item)
        post = {
            "scheduled_at": sched.isoformat(timespec="seconds"),
            "type": args.type,
            "text": args.text,
            "reply_text": reply_text,
            "image_url": args.image_url,
            "predicted_score": None,
            "status": "scheduled",
        }

    if rakuten_item:
        post["rakuten_item"] = {
            k: rakuten_item[k]
            for k in ("itemName", "itemPrice", "reviewCount", "reviewAverage", "affiliateUrl")
        }

    # コンプラ検査（NGワード・#PR自動付与・文字数・重複）— 問題があればキュー投入をブロック
    from compliance import run_checks

    issues = run_checks(post)
    if post.get("auto_fixed"):
        print(f"自動修正: {', '.join(post['auto_fixed'])}")
    if issues:
        print("⚠️ コンプラ検査で問題が見つかったためキューに入れません:", file=sys.stderr)
        for h in issues:
            print(f"  - [{h['field']}] {h['label']}: 「{h['matched']}」", file=sys.stderr)
        return 1

    path = queue_post(post)
    print(f"予約完了: {path.relative_to(ROOT)} → {sched.strftime('%Y-%m-%d %H:%M')} JST")
    print("git add / commit / push でリポジトリに反映すると、スケジューラが自動投稿します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
