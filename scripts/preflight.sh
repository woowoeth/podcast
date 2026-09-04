#!/bin/bash
# 推送前必须跑这个。一条命令，全部检查，任何一项不过就别推。
#
# 为什么需要它：这个仓库有 140 多项守护检查，但它们**从来没在 CI 里跑过**，
# 只在我记得跑的时候跑。于是接连出过这些事，全都是"改完直接推、推完才发现"：
#
#   · 提交了一个指向仓库自身的符号链接，Pages 打包无限递归，两轮部署各卡 20 分钟
#   · 心跳用 heredoc 写在管道块里，单独跑正常、真跑批一声不响
#   · 本机脚本的 SITE_FILES 漏了两个目录，本机线永远不提交它们
#   · 推送重试用 reset --soft，bot 的提交把源码回退了
#
# 每一个都能被这里的某一项挡住。靠自觉记得跑检查不是流程，是运气。
set -uo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || exit 1
export PATH="/Library/Frameworks/Python.framework/Versions/3.14/bin:$PATH"
PY=$(command -v python3 || echo python)

fail=0
say() { printf '  %s\n' "$1"; }
step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
bad()  { printf '  \033[31m✗ %s\033[0m\n' "$1"; fail=1; }
good() { printf '  \033[32m✓ %s\033[0m\n' "$1"; }

step "守护检查与单元测试"
if $PY -m unittest discover -s tests -q 2>&1 | tail -3 | grep -q '^OK'; then
  good "$($PY -m unittest discover -s tests 2>&1 | grep -o 'Ran [0-9]* tests') 全过"
else
  $PY -m unittest discover -s tests 2>&1 | tail -20
  bad "测试没全过"
fi

step "渲染层体检（真的打开页面）"
# 这一层是补上来的：用户一轮报了 11 个问题，10 个在渲染层，而当时 300 多项守护
# 里没有一项打开过真实页面。没装 playwright 就 skip，但**必须说出来**——
# 静默 skip 的检查等于不存在。
if $PY -c 'import playwright' 2>/dev/null; then
  if $PY -m unittest tests.test_render -q 2>&1 | tail -3 | grep -q '^OK'; then
    good "$($PY -m unittest tests.test_render 2>&1 | grep -o 'Ran [0-9]* tests') 全过"
  else
    bad "渲染层有不通过的项"
    $PY -m unittest tests.test_render 2>&1 | tail -20
  fi
else
  bad "没装 playwright —— 渲染层这一层没跑。装：
       python3 -m pip install playwright && python3 -m playwright install chromium
       （这一层专门抓那些静态断言看不见的问题：布局位移、hidden 没藏住、
         封面比例、点了没反应、首屏体积、深色主题对比度）"
fi

step "shell 与 YAML 语法"
for f in scripts/*.sh; do
  bash -n "$f" 2>/dev/null && good "$f" || bad "$f 语法错误"
done
$PY - <<'EOF' || fail=1
import glob, sys
try:
    import yaml
except ImportError:
    print("  (没装 pyyaml，跳过 YAML 校验)"); sys.exit(0)
bad = 0
for f in sorted(glob.glob(".github/workflows/*.yml")):
    try:
        yaml.safe_load(open(f)); print(f"  \033[32m✓\033[0m {f}")
    except Exception as e:
        print(f"  \033[31m✗\033[0m {f}: {e}"); bad = 1
sys.exit(bad)
EOF

step "入库的符号链接不许指回仓库内部"
# 这一条单独列出来，因为它炸的是部署而不是代码，测试跑绿也看不出来
if git ls-files -s | awk '$1=="120000"{print $4}' | while read -r f; do
     t=$(cd "$(dirname "$f")" && cd "$(readlink "$(basename "$f")")" 2>/dev/null && pwd)
     case "$t" in "$PWD"*) echo "$f";; esac
   done | grep -q .; then
  bad "有符号链接指回仓库内部（Pages 打包会递归）"
else
  good "没有递归符号链接"
fi

step "构建是幂等的"
# 同样的数据连续构建两次必须一字不差。不成立就说明生成产物里混进了时间戳
# 或随机顺序，而那会让每一轮跑批都在生成产物上冲突。
# 先给输入数据取一次指纹，构建完再取一次。**输入变了就不能怪构建不幂等**——
# 回填工具（repoint / video --audit）在后台跑的时候会改 data/，两次构建自然不同，
# 而报成"构建不幂等"是个错的原因，下次会照着错方向查。
dhash() { find data -name '*.json' -type f -exec shasum {} + 2>/dev/null | sort | shasum | cut -c1-16; }
d0=$(dhash)
$PY pipeline/build.py >/dev/null 2>&1
a=$(git status --porcelain | sort | md5 2>/dev/null || git status --porcelain | sort | md5sum)
$PY pipeline/build.py >/dev/null 2>&1
b=$(git status --porcelain | sort | md5 2>/dev/null || git status --porcelain | sort | md5sum)
d1=$(dhash)
if [ "$d0" != "$d1" ]; then
  bad "data/ 在构建期间被改了（有别的任务在写），这一项判断不了。
       等它跑完再来：$(pgrep -fl 'pipeline/(repoint|video|run)\.py' 2>/dev/null | head -2 | tr '\n' ' ')"
elif [ "$a" = "$b" ]; then
  good "连续两次构建结果一致"
else
  bad "构建不幂等：数据没变，两次结果却不同"
fi

step "英文站零漏译"
# 判据：汉字只许出现在 lang="zh" 的元素里。漏一条界面文案就不该推——
# 交一个中英混排的页面比不交更糟。
if [ -d en ]; then
  if $PY pipeline/enscan.py en >/tmp/preflight-en.txt 2>&1; then
    good "英文站零漏译"
  else
    bad "英文站有漏译"; head -12 /tmp/preflight-en.txt
  fi
else
  say "英文站没建（data/en/ 还没有译文）"
fi

step "体检（只查文件，不连线上）"
if $PY pipeline/healthcheck.py >/tmp/preflight-hc.txt 2>&1; then
  good "$(tail -1 /tmp/preflight-hc.txt)"
else
  grep '坏了' /tmp/preflight-hc.txt | sed 's/^/  /'
  bad "体检不通过"
fi

step "工作区里没有本该提交的东西"
untracked=$(git status --porcelain | grep -c '^??' || true)
[ "$untracked" = "0" ] && good "没有未跟踪文件" \
  || bad "$untracked 个未跟踪文件——是漏加了，还是该进 .gitignore？"

printf '\n'
if [ "$fail" = 0 ]; then
  printf '\033[32m全部通过，可以推。\033[0m\n'
else
  printf '\033[31m有不通过的项。修完再推——推上去才发现是这个仓库反复犯的错。\033[0m\n'
fi
exit "$fail"
