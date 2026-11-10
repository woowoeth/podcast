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

echo "把 claude setup-token 打出来的那串 token 粘贴进来（被折成几行就几行一起复制），然后回车（不会显示出来）："
# **一次读完整段粘贴，不是只读一行。** 终端窄的时候 setup-token 会把 token 折成两三行，
# 复制下来带着换行；原来 `read` 只读到第一个换行，后半截漏到命令行上、被当成命令执行
# （11-10 连着三次：两次 401「只收到前半截」，一次第一行没对上开头、末尾几个字符留在提示符后面）。
# 第一行之后，同一次粘贴里剩下的行已经在缓冲区里，`read -t 1` 一秒内就能读到；没有了就停。
IFS= read -rs first || first=""
raw="$first"
while IFS= read -rs -t 1 more; do raw="$raw"$'\n'"$more"; done
echo
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
    echo "粘贴的内容里找不到 sk-ant-oat 开头的 token（复制的是不是别的东西？），没有写入。" >&2
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
    *401*|*nvalid*) echo "  401／invalid 多半是没复制全（折成几行要一起复制），或者粘贴了两次。" >&2 ;;
  esac
  exit 1
fi

printf 'add-generic-password -U -a %s -s %s -w %s\n' "$USER" "$SERVICE" "$TOK" | security -i >/dev/null
unset TOK
if security find-generic-password -s "$SERVICE" >/dev/null 2>&1; then
  echo "已存进钥匙串（${SERVICE}）。下一轮定时任务起就用它，Claude app 关着也照跑。"
else
  echo "写钥匙串失败，什么都没改。" >&2
  exit 1
fi
