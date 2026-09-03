/* ourword.ai/podcast — theme, instant search, filters, timestamp seeking.
   No framework, no build step: the feed is already in the HTML, JS only
   hides rows. Works with JS off, just without search. */
(function () {
  'use strict';

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
      var have = cards.length, total = feed.getAttribute('data-total');
      moreCount.textContent = pageState === 'loading'
        ? '正在载入…' : have + ' / ' + total;
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
          if (moreBtn) { moreBtn.hidden = false; moreBtn.textContent = '加载失败，点一下重试'; }
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
      // 一旦开始搜或筛，先把全部页都补齐——只筛前 24 张会让用户以为站上没有
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
            ? '正在载入全文索引，稍后会把正文和金句一起搜进来…'
            : '全文索引没载入成功，现在只搜了标题、结论和标签。';
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
  var ytPlayer = null;        // YT.Player 实例
  var ytReady = false;
  var pendingSeek = null;     // API 还没就绪时先记下要跳到哪
  var prefer = null;          // 读者已经在用哪个：'video' | 'audio' | null

  function loadYTApi(cb) {
    if (window.YT && window.YT.Player) return cb();
    var waiting = window.__ytApiWaiting || (window.__ytApiWaiting = []);
    waiting.push(cb);
    if (window.__ytApiLoading) return;
    window.__ytApiLoading = true;
    window.onYouTubeIframeAPIReady = function () {
      (window.__ytApiWaiting || []).forEach(function (f) { f(); });
      window.__ytApiWaiting = [];
    };
    var s = document.createElement('script');
    s.src = 'https://www.youtube.com/iframe_api';
    s.async = true;
    s.onerror = function () {
      // 脚本都取不到（网络受限、被拦）：别留个转圈的空框
      window.__ytApiLoading = false;
      var w = document.querySelector('.vwrap');
      if (w) {
        w.className = 'vfail';
        w.innerHTML = '<span>加载 YouTube 播放器失败，用下面的音频，'
          + '或去 YouTube 打开原视频。</span>';
      }
    };
    document.head.appendChild(s);
  }

  function mountYouTube(seconds) {
    if (!facade && !ytPlayer) return;
    if (ytPlayer) {
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
    facade.replaceWith(wrap);
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
          onError: function () {
            // 放不了（区域限制、嵌入被关）：给条能点的外链，别停在黑框上
            var a = document.createElement('a');
            a.className = 'video-fallback';
            a.href = 'https://www.youtube.com/watch?v=' + vid;
            a.target = '_blank';
            a.rel = 'noopener';
            a.textContent = '这段视频不能内嵌播放，去 YouTube 打开 ↗';
            wrap.innerHTML = '';
            wrap.className = 'vfail';
            wrap.appendChild(a);
            ytPlayer = null;
          }
        }
      });
    });
  }

  function seekVideo(t) {
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
    var useAudio = audio && (prefer === 'audio' || (!facade && !ytPlayer));
    if (useAudio) {
      e.preventDefault();
      try {
        audio.currentTime = t;
        audio.play();
        audio.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      } catch (err) {}
      return;
    }
    if (facade || ytPlayer) {
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
        btn.setAttribute('aria-label', on ? '暂停' : '播放');
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
      // 只传 text，不传 url。同时传两个的话微信会当成两个条目：文本正常发出，
      // URL 另存成一个一百多字节的临时文件跟着发过去（实测截图里那个
      // "32058763b0241ae675c…" 就是它）。我们的文本末尾本来就带链接，
      // 少传一个字段反而干净。title 也不传：微信不用它，而某些客户端会
      // 把它当成另一个条目。
      navigator.share({ text: text })
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
        toast(wx ? '已复制，长按粘贴发给朋友；发朋友圈点右上角 ···'
                 : '已复制，粘到微信、朋友圈或任何地方');
      }).catch(function () {
        toast('复制没成功，长按选中下面的链接：' + url);
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
