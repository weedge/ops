"""小红书创作服务平台发布（creator.xiaohongshu.com，视频笔记）。

页面要点（2026-11-10 实测）：
- /publish/publish?source=official 默认“上传视频”页签，有 input[type=file]（视频）；
- 标题输入框 placeholder “填写标题会有更多赞哦”（≤20 字）；正文是 tiptap / ProseMirror；
- 话题：输入 `#词` 后弹出联想（先“加载中”），回车选第一个；
- “添加内容类型声明”下拉 →“笔记含AI合成内容”；
- 底部“暂存离开”“发布”；视频上传中时“发布”是灰的。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import NotLoggedIn, PublishError, PublishResult, browser, is_logged_in, open_until, stamp

URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
TITLE_PH = "填写标题会有更多赞哦"


def _shot(page, shots: Path, name: str, res: PublishResult) -> None:
    p = shots / f"xhs-{stamp()}-{name}.png"
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
    with browser("xiaohongshu", headless=headless, error_shots=shots) as ctx:
        page = ctx.new_page()
        try:
            open_until(page, URL, lambda: page.get_by_text("拖拽视频到此或点击上传"), what="上传视频")
        except PublishError:
            if "login" in page.url:
                raise NotLoggedIn("小红书登录已失效：运行 opsctl publish login xiaohongshu 重新扫码")
            raise
        page.locator("input[type=file]").first.set_input_files(str(video))
        page.get_by_placeholder(TITLE_PH).wait_for(timeout=120000)
        page.wait_for_timeout(2500)
        for t in ("我知道了",):
            k = page.get_by_text(t, exact=True)
            if k.count() and k.first.is_visible():
                k.first.click()

        # 标题（≤20 字）
        title = re.sub(r"\s+", " ", pack.get("title") or "").strip()[:20]
        page.get_by_placeholder(TITLE_PH).fill(title)

        # 正文 + 话题
        ed = page.locator(".tiptap.ProseMirror").first
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
        ok_topics = 0
        for t in topics:
            page.keyboard.type("#" + t, delay=60)
            try:   # 等联想列表加载完再回车选第一个
                page.get_by_text("加载中").first.wait_for(state="hidden", timeout=8000)
                page.wait_for_timeout(600)
                page.keyboard.press("Enter")
                ok_topics += 1
            except Exception:  # noqa: BLE001
                page.keyboard.press("Space")
            page.wait_for_timeout(500)
        res.notes.append(f"标题、正文已填，话题 {ok_topics}/{len(topics)} 个识别为话题")

        # 内容类型声明：笔记含 AI 合成内容
        if pack.get("ai_label"):
            try:
                page.get_by_text("添加内容类型声明").click(timeout=10000)
                page.get_by_text("笔记含AI合成内容", exact=True).click(timeout=10000)
                page.wait_for_timeout(800)
                res.notes.append("已声明：笔记含AI合成内容")
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"⚠️ AI 声明没设置成功：{str(e)[:120]}")
                page.keyboard.press("Escape")
        res.notes.append("封面用视频第一帧（平台默认）")

        # 等视频上传完（“上传中”消失）
        for _ in range(100):
            if not page.get_by_text(re.compile(r"上传中\s*\d+%")).count():
                break
            page.wait_for_timeout(3000)
        else:
            _shot(page, shots, "uploading", res)
            raise PublishError("等了 5 分钟视频还在上传")
        page.wait_for_timeout(2000)
        page.get_by_placeholder(TITLE_PH).scroll_into_view_if_needed()
        _shot(page, shots, "filled-top", res)
        page.get_by_text("添加内容类型声明").or_(page.get_by_text("笔记含AI合成内容")).first.scroll_into_view_if_needed()
        _shot(page, shots, "filled-bottom", res)

        if dry_run:
            res.ok = True
            res.notes.append("试运行：已填好，停在“发布”前，没有发布")
            return res

        page.get_by_text("发布", exact=True).last.click()
        try:
            page.wait_for_url(re.compile(r".*/publish/success.*|.*/new/note-manager.*"), timeout=180000)
        except Exception:  # noqa: BLE001
            _shot(page, shots, "after-publish", res)
            raise PublishError("点了发布，但没有跳到成功页，请到小红书后台确认是否发出")
        page.wait_for_timeout(2000)
        _shot(page, shots, "published", res)
        res.ok = True
        res.notes.append("已发布（笔记链接需人工补登记或从后台导出数据）")
        return res
