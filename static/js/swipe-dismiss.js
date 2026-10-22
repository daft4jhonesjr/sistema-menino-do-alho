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
  /** Folga para preferir fechamento horizontal sobre scroll vertical (iOS WebView). */
  var HORIZONTAL_AXIS_BIAS = 10;
  var HANDLE_AXIS_BIAS = 4;
  var MIN_CLOSE_OFFSET_PX = 60;
  /** Gesto nativo “voltar” do Safari perto da borda esquerda da tela. */
  var VIEWPORT_LEFT_EDGE_PX = 20;
  var VELOCITY_WINDOW_MS = 100;
  var CLEANUP_TIMEOUT_MS = 1200;
  /** Janela anti ghost click (touchend → click sintético ~300ms no mobile). */
  var CLICK_SUPPRESS_MS = 400;
  /** Cooldown que impede reabrir a gaveta logo após fechar por swipe. */
  var OPEN_BLOCK_MS = 400;
  /** Deslocamento mínimo (px) para tratar o gesto como swipe e cancelar o click fantasma. */
  var GHOST_CLICK_DELTA_PX = 40;
  var SETTLE_CLOSE_MS = 250;
  var SETTLE_OPEN_MS = 200;
  var DRAGGING_BODY_CLASS = 'swipe-dismiss-dragging';
  var EXITING_CLASS = 'swipe-dismiss-exiting';
  var BACKDROP_CLASS = 'swipe-dismiss-backdrop';
  var HANDLE_SELECTOR = '[data-swipe-handle]';
  var TEXT_INPUT_SELECTOR =
    'input, textarea, select, [contenteditable=""], [contenteditable="true"]';
  /** Curva de saída alinhada ao fechamento por swipe (evita conflito com transition Tailwind). */
  var SETTLE_CLOSE_EASING = 'cubic-bezier(0.16, 1, 0.3, 1)';

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
   *   minClosePx?: number,
   *   flickVelocity?: number,
   *   duration?: number,
   *   allowBodySwipe?: boolean
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
    var minClosePx =
      typeof options.minClosePx === 'number' ? options.minClosePx : MIN_CLOSE_OFFSET_PX;
    var flickVelocity =
      typeof options.flickVelocity === 'number' ? options.flickVelocity : FLICK_VELOCITY;
    var duration = typeof options.duration === 'number' ? options.duration : DURATION_MS;
    var allowBodySwipe = options.allowBodySwipe !== false;

    /** 'idle' | 'pending' | 'dragging' | 'returning' | 'closing' */
    var state = 'idle';
    /** @type {null | { id: number, startX: number, startY: number, base: number, offset: number, width: number, fromHandle: boolean, scrollRoot: Element|null, samples: Array<{x: number, t: number}> }} */
    var gesture = null;
    var settleToken = 0;
    var cleanupRaf = 0;
    var suppressClickUntil = 0;
    var openBlockTimer = 0;

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

    function unlockScrollRoot(g) {
      if (!g || !g.scrollRoot) return;
      g.scrollRoot.style.overflow = '';
    }

    function beginDrag(g) {
      cancelPendingCleanup();
      settleToken++;
      panel.style.transition = 'none';
      panel.style.willChange = 'transform';
      var backdrop = getBackdrop();
      if (backdrop) {
        backdrop.style.transition = 'none';
        backdrop.style.willChange = 'opacity';
      }
      if (g && g.scrollRoot) {
        g.scrollRoot.style.overflow = 'hidden';
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

    function setSwiping(active) {
      panel.dataset.isSwiping = active ? 'true' : 'false';
    }

    /** Durante a saída o painel não recebe toques (pointer-events: none via CSS). */
    function setExiting(active) {
      panel.classList.toggle(EXITING_CLASS, !!active);
    }

    function markBackdrop() {
      var backdrop = getBackdrop();
      if (backdrop) backdrop.classList.add(BACKDROP_CLASS);
    }

    /**
     * Trava temporária contra reabertura (ghost click no botão de abrir / toggle).
     * Funções como abrirModalNovaVenda() devem consultar dataset.bloquearAbertura.
     */
    function blockReopenTemporarily() {
      panel.dataset.bloquearAbertura = 'true';
      if (openBlockTimer) {
        global.clearTimeout(openBlockTimer);
        openBlockTimer = 0;
      }
      openBlockTimer = global.setTimeout(function () {
        openBlockTimer = 0;
        delete panel.dataset.bloquearAbertura;
      }, OPEN_BLOCK_MS);
    }

    /**
     * Sincroniza classes Tailwind com o transform inline ANTES de limpar o style.
     * Sem isso, o WebKit pode renderizar 1 frame em translateX(0) (painel “pisca”).
     */
    function syncClosedClassesBeforeClearingInline() {
      panel.style.transition = 'none';
      panel.style.transform = 'translate3d(100%, 0, 0)';
      panel.classList.add('translate-x-full');
      panel.classList.remove('translate-x-0');
      // Evita estado ambíguo no DOM após o swipe (onClose também pode aplicar hidden)
      panel.classList.add('hidden');
      panel.setAttribute('aria-hidden', 'true');

      var backdrop = getBackdrop();
      if (backdrop) {
        backdrop.style.transition = 'none';
        backdrop.style.opacity = '0';
        backdrop.classList.add('opacity-0');
        backdrop.classList.remove('opacity-100', 'is-visible');
        backdrop.setAttribute('aria-hidden', 'true');
      }

      // Limpa totalmente os estilos inline para não deixar o elemento ambíguo
      void panel.offsetWidth;
      panel.style.transform = '';
      panel.style.transition = '';
      panel.style.willChange = '';
      if (backdrop) {
        backdrop.style.opacity = '';
        backdrop.style.transition = '';
        backdrop.style.willChange = '';
      }
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
        if (!panelDone && (expired || reopened || !isRendered(panel) || panel.classList.contains('translate-x-full') || panel.classList.contains('hidden'))) {
          clearPanelStyles();
          panelDone = true;
        }
        if (!backdropDone && (expired || reopened || !isRendered(getBackdrop()) || (getBackdrop() && getBackdrop().classList.contains('hidden')))) {
          clearBackdropStyles();
          backdropDone = true;
        }
        if (!panelDone || !backdropDone) {
          cleanupRaf = global.requestAnimationFrame(tick);
        }
      }

      tick();
    }

    function animate(toClosed, done, settleMs, settleEasing) {
      var token = ++settleToken;
      var ms = typeof settleMs === 'number' ? settleMs : duration;
      var ease = settleEasing || EASING;
      var transition = ms + 'ms ' + ease;
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
      var fallback = global.setTimeout(finish, ms + 80);
    }

    function settleOpen() {
      state = 'returning';
      setSwiping(false);
      document.body.classList.remove(DRAGGING_BODY_CLASS);
      animate(
        false,
        function () {
          state = 'idle';
          // Devolve à posição aberta e só então limpa o inline (sem piscar)
          panel.style.transition = 'none';
          panel.style.transform = translate(0);
          void panel.offsetWidth;
          clearPanelStyles();
          clearBackdropStyles();
        },
        SETTLE_OPEN_MS,
        'ease-out'
      );
    }

    /**
     * Conclusão do swipe: anima com style.transform, chama onClose (com flag para
     * pular 2ª animação) mantendo o inline em 100%, e só então sincroniza classes
     * Tailwind e limpa o style — evita o frame em translateX(0) no WebKit.
     */
    function settleClosed() {
      state = 'closing';
      setSwiping(true);
      setExiting(true);
      blockReopenTemporarily();
      suppressClickUntil = now() + CLICK_SUPPRESS_MS + SETTLE_CLOSE_MS;
      document.body.classList.remove(DRAGGING_BODY_CLASS);
      animate(
        true,
        function () {
          var token = settleToken;
          // Congela fora da tela com inline até o onClose aplicar as classes
          panel.style.transition = 'none';
          panel.style.transform = 'translate3d(100%, 0, 0)';
          var backdrop = getBackdrop();
          if (backdrop) {
            backdrop.style.transition = 'none';
            backdrop.style.opacity = '0';
          }

          panel.dataset.swipeClosed = 'true';
          state = 'idle';
          try {
            onClose();
          } catch (err) {
            if (global.console) console.error('[swipe-dismiss] onClose falhou', err);
          }
          delete panel.dataset.swipeClosed;

          if (token !== settleToken) {
            setSwiping(false);
            setExiting(false);
            return;
          }

          if (isOpen()) {
            // Fechamento recusado (ex.: confirmação de descarte): volta ao lugar.
            delete panel.dataset.bloquearAbertura;
            if (openBlockTimer) {
              global.clearTimeout(openBlockTimer);
              openBlockTimer = 0;
            }
            setSwiping(false);
            setExiting(false);
            void panel.offsetWidth;
            settleOpen();
          } else {
            // onClose já fechou (classes/hidden). Sincroniza e limpa o inline com segurança.
            syncClosedClassesBeforeClearingInline();
            deferCleanupAfterClose();
            // Renova a trava após a animação (cobre o delay do click fantasma)
            blockReopenTemporarily();
            // Mantém a trava até a janela de click sintético do mobile acabar
            global.setTimeout(function () {
              setSwiping(false);
              setExiting(false);
            }, CLICK_SUPPRESS_MS);
          }
        },
        SETTLE_CLOSE_MS,
        SETTLE_CLOSE_EASING
      );
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
      if (gesture) unlockScrollRoot(gesture);
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
      if (panel.dataset.isSwiping === 'true') return;
      if (!e.touches || e.touches.length !== 1) return;
      if (!isOpen()) return;

      var touch = e.touches[0];
      var target = e.target;
      if (!target || !target.closest) return;
      if (target.closest('[data-swipe-ignore], input[type="range"]')) return;

      var rect = panel.getBoundingClientRect();
      var fromPanelEdge = touch.clientX - rect.left <= edgeWidth;
      var fromViewportEdge = touch.clientX <= VIEWPORT_LEFT_EDGE_PX;
      var fromEdge = fromPanelEdge || fromViewportEdge;
      var handle = target.closest(handleSelector);
      var onHandle = !!handle && panel.contains(handle);
      if (!fromEdge && !onHandle && !allowBodySwipe) return;
      if (!fromEdge && !onHandle && target.closest(TEXT_INPUT_SELECTOR)) return;
      if (insideScrolledHorizontalScroller(target, panel)) return;

      var scrollRoot = target.closest(
        '[data-swipe-scroll], .overflow-y-auto, .overflow-y-scroll, .overscroll-contain'
      );
      if (scrollRoot && !panel.contains(scrollRoot)) scrollRoot = null;

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
        fromHandle: onHandle,
        scrollRoot: scrollRoot,
        samples: [{ x: touch.clientX, t: now() }]
      };
      state = 'pending';
    }

    function axisBias(g) {
      return g && g.fromHandle ? HANDLE_AXIS_BIAS : HORIZONTAL_AXIS_BIAS;
    }

    function isClearlyHorizontal(dx, dy, g) {
      return Math.abs(dx) > Math.abs(dy) + axisBias(g);
    }

    function isClearlyVertical(dx, dy, g) {
      return Math.abs(dy) > Math.abs(dx) + axisBias(g);
    }

    function onTouchMove(e) {
      if (!gesture) return;
      var touch = findTouch(e.touches, gesture.id);
      if (!touch) return;
      var dx = touch.clientX - gesture.startX;
      var dy = touch.clientY - gesture.startY;

      if (state === 'pending') {
        if (Math.abs(dx) < AXIS_LOCK_PX && Math.abs(dy) < AXIS_LOCK_PX) return;
        var leftward = dx < 0 && gesture.base === 0;
        if (leftward || isClearlyVertical(dx, dy, gesture)) {
          abortPending();
          return;
        }
        if (!isClearlyHorizontal(dx, dy, gesture)) return;
        state = 'dragging';
        // Rebase para o painel não "pular" a folga do travamento de eixo.
        gesture.startX = touch.clientX;
        gesture.startY = touch.clientY;
        dx = 0;
        beginDrag(gesture);
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
      var deltaX = active.offset;
      unlockScrollRoot(gesture);
      gesture = null;
      // Trava anti click sintético (touchend → click) em cima da animação de fechamento
      suppressClickUntil = now() + CLICK_SUPPRESS_MS;
      setSwiping(true);

      var threshold = Math.min(active.width, global.innerWidth) * closeRatio;
      var flickRight = v > flickVelocity;
      var flickLeft = v < -flickVelocity;
      var passedDistance =
        active.offset >= minClosePx || active.offset > threshold;
      if (active.offset > 0 && (flickRight || (passedDistance && !flickLeft))) {
        // Cancela o click sintético (ghost click) que o iOS dispararia após o touchend
        if (deltaX > GHOST_CLICK_DELTA_PX && e.cancelable) {
          e.preventDefault();
        }
        e.stopPropagation();
        if (e.stopImmediatePropagation) e.stopImmediatePropagation();
        blockReopenTemporarily();
        settleClosed();
      } else {
        settleOpen();
      }
    }

    function onTouchCancel(e) {
      if (!gesture) return;
      if (!findTouch(e.changedTouches, gesture.id)) return;
      if (state === 'dragging') {
        unlockScrollRoot(gesture);
        gesture = null;
        settleOpen();
      } else {
        abortPending();
      }
    }

    function shouldSuppressResidualClick() {
      return (
        panel.dataset.isSwiping === 'true' ||
        panel.dataset.bloquearAbertura === 'true' ||
        now() < suppressClickUntil
      );
    }

    function suppressResidualEvent(e) {
      if (e.cancelable) e.preventDefault();
      e.stopPropagation();
      if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    }

    function onClickCapture(e) {
      if (shouldSuppressResidualClick()) {
        suppressResidualEvent(e);
      }
    }

    function onDocumentClickCapture(e) {
      if (!e.isTrusted || !shouldSuppressResidualClick()) return;
      // Clique residual do gesto: não pode alcançar o botão disparador atrás do painel
      suppressResidualEvent(e);
    }

    panel.addEventListener('touchstart', onTouchStart, { passive: true });
    panel.addEventListener('touchmove', onTouchMove, { passive: false, capture: true });
    panel.addEventListener('touchend', onTouchEnd, { passive: false });
    panel.addEventListener('touchcancel', onTouchCancel, { passive: true });
    panel.addEventListener('click', onClickCapture, true);
    document.addEventListener('click', onDocumentClickCapture, true);
    panel.classList.add('swipe-dismiss-panel');
    markBackdrop();
    setSwiping(false);
    setExiting(false);

    var controller = {
      /** Fecha com a mesma animação do gesto (útil para botões "voltar"). */
      dismiss: function () {
        if (state === 'closing' || panel.dataset.isSwiping === 'true' || !isOpen()) return;
        gesture = null;
        beginDrag(null);
        settleClosed();
      },
      isDragging: function () {
        return state === 'dragging' || panel.dataset.isSwiping === 'true';
      },
      destroy: function () {
        cancelPendingCleanup();
        settleToken++;
        if (openBlockTimer) {
          global.clearTimeout(openBlockTimer);
          openBlockTimer = 0;
        }
        panel.removeEventListener('touchstart', onTouchStart);
        panel.removeEventListener('touchmove', onTouchMove);
        panel.removeEventListener('touchend', onTouchEnd);
        panel.removeEventListener('touchcancel', onTouchCancel);
        panel.removeEventListener('click', onClickCapture, true);
        document.removeEventListener('click', onDocumentClickCapture, true);
        panel.classList.remove('swipe-dismiss-panel');
        clearPanelStyles();
        clearBackdropStyles();
        document.body.classList.remove(DRAGGING_BODY_CLASS);
        setSwiping(false);
        setExiting(false);
        delete panel.dataset.swipeClosed;
        delete panel.dataset.bloquearAbertura;
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
