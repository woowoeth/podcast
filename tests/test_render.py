#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染层体检：真的打开页面，按读者会看见的东西下判据。

写这个的原因很具体。用户一轮里报了 11 个问题，**10 个在渲染层**：

  · 点播放整页往下跳 189px（hidden 被自己的 CSS 废掉）
  · 播放界面又黑又方、播放键掉到图片外面（aspect-ratio / inset 有兼容门槛）
  · 点播放没生效（新建 iframe 上的 autoplay 不算用户手势）
  · 「加载更多」是假的（IntersectionObserver 在某些环境不回调）
  · 人名和时间没底对齐、时长文字多余、模块贴着分割线
  · og:image 3.2 MB，微信抓成灰图；首页一次下 5.6 MB 图

那时候仓库里有 300 多项守护，**没有一项打开过真实页面**——161 处全是读源码和
产物做静态断言。静态断言查得了"代码里有没有这一行"，查不了"读者眼里是什么样"。
所以这些问题只能等人来报，而这正是用户不接受的那件事。

判据只写"读者会察觉"的那类，不写主观审美：
  ① 点主控件不许让页面跳（布局位移）
  ② 任何视口下不许横向溢出
  ③ 带 hidden 的元素 computed display 必须是 none
  ④ 该出现的图必须真的解码出来（naturalWidth > 0）
  ⑤ 点了主控件必须有可观测的 DOM 变化
  ⑥ 首屏传输体积有上限
  ⑦ 深浅两套主题、手机和桌面两个宽度都过

没装 playwright 就 skip，但**会大声说自己 skip 了**（healthcheck 里也报），
不然这层检查会静默消失——那和没有一样。
"""
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "/podcast"

try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except ImportError:                                    # pragma: no cover
    HAVE_PW = False

# 首屏传输上限，单位 KB，**gzip 之后**（本机服务器不压缩，测试自己压一遍，
# 否则和读者实际下的量差一个数量级）。
#
# 第一方和第三方必须分开算。混在一起量出来是 205 KB，其中 168 KB 是 Google
# Analytics 一个脚本——那样这条检查会永远红，而我为了让它过只会去抬阈值，
# 于是"首页别又变成一次下 288 KB"这个信号就彻底没了。
MAX_HOME_KB = 45          # 实测 35 KB（文档 17 + CSS 9 + JS 9）
MAX_EP_KB = 60
# 第三方给一个宽但有限的上限：不管现在挂着什么，再多挂一个重的嵌入要能被拦住。
MAX_THIRD_PARTY_KB = 220


class _Root(http.server.SimpleHTTPRequestHandler):
    """把仓库挂在 /podcast/ 下，和线上路径一致。"""

    def translate_path(self, path):
        # 必须 unquote：slug 是中文，URL 里是百分号编码的。父类会解码，
        # 覆盖了就得自己解——不解就永远 404，而 404 页上什么都测不到，
        # 测试会以"元素找不到"的样子失败，看不出真因。
        from urllib.parse import unquote
        path = unquote(path.split("?", 1)[0].split("#", 1)[0])
        if path.startswith(BASE):
            path = path[len(BASE):]
        rel = os.path.normpath(path).lstrip("/")
        full = os.path.join(ROOT, rel)
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        return full

    def log_message(self, *a):
        pass


def _episode_slugs(n=3, want_video=None):
    out = []
    d = os.path.join(ROOT, "data", "episodes")
    for f in sorted(os.listdir(d), reverse=True):
        if not f.endswith(".json"):
            continue
        with open(os.path.join(d, f), encoding="utf-8") as fh:
            ep = json.load(fh)
        if want_video is True and not ep.get("youtube_id"):
            continue
        if want_video is False and (ep.get("youtube_id") or not ep.get("audio")):
            continue
        if os.path.isdir(os.path.join(ROOT, "p", ep.get("slug", ""))):
            out.append(ep["slug"])
        if len(out) >= n:
            break
    return out


@unittest.skipUnless(HAVE_PW, "没装 playwright —— 渲染层这一层没跑，"
                              "跑 python3 -m pip install playwright "
                              "&& python3 -m playwright install chromium")
class Render(unittest.TestCase):
    srv = None
    port = 0

    @classmethod
    def setUpClass(cls):
        socketserver.TCPServer.allow_reuse_address = True
        cls.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Root)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.shutdown()
        cls.srv.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{BASE}{path}"

    def page(self, width=375, height=812, theme=None):
        ctx = self.browser.new_context(viewport={"width": width, "height": height},
                                       device_scale_factor=2)
        p = ctx.new_page()
        if theme:
            p.add_init_script(
                "try{localStorage.setItem('podcast-theme','%s')}catch(e){}" % theme)
        return p

    def test_plays_inline_even_when_the_api_script_is_blocked(self):
        """内容拦截器常把 youtube.com/iframe_api 当追踪脚本拦掉。拦法有两种，
        两种我都栽过：

          · 返回错误 → onerror 触发 → 我原来直接跳去 YouTube（用户："非要让我
            跳出去看"）
          · 返回 200 但**空 body** → onerror 不触发，window.YT 永远不出现，
            回调永远不跑，页面上挂一个空黑框，没有任何出口

        两种情况下 nocookie/embed 本身都是好的，所以正确的兜底是**普通 iframe
        内嵌播**，不是跳出站。这条分别模拟两种拦法，都要求页面上出现能播的
        iframe。"""
        slug = _episode_slugs(1, want_video=True)
        self.assertTrue(slug)
        for how in ("abort", "empty"):
            p = self.page()
            if how == "abort":
                p.route("**/iframe_api*", lambda route: route.abort())
            else:
                p.route("**/iframe_api*", lambda route: route.fulfill(
                    status=200, content_type="text/javascript", body=""))
            p.goto(self.url(f"/p/{slug[0]}/"), wait_until="load")
            p.click(".video-facade")
            p.wait_for_timeout(8000)          # 超时兜底是 6 秒
            got = p.evaluate("""() => {
              const f = document.querySelector('.vwrap iframe');
              const off = document.querySelector('.video-facade.offsite');
              return {inline: f ? f.src : null, wentOffsite: !!off};
            }""")
            p.context.close()
            self.assertFalse(got["wentOffsite"],
                             f"iframe_api 被{how}掉之后就跳出站了，不该这样")
            self.assertTrue(got["inline"] and "embed/" in got["inline"],
                            f"iframe_api 被{how}掉之后页面上没有能播的 iframe："
                            f"{got}")

    # ------------------------------------------------------ 英文站
    def test_english_edition_renders(self):
        """英文站真的打开来看：没有中英混排、语言切换能回中文、hreflang 三语齐。

        构建时的 enscan 是查 HTML 源码，这一条查**渲染出来的可见文字**——
        两者会漏的东西不一样（CSS 生成内容 ::before、JS 插进来的文案，
        源码扫不到）。"""
        en = os.path.join(ROOT, "en", "index.html")
        if not os.path.exists(en):
            self.skipTest("英文站还没建（PODCAST_EN=1 python3 pipeline/build.py）")
        for path in ("/en/", "/en/sources/", "/en/log/"):
            p = self.page()
            p.goto(self.url(path), wait_until="load")
            bad = p.evaluate("""() => {
              const cjk = /[\u4e00-\u9fff]/;
              const bad = [];
              const walk = el => {
                if (el.closest('[lang^="zh"]')) return;
                const cs = getComputedStyle(el);
                for (const pseudo of ['::before', '::after']) {
                  const c = getComputedStyle(el, pseudo).content;
                  if (c && c !== 'none' && cjk.test(c)) bad.push(pseudo + ' ' + c);
                }
              };
              document.querySelectorAll('body *').forEach(walk);
              document.querySelectorAll('body *').forEach(el => {
                if (el.children.length) return;
                if (el.closest('[lang^="zh"]')) return;
                const t = (el.textContent || '').trim();
                if (t && cjk.test(t)) bad.push(el.tagName + ': ' + t.slice(0, 40));
              });
              return [...new Set(bad)].slice(0, 5);
            }""")
            css_ok = p.evaluate(
                "() => getComputedStyle(document.documentElement)"
                ".getPropertyValue('--css').trim()")
            js_ok = p.evaluate(
                "() => !!document.querySelector('script[src*=\"site.js\"]')"
                " && [...document.querySelectorAll('script[src]')]"
                ".every(s => s.src.indexOf('/en/assets/') === -1)")
            langs = p.evaluate("""() => ({
              html: document.documentElement.lang,
              hreflang: [...document.querySelectorAll('link[rel=alternate][hreflang]')]
                          .map(l => l.hreflang).sort(),
              backToZh: !!document.querySelector('a[lang="zh"][href$="/podcast/"]'),
            })""")
            p.context.close()
            self.assertEqual(bad, [], f"{path} 上有中英混排：{bad}")
            # 样式和脚本必须真的加载上。英文站上线第一版的 asset() 用了英文的
            # BASE，指向 /podcast/en/assets/（不存在），整站裸奔——而那一版的
            # 这条测试只查文字，没查样式，所以放过去了。
            self.assertEqual(css_ok, "ok",
                             f"{path} 的样式表没生效（--css 探针取不到）")
            self.assertTrue(js_ok, f"{path} 的脚本没加载")
            self.assertEqual(langs["html"], "en", f"{path} 的 html lang 不对")
            self.assertIn("zh-Hans", langs["hreflang"], f"{path} 缺 hreflang")
            self.assertTrue(langs["backToZh"], f"{path} 没有回中文站的入口")

    def test_english_episode_quotes_are_the_original_words(self):
        """英文源节目的金句在页面上必须和中文站的 raw **逐字一致**。
        这是这个站的前提：读者点时间戳能核对到那一秒说的就是这句。"""
        import json as _j
        en_dir = os.path.join(ROOT, "data", "en")
        if not os.path.isdir(en_dir):
            self.skipTest("还没有译文")
        rec = None
        for f in sorted(os.listdir(en_dir)):
            if f.startswith("_") or not f.endswith(".json"):
                continue
            r = _j.load(open(os.path.join(en_dir, f), encoding="utf-8"))
            if r.get("source_lang") == "en" and os.path.isdir(
                    os.path.join(ROOT, "en", "p", r["slug"])):
                rec = r
                break
        if not rec:
            self.skipTest("没有可测的英文源译文")
        src = _j.load(open(os.path.join(ROOT, "data", "episodes",
                                        rec["slug"] + ".json"), encoding="utf-8"))
        want = [q.get("raw") for q in (src["digest"].get("quotes") or [])]
        p = self.page()
        p.goto(self.url(f"/en/p/{rec['slug']}/"), wait_until="load")
        got = p.evaluate("""() => [...document.querySelectorAll('.quote .raw')]
                                  .map(e => e.textContent.trim())""")
        p.context.close()
        self.assertEqual(got, [w for w in want if w][:len(got)],
                         "英文页上的金句和原话不一致——被回译了")

    # ------------------------------------------------------ ① 点了不许跳
    def test_clicking_play_does_not_shift_the_page(self):
        """用户："为啥点播放按钮整个界面会跳一下"。真因是 .video-facade{display:block}
        把 hidden 废掉了：假门没藏起来，播放器和它并排，卡片 192→381px。

        判据不是"有没有那条 CSS"（那是静态断言），是**点完之后页面高度和后面
        内容的位置有没有变**。任何以后再引入这类位移的改动都会被这条拦下。"""
        slugs = _episode_slugs(2, want_video=True)
        self.assertTrue(slugs, "没有带视频的单集页可测")
        for slug in slugs:
            for w in (375, 1280):
                p = self.page(width=w)
                p.goto(self.url(f"/p/{slug}/"), wait_until="load")
                before = p.evaluate("""() => {
                  const box = document.querySelector('[data-player-box]');
                  const after = document.querySelector('.section');
                  return {page: document.body.scrollHeight,
                          box: Math.round(box.getBoundingClientRect().height),
                          next: after ? Math.round(after.getBoundingClientRect().top) : 0};
                }""")
                p.click(".video-facade")
                p.wait_for_timeout(600)
                now = p.evaluate("""() => {
                  const box = document.querySelector('[data-player-box]');
                  const after = document.querySelector('.section');
                  return {page: document.body.scrollHeight,
                          box: Math.round(box.getBoundingClientRect().height),
                          next: after ? Math.round(after.getBoundingClientRect().top) : 0};
                }""")
                p.context.close()
                self.assertLessEqual(
                    abs(now["box"] - before["box"]), 4,
                    f"{slug} @{w}px：点播放后播放器卡片从 {before['box']} 变成 "
                    f"{now['box']}px")
                self.assertLessEqual(
                    abs(now["page"] - before["page"]), 8,
                    f"{slug} @{w}px：点播放后页面高度从 {before['page']} 变成 "
                    f"{now['page']}px，正文被推动了")

    def test_video_poster_is_16_by_9(self):
        """封面必须是 16:9。原来靠 aspect-ratio，而 img 上写了 height 属性会把它
        废掉（两边都定死时 aspect-ratio 不生效），16:9 的框退回 4:3，露出 YouTube
        缩略图自带的黑边——用户报的"播放界面能再丑一点么"。

        位移检查抓不到这个：封面和播放器**两边高度一起变**，差值仍然是 0。
        所以得单独量比例。我在反向验证里发现那条判据是空的，才加了这一条。"""
        for slug in _episode_slugs(2, want_video=True):
            for w in (375, 1280):
                p = self.page(width=w)
                p.goto(self.url(f"/p/{slug}/"), wait_until="load")
                r = p.evaluate("""() => {
                  const f = document.querySelector('.video-facade .frame');
                  const b = f.getBoundingClientRect();
                  return {w: b.width, h: b.height};
                }""")
                p.context.close()
                self.assertGreater(r["h"], 10, f"{slug} @{w}px 封面塌了")
                self.assertAlmostEqual(
                    r["w"] / r["h"], 16 / 9, delta=0.06,
                    msg=f"{slug} @{w}px 封面是 {r['w']:.0f}×{r['h']:.0f}"
                        f"（{r['w']/r['h']:.2f}），不是 16:9")

    # ------------------------------------------------- ② 不许横向溢出
    def test_no_horizontal_overflow(self):
        for path in ["/", "/sources/", "/log/"] + [f"/p/{s}/" for s in _episode_slugs(2)]:
            for w in (320, 375, 768, 1280):
                p = self.page(width=w)
                p.goto(self.url(path), wait_until="load")
                over = p.evaluate("""() => {
                  const bad = [];
                  document.querySelectorAll('body *').forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0) return;
                    if (r.right > window.innerWidth + 1 || r.left < -1) {
                      const cs = getComputedStyle(el);
                      if (cs.overflowX === 'auto' || cs.overflowX === 'scroll') return;
                      let a = el.parentElement, scroller = false;
                      while (a) { const c = getComputedStyle(a);
                        if (c.overflowX === 'auto' || c.overflowX === 'scroll') { scroller = true; break; }
                        a = a.parentElement; }
                      if (!scroller) bad.push(el.tagName + '.' + el.className +
                        ' [' + Math.round(r.left) + '→' + Math.round(r.right) + ']');
                    }
                  });
                  return {docWider: document.documentElement.scrollWidth > window.innerWidth + 1,
                          bad: bad.slice(0, 4)};
                }""")
                p.context.close()
                self.assertFalse(over["docWider"],
                                 f"{path} @{w}px 横向能滚：{over['bad']}")

    # --------------------------------------------- ③ hidden 必须真的藏住
    def test_hidden_elements_are_actually_hidden(self):
        """作者样式表排在 UA 表之后，所以 .foo{display:block} 会把
        [hidden]{display:none} 废掉。线上两处都栽在这上面（假门、自绘音频控件）。
        这条是在真浏览器里读 computed display，不是读 CSS 文本。"""
        paths = ["/"] + [f"/p/{s}/" for s in
                         _episode_slugs(2, want_video=True) + _episode_slugs(2, want_video=False)]
        for path in paths:
            p = self.page()
            # 关掉脚本：.aui 是**带着 hidden 出场**的，脚本一跑就把它 unhide 了，
            # 于是"hidden 有没有被 CSS 废掉"这件事在脚本跑完之后就看不见了。
            # 我第一版没关脚本，把 .aui[hidden] 的修复撤掉之后检查照样是绿的——
            # 那条判据当时是空的。
            p.route("**/site.js*", lambda route: route.abort())
            p.goto(self.url(path), wait_until="load")
            bad = p.evaluate("""() => {
              const bad = [];
              document.querySelectorAll('[hidden]').forEach(el => {
                if (getComputedStyle(el).display !== 'none')
                  bad.push(el.tagName + '.' + el.className + ' → ' +
                           getComputedStyle(el).display);
              });
              return bad;
            }""")
            p.context.close()
            self.assertEqual(bad, [], f"{path}：这些元素带 hidden 但没藏住 {bad}")

    # ------------------------------------------ ④ 该出现的图必须真解码
    def test_visible_images_actually_load(self):
        for path in ["/"] + [f"/p/{s}/" for s in _episode_slugs(1, want_video=True)]:
            p = self.page()
            p.goto(self.url(path), wait_until="load")
            p.evaluate("() => document.querySelectorAll('img').forEach(i=>i.loading='eager')")
            p.wait_for_timeout(2500)
            bad = p.evaluate("""() => {
              const bad = [];
              document.querySelectorAll('img').forEach(i => {
                const r = i.getBoundingClientRect();
                if (r.width > 40 && r.top < window.innerHeight * 2 && !i.naturalWidth)
                  bad.push(i.currentSrc || i.src);
              });
              return bad.slice(0, 3);
            }""")
            p.context.close()
            self.assertEqual(bad, [], f"{path}：这些图占了位置但没加载出来 {bad}")

    # ------------------------------------- ⑤ 点了主控件必须有可观测变化
    def test_primary_controls_do_something(self):
        """用户："点击播放器为啥只有声音没有视频"、"也没真正实现下拉加载更多"。
        两次都是"点了看起来没反应"。判据：点完 DOM 必须变。"""
        slug = _episode_slugs(1, want_video=True)
        if slug:
            p = self.page()
            p.goto(self.url(f"/p/{slug[0]}/"), wait_until="load")
            p.click(".video-facade")
            p.wait_for_timeout(800)
            got = p.evaluate("() => !!document.querySelector('.vwrap, .vfail')")
            p.context.close()
            self.assertTrue(got, "点了假门既没换成播放器也没给出口")

        p = self.page()
        p.goto(self.url("/"), wait_until="load")
        first = p.evaluate("() => document.querySelectorAll('[data-card]').length")
        p.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        p.wait_for_timeout(1500)
        after = p.evaluate("() => document.querySelectorAll('[data-card]').length")
        p.context.close()
        self.assertGreater(after, first,
                           f"滚到底没有加载更多（还是 {first} 张）")

    # ---------------------------------------------------- ⑥ 首屏别太重
    def test_first_paint_stays_light(self):
        for path, cap in (("/", MAX_HOME_KB),
                          (f"/p/{_episode_slugs(1)[0]}/", MAX_EP_KB)):
            p = self.page()
            blobs = []

            # 只算首屏必须下的三类：文档、样式、脚本。fetch/xhr 是按需的
            # （搜索索引、后续分页），把它们算进来会把"首屏"量成"全部"。
            def tally(resp, blobs=blobs):
                try:
                    if resp.request.resource_type in ("document", "stylesheet",
                                                      "script"):
                        mine = resp.url.startswith("http://127.0.0.1")
                        blobs.append((mine, resp.body()))
                except Exception:
                    pass

            p.on("response", tally)
            p.goto(self.url(path), wait_until="load")
            p.wait_for_timeout(400)
            p.context.close()
            import gzip
            def kbs(rows):
                return sum(len(gzip.compress(b, 6)) for _, b in rows) // 1024
            mine = kbs([x for x in blobs if x[0]])
            other = kbs([x for x in blobs if not x[0]])
            print(f"    {path} 首屏：自己的 {mine} KB · 第三方 {other} KB")
            self.assertLessEqual(mine, cap,
                                 f"{path} 我们自己的首屏资源 {mine} KB > {cap} KB")
            self.assertLessEqual(
                other, MAX_THIRD_PARTY_KB,
                f"{path} 第三方脚本 {other} KB > {MAX_THIRD_PARTY_KB} KB")

    # ------------------------------------------------ ⑦ 深色也得能看
    def test_dark_theme_has_no_invisible_text(self):
        """深色主题下如果某处颜色没跟着换，就会变成白底白字或黑底黑字。
        判据是文字和它背后的底色对比度不能低于 3:1。"""
        for theme in ("dark", "light"):
            for path in ["/", f"/p/{_episode_slugs(1)[0]}/"]:
                p = self.page(theme=theme)
                p.goto(self.url(path), wait_until="load")
                bad = p.evaluate("""() => {
                  const lum = c => { const m = c.match(/[\\d.]+/g).map(Number);
                    const f = v => { v/=255; return v <= .03928 ? v/12.92
                      : Math.pow((v+.055)/1.055, 2.4); };
                    return .2126*f(m[0]) + .7152*f(m[1]) + .0722*f(m[2]); };
                  const bg = el => { let a = el;
                    while (a) { const c = getComputedStyle(a).backgroundColor;
                      if (c && !/rgba\\(0, 0, 0, 0\\)|transparent/.test(c)) return c;
                      a = a.parentElement; }
                    return 'rgb(255,255,255)'; };
                  const bad = [];
                  document.querySelectorAll('p,h1,h2,h3,h4,li,td,a,span,b').forEach(el => {
                    if (!el.textContent.trim()) return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8 || r.top > window.innerHeight) return;
                    const cs = getComputedStyle(el);
                    if (cs.visibility === 'hidden' || cs.display === 'none') return;
                    if (parseFloat(cs.opacity) < .5) return;
                    const l1 = lum(cs.color), l2 = lum(bg(el));
                    const ratio = (Math.max(l1,l2)+.05)/(Math.min(l1,l2)+.05);
                    if (ratio < 3) bad.push(el.tagName + '.' + el.className +
                      ' ' + ratio.toFixed(2) + ':1');
                  });
                  return [...new Set(bad)].slice(0, 4);
                }""")
                p.context.close()
                self.assertEqual(bad, [],
                                 f"{theme} 主题 {path} 首屏这些文字对比度不足 {bad}")


if __name__ == "__main__":
    if not HAVE_PW:
        print("没装 playwright，渲染层这一层不会跑", file=sys.stderr)
    unittest.main()
