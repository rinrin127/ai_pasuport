"""Threads Graph API クライアント（Meta公式 Threads API）

必要な環境変数:
  THREADS_ACCESS_TOKEN : 長期アクセストークン（60日有効・要定期リフレッシュ）
  THREADS_USER_ID      : ThreadsユーザーID（省略時は "me"）

必要なアプリ権限:
  threads_basic, threads_content_publish, threads_manage_insights
"""
from __future__ import annotations

import time

import requests

API_BASE = "https://graph.threads.net/v1.0"

# 投稿インサイトで取得するメトリクス
MEDIA_METRICS = "views,likes,replies,reposts,quotes,shares"
# アカウントインサイトで取得するメトリクス
USER_METRICS = "views,likes,replies,reposts,quotes,followers_count"


class ThreadsAPIError(RuntimeError):
    pass


class ThreadsClient:
    def __init__(self, access_token: str, user_id: str = "me", timeout: int = 30):
        self.access_token = access_token
        self.user_id = user_id
        self.timeout = timeout

    # ---- 低レベルHTTP ----

    def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["access_token"] = self.access_token
        url = f"{API_BASE}/{path}"
        if method == "GET":
            r = requests.get(url, params=params, timeout=self.timeout)
        else:
            r = requests.post(url, data=params, timeout=self.timeout)
        if r.status_code >= 400:
            raise ThreadsAPIError(f"Threads API {r.status_code}: {r.text}")
        return r.json()

    def _get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params)

    def _post(self, path: str, params: dict | None = None) -> dict:
        return self._request("POST", path, params)

    # ---- プロフィール ----

    def get_profile(self) -> dict:
        return self._get(
            "me", {"fields": "id,username,name,threads_profile_picture_url"}
        )

    # ---- 投稿（コンテナ作成 → 公開の2段階） ----

    def create_text_container(self, text: str, reply_to_id: str | None = None) -> str:
        params = {"media_type": "TEXT", "text": text}
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        return self._post(f"{self.user_id}/threads", params)["id"]

    def create_image_container(
        self, image_url: str, text: str | None = None, reply_to_id: str | None = None
    ) -> str:
        params = {"media_type": "IMAGE", "image_url": image_url}
        if text:
            params["text"] = text
        if reply_to_id:
            params["reply_to_id"] = reply_to_id
        return self._post(f"{self.user_id}/threads", params)["id"]

    def publish_container(self, creation_id: str) -> str:
        return self._post(
            f"{self.user_id}/threads_publish", {"creation_id": creation_id}
        )["id"]

    def publish_text(self, text: str, reply_to_id: str | None = None, wait: int = 3) -> str:
        """テキスト投稿を作成して公開し、メディアIDを返す"""
        cid = self.create_text_container(text, reply_to_id=reply_to_id)
        time.sleep(wait)
        return self.publish_container(cid)

    def publish_image(self, image_url: str, text: str | None = None, wait: int = 30) -> str:
        """画像投稿を作成して公開する。画像は処理待ちが必要なため待機時間長め"""
        cid = self.create_image_container(image_url, text=text)
        time.sleep(wait)
        return self.publish_container(cid)

    # ---- 取得 ----

    def get_recent_posts(self, limit: int = 25) -> list[dict]:
        data = self._get(
            f"{self.user_id}/threads",
            {"fields": "id,text,timestamp,permalink,media_type", "limit": limit},
        )
        return data.get("data", [])

    # ---- インサイト ----

    @staticmethod
    def _flatten_metrics(data: dict) -> dict:
        """insightsレスポンスの values / total_value 両形式を {name: value} に正規化"""
        out: dict[str, int] = {}
        for m in data.get("data", []):
            name = m.get("name")
            value = 0
            if m.get("total_value") is not None:
                value = m["total_value"].get("value", 0)
            elif m.get("values"):
                # 時系列(views等)は合計、単発値はそのまま
                vals = [v.get("value", 0) for v in m["values"]]
                value = sum(vals) if len(vals) > 1 else (vals[0] if vals else 0)
            out[name] = value or 0
        return out

    def get_media_insights(self, media_id: str) -> dict:
        data = self._get(f"{media_id}/insights", {"metric": MEDIA_METRICS})
        return self._flatten_metrics(data)

    def get_user_insights(self) -> dict:
        data = self._get(
            f"{self.user_id}/threads_insights", {"metric": USER_METRICS}
        )
        return self._flatten_metrics(data)


def refresh_long_lived_token(access_token: str) -> dict:
    """長期トークン（60日）をリフレッシュする。発行から24時間以上経過している必要あり。

    戻り値: {"access_token": "...", "token_type": "bearer", "expires_in": 5183944}
    """
    r = requests.get(
        "https://graph.threads.net/refresh_access_token",
        params={"grant_type": "th_refresh_token", "access_token": access_token},
        timeout=30,
    )
    if r.status_code >= 400:
        raise ThreadsAPIError(f"Token refresh failed {r.status_code}: {r.text}")
    return r.json()
