/**
 * Navegação interna com View Transitions API (MPA Flask) + fallback CSS estilo iOS.
 * - Links same-origin: interceptados com capture para animar saída antes do reload.
 * - document.startViewTransition quando disponível (Safari 18+ / Chrome).
 * - Fallback: classe .page-nav-exit no <main> e atraso de 300ms.
 */
(function (global) {
  'use strict';

  var FALLBACK_MS = 300;

  var reducedMotion =
    global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function getTransitionTarget() {
    return document.querySelector('main') || document.body;
  }

  /**
   * @param {string} url — URL absoluta ou relativa same-origin
   * @param {{ skipViewTransition?: boolean }} [options]
   */
  function navigate(url, options) {
    if (!url) return;
    options = options || {};

    if (reducedMotion || options.skipViewTransition) {
      global.location.href = url;
      return;
    }

    var go = function () {
      global.location.href = url;
    };

    if (document.startViewTransition && !options.forceFallback) {
      try {
        document.startViewTransition(go);
        return;
      } catch (err) {
        /* continua no fallback */
      }
    }

    var target = getTransitionTarget();
    target.classList.add('page-nav-exit');
    if (!document.querySelector('main')) {
      document.documentElement.classList.add('page-nav-exit-root');
    }
    global.setTimeout(go, FALLBACK_MS);
  }

  function linkShouldNavigateInternally(link, e) {
    if (!link || !link.href) return null;
    if (e && e.defaultPrevented) return null;
    if (e && e.button !== 0) return null;
    if (e && (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)) return null;
    if (link.target === '_blank') return null;
    if (link.hasAttribute('download')) return null;
    if (link.getAttribute('onclick')) return null;

    var hrefAttr = link.getAttribute('href') || '';
    if (!hrefAttr || hrefAttr === '#' || hrefAttr.charAt(0) === '#') return null;
    if (/^(mailto:|tel:|javascript:)/i.test(hrefAttr)) return null;

    var url;
    try {
      url = new URL(link.href, global.location.origin);
    } catch (err) {
      return null;
    }

    if (url.origin !== global.location.origin) return null;
    if (url.href === global.location.href) return null;

    if (
      url.pathname === global.location.pathname &&
      url.search === global.location.search &&
      url.hash
    ) {
      return null;
    }

    return url.href;
  }

  function onDocumentClick(e) {
    if (reducedMotion) return;

    var link = e.target.closest && e.target.closest('a');
    var destino = linkShouldNavigateInternally(link, e);
    if (!destino) return;

    e.preventDefault();
    navigate(destino);
  }

  /**
   * Fecha drawer/menu lateral ao deslizar da esquerda para a direita (iOS / Capacitor).
   * @param {{ panel: Element, backdrop?: Element|null, isOpen: function(): boolean, onClose: function() }} options
   */
  function setupDrawerSwipeToClose(options) {
    if (!options || !options.panel || typeof options.onClose !== 'function') return;

    var panel = options.panel;
    var backdrop = options.backdrop || null;
    var isOpen = options.isOpen || function () { return true; };
    var onClose = options.onClose;
    var minDeltaX = typeof options.minDeltaX === 'number' ? options.minDeltaX : 50;

    function alvoIgnoraSwipe(target) {
      if (!target || !target.closest) return false;
      return !!target.closest(
        'input, textarea, select, button, [contenteditable="true"], label[for]'
      );
    }

    function registrarSwipeFechar(el) {
      if (!el) return;

      var startX = 0;
      var startY = 0;
      var rastreando = false;

      el.addEventListener(
        'touchstart',
        function (e) {
          if (!isOpen()) return;
          if (alvoIgnoraSwipe(e.target)) return;
          if (!e.touches || !e.touches.length) return;
          var touch = e.touches[0];
          startX = touch.screenX;
          startY = touch.screenY;
          rastreando = true;
        },
        { passive: true }
      );

      el.addEventListener(
        'touchend',
        function (e) {
          if (!rastreando) return;
          rastreando = false;
          if (!isOpen()) return;
          if (!e.changedTouches || !e.changedTouches.length) return;
          var touch = e.changedTouches[0];
          var diffX = touch.screenX - startX;
          var diffY = touch.screenY - startY;
          if (Math.abs(diffX) > Math.abs(diffY) && diffX > minDeltaX) {
            onClose();
          }
        },
        { passive: true }
      );

      el.addEventListener(
        'touchcancel',
        function () {
          rastreando = false;
        },
        { passive: true }
      );
    }

    registrarSwipeFechar(panel);
    registrarSwipeFechar(backdrop);
  }

  global.MeninoAlhoNavigation = {
    navigate: navigate,
    FALLBACK_MS: FALLBACK_MS,
    setupDrawerSwipeToClose: setupDrawerSwipeToClose
  };

  if (!reducedMotion) {
    document.addEventListener('click', onDocumentClick, true);
  }
})(typeof window !== 'undefined' ? window : globalThis);
