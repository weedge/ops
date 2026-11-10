"""发布执行器（05 §4）。

python -m popto_ops.executors.publish register     # content.pack.ready → 登记发布包
python -m popto_ops.executors.publish batch-card   # 17:00 → 今晚待发布 N 条，一张卡片
python -m popto_ops.executors.publish run          # content.publish.approved → 执行（一期全部半自动）
python -m popto_ops.executors.publish record       # content.published.manual → 人发完后补登记

一期所有平台按“半自动”执行：执行器把发布包整理好推给人，人在手机 / 后台发完后回复
「已发 <发布包ID> <链接>」（或 opsctl published），执行器补登记并排定数据采集。
自动发布适配器（Popto 发布中心 / socialpy）在 AUTO_ADAPTERS 里登记后才会启用。
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from .. import db
from ..collectors.metrics import register_post
from ..scriptkit import emit_result, hub_client, load_task
from ..timeutil import iso, now, parse_iso
from . import ActionLedger

PLATFORM_LABEL = {"douyin": "抖音", "wechat_channels": "视频号", "xiaohongshu": "小红书", "kuaishou": "快手",
                  "bilibili": "B站", "zhihu": "知乎", "wechat_mp": "公众号"}
AUTO_ADAPTERS: dict[str, object] = {}   # platform -> adapter(pack) -> {"platform_post_id","url"}；一期为空


def _pack_meta(pack_dir: str | None) -> dict:
    if not pack_dir:
        return {}
    p = Path(pack_dir).expanduser() / "meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def cmd_register() -> None:
    lease = load_task()
    ops = db.open_ops()
    ids = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        meta = _pack_meta(d.get("pack_dir"))
        platforms = d.get("platforms") or ([d["platform"]] if d.get("platform") else meta.get("platforms") or [])
        content = d.get("content_id") or (e.get("subject") or "").removeprefix("content:")
        for pf in platforms:
            pid = f"pk_{content}_{pf}"
            pm = (meta.get("per_platform") or {}).get(pf, {})
            ops.execute(
                "INSERT INTO publish_packs(id,content_id,platform,account,title,scheduled_at,pack_dir,publish_mode,status,created_at,updated_at)"
                " VALUES (?,?,?,?,?,?,?,?, 'ready', ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title,"
                " scheduled_at=excluded.scheduled_at, pack_dir=excluded.pack_dir, status=CASE WHEN status IN ('rejected','failed') THEN 'ready' ELSE status END,"
                " updated_at=excluded.updated_at",
                (pid, content, pf, pm.get("account") or d.get("account"), pm.get("title") or d.get("title") or meta.get("title"),
                 pm.get("scheduled_at") or d.get("scheduled_at"), d.get("pack_dir"), pm.get("publish_mode") or "semi_auto",
                 iso(), iso()))
            ids.append(pid)
    emit_result(f"登记 {len(ids)} 个发布包，等 17:00 统一发卡片" if ids else "事件里没有发布包信息", packs=ids)


def cmd_batch_card() -> None:
    ops = db.open_ops()
    packs = db.rows(ops.execute("SELECT * FROM publish_packs WHERE status='ready' ORDER BY scheduled_at, platform"))
    if not packs:
        emit_result("今晚没有待发布内容")
        return
    items = []
    for p in packs:
        t = ""
        if p["scheduled_at"]:
            try:
                t = parse_iso(p["scheduled_at"]).strftime("%H:%M")
            except ValueError:
                t = p["scheduled_at"]
        items.append({"platform": PLATFORM_LABEL.get(p["platform"], p["platform"]), "time": t,
                      "title": p["title"] or p["content_id"], "note": "半自动" if p["publish_mode"] != "auto" else "自动",
                      "pack_id": p["id"]})
    expires = now().replace(hour=22, minute=0, second=0)
    if expires <= now():
        expires = now() + timedelta(hours=2)
    card = {
        "kind": "publish_batch",
        "title": f"今晚 {len(packs)} 条内容待发布",
        "proposal": "全部发布（每条都已通过合规复核和视频质检，才会进入发布包）",
        "reasons": ["发布包目录里有标题、正文、话题、封面和渠道码，可在详情里逐条查看"],
        "risks": ["半自动平台需要你在手机上发，发完回复「已发 发布包ID 链接」以便回收数据"],
        "items": items,
        "options": [
            {"key": "approve_all", "label": "全部发布",
             "emit": [{"type": "content.publish.approved", "subject": f"pack:{p['id']}", "data": {"pack_id": p["id"]}} for p in packs]},
            {"key": "reject_all", "label": "全部不发", "require_reason": True},
        ],
        "expires_at": iso(expires),
        "on_expire": "reject_all",
        "links": sorted({p["pack_dir"] for p in packs if p["pack_dir"]})[:5],
    }
    ops.executemany("UPDATE publish_packs SET status='carded', updated_at=? WHERE id=?", [(iso(), p["id"]) for p in packs])
    emit_result(f"今晚 {len(packs)} 条待发布，已生成批量卡片", status="needs_decision", needs_decision=True, decision_card=card)


def _manual_instructions(p: dict, meta: dict) -> str:
    pm = (meta.get("per_platform") or {}).get(p["platform"], {})
    lines = [f"**{PLATFORM_LABEL.get(p['platform'], p['platform'])} · {p['title'] or p['content_id']}**（`{p['id']}`）"]
    for k, label in (("title", "标题"), ("body", "正文"), ("topics", "话题"), ("scheduled_at", "时间"), ("ai_label", "AI 声明")):
        v = pm.get(k) or meta.get(k)
        if v:
            lines.append(f"- {label}：{' '.join(v) if isinstance(v, list) else v}")
    if p["pack_dir"]:
        lines.append(f"- 素材目录：`{p['pack_dir']}`")
    lines.append(f"- 发完回复：`已发 {p['id']} <链接>`")
    return "\n".join(lines)


def cmd_run() -> None:
    lease = load_task()
    ops = db.open_ops()
    ledger = ActionLedger(ops)
    hub = hub_client()
    done, manual, skipped = [], [], []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        pack_id = d.get("pack_id") or (e.get("subject") or "").removeprefix("pack:")
        p = ops.execute("SELECT * FROM publish_packs WHERE id=?", (pack_id,)).fetchone()
        if not p:
            skipped.append(f"{pack_id}（不存在）")
            continue
        p = dict(p)
        ok, why = ledger.begin(f"publish:{pack_id}", "publish", pack_id, e.get("received_at") or e.get("occurred_at"))
        if not ok:
            skipped.append(f"{pack_id}（{why}）")
            continue
        meta = _pack_meta(p["pack_dir"])
        adapter = AUTO_ADAPTERS.get(p["platform"]) if p["publish_mode"] == "auto" else None
        if adapter:
            try:
                r = adapter(p, meta)  # type: ignore[operator]
                rec = {"platform": p["platform"], "platform_post_id": r.get("platform_post_id"), "url": r.get("url"),
                       "content_id": p["content_id"], "title": p["title"], "publish_mode": "auto",
                       "channel_code": meta.get("channel_code"), "post_id": f"{p['platform']}_{r.get('platform_post_id')}"}
                register_post(ops, rec)
                ops.execute("UPDATE publish_packs SET status='published', updated_at=? WHERE id=?", (iso(), pack_id))
                ledger.finish(f"publish:{pack_id}", "done", r)
                done.append(rec)
                continue
            except Exception as ex:  # noqa: BLE001
                ledger.finish(f"publish:{pack_id}", "failed", {"error": str(ex)[:300]})
                ops.execute("UPDATE publish_packs SET status='failed', updated_at=? WHERE id=?", (iso(), pack_id))
                skipped.append(f"{pack_id}（自动发布失败：{ex}）")
                continue
        ops.execute("UPDATE publish_packs SET status='approved', updated_at=? WHERE id=?", (iso(), pack_id))
        ledger.finish(f"publish:{pack_id}", "awaiting_manual")
        manual.append(_manual_instructions(p, meta))
    if manual:
        hub.notify("message", f"请手动发布 {len(manual)} 条（已批准）", "\n\n".join(manual), priority=1,
                   ref=f"manual-publish:{lease['task']['id']}", dedup=True)
    follow = [{"type": "social.post.published", "subject": f"post:{r['post_id']}", "summary": f"{r['platform']} 已发布「{r['title']}」",
               "data": r} for r in done]
    emit_result(f"自动发布 {len(done)} 条，推送人工发布 {len(manual)} 条，跳过 {len(skipped)} 条",
                follow_up_events=follow, skipped=skipped)


def record_manual(pack_id: str, url: str, platform_post_id: str | None = None) -> dict:
    ops = db.open_ops()
    p = ops.execute("SELECT * FROM publish_packs WHERE id=?", (pack_id,)).fetchone()
    if not p:
        raise SystemExit(f"找不到发布包 {pack_id}")
    p = dict(p)
    meta = _pack_meta(p["pack_dir"])
    ppid = platform_post_id or url.rstrip("/").split("/")[-1].split("?")[0]
    rec = {"platform": p["platform"], "platform_post_id": ppid, "url": url, "content_id": p["content_id"],
           "title": p["title"], "publish_mode": "semi_auto", "channel_code": meta.get("channel_code"),
           "post_id": f"{p['platform']}_{ppid}"[:80], "published_at": iso()}
    ops.execute("UPDATE publish_packs SET status='published', updated_at=? WHERE id=?", (iso(), pack_id))
    ActionLedger(ops).finish(f"publish:{pack_id}", "done", rec)
    return rec


def cmd_record() -> None:
    lease = load_task()
    recs = []
    for e in lease.get("events") or []:
        d = e.get("data") or {}
        if d.get("pack_id") and d.get("url"):
            recs.append(record_manual(d["pack_id"], d["url"], d.get("platform_post_id")))
    follow = [{"type": "social.post.published", "subject": f"post:{r['post_id']}", "summary": f"{r['platform']} 已发布「{r['title']}」",
               "data": r} for r in recs]
    emit_result(f"补登记 {len(recs)} 条人工发布", follow_up_events=follow)


if __name__ == "__main__":
    cmds = {"register": cmd_register, "batch-card": cmd_batch_card, "run": cmd_run, "record": cmd_record}
    fn = cmds.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if not fn:
        print(__doc__)
        sys.exit(2)
    fn()
