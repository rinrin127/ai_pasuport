"""楽天市場商品検索APIクライアント（楽天ウェブサービス IchibaItem/Search）

threads-rakuten-affili スキルの rakuten_search.py と同じ選定基準・スコアリングを使用。

必要な環境変数:
  RAKUTEN_APP_ID       : https://webservice.rakuten.co.jp/ で発行（無料・即時）
  RAKUTEN_AFFILIATE_ID : https://affiliate.rakuten.co.jp/ の「パラメータ確認」で表示される
                         xxxx.xxxx.xxxx 形式のID

使い方:
  python src/rakuten_client.py --test                 # API疎通確認
  python src/rakuten_client.py --keyword "美顔ローラー"  # 候補商品を表示
  python src/rakuten_client.py --genres               # config/rakuten.json のジャンル巡回
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

API = "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601"
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "rakuten.json"


class RakutenAPIError(RuntimeError):
    pass


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _credentials() -> tuple[str, str]:
    app_id = os.environ.get("RAKUTEN_APP_ID", "")
    affiliate_id = os.environ.get("RAKUTEN_AFFILIATE_ID", "")
    if not app_id or not affiliate_id:
        raise RakutenAPIError(
            "RAKUTEN_APP_ID / RAKUTEN_AFFILIATE_ID が未設定です。"
            "README.md の楽天セットアップ手順を参照してください。"
        )
    return app_id, affiliate_id


def _fetch(params: dict) -> dict:
    r = requests.get(API, params=params, timeout=20)
    if r.status_code >= 400:
        raise RakutenAPIError(f"楽天API {r.status_code}: {r.text[:300]}")
    return r.json()


def score_item(it: dict, cfg: dict) -> float:
    """レビュー件数×評価×価格帯適合の複合スコア(0-100) — スキルと同一ロジック"""
    rc = it.get("reviewCount", 0)
    ra = it.get("reviewAverage", 0.0)
    price = it.get("itemPrice", 0)
    s = 0.0
    s += min(rc / cfg.get("review_count_cap", 2000), 1.0) * 45
    s += max(0.0, (ra - 4.0) / 1.0) * 35
    lo, hi = cfg.get("price_range", [1500, 6000])
    s += 20 if lo <= price <= hi else 8
    return round(s, 1)


def _normalize(it: dict, cfg: dict, genre_label: str | None = None) -> dict | None:
    """フィルタを適用し、通過した商品を正規化して返す（弾かれたらNone）"""
    if it.get("reviewCount", 0) < cfg.get("min_review_count", 100):
        return None
    if it.get("reviewAverage", 0) < cfg.get("min_review_average", 4.2):
        return None
    name = it.get("itemName", "")
    if any(ng in name for ng in cfg.get("exclude_keywords", [])):
        return None
    return {
        "genre": genre_label,
        "itemName": name,
        "itemPrice": it.get("itemPrice"),
        "reviewCount": it.get("reviewCount"),
        "reviewAverage": it.get("reviewAverage"),
        "affiliateUrl": it.get("affiliateUrl") or it.get("itemUrl"),
        "itemCaption": (it.get("itemCaption") or "")[:800],
        "shopName": it.get("shopName"),
        "score": score_item(it, cfg),
    }


def search_keyword(keyword: str, top: int = 5, apply_filters: bool = True) -> list[dict]:
    """キーワード検索で候補商品を返す（スコア降順）"""
    app_id, affiliate_id = _credentials()
    cfg = load_config()["filters"]
    params = {
        "applicationId": app_id,
        "affiliateId": affiliate_id,
        "keyword": keyword,
        "sort": "-reviewCount",
        "hits": 30,
        "formatVersion": 2,
    }
    items = []
    for it in _fetch(params).get("Items", []):
        norm = _normalize(it, cfg) if apply_filters else _normalize(it, {**cfg, "min_review_count": 0, "min_review_average": 0})
        if norm:
            items.append(norm)
    items.sort(key=lambda x: x["score"], reverse=True)
    return items[:top]


def search_genres(top: int = 10) -> list[dict]:
    """config/rakuten.json のジャンルを巡回して上位商品を返す（日次リサーチ用）"""
    app_id, affiliate_id = _credentials()
    conf = load_config()
    cfg = conf["filters"]
    all_items: list[dict] = []
    for g in conf["genres"]:
        params = {
            "applicationId": app_id,
            "affiliateId": affiliate_id,
            "genreId": g["genreId"],
            "sort": "-reviewCount",
            "hits": 30,
            "formatVersion": 2,
            "minPrice": cfg.get("price_range", [1500, 6000])[0],
            "maxPrice": cfg.get("price_range", [1500, 6000])[1],
        }
        try:
            for it in _fetch(params).get("Items", []):
                norm = _normalize(it, cfg, genre_label=g["label"])
                if norm:
                    all_items.append(norm)
        except RakutenAPIError as e:
            print(f"[warn] genre {g['label']}: {e}", file=sys.stderr)
        time.sleep(1.1)  # レート制限(1req/sec)遵守
    all_items.sort(key=lambda x: x["score"], reverse=True)
    return all_items[:top]


def test_connection() -> None:
    app_id, _ = _credentials()
    _fetch({"applicationId": app_id, "keyword": "test", "hits": 1})


def _print_items(items: list[dict]) -> None:
    for i, it in enumerate(items, 1):
        print(
            f"  {i}. [{it['score']}] {it['itemName'][:40]}… ¥{it['itemPrice']} "
            f"(★{it['reviewAverage']}/{it['reviewCount']}件)"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description="楽天商品リサーチ")
    ap.add_argument("--test", action="store_true", help="API疎通確認のみ")
    ap.add_argument("--keyword", help="キーワード検索")
    ap.add_argument("--genres", action="store_true", help="設定ジャンルを巡回リサーチ")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--out", help="結果をJSONで保存するパス")
    args = ap.parse_args()

    if args.test:
        try:
            test_connection()
            print("[OK] 楽天API疎通確認に成功しました")
            return 0
        except Exception as e:
            print(f"[NG] 楽天API疎通失敗: {e}", file=sys.stderr)
            return 1

    if args.keyword:
        items = search_keyword(args.keyword, top=args.top)
    elif args.genres:
        items = search_genres(top=args.top)
    else:
        ap.error("--test / --keyword / --genres のいずれかを指定してください")
        return 2

    print(f"[OK] {len(items)}商品:")
    _print_items(items)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {"date": time.strftime("%Y-%m-%d"), "products": items},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"保存: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
