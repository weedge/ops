"""自有账号的指标：直接读创作者后台（复用自动发布的登录态），比公开接口准——
抖音公开接口拿不到自己作品的播放量（web 接口播放数是 0，app 接口对部分作品返回空）。

抖音：creator.douyin.com/janus/douyin/creator/pc/work_list（按 max_cursor 翻页，每页 12 条，无需签名）。
"""
from __future__ import annotations

WORK_LIST = ("https://creator.douyin.com/janus/douyin/creator/pc/work_list"
             "?status=0&count=12&max_cursor={cursor}&scene=star_atlas&device_platform=android&aid=1128")


def _metrics(st: dict) -> dict:
    return {"views": st.get("play_count"), "likes": st.get("digg_count"), "comments": st.get("comment_count"),
            "shares": st.get("share_count") if st.get("share_count") is not None else st.get("forward_count"),
            "favorites": st.get("collect_count")}


def douyin_stats(aweme_ids: set[str], max_pages: int = 15) -> dict[str, dict]:
    """返回 {aweme_id: metrics}；没登录 / 接口失败抛异常，由调用方降级到 TikHub。"""
    from ..executors.publishers import NotLoggedIn, browser, profile_dir

    if not any(profile_dir("douyin").iterdir()):
        raise NotLoggedIn("抖音创作者后台没登录（opsctl publish login douyin）")
    want, out, cursor = set(map(str, aweme_ids)), {}, 0
    with browser("douyin") as ctx:
        for _ in range(max_pages):
            r = ctx.request.get(WORK_LIST.format(cursor=cursor), timeout=30000)
            if r.status != 200:
                raise RuntimeError(f"创作者后台作品列表 HTTP {r.status}")
            d = r.json()
            if d.get("status_code") not in (0, None):
                raise NotLoggedIn(f"创作者后台返回 status_code={d.get('status_code')}（多半是登录失效）")
            for it in d.get("aweme_list") or []:
                aid = str(it.get("aweme_id"))
                if aid in want:
                    out[aid] = _metrics(it.get("statistics") or {})
            if want <= set(out) or not d.get("has_more"):
                break
            cursor = d.get("max_cursor") or 0
    return out
