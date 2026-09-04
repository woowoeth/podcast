#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""繁体站的守护检查。

八条判据，各防一类真实发生过的事故（多数在主站那边都真的抓到过东西）：

① 结构一一对应 —— 简体站每页 /tw/ 都要有，反过来不能多。少一页是死链，
   多一页是没人维护的孤儿。
② 链接集合一致 —— 两版页面的 href/src 取出来，繁体去掉 /tw 后必须逐个相同。
   主站那边正是这条抓到一个隐蔽 bug：占位符用了 \\x00，而 OpenCC 底层是
   C 字符串遇 \\x00 就截断，URL 被正文填满（href="居里 — 人類世界生存法則"），
   页面照样渲染、构建照样通过。
③ 没有漏转 —— 正文里不该再出现只存在于简体的字。漏转通常不是整页漏，
   是某个模板分支漏，抽查看不见。
④ 歧义字都登记过 —— 一简多繁里两边都讲得通的那些字（隻/髮/麵/餘/曆…），
   每处的三字上下文必须在 pipeline/tw_allow.txt 里。出现没登记的就拦，
   人看一眼再登记。新节目每天进来，这条会持续报几条要审，这是有意的。
   三字而不是二字：分词错会造出合法的二字词（「明白髮生」里的「白髮」），
   二字白名单放它过就抓不到。
⑤ hreflang 两版必须完全一致 —— 它描述「另一个语言版本在哪」，两边内容本来就一样。
⑥ 地图类文件指向自己 —— sitemap/feed/search.json 里的地址在元素文本里，
   不走属性重写那套规则，漏了就把繁体站的地图指向了简体站。
⑦ 绝不该出现的组合 —— 逐字审只能看见你想到要看的字；这张表一次扫出
   「核心系统→核心繫統」「强调制度→強調製度」这类跨词边界切错。
"""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "pipeline"))

import tw as TW  # noqa: E402

TWDIR = os.path.join(ROOT, "tw")
# en/ 是英文版（.gitignore 里，界面文案层未完工，默认不上线），
# 另一份内容，不参与繁简结构比对。
SKIP = TW.SKIP_DIRS | {"en"}
TAG = re.compile(r"<script.*?</script>|<style.*?</style>", re.S)
LINK = re.compile(r'\b(?:href|src)="([^"]*)"')
ALT = re.compile(r'<link rel="alternate" hreflang[^>]*>')
SIMP_ONLY = "们这时说过还没个来对开关问题实现样种应认识电话书长门闻见丽乐乡习买卖头条声词语读观"


def pages(root, skip):
    out = {}
    for dp, dn, fn in os.walk(root):
        rel = os.path.relpath(dp, root)
        if set(rel.split(os.sep)) & skip:
            dn[:] = []
            continue
        for f in fn:
            if f in ("index.html", "404.html"):
                p = os.path.join(dp, f)
                out[os.path.relpath(p, root).replace(os.sep, "/")] = p
    return out


def body(s):
    return " ".join(re.findall(r">([^<>]+)<", TAG.sub("", s)))


@unittest.skipUnless(os.path.isdir(TWDIR), "还没构建 tw/（跑一次 pipeline/build.py）")
class TraditionalSite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sc = pages(ROOT, SKIP)
        cls.tw = pages(TWDIR, SKIP - {"tw"})

    def test_1_structure(self):
        miss = sorted(set(self.sc) - set(self.tw))
        extra = sorted(set(self.tw) - set(self.sc))
        self.assertEqual([], miss[:5], "繁体站缺页")
        self.assertEqual([], extra[:5], "繁体站有孤儿页")

    def test_1b_expected_sections(self):
        """独立于 SKIP 表的断言：这些东西繁体站必须有。

        test_1 拿同一份 SKIP 表算两边，跳错了会两边一起漏，它自己看不见。
        第一版就把 sources/ 当数据目录跳过了，繁体站少一页而 test_1 全绿。
        """
        for rel in ("index.html", "404.html", "sources/index.html", "log/index.html",
                    "feed.xml", "sitemap.xml", "search.json"):
            self.assertTrue(os.path.exists(os.path.join(TWDIR, rel)),
                            "繁体站缺 %s" % rel)
        for d in ("p", "e", "s"):
            n_sc = len([x for x in os.listdir(os.path.join(ROOT, d))
                        if os.path.isdir(os.path.join(ROOT, d, x))])
            n_tw = len([x for x in os.listdir(os.path.join(TWDIR, d))
                        if os.path.isdir(os.path.join(TWDIR, d, x))])
            self.assertEqual(n_sc, n_tw, "%s/ 两版数量不一致" % d)

    def test_2_links_match(self):
        bad = []
        for k in sorted(set(self.sc) & set(self.tw)):
            a = LINK.findall(ALT.sub("", open(self.sc[k], encoding="utf-8").read()))
            b = LINK.findall(ALT.sub("", open(self.tw[k], encoding="utf-8").read()))
            # 跨站地址两版**本来就不同**，不能靠去前缀对齐：简体页脚写 /
            # 和 /skill/，繁体页脚写 /tw/ 和 /skill/tw/ —— 后者才是对的。
            # 这一条以前把繁体的正确写法判成「对不上」，等于在要求繁体页
            # 把读者送回简体主站。
            b = [x.replace("/podcast/tw/", "/podcast/") for x in b]
            for pair in (("/tw/", "/"), ("/skill/tw/", "/skill/")):
                b = [pair[1] if x == pair[0] else x for x in b]
            b = [x.replace("Noto+Serif+TC", "Noto+Serif+SC") for x in b]
            if a != b:
                bad.append((k, [(x, y) for x, y in zip(a, b) if x != y][:2]))
        self.assertEqual([], bad[:3], "繁简两版的链接对不上")

    def test_3_no_simplified_left(self):
        bad = []
        for k, p in sorted(self.tw.items()):
            with open(p, encoding="utf-8") as fh:
                hit = sorted({c for c in body(fh.read()) if c in SIMP_ONLY})
            if hit:
                bad.append((k, "".join(hit[:6])))
        self.assertEqual([], bad[:3], "繁体页里还有简体字（漏转）")

    def test_4_ambiguous_reviewed(self):
        allow = TW.load_allow()
        unseen = {}
        for k, p in sorted(self.tw.items()):
            with open(p, encoding="utf-8") as fh:
                cs = TW.contexts(body(fh.read()))
            for c in cs:
                if c not in allow:
                    unseen.setdefault(c, k)
        self.assertEqual(
            {}, dict(sorted(unseen.items())[:20]),
            "这些歧义字上下文没人看过。看一眼对不对，对就跑 "
            "python3 pipeline/gen_tw_allow.py 登记进 pipeline/tw_allow.txt")

    def test_5_hreflang_identical(self):
        bad = [k for k in sorted(set(self.sc) & set(self.tw))
               if ALT.findall(open(self.sc[k], encoding="utf-8").read())
               != ALT.findall(open(self.tw[k], encoding="utf-8").read())]
        self.assertEqual([], bad[:3], "hreflang 两版不一致")

    def test_6_maps_point_at_self(self):
        bad = []
        for f in ("sitemap.xml", "feed.xml", "search.json", "llms.txt"):
            p = os.path.join(TWDIR, f)
            if not os.path.exists(p):
                continue
            with open(p, encoding="utf-8") as fh:
                n = len(re.findall(r"https://ourword\.ai/podcast/(?!tw/)", fh.read()))
            if n:
                bad.append((f, n))
        self.assertEqual([], bad, "繁体站的地图文件指向了简体站")

    def test_6b_locale_markers(self):
        """语言标记必须自报繁体。

        og:locale / inLanguage / <language> 是标记不是正文，转换转不到它们。
        漏了的后果是搜索引擎和分享卡片把繁体页按简体归类 —— 页面看着全对，
        只有翻 head 才看得见。
        """
        bad = []
        for k, p in sorted(self.tw.items()):
            with open(p, encoding="utf-8") as fh:
                t = fh.read()
            if re.search(r'og:locale"\s*content="zh_CN"', t):
                bad.append((k, "og:locale 仍是 zh_CN"))
            elif re.search(r'"inLanguage":\s*"zh-CN"', t):
                bad.append((k, "inLanguage 仍是 zh-CN"))
        fp = os.path.join(TWDIR, "feed.xml")
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as fh:
                if re.search(r"<language>zh-c?n</language>", fh.read(), re.I):
                    bad.append(("feed.xml", "<language> 仍是 zh-CN"))
        self.assertEqual([], bad[:3], "繁体站的语言标记还写着简体")

    def test_7_no_missegmented(self):
        """绝不该出现的组合：分词切错的系统性筛子。

        逐字审只能看见你想到要看的字；这张表一次扫出「核心系统→核心繫統」
        「强调制度→強調製度」这类跨词边界切错 —— 两处都是这么发现的。
        """
        bad = []
        for k, p in sorted(self.tw.items()):
            with open(p, encoding="utf-8") as fh:
                t = body(fh.read())
            for w in TW.NEVER:
                if w in t:
                    i = t.index(w)
                    bad.append((k, w, t[max(0, i - 10):i + 11]))
                    break
        self.assertEqual([], bad[:3], "繁体页里有分词切错的组合")


if __name__ == "__main__":
    unittest.main()
