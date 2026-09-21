# ourword MCP server

把 [原声](https://ourword.ai/podcast/)（ourword.ai）的播客深读开放给 agent 查。

每天从 190 多档中英文播客里挑出值得记住的判断，写成中文深读。**要点、金句、数字
都带时间戳**，点一下回到它在原声里被说出的那一秒 —— 这个服务器把同一批数据变成
可查的接口。

## 为什么需要它

站上本来就有 `llms.txt` / `llms-full.txt`，但那是给「整篇读一遍」准备的：
`llms-full.txt` 有 6.9 MB，agent 要回答「谁讲过 reward hacking」只能把它整份读进
上下文。这个服务器让它先筛出几条，再按需要取那一集的结构。

## 装

不用装。只要有 Python 3.10+：

```json
{
  "mcpServers": {
    "ourword": {
      "command": "python3",
      "args": ["/path/to/mcp/ourword_mcp.py"]
    }
  }
}
```

只用标准库，没有第三方依赖。数据从线上取，所以不会拿到发行当天的旧快照。

## 四个工具

| 工具 | 做什么 |
|---|---|
| `search_episodes` | 按关键词、节目、分类找集 |
| `get_episode` | 一集的要点／金句／数字／术语，每条带时间戳 |
| `list_shows` | 在册节目，各自已深读多少篇、最新一篇是哪天 |
| `latest_episodes` | 最近的深读，可按分类 |

## 关于原文

**这里不提供逐字稿。** 逐字稿是各播客的版权内容，站上任何一份文件里都没有，
这个接口也不会有。给出去的是我们自己写的判断、可核对的数字、带署名的短引用，
以及回到原音频那一秒的链接 —— 引用的人可以自己去核。

## 自建

```bash
OURWORD_SITE=/path/to/your/checkout python3 ourword_mcp.py
```
