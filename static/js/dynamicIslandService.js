/**
 * Ponte Capacitor ↔ Live Activities (Dynamic Island).
 * Seguro na Web: no-op quando não há Capacitor/iOS.
 */
(function (global) {
    'use strict';

    var STORAGE_KEY = 'menino_alho_live_activity';

    function isCapacitorIOS() {
        try {
            var Cap = global.Capacitor;
            if (!Cap || typeof Cap.isNativePlatform !== 'function') return false;
            if (!Cap.isNativePlatform()) return false;
            var platform = typeof Cap.getPlatform === 'function' ? Cap.getPlatform() : '';
            return platform === 'ios';
        } catch (err) {
            return false;
        }
    }

    function getPlugin() {
        var Cap = global.Capacitor;
        if (!Cap) return null;
        if (Cap.Plugins && Cap.Plugins.DeliveryActivity) {
            return Cap.Plugins.DeliveryActivity;
        }
        if (typeof Cap.registerPlugin === 'function') {
            return Cap.registerPlugin('DeliveryActivity');
        }
        return null;
    }

    function calcProgresso(entregues, total) {
        if (!total || total <= 0) return 0;
        return Math.min(entregues / total, 1);
    }

    function salvarEstado(estado) {
        try {
            global.sessionStorage.setItem(STORAGE_KEY, JSON.stringify(estado));
        } catch (err) {}
    }

    function lerEstado() {
        try {
            var raw = global.sessionStorage.getItem(STORAGE_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (err) {
            return null;
        }
    }

    function limparEstado() {
        try {
            global.sessionStorage.removeItem(STORAGE_KEY);
        } catch (err) {}
    }

    async function startDeliveryActivity(options) {
        options = options || {};
        if (!isCapacitorIOS()) {
            return { started: false, skipped: true, reason: 'not_ios' };
        }
        var plugin = getPlugin();
        if (!plugin || typeof plugin.start !== 'function') {
            console.warn('[DynamicIsland] Plugin DeliveryActivity indisponível.');
            return { started: false, skipped: true, reason: 'no_plugin' };
        }

        var totalCaixas = Number(options.totalCaixas || 0);
        var caixasEntregues = Number(options.caixasEntregues || 0);
        var payload = {
            numeroCarga: String(options.numeroCarga || gerarNumeroCarga()),
            placaVeiculo: String(options.placaVeiculo || ''),
            proximoCliente: String(options.proximoCliente || '—'),
            totalCaixas: totalCaixas,
            caixasEntregues: caixasEntregues,
            status: String(options.status || 'Em rota'),
            progresso: calcProgresso(caixasEntregues, totalCaixas)
        };

        var result = await plugin.start(payload);
        salvarEstado(Object.assign({}, payload, { activityId: result && result.activityId }));
        return result;
    }

    async function updateDeliveryActivity(options) {
        options = options || {};
        if (!isCapacitorIOS()) {
            return { updated: false, skipped: true, reason: 'not_ios' };
        }
        var plugin = getPlugin();
        if (!plugin || typeof plugin.update !== 'function') {
            return { updated: false, skipped: true, reason: 'no_plugin' };
        }

        var estado = lerEstado() || {};
        var caixasEntregues = options.caixasEntregues != null
            ? Number(options.caixasEntregues)
            : Number(estado.caixasEntregues || 0);
        var totalCaixas = options.totalCaixas != null
            ? Number(options.totalCaixas)
            : Number(estado.totalCaixas || 0);
        var payload = {
            caixasEntregues: caixasEntregues,
            totalCaixas: totalCaixas,
            proximoCliente: String(options.proximoCliente || estado.proximoCliente || '—'),
            status: String(options.status || estado.status || 'Em rota'),
            progresso: calcProgresso(caixasEntregues, totalCaixas)
        };

        var result = await plugin.update(payload);
        salvarEstado(Object.assign({}, estado, payload));
        return result;
    }

    async function stopDeliveryActivity(options) {
        options = options || {};
        if (!isCapacitorIOS()) {
            limparEstado();
            return { stopped: false, skipped: true, reason: 'not_ios' };
        }
        var plugin = getPlugin();
        if (!plugin || typeof plugin.stop !== 'function') {
            limparEstado();
            return { stopped: false, skipped: true, reason: 'no_plugin' };
        }

        var estado = lerEstado() || {};
        var payload = {
            status: String(options.status || 'Finalizado'),
            caixasEntregues: options.caixasEntregues != null
                ? Number(options.caixasEntregues)
                : Number(estado.caixasEntregues || 0),
            totalCaixas: options.totalCaixas != null
                ? Number(options.totalCaixas)
                : Number(estado.totalCaixas || 0),
            dismissalSeconds: Number(options.dismissalSeconds != null ? options.dismissalSeconds : 5)
        };

        var result = await plugin.stop(payload);
        limparEstado();
        return result;
    }

    function gerarNumeroCarga() {
        var d = new Date();
        var y = d.getFullYear();
        var m = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        return 'CG-' + y + '-' + m + day;
    }

    global.MeninoAlhoDynamicIsland = {
        isAvailable: isCapacitorIOS,
        startDeliveryActivity: startDeliveryActivity,
        updateDeliveryActivity: updateDeliveryActivity,
        stopDeliveryActivity: stopDeliveryActivity,
        lerEstado: lerEstado,
        gerarNumeroCarga: gerarNumeroCarga
    };
})(typeof window !== 'undefined' ? window : this);
