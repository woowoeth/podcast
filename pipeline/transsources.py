#!/usr/bin/env python3
"""把信源简介译成英文，存 data/en/_sources.json。

节目简介是静态配置（resolve_sources.py 里的 desc），一共一百多条，一次调用译完。
不放进 translate.py：那个是按集跑的、带断点，简介是整体一份。
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from lib import llm                                              # noqa: E402
from lib.util import log                                         # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "en" / "_sources.json"

SYSTEM = ("You translate one-line podcast descriptions from Chinese to English "
          "for a curated podcast site. Keep each one a single line, same length "
          "or shorter, same claim, no marketing language. Show names, hosts and "
          "companies are already in Latin script — copy them byte-identical. "
          "Output valid JSON only.")


def main() -> int:
    srcs = json.loads((ROOT / "data" / "sources.json").read_text())["sources"]
    have = {}
    if OUT.exists():
        try:
            have = json.loads(OUT.read_text()).get("sources", {})
        except Exception:
            have = {}
    # 集 id（源名-十位哈希）不可能是信源 id —— 信源 id 里从来没有「-」。
    # 11-09 模型答非所问，把两集的标题译文按集 id 交了回来，被原样存进这里。
    junk = [k for k in have if "-" in k]
    for k in junk:
        del have[k]
    if junk:
        log(f"清掉 {len(junk)} 条不是信源的条目：{'、'.join(junk[:4])}")
    todo = [s for s in srcs if s.get("desc") and s["id"] not in have]
    if not todo and junk:
        OUT.write_text(json.dumps({"sources": have}, ensure_ascii=False,
                                  indent=1) + "\n")
    if not todo:
        log("信源简介都译过了")
        return 0
    log(f"要译 {len(todo)} 条（已有 {len(have)} 条）")
    # 分批，一批 40 条：一次全塞进去输出会被 max_tokens 截断
    for i in range(0, len(todo), 40):
        batch = todo[i:i + 40]
        body = "\n".join(f'{s["id"]}: {s["desc"]}' for s in batch)
        ask = ('Translate each line. Return JSON: {"items": [{"id": "...", '
               '"desc": "..."}, ...]} with one entry per input line, same ids.')
        try:
            r = llm.call_json(SYSTEM, body + "\n\n" + ask,
                              max_tokens=4000, temperature=0.2, role="review", step="sources")
        except Exception as ex:
            log(f"  这一批失败：{type(ex).__name__}")
            continue
        got, stray = 0, []
        asked = {s["id"] for s in batch}
        for it in (r.get("items") or []):
            sid, desc = it.get("id"), (it.get("desc") or "").strip()
            # **只收这一批问到的 id。** 原来是「有 id 有译文就存」，模型交回别的东西
            # 也照单全收（11-09：问的是两条信源，存进去的是两集的标题）。
            if sid not in asked:
                stray.append(str(sid))
                continue
            if desc:
                have[sid] = {"desc": desc}
                got += 1
        log(f"  第 {i//40 + 1} 批：{got}/{len(batch)} 条")
        if stray:
            log(f"  答非所问，丢掉 {len(stray)} 条没问过的：{'、'.join(stray[:4])}")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"sources": have}, ensure_ascii=False,
                                  indent=1) + "\n")
    log(f"共 {len(have)} 条 → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    import atexit
    # 用量进账本（data/usage.json）：中途崩掉、提前退出的那一轮也要记（见 lib/llm.persist_usage）
    atexit.register(lambda: llm.persist_usage())
    raise SystemExit(main())
