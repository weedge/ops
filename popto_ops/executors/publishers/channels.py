"""视频号助手发布（channels.weixin.qq.com）。

页面要点（2026-11-10 实测）：
- 直接打开 /platform/post/create 会被跳回首页，要在首页点“发表视频”；
- 表单渲染在 wujie 微前端的 shadow DOM 里（Playwright 定位器能穿透）；
- 视频描述是 .input-editor（contenteditable），话题直接写 `#词 `；
- 短标题 6–16 字，只能用常规文字；
- “视频标注”下拉 → “含AI生成内容”；
- 视频没处理完时“发表”按钮带 weui-desktop-btn_disabled。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import NotLoggedIn, PublishError, PublishResult, browser, is_logged_in, open_until, stamp

HOME = "https://channels.weixin.qq.com/platform"


def short_title(title: str) -> str | None:
    """视频号短标题：6–16 字。符号只支持书名号、引号、冒号、加号、问号、百分号、摄氏度，逗号用空格代替（平台提示原文）。"""
    t = re.sub(r"[，,、；;。.！!]", " ", title or "")
    t = re.sub(r"[^\w\u4e00-\u9fff《》“”\"：:+?？%° ]", "", t)
    t = re.sub(r"\s+", " ", t).strip()[:16].strip()
    return t if len(t) >= 6 else None


def _shot(page, shots: Path, name: str, res: PublishResult) -> None:
    p = shots / f"channels-{stamp()}-{name}.png"
    try:
        page.screenshot(path=str(p))
        res.screenshots.append(str(p))
    except Exception:  # noqa: BLE001
        pass


def publish(pack: dict, meta: dict, *, dry_run: bool, shots: Path, headless: bool = True) -> PublishResult:
    res = PublishResult(ok=False, dry_run=dry_run)
    video = Path(pack["video"] or "")
    if not video.is_file():
        raise PublishError(f"视频不存在：{video}")
    with browser("wechat_channels", headless=headless, error_shots=shots) as ctx:
        page = ctx.new_page()
        try:
            open_until(page, HOME, lambda: page.get_by_role("button", name="发表视频"), what="发表视频")
        except PublishError:
            if not is_logged_in("wechat_channels", page.url):
                raise NotLoggedIn("视频号登录已失效：运行 opsctl publish login wechat_channels 重新扫码")
            raise
        page.get_by_role("button", name="发表视频").click(timeout=30000)
        page.locator("input[type=file]").first.wait_for(state="attached", timeout=60000)
        page.locator("input[type=file]").first.set_input_files(str(video))
        page.locator(".input-editor").first.wait_for(timeout=60000)
        page.wait_for_timeout(3000)

        # 描述 + 话题
        ed = page.locator(".input-editor").first
        ed.click()
        body = (pack.get("body") or "").strip()
        for i, line in enumerate(body.splitlines()):
            if i:
                page.keyboard.press("Enter")
            if line:
                page.keyboard.insert_text(line)
        topics = [t.lstrip("#").strip() for t in pack.get("topics") or [] if t.strip("# ")][:5]
        if topics:
            page.keyboard.press("Enter")
            for t in topics:
                page.keyboard.type(f"#{t} ", delay=40)
                page.wait_for_timeout(400)
        res.notes.append(f"描述已填，话题 {len(topics)} 个")

        # 短标题
        st = short_title(pack.get("title") or "")
        if st:
            page.get_by_placeholder("填写短标题有机会获得更多流量").fill(st)
            page.wait_for_timeout(800)
            warn = page.get_by_text("标题包含特殊字符")
            if warn.count() and warn.first.is_visible():
                page.get_by_placeholder("填写短标题有机会获得更多流量").fill("")
                res.notes.append(f"⚠️ 短标题「{st}」被平台判为含特殊字符，已留空")
            else:
                res.notes.append(f"短标题：{st}")
        else:
            res.notes.append("标题不满 6 字或全是符号，短标题留空")

        # 视频标注：含 AI 生成内容
        if pack.get("ai_label"):
            try:
                page.get_by_text("选择视频标注").click(timeout=10000)
                page.get_by_text("含AI生成内容", exact=True).click(timeout=10000)
                page.wait_for_timeout(800)
                res.notes.append("视频标注：含AI生成内容")
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"⚠️ AI 标注没设置成功：{str(e)[:120]}")
        res.notes.append("封面用平台自动生成的")

        # 等视频处理完（发表按钮可点）
        btn = page.get_by_role("button", name="发表", exact=True)
        for _ in range(100):
            if "disabled" not in (btn.first.get_attribute("class") or ""):
                break
            page.wait_for_timeout(3000)
        else:
            _shot(page, shots, "not-ready", res)
            raise PublishError("等了 5 分钟，“发表”按钮还不能点（视频可能还在处理）")
        page.locator(".input-editor").first.scroll_into_view_if_needed()
        _shot(page, shots, "filled-top", res)
        btn.first.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        _shot(page, shots, "filled-bottom", res)

        if dry_run:
            res.ok = True
            res.notes.append("试运行：已填好，停在“发表”前，没有发表")
            return res

        btn.first.click()
        try:
            page.wait_for_url(re.compile(r".*/platform/post/list.*"), timeout=180000)
        except Exception:  # noqa: BLE001
            _shot(page, shots, "after-publish", res)
            raise PublishError("点了发表，但没有跳到内容管理页，请到视频号助手确认是否发出")
        page.wait_for_timeout(3000)
        _shot(page, shots, "published", res)
        res.ok = True
        res.notes.append("已发表（视频号没有公开作品链接，需人工补登记或从后台导出数据）")
        return res
