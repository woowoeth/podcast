#!/usr/bin/env python3
"""data/usage.json 的 git 合并驱动。

这份账本是**逐日、逐角色的累加计数**，两条部署线（云端 Actions / 本地 launchd）
各自往里加，天然会在同一天同一字段上撞车。默认的行合并每次都判冲突，
但冲突的语义其实是确定的：两边各自加了多少，就都算上。

规则：ours + theirs - base（base 是共同祖先里已经算过的那份，不能重复计）。
到今天为止手工做过 5 次，每次结论一样，所以改成机器做。

任何一边不是预期形状（不是 dict / 值不是数）就退回冲突，让人来看 ——
账目宁可停下，不要悄悄算错。
"""
import json
import sys


def _leaves(node, prefix=()):
    """把嵌套 dict 压平成 {路径: 数}；遇到非数的叶子就抛。"""
    out = {}
    for k, v in node.items():
        if isinstance(v, dict):
            out.update(_leaves(v, prefix + (k,)))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[prefix + (k,)] = v
        else:
            raise TypeError(f"{'/'.join(prefix + (k,))} 不是数：{v!r}")
    return out


def _nest(flat):
    out: dict = {}
    for path, v in sorted(flat.items()):
        cur = out
        for k in path[:-1]:
            cur = cur.setdefault(k, {})
        cur[path[-1]] = v
    return out


def merge(base_text, ours_text, theirs_text):
    base, ours, theirs = (json.loads(t or "{}") for t in (base_text, ours_text, theirs_text))
    b, o, t = _leaves(base), _leaves(ours), _leaves(theirs)
    out = {}
    for path in set(o) | set(t):
        # 两边都没动 base 的那条，就是 base；动了的部分才各自加
        v = o.get(path, b.get(path, 0)) + t.get(path, b.get(path, 0)) - b.get(path, 0)
        if v != int(v):
            v = round(v, 6)
        else:
            v = int(v)
        out[path] = v
    return json.dumps(_nest(out), ensure_ascii=False, indent=2) + "\n"


def main(argv):
    base_p, ours_p, theirs_p = argv[1:4]
    try:
        with open(base_p) as f:
            base = f.read()
        with open(ours_p) as f:
            ours = f.read()
        with open(theirs_p) as f:
            theirs = f.read()
        merged = merge(base, ours, theirs)
    except (ValueError, TypeError, OSError) as e:
        print(f"usage.json 合不了，留给人处理：{e}", file=sys.stderr)
        return 1
    with open(ours_p, "w") as f:
        f.write(merged)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
