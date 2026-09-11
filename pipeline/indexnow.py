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
from lib.util import log  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
KEY = os.environ.get("INDEXNOW_KEY", "8f3c2a1b9d4e6f708192a3b4c5d6e7f0")
KEY_FILE = ROOT / f"{KEY}.txt"
ENDPOINT = "https://api.indexnow.org/indexnow"


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
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    def _post(payload: dict):
        rq = urllib.request.Request(
            ENDPOINT, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST")
        with urllib.request.urlopen(rq, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")[:300]

    # **把错误正文打出来。** 原来只打异常类型（`HTTPError: HTTP Error 403`），
    # 而真正的原因在正文里一句话就写着：
    #   {"errorCode":"UserForbiddedToAccessSite","message":"User is unauthorized
    #    to access the site. Please verify the site using the key"}
    # 少了这一行，这个 ping 连着几天每轮都失败而没人知道它为什么失败。
    try:
        st, txt = _post(body)
        log(f"indexnow: {st} submitted {len(urls)} urls")
        _note(ok=True, detail=f"{st}")
        return 0
    except urllib.error.HTTPError as ex:
        detail = ex.read().decode("utf-8", "replace")[:300]
        log(f"indexnow: {ex.code} {detail}")
        # 子目录 keyLocation 被拒（403 UserForbidden…）时再试一次不带
        # keyLocation 的：那条路 Bing 收 200。真正的解法是把 key 文件放到
        # **域名根目录**（见 _note 里的提示），这里只是不要连提交都放弃。
        if ex.code == 403:
            try:
                st, txt = _post({k: v for k, v in body.items()
                                 if k != "keyLocation"})
                log(f"indexnow: 去掉 keyLocation 后 {st}")
                _note(ok=False, detail=f"403 → 去掉 keyLocation 后 {st}；"
                                       f"key 文件需要放到域名根目录才算验证通过")
                return 0
            except Exception as ex2:
                detail = f"{detail} / 重试也失败 {type(ex2).__name__}"
        _note(ok=False, detail=detail)
        return 0  # never fail the digest over a ping
    except Exception as ex:
        log(f"indexnow: {type(ex).__name__}: {ex}")
        _note(ok=False, detail=f"{type(ex).__name__}: {str(ex)[:120]}")
        return 0


if __name__ == "__main__":
    raise SystemExit(ping())
