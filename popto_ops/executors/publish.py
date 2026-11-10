"""发布执行器（05 §4）。

python -m popto_ops.executors.publish register     # content.pack.ready → 登记发布包
python -m popto_ops.executors.publish batch-card   # 17:00 → 今晚待发布 N 条，一张卡片
python -m popto_ops.executors.publish run          # content.publish.approved → 执行（按平台 off / dry_run / live）
python -m popto_ops.executors.publish record       # content.published.manual → 人发完后补登记

一期所有平台按“半自动”执行：执行器把发布包整理好推给人，人在手机 / 后台发完后回复
「已发 <发布包ID> <链接>」（或 opsctl published），执行器补登记并排定数据采集。
自动发布：executors/publishers/（Playwright 驱动创作者后台），按 agents.yaml 的 publishers.<平台>.mode 启用（off / dry_run / live）。
"""
from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

from .. import db
from .. import platforms as platforms_mod
from ..collectors.metrics import register_post
from ..scriptkit import emit_result, hub_client, load_task
from ..timeutil import iso, now, parse_iso
from . import ActionLedger

PLATFORM_LABEL = platforms_mod.LABELS


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
        for raw_pf in platforms:
            pf = platforms_mod.normalize(raw_pf)  # Agent 常写“抖音”：库里统一存 douyin
            pid = f"pk_{content}_{pf}"
            per = meta.get("per_platform") or {}
            pm = per.get(raw_pf) or per.get(pf) or {}
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
    from ..config import load_agents_config

    cfg = load_agents_config()
    mode_label = {"live": "自动发布", "dry_run": "自动发布·试运行（仍需人工发）", "off": "半自动（人工发）"}
    items, modes = [], set()
    for p in packs:
        mode = _publisher_mode(cfg, p["platform"], _pack_meta(p["pack_dir"]))
        modes.add(mode)
        t = ""
        if p["scheduled_at"]:
            try:
                t = parse_iso(p["scheduled_at"]).strftime("%H:%M")
            except ValueError:
                t = p["scheduled_at"]
        items.append({"platform": PLATFORM_LABEL.get(p["platform"], p["platform"]), "time": t,
                      "title": p["title"] or p["content_id"], "note": mode_label.get(mode, "半自动（人工发）"),
                      "pack_id": p["id"]})
    expires = now().replace(hour=22, minute=0, second=0)
    if expires <= now():
        expires = now() + timedelta(hours=2)
    card = {
        "kind": "publish_batch",
        "title": f"今晚 {len(packs)} 条内容待发布",
        "proposal": "全部发布（每条都已通过合规复核和视频质检，才会进入发布包）",
        "reasons": ["发布包目录里有标题、正文、话题、封面和渠道码，可在详情里逐条查看"],
        "risks": (["自动发布会直接发到正式账号；失败或登录失效时自动转人工，群里会提醒"] if "live" in modes else []) +
                 (["人工发布的平台需要你在手机上发，发完回复「已发 发布包ID 链接」以便回收数据"] if modes - {"live"} else []),
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


def _per_platform(meta: dict, platform: str) -> dict:
    """meta.json 里平台键可能是中文（Agent 写的“抖音”），按归一后的代码匹配。"""
    for k, v in (meta.get("per_platform") or {}).items():
        if platforms_mod.normalize(k) == platform:
            return v or {}
    return {}


def _pack_files(p: dict, meta: dict) -> list[Path]:
    """要随发布消息一起发进群的文件：封面、视频（手机上保存后直接发）。"""
    if not p.get("pack_dir"):
        return []
    d = Path(p["pack_dir"])
    pm = _per_platform(meta, p["platform"])
    media = meta.get("media") or {}
    names = [pm.get("cover") or media.get("cover"), media.get("video"), *(media.get("images") or [])]
    out = []
    for n in names:
        if n and (d / n).is_file() and (d / n) not in out:
            out.append(d / n)
    return out


def _manual_instructions(p: dict, meta: dict) -> str:
    pm = _per_platform(meta, p["platform"])
    lines = []
    if meta.get("is_test"):
        lines.append(f"> ⚠️ **测试内容，不要真的发布**：{meta.get('test_note') or ''}")
    lines.append(f"**{PLATFORM_LABEL.get(p['platform'], p['platform'])} · {p['title'] or p['content_id']}**（`{p['id']}`）")
    for k, label in (("account", "账号"), ("scheduled_at", "时间"), ("title", "标题")):
        v = pm.get(k) or meta.get(k)
        if v:
            lines.append(f"- {label}：{v[11:16] if k == 'scheduled_at' and len(str(v)) > 16 else v}")
    body = pm.get("body") or meta.get("body")
    topics = pm.get("topics") or meta.get("topics")
    if body or topics:
        lines += ["", "**正文（整段复制）**", "", str(body or "") + ("\n" + " ".join(topics) if topics else "")]
    if pm.get("pinned_comment"):
        lines += ["", f"**置顶评论**：{pm['pinned_comment']}"]
    if pm.get("ai_label") or meta.get("ai_label"):
        lines.append("- 发布时勾选「AI 生成内容」声明")
    files = _pack_files(p, meta)
    if files:
        lines.append("- 素材：见下方文件 " + "、".join(f"「{f.name}」" for f in files))
    lines += ["", f"发完回复：`已发 {p['id']} <链接>`"]
    return "\n".join(lines)


def _publisher_mode(cfg: dict, platform: str, meta: dict) -> str:
    """off / dry_run / live；测试内容最多试运行。"""
    mode = str(((cfg.get("publishers") or {}).get(platform) or {}).get("mode") or "off")
    if mode == "live" and meta.get("is_test"):
        return "dry_run"
    return mode if mode in ("off", "dry_run", "live") else "off"


def cmd_run() -> None:
    from ..config import load_agents_config
    from . import publishers as pubs

    lease = load_task()
    ops = db.open_ops()
    ledger = ActionLedger(ops)
    hub = hub_client()
    cfg = load_agents_config()
    done, manual, skipped, notes = [], [], [], []
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
        mode = _publisher_mode(cfg, p["platform"], meta)
        label = PLATFORM_LABEL.get(p["platform"], p["platform"])
        if mode in ("dry_run", "live") and pubs.module_for(p["platform"]):
            try:
                r = pubs.run(p, meta, dry_run=(mode == "dry_run"))
            except pubs.NotLoggedIn as ex:
                hub.notify("alert", f"{label}登录失效，改为人工发布", f"{ex}\n\n发布包 `{pack_id}` 已转人工发布。",
                           priority=1, ref=f"login:{p['platform']}", dedup=False)
                r = None
            except Exception as ex:  # noqa: BLE001
                hub.notify("message", f"{label}自动{'发布试运行' if mode == 'dry_run' else '发布'}失败，改为人工发布",
                           f"`{pack_id}`：{pubs.friendly_error(ex)}" + ("\n\n出错时的页面截图见下一条。" if getattr(ex, "screenshot", None) else ""),
                           priority=1, ref=f"pubfail:{pack_id}", dedup=True)
                if getattr(ex, "screenshot", None):
                    hub.notify("file", Path(ex.screenshot).name, ex.screenshot, priority=1,
                               ref=f"pubfail-shot:{pack_id}", dedup=True)
                r = None
            if r and r.dry_run:
                notes.append(f"{label}试运行：" + "；".join(r.notes))
                hub.notify("message", f"{label}自动发布试运行（没有发布）",
                           f"`{pack_id}` 已在{label}后台自动填好并停在发布前，截图见下方。\n\n" +
                           "\n".join(f"- {n}" for n in r.notes) + "\n\n试运行期间仍需人工发布（见下一条）。",
                           priority=1, ref=f"dryrun:{pack_id}", dedup=True)
                for sp in r.screenshots:
                    hub.notify("file", Path(sp).name, sp, priority=1, ref=f"dryrun-shot:{pack_id}:{Path(sp).name}", dedup=True)
            elif r and r.ok:
                rec = {"platform": p["platform"], "platform_post_id": r.platform_post_id, "url": r.url,
                       "content_id": p["content_id"], "title": p["title"], "publish_mode": "auto",
                       "channel_code": meta.get("channel_code"),
                       "post_id": f"{p['platform']}_{r.platform_post_id}" if r.platform_post_id else None}
                if r.platform_post_id or r.url:
                    rec["post_id"] = register_post(ops, {k: v for k, v in rec.items() if v is not None})
                ops.execute("UPDATE publish_packs SET status='published', updated_at=? WHERE id=?", (iso(), pack_id))
                ledger.finish(f"publish:{pack_id}", "done", {"url": r.url, "notes": r.notes})
                hub.notify("message", f"✅ {label}已自动发布「{p['title'] or p['content_id']}」",
                           ("链接：" + r.url if r.url else "没拿到作品链接，发完回复「已发 " + pack_id + " <链接>」补登记") +
                           "\n\n" + "\n".join(f"- {n}" for n in r.notes), priority=1, ref=f"published:{pack_id}", dedup=True)
                if rec.get("post_id") and (r.platform_post_id or r.url):
                    done.append(rec)
                continue
        ops.execute("UPDATE publish_packs SET status='approved', updated_at=? WHERE id=?", (iso(), pack_id))
        ledger.finish(f"publish:{pack_id}", "awaiting_manual")
        manual.append((p, meta))
    # 每个发布包一条消息，紧跟它的封面 / 视频文件（中台按入队顺序发）
    for i, (p, meta) in enumerate(manual, 1):
        hub.notify("message", f"请手动发布（{i}/{len(manual)}）{p['title'] or p['content_id']}", _manual_instructions(p, meta),
                   priority=1, ref=f"manual-publish:{p['id']}", dedup=True)
        for f in _pack_files(p, meta):
            hub.notify("file", f.name, str(f), priority=1, ref=f"manual-publish-file:{p['id']}:{f.name}", dedup=True)
    follow = [{"type": "social.post.published", "subject": f"post:{r['post_id']}", "summary": f"{r['platform']} 已发布「{r['title']}」",
               "data": r} for r in done]
    emit_result(f"自动发布 {len(done)} 条，推送人工发布 {len(manual)} 条（含素材文件），跳过 {len(skipped)} 条"
                + ("；" + "；".join(notes) if notes else ""), follow_up_events=follow, skipped=skipped)


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
