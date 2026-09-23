#!/usr/bin/env python3
"""Ping IndexNow after a build so Bing / Yandex / others recrawl new URLs.

Google retired the sitemap ping; IndexNow is what still accepts a push.
The key file must be served at keyLocation (committed next to the site root).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from build import SITE, ep_url, load  # noqa: E402
from lib import net  # noqa: E402
from lib.util import log  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = os.environ.get("INDEXNOW_KEY", "8f3c2a1b9d4e6f708192a3b4c5d6e7f0")
KEY_FILE = ROOT / f"{KEY}.txt"
# **两个端点，不是一个。** IndexNow 的参与方之间会互相转发提交，
# 所以任何一个收下都算进了网络。api.indexnow.org 是共享入口，
# 但它对本站一直回 403（见下），而 Yandex 那个入口同样的 key、
# 同样的请求体回 202 —— 实测出来的，不是猜的。
ENDPOINTS = ["https://api.indexnow.org/indexnow",
             "https://yandex.com/indexnow"]


def key_file() -> pathlib.Path:
    KEY_FILE.write_text(KEY + "\n")
    return KEY_FILE


def _trees() -> list[str]:
    """现在真的存在哪几棵树。加第四棵不需要改这里。"""
    root = pathlib.Path(__file__).resolve().parent.parent
    return [""] + [f"/{d}" for d in ("tw", "en")
                   if (root / d / "index.html").exists()]


def urls_to_submit(limit: int = 40) -> list[str]:
    """**三棵树的 URL 都要提交。**

    原来这里只提交简体：`SITE` 是简体的站点根，`ep_url()` 也走简体的 BASE。
    于是 /en/ 和 /tw/ 的新页面从来没被推给搜索引擎——它们只能等爬虫自己
    回来，而那是几天到几周。三棵树同一次推送上线，通知也该是三份。
    """
    eps, _ = load()
    out = []
    for t in _trees():
        base = SITE + t
        out += [base + "/", base + "/sitemap.xml", base + "/llms.txt",
                base + "/sources/"]
    for ep in eps[:limit]:
        try:
            u = ep_url(ep)
        except Exception:
            continue
        for t in _trees():
            # ep_url 给的是简体地址；换成这棵树的前缀
            out.append(u.replace(SITE + "/", SITE + t + "/", 1) if t else u)
    # unique, stable order
    seen, uniq = set(), []
    for u in out:
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _note(ok: bool, detail: str) -> None:
    """把结果写进 data/indexnow.json —— 体检读它。

    这个 ping 的失败一直是静默的：脚本里 `return 0  # never fail`，
    外层又是 `|| echo`。两层加起来，连着几天每轮都 403 而没有任何人知道。
    不让它拦住发布是对的，但**必须留痕**。
    """
    import datetime as _dt
    f = ROOT / "data" / "indexnow.json"
    try:
        d = json.loads(f.read_text())
    except Exception:
        d = {}
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    d["at"] = now
    d["ok"] = bool(ok)
    d["detail"] = detail
    if ok:
        d["last_ok"] = now
    else:
        d["fail_streak"] = int(d.get("fail_streak") or 0) + 1
    if ok:
        d["fail_streak"] = 0
    f.write_text(json.dumps(d, ensure_ascii=False, indent=1) + "\n")


def ping(urls: list[str] | None = None) -> int:
    key_file()
    urls = urls or urls_to_submit()
    if not urls:
        log("indexnow: nothing to submit")
        return 0
    body = {
        "host": "ourword.ai",
        "key": KEY,
        "keyLocation": f"{SITE}/{KEY}.txt",
        "urlList": urls,
    }

    def _post(endpoint: str, payload: dict):
        rq = urllib.request.Request(
            endpoint, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST")
        # **走项目的 TLS 上下文。** 直接 urlopen 用的是 stdlib 默认信任链，
        # 本机链路里有自签证书时会 CERTIFICATE_VERIFY_FAILED ——
        # 同一个地址 curl 通、这里不通，第一次看见会以为是端点挂了。
        with urllib.request.urlopen(rq, timeout=20, context=net.ctx()) as r:
            return r.status, r.read().decode("utf-8", "replace")[:300]

    # **把错误正文打出来。** 原来只打异常类型（`HTTPError: HTTP Error 403`），
    # 而真正的原因在正文里一句话就写着：
    #   {"errorCode":"UserForbiddedToAccessSite","message":"User is unauthorized
    #    to access the site. Please verify the site using the key"}
    # 少了这一行，这个 ping 连着几天每轮都失败而没人知道它为什么失败。
    #
    # **那句话指的不是 key 文件。** 仓库里原来写着「把 key 放到域名根目录就
    # 算验证通过」，2026-09-23 实测推翻了：根目录 https://ourword.ai/<key>.txt
    # 已经 200、内容也对，Bing 照样 403；而**同一把 key、同一个请求体**发给
    # Yandex 是 202。所以卡的是 Bing 那边没认这个站的归属
    # （要在 Bing Webmaster Tools 里验证 ourword.ai），不是 key 放哪。
    fails = []
    for ep in ENDPOINTS:
        try:
            st, _ = _post(ep, body)
            log(f"indexnow: {ep} {st} submitted {len(urls)} urls")
            _note(ok=True, detail=f"{st} via {ep}"
                                  + (f"；{ENDPOINTS[0]} 仍 403" if fails else ""))
            return 0
        except urllib.error.HTTPError as ex:
            detail = ex.read().decode("utf-8", "replace")[:200]
            log(f"indexnow: {ep} {ex.code} {detail}")
            fails.append(f"{ep} {ex.code} {detail}")
        except Exception as ex:
            log(f"indexnow: {ep} {type(ex).__name__}: {ex}")
            fails.append(f"{ep} {type(ex).__name__}")
    _note(ok=False, detail=" | ".join(fails)[:400])
    return 0  # never fail the digest over a ping


if __name__ == "__main__":
    raise SystemExit(ping())
