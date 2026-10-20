"""コンプラ検査（薬機法・景表法・ステマ規制）

threads-rakuten-affili スキルの compliance_check.py と同じ検査基準:
- 断定的効果効能・医薬品的表現の機械ブロック
- アフィリエイト投稿への #PR 自動付与（欠落は投稿ブロック対象）
- 過去投稿（published/）との重複検査（冒頭60字一致）
追加NGワードは config/ng_words.txt で管理（週次分析で追記更新）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NG_WORDS_PATH = ROOT / "config" / "ng_words.txt"
PUBLISHED_DIR = ROOT / "published"

# 断定的効果効能・医薬品的表現（正規表現）— スキルと同一
NG_PATTERNS = [
    (r"(治る|治療|完治|治す)", "医薬品的表現"),
    (r"(痩せる|やせる|脂肪が(落ち|燃え))", "断定的効果(ダイエット)"),
    (r"(シミ|シワ|ほうれい線)が(消え|なくな|薄くな)", "断定的効果(美容)"),
    (r"(効く|効果(あり|抜群|絶大)|即効)", "効能断定"),
    (r"(アンチエイジング|若返(り|る))", "医薬品的表現(老化)"),
    (r"(絶対|必ず|100%|確実に)", "保証表現"),
    (r"(No\.?1|ナンバーワン|日本一)(?!.*(調べ|調査|出典))", "最上級表現(出典なし)"),
    (r"(飲むだけで|塗るだけで|貼るだけで).{0,10}(変わ|改善|解消)", "簡便断定"),
    (r"(便秘|アトピー|ニキビ|花粉症|不眠)(が|を)(治|改善|解消)", "症状改善断定"),
]


def _load_extra_ng() -> list[tuple[str, str]]:
    if not NG_WORDS_PATH.exists():
        return []
    pats = []
    for line in NG_WORDS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            pats.append((re.escape(line), "追加NGワード"))
    return pats


def check_text(text: str) -> list[dict]:
    hits = []
    for pat, label in NG_PATTERNS + _load_extra_ng():
        m = re.search(pat, text)
        if m:
            hits.append({"label": label, "matched": m.group(0)})
    return hits


def _published_heads() -> set[str]:
    """過去投稿の冒頭60字セット（重複投稿ブロック用）"""
    heads = set()
    if PUBLISHED_DIR.exists():
        for p in PUBLISHED_DIR.glob("*.json"):
            try:
                post = json.loads(p.read_text(encoding="utf-8"))
                heads.add(post.get("text", "")[:60])
            except Exception:
                continue
    return heads


def is_affiliate(post: dict) -> bool:
    """アフィリエイト投稿か（楽天商品付き or ぶら下げにリンクあり）"""
    if post.get("rakuten_item"):
        return True
    reply = post.get("reply_text") or ""
    return "http" in reply or "【PR】" in reply


def ensure_pr_tag(post: dict) -> bool:
    """アフィリエイト投稿の本文に #PR がなければ自動付与。付与したらTrue"""
    text = post.get("text", "")
    if "#PR" in text or "#アフィリエイト" in text:
        return False
    post["text"] = text.rstrip() + "\n#PR"
    return True


def run_checks(post: dict, check_duplicate: bool = True) -> list[dict]:
    """投稿1件を検査して問題リストを返す（空=OK）。#PRは自動付与する"""
    issues = []
    if is_affiliate(post) and ensure_pr_tag(post):
        post.setdefault("auto_fixed", []).append("#PR自動付与")

    for field in ("text", "reply_text"):
        value = post.get(field) or ""
        for hit in check_text(value):
            issues.append({**hit, "field": field})

    if len(post.get("text", "")) > 500:
        issues.append({"label": "文字数超過", "matched": f"{len(post['text'])}文字", "field": "text"})

    if check_duplicate and post.get("text", "")[:60] in _published_heads():
        issues.append({"label": "重複投稿", "matched": "過去投稿と冒頭60字一致", "field": "text"})

    return issues
