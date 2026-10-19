/**
 * Overlay global de transição de páginas (engrenagem girando).
 * - window.showLoading() / window.hideLoading() para controle manual.
 * - Cliques nos links do menu principal acionam o overlay antes da página descarregar.
 * - Card de ações rápidas (#overlay-carregando-global): window.mostrarCarregamentoAcao(ms) /
 *   window.ocultarCarregamentoAcao(); disparado por cliques em [data-acao-rapida].
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

  var ACAO_OVERLAY_ID = 'overlay-carregando-global';
  var ACAO_SELECTOR = '[data-acao-rapida]';
  // Teto para a navegação dos atalhos: evita tela travada se o usuário cancelar o carregamento.
  var ACAO_NAVEGACAO_LIMITE_MS = 15000;
  var acaoTimer = null;

  function mostrarCarregamentoAcao(tempoLimiteMs) {
    if (tempoLimiteMs === undefined) tempoLimiteMs = 600;
    var overlay = document.getElementById(ACAO_OVERLAY_ID);
    if (!overlay) return;
    if (acaoTimer) {
      global.clearTimeout(acaoTimer);
      acaoTimer = null;
    }
    overlay.classList.remove('hidden');
    overlay.setAttribute('aria-hidden', 'false');
    if (tempoLimiteMs > 0) {
      acaoTimer = global.setTimeout(ocultarCarregamentoAcao, tempoLimiteMs);
    }
  }

  function ocultarCarregamentoAcao() {
    if (acaoTimer) {
      global.clearTimeout(acaoTimer);
      acaoTimer = null;
    }
    var overlay = document.getElementById(ACAO_OVERLAY_ID);
    if (!overlay) return;
    overlay.classList.add('hidden');
    overlay.setAttribute('aria-hidden', 'true');
  }

  function onAcaoRapidaClick(e) {
    if (e.defaultPrevented) return;
    var link = e.target.closest && e.target.closest(ACAO_SELECTOR);
    if (!link) return;
    if (link.tagName === 'A' && !linkDisparaLoading(link, e)) return;
    mostrarCarregamentoAcao(link.tagName === 'A' ? ACAO_NAVEGACAO_LIMITE_MS : undefined);
  }

  global.showLoading = showLoading;
  global.hideLoading = hideLoading;
  global.mostrarCarregamentoAcao = mostrarCarregamentoAcao;
  global.ocultarCarregamentoAcao = ocultarCarregamentoAcao;

  document.addEventListener('click', onNavClick, true);
  document.addEventListener('click', onAcaoRapidaClick, true);
  document.addEventListener('DOMContentLoaded', hideLoading);
  global.addEventListener('load', hideLoading);
  global.addEventListener('pageshow', function (e) {
    if (e.persisted) {
      hideLoading();
      ocultarCarregamentoAcao();
    }
  });
})(typeof window !== 'undefined' ? window : globalThis);
