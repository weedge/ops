"""发布后数据回收：T+1h / 24h / 72h / 7d 快照，异常（同平台同节点中位数 ×3 或 ×0.3）发事件（07）。

python -m popto_ops.collectors.metrics plan       # social.post.published → 登记 posts + 排采集计划
python -m popto_ops.collectors.metrics collect    # cron.collect_metrics → 采到点的快照
python -m popto_ops.collectors.metrics import <csv>   # 创作者后台导出的数据手工导入
"""
from __future__ import annotations

import csv
import statistics
import sys
from datetime import timedelta
from pathlib import Path

from .. import db, platforms
from ..config import load_agents_config
from ..scriptkit import emit_result, hub_client, load_task
from ..timeutil import iso, now, parse_iso
from .tikhub import TikHub

CHECKPOINTS = {"1h": timedelta(hours=1), "24h": timedelta(hours=24), "72h": timedelta(hours=72), "7d": timedelta(days=7)}
METRIC_COLS = ["views", "likes", "comments", "shares", "favorites", "follows_gained", "completion_rate",
               "avg_watch_sec", "profile_visits"]
GIVE_UP_AFTER = timedelta(hours=12)
LATE_TOLERANCE = timedelta(hours=2)   # 登记时某个节点已过点超过这么久，就不再采这个节点


def register_post(ops, d: dict) -> str:
    platform = platforms.normalize(d["platform"])
    ppid = str(d.get("platform_post_id") or d.get("url") or "")
    pid = d.get("post_id") or f"{platform}_{ppid}"[:80]
    published = d.get("published_at") or iso()
    ops.execute(
        "INSERT INTO posts(id,content_id,platform,account,platform_post_id,url,title,pillar,published_at,channel_code,publish_mode)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET url=excluded.url, title=COALESCE(excluded.title,title)",
        (pid, d.get("content_id"), platform, d.get("account"), ppid, d.get("url"), d.get("title"), d.get("pillar"),
         published, d.get("channel_code"), d.get("publish_mode", "manual")),
    )
    base = parse_iso(published)
    late = False
    for cp, delta in CHECKPOINTS.items():
        due = base + delta
        if now() - due > LATE_TOLERANCE:
            # 补登记时早已过点：不拿现在的数冒充 1h / 24h 快照（会污染同节点中位数和异常判断）
            ops.execute("INSERT OR IGNORE INTO metric_jobs(post_id,checkpoint,due_at,done_at,last_error) VALUES (?,?,?,?,?)",
                        (pid, cp, iso(due), "skipped:" + iso(), "登记时已过点，未采"))
            late = True
        else:
            ops.execute("INSERT OR IGNORE INTO metric_jobs(post_id,checkpoint,due_at) VALUES (?,?,?)", (pid, cp, iso(due)))
    if late:  # 补一张“补登记时”的快照，让这条内容至少有一份真实数据
        ops.execute("INSERT OR IGNORE INTO metric_jobs(post_id,checkpoint,due_at) VALUES (?,?,?)", (pid, "late", iso()))
    return pid


def cmd_plan() -> None:
    lease = load_task()
    ops = db.open_ops()
    ids = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        if not d.get("platform"):
            continue
        ids.append(register_post(ops, d))
    late = [i for i in ids if ops.execute("SELECT 1 FROM metric_jobs WHERE post_id=? AND checkpoint='late'", (i,)).fetchone()]
    msg = f"登记 {len(ids)} 条已发布内容"
    if len(ids) - len(late):
        msg += f"，{len(ids) - len(late)} 条按 1h / 24h / 72h / 7d 排定采集"
    if late:
        msg += f"，{len(late)} 条登记时已过点（只补采一次当前数据）"
    emit_result(msg if ids else "事件里没有可登记的发布信息", posts=ids)


def _anomaly(ops, post: dict, cp: str, views: int | None) -> str | None:
    if not views or cp not in CHECKPOINTS:   # “late”补登记快照各条发布时长不同，互相比没有意义
        return None
    vals = [r[0] for r in ops.execute(
        "SELECT m.views FROM post_metrics m JOIN posts p ON p.id=m.post_id WHERE p.platform=? AND m.checkpoint=?"
        " AND m.post_id<>? AND m.views IS NOT NULL ORDER BY m.captured_at DESC LIMIT 60", (post["platform"], cp, post["id"]))]
    if len(vals) < 5:
        return None
    med = statistics.median(vals)
    if med <= 0:
        return None
    ratio = views / med
    if ratio >= 3:
        return f"高于同平台 {cp} 中位数 {ratio:.1f} 倍（{views} vs {med:.0f}）"
    if ratio <= 0.3:
        return f"低于同平台 {cp} 中位数（{ratio:.2f} 倍，{views} vs {med:.0f}）"
    return None


def save_snapshot(ops, post: dict, cp: str, m: dict, source: str, raw_ref: str | None = None) -> list[dict]:
    ts = iso()
    ops.execute(
        "INSERT OR REPLACE INTO post_metrics(post_id,captured_at,checkpoint,source," + ",".join(METRIC_COLS) + ",raw_ref)"
        " VALUES (?,?,?,?," + ",".join("?" * len(METRIC_COLS)) + ",?)",
        (post["id"], ts, cp, source, *[m.get(c) for c in METRIC_COLS], raw_ref),
    )
    ops.execute("UPDATE metric_jobs SET done_at=?, last_error=NULL WHERE post_id=? AND checkpoint=?", (ts, post["id"], cp))
    evs = [{"source": post["platform"], "type": "social.metrics.snapshot", "subject": f"post:{post['id']}",
            "summary": f"「{post.get('title') or post['id']}」{cp} 播放 {m.get('views')} 赞 {m.get('likes')} 评 {m.get('comments')}",
            "dedup_key": f"snapshot:{post['id']}:{cp}", "payload_ref": f"ops.db://post_metrics/{post['id']}"}]
    why = _anomaly(ops, post, cp, m.get("views"))
    if why:
        evs.append({"source": post["platform"], "type": "social.metrics.anomaly", "subject": f"post:{post['id']}",
                    "summary": f"「{post.get('title') or post['id']}」{why}", "dedup_key": f"anomaly:{post['id']}:{cp}",
                    "payload_ref": f"ops.db://post_metrics/{post['id']}", "data": {"checkpoint": cp, "post": post}})
    return evs


def cmd_collect() -> None:
    cfg = load_agents_config()
    ops = db.open_ops()
    hub = hub_client()
    th = TikHub(proxy=None)
    budget = int((cfg.get("budgets") or {}).get("tikhub_requests", 0) or 0)
    try:
        used = int(hub.get("/v1/counter", name="tikhub_requests").get("value", 0))
    except Exception:  # noqa: BLE001
        used = 0
    due = db.rows(ops.execute(
        "SELECT j.*, p.platform, p.platform_post_id, p.title, p.id AS pid FROM metric_jobs j JOIN posts p ON p.id=j.post_id"
        " WHERE j.done_at IS NULL AND j.due_at<=? ORDER BY j.due_at LIMIT 200", (iso(),)))
    events, done, manual, failed = [], 0, [], []
    # 自有作品优先读抖音创作者后台（有真实播放量），一次会话取完；取不到的再走 TikHub
    creator: dict[str, dict] = {}
    dy_ids = {j["platform_post_id"] for j in due if j["platform"] == "douyin" and str(j["platform_post_id"] or "").isdigit()}
    creator_err = None
    if dy_ids:
        try:
            from .creator import douyin_stats
            creator = douyin_stats(dy_ids)
        except Exception as e:  # noqa: BLE001
            creator_err = str(e)[:200]
    for j in due:
        post = {"id": j["pid"], "platform": j["platform"], "title": j["title"]}
        try:
            if j["platform"] == "douyin" and j["platform_post_id"] in creator:
                events += save_snapshot(ops, post, j["checkpoint"], creator[j["platform_post_id"]], "creator")
                done += 1
                continue
            if j["platform"] == "douyin" and th.configured and j["platform_post_id"].isdigit():
                if budget and used + th.requests >= budget:
                    raise RuntimeError("TikHub 今日请求预算已用完")
                m = th.douyin_video_stats(j["platform_post_id"])
                events += save_snapshot(ops, post, j["checkpoint"], m, "tikhub")
                done += 1
                continue
            raise LookupError("该平台没有可用的自动采集源，需要创作者后台导出或截图识别")
        except Exception as e:  # noqa: BLE001
            ops.execute("UPDATE metric_jobs SET attempts=attempts+1, last_error=? WHERE post_id=? AND checkpoint=?",
                        (str(e)[:300], j["pid"], j["checkpoint"]))
            if now() - parse_iso(j["due_at"]) > GIVE_UP_AFTER:
                ops.execute("UPDATE metric_jobs SET done_at=? WHERE post_id=? AND checkpoint=?", ("skipped:" + iso(), j["pid"], j["checkpoint"]))
            (manual if isinstance(e, LookupError) else failed).append(f"{j['pid']}@{j['checkpoint']}")
    if th.requests:
        try:
            hub.post("/v1/counter", {"name": "tikhub_requests", "value": th.requests})
        except Exception:  # noqa: BLE001
            pass
    parts = [f"采到 {done} 个快照"]
    if creator_err:
        parts.append(f"创作者后台没取到（{creator_err}），已用 TikHub 兜底")
    if manual:
        parts.append(f"{len(manual)} 个需人工导出（opsctl metrics import）")
    if failed:
        parts.append(f"{len(failed)} 个采集失败")
    anomalies = [e for e in events if e["type"] == "social.metrics.anomaly"]
    if anomalies:
        parts.append(f"{len(anomalies)} 条异常已转单条复盘")
    emit_result("；".join(parts) if due else "没有到点的采集任务", follow_up_events=events,
                manual=manual, failed=failed)


def import_csv(path: Path) -> tuple[int, list[dict]]:
    ops = db.open_ops()
    n, events = 0, []
    with path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            pid = row.get("post_id")
            if not pid and row.get("platform") and row.get("platform_post_id"):
                r = ops.execute("SELECT id FROM posts WHERE platform=? AND platform_post_id=?",
                                (row["platform"], row["platform_post_id"])).fetchone()
                pid = r[0] if r else None
            if not pid:
                continue
            p = ops.execute("SELECT id, platform, title FROM posts WHERE id=?", (pid,)).fetchone()
            if not p:
                continue
            m = {}
            for c in METRIC_COLS:
                v = (row.get(c) or "").strip()
                if v:
                    m[c] = float(v) if c in ("completion_rate", "avg_watch_sec") else int(float(v))
            events += save_snapshot(ops, dict(p), row.get("checkpoint") or "manual", m, "export", raw_ref=str(path))
            n += 1
    return n, events


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "plan":
        cmd_plan()
    elif cmd == "collect":
        cmd_collect()
    elif cmd == "import" and len(argv) > 1:
        n, events = import_csv(Path(argv[1]))
        hub = hub_client()
        for e in events:
            try:
                hub.emit(e)
            except Exception:  # noqa: BLE001
                pass
        print(f"导入 {n} 行，发出 {len(events)} 个事件")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
