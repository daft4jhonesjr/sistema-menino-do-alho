/**
 * Interceptação suave de navegação interna (View Transitions / estilo iOS).
 * Em iOS 18+ / Safari com document.startViewTransition, anima a troca de tela.
 * Em navegadores sem suporte, o link segue o fluxo normal (fallback CSS no <main>).
 */
(function () {
  'use strict';

  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return;
  }

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented) return;
    if (e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;

    var link = e.target.closest && e.target.closest('a');
    if (!link || !link.href) return;
    if (link.target === '_blank') return;
    if (link.hasAttribute('download')) return;
    if (link.getAttribute('onclick')) return;

    var hrefAttr = link.getAttribute('href') || '';
    if (!hrefAttr || hrefAttr === '#' || hrefAttr.charAt(0) === '#') return;
    if (/^(mailto:|tel:|javascript:)/i.test(hrefAttr)) return;

    var url;
    try {
      url = new URL(link.href, window.location.origin);
    } catch (err) {
      return;
    }

    if (url.origin !== window.location.origin) return;
    if (url.href === window.location.href) return;

    // Âncoras na mesma página (hash-only) — deixa o browser rolar normalmente
    if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) {
      return;
    }

    if (!document.startViewTransition) return;

    e.preventDefault();
    document.startViewTransition(function () {
      window.location.href = link.href;
    });
  }, true);
})();
