"""Manual source expansions kept out of the giant CURATED list.

Imported by resolve_sources. Same dict shape. New shows still go through
feeds.fetch + transcript.acquire (feed/notes/page/youtube/asr).
"""

EXTRA: list[dict] = [
  # --- ideas ---
  dict(id="tyler", name="Conversations with Tyler", zh="Conversations with Tyler",
       cat="ideas", tier=2, lang="en", itunes=983795625,
       feed="https://rss.libsyn.com/shows/137081/destinations/850607.xml",
       yt="UC_AnpBvnhXTcipgGEHLWoOg",
       desc="Tyler Cowen 逼受访者给出可反驳判断，覆盖经济、历史与思想"),
  dict(id="econtalk", name="EconTalk", zh="EconTalk", cat="ideas", tier=2, lang="en",
       itunes=135066958, feed="https://feeds.simplecast.com/wgl4xEgL",
       desc="Russ Roberts 与学者的长谈，机制与判断可回论文核对"),
  dict(id="hiddenbrain", name="Hidden Brain", zh="Hidden Brain", cat="ideas", tier=2, lang="en",
       itunes=1028908750, feed="https://feeds.npr.org/510308/podcast.xml",
       desc="Shankar Vedantam 用行为科学解释日常判断，论断可回实验与论文"),
  dict(id="inourtime", name="In Our Time", zh="In Our Time", cat="ideas", tier=2, lang="en",
       itunes=73330895, feed="https://podcasts.files.bbci.co.uk/b006qykl.rss",
       desc="Melvyn Bragg 与学者讨论思想史与科学史，可被史学界透项抬杠"),
  dict(id="philosophize", name="Philosophize This!", zh="Philosophize This!", cat="ideas", tier=2, lang="en",
       itunes=659155419, feed="https://feeds.megaphone.fm/QCD6036500916",
       desc="Stephen West 逐家讲哲学史，概念链清楚、可回原典"),
  dict(id="knowledge", name="The Knowledge Project", zh="The Knowledge Project", cat="ideas", tier=2, lang="en",
       itunes=990149481, feed="https://theknowledgeproject.libsyn.com/rss",
       desc="Shane Parrish 追决策与心智模型，受访者给出可复制的判断"),
  # --- hist ---
  dict(id="restishistory", name="The Rest Is History", zh="The Rest Is History",
       cat="hist", tier=2, lang="en", itunes=1537788786,
       feed="https://feeds.megaphone.fm/GLT4787413333",
       yt="UCwayCyXbToTPxYJUBcEx74g",
       desc="Holland 与 Sandbrook 系列化深挖历史事件，叙事密、可被史学界抬杠"),
  dict(id="fallciv", name="Fall of Civilizations", zh="Fall of Civilizations",
       cat="hist", tier=2, lang="en", itunes=1449884495,
       feed="https://anchor.fm/s/101adcf44/podcast/rss",
       desc="单文明长篇考古与一手文献，时间线与证据密度极高"),
  dict(id="throughline", name="Throughline", zh="Throughline", cat="hist", tier=2, lang="en",
       itunes=1451872743, feed="https://feeds.npr.org/510333/podcast.xml",
       desc="NPR 把当下事件推回历史线索，一手素材与时间线可核对"),
  dict(id="empire", name="Empire", zh="Empire", cat="hist", tier=2, lang="en",
       itunes=1639562281, feed="https://feeds.megaphone.fm/empire",
       desc="Dalrymple 与 Anand 讲帝国与东方史，一手档案密、可被专业史家抬杠"),
  dict(id="historyextra", name="History Extra", zh="History Extra", cat="hist", tier=2, lang="en",
       itunes=256580326, feed="https://feeds.feedburner.com/historyextra",
       desc="BBC History 学者访谈，论断给出处与反方观点"),
  # --- parent ---
  dict(id="goodinside", name="Good Inside with Dr. Becky", zh="Good Inside",
       cat="parent", tier=2, lang="en", itunes=1561689671,
       feed="https://feeds.simplecast.com/Y5N0xWWZ",
       desc="临床心理育儿框架，论断可在真实家庭场景检验"),
  dict(id="raisinggh", name="Raising Good Humans", zh="Raising Good Humans",
       cat="parent", tier=2, lang="en", itunes=1473072044,
       feed="https://rss.art19.com/raising-good-humans",
       desc="Aliza Pressman 发展心理育儿，原则可回发展科学核对"),
  # --- biz (creator economy) ---
  dict(id="colinsamir", name="The Colin and Samir Show", zh="Colin and Samir",
       cat="biz", tier=3, lang="en", itunes=1379942034,
       feed="https://feeds.megaphone.fm/LI6529969937",
       yt="UCamLstJyCa-t5gfZegxsFMw",
       desc="创作者经济机制：CPM、分成、制作成本与平台规则"),
]
