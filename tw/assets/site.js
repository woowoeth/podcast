/* ourword.ai/podcast — theme, instant search, filters, timestamp seeking.
   No framework, no build step: the feed is already in the HTML, JS only
   hides rows. Works with JS off, just without search. */
(function () {
  'use strict';

  /* ------------------------------------------------------------- 運行時文案 */
  /* **JS 注入的文案也要跟著語言走。** 「零漏譯」那道閘只掃靜態 HTML，
     掃不到這裡——所以英文站上有 10 處中文是它放行的：加載提示、
     全文索引提示、播放/暫停、去 YouTube、三條分享提示。英文讀者點一下分享
     就彈一句中文，這正是"半成品"的樣子。

     繁體不用管：tw.py 會把這個文件裡的中文一併轉成繁體（.js 在它的
     TEXT_EXT 裡），所以這張表只需要 zh 和 en 兩列。 */
  var EN = document.documentElement.lang.slice(0, 2).toLowerCase() === 'en';
  var STR = {
    loading:      ['正在載入…', 'Loading\u2026'],
    loadFailed:   ['加載失敗，點一下重試', 'Could not load. Tap to retry.'],
    idxLoading:   ['正在載入全文索引，稍後會把正文和金句一起搜進來…',
                   'Loading the full-text index \u2014 points and quotes will be '
                   + 'searchable in a moment\u2026'],
    idxFailed:    ['全文索引沒載入成功，現在只搜了標題、結論和標籤。',
                   'The full-text index did not load; searching titles, '
                   + 'summaries and tags only.'],
    videoTitle:   ['原節目視頻', 'Episode video'],
    openOnYT:     ['去 YouTube 打開原視頻', 'Open the video on YouTube'],
    pause:        ['暫停', 'Pause'],
    play:         ['播放', 'Play'],
    copiedWeChat: ['已複製，長按粘貼發給朋友；發朋友圈點右上角 ···',
                   'Copied. Long-press to paste; for Moments use the \u00b7\u00b7\u00b7 '
                   + 'menu at the top right.'],
    copied:       ['已複製，粘到微信、朋友圈或任何地方',
                   'Copied \u2014 paste it anywhere.'],
    copyFailed:   ['複製沒成功，長按選中下面的鏈接：',
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
    // 站點默認淺色，不跟隨系統，所以沒有屬性就是淺色——
    // 這樣第一次點擊一定進深色，而不是"看系統而定"。
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

  /* --------------------------------------------------------------- filters */
  var feed = document.querySelector('[data-feed]');
  if (feed) {
    var input = document.querySelector('[data-search]');
    var chips = [].slice.call(document.querySelectorAll('[data-cat-chip]'));
    var cards = [].slice.call(feed.querySelectorAll('[data-card]'));
    var count = document.querySelector('[data-count]');
    var empty = feed.querySelector('[data-empty]');
    var cat = 'all';

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

    /* 首屏只渲染 24 張卡片（原來 257 張全內聯，index.html 288 KB）。剩下的在
       cards.json 裡，滾到底、點"加載更多"或一開始搜索就補齊。
       **搜索和篩選必須覆蓋全部**，所以在篩選之前一定要先補齊——否則用戶會以為
       站上沒有那篇文章，那比慢更糟。 */
    /* 分頁加載。第一版有兩個毛病，都是用戶挑出來的：
       1. 一次把剩下 231 張全塞進來——那不是分頁，是"晚一點的全量加載"。
       2. 只靠 IntersectionObserver，而它在某些環境下根本不回調（我在瀏覽器面板裡
          新建一個觀察器在同一個元素上也從不觸發）。現在主路徑是滾動監聽 +
          getBoundingClientRect，到處都能跑；IO 只是可選的省電優化。 */
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
      var have = cards.length, total = feed.getAttribute('data-total');
      moreCount.textContent = pageState === 'loading'
        ? T('loading') : have + ' / ' + total;
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
          // 一頁裝不滿一屏時繼續裝，否則滾動條不動就再也觸發不了
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
      var r = sentinel.getBoundingClientRect();
      if (r.top - window.innerHeight < 800) loadPage();
    }

    if (moreBtn) moreBtn.addEventListener('click', function () { loadPage(); });
    if (sentinel) {
      /* 節流用時間戳，不用 requestAnimationFrame。**隱藏或後臺的標籤頁裡 rAF
         回調不執行**，滾動就永遠不觸發加載——我在瀏覽器面板裡查這個 bug 時，
         派發 scroll 事件毫無反應、點按鈕卻正常，就是這個原因。
         IntersectionObserver 在同樣的環境裡也從不回調，所以它只能當可選優化，
         主路徑必須是普通的滾動監聽。 */
      var last = 0;
      var onScroll = function () {
        var now = Date.now();
        if (now - last < 120) return;
        last = now;
        maybeLoad();
      };
      window.addEventListener('scroll', onScroll, { passive: true });
      window.addEventListener('resize', onScroll, { passive: true });
      // 標籤頁從後臺切回來時補一次：後臺期間的滾動可能沒被處理
      document.addEventListener('visibilitychange', function () {
        if (!document.hidden) maybeLoad();
      });
      maybeLoad();          // 首屏可能就已經到底了（窄屏、卡片少）
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
      // 一旦開始搜或篩，先把全部頁都補齊——只篩前 24 張會讓用戶以為站上沒有
      // 那篇文章，那比慢更糟
      if ((terms.length || cat !== 'all') && pageState !== 'done') {
        loadAll();
        return;
      }
      if (terms.length) loadDeep();
      var shown = 0;
      cards.forEach(function (c) {
        var okCat = cat === 'all' || c.getAttribute('data-cat') === cat;
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
      var hero = cards.filter(function (c) { return c.style.display !== 'none'; })[0];
      cards.forEach(function (c) { c.classList.toggle('hero', c === hero && !q && cat === 'all'); });
      sync(q);
    }

    function sync(q) {
      var p = new URLSearchParams();
      if (cat !== 'all') p.set('c', cat);
      if (q) p.set('q', q);
      var s = p.toString();
      history.replaceState(null, '', s ? '?' + s : location.pathname);
    }

    chips.forEach(function (ch) {
      ch.addEventListener('click', function () {
        cat = ch.getAttribute('data-cat-chip');
        chips.forEach(function (o) { o.setAttribute('aria-pressed', String(o === ch)); });
        run();
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

  /* --------------------------------------- 時間戳 → 頁內播放器（音頻或視頻） */
  /* 用 YouTube 官方的 IFrame Player API，不是自己拼 embed 的 src。
     為什麼換：上一版點了假門就新建一個 `?autoplay=1` 的 iframe，而**在新建的
     iframe 上加 autoplay 不算用戶手勢**（iOS 尤其嚴），播放器加載了卻不會動，
     看起來就是"點播放沒生效"。API 的 playVideo() 是在點擊這條鏈路裡調的，算手勢。

     順帶解決了跳轉：seekTo(秒) 就地跳，不用換 src 重載整個播放器——換 src 每次
     都要重新握手，跳一次要等好幾秒，還會丟掉已緩衝的部分。

     API 腳本約 100 KB，所以**只在第一次點擊時才加載**：不點視頻的讀者一個字節
     都不下載，首屏體積不受影響。 */
  var audio = document.querySelector('audio[data-player]');
  var facade = document.querySelector('.video-facade');
  var facadeNode = null;      // 假門本體，放不出來時要放回來
  var ytPlayer = null;        // YT.Player 實例
  var ytReady = false;
  var pendingSeek = null;     // API 還沒就緒時先記下要跳到哪
  var prefer = null;          // 讀者已經在用哪個：'video' | 'audio' | null

  /* 加載官方 API，**並且給它一個截止時間**。
     為什麼需要超時：內容攔截器常把 youtube.com/iframe_api 當追蹤腳本攔掉
     （EasyPrivacy 一類的名單裡就有），而攔法有兩種——
       · 返回錯誤 → onerror 觸發
       · 返回 200 但空 body → **onerror 不觸發**，window.YT 永遠不出現，
         回調永遠不跑，頁面上就掛著一個空的黑框，沒有任何出口
     第二種我原來完全沒處理。實測這臺機器上取 iframe_api 就是 0 字節。 */
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

  /* API 拿不到時的兜底：直接放一個普通 embed iframe。
     **拿不到 API 腳本不等於視頻不能內嵌播** ——實測攔掉 iframe_api 的同時
     nocookie/embed 頁照樣能取到。我原來的兜底直接跳去 YouTube，太悲觀了，
     用戶報的"為啥非要讓我跳出去看"就是這個。
     代價只是跳轉精度：沒有 seekTo，只能靠 ?start= 重設 src。 */
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

  /* 視頻這條路走不通了：把收起的音頻條放出來。
     不這麼做的後果是讀者停在一個黑框上，正文裡幾十個時間戳全都沒處跳。 */
  function revealAudio() {
    var strip = document.querySelector('[data-audio-strip]');
    if (!strip || !strip.hidden) return false;
    strip.hidden = false;
    strip.classList.add('fellback');
    prefer = 'audio';
    return true;
  }

  /* 視頻放不出來時怎麼收場。
     不留那句"這段視頻不能內嵌播放，去 YouTube 打開"——有音頻的話音頻條自己
     會帶一行說明，那句是重複的。沒音頻的 21 篇才需要出口：把封面放回來，點
     它去 YouTube 開原視頻（正文裡每個時間戳的 href 本來也指向那兒）。 */
  function videoFailed(wrap, code) {
    if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
    ytPlayer = null;
    ytReady = false;
    if (revealAudio()) return;
    if (!facadeNode) return;
    facadeNode.hidden = false;
    facadeNode.classList.add('offsite');
    // 說清是誰的問題：101/150 就是作者關掉了站外嵌入，不是我們的頁面壞了
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
    // 外層撐 16:9，iframe 絕對填滿。YT.Player 會把給它的元素**換成** iframe，
    // 換掉之後 class 不一定還在，所以尺寸交給外層，別指望 iframe 自己帶樣式。
    var wrap = document.createElement('div');
    wrap.className = 'vwrap';
    var host = document.createElement('div');
    wrap.appendChild(host);
    // 假門是**藏起來**不是替換掉：放不出來的時候要能把它放回來。21 篇有視頻
    // 沒音頻的，box 一拿掉又沒有兜底就成了一張空卡。
    facadeNode = facade;
    facade.parentNode.insertBefore(wrap, facade);
    facade.hidden = true;
    facade = null;
    loadYTApi(function () {
      ytPlayer = new YT.Player(host, {
        // nocookie 域：點播放之前 YouTube 拿不到任何東西，點了之後也不落
        // 廣告 cookie。YT.Player 默認走 youtube.com，得顯式指定。
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
            // 101/150 是"作者關掉了站外嵌入"，那是 YouTube 那邊的設置，
            // 換成普通 iframe 也放不出來，只能給外鏈。
            // 其餘碼（2/5/100）多是播放器自身問題，普通 iframe 往往還能放。
            var code = ev && ev.data;
            if (window.console) console.warn('[player] YT onError', code);
            if (code === 101 || code === 150) videoFailed(wrap, code);
            else mountPlain(wrap, vid, pendingSeek);
          }
        }
      });
    }, function () {
      // API 腳本被攔或超時 → 直接上普通 embed，別跳出站
      if (window.console) console.warn('[player] iframe_api 拿不到，改用普通 embed');
      mountPlain(wrap, vid, pendingSeek);
    });
  }

  function seekVideo(t) {
    if (plainFrame) {
      // 沒有 API，只能重設 src。跳一次要重新握手，但比不能跳好。
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
    // 跳讀者已經在用的那個播放器；都沒用過時，有視頻就用視頻。
    // （兩個都在頁面上：視頻是加分項，音頻走播客 CDN，一定放得出來。）
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

  /* ----------------------------------------------------------- 音頻控件外觀 */
  /* 原生 <audio controls> 在每個平臺長得都不一樣（Chrome 是灰藥丸、iOS 是另一
     套、Firefox 又一套），放在這張卡里像貼上去的。這裡自己畫：圓形播放鍵 +
     一條進度軌 + mono 時碼，和站上其他部件同一套語言。

     漸進增強：HTML 裡 <audio> 帶著 controls 出，自定義那層默認 hidden。腳本跑
     起來才摘掉 controls、顯出自定義層——腳本沒跑（報錯、被攔）就還是原生控件，
     不會變成一個點不動的死條。 */
  function fmt(t) {
    t = Math.max(0, Math.floor(t || 0));
    var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
    var mm = h ? (m < 10 ? '0' + m : m) : m;
    return (h ? h + ':' : '') + mm + ':' + (s < 10 ? '0' + s : s);
  }

  // 樣式表真的生效了嗎。site.css 裡定義了 --css:ok；取不到就說明這份樣式沒
  // 到位（緩存裡是舊版本、請求被攔、文件 404）。那種情況下**不要**做依賴樣式的
  // DOM 替換：把能用的原生控件換成一條沒樣式、看不見的進度軌，比不換糟得多。
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

      // 拖動：pointer 事件一套搞定鼠標和觸摸，不用分別寫 mouse/touch
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
      // 鍵盤：軌道是 role=slider，左右鍵 ±10 秒
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
  /* 微信和朋友圈不給網頁調起分享——那需要認證公眾號、JS 接口安全域名和服務端
     簽名。所以和「走你」同一套辦法：把內容拼成一段能直接粘貼的文本。手機上先試
     系統分享面板（裝了微信就在裡面），不行就複製，用戶自己粘。 */
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
    // Safari 在非安全上下文、以及舊 WebView 裡沒有 clipboard API
    return new Promise(function (res, rej) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      ta.setSelectionRange(0, ta.value.length);   // iOS 需要顯式選區
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) {}
      ta.remove();
      ok ? res() : rej(new Error('execCommand failed'));
    });
  }

  // 點擊時讀 UA，而不是載入時讀一次：微信內和微信外走的是兩條不同的提示語，
  // 這條分支必須能在開發時真的驗一遍，而不是只靠肉眼讀代碼。
  function inWeChat() { return /MicroMessenger/i.test(navigator.userAgent); }

  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-share]');
    if (!b) return;
    var text = b.getAttribute('data-share-text') || '';
    var url = b.getAttribute('data-share-url') || location.href;
    var title = b.getAttribute('data-share-title') || document.title;

    // 微信內置瀏覽器會宣稱支持 navigator.share，但拉起來的面板往往是空的，
    // 所以在微信裡一律走複製 + 提示右上角菜單——那才是分享到朋友圈的正路。
    var wx = inWeChat();
    if (!wx && navigator.share) {
      // 只傳 text，不傳 url。同時傳兩個的話微信會當成兩個條目：文本正常發出，
      // URL 另存成一個一百多字節的臨時文件跟著發過去（實測截圖裡那個
      // "32058763b0241ae675c…" 就是它）。我們的文本末尾本來就帶鏈接，
      // 少傳一個字段反而乾淨。title 也不傳：微信不用它，而某些客戶端會
      // 把它當成另一個條目。
      navigator.share({ text: text })
        .then(function () { track('share_native'); })
        .catch(function (err) {
          // 用戶主動取消不算失敗，不該彈提示
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
