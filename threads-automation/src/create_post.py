"""投稿作成CLI — 手動作成 or Claude APIによるAI生成で予約キューに追加する

使い方:
  # 手動で本文を指定して予約
  python create_post.py --text "投稿本文" --at "2026-08-15 07:00" --type A \
      --reply "ぶら下げ本文（アフィリンク等・任意）"

  # Claude APIでバズ投稿を生成して予約（要 ANTHROPIC_API_KEY）
  python create_post.py --generate --theme "雨の日の室内遊びネタ" --type B \
      --at "2026-08-15 07:00"

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
    p.add_argument("--theme", help="生成テーマ（--generate 時に必須）")
    p.add_argument("--model", default="claude-opus-5", help="生成に使うClaudeモデル")
    args = p.parse_args()

    if args.now:
        sched = datetime.now(JST)
    elif args.at:
        sched = parse_when(args.at)
    else:
        p.error("--at か --now のどちらかを指定してください")

    if args.generate:
        if not args.theme:
            p.error("--generate には --theme が必要です")
        result = generate_with_claude(args.theme, args.type, args.model)
        post = {
            "scheduled_at": sched.isoformat(timespec="seconds"),
            "type": result["type"],
            "text": result["text"],
            "reply_text": result.get("reply_text") or (args.reply or ""),
            "image_url": args.image_url,
            "predicted_score": result["predicted_score"],
            "status": "scheduled",
        }
        print(f"--- 生成結果（予測 {result['predicted_score']['total']}点 / 型{result['type']}）---")
        print(result["text"])
        if result.get("reply_text"):
            print("--- ぶら下げ ---")
            print(result["reply_text"])
        print(f"--- 採点理由: {result['rationale']}")
    else:
        if not args.text:
            p.error("--text か --generate のどちらかを指定してください")
        if len(args.text) > 500:
            p.error(f"本文が500文字を超えています（{len(args.text)}文字）")
        post = {
            "scheduled_at": sched.isoformat(timespec="seconds"),
            "type": args.type,
            "text": args.text,
            "reply_text": args.reply or "",
            "image_url": args.image_url,
            "predicted_score": None,
            "status": "scheduled",
        }

    path = queue_post(post)
    print(f"予約完了: {path.relative_to(ROOT)} → {sched.strftime('%Y-%m-%d %H:%M')} JST")
    print("git add / commit / push でリポジトリに反映すると、スケジューラが自動投稿します。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
