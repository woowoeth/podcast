"""专题：把一组手写的长文（data/zt/<slug>/）渲染成站上的一页。

专题和单集深读不是一回事：单集页由管线从文稿生成，专题是编辑写的读物，
横跨很多集。所以它不走 episode_page，而是读 data/zt/<slug>/ 下的 markdown：

  meta.json      {"slug","title","desc","date"}
  00_open.md     第一行「# 书名」，第二行副标题，然后是开篇
  chNN.md        每章一问：「## 第N问　问题」「!! 一句话答案」「### 小标题」……
  99_close.md    结尾

标记只有这几种（写法与页面样式一一对应，别的都不认）：
  ==标红==   唯一的强调
  > 「原话」——人名（人名，#集号）   嘉宾原话卡片
  #### 带走这三句 / #### 这本读物怎么来的   + 「- 」列表
  （人名，#集号）   出处：链到那一集，显示节目期号，不显示时间

**集号是专题写作时的内部编号**（按日期排的 001–150），不是节目期号，也不是 slug。
映射表 data/zt/<slug>/episodes.json 把它对到**原节目标题**，构建时再按标题查当前 slug：
集重新生成后 slug 会变（2026-09-24 实测 18 集一夜之间换了地址），按 slug 写死就全成 404。
标题也查不到的（集下站了）：只显示期号、不挂链接，记进 missing 由构建日志报警、由守护变红，
**不让整站构建失败**——一个专题的出处坏了，不该拦住每天的发布。
"""
from __future__ import annotations

import html
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
ZT = ROOT / "data" / "zt"


class ZtError(ValueError):
    pass


def topics() -> list[dict]:
    out = []
    if not ZT.is_dir():
        return out
    for d in sorted(p for p in ZT.iterdir() if p.is_dir()):
        meta = d / "meta.json"
        if meta.exists():
            m = json.loads(meta.read_text())
            m["dir"] = d
            out.append(m)
    return out


CITE = re.compile(r"[（(]([^（）()]{1,40}?)[，,]\s*#(\d{3})(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?"
                  r"(?:[、，,]\s*#(\d{3})(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?)*[）)]")
BARE = re.compile(r"(?<![\w#&])#(\d{3})(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?")


def render(topic: dict, base: str, live: dict[str, str]) -> dict:
    """live：已发布的集，{原节目标题: 当前 slug}。

    返回 {title, subtitle, toc:[(n,q)], body_html, n_src, missing}；
    missing 是找不到已发布的集的内部集号（只显示期号、不挂链接）。
    """
    d: pathlib.Path = topic["dir"]
    epmap = json.loads((d / "episodes.json").read_text())   # 内部集号 → {slug, no, title}
    missing: list[str] = []

    def src(eid: str) -> str:
        x = epmap.get(eid)
        slug = live.get(x["title"]) if x else None
        if not slug:
            missing.append(eid)
            label = html.escape(x["no"]) if x else html.escape("#" + eid)
            return f'<span class="zt-src zt-src-dead" title="这一集找不到了">{label}</span>'
        return (f'<a class="zt-src" href="{base}/p/{html.escape(slug, quote=True)}/" '
                f'title="{html.escape(x["title"], quote=True)}">{html.escape(x["no"])}</a>')

    def inline(s: str) -> str:
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
        s = re.sub(r"__(.+?)__", r"\1", s)
        # **标红要在切出处之前定下范围。** 标红是整句，句子中间常夹着一个出处；
        # 先按出处切段、再分段找 ==，一对 == 会被切到两段里、原样漏到页面上。
        s = re.sub(r"==(.+?)==", "\x02\\1\x03", s)
        out, last = [], 0
        for m in CITE.finditer(s):
            out.append(_mark(html.escape(s[last:m.start()], quote=False), src))
            # 出处做成句末一个小上标，不带括号和人名：正文里已经点了名，
            # 括号里再写一遍读起来像论文脚注，每段都被打断一次（试读读者原话）。
            ids = re.findall(r"#(\d{3})", m.group(0))
            out.append('<sup class="zt-sup">' + "、".join(src(i) for i in ids) + "</sup>")
            last = m.end()
        out.append(_mark(html.escape(s[last:], quote=False), src))
        return "".join(out).replace("\x02", '<mark class="zt-hot">').replace("\x03", "</mark>")

    files = [d / "00_open.md"] + sorted(d.glob("ch[0-9][0-9].md")) + [d / "99_close.md"]
    title = subtitle = ""
    body: list[str] = []
    toc: list[tuple[int, str]] = []
    state = {"in_q": False, "first": True}
    for fi, f in enumerate(files):
        if not f.exists():
            raise ZtError(f"专题缺文件：{f}")
        lines = f.read_text(encoding="utf-8").split("\n")
        if fi == 0:
            title = lines[0].lstrip("# ").strip()
            subtitle = lines[1].strip()
            lines = lines[2:]
        para: list[str] = []
        lst: list[str] = []
        box = [None, ""]

        def flush() -> None:
            if para:
                cls = ' class="zt-open"' if (fi == 0 and state["first"]) else ""
                state["first"] = False
                body.append(f"<p{cls}>{inline(''.join(para))}</p>")
                para.clear()
            if lst:
                items = "".join(f"<li>{inline(x)}</li>" for x in lst)
                if box[0] == "take":
                    body.append(f'<div class="zt-take"><p class="zt-lbl">{html.escape(box[1])}</p><ul>{items}</ul></div>')
                elif box[0] == "about":
                    body.append(f'<div class="zt-about"><p class="zt-lbl">{html.escape(box[1])}</p><ul>{items}</ul></div>')
                else:
                    body.append(f'<ul class="zt-list">{items}</ul>')
                lst.clear()
                box[0] = None

        for ln in lines:
            s = ln.rstrip()
            if not s.strip():
                flush(); continue
            m = re.match(r"^##\s+第\s*(\d+)\s*问[\s　:：]*(.+)$", s)
            if m:
                flush()
                if state["in_q"]:
                    body.append("</section>")
                state["in_q"] = True
                n, q = int(m.group(1)), m.group(2).strip()
                toc.append((n, q))
                body.append(f'<section class="zt-q" id="q{n}"><p class="zt-no">第 {n} 问</p><h2>{inline(q)}</h2>')
                continue
            if s.startswith("## "):
                flush()
                if state["in_q"]:
                    body.append("</section>"); state["in_q"] = False
                body.append(f'<h2 class="zt-h2">{inline(s[3:].strip())}</h2>')
                continue
            if s.startswith("#### "):
                flush()
                lab = s[5:].strip()
                box[0] = "take" if "带走" in lab else ("about" if "怎么来的" in lab else None)
                box[1] = lab                         # 标签照稿子写的显示（「带走这三句」「带走这十句」）
                continue
            if s.startswith("### "):
                flush(); body.append(f"<h3>{inline(s[4:].strip())}</h3>"); continue
            if s.startswith("!! "):
                flush(); body.append(f'<p class="zt-answer">{inline(s[3:].strip())}</p>'); continue
            if s.startswith(">"):
                flush()
                q = s.lstrip("> ").strip()
                mq = re.match(r"^「([^」]+)」\s*[—–-]+\s*([^（(]+?)\s*([（(].*[）)])?\s*$", q)
                if mq:
                    ids = re.findall(r"#(\d{3})", mq.group(3) or "")
                    tail = (" · " + "、".join(src(i) for i in ids)) if ids else ""
                    body.append(f'<figure class="zt-quote"><p class="raw">{inline(mq.group(1))}</p>'
                                f'<figcaption class="attrib"><b>{html.escape(mq.group(2).strip(), quote=False)}</b>{tail}</figcaption></figure>')
                else:
                    body.append(f'<figure class="zt-quote"><p class="raw">{inline(q)}</p></figure>')
                continue
            mm = re.match(r"^\s*[-*]\s+(.*)$", s)
            if mm:
                if para:
                    flush()
                lst.append(mm.group(1)); continue
            if lst:
                flush()
            para.append(s.strip())
        flush()
        if fi == 0:
            body.append("\0TOC\0")
    if state["in_q"]:
        body.append("</section>")
    toc_html = (f'<nav class="zt-toc" aria-label="目录"><p class="zt-lbl">这本读物回答的 {len(toc)} 个问题</p><ol>'
                + "".join(f'<li><a href="#q{n}">{inline(q)}</a></li>' for n, q in toc) + "</ol></nav>")
    body_html = "\n".join(body).replace("\0TOC\0", toc_html)
    return {"title": title, "subtitle": subtitle, "toc": toc, "body_html": body_html,
            "n_src": body_html.count('class="zt-src"'), "missing": sorted(set(missing))}


def _mark(escaped: str, src) -> str:
    return BARE.sub(lambda m: src(m.group(1)), escaped)
