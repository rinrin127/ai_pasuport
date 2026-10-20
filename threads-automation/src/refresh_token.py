"""長期アクセストークンのリフレッシュ（60日有効 → 期限前に更新する）

GitHub Actionsから月次で実行し、新トークンをリポジトリSecretに書き戻す想定。
標準出力には新トークンを出さず、GITHUB_OUTPUT 経由でのみ渡す。
"""
from __future__ import annotations

import os
import sys

from threads_client import refresh_long_lived_token


def main() -> int:
    token = os.environ.get("THREADS_ACCESS_TOKEN")
    if not token:
        print("THREADS_ACCESS_TOKEN が未設定です", file=sys.stderr)
        return 1

    result = refresh_long_lived_token(token)
    new_token = result["access_token"]
    expires_days = result.get("expires_in", 0) // 86400
    print(f"トークンをリフレッシュしました（有効期限: 約{expires_days}日）")

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as f:
            f.write(f"new_token={new_token}\n")
    else:
        # ローカル実行時のみトークンを表示
        print("新しいトークン（Secretに設定してください）:")
        print(new_token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
