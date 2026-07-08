# -*- coding: utf-8 -*-
"""
github_store.py — 把掃描結果 JSON 存進 GitHub repo (透過 GitHub API)

為什麼用 GitHub API 而非 git 指令:
  • Railway 容器不一定有設定好 git 認證
  • GitHub Contents API 用 token 就能直接 PUT 檔案, 最單純
  • 免費, 有版本歷史

需要的環境變數:
  GITHUB_TOKEN   — Personal Access Token (Fine-grained, 給該 repo 的 Contents 讀寫權)
  GITHUB_REPO    — 例如 "yourname/tw-stock-app"
  GITHUB_BRANCH  — 預設 "main"

Streamlit 端讀取則用公開 raw URL (見 streamlit_app.py), 不需 token。
"""
import os
import json
import base64
import requests

API_ROOT = "https://api.github.com"


def _cfg():
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    branch = os.environ.get("GITHUB_BRANCH", "main")
    return token, repo, branch


def push_json(path_in_repo: str, data: dict,
              commit_msg: str = None) -> tuple[bool, str]:
    """
    把 data 以 JSON 上傳/更新到 repo 的 path_in_repo。
    回傳 (成功, 訊息)。
    """
    token, repo, branch = _cfg()
    if not token or not repo:
        return False, "缺 GITHUB_TOKEN / GITHUB_REPO 環境變數"

    url = f"{API_ROOT}/repos/{repo}/contents/{path_in_repo}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    content_str = json.dumps(data, ensure_ascii=False, indent=2)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("ascii")

    # 最多重試 3 次: 遇 409 (sha 過期) 就重新抓最新 sha 再 PUT
    for attempt in range(3):
        # 每次都重新抓最新 sha (409 通常是 sha 過期造成)
        sha = None
        try:
            r = requests.get(url, headers=headers,
                             params={"ref": branch}, timeout=15)
            if r.status_code == 200:
                sha = r.json().get("sha")
        except Exception:
            pass

        payload = {
            "message": commit_msg or f"update {path_in_repo}",
            "content": content_b64,
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        try:
            r = requests.put(url, headers=headers, json=payload, timeout=20)
            if r.status_code in (200, 201):
                return True, "OK"
            if r.status_code == 409 and attempt < 2:
                import time as _t
                _t.sleep(1.0)  # 稍等再重抓 sha 重試
                continue
            return False, f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as e:
            if attempt < 2:
                continue
            return False, str(e)
    return False, "重試 3 次仍失敗"
