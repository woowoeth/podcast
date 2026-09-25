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
# 内联从 24 张放大到 41 张（=「最新」那一档），首页自己的首屏从 41 KB 到
# 48 KB；换来的是默认视图**一个请求都不用发**（原来滚一下就要拉 35 KB 的
# cards-1.json）。上限抬到 56，不是抬到"够用就行"——再多就该问是不是又在
# 往首屏堆东西了。
MAX_HOME_KB = 56          # 实测 48 KB（文档 23 + CSS 11 + JS 10，均为 gzip 后）
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
class Harness(unittest.TestCase):
    """只有脚手架，不含判据。

    test_walkthrough.py 复用它。**不要让那一层去继承 Render**：
    继承会把这里的每条测试在那边再跑一遍（实测 11 条变 30 条，多花两分钟
    却一条新信息都没有）。
    """

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


class Render(Harness):
    """渲染层的判据：页面长得对不对。全部来自他实际报过的界面问题。"""

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
              // 回中文的入口现在在语言下拉里（三项一个控件），不再是独立链接
              backToZh: (() => {
                const s = document.getElementById('lang-toggle');
                if (!s) return false;
                if (s.tagName === 'SELECT')
                  return [...s.options].some(o => o.value === 'sc');
                return /[/]podcast[/]?$/.test(s.getAttribute('href') || '');
              })(),
            })""")
            # 口号在手机上必须一行，语言下拉的箭头必须真的画出来。
            # 两条都只有在浏览器里量才看得出：箭头那次是 CSS 简写把
            # background-image 重置了，源码里那条规则是对的。
            p.wait_for_timeout(600)
            chrome = p.evaluate("""() => {
              const sl = document.querySelector('.slogan');
              const sel = document.getElementById('lang-toggle');
              const lh = sl ? parseFloat(getComputedStyle(sl).lineHeight) : 0;
              const br = document.querySelector('.brand');
              const sd = document.querySelector('.mast-side');
              const fam = el => el ? getComputedStyle(el)
                .fontFamily.split(',')[0].replace(/"/g, '') : null;
              const h1 = document.querySelector('.brand h1, .brand .wordmark, .card h3');
              // 只看真正的正文元素。.slogan 也算（字标下那句是句子，不是控件）
              const bd = document.querySelector('.card .dek, .point .body, .slogan');
              return {
                lines: sl && lh ? Math.round(sl.getBoundingClientRect().height / lh) : 0,
                arrow: sel ? getComputedStyle(sel).backgroundImage !== 'none' : false,
                isSelect: sel ? sel.tagName === 'SELECT' : false,
                mastWrapped: (br && sd)
                  ? Math.abs(br.getBoundingClientRect().top
                             - sd.getBoundingClientRect().top) > 8 : false,
                h1Font: fam(h1), bodyFont: fam(bd),
              };
            }""")
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
            self.assertLessEqual(chrome["lines"], 1,
                                 f"{path} 的口号在 375px 上折了 {chrome['lines']} 行")
            if chrome["isSelect"]:
                self.assertTrue(chrome["arrow"], f"{path} 语言下拉没有箭头")
            # 字标行不许换行：.mast-side 一换行就把 .mast-side 撑成两行，
            # 而 .mast-top 是 align-items:flex-end，字标被压到第二行去
            # ——用户报过两次"英文还是会换行"。
            self.assertFalse(chrome["mastWrapped"],
                             f"{path} 的字标行换行了")
            # 字体必须真的加载上，而且解析到指定的那两个
            self.assertEqual(chrome["h1Font"], "Playfair Display",
                             f"{path} 标题字体是 {chrome['h1Font']}")
            self.assertEqual(chrome["bodyFont"], "Source Serif 4",
                             f"{path} 正文字体是 {chrome['bodyFont']}")

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
            # **等到站点自己声明的那个上限。** site.js 里的兜底是 6 秒：
            # 拿不到 iframe_api 就换普通 embed。原来这里只等 800ms——
            # 本机网络快，API 一下就到，所以本机永远过；CI 的机房 IP 拿不到
            # 那个脚本，6 秒兜底还没触发，于是 CI 里一直是红的。
            # 而那条红一直没人看见：ci.yml 的测试步骤是 `| tail -40` 且
            # **没有 pipefail**，退出码取的是 tail 的，永远 0。
            deadline = 7000
            try:
                p.wait_for_selector(".vwrap, .vfail", timeout=deadline)
                got = True
            except Exception:
                got = p.evaluate(
                    "() => !!document.querySelector('.vwrap, .vfail')")
            state = p.evaluate("""() => ({
                facade: !!document.querySelector('.video-facade'),
                wrap: !!document.querySelector('.vwrap'),
                fail: !!document.querySelector('.vfail'),
                yt: !!(window.YT && window.YT.Player)})""")
            p.context.close()
            self.assertTrue(got, f"点了假门 {deadline}ms 内既没换成播放器也没给"
                                 f"出口（site.js 声明的兜底是 6 秒）：{state}")

        # 「最新」滑到底只取到这一档的最后一篇，不会把整个存档拉下来。
        # 所以这一条改在**分类档**上验——那才是"补齐全部"的场景。
        p = self.page()
        p.goto(self.url("/"), wait_until="load")
        first = p.evaluate("() => document.querySelectorAll('[data-card]').length")
        p.click("[data-cat-chip]:not([data-cat-chip='new']):not([data-cat-chip='hot'])")
        p.wait_for_timeout(2000)
        after = p.evaluate("() => document.querySelectorAll('[data-card]').length")
        p.context.close()
        self.assertGreater(after, first,
                           f"选了分类之后没有补齐全部（还是 {first} 张）——"
                           f"只筛内联那批会让读者以为站上没有那篇")

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

    # ------------------------------------------------ ⑦ 两套主题都得能看清
    def test_text_meets_wcag_aa_in_both_themes(self):
        """判据是 WCAG AA：正文 4.5:1，大字（≥24px 或 ≥18.66px 加粗）3:1。

        原来这一条的判据是 3:1，用意只是抓"某个颜色没跟着主题换"（白底白字）。
        那个目的它达到了，但 3:1 放过了真正难读的一档：走查时实测浅色主题下
        有 **76 处**在 4.5 以下，最差 3.97——全是 11-13px 的日期、篇数、角标，
        用的都是 --faint。小字加低对比，"差一点"就是看不清。

        两处必须做对，不然量出来的是假数（这两个坑我都踩了）：
        · **底色要逐层合成 alpha。** 吸顶条底色带 0.88 透明度，不合成会把
          浅色底算成近黑，量出 3.05:1（真值 4.88）。而 color(srgb 0.95 …)
          的分量是 0-1 不是 0-255，读错同样会算成近黑。
        · **主题要在加载前定下来。** 站里的脚本自己写 data-theme，加载后再改
          会跟它抢，getComputedStyle 还可能读到改之前的值——上一版就这样
          量出 12 处假的低对比。所以设完要断言真的设上了。
        """
        js = """() => {
          const px = c => {
            const srgb = /^color\\(\\s*srgb/i.test(c);
            const m = (c.match(/[\\d.]+/g) || [0,0,0]).map(Number);
            const k = srgb ? 255 : 1;
            return [(m[0]||0)*k, (m[1]||0)*k, (m[2]||0)*k, m.length>3 ? m[3] : 1];
          };
          const over = (f,b) => { const a=f[3];
            return [f[0]*a+b[0]*(1-a), f[1]*a+b[1]*(1-a), f[2]*a+b[2]*(1-a), 1]; };
          const lum = c => { const f=v=>{v/=255;
            return v<=.03928 ? v/12.92 : Math.pow((v+.055)/1.055,2.4);};
            return .2126*f(c[0])+.7152*f(c[1])+.0722*f(c[2]); };
          const bgOf = el => { const st=[]; let n=el;
            while(n){ const c=px(getComputedStyle(n).backgroundColor);
              if(c[3]>0) st.push(c); n=n.parentElement; }
            st.push([255,255,255,1]);
            let acc=st[st.length-1];
            for(let i=st.length-2;i>=0;i--) acc=over(st[i],acc);
            return acc; };
          const R=(f,b)=>{const a=lum(f),c=lum(b);
            return (Math.max(a,c)+.05)/(Math.min(a,c)+.05)};
          const bad=[];
          for (const el of document.querySelectorAll('*')) {
            const cs=getComputedStyle(el);
            if (cs.visibility==='hidden' || cs.display==='none') continue;
            if (parseFloat(cs.opacity) < 0.5) continue;
            const r0=el.getBoundingClientRect();
            if (r0.width<4 || r0.height<4) continue;
            const t=[...el.childNodes].filter(n=>n.nodeType===3)
                     .map(n=>n.textContent.trim()).join('');
            if (t.length<2) continue;
            const bg=bgOf(el);
            const r=R(over(px(cs.color), bg), bg);
            const size=parseFloat(cs.fontSize);
            const bold=parseInt(cs.fontWeight)>=700;
            const need=(size>=24 || (size>=18.66 && bold)) ? 3 : 4.5;
            if (r < need-0.005)
              bad.push(el.tagName+'.'+(el.className||'').toString().slice(0,20)+
                       ' '+r.toFixed(2)+'/'+need+' '+size+'px '+
                       JSON.stringify(t.slice(0,20)));
          }
          return [...new Set(bad)].slice(0, 6);
        }"""
        slug = _episode_slugs(1)[0]
        paths = ["/", "/sources/", f"/p/{slug}/", "/log/"]
        for sub in ("tw", "en"):
            if os.path.isdir(os.path.join(ROOT, sub)):
                paths += [f"/{sub}/", f"/{sub}/p/{slug}/"]
        for theme in ("light", "dark"):
            for path in paths:
                p = self.page(width=1280, height=900, theme=theme)
                p.goto(self.url(path), wait_until="load")
                p.evaluate("t => document.documentElement.setAttribute('data-theme', t)",
                           theme)
                p.wait_for_timeout(200)
                got = p.evaluate(
                    "() => document.documentElement.getAttribute('data-theme')")
                bad = p.evaluate(js)
                p.context.close()
                self.assertEqual(got, theme,
                                 f"{path} 的主题没设上（想要 {theme}，实际 {got}）"
                                 f"——量出来的数是假的")
                self.assertEqual(bad, [],
                                 f"{theme} 主题 {path} 这些文字不到 WCAG AA：{bad}")


if __name__ == "__main__":
    if not HAVE_PW:
        print("没装 playwright，渲染层这一层不会跑", file=sys.stderr)
    unittest.main()


class SearchMatchesWhatTheCardSays(Harness):
    """搜索要能搜到卡片上写着的字，也能搜到卡片上没写的原标题。

    为什么要在浏览器里敲一遍：卡片的搜索文本原来整份抄在 data-hay 属性里，
    63 张卡抄一遍标题+摘要+源名+标签，全是压不掉的独有文本，
    首屏 gzip 因此顶到 56.9 KB（上限 56）。
    改成属性只留**看不见的两样**（英文原标题、术语），可见的由 site.js
    现从 DOM 读 —— 首屏降到 54.3 KB。

    这是**运行时行为**的改动：静态断言看不见它对不对，只有真敲一个词才知道。
    两头都要验：摘要里的词（现在来自 DOM）和原标题里的词（仍来自属性）。
    """

    def _first_card(self, p):
        return p.evaluate("""() => {
          const c = document.querySelector('[data-card]');
          if (!c) return null;
          const t = s => (c.querySelector(s) || {}).textContent || '';
          return {href: c.getAttribute('href'),
                  dek: t('.dek'), title: t('h2'), src: t('.src'),
                  hay: c.getAttribute('data-hay') || ''};
        }""")

    def _block_deep_index(self, p):
        """把懒加载的 search.json 掐掉，只留内联那条路。

        **第一版没掐，于是这道闸是假绿的。** 把 site.js 改回「只读属性」
        注入缺陷之后闸照样过 —— 因为敲第一个字就会去拉 search.json，
        350ms 内它已经落地接管，我量到的是深索引的结果，
        不是我改的那条内联路径。验之前先确认量的是要量的那个东西。
        """
        p.route("**/search.json*", lambda route: route.abort())

    def _visible_after(self, p, term):
        p.fill("[data-search]", term)
        p.wait_for_timeout(350)
        # 分批露出：匹配的卡可能排在第二批之后，先全部露出来再看
        p.evaluate("() => document.querySelector('[data-feed]')._reveal(1e6)")
        return p.evaluate("""() => Array.from(document.querySelectorAll('[data-card]'))
            .filter(c => c.offsetParent !== null).map(c => c.getAttribute('href'))""")

    def test_a_word_only_in_the_summary_still_matches(self):
        p = self.page()
        self._block_deep_index(p)
        p.goto(self.url("/"), wait_until="load")
        card = self._first_card(p)
        self.assertTrue(card and card["dek"], "首页第一张卡没有摘要，判据失效")
        # 挑一个只在摘要里、不在标题里的词
        words = [w for w in re.findall(r"[一-鿿]{3,6}", card["dek"])
                 if w not in card["title"]]
        self.assertTrue(words, "摘要里找不到标题没有的词，判据失效")
        hit = self._visible_after(p, words[0])
        self.assertIn(card["href"], hit,
                      f"搜摘要里的「{words[0]}」搜不到这张卡 —— "
                      f"可见文字没有进搜索池")

    def test_a_word_only_in_the_attribute_still_matches(self):
        """英文原标题在卡片上不显示，只在 data-hay 里 —— 它也要能搜到。"""
        p = self.page()
        self._block_deep_index(p)
        p.goto(self.url("/"), wait_until="load")
        target = p.evaluate("""() => {
          for (const c of document.querySelectorAll('[data-card]')) {
            const hay = c.getAttribute('data-hay') || '';
            const seen = ['h2', '.dek', '.src'].map(s =>
              (c.querySelector(s) || {}).textContent || '').join(' ');
            const w = hay.split(/\\s+/).filter(x =>
              /^[A-Za-z][A-Za-z-]{5,}$/.test(x) && !seen.includes(x));
            if (w.length) return {href: c.getAttribute('href'), word: w[0]};
          }
          return null;
        }""")
        if not target:
            self.skipTest("首屏这批卡里没有「只在属性里」的词")
        hit = self._visible_after(p, target["word"])
        self.assertIn(target["href"], hit,
                      f"搜只在 data-hay 里的「{target['word']}」搜不到这张卡")

    def test_a_word_in_nothing_matches_nothing(self):
        """反面：乱敲一个词不该留下卡片。只验「该搜到的搜到」等于没验。"""
        p = self.page()
        self._block_deep_index(p)
        p.goto(self.url("/"), wait_until="load")
        self.assertEqual([], self._visible_after(p, "zzqqxx不存在的词"),
                         "搜一个不存在的词还有卡片留着 —— 筛选根本没生效")


class TheEssentialTabShowsOnlyCoreSources(Harness):
    """「必看」只显示核心源（当前 tier 1）的内容。

    两件事都要真点一遍，静态断言看不见：
    一是**筛出来的每一张都带 data-core**，二是**筛的是全站不是内联那批**
    ——「最新」那一档就是内联的那批，而「必看」是筛子，
    只筛前 60 张会让读者以为站上没有那篇。
    """

    def _click(self, p, chip):
        p.click(f'[data-cat-chip="{chip}"]')
        p.wait_for_timeout(1200)
        p.evaluate("() => document.querySelector('[data-feed]')._reveal(1e6)")
        return p.evaluate("""() => {
          const v = Array.from(document.querySelectorAll('[data-card]'))
            .filter(c => c.offsetParent !== null);
          return {n: v.length, allCore: v.every(c => c.hasAttribute('data-core'))};
        }""")

    def test_it_filters_to_core_and_nothing_else(self):
        p = self.page()
        p.goto(self.url("/"), wait_until="load")
        got = self._click(p, "core")
        self.assertGreater(got["n"], 0, "点「必看」一张卡都没有")
        self.assertTrue(got["allCore"], "「必看」里混进了非核心源的卡")

    def test_it_reaches_past_the_inline_batch(self):
        """必看有 178 篇，而首屏只内联 60 张 —— 它必须去把其余的拉回来。"""
        p = self.page()
        p.goto(self.url("/"), wait_until="load")
        inline = p.evaluate(
            "() => document.querySelectorAll('[data-card][data-core]').length")
        got = self._click(p, "core")
        self.assertGreater(got["n"], inline,
                           f"只筛了内联那批（{inline} 张）—— "
                           f"读者会以为站上只有这么多")

    def test_the_chip_count_matches_what_it_shows(self):
        p = self.page()
        p.goto(self.url("/"), wait_until="load")
        said = int(p.inner_text('[data-cat-chip="core"] .n'))
        got = self._click(p, "core")
        self.assertEqual(said, got["n"],
                         f"chip 上写 {said}，筛出来 {got['n']} —— 数对不上")


class TabsRevealInBatches(Harness):
    """每一档先露一批，滑到底再露下一批（2026-09-25 用户：「每个 tab 下面都要滑动好久」）。

    三件事都得真滑一遍：一开始只露一批；滑到底会露下一批、最后能露完；
    「最新」只取到这一档的最后一篇，不借机把整个存档拉下来。
    """

    VIS = """() => Array.from(document.querySelectorAll('[data-card]'))
               .filter(c => c.offsetParent !== null).length"""

    def _scroll_until_done(self, p, rounds=40):
        for _ in range(rounds):
            p.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            p.wait_for_timeout(250)
            left = p.evaluate("""() => { const b = document.querySelector('[data-more-left]');
                return b && !b.hidden ? +b.querySelector('[data-left]').textContent : 0; }""")
            if not left:
                return True
        return False

    def test_a_tab_starts_with_one_batch_and_says_how_many_are_left(self):
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        p.click('[data-cat-chip="core"]')
        p.wait_for_timeout(1500)
        step = p.evaluate("() => +document.querySelector('[data-feed]').getAttribute('data-page-size')")
        vis = p.evaluate(self.VIS)
        matched = p.evaluate("() => +document.querySelector('[data-feed]').getAttribute('data-matched')")
        left = p.evaluate("() => document.querySelector('[data-more-left]').innerText")
        p.context.close()
        self.assertGreater(matched, step, "必看不到一批，这条判据此刻无效")
        self.assertLessEqual(vis, step, f"一点进来就露了 {vis} 张 —— 没有分批")
        self.assertIn(str(matched - vis), left, f"底下没说还剩几篇：{left!r}")

    def test_scrolling_reveals_every_batch_until_the_chip_count(self):
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        said = int(p.inner_text('[data-cat-chip="core"] .n'))
        p.click('[data-cat-chip="core"]')
        p.wait_for_timeout(1500)
        first = p.evaluate(self.VIS)
        done = self._scroll_until_done(p)
        last = p.evaluate(self.VIS)
        p.context.close()
        self.assertTrue(done, "滑了 40 次还没露完 —— 滑到底不露下一批")
        self.assertGreater(last, first, "滑到底没有露出下一批")
        self.assertEqual(said, last, f"chip 上写 {said}，全露完是 {last}")

    def test_the_newest_tab_pages_only_its_own_window(self):
        """「最新」超出内联的那些从分页文件取 —— 取到这一档最后一篇为止，不多拉。"""
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        feed = p.evaluate("""() => { const f = document.querySelector('[data-feed]');
            return {n: +f.getAttribute('data-new-total'), head: +f.getAttribute('data-head'),
                    step: +f.getAttribute('data-page-size')}; }""")
        done = self._scroll_until_done(p, rounds=60)
        got = p.evaluate("""() => ({loaded: document.querySelectorAll('[data-card]').length,
            shownNew: Array.from(document.querySelectorAll('[data-card][data-new]'))
                        .filter(c => c.offsetParent !== null).length,
            end: !document.querySelector('[data-feed-end]').hidden})""")
        p.context.close()
        self.assertTrue(done, "「最新」滑到底露不完")
        self.assertEqual(feed["n"], got["shownNew"], f"「最新」写 {feed['n']} 篇，只露出 {got['shownNew']}")
        self.assertLessEqual(got["loaded"], max(feed["head"], feed["n"]) + feed["step"],
                             f"「最新」借机拉了存档：装了 {got['loaded']} 张，这一档只有 {feed['n']}")
        self.assertTrue(got["end"], "「最新」露完了没说「以上是最近七天」")

    def test_the_hot_tab_lists_shows_you_can_open(self):
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        p.click('[data-cat-chip="hot"]')
        p.wait_for_selector(".hot-src", timeout=5000)
        n = int(re.search(r"\d+", p.inner_text(".hot-note")).group(0))
        got = p.evaluate("""() => ({links: Array.from(document.querySelectorAll('.hot-src')).map(a => a.getAttribute('href')),
            feedHidden: document.querySelector('[data-feed]').offsetParent === null,
            marks: document.querySelectorAll('.hot-src .hot-up').length})""")
        p.click(".hot-src")
        p.wait_for_load_state("load")
        landed = p.url
        p.context.close()
        self.assertEqual(n, len(got["links"]), f"「热门」写 {n} 个，名单里 {len(got['links'])} 个")
        self.assertTrue(got["feedHidden"], "切到「热门」卡片还摆着")
        self.assertEqual(n, got["marks"], "有的信源没写有没有更新")
        self.assertIn("/s/", landed, f"点名单里的信源没进信源页：{landed}")


# ---- 2026-09-25 审查补的：返回落点、换档从头、热门的出口、繁体取自己的名单 ----
_VIS_ALL = """() => Array.from(document.querySelectorAll('[data-card]'))
           .filter(c => c.offsetParent !== null).length"""

def _bottom(p, n=1, wait=300):
    for _ in range(n):
        p.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        p.wait_for_timeout(wait)


class BackReturnsToTheSameCard(Harness):
    """从单集页返回，要落在刚才点的那张卡上——包括露到了第几批。"""

    def _roundtrip(self, prep):
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        prep(p)
        href = p.evaluate("""() => { const v=[...document.querySelectorAll('[data-card]')]
            .filter(c => c.offsetParent !== null); const c = v[v.length - 2];
            c.scrollIntoView({block: 'center'}); return c.getAttribute('href'); }""")
        p.wait_for_timeout(200)
        p.click(f'[data-card][href="{href}"]')
        p.wait_for_load_state("load"); p.wait_for_timeout(500)
        ep_y = p.evaluate("scrollY")
        p.go_back(wait_until="load"); p.wait_for_timeout(2500)
        where = p.evaluate("""(h) => { const c = document.querySelector(`[data-card][href="${h}"]`);
            if (!c) return 'missing'; if (c.offsetParent === null) return 'hidden';
            const r = c.getBoundingClientRect();
            return (r.bottom > 0 && r.top < innerHeight) ? 'inview' : 'off ' + Math.round(r.top); }""", href)
        p.context.close()
        return ep_y, where

    def test_opening_an_episode_starts_at_the_top(self):
        ep_y, _ = self._roundtrip(lambda p: _bottom(p, 2))
        self.assertEqual(0, ep_y, f"从首页点进单集，单集页被滚到了 {ep_y}px —— 首页的位置被套到了别的页上")

    def test_back_lands_on_the_card_in_a_later_batch(self):
        def prep(p):
            p.click('[data-cat-chip="core"]'); p.wait_for_timeout(1500)
            _bottom(p, 3)
        _, where = self._roundtrip(prep)
        self.assertEqual("inview", where, f"「必看」露到第四批点进去再返回，那张卡：{where}")

    def test_back_lands_on_a_paged_in_newest_card(self):
        _, where = self._roundtrip(lambda p: _bottom(p, 5))
        self.assertEqual("inview", where, f"「最新」从分页文件补进来的卡，返回后：{where}")


class TabSwitchesStartOver(Harness):
    def test_scroll_reveals_one_batch_at_a_time(self):
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        p.click('[data-cat-chip="core"]'); p.wait_for_timeout(1500)
        step = p.evaluate("() => +document.querySelector('[data-feed]').getAttribute('data-page-size')")
        a = p.evaluate(_VIS_ALL); _bottom(p, 1, 600); b = p.evaluate(_VIS_ALL)
        p.context.close()
        self.assertGreater(b, a, "滑到底没露下一批")
        self.assertLessEqual(b - a, 2 * step, f"滑一次露了 {b - a} 张 —— 一次全露了，不是分批")

    def test_the_reveal_button_reveals_a_batch(self):
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        p.click('[data-cat-chip="core"]'); p.wait_for_timeout(1500)
        a = p.evaluate(_VIS_ALL)
        # 不滚动，直接按按钮（滚动事件不来时的出口）
        p.evaluate("() => document.querySelector('[data-reveal]').click()")
        p.wait_for_timeout(400)
        b = p.evaluate(_VIS_ALL)
        p.context.close()
        self.assertGreater(b, a, "点「再看一批」什么都没露")

    def test_a_new_chip_starts_from_its_first_batch(self):
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        step = p.evaluate("() => +document.querySelector('[data-feed]').getAttribute('data-page-size')")
        p.click('[data-cat-chip="core"]'); p.wait_for_timeout(1500)
        _bottom(p, 4)
        p.click('[data-cat-chip="ai"]'); p.wait_for_timeout(800)
        chip = p.evaluate(_VIS_ALL)
        first_top = p.evaluate("() => [...document.querySelectorAll('[data-card]')].find(c => c.offsetParent).getBoundingClientRect().top")
        p.context.close()
        self.assertLessEqual(chip, step, f"换到「AI」一下露了 {chip} 张 —— 沿用了上一档露到的批数")
        self.assertGreater(first_top, -400, f"换到「AI」后第一张卡在视口上方 {-first_top:.0f}px —— 没回到这一档开头")


class TheHotTabGivesTheFeedBack(Harness):
    def test_leaving_hot_or_searching_in_it_shows_cards(self):
        p = self.page(width=390, height=844)
        p.goto(self.url("/"), wait_until="load")
        p.click('[data-cat-chip="hot"]'); p.wait_for_selector(".hot-src", timeout=5000)
        p.fill("[data-search]", "的"); p.wait_for_timeout(1500)
        in_hot = p.evaluate(_VIS_ALL)
        p.fill("[data-search]", ""); p.wait_for_timeout(300)
        p.click('[data-cat-chip="new"]'); p.wait_for_timeout(500)
        back = p.evaluate(_VIS_ALL)
        hot_vis = p.evaluate("() => document.querySelector('[data-hot]').offsetParent !== null")
        p.context.close()
        self.assertGreater(in_hot, 0, "在「热门」里搜，一张卡都没出来")
        self.assertGreater(back, 0, "从「热门」切回「最新」，卡片没回来")
        self.assertFalse(hot_vis, "切回「最新」，热门名单还挂着")

    def test_the_traditional_hot_tab_stays_in_its_tree(self):
        p = self.page(width=390, height=844)
        got = []
        p.on("request", lambda r: got.append(r.url) if "hot.json" in r.url else None)
        p.goto(self.url("/tw/"), wait_until="load")
        p.click('[data-cat-chip="hot"]'); p.wait_for_selector(".hot-src", timeout=5000)
        hrefs = p.evaluate("() => [...document.querySelectorAll('.hot-src')].map(a => a.getAttribute('href'))")
        p.context.close()
        self.assertTrue(got and all("/podcast/tw/hot.json" in u for u in got), f"繁体首页取的是 {got}")
        bad = [h for h in hrefs if not h.startswith("/podcast/tw/")]
        self.assertEqual([], bad[:3], f"繁体「热门」名单链回了别的站：{len(bad)} 个")


