"""撤下的稿，旧地址跳到留下的那一篇（2026-11-27 马斯克达沃斯那场撤 5 留 1）。"""
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
RED = ROOT / "data" / "redirects.json"


class WithdrawnPagesRedirect(unittest.TestCase):
    def setUp(self):
        self.red = json.loads(RED.read_text()) if RED.exists() else {"p": {}, "e": {}}
        self.eps = {}
        for f in (ROOT / "data" / "episodes").glob("*.json"):
            e = json.loads(f.read_text())
            self.eps[e["id"]] = e
        self.slugs = {e["slug"] for e in self.eps.values()}

    def test_every_target_is_still_on_the_site(self):
        # 留下那篇以后也被撤了的话，build 会悄悄不建这条跳转，旧地址就 404 了
        for kind in ("p", "e"):
            for old, to in (self.red.get(kind) or {}).items():
                self.assertIn(to, self.eps, f"/{kind}/{old}/ 要跳去的 {to} 已经不在了")

    def test_an_old_address_is_not_also_a_live_page(self):
        for old in self.red.get("p") or {}:
            self.assertNotIn(old, self.slugs, f"/p/{old}/ 还有一篇活稿，跳转会被它盖住")
        for old in self.red.get("e") or {}:
            self.assertNotIn(old, self.eps, f"/e/{old}/ 还有一篇活稿")

    def test_built_site_serves_the_redirect(self):
        for old, to in (self.red.get("p") or {}).items():
            page = ROOT / "p" / old / "index.html"
            self.assertTrue(page.exists(), f"/p/{old}/ 没建出来（旧链接会 404）")
            self.assertIn(self.eps[to]["slug"][:20], __import__("urllib.parse").parse.unquote(page.read_text()))

    def test_redirects_are_not_in_the_sitemap(self):
        sm = (ROOT / "sitemap.xml").read_text()
        import urllib.parse
        sm = urllib.parse.unquote(sm)
        for old in self.red.get("p") or {}:
            self.assertNotIn(f"/p/{old}/", sm)


if __name__ == "__main__":
    unittest.main()
