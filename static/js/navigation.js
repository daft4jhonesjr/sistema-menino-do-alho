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

  var DRAWER_DRAG_TRANSITION = 'transform 0.25s cubic-bezier(0.16, 1, 0.3, 1)';
  var DRAWER_DRAG_VELOCITY = 0.35;

  /**
   * Drawer lateral com arrasto interativo (abrir da borda / fechar com swipe).
   * Drawer à esquerda: abrir com deltaX > 0; fechar com deltaX < 0.
   * @param {{
   *   panel: Element,
   *   backdrop?: Element|null,
   *   isOpen: function(): boolean,
   *   onOpen: function(),
   *   onClose: function(),
   *   drawerPosition?: 'left'|'right',
   *   edgeWidth?: number,
   *   openThreshold?: number,
   *   closeThreshold?: number,
   *   draggingBodyClass?: string,
   *   closeGesture?: boolean,
   *   canStartOpenGesture?: function(): boolean,
   *   shouldIgnoreTouchTarget?: function(Element): boolean
   * }} options
   */
  function setupDrawerInteractiveDrag(options) {
    if (
      !options ||
      !options.panel ||
      typeof options.onOpen !== 'function' ||
      typeof options.onClose !== 'function'
    ) {
      return;
    }

    var panel = options.panel;
    var backdrop = options.backdrop || null;
    var isOpen = options.isOpen || function () {
      return false;
    };
    var onOpen = options.onOpen;
    var onClose = options.onClose;
    var drawerPosition = options.drawerPosition === 'right' ? 'right' : 'left';
    var isRightDrawer = drawerPosition === 'right';
    var onOpenGestureStart =
      typeof options.onOpenGestureStart === 'function' ? options.onOpenGestureStart : null;

    var edgeWidth = typeof options.edgeWidth === 'number' ? options.edgeWidth : 40;
    var openThreshold = typeof options.openThreshold === 'number' ? options.openThreshold : 0.35;
    var closeThreshold = typeof options.closeThreshold === 'number' ? options.closeThreshold : 0.3;
    var draggingBodyClass = options.draggingBodyClass || 'site-nav-mobile-dragging';
    var canStartOpenGesture =
      options.canStartOpenGesture ||
      function () {
        return !isOpen();
      };
    var shouldIgnoreTouchTarget =
      options.shouldIgnoreTouchTarget ||
      function () {
        return false;
      };

    /** @type {null | { mode: 'open'|'close', startX: number, startY: number, startTime: number, drawerWidth: number, locked: boolean }} */
    var gesture = null;

    function transformClosed(width) {
      return isRightDrawer
        ? 'translate3d(' + width + 'px, 0, 0)'
        : 'translate3d(' + -width + 'px, 0, 0)';
    }

    function transformOpen() {
      return 'translate3d(0, 0, 0)';
    }

    function openProgressFromTx(width, tx) {
      if (isRightDrawer) {
        return Math.min(1, Math.max(0, 1 - tx / width));
      }
      return Math.min(1, Math.max(0, (width + tx) / width));
    }

    function alvoIgnoraSwipe(target) {
      if (!target || !target.closest) return false;
      return !!target.closest(
        'input, textarea, select, button, [contenteditable="true"], label[for]'
      );
    }

    function getDrawerWidth() {
      var rect = panel.getBoundingClientRect();
      if (rect.width > 0) return rect.width;
      return Math.min(global.innerWidth || 320, 320);
    }

    function setDragging(on) {
      document.body.classList.toggle(draggingBodyClass, !!on);
    }

    function setBackdropProgress(progress, fullyOpen) {
      if (!backdrop) return;
      var p = Math.min(1, Math.max(0, progress));
      if (p > 0) {
        backdrop.classList.remove('hidden');
        backdrop.classList.add('is-drag-preview');
        backdrop.style.opacity = String(p);
        if (fullyOpen) {
          backdrop.classList.add('is-visible');
          backdrop.setAttribute('aria-hidden', 'false');
        }
      } else {
        backdrop.style.opacity = '0';
        backdrop.classList.remove('is-drag-preview', 'is-visible');
        if (!isOpen()) {
          backdrop.classList.add('hidden');
          backdrop.setAttribute('aria-hidden', 'true');
        }
      }
    }

    function clearInlineStyles() {
      panel.style.transform = '';
      panel.style.transition = '';
      if (backdrop) {
        backdrop.style.opacity = '';
        backdrop.classList.remove('is-drag-preview');
      }
    }

    function waitTransitionEnd(callback) {
      function onEnd(e) {
        if (e.target !== panel || e.propertyName !== 'transform') return;
        panel.removeEventListener('transitionend', onEnd);
        clearInlineStyles();
        setDragging(false);
        if (callback) callback();
      }
      panel.addEventListener('transitionend', onEnd);
    }

    function detachDocumentTouch() {
      document.removeEventListener('touchmove', onTouchMove);
      document.removeEventListener('touchend', onTouchEnd);
      document.removeEventListener('touchcancel', onTouchCancel);
    }

    function onTouchMove(e) {
      if (!gesture || !e.touches || !e.touches.length) return;

      var touch = e.touches[0];
      var deltaX = touch.clientX - gesture.startX;
      var deltaY = touch.clientY - gesture.startY;

      if (!gesture.locked) {
        if (Math.abs(deltaX) < 6 && Math.abs(deltaY) < 6) return;
        if (Math.abs(deltaY) >= Math.abs(deltaX)) {
          var eraAbrir = gesture.mode === 'open';
          detachDocumentTouch();
          gesture = null;
          setDragging(false);
          if (eraAbrir) {
            clearInlineStyles();
            setBackdropProgress(0, false);
          }
          return;
        }
        if (gesture.mode === 'open') {
          var openWrongDir = isRightDrawer ? deltaX >= 0 : deltaX <= 0;
          if (openWrongDir) {
            detachDocumentTouch();
            gesture = null;
            setDragging(false);
            clearInlineStyles();
            setBackdropProgress(0, false);
            return;
          }
        }
        if (gesture.mode === 'close') {
          var closeWrongDir = isRightDrawer ? deltaX <= 0 : deltaX >= 0;
          if (closeWrongDir) {
            return;
          }
        }
        gesture.locked = true;
        gesture.drawerWidth = getDrawerWidth();
        if (gesture.mode === 'close') {
          setDragging(true);
        }
      }

      if (gesture.mode === 'open') {
        if (isRightDrawer ? deltaX >= 0 : deltaX <= 0) return;
        if (e.cancelable) e.preventDefault();
        panel.style.transition = 'none';
        var txOpen = isRightDrawer
          ? Math.max(0, gesture.drawerWidth + deltaX)
          : Math.min(0, -gesture.drawerWidth + deltaX);
        panel.style.transform = 'translate3d(' + txOpen + 'px, 0, 0)';
        setBackdropProgress(openProgressFromTx(gesture.drawerWidth, txOpen), false);
      } else if (gesture.mode === 'close') {
        if (isRightDrawer ? deltaX <= 0 : deltaX >= 0) return;
        if (e.cancelable) e.preventDefault();
        panel.style.transition = 'none';
        var txClose = isRightDrawer ? Math.max(0, deltaX) : Math.min(0, deltaX);
        panel.style.transform = 'translate3d(' + txClose + 'px, 0, 0)';
        setBackdropProgress(openProgressFromTx(gesture.drawerWidth, txClose), false);
      }
    }

    function onTouchEnd(e) {
      if (!gesture) return;
      var active = gesture;
      gesture = null;
      detachDocumentTouch();

      if (!e.changedTouches || !e.changedTouches.length) {
        setDragging(false);
        return;
      }

      var touch = e.changedTouches[0];
      var deltaX = touch.clientX - active.startX;
      var elapsed = Math.max(1, Date.now() - active.startTime);
      var velocityX = deltaX / elapsed;

      panel.style.transition = DRAWER_DRAG_TRANSITION;

      if (active.mode === 'open') {
        if (!active.locked) {
          setDragging(false);
          clearInlineStyles();
          setBackdropProgress(0, false);
          return;
        }
        var openedRatio = isRightDrawer
          ? -deltaX / active.drawerWidth
          : deltaX / active.drawerWidth;
        var abrirCompleto = isRightDrawer
          ? openedRatio >= openThreshold || velocityX < -DRAWER_DRAG_VELOCITY
          : openedRatio >= openThreshold || velocityX > DRAWER_DRAG_VELOCITY;
        if (abrirCompleto) {
          onOpen();
          panel.style.transform = transformOpen();
          setBackdropProgress(1, true);
          waitTransitionEnd(null);
        } else {
          panel.style.transform = transformClosed(active.drawerWidth);
          setBackdropProgress(0, false);
          waitTransitionEnd(null);
        }
        return;
      }

      if (active.mode === 'close') {
        var closeCancelDir = isRightDrawer ? deltaX <= 0 : deltaX >= 0;
        if (!active.locked || closeCancelDir) {
          panel.style.transform = transformOpen();
          setBackdropProgress(1, true);
          waitTransitionEnd(null);
          return;
        }
        var closedRatio = isRightDrawer
          ? deltaX / active.drawerWidth
          : Math.abs(deltaX) / active.drawerWidth;
        var fecharCompleto = isRightDrawer
          ? closedRatio >= closeThreshold || velocityX > DRAWER_DRAG_VELOCITY
          : closedRatio >= closeThreshold || velocityX < -DRAWER_DRAG_VELOCITY;
        if (fecharCompleto) {
          panel.style.transform = transformClosed(active.drawerWidth);
          setBackdropProgress(0, false);
          waitTransitionEnd(onClose);
        } else {
          panel.style.transform = transformOpen();
          setBackdropProgress(1, true);
          waitTransitionEnd(null);
        }
      }
    }

    function onTouchCancel() {
      if (!gesture) return;
      var active = gesture;
      gesture = null;
      detachDocumentTouch();
      panel.style.transition = DRAWER_DRAG_TRANSITION;

      if (active.mode === 'open') {
        panel.style.transform = transformClosed(active.drawerWidth);
        setBackdropProgress(0, false);
        waitTransitionEnd(null);
      } else {
        panel.style.transform = transformOpen();
        setBackdropProgress(1, true);
        waitTransitionEnd(null);
      }
    }

    function attachDocumentTouch() {
      document.addEventListener('touchmove', onTouchMove, { passive: false });
      document.addEventListener('touchend', onTouchEnd, { passive: true });
      document.addEventListener('touchcancel', onTouchCancel, { passive: true });
    }

    function beginOpenGesture(touch) {
      if (onOpenGestureStart) {
        try {
          onOpenGestureStart();
        } catch (err) {
          /* ignore */
        }
      }
      setDragging(true);
      panel.style.transition = 'none';
      void panel.offsetWidth;
      var largura = getDrawerWidth();
      gesture = {
        mode: 'open',
        startX: touch.clientX,
        startY: touch.clientY,
        startTime: Date.now(),
        drawerWidth: largura,
        locked: false
      };
      panel.classList.remove('hidden');
      panel.style.transform = transformClosed(largura);
      setBackdropProgress(0, false);
      attachDocumentTouch();
    }

    function beginCloseGesture(touch) {
      gesture = {
        mode: 'close',
        startX: touch.clientX,
        startY: touch.clientY,
        startTime: Date.now(),
        drawerWidth: getDrawerWidth(),
        locked: false
      };
      attachDocumentTouch();
    }

    document.addEventListener(
      'touchstart',
      function (e) {
        if (gesture) return;
        // Não processa abertura se a gaveta deste drawer já está aberta
        if (isOpen()) return;
        // Trava global anti ghost click do iOS (pós-swipe)
        if (document.body.dataset.iosBloqueioClique === 'true') return;
        if (
          global.MeninoAlhoSwipeDismiss &&
          typeof global.MeninoAlhoSwipeDismiss.isAberturaBloqueada === 'function' &&
          global.MeninoAlhoSwipeDismiss.isAberturaBloqueada(panel)
        ) {
          return;
        }
        if (panel.dataset.bloquearAbertura === 'true') return;
        // Outra gaveta swipe-dismiss em cooldown de reabertura (anti ghost click)
        if (document.querySelector('[data-swipe-dismiss][data-bloquear-abertura="true"]')) {
          return;
        }
        if (document.body.classList.contains('swipe-dismiss-dragging')) return;
        if (!canStartOpenGesture()) return;
        if (shouldIgnoreTouchTarget(e.target)) return;
        // Toque iniciado dentro de uma gaveta aberta: nunca abre por gesto de borda
        if (
          e.target &&
          e.target.closest &&
          e.target.closest('[data-swipe-dismiss]:not(.hidden):not(.translate-x-full)')
        ) {
          return;
        }
        if (!e.touches || !e.touches.length) return;
        var touch = e.touches[0];
        if (isRightDrawer) {
          if (touch.clientX < global.innerWidth - edgeWidth) return;
        } else if (touch.clientX > edgeWidth) {
          return;
        }
        beginOpenGesture(touch);
      },
      { passive: true }
    );

    function onCloseTouchStart(e) {
      if (gesture) return;
      if (!isOpen()) return;
      if (alvoIgnoraSwipe(e.target)) return;
      if (!e.touches || !e.touches.length) return;
      beginCloseGesture(e.touches[0]);
    }

    if (options.closeGesture === false) return;
    panel.addEventListener('touchstart', onCloseTouchStart, { passive: true });
    if (backdrop) {
      backdrop.addEventListener('touchstart', onCloseTouchStart, { passive: true });
    }
  }

  global.MeninoAlhoNavigation = {
    navigate: navigate,
    FALLBACK_MS: FALLBACK_MS,
    setupDrawerInteractiveDrag: setupDrawerInteractiveDrag
  };

  if (!reducedMotion) {
    document.addEventListener('click', onDocumentClick, true);
  }
})(typeof window !== 'undefined' ? window : globalThis);
