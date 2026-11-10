#!/bin/bash
# 给本机定时任务一个长效 Claude 登录（一年），不再依赖 Claude app 开着、CLI 自己续登录。
#
#   claude setup-token            # 浏览器里点授权，终端最后打出一串 sk-ant-oat01-…
#   bash scripts/set-cli-token.sh # 把那串粘进来
#
# 为什么：11-05 → 11-09 Claude app 关着的四天，定时任务八班全是 403、一篇没出；
# app 一打开，登录自己续上了。长效 token 不需要续期，local-daily.sh 开头从钥匙串读它。
#
# 输入不回显、不进 shell 历史、不经过对话；先拿它真调一次模型，通了才存进钥匙串。
# 存的时候 token 走 stdin，不出现在命令行参数里（ps 看不到）。
set -uo pipefail
SERVICE="${CLI_TOKEN_SERVICE:-ourword-podcast-claude-token}"
export PATH="$HOME/bin/node/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

# 退出时把终端里没读走的输入清掉：原来粘贴进来的后半截会在脚本退出后漏到命令行上、
# 被 zsh 当成命令执行，token 的片段就进了 shell 历史（11-10 四次）。
flush_tty(){ [ -t 0 ] && perl -MPOSIX -e 'POSIX::tcflush(0, POSIX::TCIFLUSH)' 2>/dev/null; true; }
trap flush_tty EXIT

# **先从剪贴板读，不让 token 经过终端的行输入。** 终端按行交付输入：折成几行的 token 复制下来，
# 最后一行后面没有换行，要等人按回车才交给脚本 —— 按行读、按超时判断「粘贴完了」，总有一段
# 赶不上（11-10：收到 104 个字符，最后一行 rwAA 在脚本已经去调模型之后才到，漏到了命令行上）。
echo "1. 在 claude setup-token 打出 token 的地方，从 sk-ant-oat01- 一直选到 token 的最后一行，几行一起复制（Cmd+C）"
echo "2. 回到这里直接按回车（不用粘贴）："
IFS= read -rs _ || true
echo
raw=$(pbpaste 2>/dev/null || true)
from_clipboard=1
if ! printf '%s' "$raw" | grep -q "sk-ant-oat"; then
  from_clipboard=0
  echo "剪贴板里没有 sk-ant-oat 开头的 token。也可以把它粘贴进来，粘贴完再多按一次回车（空一行表示结束）："
  raw="" started=0
  while IFS= read -rs line; do
    raw="$raw"$'\n'"$line"
    case "$line" in *sk-ant-oat*) started=1 ;; esac
    if [ "$started" = 1 ] && [ -z "$(printf '%s' "$line" | tr -d '[:space:]')" ]; then break; fi
  done
  # 读到输入结束、最后一行没有换行时，read 返回失败、不进循环体 —— 那一行在 $line 里，补上
  [ -n "${line:-}" ] && raw="$raw"$'\n'"$line"
  echo
fi
# 从含 sk-ant-oat 的那一行起取，后面「整行都是 token 字符」的行接上（折行），
# 碰到别的文字（空行、「Store this token securely」）就停。
TOK=$(printf '%s\n' "$raw" | awk '
  { gsub(/^[ \t\r]+|[ \t\r]+$/, "") }
  !on { i = index($0, "sk-ant-oat")
        if (i) { on = 1; tok = substr($0, i)
                 if (tok ~ /[^A-Za-z0-9_-]/) { sub(/[^A-Za-z0-9_-].*/, "", tok); stop = 1 } }
        next }
  on && !stop { if ($0 ~ /^[A-Za-z0-9_-]+$/) tok = tok $0; else stop = 1 }
  END { print tok }')
if [ -z "$TOK" ]; then
  if [ -z "$(printf '%s' "$raw" | tr -d '[:space:]')" ]; then
    echo "没读到内容，什么都没改。" >&2
  else
    echo "找不到 sk-ant-oat 开头的 token（复制的是不是别的东西？），没有写入。" >&2
  fi
  exit 1
fi
echo "收到 ${#TOK} 个字符的 token。"

echo "先拿它调一次模型……"
out=$(CLAUDE_CODE_OAUTH_TOKEN="$TOK" perl -e 'alarm 120; exec @ARGV' \
      claude -p --model haiku "reply with the single word ok" 2>&1 | tail -3)
# **整行就是 ok 才算通。** 原来写的是 *ok*：报错「Invalid OAuth token」里的 t-o-k-en
# 就含 ok，自测时一个假 token 被当成通过存了进去。
last=$(printf '%s\n' "$out" | tail -1 | tr -d '[:space:].!' | tr '[:upper:]' '[:lower:]')
if [ "$last" != "ok" ]; then
  echo "这个 token 调不通，没有写入：${out:0:200}" >&2
  case "$out" in
    *401*|*nvalid*) echo "  401／invalid 多半是没复制全：要从 sk-ant-oat01- 一直选到 token 的最后一行（折成几行就几行一起选）。" >&2 ;;
  esac
  exit 1
fi

printf 'add-generic-password -U -a %s -s %s -w %s\n' "$USER" "$SERVICE" "$TOK" | security -i >/dev/null
unset TOK
if security find-generic-password -s "$SERVICE" >/dev/null 2>&1; then
  echo "已存进钥匙串（${SERVICE}）。下一轮定时任务起就用它，Claude app 关着也照跑。"
  if [ "$from_clipboard" = 1 ]; then
    printf '' | pbcopy 2>/dev/null && echo "（剪贴板已清空）"
  fi
  exit 0
else
  echo "写钥匙串失败，什么都没改。" >&2
  exit 1
fi
