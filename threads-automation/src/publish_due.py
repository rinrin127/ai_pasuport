"""予約投稿の実行スクリプト（GitHub Actionsから15分おきに実行される想定）

queue/*.json のうち scheduled_at（JST）を過ぎたものを Threads に投稿し、
published/ に移動する。ぶら下げ（reply_text）があればリプライとして投稿する。
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from compliance import run_checks
from threads_client import ThreadsClient

JST = ZoneInfo("Asia/Tokyo")
ROOT = Path(__file__).resolve().parent.parent
QUEUE_DIR = ROOT / "queue"
PUBLISHED_DIR = ROOT / "published"


def load_post(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print(f"[skip] {path.name}: JSONの読み込みに失敗", file=sys.stderr)
        return None


def save_json(path: Path, data: dict) -> None:
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def publish_one(client: ThreadsClient, post: dict) -> dict:
    """1件投稿する。post を更新して返す"""
    if post.get("image_url"):
        media_id = client.publish_image(post["image_url"], text=post.get("text"))
    else:
        media_id = client.publish_text(post["text"])
    post["media_id"] = media_id
    post["published_at"] = datetime.now(JST).isoformat(timespec="seconds")

    # ぶら下げ（アフィリンク等）をリプライとして投稿
    if post.get("reply_text"):
        time.sleep(5)
        post["reply_media_id"] = client.publish_text(
            post["reply_text"], reply_to_id=media_id
        )

    post["status"] = "published"
    return post


def main() -> int:
    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        print("THREADS_ACCESS_TOKEN が未設定です", file=sys.stderr)
        return 1
    client = ThreadsClient(token, os.environ.get("THREADS_USER_ID", "me"))

    now = datetime.now(JST)
    published = 0
    errors = 0

    for path in sorted(QUEUE_DIR.glob("*.json")):
        post = load_post(path)
        if not post or post.get("status") != "scheduled":
            continue
        try:
            sched = datetime.fromisoformat(post["scheduled_at"])
        except (KeyError, ValueError):
            print(f"[skip] {path.name}: scheduled_at が不正", file=sys.stderr)
            continue
        if sched.tzinfo is None:
            sched = sched.replace(tzinfo=JST)
        if sched > now:
            continue

        # 投稿直前の最終コンプラ検査（NGワード・#PR・文字数・重複）
        issues = run_checks(post)
        if issues:
            post["status"] = "blocked"
            post["compliance_issues"] = issues
            save_json(path, post)
            print(
                f"[blocked] {path.name}: コンプラ検査NG "
                + "; ".join(f"{h['label']}「{h['matched']}」" for h in issues),
                file=sys.stderr,
            )
            errors += 1
            continue

        print(f"[publish] {path.name}: {post['text'][:30]}...")
        try:
            post = publish_one(client, post)
            save_json(PUBLISHED_DIR / path.name, post)
            path.unlink()
            published += 1
        except Exception as e:
            # 失敗した投稿は status=error にして残す（無限リトライ防止）
            post["status"] = "error"
            post["error"] = str(e)
            save_json(path, post)
            errors += 1
            print(f"[error] {path.name}: {e}", file=sys.stderr)
            traceback.print_exc()

    print(f"完了: 投稿 {published} 件 / エラー {errors} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
