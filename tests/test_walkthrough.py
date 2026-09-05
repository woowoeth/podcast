"""站在读者角度走一遍：搜索、筛选、加载更多、时间戳、分享、404。

为什么和 test_render.py 分开：那一层查的是「页面长得对不对」（布局位移、
对比度、封面比例、首屏体积），这一层查的是「点下去有没有发生该发生的事」。
两层的判据来源不同——渲染层来自他报过的界面问题，这一层来自我手动走查时
真正点过的路径。

这些检查原来是我在 scratchpad 里临时写的脚本，走完一遍就没了。**临时脚本
等于没有检查**：下一次退化没人会知道。所以它们必须在仓库里、在 CI 里跑。

写这一层时我自己错了三次，每次都"发现"了一个不存在的 bug，判据里都记下来了：
  · `.toast` 是 position:fixed，而 offsetParent 对 fixed 元素**恒为 null**
    ——于是量出"点分享没有任何反馈"，其实连复制失败的降级提示都是对的；
  · 搜索会把全部卡片补进 DOM，所以"清空后回到 24 张"是错的期望，
    正确的期望是"回到搜索前的可见集合"；
  · 分类筛选后可见数是那个分类自己的条目数（75），拿它和首屏的 24 比毫无意义。
"""
from __future__ import annotations

import os
import re
import sys
import unittest

# tests/ 没有 __init__.py（discover 以它为顶层），所以从仓库根跑
# `python3 -m unittest tests.test_walkthrough` 时 test_render 不在 path 上。
# 两种调用方式都要能用：discover 会自己加，直接指名不会。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_render import HAVE_PW, ROOT, Harness, _episode_slugs   # noqa: E402,F401

CJK = re.compile(r"[一-鿿]")


@unittest.skipUnless(HAVE_PW, "没装 playwright —— 读者走查这一层没跑，"
                              "跑 python3 -m pip install playwright "
                              "&& python3 -m playwright install chromium")
class Walkthrough(Harness):
    """复用 Harness（本地服务器 + 浏览器），判据是自己的。

    继承 Render 会把渲染层那 11 条在这里再跑一遍——多两分钟，零新信息。"""

    # ---------------------------------------------------------------- 搜索
    def test_search_narrows_and_clearing_restores(self):
        for w, h in ((390, 844), (1280, 900)):
            p = self.page(width=w, height=h)
            p.goto(self.url("/"), wait_until="load")
            vis = ("() => [...document.querySelectorAll('[data-card]')]"
                   ".filter(e => e.offsetParent).length")
            before = p.evaluate(vis)
            p.fill(".search input", "半导体")
            p.wait_for_timeout(500)
            narrowed = p.evaluate(vis)
            p.fill(".search input", "")
            p.wait_for_timeout(500)
            after = p.evaluate(vis)
            p.context.close()
            self.assertGreater(before, 0, f"{w}px 首屏一张卡片都没有")
            self.assertLess(narrowed, before,
                            f"{w}px 搜索没有筛掉任何东西（{before} → {narrowed}）")
            self.assertGreater(narrowed, 0, f"{w}px 搜「半导体」一条都没有")
            # 搜索会把全部卡片补进 DOM（要能搜到全站），所以清空后**不会**
            # 回到首屏那 24 张。正确的期望是"至少回到搜索前的量"。
            self.assertGreaterEqual(after, before,
                                    f"{w}px 清空搜索后没恢复（{narrowed} → {after}）")

    def test_a_search_with_no_hits_says_so(self):
        p = self.page(width=1280, height=900)
        p.goto(self.url("/"), wait_until="load")
        p.fill(".search input", "zzzxqv这个词不存在")
        p.wait_for_timeout=getattr(p, "wait_for_timeout")
        p.wait_for_timeout(500)
        vis = p.evaluate("() => [...document.querySelectorAll('[data-card]')]"
                         ".filter(e => e.offsetParent).length")
        msg = p.evaluate("""() => {
            const e = document.querySelector('.empty, [data-empty]');
            if (!e) return null;
            const cs = getComputedStyle(e), r = e.getBoundingClientRect();
            return (cs.display !== 'none' && cs.visibility !== 'hidden'
                    && r.height > 0) ? e.textContent.trim() : null; }""")
        p.context.close()
        self.assertEqual(vis, 0, "搜不存在的词还有卡片显示")
        self.assertTrue(msg, "搜不到时页面是空白的——没有一句话告诉读者发生了什么")

    # ---------------------------------------------------------------- 筛选
    def test_category_filter_selects_exactly_one_and_changes_the_set(self):
        p = self.page(width=1280, height=900)
        p.goto(self.url("/"), wait_until="load")
        chips = p.locator(".chip[aria-pressed=false]")
        n = chips.count()
        if not n:
            p.context.close()
            self.skipTest("首页没有分类按钮")
        # 按钮上自带条目数，筛完的可见数应当等于它——拿它和首屏的 24 比
        # 毫无意义（第一版就是这么错的：75 > 24 被判成"筛选没生效"）。
        label = chips.first.inner_text().strip()
        want = re.search(r"(\d+)\s*$", label)
        chips.first.click()
        p.wait_for_timeout(600)
        vis = p.evaluate("() => [...document.querySelectorAll('[data-card]')]"
                         ".filter(e => e.offsetParent).length")
        pressed = p.evaluate("() => document.querySelectorAll("
                             "'.chip[aria-pressed=true]').length")
        p.context.close()
        self.assertEqual(pressed, 1, f"点完有 {pressed} 个按钮是选中态，应当只有 1 个")
        self.assertGreater(vis, 0, f"选了「{label}」之后一张卡片都没有")
        if want:
            self.assertEqual(vis, int(want.group(1)),
                             f"「{label}」按钮上写着 {want.group(1)} 条，"
                             f"筛出来 {vis} 条")

    # ------------------------------------------------------------ 加载更多
    def test_the_default_view_does_not_fetch_the_whole_archive(self):
        """默认档滚到底**不许**把整个存档拉下来，而且要给出口。

        「最新」就是内联的那一批（近七天）。原来滚到底会一页页拉 cards-N.json
        ——那正是"每次打开网站太慢"的来源。判据两面都要：不许多拉，
        也不许静默 dead-end。
        """
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        n0 = p.evaluate("() => document.querySelectorAll('[data-card]').length")
        got = []
        p.on("request", lambda r: got.append(r.url) if "cards-" in r.url else None)
        for _ in range(3):
            p.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            p.wait_for_timeout(800)
        n1 = p.evaluate("() => document.querySelectorAll('[data-card]').length")
        end = p.evaluate("""() => { const e=document.querySelector('[data-feed-end]');
            if (!e) return null;
            const cs=getComputedStyle(e);
            return cs.display!=='none' ? e.textContent.trim() : null; }""")
        p.context.close()
        self.assertEqual(n1, n0, f"默认档滚到底又加载了卡片（{n0} → {n1}）")
        self.assertFalse(got, f"默认档滚到底去拉了分页文件：{got[:2]}")
        self.assertTrue(end, "默认档到底了没有出口提示——读者会以为站还在转")

    def test_a_category_loads_the_whole_archive(self):
        """选了分类必须补齐全部：只筛内联那批会让读者以为站上没有那篇。"""
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        n0 = p.evaluate("() => document.querySelectorAll('[data-card]').length")
        p.click(".chip[aria-pressed='false']")
        p.wait_for_timeout(2500)
        n1 = p.evaluate("() => document.querySelectorAll('[data-card]').length")
        p.context.close()
        self.assertGreater(n1, n0, f"选分类后没有补齐（还是 {n0} 张）")

    # -------------------------------------------------------------- 时间戳
    def test_clicking_a_timestamp_does_something(self):
        slug = _episode_slugs(1)[0]
        p = self.page(width=1280, height=900)
        p.goto(self.url(f"/p/{slug}/"), wait_until="load")
        ts = p.locator("[data-t], .ts").first
        if not ts.count():
            p.context.close()
            self.skipTest("这篇没有时间戳控件")
        y0 = p.evaluate("() => window.scrollY")
        ts.click()
        p.wait_for_timeout(700)
        got = p.evaluate("""() => ({
            y: window.scrollY,
            audio: (document.querySelector('audio') || {}).currentTime || 0,
            video: !!document.querySelector('.vwrap iframe')})""")
        p.context.close()
        moved = (abs(got["y"] - y0) > 20 or got["audio"] > 0 or got["video"])
        self.assertTrue(moved, f"点了时间戳什么也没发生：{got}")

    # ---------------------------------------------------------------- 分享
    def test_share_always_gives_feedback(self):
        """给权限和不给权限都必须有反馈。

        **不能用 offsetParent 判它可见**：`.toast` 是 position:fixed，
        offsetParent 对 fixed 元素恒为 null。我第一版就是这么量的，
        于是"发现"了一个不存在的 bug——分享连降级提示都是对的。
        """
        vis = """() => {
            const t = document.querySelector('.toast');
            if (!t) return null;
            const cs = getComputedStyle(t), r = t.getBoundingClientRect();
            return (cs.visibility !== 'hidden' && parseFloat(cs.opacity) > 0.5
                    && r.width > 0 && r.height > 0) ? t.textContent.trim() : null; }"""
        for perms, tag in (([], "不给剪贴板权限"), (["clipboard-write"], "给权限")):
            ctx = self.browser.new_context(viewport={"width": 1280, "height": 900},
                                           permissions=perms)
            p = ctx.new_page()
            p.goto(self.url("/"), wait_until="load")
            p.locator(".share-btn").first.click()
            p.wait_for_timeout(800)
            msg = p.evaluate(vis)
            ctx.close()
            self.assertTrue(msg, f"{tag}：点了分享没有任何反馈")

    # ------------------------------------------------- JS 注入的文案要跟语言
    def test_runtime_strings_are_english_on_the_english_site(self):
        """JS 写进 DOM 的字，「零漏译」那道闸扫不到。

        实测过的洞：site.js 里 10 处中文文案在英文站上原样显示——
        英文读者点一下分享就弹一句中文。
        """
        if not os.path.isdir(os.path.join(ROOT, "en")):
            self.skipTest("英文站还没建")
        ctx = self.browser.new_context(viewport={"width": 1280, "height": 900},
                                       permissions=["clipboard-write"])
        p = ctx.new_page()
        p.goto(self.url("/en/"), wait_until="load")
        p.locator(".share-btn").first.click()
        p.wait_for_timeout(800)
        toast = p.evaluate("() => (document.querySelector('.toast')||{}).textContent")
        ctx.close()
        self.assertTrue(toast, "英文站点分享没有反馈")
        self.assertIsNone(CJK.search(toast),
                          f"英文站的分享提示是中文：{toast!r}")

        slug = _episode_slugs(1)[0]
        if os.path.isdir(os.path.join(ROOT, "en", "p", slug)):
            p2 = self.page(width=1280, height=900)
            p2.goto(self.url(f"/en/p/{slug}/"), wait_until="load")
            labels = p2.evaluate("""() => [...document.querySelectorAll(
                '[aria-label], iframe[title]')].map(
                e => e.getAttribute('aria-label') || e.getAttribute('title'))
                .filter(Boolean)""")
            p2.context.close()
            bad = [x for x in labels if CJK.search(x)]
            self.assertFalse(bad, f"英文文章页这些 aria-label / title 是中文：{bad[:4]}")

    # ---------------------------------------------------------- 不许有报错
    def test_no_console_errors_on_the_main_paths(self):
        slug = _episode_slugs(1)[0]
        paths = ["/", "/sources/", f"/p/{slug}/", "/log/"]
        for sub in ("tw", "en"):
            if os.path.isdir(os.path.join(ROOT, sub)):
                paths.append(f"/{sub}/")
        for path in paths:
            errs: list[str] = []
            p = self.page(width=1280, height=900)
            p.on("console", lambda m: errs.append(m.text)
                 if m.type == "error" else None)
            p.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
            p.goto(self.url(path), wait_until="load")
            p.wait_for_timeout(900)
            p.context.close()
            self.assertFalse(errs, f"{path} 有 JS 报错：{errs[:3]}")
