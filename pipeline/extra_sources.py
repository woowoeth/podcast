"""Manual source expansions kept out of the giant CURATED list.

Imported by resolve_sources. Same dict shape. New shows still go through
feeds.fetch + transcript.acquire (feed/notes/page/youtube/asr).
"""

EXTRA: list[dict] = [
  dict(id="tyler", name="Conversations with Tyler", zh="Conversations with Tyler",
       cat="ideas", tier=3, lang="en", itunes=983795625,
       feed="https://rss.libsyn.com/shows/137081/destinations/850607.xml",
       yt="UC_AnpBvnhXTcipgGEHLWoOg",
       desc="Tyler Cowen 逼受访者给出可反驳判断，覆盖经济、历史与思想"),
  dict(id="econtalk", name="EconTalk", zh="EconTalk", cat="ideas", tier=3, lang="en",
       itunes=135066958, feed="https://feeds.simplecast.com/wgl4xEgL",
       desc="Russ Roberts 与学者的长谈，机制与判断可回论文核对"),
  dict(id="restishistory", name="The Rest Is History", zh="The Rest Is History",
       cat="hist", tier=3, lang="en", itunes=1537788786,
       feed="https://feeds.megaphone.fm/GLT4787413333",
       yt="UCwayCyXbToTPxYJUBcEx74g",
       desc="Holland 与 Sandbrook 系列化深挖历史事件，叙事密、可被史学界抬杠"),
  dict(id="fallciv", name="Fall of Civilizations", zh="Fall of Civilizations",
       cat="hist", tier=3, lang="en", itunes=1449884495,
       feed="https://anchor.fm/s/101adcf44/podcast/rss",
       desc="单文明长篇考古与一手文献，时间线与证据密度极高"),
  dict(id="colinsamir", name="The Colin and Samir Show", zh="Colin and Samir",
       cat="biz", tier=3, lang="en", itunes=1379942034,
       feed="https://feeds.megaphone.fm/LI6529969937",
       yt="UCamLstJyCa-t5gfZegxsFMw",
       desc="创作者经济机制：CPM、分成、制作成本与平台规则"),
  dict(id="goodinside", name="Good Inside with Dr. Becky", zh="Good Inside",
       cat="parent", tier=3, lang="en", itunes=1561689671,
       feed="https://feeds.simplecast.com/Y5N0xWWZ",
       desc="临床心理育儿框架，论断可在真实家庭场景检验"),
]
