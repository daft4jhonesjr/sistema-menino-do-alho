/**
 * Drag & Drop estilo iOS — PointerEvents + háptica Capacitor.
 * Expõe: window.dispararHapticFeedback, window.iniciarDragDropIOS
 */
(function (global) {
    'use strict';

    var PRESS_DELAY_MS = 150;
    var MOVE_CANCEL_PX = 10;
    var ACTIVE_INSTANCES = [];

    function isCapacitorNative() {
        try {
            var Cap = global.Capacitor;
            if (!Cap) return false;
            if (typeof Cap.isNativePlatform === 'function') {
                return !!Cap.isNativePlatform();
            }
            return !!(Cap.isNative || Cap.platform === 'ios' || Cap.platform === 'android');
        } catch (err) {
            return false;
        }
    }

    function getHapticsPlugin() {
        try {
            var Cap = global.Capacitor;
            if (Cap && Cap.Plugins && Cap.Plugins.Haptics) return Cap.Plugins.Haptics;
            if (global.CapacitorHaptics) return global.CapacitorHaptics;
        } catch (err) { /* ignore */ }
        return null;
    }

    /**
     * Feedback tátil nativo (Capacitor) ou fallback vibrate.
     * @param {'medium'|'light'|'heavy'|'soft'} [tipo='medium']
     */
    function dispararHapticFeedback(tipo) {
        tipo = (tipo || 'medium').toLowerCase();
        var styleMap = {
            light: 'LIGHT',
            medium: 'MEDIUM',
            heavy: 'HEAVY',
            soft: 'SOFT'
        };
        var style = styleMap[tipo] || 'MEDIUM';

        try {
            if (isCapacitorNative()) {
                var Haptics = getHapticsPlugin();
                if (Haptics && typeof Haptics.impact === 'function') {
                    var result = Haptics.impact({ style: style });
                    if (result && typeof result.catch === 'function') {
                        result.catch(function () { /* silencioso */ });
                    }
                    return;
                }
            }
        } catch (err) { /* fallback abaixo */ }

        try {
            if (navigator.vibrate) {
                var ms = tipo === 'light' ? 10 : (tipo === 'heavy' ? 25 : 15);
                navigator.vibrate(ms);
            }
        } catch (err2) { /* sem suporte */ }
    }

    function closestItem(el, container, itemSelector) {
        if (!el || !container) return null;
        var node = el;
        while (node && node !== container) {
            if (node.nodeType === 1 && node.matches && node.matches(itemSelector)) {
                return node;
            }
            node = node.parentNode;
        }
        return null;
    }

    function getDraggableItems(container, itemSelector) {
        return Array.prototype.slice.call(container.querySelectorAll(itemSelector)).filter(function (el) {
            return el.parentNode === container || container.contains(el);
        }).filter(function (el) {
            // apenas filhos diretos que batem o seletor, quando possível
            return el.parentElement === container;
        });
    }

    function itemId(el, idAttr) {
        if (!el) return null;
        var id = el.getAttribute(idAttr);
        if (id != null && id !== '') return id;
        if (el.id) return el.id;
        return null;
    }

    function collectOrder(container, itemSelector, idAttr) {
        return getDraggableItems(container, itemSelector).map(function (el) {
            return itemId(el, idAttr);
        }).filter(function (id) {
            return id != null && id !== '';
        });
    }

    function applySavedOrder(container, orderIds, itemSelector, idAttr) {
        if (!container || !orderIds || !orderIds.length) return;
        orderIds.forEach(function (id) {
            var el = null;
            var items = getDraggableItems(container, itemSelector);
            for (var i = 0; i < items.length; i++) {
                if (itemId(items[i], idAttr) === String(id)) {
                    el = items[i];
                    break;
                }
            }
            if (el) container.appendChild(el);
        });
    }

    /**
     * @param {string|Element} containerSelector
     * @param {function(string[]):void} [onReorderCallback]
     * @param {object} [options]
     * @returns {{destroy: function, refresh: function, getOrder: function}|null}
     */
    function iniciarDragDropIOS(containerSelector, onReorderCallback, options) {
        options = options || {};
        var container = typeof containerSelector === 'string'
            ? document.querySelector(containerSelector)
            : containerSelector;
        if (!container) return null;

        var itemSelector = options.itemSelector || '.ios-draggable-item';
        var idAttr = options.idAttribute || 'data-id';
        var pressDelay = options.pressDelay != null ? options.pressDelay : PRESS_DELAY_MS;
        var handleSelector = options.handleSelector || null;
        var filterSelector = options.filterSelector || 'input, button, a, select, textarea, label, .no-drag';
        var placeholderClass = options.placeholderClass || 'ios-drag-placeholder';
        var draggingClass = options.draggingClass || 'ios-dragging';
        var disabled = !!options.disabled;

        // Marca itens existentes
        function decorateItems() {
            getDraggableItems(container, itemSelector).forEach(function (el) {
                el.classList.add('ios-draggable-item');
                if (!el.hasAttribute('data-ios-dnd')) {
                    el.setAttribute('data-ios-dnd', '1');
                }
            });
        }
        decorateItems();

        var state = {
            active: false,
            armed: false,
            item: null,
            placeholder: null,
            startX: 0,
            startY: 0,
            pointerId: null,
            delayTimer: null,
            offsetY: 0,
            origParent: null,
            origNext: null
        };

        function clearDelay() {
            if (state.delayTimer) {
                clearTimeout(state.delayTimer);
                state.delayTimer = null;
            }
        }

        function cleanupDragClasses() {
            if (state.item) {
                state.item.classList.remove(draggingClass);
                state.item.style.position = '';
                state.item.style.left = '';
                state.item.style.top = '';
                state.item.style.width = '';
                state.item.style.zIndex = '';
                state.item.style.pointerEvents = '';
                state.item.style.transition = '';
            }
            if (state.placeholder && state.placeholder.parentNode) {
                state.placeholder.parentNode.removeChild(state.placeholder);
            }
            state.placeholder = null;
        }

        function resetState() {
            clearDelay();
            cleanupDragClasses();
            state.active = false;
            state.armed = false;
            state.item = null;
            state.pointerId = null;
            state.origParent = null;
            state.origNext = null;
        }

        function createPlaceholder(fromEl) {
            var ph = document.createElement(fromEl.tagName === 'TR' ? 'tr' : 'div');
            ph.className = placeholderClass;
            if (fromEl.tagName === 'TR') {
                var colCount = fromEl.children.length || 1;
                var td = document.createElement('td');
                td.colSpan = colCount;
                td.style.height = (fromEl.getBoundingClientRect().height || 48) + 'px';
                td.innerHTML = '&nbsp;';
                ph.appendChild(td);
            } else {
                var rect = fromEl.getBoundingClientRect();
                ph.style.height = Math.max(rect.height, 40) + 'px';
                ph.style.width = '100%';
            }
            return ph;
        }

        function getItemUnderPoint(x, y) {
            var el = document.elementFromPoint(x, y);
            var item = closestItem(el, container, itemSelector);
            if (item) return item;
            // Sobre irmão não-arrastável (ex.: tr.pedido-detalhes) → usa o item anterior
            var node = el;
            while (node && node !== container) {
                if (node.parentNode === container) {
                    var prev = node.previousElementSibling;
                    while (prev) {
                        if (prev.matches && prev.matches(itemSelector) && prev !== state.item) {
                            return prev;
                        }
                        prev = prev.previousElementSibling;
                    }
                    break;
                }
                node = node.parentNode;
            }
            return null;
        }

        function movePlaceholderBefore(target) {
            if (!state.placeholder || !target || target === state.item) return;
            var items = getDraggableItems(container, itemSelector);
            var targetIndex = items.indexOf(target);
            var phParent = state.placeholder.parentNode;
            var phIndex = phParent ? Array.prototype.indexOf.call(phParent.children, state.placeholder) : -1;

            if (targetIndex < 0) return;

            // Heurística: se o ponteiro está na metade inferior, coloca depois
            var rect = target.getBoundingClientRect();
            var midY = rect.top + rect.height / 2;
            var after = state._lastY > midY;

            if (after) {
                if (target.nextSibling !== state.placeholder) {
                    container.insertBefore(state.placeholder, target.nextSibling);
                }
            } else {
                if (target !== state.placeholder.nextSibling) {
                    container.insertBefore(state.placeholder, target);
                }
            }
            // evita inserir placeholder "em cima" do item flutuante se ainda estiver no DOM
            if (state.item && state.item.parentNode === container) {
                // item já saiu do fluxo visual via position fixed
            }
            void phIndex;
        }

        function liftItem(item, clientX, clientY) {
            state.active = true;
            state.item = item;
            state.origParent = item.parentNode;
            state.origNext = item.nextSibling;

            var rect = item.getBoundingClientRect();
            state.offsetY = clientY - rect.top;
            state.offsetX = clientX - rect.left;

            state.placeholder = createPlaceholder(item);
            item.parentNode.insertBefore(state.placeholder, item);

            item.classList.add(draggingClass);
            item.style.transition = 'none';
            item.style.position = 'fixed';
            item.style.left = rect.left + 'px';
            item.style.top = rect.top + 'px';
            item.style.width = rect.width + 'px';
            item.style.zIndex = '1000';
            item.style.pointerEvents = 'none';

            dispararHapticFeedback('medium');
        }

        function onPointerDown(e) {
            if (disabled || state.active || state.armed) return;
            if (e.button != null && e.button !== 0) return;

            var target = e.target;
            if (target.closest && filterSelector) {
                var filtered = target.closest(filterSelector);
                if (filtered && container.contains(filtered)) {
                    // permite drag pelo handle mesmo se o handle estiver fora do filter
                    if (!(handleSelector && target.closest(handleSelector))) {
                        return;
                    }
                }
            }

            var item = closestItem(target, container, itemSelector);
            if (!item || !container.contains(item)) return;

            if (handleSelector) {
                if (!target.closest || !target.closest(handleSelector)) return;
            }

            state.armed = true;
            state.item = item;
            state.startX = e.clientX;
            state.startY = e.clientY;
            state.pointerId = e.pointerId;
            state._lastY = e.clientY;

            try {
                container.setPointerCapture && container.setPointerCapture(e.pointerId);
            } catch (err) { /* ignore */ }

            clearDelay();
            state.delayTimer = setTimeout(function () {
                state.delayTimer = null;
                if (!state.armed || state.active) return;
                // cancelou se o dedo andou demais (scroll)
                liftItem(item, state.startX, state.startY);
            }, pressDelay);
        }

        function onPointerMove(e) {
            if (!state.armed && !state.active) return;
            if (state.pointerId != null && e.pointerId !== state.pointerId) return;

            state._lastY = e.clientY;

            if (state.armed && !state.active) {
                var dx = e.clientX - state.startX;
                var dy = e.clientY - state.startY;
                if (Math.abs(dx) > MOVE_CANCEL_PX || Math.abs(dy) > MOVE_CANCEL_PX) {
                    // movimento antes do delay = scroll / gesto normal
                    resetState();
                }
                return;
            }

            if (!state.active || !state.item) return;
            e.preventDefault();

            var top = e.clientY - state.offsetY;
            state.item.style.top = top + 'px';
            state.item.style.left = (e.clientX - state.offsetX) + 'px';

            var under = getItemUnderPoint(e.clientX, e.clientY);
            if (under && under !== state.item) {
                movePlaceholderBefore(under);
            }
        }

        function onPointerUp(e) {
            if (state.pointerId != null && e.pointerId !== state.pointerId) return;

            if (state.armed && !state.active) {
                resetState();
                return;
            }

            if (!state.active || !state.item) {
                resetState();
                return;
            }

            var item = state.item;
            var ph = state.placeholder;

            if (ph && ph.parentNode) {
                ph.parentNode.insertBefore(item, ph);
                ph.parentNode.removeChild(ph);
            }

            item.classList.remove(draggingClass);
            item.style.position = '';
            item.style.left = '';
            item.style.top = '';
            item.style.width = '';
            item.style.zIndex = '';
            item.style.pointerEvents = '';
            item.style.transition = '';

            state.placeholder = null;
            state.active = false;
            state.armed = false;
            state.item = null;
            state.pointerId = null;

            dispararHapticFeedback('light');

            var novaOrdem = collectOrder(container, itemSelector, idAttr);
            if (typeof onReorderCallback === 'function') {
                try {
                    onReorderCallback(novaOrdem);
                } catch (err) {
                    console.warn('[ios-dnd] callback erro:', err);
                }
            }
        }

        function onPointerCancel() {
            if (state.active && state.item && state.origParent) {
                if (state.origNext && state.origNext.parentNode === state.origParent) {
                    state.origParent.insertBefore(state.item, state.origNext);
                } else {
                    state.origParent.appendChild(state.item);
                }
            }
            resetState();
        }

        // touch-action no container já está nos itens via CSS
        container.addEventListener('pointerdown', onPointerDown, { passive: true });
        container.addEventListener('pointermove', onPointerMove, { passive: false });
        container.addEventListener('pointerup', onPointerUp, { passive: true });
        container.addEventListener('pointercancel', onPointerCancel, { passive: true });

        var api = {
            destroy: function () {
                clearDelay();
                resetState();
                container.removeEventListener('pointerdown', onPointerDown);
                container.removeEventListener('pointermove', onPointerMove);
                container.removeEventListener('pointerup', onPointerUp);
                container.removeEventListener('pointercancel', onPointerCancel);
                var idx = ACTIVE_INSTANCES.indexOf(api);
                if (idx >= 0) ACTIVE_INSTANCES.splice(idx, 1);
            },
            refresh: function () {
                decorateItems();
            },
            getOrder: function () {
                return collectOrder(container, itemSelector, idAttr);
            },
            applyOrder: function (ids) {
                applySavedOrder(container, ids, itemSelector, idAttr);
            },
            container: container
        };

        ACTIVE_INSTANCES.push(api);
        return api;
    }

    global.dispararHapticFeedback = dispararHapticFeedback;
    global.iniciarDragDropIOS = iniciarDragDropIOS;
    global._iosDragDropApplyOrder = applySavedOrder;
})(typeof window !== 'undefined' ? window : this);
