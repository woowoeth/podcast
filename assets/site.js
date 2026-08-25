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
      c._slug = decodeURIComponent((c.getAttribute('href') || '').replace(/.*\/p\/|\/$/g, ''));
    });

    var deep = null, deepState = 'idle';
    var base = (document.querySelector('link[rel="alternate"]') || {}).href || '';
    var indexUrl = base ? base.replace(/feed\.xml$/, 'search.json') : 'search.json';

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

  /* ------------------------------------------------ timestamps -> the audio */
  var audio = document.querySelector('audio[data-player]');
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[data-t]');
    if (!a) return;
    var t = parseInt(a.getAttribute('data-t'), 10);
    if (isNaN(t)) return;
    // With an inline player, a timestamp seeks in place. Without one it falls
    // through to the href, which points at the original video or page.
    if (audio) {
      e.preventDefault();
      try {
        audio.currentTime = t;
        audio.play();
        audio.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      } catch (err) {}
    }
  });

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
      navigator.share({ title: title, text: text, url: url })
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
