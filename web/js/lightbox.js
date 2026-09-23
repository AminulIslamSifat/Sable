/* lightbox.js — in-app image viewer for Sable.
 * Replaces the scattered `window.open(url, "_blank")` calls.
 * Usage: openLightbox(url)  |  openLightbox([url1, url2, ...], startIndex)
 */
(function () {
  "use strict";

  var _urls = [];
  var _idx = 0;
  var _overlay = null;

  function _injectStyles() {
    if (document.getElementById("sable-lightbox-styles")) return;
    var s = document.createElement("style");
    s.id = "sable-lightbox-styles";
    s.textContent = [
      '.slb-overlay{position:fixed;inset:0;z-index:99999;display:flex;align-items:center;justify-content:center;',
      'background:rgba(8,8,14,.9);backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);',
      'opacity:0;transition:opacity .18s ease;}',
      '.slb-overlay.slb-open{opacity:1;}',

      /* image stage — sized to leave permanent room for the rails */
      '.slb-stage{display:flex;align-items:center;justify-content:center;',
      'max-width:calc(100vw - 200px);max-height:calc(100vh - 150px);}',
      '.slb-img{max-width:calc(100vw - 200px);max-height:calc(100vh - 150px);object-fit:contain;border-radius:10px;',
      'box-shadow:0 24px 70px rgba(0,0,0,.6);background:#111;user-select:none;',
      'transition:transform .18s ease;cursor:zoom-in;}',
      '.slb-img.slb-zoomed{cursor:zoom-out;transform:scale(1.5);}',

      /* full-height rails pinned to viewport edges — pointer-events:none so empty',
      '   rail space still falls through to the overlay (and closes it) */
      '.slb-rail{position:fixed;top:0;bottom:0;width:96px;display:flex;align-items:center;',
      'pointer-events:none;z-index:2;}',
      '.slb-rail-left{left:0;justify-content:flex-start;padding-left:22px;}',
      '.slb-rail-right{right:0;justify-content:flex-end;padding-right:22px;}',

      '.slb-btn{pointer-events:auto;display:flex;align-items:center;justify-content:center;',
      'width:52px;height:52px;border-radius:50%;border:1px solid rgba(255,255,255,.16);',
      'background:rgba(20,20,28,.82);color:#eaeaf2;font-size:24px;line-height:1;cursor:pointer;',
      'transition:background .15s ease,border-color .15s ease;backdrop-filter:blur(6px);',
      '-webkit-user-select:none;user-select:none;}',
      '.slb-btn:hover{background:var(--accent,#5a5c8c);border-color:transparent;}',
      '.slb-btn:active{background:var(--accent,#5a5c8c);}',

      /* close pinned to the viewport corner, never over the image */
      '.slb-close{position:fixed;top:20px;right:20px;z-index:3;font-size:20px;}',

      '.slb-bar{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);z-index:2;',
      'display:flex;align-items:center;gap:14px;padding:8px 18px;border-radius:999px;',
      'background:rgba(20,20,28,.82);border:1px solid rgba(255,255,255,.12);',
      'color:var(--muted,#a0a0b8);font-size:12px;font-family:inherit;white-space:nowrap;',
      'backdrop-filter:blur(6px);}',
      '.slb-dl{color:var(--accent-text,#c8c8ff);text-decoration:none;cursor:pointer;}',
      '.slb-dl:hover{text-decoration:underline;}',

      '@media (max-width:720px){',
      '.slb-stage,.slb-img{max-width:calc(100vw - 132px);max-height:calc(100vh - 140px);}',
      '.slb-rail{width:64px;}',
      '.slb-rail-left{padding-left:8px;}.slb-rail-right{padding-right:8px;}',
      '.slb-btn{width:44px;height:44px;font-size:20px;}',
      '.slb-close{top:12px;right:12px;width:40px;height:40px;}',
      '.slb-bar{bottom:12px;}}'
    ].join('');
    document.head.appendChild(s);
  }

  function _render() {
    if (!_overlay) return;
    var img = _overlay.querySelector('.slb-img');
    var counter = _overlay.querySelector('.slb-counter');
    var dl = _overlay.querySelector('.slb-dl');
    var rails = _overlay.querySelectorAll('.slb-rail');
    var multi = _urls.length > 1;

    img.classList.remove('slb-zoomed');
    img.src = _urls[_idx] || '';
    if (counter) counter.textContent = multi ? (_idx + 1) + ' / ' + _urls.length : '';
    if (dl) dl.href = _urls[_idx] || '#';
    for (var i = 0; i < rails.length; i++) rails[i].style.display = multi ? '' : 'none';
  }

  function _step(d) {
    if (_urls.length < 2) return;
    _idx = (_idx + d + _urls.length) % _urls.length;
    _render();
  }

  function _isControl(t) {
    return !!(t && t.closest && t.closest('.slb-btn, .slb-bar, .slb-img'));
  }

  function _onKey(e) {
    if (!_overlay) return;
    if (e.key === 'Escape') closeLightbox();
    else if (e.key === 'ArrowLeft') { e.preventDefault(); _step(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); _step(1); }
  }

  function openLightbox(urls, startIndex) {
    _injectStyles();
    _urls = Array.isArray(urls) ? urls.filter(Boolean) : [urls].filter(Boolean);
    if (!_urls.length) return;
    _idx = Math.min(Math.max(startIndex || 0, 0), _urls.length - 1);

    if (_overlay) { _render(); return; }

    _overlay = document.createElement('div');
    _overlay.className = 'slb-overlay';
    _overlay.innerHTML =
      '<div class="slb-stage"><img class="slb-img" alt="Image preview"></div>' +
      '<div class="slb-rail slb-rail-left"><button class="slb-btn slb-prev" title="Previous (\u2190)">\u2039</button></div>' +
      '<div class="slb-rail slb-rail-right"><button class="slb-btn slb-next" title="Next (\u2192)">\u203a</button></div>' +
      '<button class="slb-btn slb-close" title="Close (Esc)">\u2715</button>' +
      '<div class="slb-bar"><span class="slb-counter"></span>' +
        '<a class="slb-dl" download title="Download">\u2193 download</a></div>';

    document.body.appendChild(_overlay);
    requestAnimationFrame(function () { _overlay.classList.add('slb-open'); });

    /* close only on a genuine backdrop click — controls are exempt */
    _overlay.addEventListener('click', function (e) {
      if (_isControl(e.target)) return;
      closeLightbox();
    });
    _overlay.addEventListener('mousedown', function (e) {
      e.stopPropagation();
    });

    _overlay.querySelector('.slb-close').addEventListener('click', function (e) {
      e.stopPropagation(); closeLightbox();
    });
    _overlay.querySelector('.slb-prev').addEventListener('click', function (e) {
      e.stopPropagation(); _step(-1);
    });
    _overlay.querySelector('.slb-next').addEventListener('click', function (e) {
      e.stopPropagation(); _step(1);
    });
    _overlay.querySelector('.slb-img').addEventListener('click', function (e) {
      e.stopPropagation(); this.classList.toggle('slb-zoomed');
    });

    document.addEventListener('keydown', _onKey);
    _render();
  }

  function closeLightbox() {
    if (!_overlay) return;
    document.removeEventListener('keydown', _onKey);
    var el = _overlay;
    _overlay = null;
    el.classList.remove('slb-open');
    setTimeout(function () { el.remove(); }, 180);
  }

  window.openLightbox = openLightbox;
  window.closeLightbox = closeLightbox;
})();
