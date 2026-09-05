/* ourword.ai/podcast — theme, instant search, filters, timestamp seeking.
   No framework, no build step: the feed is already in the HTML, JS only
   hides rows. Works with JS off, just without search. */
(function () {
  'use strict';

  /* ------------------------------------------------------------- 运行时文案 */
  /* **JS 注入的文案也要跟着语言走。** 「零漏译」那道闸只扫静态 HTML，
     扫不到这里——所以英文站上有 10 处中文是它放行的：加载提示、
     全文索引提示、播放/暂停、去 YouTube、三条分享提示。英文读者点一下分享
     就弹一句中文，这正是"半成品"的样子。

     繁体不用管：tw.py 会把这个文件里的中文一并转成繁体（.js 在它的
     TEXT_EXT 里），所以这张表只需要 zh 和 en 两列。 */
  var EN = document.documentElement.lang.slice(0, 2).toLowerCase() === 'en';
  var STR = {
    loading:      ['正在载入…', 'Loading\u2026'],
    loadFailed:   ['加载失败，点一下重试', 'Could not load. Tap to retry.'],
    idxLoading:   ['正在载入全文索引，稍后会把正文和金句一起搜进来…',
                   'Loading the full-text index \u2014 points and quotes will be '
                   + 'searchable in a moment\u2026'],
    idxFailed:    ['全文索引没载入成功，现在只搜了标题、结论和标签。',
                   'The full-text index did not load; searching titles, '
                   + 'summaries and tags only.'],
    videoTitle:   ['原节目视频', 'Episode video'],
    openOnYT:     ['去 YouTube 打开原视频', 'Open the video on YouTube'],
    pause:        ['暂停', 'Pause'],
    play:         ['播放', 'Play'],
    copiedWeChat: ['已复制，长按粘贴发给朋友；发朋友圈点右上角 ···',
                   'Copied. Long-press to paste; for Moments use the \u00b7\u00b7\u00b7 '
                   + 'menu at the top right.'],
    copied:       ['已复制，粘到微信、朋友圈或任何地方',
                   'Copied \u2014 paste it anywhere.'],
    copyFailed:   ['复制没成功，长按选中下面的链接：',
                   'Copy did not work. Select this link by hand: ']
  };
  function T(k) { var v = STR[k]; return v ? (EN ? v[1] : v[0]) : k; }

  /* ---------------------------------------------------------------- theme */
  var KEY = 'podcast-theme';
  function apply(t) {
    if (t === 'light' || t === 'dark') document.documentElement.setAttribute('data-theme', t);
    else document.documentElement.removeAttribute('data-theme');
  }
  try { apply(localStorage.getItem(KEY)); } catch (e) {}

  function current() {
    // 站点默认浅色，不跟随系统，所以没有属性就是浅色——
    // 这样第一次点击一定进深色，而不是"看系统而定"。
    return document.documentElement.getAttribute('data-theme') === 'dark'
      ? 'dark' : 'light';
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-theme-toggle]');
    if (!b) return;
    var next = current() === 'dark' ? 'light' : 'dark';
    apply(next);
    try { localStorage.setItem(KEY, next); } catch (err) {}
  });

  /* ------------------------------------------------- back to where I was */
  /* 从单集页返回要停在原位。列表是静态的，浏览器多半自己会恢复；但一旦
     筛选或搜索改变了可见卡片，恢复到的那个像素位置对应的已经是别的内容。
     所以连筛选和搜索一起记：回来先把状态还原，再放位置。
     只有「返回」才恢复，重新打开首页不该跳到半截 —— 存完就清。 */
  (function () {
    var KEY = 'pod_feed_pos';
    addEventListener('pagehide', function () {
      try {
        var input = document.querySelector('[data-search]');
        var on = document.querySelector('[data-cat-chip][aria-pressed="true"]');
        sessionStorage.setItem(KEY, JSON.stringify({
          y: scrollY,
          cat: on ? on.getAttribute('data-cat-chip') : '',
          q: input ? input.value : ''
        }));
      } catch (e) {}
    });
    var raw = null;
    try { raw = sessionStorage.getItem(KEY); sessionStorage.removeItem(KEY); } catch (e) {}
    if (!raw) return;
    var st = null;
    try { st = JSON.parse(raw); } catch (e) {}
    if (!st || !st.y) return;
    var nav = (performance.getEntriesByType && performance.getEntriesByType('navigation')[0]) || {};
    var back = nav.type === 'back_forward'
      || (document.referrer && document.referrer.indexOf(location.origin) === 0);
    if (!back) return;
    addEventListener('load', function () {
      setTimeout(function () {
        var input = document.querySelector('[data-search]');
        if (st.q && input && input.value !== st.q) {
          input.value = st.q;
          input.dispatchEvent(new Event('input'));
        }
        if (st.cat) {
          var b = document.querySelector('[data-cat-chip="' + st.cat + '"]');
          if (b && b.getAttribute('aria-pressed') !== 'true') b.click();
        }
        scrollTo(0, st.y);
      }, 60);
    });
  })();

  /* --------------------------------------------------------------- filters */
  var feed = document.querySelector('[data-feed]');
  if (feed) {
    var input = document.querySelector('[data-search]');
    var chips = [].slice.call(document.querySelectorAll('[data-cat-chip]'));
    var cards = [].slice.call(feed.querySelectorAll('[data-card]'));
    var count = document.querySelector('[data-count]');
    var empty = feed.querySelector('[data-empty]');
    // 默认档是「最新」（近七天，构建期算好、卡上打 data-new）。
    // 「全部」去掉了：每次打开都要载入整个存档，而读者要的是最近的。
    var cat = 'new';

    // Pre-lowercase the inline haystack once; filtering then costs nothing.
    // The inline text is title + dek + source + tags only, so search works
    // immediately. The full index — every point body, every quote in both
    // languages, the facts and the glossary — is fetched on the first
    // keystroke and takes over once it lands.
    cards.forEach(function (c) {
      c._hay = (c.getAttribute('data-hay') || '').toLowerCase();
      c._slug = decodeURIComponent((c.getAttribute('href') || '').replace(/.*\/(?:p|e)\/|\/$/g, ''));
    });

    var deep = null, deepState = 'idle';
    var base = (document.querySelector('link[rel="alternate"]') || {}).href || '';
    var indexUrl = base ? base.replace(/feed\.xml$/, 'search.json') : 'search.json';

    /* 首屏只渲染 24 张卡片（原来 257 张全内联，index.html 288 KB）。剩下的在
       cards.json 里，滚到底、点"加载更多"或一开始搜索就补齐。
       **搜索和筛选必须覆盖全部**，所以在筛选之前一定要先补齐——否则用户会以为
       站上没有那篇文章，那比慢更糟。 */
    /* 分页加载。第一版有两个毛病，都是用户挑出来的：
       1. 一次把剩下 231 张全塞进来——那不是分页，是"晚一点的全量加载"。
       2. 只靠 IntersectionObserver，而它在某些环境下根本不回调（我在浏览器面板里
          新建一个观察器在同一个元素上也从不触发）。现在主路径是滚动监听 +
          getBoundingClientRect，到处都能跑；IO 只是可选的省电优化。 */
    var sentinel = document.querySelector('[data-sentinel]');
    var moreBtn = document.querySelector('[data-more]');
    var moreCount = document.querySelector('[data-more-count]');
    var pageSize = parseInt(feed.getAttribute('data-page-size'), 10) || 24;
    var totalPages = parseInt(feed.getAttribute('data-pages'), 10) || 0;
    var nextPage = 1;
    var pageState = totalPages ? 'idle' : 'done';
    var waiting = [];

    function pageUrl(n) {
      return base ? base.replace(/feed\.xml$/, 'cards-' + n + '.json')
                  : 'cards-' + n + '.json';
    }

    function showCount() {
      if (!moreCount) return;
      if (pageState === 'loading') { moreCount.textContent = T('loading'); return; }
      // 计数要跟着**当前档位**。默认档是「最新」，而它就是内联的这一批，
      // 显示「41 / 274」会让人以为还有 233 篇没载入、催他往下滚。
      var shown = cards.filter(function (c) { return c.style.display !== 'none'; }).length;
      var total = feed.getAttribute('data-total');
      // 「最新」档底下那句提示已经把话说完了，再挂一个孤零零的数字只是噪音。
      moreCount.textContent = (cat === 'new') ? '' : shown + ' / ' + total;
    }

    function loadPage(then) {
      if (pageState === 'done') { then && then(); return; }
      if (pageState === 'loading') { waiting.push(then); return; }
      pageState = 'loading';
      if (moreBtn) moreBtn.hidden = true;
      showCount();
      fetch(pageUrl(nextPage)).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (html) {
          if (!html) throw new Error('bad payload');
          var box = document.createElement('div');
          box.innerHTML = html.join('');
          var anchor = feed.querySelector('[data-empty]');
          [].slice.call(box.querySelectorAll('[data-card]')).forEach(function (c) {
            c._hay = (c.getAttribute('data-hay') || '').toLowerCase();
            c._slug = decodeURIComponent((c.getAttribute('href') || '').replace(/.*\/p\/|\/$/g, ''));
            cards.push(c);
            anchor ? feed.insertBefore(c, anchor) : feed.appendChild(c);
          });
          nextPage++;
          pageState = nextPage > totalPages ? 'done' : 'idle';
          if (pageState === 'done' && sentinel) sentinel.remove();
          showCount();
          run();
          var fs = [then].concat(waiting); waiting = [];
          fs.forEach(function (f) { f && f(); });
          // 一页装不满一屏时继续装，否则滚动条不动就再也触发不了
          if (pageState === 'idle') maybeLoad();
        })
        .catch(function () {
          pageState = 'idle';
          if (moreBtn) { moreBtn.hidden = false; moreBtn.textContent = T('loadFailed'); }
          if (moreCount) moreCount.textContent = '';
          var fs = [then].concat(waiting); waiting = [];
          fs.forEach(function (f) { f && f(); });
        });
    }

    function loadAll(then) {
      if (pageState === 'done') { then && then(); return; }
      loadPage(function () { loadAll(then); });
    }

    function maybeLoad() {
      if (pageState !== 'idle' || !sentinel) return;
      // 「最新」这一档就是内联的这一批，往下没有更多了——滚到底不该悄悄把
      // 整个存档拉下来。想看更多的读者点分类，那时才补齐（run() 里那条）。
      if (cat === 'new') return;
      var r = sentinel.getBoundingClientRect();
      if (r.top - window.innerHeight < 800) loadPage();
    }

    if (moreBtn) moreBtn.addEventListener('click', function () { loadPage(); });
    if (sentinel) {
      /* 节流用时间戳，不用 requestAnimationFrame。**隐藏或后台的标签页里 rAF
         回调不执行**，滚动就永远不触发加载——我在浏览器面板里查这个 bug 时，
         派发 scroll 事件毫无反应、点按钮却正常，就是这个原因。
         IntersectionObserver 在同样的环境里也从不回调，所以它只能当可选优化，
         主路径必须是普通的滚动监听。 */
      var last = 0;
      var onScroll = function () {
        var now = Date.now();
        if (now - last < 120) return;
        last = now;
        maybeLoad();
      };
      window.addEventListener('scroll', onScroll, { passive: true });
      window.addEventListener('resize', onScroll, { passive: true });
      // 标签页从后台切回来时补一次：后台期间的滚动可能没被处理
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) maybeLoad();
      });
      maybeLoad();          // 首屏可能就已经到底了（窄屏、卡片少）
      showCount();
    }

    function loadDeep() {
      if (deepState !== 'idle') return;
      deepState = 'loading';
      fetch(indexUrl).then(function (r) { return r.ok ? r.json() : null; })
        .then(function (rows) {
          if (!rows) { deepState = 'failed'; return; }
          deep = Object.create(null);
          rows.forEach(function (r) { deep[r.s] = r.h; });
          deepState = 'ready';
          run();                       // re-run so the current query deepens
        })
        .catch(function () { deepState = 'failed'; });
    }

    function run() {
      var q = (input && input.value || '').trim().toLowerCase();
      var terms = q ? q.split(/\s+/) : [];
      // 一旦开始搜或**选了某个分类**，先把全部页都补齐——只筛前 24 张会让
      // 用户以为站上没有那篇文章，那比慢更糟。
      // 但「最新」这一档例外：它就是内联的这一批（构建期保证 data-new 的
      // 那些全部内联），补齐反而把整个存档拉下来，正是要避免的那件事。
      if ((terms.length || (cat !== 'new' && cat !== 'all')) &&
          pageState !== 'done') {
        loadAll();
        return;
      }
      if (terms.length) loadDeep();
      var shown = 0;
      cards.forEach(function (c) {
        // 「最新」认的是构建期打的 data-new，客户端不自己算日期——
        // 两边各算一次，迟早算出两个答案。
        var okCat = cat === 'all' ? true
                  : cat === 'new' ? c.hasAttribute('data-new')
                  : c.getAttribute('data-cat') === cat;
        var hay = c._hay;
        if (deep && deep[c._slug]) hay = c._hay + ' ' + deep[c._slug];
        var okQ = true;
        for (var i = 0; i < terms.length; i++) {
          if (hay.indexOf(terms[i]) === -1) { okQ = false; break; }
        }
        var on = okCat && okQ;
        // The hero card can only span two columns while it is the first shown.
        c.style.display = on ? '' : 'none';
        if (on) shown++;
      });
      if (empty) {
        empty.hidden = shown !== 0;
        var note = empty.querySelector('[data-deep-note]');
        if (note) {
          note.hidden = deepState === 'ready' || deepState === 'idle';
          note.textContent = deepState === 'loading'
            ? T('idxLoading')
            : T('idxFailed');
        }
      }
      if (count) count.textContent = shown;
      showCount();
      tailNote();
      var hero = cards.filter(function (c) { return c.style.display !== 'none'; })[0];
      cards.forEach(function (c) {
        c.classList.toggle('hero', c === hero && !q && cat === 'new');
      });
      sync(q);
    }

    function sync(q) {
      var p = new URLSearchParams();
      if (cat !== 'new') p.set('c', cat);
      if (q) p.set('q', q);
      var s = p.toString();
      history.replaceState(null, '', s ? '?' + s : location.pathname);
    }

    // 「最新」档到底了要有出口，不能静默 dead-end：读者滚完 41 篇看到的
    // 应该是「按分类看更多」，而不是一个不再动的加载提示。
    function tailNote() {
      var end = document.querySelector('[data-feed-end]');
      if (!end) return;
      end.hidden = cat !== 'new';
      if (moreBtn) moreBtn.hidden = moreBtn.hidden || cat === 'new';
      if (sentinel) sentinel.classList.toggle('at-end', cat === 'new');
    }

    chips.forEach(function (ch) {
      ch.addEventListener('click', function () {
        cat = ch.getAttribute('data-cat-chip');
        chips.forEach(function (o) { o.setAttribute('aria-pressed', String(o === ch)); });
        run();
        /* 换筛选要把列表带回开头。原来不动：读者往下翻了几屏，一换分类
           内容整个换掉、位置却留在原地，落在新一批卡片的中间，前面那些
           永远不会被看到。换分类这个动作的意思就是「给我看别的」。
           回到筛选条本身，不是回页首 —— 上面还有搜索框，刚翻过去了。
           瞬时不平滑：内容已经换掉，平滑滚过去是滑过一堆不存在的东西。 */
        var bar = ch.parentElement;
        if (bar) {
          var y = bar.getBoundingClientRect().top + scrollY - 8;
          if (scrollY > y) scrollTo(0, y);
        }
      });
    });
    if (input) {
      var timer;
      input.addEventListener('input', function () {
        clearTimeout(timer); timer = setTimeout(run, 60);
      });
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') { input.value = ''; run(); input.blur(); }
      });
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === '/' && document.activeElement !== input &&
          !/^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) {
        e.preventDefault(); if (input) input.focus();
      }
    });

    // Restore state from a shared URL.
    var qs = new URLSearchParams(location.search);
    if (qs.get('c')) {
      var want = qs.get('c');
      chips.forEach(function (o) {
        var on = o.getAttribute('data-cat-chip') === want;
        o.setAttribute('aria-pressed', String(on));
        if (on) cat = want;
      });
    }
    if (qs.get('q') && input) input.value = qs.get('q');
    if (qs.get('c') || qs.get('q')) run();
  }

  /* --------------------------------------- 时间戳 → 页内播放器（音频或视频） */
  /* 用 YouTube 官方的 IFrame Player API，不是自己拼 embed 的 src。
     为什么换：上一版点了假门就新建一个 `?autoplay=1` 的 iframe，而**在新建的
     iframe 上加 autoplay 不算用户手势**（iOS 尤其严），播放器加载了却不会动，
     看起来就是"点播放没生效"。API 的 playVideo() 是在点击这条链路里调的，算手势。

     顺带解决了跳转：seekTo(秒) 就地跳，不用换 src 重载整个播放器——换 src 每次
     都要重新握手，跳一次要等好几秒，还会丢掉已缓冲的部分。

     API 脚本约 100 KB，所以**只在第一次点击时才加载**：不点视频的读者一个字节
     都不下载，首屏体积不受影响。 */
  var audio = document.querySelector('audio[data-player]');
  var facade = document.querySelector('.video-facade');
  var facadeNode = null;      // 假门本体，放不出来时要放回来
  var ytPlayer = null;        // YT.Player 实例
  var ytReady = false;
  var pendingSeek = null;     // API 还没就绪时先记下要跳到哪
  var prefer = null;          // 读者已经在用哪个：'video' | 'audio' | null

  /* 加载官方 API，**并且给它一个截止时间**。
     为什么需要超时：内容拦截器常把 youtube.com/iframe_api 当追踪脚本拦掉
     （EasyPrivacy 一类的名单里就有），而拦法有两种——
       · 返回错误 → onerror 触发
       · 返回 200 但空 body → **onerror 不触发**，window.YT 永远不出现，
         回调永远不跑，页面上就挂着一个空的黑框，没有任何出口
     第二种我原来完全没处理。实测这台机器上取 iframe_api 就是 0 字节。 */
  function loadYTApi(cb, fail) {
    if (window.YT && window.YT.Player) return cb();
    var fired = false;
    var once = function (f) { return function () { if (!fired) { fired = true; f(); } }; };
    var ok = once(cb), no = once(fail);
    setTimeout(function () { if (!(window.YT && window.YT.Player)) no(); }, 6000);
    var waiting = window.__ytApiWaiting || (window.__ytApiWaiting = []);
    waiting.push(ok);
    if (window.__ytApiLoading) return;
    window.__ytApiLoading = true;
    window.onYouTubeIframeAPIReady = function () {
      (window.__ytApiWaiting || []).forEach(function (f) { f(); });
      window.__ytApiWaiting = [];
    };
    var s = document.createElement('script');
    s.src = 'https://www.youtube.com/iframe_api';
    s.async = true;
    s.onerror = function () { window.__ytApiLoading = false; no(); };
    document.head.appendChild(s);
  }

  /* API 拿不到时的兜底：直接放一个普通 embed iframe。
     **拿不到 API 脚本不等于视频不能内嵌播** ——实测拦掉 iframe_api 的同时
     nocookie/embed 页照样能取到。我原来的兜底直接跳去 YouTube，太悲观了，
     用户报的"为啥非要让我跳出去看"就是这个。
     代价只是跳转精度：没有 seekTo，只能靠 ?start= 重设 src。 */
  var plainFrame = null;

  function mountPlain(wrap, vid, seconds) {
    if (!wrap || !wrap.isConnected) return;
    var f = document.createElement('iframe');
    f.setAttribute('allow', 'accelerometer; autoplay; clipboard-write; ' +
      'encrypted-media; gyroscope; picture-in-picture');
    f.setAttribute('allowfullscreen', '');
    f.setAttribute('title', T('videoTitle'));
    f.src = plainSrc(vid, seconds);
    wrap.innerHTML = '';
    wrap.appendChild(f);
    plainFrame = f;
    plainFrame.setAttribute('data-yt', vid);
  }

  function plainSrc(vid, seconds) {
    return 'https://www.youtube-nocookie.com/embed/' + vid
      + '?rel=0&playsinline=1&autoplay=1'
      + (seconds ? '&start=' + Math.floor(seconds) : '');
  }

  /* 视频这条路走不通了：把收起的音频条放出来。
     不这么做的后果是读者停在一个黑框上，正文里几十个时间戳全都没处跳。 */
  function revealAudio() {
    var strip = document.querySelector('[data-audio-strip]');
    if (!strip || !strip.hidden) return false;
    strip.hidden = false;
    strip.classList.add('fellback');
    prefer = 'audio';
    return true;
  }

  /* 视频放不出来时怎么收场。
     不留那句"这段视频不能内嵌播放，去 YouTube 打开"——有音频的话音频条自己
     会带一行说明，那句是重复的。没音频的 21 篇才需要出口：把封面放回来，点
     它去 YouTube 开原视频（正文里每个时间戳的 href 本来也指向那儿）。 */
  function videoFailed(wrap, code) {
    if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
    ytPlayer = null;
    ytReady = false;
    if (revealAudio()) return;
    if (!facadeNode) return;
    facadeNode.hidden = false;
    facadeNode.classList.add('offsite');
    // 说清是谁的问题：101/150 就是作者关掉了站外嵌入，不是我们的页面坏了
    if (code === 101 || code === 150) facadeNode.classList.add('noembed');
    facadeNode.setAttribute('aria-label', T('openOnYT'));
    facadeNode.onclick = function (ev) {
      ev.preventDefault();
      window.open('https://www.youtube.com/watch?v=' + facadeNode.getAttribute('data-yt'),
                  '_blank', 'noopener');
    };
  }

  function mountYouTube(seconds) {
    if (!facade && !ytPlayer && !plainFrame) return;
    if (ytPlayer || plainFrame) {
      if (seconds != null) seekVideo(seconds);
      return;
    }
    var vid = facade.getAttribute('data-yt');
    if (!vid) return;
    pendingSeek = seconds;
    // 外层撑 16:9，iframe 绝对填满。YT.Player 会把给它的元素**换成** iframe，
    // 换掉之后 class 不一定还在，所以尺寸交给外层，别指望 iframe 自己带样式。
    var wrap = document.createElement('div');
    wrap.className = 'vwrap';
    var host = document.createElement('div');
    wrap.appendChild(host);
    // 假门是**藏起来**不是替换掉：放不出来的时候要能把它放回来。21 篇有视频
    // 没音频的，box 一拿掉又没有兜底就成了一张空卡。
    facadeNode = facade;
    facade.parentNode.insertBefore(wrap, facade);
    facade.hidden = true;
    facade = null;
    loadYTApi(function () {
      ytPlayer = new YT.Player(host, {
        // nocookie 域：点播放之前 YouTube 拿不到任何东西，点了之后也不落
        // 广告 cookie。YT.Player 默认走 youtube.com，得显式指定。
        host: 'https://www.youtube-nocookie.com',
        videoId: vid,
        playerVars: {
          rel: 0, playsinline: 1, modestbranding: 1,
          start: pendingSeek ? Math.floor(pendingSeek) : 0
        },
        events: {
          onReady: function (ev) {
            ytReady = true;
            if (pendingSeek != null) ev.target.seekTo(pendingSeek, true);
            pendingSeek = null;
            try { ev.target.playVideo(); } catch (err) {}
          },
          onError: function (ev) {
            // 101/150 是"作者关掉了站外嵌入"，那是 YouTube 那边的设置，
            // 换成普通 iframe 也放不出来，只能给外链。
            // 其余码（2/5/100）多是播放器自身问题，普通 iframe 往往还能放。
            var code = ev && ev.data;
            if (window.console) console.warn('[player] YT onError', code);
            if (code === 101 || code === 150) videoFailed(wrap, code);
            else mountPlain(wrap, vid, pendingSeek);
          }
        }
      });
    }, function () {
      // API 脚本被拦或超时 → 直接上普通 embed，别跳出站
      if (window.console) console.warn('[player] iframe_api 拿不到，改用普通 embed');
      mountPlain(wrap, vid, pendingSeek);
    });
  }

  function seekVideo(t) {
    if (plainFrame) {
      // 没有 API，只能重设 src。跳一次要重新握手，但比不能跳好。
      plainFrame.src = plainSrc(plainFrame.getAttribute('data-yt'), t);
      return;
    }
    if (!ytPlayer) return;
    if (!ytReady) { pendingSeek = t; return; }
    try {
      ytPlayer.seekTo(t, true);
      ytPlayer.playVideo();
    } catch (err) {}
  }

  if (facade) {
    facade.addEventListener('click', function () {
      prefer = 'video';
      mountYouTube(null);
    });
  }
  if (audio) {
    audio.addEventListener('play', function () { prefer = 'audio'; });
  }

  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[data-t]');
    if (!a) return;
    var t = parseInt(a.getAttribute('data-t'), 10);
    if (isNaN(t)) return;
    // 跳读者已经在用的那个播放器；都没用过时，有视频就用视频。
    // （两个都在页面上：视频是加分项，音频走播客 CDN，一定放得出来。）
    var strip = document.querySelector('[data-audio-strip]');
    var audioUsable = audio && strip && !strip.hidden;
    var useAudio = audioUsable && (prefer === 'audio' || (!facade && !ytPlayer));
    if (useAudio) {
      e.preventDefault();
      try {
        audio.currentTime = t;
        audio.play();
        audio.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      } catch (err) {}
      return;
    }
    if (facade || ytPlayer || plainFrame) {
      e.preventDefault();
      mountYouTube(t);
      var box = document.querySelector('[data-player-box]');
      if (box) box.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  });

  /* ----------------------------------------------------------- 音频控件外观 */
  /* 原生 <audio controls> 在每个平台长得都不一样（Chrome 是灰药丸、iOS 是另一
     套、Firefox 又一套），放在这张卡里像贴上去的。这里自己画：圆形播放键 +
     一条进度轨 + mono 时码，和站上其他部件同一套语言。

     渐进增强：HTML 里 <audio> 带着 controls 出，自定义那层默认 hidden。脚本跑
     起来才摘掉 controls、显出自定义层——脚本没跑（报错、被拦）就还是原生控件，
     不会变成一个点不动的死条。 */
  function fmt(t) {
    t = Math.max(0, Math.floor(t || 0));
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    var mm = h ? (m < 10 ? '0' + m : m) : m;
    return (h ? h + ':' : '') + mm + ':' + (s < 10 ? '0' + s : s);
  }

  // 样式表真的生效了吗。site.css 里定义了 --css:ok；取不到就说明这份样式没
  // 到位（缓存里是旧版本、请求被拦、文件 404）。那种情况下**不要**做依赖样式的
  // DOM 替换：把能用的原生控件换成一条没样式、看不见的进度轨，比不换糟得多。
  var cssOk = false;
  try {
    cssOk = getComputedStyle(document.documentElement)
      .getPropertyValue('--css').trim() === 'ok';
  } catch (e) {}

  if (audio) {
    var ui = document.querySelector('[data-audio-ui]');
    if (ui && cssOk) {
      audio.removeAttribute('controls');
      ui.hidden = false;
      var btn = ui.querySelector('.aplay');
      var bar = ui.querySelector('.abar');
      var fill = ui.querySelector('.afill');
      var cur = ui.querySelector('.acur');
      var tot = ui.querySelector('.atot');
      var known = parseInt(ui.getAttribute('data-dur') || '0', 10) || 0;

      var total = function () {
        return isFinite(audio.duration) && audio.duration ? audio.duration : known;
      };
      var paint = function () {
        var d = total();
        fill.style.width = d ? (Math.min(1, audio.currentTime / d) * 100) + '%' : '0%';
        cur.textContent = fmt(audio.currentTime);
        tot.textContent = d ? fmt(d) : '--:--';
      };
      var seekTo = function (ratio) {
        var d = total();
        if (!d) return;
        try { audio.currentTime = Math.min(d - 0.2, Math.max(0, ratio * d)); } catch (e) {}
        paint();
      };
      var ratioAt = function (clientX) {
        var b = bar.getBoundingClientRect();
        return b.width ? (clientX - b.left) / b.width : 0;
      };

      btn.addEventListener('click', function () {
        if (audio.paused) audio.play(); else audio.pause();
      });
      var sync = function () {
        var on = !audio.paused && !audio.ended;
        ui.classList.toggle('playing', on);
        btn.setAttribute('aria-label', on ? T('pause') : T('play'));
      };
      audio.addEventListener('play', sync);
      audio.addEventListener('pause', sync);
      audio.addEventListener('ended', sync);
      audio.addEventListener('timeupdate', paint);
      audio.addEventListener('loadedmetadata', paint);

      // 拖动：pointer 事件一套搞定鼠标和触摸，不用分别写 mouse/touch
      var dragging = false;
      bar.addEventListener('pointerdown', function (ev) {
        dragging = true;
        try { bar.setPointerCapture(ev.pointerId); } catch (e) {}
        seekTo(ratioAt(ev.clientX));
      });
      bar.addEventListener('pointermove', function (ev) {
        if (dragging) seekTo(ratioAt(ev.clientX));
      });
      bar.addEventListener('pointerup', function () { dragging = false; });
      bar.addEventListener('pointercancel', function () { dragging = false; });
      // 键盘：轨道是 role=slider，左右键 ±10 秒
      bar.addEventListener('keydown', function (ev) {
        var step = ev.key === 'ArrowLeft' ? -10 : ev.key === 'ArrowRight' ? 10 : 0;
        if (!step) return;
        ev.preventDefault();
        var d = total();
        if (d) seekTo((audio.currentTime + step) / d);
      });

      tot.textContent = known ? fmt(known) : '--:--';
      paint();
      sync();
    }
  }

  /* ---------------------------------------------------------------- share */
  /* 微信和朋友圈不给网页调起分享——那需要认证公众号、JS 接口安全域名和服务端
     签名。所以和「走你」同一套办法：把内容拼成一段能直接粘贴的文本。手机上先试
     系统分享面板（装了微信就在里面），不行就复制，用户自己粘。 */
  var toastEl;
  function toast(msg) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.className = 'toast';
      toastEl.setAttribute('role', 'status');
      toastEl.setAttribute('aria-live', 'polite');
      document.body.appendChild(toastEl);
    }
    toastEl.textContent = msg;
    toastEl.classList.add('on');
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { toastEl.classList.remove('on'); }, 2600);
  }

  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // Safari 在非安全上下文、以及旧 WebView 里没有 clipboard API
    return new Promise(function (res, rej) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, ta.value.length);   // iOS 需要显式选区
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      ta.remove();
      ok ? res() : rej(new Error('execCommand failed'));
    });
  }

  // 点击时读 UA，而不是载入时读一次：微信内和微信外走的是两条不同的提示语，
  // 这条分支必须能在开发时真的验一遍，而不是只靠肉眼读代码。
  function inWeChat() { return /MicroMessenger/i.test(navigator.userAgent); }

  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-share]');
    if (!b) return;
    var text = b.getAttribute('data-share-text') || '';
    var url = b.getAttribute('data-share-url') || location.href;
    var title = b.getAttribute('data-share-title') || document.title;

    // 微信内置浏览器会宣称支持 navigator.share，但拉起来的面板往往是空的，
    // 所以在微信里一律走复制 + 提示右上角菜单——那才是分享到朋友圈的正路。
    var wx = inWeChat();
    if (!wx && navigator.share) {
      // **要传 url。** 之前只传 text（末尾自带链接），微信收到的是一段
      // 纯文本 —— 它没有链接可认，分享卡就是一块灰色占位，页面上的
      // og:image / og:title / og:description 从头到尾没被用到。用户报了
      // 好几轮「分享图没显示」，查到最后不是图的问题，是压根没走链接卡。
      //
      // 当初不传 url 的理由是「同时传两个微信会当成两个条目，URL 另存成
      // 一个临时文件跟着发过去」—— 那是因为**文本里也带着同一个链接**，
      // 一次分享出现了两个 URL。现在 desc 是不带链接的一句简介，
      // 链接只由 url 字段出一次，重复没有了，链接卡才建得起来。
      var desc = b.getAttribute('data-share-desc') || '';
      navigator.share({ title: title, text: desc, url: url })
        .then(function () { track('share_native'); })
        .catch(function (err) {
          // 用户主动取消不算失败，不该弹提示
          if (err && err.name === 'AbortError') return;
          fallback(text);
        });
      return;
    }
    fallback(text);

    function fallback(t) {
      copy(t).then(function () {
        track('share_copy');
        toast(wx ? T('copiedWeChat') : T('copied'));
      }).catch(function () {
        toast(T('copyFailed') + url);
      });
    }
  });

  function track(name) {
    try { if (window.gtag) window.gtag('event', name); } catch (e) {}
  }

  /* --------------------------------------------------- lazy cover fallback */
  [].forEach.call(document.querySelectorAll('.cover img'), function (img) {
    img.addEventListener('error', function () {
      var f = document.createElement('div');
      f.className = 'fallback';
      f.textContent = img.getAttribute('data-initial') || '◎';
      img.replaceWith(f);
    });
  });
})();
