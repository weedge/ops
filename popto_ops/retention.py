"""数据保留期清理（08 §6 个人信息、§7 审计）：每天 04:30 由规则「数据保留清理」拉起，也可以手动试运行。

python -m popto_ops.retention run [--dry-run]

- 运行目录 runs/<任务>/：保留 retention.runs_days 天（默认 90，见 08 §7）。删之前，ops.db 里还引用着的文件
  （脚本、发布包、PRD）先挪到 content/_archived_runs/<任务>/，引用路径一并改写；
- WorkBuddy 收发件箱、发布截图、人工登录时的页面快照：同样按 runs_days；
- hub.db：事件、任务、卡片、通知的元数据（编号、类型、状态、时间、谁批的）永久保留；可能带评论原文的正文
  （任务摘要 / 输出 / 报错、运行报错、事件摘要 / 数据、通知标题 / 正文、卡片内容）超过 retention.text_days 天
  （默认 170）清空；ops.db 里需求信号的原话（signals.quote）同样处理；
- 评论原文本身由评论采集按 purge_after 删除（180 天），这里每天再兜一次底；
- 两个天数上限都是 170：周报会引用上一周的评论，留 10 天余量，保证评论原文的所有副本都在 180 天内删掉；
- 日志超过 retention.log_max_mb（默认 20）就轮转，保留 retention.log_keep 份旧的（默认 2）；
  launchd 以追加方式写日志，可以就地截断；
- 没打过码的运行目录（opsd 中途被杀、来不及扫）补扫一遍。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import config as cfgmod
from . import db, paths, redact
from .scriptkit import emit_result
from .timeutil import TZ, iso, now

MAX_DAYS = 170
ACTIVE = ("queued", "leased", "waiting_decision")
ARCHIVE = "_archived_runs"
RUN_NAME = re.compile(r"^t_(\d{8})_(\d{6})_")
OTHER_DIRS = ("workbuddy-inbox", "workbuddy-outbox", "publish-shots")

# hub.db 里要清空的正文：(表, 列, 时间列, 只清已结束的)
HUB_TEXT = [
    ("tasks", ("summary", "output", "error"), "COALESCE(updated_at, created_at)", "state IN ('done','failed','cancelled')"),
    ("runs", ("error_detail",), "COALESCE(finished_at, started_at)", "1=1"),
    ("events", ("summary", "data"), "COALESCE(last_seen_at, received_at)", "1=1"),
    ("outbox", ("title", "body", "error"), "created_at", "status<>'pending'"),
    ("cards", ("title", "payload", "reason"), "created_at", "status IN ('decided','expired')"),
]


def settings(cfg: dict) -> dict:
    r = cfg.get("retention") or {}

    def days(key: str, default: int) -> int:
        try:
            v = int(r.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(1, min(v, MAX_DAYS))

    def num(key: str, default: float) -> float:
        try:
            return float(r.get(key, default))
        except (TypeError, ValueError):
            return default

    return {"enabled": bool(r.get("enabled", True)), "runs_days": days("runs_days", 90),
            "text_days": days("text_days", MAX_DAYS), "log_max_mb": max(1.0, num("log_max_mb", 20)),
            "log_keep": max(1, int(num("log_keep", 2)))}


# ---------------------------------------------------------------- 运行目录
def run_created(d: Path) -> datetime:
    m = RUN_NAME.match(d.name)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=TZ)
        except ValueError:
            pass
    return datetime.fromtimestamp(d.stat().st_mtime, TZ)


def run_dirs(data: Path) -> list[Path]:
    root = data / "runs"
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir() if d.is_dir() and not d.is_symlink())


def task_states(hub, ids: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        q = f"SELECT id, state FROM tasks WHERE id IN ({','.join('?' * len(chunk))})"
        out.update({r[0]: r[1] for r in hub.execute(q, chunk)})
    return out


def text_columns(conn) -> list[tuple[str, str]]:
    cols = []
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        for r in conn.execute(f'PRAGMA table_info("{t}")'):
            if (r[2] or "").upper() in ("TEXT", ""):
                cols.append((t, r[1]))
    return cols


def _link_or_copy(src, dst) -> None:
    try:
        os.link(src, dst)  # 同一块盘：硬链接，不多占空间
    except OSError:
        shutil.copy2(src, dst)


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True, copy_function=_link_or_copy, dirs_exist_ok=True)
    elif not dst.exists():
        _link_or_copy(src, dst)


def archive_refs(ops, data: Path, run_dir: Path, *, dry_run: bool, cols: list[tuple[str, str]] | None = None) -> int:
    """ops.db 里还引用着这个运行目录里的文件：复制到 content/_archived_runs/<任务>/，引用改写过去。返回归档条目数。"""
    tid = run_dir.name
    needle = f"runs/{tid}/"
    target = f"content/{ARCHIVE}/{tid}/"
    pat = re.compile(re.escape(needle) + r"([^\s\"'\]\),;]+)")
    moved: set[str] = set()
    for table, col in cols if cols is not None else text_columns(ops):
        rows = ops.execute(f'SELECT rowid, "{col}" FROM "{table}" WHERE "{col}" LIKE ?', (f"%{needle}%",)).fetchall()
        for rowid, val in rows:
            for sub in {m.group(1).rstrip("/") for m in pat.finditer(str(val or ""))}:
                src = run_dir / sub
                if sub in moved or not src.exists():
                    continue
                if not dry_run:
                    _copy(src, data / target / sub)
                moved.add(sub)
            if not dry_run:
                ops.execute(f'UPDATE "{table}" SET "{col}"=? WHERE rowid=?', (str(val).replace(needle, target), rowid))
    return len(moved)


def purge_runs(hub, ops, data: Path, cutoff: datetime, *, dry_run: bool) -> dict:
    st = {"deleted": 0, "archived": 0, "kept_active": 0, "failed": 0}
    old = [d for d in run_dirs(data) if run_created(d) < cutoff]
    if not old:
        return st
    states = task_states(hub, [d.name for d in old])
    # 只看真的存过运行目录路径的列（现在是脚本路径、发布包目录、PRD 路径），省得每个目录把全库扫一遍
    cols = [(t, c) for t, c in text_columns(ops)
            if ops.execute(f'SELECT 1 FROM "{t}" WHERE "{c}" LIKE ? LIMIT 1', ("%runs/t_%",)).fetchone()]
    for d in old:
        if states.get(d.name) in ACTIVE:
            st["kept_active"] += 1
            continue
        try:
            st["archived"] += archive_refs(ops, data, d, dry_run=dry_run, cols=cols)
            if not dry_run:
                shutil.rmtree(d)
            st["deleted"] += 1
        except OSError:
            st["failed"] += 1
    return st


def _newest_mtime(p: Path) -> float:
    try:
        newest = p.lstat().st_mtime
    except OSError:
        return 0.0
    if p.is_dir() and not p.is_symlink():
        for dirpath, _dirs, files in os.walk(p, followlinks=False):
            for name in files:
                try:
                    newest = max(newest, os.lstat(Path(dirpath) / name).st_mtime)
                except OSError:
                    pass
    return newest


def purge_old_entries(root: Path, cutoff: datetime, *, dry_run: bool) -> int:
    """目录下按条目（文件或子目录）删：最新的文件都早于 cutoff 才删。"""
    if not root.is_dir():
        return 0
    n = 0
    for p in root.iterdir():
        if p.is_symlink() or _newest_mtime(p) >= cutoff.timestamp():
            continue
        if not dry_run:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        n += 1
    return n


# ---------------------------------------------------------------- 数据库正文
def blank_hub_text(hub, cutoff_iso: str, *, dry_run: bool) -> dict[str, int]:
    out = {}
    for table, cols, ts, cond in HUB_TEXT:
        where = f"{ts}<? AND {cond} AND ({' OR '.join(f'{c} IS NOT NULL' for c in cols)})"
        if dry_run:
            n = hub.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", (cutoff_iso,)).fetchone()[0]
        else:
            n = hub.execute(f"UPDATE {table} SET {', '.join(f'{c}=NULL' for c in cols)} WHERE {where}", (cutoff_iso,)).rowcount
        out[table] = n
    return out


def blank_ops_text(ops, cutoff_iso: str, *, dry_run: bool) -> dict[str, int]:
    sig_where = "created_at<? AND quote IS NOT NULL"
    com_where = "purge_after<?"
    if dry_run:
        sig = ops.execute(f"SELECT COUNT(*) FROM signals WHERE {sig_where}", (cutoff_iso,)).fetchone()[0]
        com = ops.execute(f"SELECT COUNT(*) FROM comments WHERE {com_where}", (iso(),)).fetchone()[0]
    else:
        sig = ops.execute(f"UPDATE signals SET quote=NULL WHERE {sig_where}", (cutoff_iso,)).rowcount
        com = ops.execute(f"DELETE FROM comments WHERE {com_where}", (iso(),)).rowcount
    return {"signals": sig, "comments": com}


# ---------------------------------------------------------------- 日志、打码
def rotate_logs(dirs: list[Path], max_bytes: int, keep: int, *, dry_run: bool) -> list[str]:
    rotated = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.log")):
            try:
                if p.is_symlink() or p.stat().st_size <= max_bytes:
                    continue
            except OSError:
                continue
            rotated.append(f"{d.name}/{p.name}")
            if dry_run:
                continue
            for i in range(keep, 1, -1):
                older = p.with_name(f"{p.name}.{i - 1}")
                if older.exists():
                    os.replace(older, p.with_name(f"{p.name}.{i}"))
            first = p.with_name(f"{p.name}.1")
            shutil.copy2(p, first)
            os.truncate(p, 0)  # 写日志的进程还开着这个文件（追加模式），不能换文件，只能截断
            redact.scrub_file(first)  # 轮转出来的旧日志不再有人写，可以打码
    return rotated


def scrub_pending(hub, data: Path, *, dry_run: bool) -> dict[str, int]:
    """没打过码、或打码后又有改动的运行目录补扫；正在跑的任务不碰（Agent 还在写文件）。"""
    dirs = run_dirs(data)
    states = task_states(hub, [d.name for d in dirs])
    out = {"dirs": 0, "files": 0, "hits": 0}
    for d in dirs:
        if states.get(d.name) == "leased" or not redact.needs_scrub(d):
            continue
        r = redact.scrub_tree(d, dry_run=dry_run)
        out["dirs"] += 1
        out["files"] += r["files"]
        out["hits"] += r["hits"]
    for sub in ("workbuddy-inbox", "workbuddy-outbox", "logs/browser-login"):
        r = redact.scrub_tree(data / sub, mark=False, dry_run=dry_run)
        out["files"] += r["files"]
        out["hits"] += r["hits"]
    return out


# ---------------------------------------------------------------- 入口
def run(*, dry_run: bool = False, cfg: dict | None = None) -> dict:
    s = settings(cfg if cfg is not None else cfgmod.load_agents_config())
    if not s["enabled"]:
        return {"summary": "数据保留清理已停用（retention.enabled=false），没有改动", "details": {}}
    data = paths.ops_data()
    t = now()
    runs_cut = t - timedelta(days=s["runs_days"])
    text_cut = iso(t - timedelta(days=s["text_days"]))
    hub, ops = db.open_hub(), db.open_ops()
    try:
        scrub = scrub_pending(hub, data, dry_run=dry_run)  # 先打码再归档：归档出去的副本是干净的
        runs = purge_runs(hub, ops, data, runs_cut, dry_run=dry_run)
        others = sum(purge_old_entries(data / sub, runs_cut, dry_run=dry_run) for sub in OTHER_DIRS)
        others += purge_old_entries(data / "logs" / "browser-login" / ".playwright-cli", runs_cut, dry_run=dry_run)
        hub_n = blank_hub_text(hub, text_cut, dry_run=dry_run)
        ops_n = blank_ops_text(ops, text_cut, dry_run=dry_run)
        rotated = rotate_logs([data / "logs", data / "notify"], int(s["log_max_mb"] * 1024 * 1024), s["log_keep"],
                              dry_run=dry_run)
    finally:
        hub.close()
        ops.close()

    parts = []
    if runs["deleted"] or runs["kept_active"] or runs["failed"]:
        p = f"删除 {s['runs_days']} 天前的运行目录 {runs['deleted']} 个"
        if runs["archived"]:
            p += f"（内容库还引用的 {runs['archived']} 份文件已挪到 content/{ARCHIVE}/）"
        if runs["kept_active"]:
            p += f"，{runs['kept_active']} 个任务还没结束先留着"
        if runs["failed"]:
            p += f"，{runs['failed']} 个删除失败"
        parts.append(p)
    if others:
        parts.append(f"收发件箱 / 发布截图 / 登录快照删 {others} 项")
    blanked = {k: v for k, v in hub_n.items() if v}
    if blanked:
        names = {"tasks": "任务", "runs": "运行记录", "events": "事件", "outbox": "通知", "cards": "卡片"}
        parts.append(f"hub.db 清空 {s['text_days']} 天前的正文：" + "、".join(f"{names[k]} {v}" for k, v in blanked.items()))
    if ops_n["signals"]:
        parts.append(f"需求信号原话 {ops_n['signals']} 条")
    if ops_n["comments"]:
        parts.append(f"过期评论 {ops_n['comments']} 条")
    if rotated:
        parts.append(f"日志轮转 {len(rotated)} 个（{'、'.join(rotated)}）")
    if scrub["hits"]:
        parts.append(f"补打码：{scrub['files']} 个文件 {scrub['hits']} 处（Cookie / token / 手机号）")
    summary = "；".join(parts) if parts else "没有到期的数据"
    if dry_run:
        summary = "（试运行，没有改动）" + summary
    details = {"settings": s, "scrub": scrub, "runs": runs, "other_entries": others, "hub_text": hub_n,
               "ops_text": ops_n, "rotated_logs": rotated, "dry_run": dry_run}
    return {"summary": summary, "details": details}


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        r = run(dry_run="--dry-run" in sys.argv[2:])
        emit_result(r["summary"], retention=r["details"])
    else:
        print(__doc__)
        sys.exit(2)
