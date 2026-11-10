"""抖音创作者中心发布（creator.douyin.com）。

页面要点（2026-11-10 实测）：
- 上传页 /content/upload 有 input[type=file]；上传后跳到 /content/post/video 或 /content/publish（灰度不同），
  所以以“作品标题”输入框出现为准，不看网址；
- 简介是富文本编辑器（.zone-container[contenteditable]），话题输入 `#词` 后按空格变成话题；
- 自主声明：点“请选择自主声明” → 单选“内容由AI生成”（点 label）→ 确定；
- 封面：竖封面 3:4 / 横封面 4:3 的“选择封面”弹窗；
- 底部“发布”“暂存离开”。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import NotLoggedIn, PublishError, PublishResult, browser, is_logged_in, open_until, stamp

UPLOAD = "https://creator.douyin.com/creator-micro/content/upload"
TITLE_PH = "填写作品标题"


def _shot(page, shots: Path, name: str, result: PublishResult) -> None:
    p = shots / f"douyin-{stamp()}-{name}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
        result.screenshots.append(str(p))
    except Exception:  # noqa: BLE001
        pass


def _dismiss(page) -> None:
    for name in ("我知道了", "知道了", "关闭"):
        b = page.get_by_role("button", name=name)
        if b.count():
            try:
                b.first.click(timeout=2000)
            except Exception:  # noqa: BLE001
                pass


def publish(pack: dict, meta: dict, *, dry_run: bool, shots: Path, headless: bool = True) -> PublishResult:
    """pack: video / cover / title / body / topics / ai_label。dry_run=True 时停在“发布”前。"""
    res = PublishResult(ok=False, dry_run=dry_run)
    video = Path(pack["video"])
    if not video.is_file():
        raise PublishError(f"视频不存在：{video}")
    with browser("douyin", headless=headless, error_shots=shots) as ctx:
        page = ctx.new_page()
        page.goto(UPLOAD, wait_until="commit", timeout=90000)
        page.wait_for_timeout(3000)
        if not is_logged_in("douyin", page.url, ctx.cookies()):
            raise NotLoggedIn("抖音登录已失效：运行 opsctl publish login douyin 重新扫码")
        open_until(page, UPLOAD, lambda: page.get_by_text("点击上传").first, what="点击上传")
        page.set_input_files("input[type=file]", str(video))
        page.get_by_placeholder(TITLE_PH).wait_for(timeout=180000)
        page.wait_for_timeout(2500)
        _dismiss(page)

        # 标题（≤30 字）
        title = (pack.get("title") or "")[:30]
        page.get_by_placeholder(TITLE_PH).fill(title)

        # 简介 + 话题
        ed = page.locator(".zone-container[contenteditable=true]").first
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
            page.keyboard.type("#" + t, delay=60)
            page.wait_for_timeout(1800)          # 等联想出来，空格确认为话题
            page.keyboard.press("Space")
            page.wait_for_timeout(500)
        res.notes.append(f"标题、简介已填，话题 {len(topics)} 个")

        # 自主声明：内容由 AI 生成
        if pack.get("ai_label"):
            try:
                page.get_by_text("请选择自主声明").click(timeout=10000)
                page.locator("label.semi-radio", has_text="内容由AI生成").click(timeout=10000)
                page.get_by_role("button", name="确定").click(timeout=10000)
                page.wait_for_timeout(1000)
                res.notes.append("已声明：内容由AI生成")
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"⚠️ AI 声明没设置成功：{str(e)[:120]}")
                page.keyboard.press("Escape")

        # 封面（失败不阻断：平台会用默认帧）
        cover = pack.get("cover")
        if cover and Path(cover).is_file():
            try:
                _set_cover(page, Path(cover))
                res.notes.append("封面已设置")
            except Exception as e:  # noqa: BLE001
                _shot(page, shots, "cover-failed", res)
                res.notes.append(f"封面没设置成功（用平台默认帧）：{str(e)[:120]}")
                _close_cover_modal(page)

        # 等上传完成
        for _ in range(60):
            txt = page.inner_text("body")
            if "上传中" not in txt and "上传失败" not in txt:
                break
            page.wait_for_timeout(3000)
        if "上传失败" in page.inner_text("body"):
            _shot(page, shots, "upload-failed", res)
            raise PublishError("视频上传失败")
        page.get_by_role("button", name="发布", exact=True).scroll_into_view_if_needed()
        page.wait_for_timeout(800)
        _shot(page, shots, "filled", res)

        if dry_run:
            res.ok = True
            res.notes.append("试运行：已填好，停在“发布”前，没有发布")
            return res

        created: dict = {}

        def on_resp(r):
            if "aweme" in r.url and "create" in r.url and r.request.method == "POST":
                try:
                    created.update(r.json())
                except Exception:  # noqa: BLE001
                    pass
        page.on("response", on_resp)
        page.get_by_role("button", name="发布", exact=True).click()
        try:
            page.wait_for_url(re.compile(r".*/content/manage.*"), timeout=180000)
        except Exception:  # noqa: BLE001
            _shot(page, shots, "after-publish", res)
            raise PublishError("点了发布，但没有跳到作品管理页，请到后台确认是否发出")
        page.wait_for_timeout(3000)
        _shot(page, shots, "published", res)
        aid = str(created.get("aweme_id") or (created.get("item_id")) or
                  ((created.get("aweme") or {}).get("aweme_id") if isinstance(created.get("aweme"), dict) else "") or "")
        res.platform_post_id = aid or None
        res.url = f"https://www.douyin.com/video/{aid}" if aid else None
        res.ok = True
        res.notes.append("已发布" + ("" if aid else "（没截到作品 ID，数据采集时会按标题在后台自动对上）"))
        if not aid:
            res.notes.append("发布接口返回：" + json.dumps(created, ensure_ascii=False)[:200])
        return res


def _set_cover(page, cover: Path) -> None:
    """封面弹窗：“设置竖封面”页签 →“上传封面”（弹系统选文件框）→ 可能有裁剪确认 →“完成”。"""
    page.get_by_text("选择封面").first.click(timeout=10000)
    page.get_by_text("设置竖封面").wait_for(timeout=15000)
    with page.expect_file_chooser(timeout=15000) as fc:
        page.locator("text=上传封面").first.click(force=True)   # 按钮里带“+”图标，精确文字匹配不到
    fc.value.set_files(str(cover))
    page.wait_for_timeout(4000)
    for name in ("确定", "确认"):           # 有的版本上传后先弹裁剪确认
        b = page.get_by_role("button", name=name, exact=True)
        if b.count() and b.first.is_visible():
            b.first.click()
            page.wait_for_timeout(1500)
    page.get_by_role("button", name="完成", exact=True).click(timeout=15000)
    page.wait_for_timeout(1500)
    later = page.get_by_role("button", name="暂不设置", exact=True)   # “设置横封面获更多流量”提示：竖版内容不设横封面
    if later.count() and later.first.is_visible():
        later.first.click()
        page.wait_for_timeout(1500)
    if page.get_by_text("设置竖封面").is_visible():
        raise PublishError("点了“完成”，封面弹窗还在")


def _close_cover_modal(page) -> None:
    for sel in ("[class*=close]", "[aria-label=close]", "[aria-label=关闭]"):
        try:
            loc = page.locator(sel).filter(has=page.locator("svg"))
            if page.get_by_text("设置竖封面").is_visible() and loc.count():
                loc.last.click(timeout=3000)
        except Exception:  # noqa: BLE001
            pass
