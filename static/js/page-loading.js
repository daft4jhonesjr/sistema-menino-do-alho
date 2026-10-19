/**
 * Overlay global de transição de páginas (engrenagem girando).
 * - window.showLoading() / window.hideLoading() para controle manual.
 * - Cliques nos links do menu principal acionam o overlay antes da página descarregar.
 * - Carregado no <head> para registrar o listener de captura antes do navigation.js,
 *   que chama preventDefault() nos links internos para animar a View Transition.
 */
(function (global) {
  'use strict';

  var OVERLAY_ID = 'page-loading-overlay';
  var NAV_SELECTOR = '#site-nav, #site-nav-links';

  function getOverlay() {
    return document.getElementById(OVERLAY_ID);
  }

  function showLoading() {
    var overlay = getOverlay();
    if (!overlay) return;
    overlay.classList.add('is-visible');
    overlay.setAttribute('aria-hidden', 'false');
    document.documentElement.setAttribute('aria-busy', 'true');
  }

  function hideLoading() {
    var overlay = getOverlay();
    if (!overlay) return;
    overlay.classList.remove('is-visible');
    overlay.setAttribute('aria-hidden', 'true');
    document.documentElement.removeAttribute('aria-busy');
  }

  function linkDisparaLoading(link, e) {
    if (!link || !link.href) return false;
    if (e.button !== 0) return false;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return false;
    if (link.target && link.target !== '_self') return false;
    if (link.hasAttribute('download')) return false;
    if (link.hasAttribute('data-no-loading')) return false;

    var hrefAttr = link.getAttribute('href') || '';
    if (!hrefAttr || hrefAttr.charAt(0) === '#') return false;
    if (/^(mailto:|tel:|javascript:)/i.test(hrefAttr)) return false;

    var url;
    try {
      url = new URL(link.href, global.location.href);
    } catch (err) {
      return false;
    }

    if (url.origin !== global.location.origin) return false;
    if (
      url.pathname === global.location.pathname &&
      url.search === global.location.search &&
      url.hash
    ) {
      return false;
    }

    return true;
  }

  function onNavClick(e) {
    if (e.defaultPrevented) return;
    var link = e.target.closest && e.target.closest('a');
    if (!link || !link.closest(NAV_SELECTOR)) return;
    if (!linkDisparaLoading(link, e)) return;
    showLoading();
  }

  global.showLoading = showLoading;
  global.hideLoading = hideLoading;

  document.addEventListener('click', onNavClick, true);
  document.addEventListener('DOMContentLoaded', hideLoading);
  global.addEventListener('load', hideLoading);
  global.addEventListener('pageshow', function (e) {
    if (e.persisted) hideLoading();
  });
})(typeof window !== 'undefined' ? window : globalThis);
