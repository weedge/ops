"""TikHub 最小客户端（抖音单条作品数据、评论）。Key 放钥匙串 popto-ops/tikhub。

注意（实测坑）：必须带非 Python 默认的 User-Agent，否则 Cloudflare 返回 403 / 1010。
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from ..scriptkit import keychain

BASE = "https://api.tikhub.io"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) popto-ops/0.1"


class TikHub:
    def __init__(self, key: str | None = None, proxy: str | None = None):
        self.key = key or keychain("tikhub")
        handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy} if proxy else {})]
        self.opener = urllib.request.build_opener(*handlers)
        self.requests = 0

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def get(self, path: str, **params) -> dict:
        if not self.key:
            raise RuntimeError("未配置 TikHub Key（钥匙串 popto-ops/tikhub）")
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.key}", "User-Agent": UA})
        self.requests += 1
        with self.opener.open(req, timeout=30) as r:
            return json.loads(r.read().decode())

    def douyin_video_stats(self, aweme_id: str) -> dict:
        d = self.get("/api/v1/douyin/app/v3/fetch_one_video", aweme_id=aweme_id)
        detail = ((d.get("data") or {}).get("aweme_detail") or {})
        s = detail.get("statistics") or {}
        return {
            "views": s.get("play_count"), "likes": s.get("digg_count"), "comments": s.get("comment_count"),
            "shares": s.get("share_count"), "favorites": s.get("collect_count"),
        }

    def douyin_comments(self, aweme_id: str, cursor: int = 0, count: int = 50) -> tuple[list[dict], int, bool]:
        d = self.get("/api/v1/douyin/app/v3/fetch_video_comments", aweme_id=aweme_id, cursor=cursor, count=count)
        data = d.get("data") or {}
        out = []
        for c in data.get("comments") or []:
            out.append({"id": str(c.get("cid")), "text": c.get("text") or "", "likes": c.get("digg_count") or 0,
                        "created_at": c.get("create_time"), "author": ((c.get("user") or {}).get("uid") or "")})
        return out, int(data.get("cursor") or 0), bool(data.get("has_more"))
