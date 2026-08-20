/**
 * Ponte Capacitor ↔ Live Activities (Dynamic Island).
 * Delega para static/js/dynamic_island.js quando disponível;
 * mantém API MeninoAlhoDynamicIsland para a logística.
 */
(function (global) {
    'use strict';

    function ensureHelpers() {
        if (typeof global.iniciarLiveActivity === 'function') return true;
        // Carrega dynamic_island.js dinamicamente se ainda não existir
        return false;
    }

    function isCapacitorIOS() {
        try {
            var Cap = global.Capacitor;
            if (!Cap || typeof Cap.isNativePlatform !== 'function') return false;
            if (!Cap.isNativePlatform()) return false;
            var platform = typeof Cap.getPlatform === 'function' ? Cap.getPlatform() : '';
            return platform === 'ios' || Cap.isNativePlatform();
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

    async function startDeliveryActivity(options) {
        options = options || {};
        if (typeof global.iniciarLiveActivity === 'function') {
            return global.iniciarLiveActivity(
                options.numeroCarga,
                options.totalCaixas,
                options.proximoCliente
            );
        }
        if (!isCapacitorIOS()) {
            return { started: false, skipped: true, reason: 'not_ios' };
        }
        var plugin = getPlugin();
        if (!plugin) return { started: false, skipped: true, reason: 'no_plugin' };
        var fn = plugin.startDeliveryActivity || plugin.start;
        if (typeof fn !== 'function') return { started: false, skipped: true, reason: 'no_method' };
        try {
            return await fn.call(plugin, {
                numeroCarga: String(options.numeroCarga || '—'),
                totalCaixas: Number(options.totalCaixas || 0),
                caixasEntregues: Number(options.caixasEntregues || 0),
                proximoCliente: String(options.proximoCliente || '—'),
                status: String(options.status || 'Em rota')
            });
        } catch (err) {
            return { started: false, skipped: true, reason: 'plugin_error' };
        }
    }

    async function updateDeliveryActivity(options) {
        options = options || {};
        if (typeof global.atualizarLiveActivity === 'function') {
            return global.atualizarLiveActivity(
                options.caixasEntregues,
                options.totalCaixas,
                options.proximoCliente,
                options.status
            );
        }
        if (!isCapacitorIOS()) {
            return { updated: false, skipped: true, reason: 'not_ios' };
        }
        var plugin = getPlugin();
        if (!plugin) return { updated: false, skipped: true, reason: 'no_plugin' };
        var fn = plugin.updateDeliveryActivity || plugin.update;
        if (typeof fn !== 'function') return { updated: false, skipped: true, reason: 'no_method' };
        try {
            return await fn.call(plugin, options);
        } catch (err) {
            return { updated: false, skipped: true, reason: 'plugin_error' };
        }
    }

    async function stopDeliveryActivity(options) {
        options = options || {};
        if (typeof global.encerrarLiveActivity === 'function' && !options.status) {
            return global.encerrarLiveActivity();
        }
        if (!isCapacitorIOS()) {
            return { stopped: false, skipped: true, reason: 'not_ios' };
        }
        var plugin = getPlugin();
        if (!plugin) return { stopped: false, skipped: true, reason: 'no_plugin' };
        var fn = plugin.endDeliveryActivity || plugin.stop;
        if (typeof fn !== 'function') return { stopped: false, skipped: true, reason: 'no_method' };
        try {
            return await fn.call(plugin, {
                status: options.status || 'Concluído',
                caixasEntregues: options.caixasEntregues,
                totalCaixas: options.totalCaixas,
                dismissalSeconds: options.dismissalSeconds != null ? options.dismissalSeconds : 5
            });
        } catch (err) {
            return { stopped: false, skipped: true, reason: 'plugin_error' };
        }
    }

    function gerarNumeroCarga() {
        var d = new Date();
        var y = d.getFullYear();
        var m = String(d.getMonth() + 1).padStart(2, '0');
        var day = String(d.getDate()).padStart(2, '0');
        return 'CG-' + y + '-' + m + day;
    }

    ensureHelpers();

    global.MeninoAlhoDynamicIsland = {
        isAvailable: isCapacitorIOS,
        startDeliveryActivity: startDeliveryActivity,
        updateDeliveryActivity: updateDeliveryActivity,
        stopDeliveryActivity: stopDeliveryActivity,
        lerEstado: function () { return null; },
        gerarNumeroCarga: gerarNumeroCarga
    };
})(typeof window !== 'undefined' ? window : this);
