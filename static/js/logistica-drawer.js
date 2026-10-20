/**
 * Gaveta lateral direita (estilo iOS) com dados de logística via fetch.
 */
(function (global) {
  'use strict';

  var config = global.LOGISTICA_DRAWER_CONFIG || {};
  if (!config.apiUrl) return;

  var panel = document.getElementById('logistica-drawer-panel');
  var overlay = document.getElementById('logistica-drawer-overlay');
  var content = document.getElementById('drawerContent');
  if (!panel || !overlay || !content) return;

  var ANIM_MS = 300;
  var cache = null;
  var fetchPromise = null;
  var aberta = false;

  function formatarMoeda(valor) {
    var n = Number(valor);
    if (!isFinite(n)) n = 0;
    return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(n);
  }

  function escHtml(str) {
    return String(str == null ? '' : str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function isOpen() {
    return aberta;
  }

  global.isLogisticaDrawerOpen = isOpen;

  function mostrarCarregando() {
    content.innerHTML =
      '<div class="logistica-drawer-loading flex flex-col items-center justify-center gap-3 py-16 text-slate-500 dark:text-slate-400">' +
      '<svg class="animate-spin h-8 w-8 text-emerald-600" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" aria-hidden="true">' +
      '<circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4"></circle>' +
      '<path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>' +
      '</svg>' +
      '<p class="text-sm font-medium">Carregando entregas…</p>' +
      '</div>';
  }

  function renderErro(msg) {
    content.innerHTML =
      '<div class="p-4 text-center text-sm text-red-600 dark:text-red-400">' +
      escHtml(msg || 'Não foi possível carregar os dados.') +
      '<button type="button" id="logistica-drawer-retry" class="mt-3 block w-full py-2 rounded-lg border border-red-200 dark:border-red-800 text-red-700 dark:text-red-300">Tentar novamente</button>' +
      '</div>';
    var btn = document.getElementById('logistica-drawer-retry');
    if (btn) {
      btn.addEventListener('click', function () {
        carregarDados(true);
      });
    }
  }

  function cardEntrega(entrega, concluida) {
    var produtos = (entrega.produtos || []).slice(0, 3);
    var restantes = (entrega.produtos || []).length - produtos.length;
    var produtosHtml = produtos
      .map(function (p) {
        return '<li class="truncate">' + escHtml(p) + '</li>';
      })
      .join('');
    if (restantes > 0) {
      produtosHtml += '<li class="text-slate-400">+' + restantes + ' item(ns)</li>';
    }
    var badge = concluida
      ? '<span class="text-[10px] font-semibold uppercase tracking-wide text-emerald-600 dark:text-emerald-400">Entregue</span>'
      : '<span class="text-[10px] font-semibold uppercase tracking-wide text-amber-600 dark:text-amber-400">Pendente</span>';

    return (
      '<article class="rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50/80 dark:bg-slate-800/50 p-3 space-y-1.5">' +
      '<div class="flex items-start justify-between gap-2">' +
      '<h4 class="text-sm font-semibold text-slate-800 dark:text-slate-100 leading-snug">' +
      escHtml(entrega.cliente_nome || 'Cliente') +
      '</h4>' +
      badge +
      '</div>' +
      '<p class="text-xs text-slate-500 dark:text-slate-400">' +
      escHtml(entrega.data || '') +
      '</p>' +
      (entrega.endereco
        ? '<p class="text-xs text-slate-600 dark:text-slate-300 line-clamp-2">' + escHtml(entrega.endereco) + '</p>'
        : '') +
      '<ul class="text-xs text-slate-600 dark:text-slate-300 space-y-0.5">' +
      produtosHtml +
      '</ul>' +
      '<div class="flex justify-between text-xs pt-1 border-t border-slate-200/80 dark:border-slate-600/80">' +
      '<span class="text-slate-500">' +
      escHtml(String(entrega.quantidade || 0)) +
      ' cx</span>' +
      '<span class="font-semibold text-slate-800 dark:text-slate-100 valor-financeiro">' +
      formatarMoeda(entrega.total) +
      '</span>' +
      '</div>' +
      '</article>'
    );
  }

  function renderLista(titulo, entregas, vazioMsg) {
    if (!entregas || !entregas.length) {
      return (
        '<section class="space-y-2">' +
        '<h3 class="text-xs font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400">' +
        escHtml(titulo) +
        '</h3>' +
        '<p class="text-sm text-slate-400 dark:text-slate-500 py-2">' +
        escHtml(vazioMsg) +
        '</p></section>'
      );
    }
    var cards = entregas
      .map(function (e) {
        return cardEntrega(e, titulo.indexOf('conclu') !== -1);
      })
      .join('');
    return (
      '<section class="space-y-2">' +
      '<h3 class="text-xs font-bold uppercase tracking-wide text-slate-500 dark:text-slate-400">' +
      escHtml(titulo) +
      '</h3>' +
      '<div class="space-y-2">' +
      cards +
      '</div></section>'
    );
  }

  function renderDados(dados) {
    var resumo =
      '<div class="grid grid-cols-2 gap-2 mb-4">' +
      '<div class="rounded-lg border border-amber-200 dark:border-amber-800/50 bg-amber-50/60 dark:bg-amber-900/20 p-3">' +
      '<p class="text-[10px] uppercase text-amber-700 dark:text-amber-300">Pendentes</p>' +
      '<p class="text-lg font-bold text-amber-800 dark:text-amber-200">' +
      escHtml(String(dados.total_pedidos_pendentes || 0)) +
      '</p>' +
      '<p class="text-[10px] text-amber-600/80 dark:text-amber-400/80">' +
      escHtml(String(dados.total_caixas_pendentes || 0)) +
      ' caixas</p></div>' +
      '<div class="rounded-lg border border-emerald-200 dark:border-emerald-800/50 bg-emerald-50/60 dark:bg-emerald-900/20 p-3">' +
      '<p class="text-[10px] uppercase text-emerald-700 dark:text-emerald-300">Entregues</p>' +
      '<p class="text-lg font-bold text-emerald-800 dark:text-emerald-200">' +
      escHtml(String(dados.total_pedidos_entregues || 0)) +
      '</p></div></div>';

    var pendentes = renderLista(
      'Entregas pendentes',
      dados.entregas_pendentes,
      'Nenhuma entrega pendente.'
    );
    var diasRecentes = Number(dados.entregues_recentes_dias) || 7;
    var entregues = renderLista(
      'Entregas concluídas (recentes)',
      dados.entregas_entregues,
      'Nenhuma entrega concluída nos últimos ' + diasRecentes + ' dias.'
    );

    var linkCompleto = config.paginaUrl
      ? '<a href="' +
        escHtml(config.paginaUrl) +
        '" class="mt-4 block w-full text-center py-2.5 rounded-xl bg-emerald-600 hover:bg-emerald-700 text-white text-sm font-semibold transition-colors">Abrir página completa de logística</a>'
      : '';

    content.innerHTML =
      '<div class="p-4 pb-6 space-y-4">' +
      resumo +
      pendentes +
      entregues +
      linkCompleto +
      '</div>';

    if (typeof lucide !== 'undefined' && lucide.createIcons) {
      try {
        lucide.createIcons();
      } catch (err) {}
    }
  }

  function carregarDados(forcar) {
    if (!forcar && cache) {
      renderDados(cache);
      return Promise.resolve(cache);
    }
    if (!forcar && fetchPromise) {
      return fetchPromise;
    }

    mostrarCarregando();
    var url = config.apiUrl;
    if (forcar && url.indexOf('?') === -1) {
      url += '?refresh=1';
    }

    fetchPromise = fetch(url, {
      credentials: 'same-origin',
      headers: { Accept: 'application/json', 'X-Requested-With': 'XMLHttpRequest' }
    })
      .then(function (res) {
        if (!res.ok) throw new Error('HTTP ' + res.status);
        return res.json();
      })
      .then(function (json) {
        if (!json || !json.success) {
          throw new Error((json && json.erro) || 'Resposta inválida');
        }
        cache = json;
        renderDados(json);
        return json;
      })
      .catch(function (err) {
        renderErro(err && err.message ? err.message : 'Erro de rede');
        throw err;
      })
      .finally(function () {
        fetchPromise = null;
      });

    return fetchPromise;
  }

  function prefetchDados() {
    if (cache || fetchPromise) return;
    carregarDados(false).catch(function () {});
  }

  function abrirDrawer() {
    if (aberta) return;
    aberta = true;
    document.body.classList.add('logistica-drawer-open');
    if (typeof travarFundo === 'function') travarFundo();

    panel.style.transform = '';
    panel.style.transition = '';

    overlay.classList.remove('hidden');
    overlay.classList.add('is-visible');
    overlay.setAttribute('aria-hidden', 'false');

    panel.classList.remove('hidden');
    panel.classList.add('logistica-drawer-panel--open');
    panel.setAttribute('aria-hidden', 'false');
    panel.offsetHeight;
    panel.classList.remove('translate-x-full');
    panel.classList.add('translate-x-0');

    carregarDados(false);
  }

  function fecharDrawer() {
    if (!aberta) return;
    aberta = false;
    document.body.classList.remove('logistica-drawer-open');

    panel.classList.remove('translate-x-0');
    panel.classList.add('translate-x-full');
    panel.setAttribute('aria-hidden', 'true');

    overlay.classList.remove('is-visible');
    overlay.setAttribute('aria-hidden', 'true');

    global.setTimeout(function () {
      if (!aberta) {
        panel.classList.add('hidden');
        panel.classList.remove('logistica-drawer-panel--open');
        overlay.classList.add('hidden');
        if (typeof liberarFundo === 'function') liberarFundo();
      }
    }, ANIM_MS);
  }

  global.abrirGavetaLogistica = abrirDrawer;
  global.fecharGavetaLogistica = fecharDrawer;

  var btnFechar = document.getElementById('logistica-drawer-fechar');
  if (btnFechar) {
    btnFechar.addEventListener('click', fecharDrawer);
  }

  var btnRefresh = document.getElementById('logistica-drawer-refresh');
  if (btnRefresh) {
    btnRefresh.addEventListener('click', function () {
      cache = null;
      carregarDados(true);
    });
  }

  overlay.addEventListener('click', fecharDrawer);

  panel.addEventListener('transitionend', function (e) {
    if (e.propertyName !== 'transform') return;
    if (aberta) return;
    if (panel.classList.contains('translate-x-0') || panel.classList.contains('logistica-drawer-panel--open')) {
      return;
    }
    panel.classList.add('hidden');
    overlay.classList.add('hidden');
    overlay.classList.remove('is-visible', 'is-drag-preview');
  });

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && aberta) fecharDrawer();
  });

  if (!config.paginaAtual) {
    var pathLogistica =
      (config.paginaUrl || '/logistica').replace(/^https?:\/\/[^/]+/, '').replace(/\/$/, '') ||
      '/logistica';
    document.querySelectorAll('a[href*="logistica"]').forEach(function (link) {
      link.addEventListener('click', function (e) {
        try {
          var u = new URL(link.href, global.location.href);
          if (u.pathname.replace(/\/$/, '') !== pathLogistica) return;
          e.preventDefault();
          abrirDrawer();
        } catch (err) {}
      });
    });
  }

  if (global.MeninoAlhoSwipeDismiss && typeof global.MeninoAlhoSwipeDismiss.attach === 'function') {
    if (panel.__swipeDismiss && typeof panel.__swipeDismiss.destroy === 'function') {
      panel.__swipeDismiss.destroy();
    }
    global.MeninoAlhoSwipeDismiss.attach(panel, {
      backdrop: overlay,
      onClose: fecharDrawer,
      isOpen: isOpen,
      edgeWidth: 48,
      minClosePx: 60
    });
  }

  if (global.MeninoAlhoNavigation && global.MeninoAlhoNavigation.setupDrawerInteractiveDrag) {
    global.MeninoAlhoNavigation.setupDrawerInteractiveDrag({
      panel: panel,
      backdrop: overlay,
      drawerPosition: 'right',
      edgeWidth: 45,
      openThreshold: 0.3,
      closeGesture: false,
      draggingBodyClass: 'logistica-drawer-dragging',
      isOpen: isOpen,
      onOpen: abrirDrawer,
      onClose: fecharDrawer,
      onOpenGestureStart: prefetchDados,
      canStartOpenGesture: function () {
        if (config.paginaAtual) return false;
        if (document.body.classList.contains('modal-aberto')) return false;
        if (document.body.classList.contains('site-nav-mobile-open')) return false;
        if (isOpen()) return false;
        return true;
      },
      shouldIgnoreTouchTarget: function (target) {
        if (!target || !target.closest) return false;
        if (target.closest('#logistica-drawer-panel, #logistica-drawer-overlay')) return false;
        return !!target.closest(
          '[data-swipe-dismiss], .fixed.z-50, .fixed.z-\\[9998\\], .fixed.z-\\[9999\\], [role="dialog"], .modal-container, .bg-black\\/50, .bg-black\\/60, #site-nav-mobile-backdrop, #site-nav-links, #gaveta-contagem, #gaveta-orcamento, #gaveta-lancamento, #gaveta-balanco, #modal-nova-entrada-wrap, #modal-nova-entrada-overlay, #modal-editar-produto-wrap, #modal-editar-produto-overlay, #modal-devolver-produto-wrap, #container-nova-venda, #modal-novo-cliente-wrap, #modal-novo-cliente-panel'
        );
      }
    });
  }
})(typeof window !== 'undefined' ? window : globalThis);
