/**
 * Swipe-to-dismiss interativo para painéis laterais que entram pela direita (estilo iOS).
 *
 * O painel acompanha o dedo em tempo real (esquerda → direita), o backdrop clareia
 * proporcionalmente e, ao soltar, o painel conclui a saída ou volta à posição original
 * com a curva de desaceleração do UIKit.
 *
 * Uso declarativo (inicializado automaticamente):
 *   <div id="minha-gaveta"
 *        data-swipe-dismiss
 *        data-swipe-backdrop="#minha-gaveta-overlay"
 *        data-swipe-close="fecharMinhaGaveta">          <!-- função global -->
 *        <!-- ou data-swipe-close-click="#btn-fechar" (clica no botão de fechar) -->
 *     <header data-swipe-handle>…</header>
 *   </div>
 *
 * Uso programático:
 *   MeninoAlhoSwipeDismiss.attach(panel, { backdrop, onClose, isOpen, handle, edgeWidth });
 *
 * O arrasto só começa na faixa da borda esquerda do painel (edgeWidth) ou no cabeçalho
 * ([data-swipe-handle]); elementos com [data-swipe-ignore] nunca iniciam o gesto.
 */
(function (global) {
  'use strict';

  var EASING = 'cubic-bezier(0.32, 0.72, 0, 1)';
  var DURATION_MS = 300;
  var DEFAULT_EDGE_WIDTH = 40;
  var CLOSE_RATIO = 0.35;
  var FLICK_VELOCITY = 0.5;
  var AXIS_LOCK_PX = 8;
  var VELOCITY_WINDOW_MS = 100;
  var CLEANUP_TIMEOUT_MS = 1200;
  var CLICK_SUPPRESS_MS = 350;
  var DRAGGING_BODY_CLASS = 'swipe-dismiss-dragging';
  var HANDLE_SELECTOR = '[data-swipe-handle]';
  var TEXT_INPUT_SELECTOR =
    'input, textarea, select, [contenteditable=""], [contenteditable="true"]';

  function now() {
    return global.performance && performance.now ? performance.now() : Date.now();
  }

  function resolveElement(ref) {
    if (!ref) return null;
    if (typeof ref === 'string') return document.querySelector(ref);
    return ref;
  }

  function isRendered(el) {
    return !!el && el.getClientRects().length > 0;
  }

  function defaultIsOpen(panel) {
    return isRendered(panel) && !panel.classList.contains('translate-x-full');
  }

  function currentTranslateX(el) {
    var t = global.getComputedStyle(el).transform;
    if (!t || t === 'none') return 0;
    try {
      return new (global.DOMMatrixReadOnly || global.WebKitCSSMatrix)(t).m41 || 0;
    } catch (err) {
      return 0;
    }
  }

  /** Evita disputar o gesto com tabelas/carrosséis já rolados na horizontal. */
  function insideScrolledHorizontalScroller(target, panel) {
    for (var el = target; el && el !== panel; el = el.parentElement) {
      if (el.scrollLeft > 0 && el.scrollWidth > el.clientWidth + 1) {
        var ox = global.getComputedStyle(el).overflowX;
        if (ox === 'auto' || ox === 'scroll') return true;
      }
    }
    return false;
  }

  function translate(px) {
    return 'translate3d(' + px + 'px, 0, 0)';
  }

  /**
   * @param {Element} panel
   * @param {{
   *   backdrop?: Element|string|null,
   *   onClose: function(),
   *   isOpen?: function(): boolean,
   *   handle?: string,
   *   edgeWidth?: number,
   *   closeRatio?: number,
   *   flickVelocity?: number,
   *   duration?: number
   * }} options
   */
  function attach(panel, options) {
    if (!panel) return null;
    if (panel.__swipeDismiss) return panel.__swipeDismiss;
    options = options || {};
    if (typeof options.onClose !== 'function') return null;

    var backdropRef = options.backdrop || null;
    var onClose = options.onClose;
    var isOpen =
      typeof options.isOpen === 'function'
        ? options.isOpen
        : function () {
            return defaultIsOpen(panel);
          };
    var handleSelector = options.handle || HANDLE_SELECTOR;
    var edgeWidth = typeof options.edgeWidth === 'number' ? options.edgeWidth : DEFAULT_EDGE_WIDTH;
    var closeRatio = typeof options.closeRatio === 'number' ? options.closeRatio : CLOSE_RATIO;
    var flickVelocity =
      typeof options.flickVelocity === 'number' ? options.flickVelocity : FLICK_VELOCITY;
    var duration = typeof options.duration === 'number' ? options.duration : DURATION_MS;

    /** 'idle' | 'pending' | 'dragging' | 'returning' | 'closing' */
    var state = 'idle';
    /** @type {null | { id: number, startX: number, startY: number, base: number, offset: number, width: number, samples: Array<{x: number, t: number}> }} */
    var gesture = null;
    var settleToken = 0;
    var cleanupRaf = 0;
    var suppressClickUntil = 0;

    function getBackdrop() {
      return resolveElement(backdropRef);
    }

    function findTouch(list, id) {
      if (!list) return null;
      for (var i = 0; i < list.length; i++) {
        if (list[i].identifier === id) return list[i];
      }
      return null;
    }

    function render(offset, width) {
      panel.style.transform = translate(offset);
      var backdrop = getBackdrop();
      if (backdrop) {
        backdrop.style.opacity = String(Math.max(0, 1 - offset / width));
      }
    }

    function beginDrag() {
      cancelPendingCleanup();
      settleToken++;
      panel.style.transition = 'none';
      panel.style.willChange = 'transform';
      var backdrop = getBackdrop();
      if (backdrop) {
        backdrop.style.transition = 'none';
        backdrop.style.willChange = 'opacity';
      }
      document.body.classList.add(DRAGGING_BODY_CLASS);
    }

    function clearPanelStyles() {
      panel.style.transition = 'none';
      panel.style.transform = '';
      panel.style.willChange = '';
      void panel.offsetWidth;
      panel.style.transition = '';
    }

    function clearBackdropStyles() {
      var backdrop = getBackdrop();
      if (!backdrop) return;
      backdrop.style.transition = 'none';
      backdrop.style.opacity = '';
      backdrop.style.willChange = '';
      void backdrop.offsetWidth;
      backdrop.style.transition = '';
    }

    function cancelPendingCleanup() {
      if (cleanupRaf) {
        global.cancelAnimationFrame(cleanupRaf);
        cleanupRaf = 0;
      }
    }

    /**
     * Após o fechamento, mantém painel e backdrop congelados na posição final até que o
     * código de fechamento do painel assuma (classe translate-x-full, hidden ou reabertura),
     * evitando o "flash" do backdrop voltando à opacidade original durante o fade-out dele.
     */
    function deferCleanupAfterClose() {
      cancelPendingCleanup();
      var deadline = now() + CLEANUP_TIMEOUT_MS;
      var panelDone = false;
      var backdropDone = !getBackdrop();

      function tick() {
        cleanupRaf = 0;
        var expired = now() >= deadline;
        var reopened = isOpen();
        if (!panelDone && (expired || reopened || !isRendered(panel) || panel.classList.contains('translate-x-full'))) {
          clearPanelStyles();
          panelDone = true;
        }
        if (!backdropDone && (expired || reopened || !isRendered(getBackdrop()))) {
          clearBackdropStyles();
          backdropDone = true;
        }
        if (!panelDone || !backdropDone) {
          cleanupRaf = global.requestAnimationFrame(tick);
        }
      }

      tick();
    }

    function animate(toClosed, done) {
      var token = ++settleToken;
      var transition = duration + 'ms ' + EASING;
      panel.style.transition = 'transform ' + transition;
      panel.style.transform = toClosed ? 'translate3d(100%, 0, 0)' : translate(0);
      var backdrop = getBackdrop();
      if (backdrop) {
        backdrop.style.transition = 'opacity ' + transition;
        backdrop.style.opacity = toClosed ? '0' : '1';
      }

      var finished = false;
      function finish() {
        if (finished) return;
        finished = true;
        panel.removeEventListener('transitionend', onEnd);
        global.clearTimeout(fallback);
        if (token !== settleToken) return;
        done();
      }
      function onEnd(e) {
        if (e.target === panel && e.propertyName === 'transform') finish();
      }
      panel.addEventListener('transitionend', onEnd);
      var fallback = global.setTimeout(finish, duration + 80);
    }

    function settleOpen() {
      state = 'returning';
      document.body.classList.remove(DRAGGING_BODY_CLASS);
      animate(false, function () {
        state = 'idle';
        clearPanelStyles();
        clearBackdropStyles();
      });
    }

    function settleClosed() {
      state = 'closing';
      document.body.classList.remove(DRAGGING_BODY_CLASS);
      animate(true, function () {
        state = 'idle';
        panel.style.transition = 'none';
        var backdrop = getBackdrop();
        if (backdrop) backdrop.style.transition = 'none';
        try {
          onClose();
        } catch (err) {
          if (global.console) console.error('[swipe-dismiss] onClose falhou', err);
        }
        if (isOpen()) {
          // Fechamento recusado (ex.: confirmação de descarte): volta ao lugar.
          void panel.offsetWidth;
          settleOpen();
        } else {
          deferCleanupAfterClose();
        }
      });
    }

    function velocity(endX) {
      if (!gesture) return 0;
      var t = now();
      var samples = gesture.samples.concat([{ x: endX, t: t }]);
      var first = null;
      for (var i = 0; i < samples.length; i++) {
        if (t - samples[i].t <= VELOCITY_WINDOW_MS) {
          first = samples[i];
          break;
        }
      }
      if (!first) return 0;
      var dt = t - first.t;
      if (dt <= 0) return 0;
      return (endX - first.x) / dt;
    }

    function pushSample(x) {
      var t = now();
      var samples = gesture.samples;
      samples.push({ x: x, t: t });
      while (samples.length > 2 && t - samples[0].t > VELOCITY_WINDOW_MS) {
        samples.shift();
      }
    }

    function abortPending() {
      var base = gesture ? gesture.base : 0;
      gesture = null;
      state = 'idle';
      if (base > 0) settleOpen();
    }

    function onTouchStart(e) {
      if (state === 'pending' || state === 'dragging') {
        if (e.touches.length > 1) {
          if (state === 'dragging') {
            gesture = null;
            settleOpen();
          } else {
            abortPending();
          }
        }
        return;
      }
      if (state === 'closing') return;
      if (!e.touches || e.touches.length !== 1) return;
      if (!isOpen()) return;

      var touch = e.touches[0];
      var target = e.target;
      if (!target || !target.closest) return;
      if (target.closest('[data-swipe-ignore], input[type="range"]')) return;

      var rect = panel.getBoundingClientRect();
      var fromEdge = touch.clientX - rect.left <= edgeWidth;
      var handle = target.closest(handleSelector);
      var onHandle = !!handle && panel.contains(handle);
      if (!fromEdge && !onHandle) return;
      if (!fromEdge && target.closest(TEXT_INPUT_SELECTOR)) return;
      if (insideScrolledHorizontalScroller(target, panel)) return;

      var base = 0;
      if (state === 'returning') {
        // Agarra o painel no meio da animação de retorno, como no UIKit.
        base = Math.max(0, currentTranslateX(panel));
        settleToken++;
        panel.style.transition = 'none';
        panel.style.transform = translate(base);
      }

      gesture = {
        id: touch.identifier,
        startX: touch.clientX,
        startY: touch.clientY,
        base: base,
        offset: base,
        width: rect.width || global.innerWidth,
        samples: [{ x: touch.clientX, t: now() }]
      };
      state = 'pending';
    }

    function onTouchMove(e) {
      if (!gesture) return;
      var touch = findTouch(e.touches, gesture.id);
      if (!touch) return;
      var dx = touch.clientX - gesture.startX;
      var dy = touch.clientY - gesture.startY;

      if (state === 'pending') {
        if (Math.abs(dx) < AXIS_LOCK_PX && Math.abs(dy) < AXIS_LOCK_PX) return;
        var vertical = Math.abs(dy) >= Math.abs(dx);
        var leftward = dx < 0 && gesture.base === 0;
        if (vertical || leftward) {
          abortPending();
          return;
        }
        state = 'dragging';
        // Rebase para o painel não "pular" a folga do travamento de eixo.
        gesture.startX = touch.clientX;
        gesture.startY = touch.clientY;
        dx = 0;
        beginDrag();
      }

      if (state !== 'dragging') return;
      if (e.cancelable) e.preventDefault();

      var offset = Math.min(gesture.width, Math.max(0, gesture.base + dx));
      gesture.offset = offset;
      pushSample(touch.clientX);
      render(offset, gesture.width);
    }

    function onTouchEnd(e) {
      if (!gesture) return;
      var touch = findTouch(e.changedTouches, gesture.id);
      if (!touch) return;

      if (state === 'pending') {
        abortPending();
        return;
      }
      if (state !== 'dragging') return;

      var active = gesture;
      var v = velocity(touch.clientX);
      gesture = null;
      suppressClickUntil = now() + CLICK_SUPPRESS_MS;

      var threshold = Math.min(active.width, global.innerWidth) * closeRatio;
      var flickRight = v > flickVelocity;
      var flickLeft = v < -flickVelocity;
      if (active.offset > 0 && (flickRight || (active.offset > threshold && !flickLeft))) {
        settleClosed();
      } else {
        settleOpen();
      }
    }

    function onTouchCancel(e) {
      if (!gesture) return;
      if (!findTouch(e.changedTouches, gesture.id)) return;
      if (state === 'dragging') {
        gesture = null;
        settleOpen();
      } else {
        abortPending();
      }
    }

    function onClickCapture(e) {
      if (e.isTrusted && now() < suppressClickUntil) {
        e.preventDefault();
        e.stopPropagation();
      }
    }

    panel.addEventListener('touchstart', onTouchStart, { passive: true });
    panel.addEventListener('touchmove', onTouchMove, { passive: false });
    panel.addEventListener('touchend', onTouchEnd, { passive: true });
    panel.addEventListener('touchcancel', onTouchCancel, { passive: true });
    panel.addEventListener('click', onClickCapture, true);
    panel.classList.add('swipe-dismiss-panel');

    var controller = {
      /** Fecha com a mesma animação do gesto (útil para botões "voltar"). */
      dismiss: function () {
        if (state === 'closing' || !isOpen()) return;
        gesture = null;
        beginDrag();
        settleClosed();
      },
      isDragging: function () {
        return state === 'dragging';
      },
      destroy: function () {
        cancelPendingCleanup();
        settleToken++;
        panel.removeEventListener('touchstart', onTouchStart);
        panel.removeEventListener('touchmove', onTouchMove);
        panel.removeEventListener('touchend', onTouchEnd);
        panel.removeEventListener('touchcancel', onTouchCancel);
        panel.removeEventListener('click', onClickCapture, true);
        panel.classList.remove('swipe-dismiss-panel');
        clearPanelStyles();
        clearBackdropStyles();
        document.body.classList.remove(DRAGGING_BODY_CLASS);
        delete panel.__swipeDismiss;
      }
    };
    panel.__swipeDismiss = controller;
    return controller;
  }

  function closeHandlerFromDataset(panel) {
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

  function init(root) {
    var scope = root || document;
    var panels = scope.querySelectorAll('[data-swipe-dismiss]');
    Array.prototype.forEach.call(panels, function (panel) {
      var onClose = closeHandlerFromDataset(panel);
      if (!onClose) return;
      var edge = parseFloat(panel.getAttribute('data-swipe-edge'));
      attach(panel, {
        backdrop: panel.getAttribute('data-swipe-backdrop'),
        onClose: onClose,
        edgeWidth: isFinite(edge) ? edge : undefined
      });
    });
  }

  global.MeninoAlhoSwipeDismiss = {
    attach: attach,
    init: init,
    EASING: EASING,
    DURATION_MS: DURATION_MS
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      init();
    });
  } else {
    init();
  }
})(typeof window !== 'undefined' ? window : globalThis);
