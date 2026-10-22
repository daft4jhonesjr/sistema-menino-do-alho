/**
 * Fechamento seguro de gavetas laterais (drawers) via backdrop.
 *
 * Corrige:
 *  - delay perceptível ao clicar fora (usa pointerdown em vez de esperar o click)
 *  - ghost click no mobile (touch → click sintético reabre / dispara animação 2x)
 *  - bubbling de toques de dentro do painel para o overlay
 *
 * API global: window.MeninoAlhoDrawerSafe
 */
(function (global) {
  'use strict';

  var DURATION_MS = 300;
  var CLICK_SUPPRESS_MS = 450;
  var DEBOUNCE_MS = 320;

  function now() {
    return global.performance && performance.now ? performance.now() : Date.now();
  }

  function resolveEl(ref) {
    if (!ref) return null;
    if (typeof ref === 'string') return document.querySelector(ref);
    return ref;
  }

  /**
   * Anima a saída da gaveta e do backdrop com trava anti-reentrada.
   * Quando o fechamento veio do swipe (`dataset.swipeClosed`), pula a 2ª animação
   * (o painel já está em translateX(100%)) para evitar delay + “piscar” no WebKit.
   * @returns {boolean} true se o fechamento foi iniciado
   */
  function fecharGavetaComSeguranca(drawerEl, backdropEl, options) {
    options = options || {};
    drawerEl = resolveEl(drawerEl);
    backdropEl = resolveEl(backdropEl);
    if (!drawerEl) return false;

    var fromSwipe = drawerEl.dataset.swipeClosed === 'true' || options.skipAnimation === true;

    // Durante o arrasto/animação de swipe, ignora fechamentos concorrentes (ghost click)
    if (
      !fromSwipe &&
      drawerEl.dataset.isSwiping === 'true'
    ) {
      return false;
    }

    if (drawerEl.dataset.closing === 'true' || drawerEl.dataset.isClosing === 'true') {
      return false;
    }

    drawerEl.dataset.closing = 'true';
    drawerEl.dataset.isClosing = 'true';

    // Anima saída primeiro — NÃO aplicar `hidden` no mesmo frame
    // (no swipe as classes já foram sincronizadas; reaplicar é idempotente)
    drawerEl.classList.remove('translate-x-0');
    drawerEl.classList.add('translate-x-full');
    drawerEl.setAttribute('aria-hidden', 'true');

    if (backdropEl) {
      backdropEl.classList.add('opacity-0');
      backdropEl.classList.remove('opacity-100', 'is-visible');
      backdropEl.setAttribute('aria-hidden', 'true');
      // Mantém o overlay interceptando o ghost click até o fim da transição
      backdropEl.style.pointerEvents = 'auto';
    }

    var duration = fromSwipe
      ? 0
      : typeof options.duration === 'number'
        ? options.duration
        : DURATION_MS;
    var onDone = typeof options.onDone === 'function' ? options.onDone : null;
    var hideDrawer = options.hideDrawer !== false;
    var hideBackdrop = options.hideBackdrop !== false;

    function finalize() {
      if (hideDrawer) drawerEl.classList.add('hidden');
      if (backdropEl) {
        if (hideBackdrop) backdropEl.classList.add('hidden');
        // Mantém pointer-events até liberar a trava (anti ghost click)
      }
      if (onDone) {
        try {
          onDone();
        } catch (err) {
          if (global.console) console.error('[drawer-safe-close] onDone', err);
        }
      }
      // Libera a trava só após a janela de click sintético do mobile
      var unlockDelay = Math.max(0, CLICK_SUPPRESS_MS - duration);
      global.setTimeout(function () {
        if (backdropEl) backdropEl.style.pointerEvents = '';
        delete drawerEl.dataset.closing;
        delete drawerEl.dataset.isClosing;
      }, unlockDelay);
    }

    if (duration <= 0) {
      // Já animado pelo swipe: esconde no próximo frame (sem 2ª transição)
      global.requestAnimationFrame(finalize);
    } else {
      global.setTimeout(finalize, duration);
    }

    return true;
  }

  /**
   * Envolve uma função de fechamento com trava por elemento (dataset.closing).
   */
  function withClosingGuard(drawerRef, closeFn) {
    if (typeof closeFn !== 'function') return closeFn;
    return function () {
      var drawerEl = resolveEl(
        typeof drawerRef === 'function' ? drawerRef() : drawerRef
      );
      if (
        drawerEl &&
        (drawerEl.dataset.closing === 'true' || drawerEl.dataset.isClosing === 'true')
      ) {
        return;
      }
      return closeFn.apply(this, arguments);
    };
  }

  /**
   * Marca início/fim de fechamento em closeFn legado (quando a animação é própria).
   */
  function beginClosing(drawerEl) {
    drawerEl = resolveEl(drawerEl);
    if (!drawerEl) return false;
    if (drawerEl.dataset.closing === 'true' || drawerEl.dataset.isClosing === 'true') {
      return false;
    }
    drawerEl.dataset.closing = 'true';
    drawerEl.dataset.isClosing = 'true';
    return true;
  }

  function endClosing(drawerEl) {
    drawerEl = resolveEl(drawerEl);
    if (drawerEl) {
      delete drawerEl.dataset.closing;
      delete drawerEl.dataset.isClosing;
    }
  }

  function isClosing(drawerEl) {
    drawerEl = resolveEl(drawerEl);
    return !!(
      drawerEl &&
      (drawerEl.dataset.closing === 'true' || drawerEl.dataset.isClosing === 'true')
    );
  }

  /**
   * Liga o backdrop ao fechamento imediato (pointerdown) e suprime o click fantasma.
   */
  function bindBackdropClose(backdropEl, closeFn, options) {
    backdropEl = resolveEl(backdropEl);
    if (!backdropEl || typeof closeFn !== 'function') return null;
    if (backdropEl.__drawerSafeCloseBound) return backdropEl.__drawerSafeCloseBound;

    options = options || {};
    var onlyDirectTarget = options.onlyDirectTarget !== false;
    var lastTriggerAt = 0;
    var suppressClickUntil = 0;
    var usedPointer = false;

    function shouldHandle(e) {
      if (!e) return true;
      if (onlyDirectTarget && e.target !== backdropEl) return false;
      if (typeof e.button === 'number' && e.button !== 0) return false;
      return true;
    }

    function findSwipePanel() {
      var panels = document.querySelectorAll('[data-swipe-dismiss]');
      for (var i = 0; i < panels.length; i++) {
        var sel = panels[i].getAttribute('data-swipe-backdrop');
        if (!sel) continue;
        var matched = document.querySelector(sel);
        if (matched === backdropEl) return panels[i];
      }
      return null;
    }

    function trigger(e) {
      var panel = findSwipePanel();
      // Ignora ghost click / pointer durante swipe em andamento
      if (panel && panel.dataset.isSwiping === 'true' && panel.dataset.swipeClosed !== 'true') {
        if (e) {
          if (e.cancelable) e.preventDefault();
          e.stopPropagation();
          if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        }
        return;
      }
      var t = now();
      if (t - lastTriggerAt < DEBOUNCE_MS) {
        if (e) {
          if (e.cancelable) e.preventDefault();
          e.stopPropagation();
          if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        }
        return;
      }
      lastTriggerAt = t;
      suppressClickUntil = t + CLICK_SUPPRESS_MS;
      if (e) {
        if (e.cancelable) e.preventDefault();
        e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      }
      try {
        closeFn(e);
      } catch (err) {
        if (global.console) console.error('[drawer-safe-close] closeFn', err);
      }
    }

    function onPointerDown(e) {
      usedPointer = true;
      if (!shouldHandle(e)) return;
      trigger(e);
    }

    function onTouchStart(e) {
      if (usedPointer) return;
      if (!shouldHandle(e)) return;
      trigger(e);
    }

    function onClick(e) {
      var t = now();
      if (t < suppressClickUntil) {
        if (e.cancelable) e.preventDefault();
        e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        return;
      }
      if (!shouldHandle(e)) return;
      trigger(e);
    }

    backdropEl.addEventListener('pointerdown', onPointerDown, { passive: false, capture: true });
    backdropEl.addEventListener('touchstart', onTouchStart, { passive: false, capture: true });
    backdropEl.addEventListener('click', onClick, true);

    // Garante classes de transição no backdrop (idempotente)
    if (!backdropEl.classList.contains('transition-opacity')) {
      backdropEl.classList.add('transition-opacity');
    }
    if (!/\bduration-\d+\b/.test(backdropEl.className)) {
      backdropEl.classList.add('duration-300');
    }
    if (
      !backdropEl.classList.contains('ease-out') &&
      !backdropEl.classList.contains('ease-in-out') &&
      !backdropEl.classList.contains('ease-in')
    ) {
      backdropEl.classList.add('ease-out');
    }

    var controller = {
      trigger: function () {
        trigger(null);
      },
      destroy: function () {
        backdropEl.removeEventListener('pointerdown', onPointerDown, true);
        backdropEl.removeEventListener('touchstart', onTouchStart, true);
        backdropEl.removeEventListener('click', onClick, true);
        delete backdropEl.__drawerSafeCloseBound;
      }
    };
    backdropEl.__drawerSafeCloseBound = controller;
    return controller;
  }

  /**
   * Liga botões de fechar (X / Cancelar) com pointerdown imediato e
   * supressão do click fantasma (touchend → click) no mobile.
   */
  function bindCloseControl(controlEl, closeFn, options) {
    controlEl = resolveEl(controlEl);
    if (!controlEl || typeof closeFn !== 'function') return null;
    if (controlEl.__drawerSafeCloseBound) return controlEl.__drawerSafeCloseBound;

    options = options || {};
    var lastTriggerAt = 0;
    var suppressClickUntil = 0;
    var usedPointer = false;

    function findSwipePanelFromControl() {
      return controlEl.closest ? controlEl.closest('[data-swipe-dismiss]') : null;
    }

    function trigger(e) {
      var panel = findSwipePanelFromControl();
      // Ignora ghost click no X/Cancelar enquanto o swipe anima o fechamento
      if (panel && panel.dataset.isSwiping === 'true' && panel.dataset.swipeClosed !== 'true') {
        if (e) {
          if (e.cancelable) e.preventDefault();
          e.stopPropagation();
          if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        }
        return;
      }
      var t = now();
      if (t - lastTriggerAt < DEBOUNCE_MS) {
        if (e) {
          if (e.cancelable) e.preventDefault();
          e.stopPropagation();
          if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        }
        return;
      }
      lastTriggerAt = t;
      suppressClickUntil = t + CLICK_SUPPRESS_MS;
      if (e) {
        if (e.cancelable) e.preventDefault();
        e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      }
      try {
        closeFn(e);
      } catch (err) {
        if (global.console) console.error('[drawer-safe-close] closeControl', err);
      }
    }

    function onPointerDown(e) {
      usedPointer = true;
      if (typeof e.button === 'number' && e.button !== 0) return;
      trigger(e);
    }

    function onTouchStart(e) {
      if (usedPointer) return;
      trigger(e);
    }

    function onClick(e) {
      var t = now();
      if (t < suppressClickUntil) {
        if (e.cancelable) e.preventDefault();
        e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        return;
      }
      trigger(e);
    }

    controlEl.addEventListener('pointerdown', onPointerDown, { passive: false, capture: true });
    controlEl.addEventListener('touchstart', onTouchStart, { passive: false, capture: true });
    controlEl.addEventListener('click', onClick, true);

    // Evita disputa com swipe-to-dismiss no cabeçalho
    if (!controlEl.hasAttribute('data-swipe-ignore')) {
      controlEl.setAttribute('data-swipe-ignore', '');
    }

    var controller = {
      trigger: function () {
        trigger(null);
      },
      destroy: function () {
        controlEl.removeEventListener('pointerdown', onPointerDown, true);
        controlEl.removeEventListener('touchstart', onTouchStart, true);
        controlEl.removeEventListener('click', onClick, true);
        delete controlEl.__drawerSafeCloseBound;
      }
    };
    controlEl.__drawerSafeCloseBound = controller;
    return controller;
  }

  /**
   * Impede que toques/cliques dentro do painel subam até o backdrop.
   */
  function bindPanelStopPropagation(panelEl) {
    panelEl = resolveEl(panelEl);
    if (!panelEl || panelEl.__drawerStopPropBound) return;
    panelEl.__drawerStopPropBound = true;

    function stop(e) {
      e.stopPropagation();
    }

    panelEl.addEventListener('pointerdown', stop);
    panelEl.addEventListener('touchstart', stop, { passive: true });
    panelEl.addEventListener('click', stop);

    if (!panelEl.classList.contains('transition-transform')) {
      panelEl.classList.add('transition-transform');
    }
    if (!/\bduration-\d+\b/.test(panelEl.className)) {
      panelEl.classList.add('duration-300');
    }
    if (
      !panelEl.classList.contains('ease-out') &&
      !panelEl.classList.contains('ease-in-out') &&
      !panelEl.classList.contains('ease-in')
    ) {
      panelEl.classList.add('ease-out');
    }
  }

  function closeHandlerFromPanel(panel) {
    var fnName = panel.getAttribute('data-swipe-close');
    var clickSelector = panel.getAttribute('data-swipe-close-click');
    if (!fnName && !clickSelector) return null;
    return function () {
      if (fnName && typeof global[fnName] === 'function') {
        global[fnName]();
        return;
      }
      var btn = clickSelector ? document.querySelector(clickSelector) : null;
      if (btn) btn.click();
    };
  }

  /**
   * Inicialização declarativa:
   *  - [data-drawer-backdrop-close="nomeFnGlobal"]
   *  - painéis [data-swipe-dismiss] + data-swipe-backdrop (liga o overlay)
   */
  function init(root) {
    var scope = root || document;

    Array.prototype.forEach.call(
      scope.querySelectorAll('[data-drawer-backdrop-close]'),
      function (backdrop) {
        var fnName = backdrop.getAttribute('data-drawer-backdrop-close');
        if (!fnName) return;
        bindBackdropClose(backdrop, function () {
          if (typeof global[fnName] === 'function') global[fnName]();
        });
      }
    );

    Array.prototype.forEach.call(
      scope.querySelectorAll('[data-swipe-dismiss]'),
      function (panel) {
        bindPanelStopPropagation(panel);
        var backdropSel = panel.getAttribute('data-swipe-backdrop');
        if (!backdropSel) return;
        var backdrop = document.querySelector(backdropSel);
        if (!backdrop) return;
        // Já ligado via data-drawer-backdrop-close
        if (backdrop.hasAttribute('data-drawer-backdrop-close')) return;
        var onClose = closeHandlerFromPanel(panel);
        if (!onClose) return;
        bindBackdropClose(backdrop, onClose);
      }
    );
  }

  global.MeninoAlhoDrawerSafe = {
    DURATION_MS: DURATION_MS,
    fecharGavetaComSeguranca: fecharGavetaComSeguranca,
    withClosingGuard: withClosingGuard,
    beginClosing: beginClosing,
    endClosing: endClosing,
    isClosing: isClosing,
    bindBackdropClose: bindBackdropClose,
    bindCloseControl: bindCloseControl,
    bindPanelStopPropagation: bindPanelStopPropagation,
    init: init
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      init();
    });
  } else {
    init();
  }
})(typeof window !== 'undefined' ? window : globalThis);
