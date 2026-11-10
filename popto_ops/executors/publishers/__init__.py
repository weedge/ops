"""创作者后台自动发布（Playwright + 本机 Chrome + 每个平台独立的登录态目录）。

- 登录：`opsctl publish login <平台>` 打开有界面的 Chrome，人扫码登录，登录态存在
  ~/popto-ops-data/browser-profiles/<平台>/，之后发布复用；
- 发布：只由 executors/publish.py 在卡片批准后调用；每个发布包只发一次（ActionLedger）；
- 试运行（dry_run）：上传、填文案都做，停在“发布”按钮前截图，不点发布。
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from ... import paths
from ...timeutil import now

PLATFORMS = {
    "douyin": {"label": "抖音", "home": "https://creator.douyin.com/creator-micro/home",
               "login": "https://creator.douyin.com/", "logged_in": ["/creator-micro/"],
               "session_cookies": ["sessionid", "sessionid_ss"]},  # 抖音没登录时停在原网址、白屏，只能看 Cookie
    "wechat_channels": {"label": "视频号", "home": "https://channels.weixin.qq.com/platform",
                        "login": "https://channels.weixin.qq.com/login.html", "logged_in": ["/platform"]},
    "xiaohongshu": {"label": "小红书", "home": "https://creator.xiaohongshu.com/new/home",
                    "login": "https://creator.xiaohongshu.com/login", "logged_in": ["/new/", "/creator/"]},
}


class PublishError(RuntimeError):
    pass


class NotLoggedIn(PublishError):
    pass


class ProfileBusy(PublishError):
    """同一平台的登录态目录同一时间只能开一个 Chrome（发布 / 数据采集 / 登录检查会抢）。"""


@dataclass
class PublishResult:
    ok: bool
    dry_run: bool
    url: str | None = None
    platform_post_id: str | None = None
    screenshots: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def profile_dir(platform: str) -> Path:
    d = paths.ops_data() / "browser-profiles" / platform
    d.mkdir(parents=True, exist_ok=True)
    return d


def shots_dir(pack_id: str) -> Path:
    d = paths.ops_data() / "publish-shots" / pack_id
    d.mkdir(parents=True, exist_ok=True)
    return d


@contextmanager
def profile_lock(platform: str, wait_s: float = 600):
    """按平台排队用登录态目录（跨进程文件锁）：发布、数据采集、登录检查不会同时开同一个平台的 Chrome。"""
    import fcntl

    lock = paths.ops_data() / "browser-profiles" / f".{platform}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as f:
        deadline = time.time() + max(0.0, wait_s)
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    label = PLATFORMS.get(platform, {}).get("label", platform)
                    raise ProfileBusy(f"{label}的浏览器正被别的任务占用（发布 / 数据采集 / 登录），等了 {int(wait_s)} 秒")
                time.sleep(1)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


@contextmanager
def browser(platform: str, *, headless: bool = True, error_shots: Path | None = None, lock_wait: float = 600):
    """本机 Chrome + 该平台的持久化登录态。出错时把当前页面截图到 error_shots（发给人看出错在哪一步）。"""
    from playwright.sync_api import sync_playwright

    with profile_lock(platform, lock_wait), sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile_dir(platform)), channel="chrome", headless=headless,
            viewport={"width": 1440, "height": 900}, locale="zh-CN", timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            yield ctx
        except Exception as e:
            if error_shots and ctx.pages:
                shot = error_shots / f"{platform}-{stamp()}-error.png"
                try:
                    ctx.pages[-1].screenshot(path=str(shot))
                    e.screenshot = str(shot)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            raise
        finally:
            ctx.close()


def open_until(page, url: str, ready, *, what: str, timeout_ms: int = 60000, retries: int = 1) -> None:
    """打开入口页并等关键元素出现；没等到就刷新重试（只用于点“发布”之前的步骤，重试不会重复发）。"""
    for attempt in range(retries + 1):
        try:
            if attempt == 0:
                page.goto(url, wait_until="commit", timeout=90000)
            else:
                page.reload(wait_until="commit", timeout=90000)
            ready().wait_for(timeout=timeout_ms)
            return
        except Exception:  # noqa: BLE001
            if attempt == retries:
                raise PublishError(f"页面 {timeout_ms // 1000} 秒没加载出「{what}」（已刷新重试 {retries} 次，多半是网络慢或页面改版）")


def friendly_error(e: Exception) -> str:
    """把 Playwright 的原始报错翻译成人能看懂的一句话。"""
    msg = str(e)
    if isinstance(e, PublishError):
        return msg
    m = re.search(r"waiting for (?:get_by_role\(\"\w+\", name=\"([^\"]+)\"|get_by_text\(\"([^\"]+)\"|get_by_placeholder\(\"([^\"]+)\")", msg)
    target = next((g for g in (m.groups() if m else ()) if g), None)
    if "Timeout" in msg:
        return f"等「{target}」超时（多半是网络慢或页面改版）" if target else "页面操作超时（多半是网络慢或页面改版）"
    return msg.splitlines()[0][:200]


def is_logged_in(platform: str, url: str, cookies: list[dict] | None = None) -> bool:
    cfg = PLATFORMS[platform]
    if not (any(k in url for k in cfg["logged_in"]) and "login" not in url):
        return False
    need = cfg.get("session_cookies")
    if need and cookies is not None:
        return any(c.get("name") in need and c.get("value") for c in cookies)
    return True


def login(platform: str, timeout_s: int = 300) -> bool:
    """打开有界面的 Chrome 让人扫码；登录成功（跳到后台首页）后保存登录态并关闭。"""
    cfg = PLATFORMS[platform]
    with browser(platform, headless=False) as ctx:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(cfg["login"], wait_until="domcontentloaded")
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if is_logged_in(platform, page.url, ctx.cookies()):
                page.wait_for_timeout(3000)  # 让 Cookie 落盘
                return True
            page.wait_for_timeout(1500)
    return False


def check_login(platform: str) -> tuple[bool, str]:
    """无界面打开后台首页，看会不会被踢回登录页。"""
    cfg = PLATFORMS[platform]
    with browser(platform) as ctx:
        page = ctx.new_page()
        page.goto(cfg["home"], wait_until="commit", timeout=90000)
        page.wait_for_timeout(6000)
        return is_logged_in(platform, page.url, ctx.cookies()), page.url


def stamp() -> str:
    return now().strftime("%H%M%S")


def payload_for(p: dict, meta: dict) -> dict:
    """发布包（publish_packs 行 + meta.json）→ 发布器要的字段，路径都转成绝对路径。"""
    from ..publish import _per_platform

    d = Path(p["pack_dir"]) if p.get("pack_dir") else None
    pm = _per_platform(meta, p["platform"])
    media = meta.get("media") or {}

    def f(name):
        return str(d / name) if d and name and (d / name).is_file() else None
    return {
        "pack_id": p["id"], "platform": p["platform"],
        "video": f(media.get("video")), "cover": f(pm.get("cover") or media.get("cover")),
        "images": [x for x in (f(n) for n in media.get("images") or []) if x],
        "title": pm.get("title") or p.get("title") or meta.get("title") or "",
        "body": pm.get("body") or meta.get("body") or "",
        "topics": pm.get("topics") or meta.get("topics") or [],
        "ai_label": bool(pm.get("ai_label", meta.get("ai_label", True))),
        "is_test": bool(meta.get("is_test")),
    }


def module_for(platform: str):
    import importlib

    name = {"douyin": "douyin", "wechat_channels": "channels", "xiaohongshu": "xiaohongshu"}.get(platform)
    if not name:
        return None
    try:
        return importlib.import_module(f"{__name__}.{name}")
    except ModuleNotFoundError:
        return None


def run(p: dict, meta: dict, *, dry_run: bool, headless: bool = True) -> PublishResult:
    mod = module_for(p["platform"])
    if not mod:
        raise PublishError(f"{p['platform']} 还没有自动发布器")
    pl = payload_for(p, meta)
    if pl["is_test"] and not dry_run:
        raise PublishError("测试内容不允许真发布（meta.is_test=true），只能试运行")
    return mod.publish(pl, meta, dry_run=dry_run, shots=shots_dir(p["id"]), headless=headless)

