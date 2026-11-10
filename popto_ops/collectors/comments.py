"""评论采集：每条内容每小时最多一批，原文只进 ops.db（180 天后删除），昵称哈希化（08 §6）。

python -m popto_ops.collectors.comments collect
"""
from __future__ import annotations

import hashlib
import sys
from datetime import datetime, timedelta

from .. import db
from ..scriptkit import emit_result
from ..timeutil import TZ, iso, now
from .tikhub import TikHub

ACTIVE_DAYS = 14
RETENTION = timedelta(days=180)


def author_hash(s: str) -> str:
    return hashlib.sha256(("popto-ops:" + (s or "")).encode()).hexdigest()[:16]


def save_comments(ops, post_id: str, items: list[dict]) -> int:
    new = 0
    for c in items:
        cid = f"{post_id}:{c['id']}"
        created = c.get("created_at")
        if isinstance(created, (int, float)):
            created = iso(datetime.fromtimestamp(created, TZ))
        cur = ops.execute(
            "INSERT OR IGNORE INTO comments(id,post_id,author_hash,text,likes,created_at,reply_status,purge_after)"
            " VALUES (?,?,?,?,?,?, 'none', ?)",
            (cid, post_id, author_hash(c.get("author", "")), (c.get("text") or "")[:2000], c.get("likes") or 0,
             created or iso(), iso(now() + RETENTION)))
        new += cur.rowcount
    return new


def cmd_collect() -> None:
    ops = db.open_ops()
    purged = ops.execute("DELETE FROM comments WHERE purge_after<?", (iso(),)).rowcount
    th = TikHub()
    posts = db.rows(ops.execute("SELECT * FROM posts WHERE published_at>=?", (iso(now() - timedelta(days=ACTIVE_DAYS)),)))
    events, skipped = [], 0
    since = iso(now() - timedelta(hours=1))
    for p in posts:
        if p["platform"] != "douyin" or not th.configured or not str(p["platform_post_id"] or "").isdigit():
            skipped += 1
            continue
        try:
            items, _, _ = th.douyin_comments(p["platform_post_id"])
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        mark = iso()
        n = save_comments(ops, p["id"], items)
        if n:
            hour = now().strftime("%Y-%m-%dT%H")
            events.append({"source": p["platform"], "type": "social.comment.batch", "subject": f"post:{p['id']}",
                           "summary": f"「{p.get('title') or p['id']}」新增 {n} 条评论",
                           "dedup_key": f"{p['platform']}:comment:{p['id']}:{hour}",
                           "payload_ref": f"ops.db://comments?post_id={p['id']}&since={since}",
                           "data": {"new": n, "collected_at": mark}})
    emit_result(f"检查 {len(posts)} 条内容，{len(events)} 条有新评论，{skipped} 条暂无自动采集源；清理过期评论 {purged} 条",
                follow_up_events=events)


if __name__ == "__main__":
    if sys.argv[1:2] == ["collect"]:
        cmd_collect()
    else:
        print(__doc__)
        sys.exit(2)
