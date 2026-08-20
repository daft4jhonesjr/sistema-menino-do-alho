/**
 * Helper Dynamic Island / Live Activities — Menino do Alho
 * Seguro na Web: no-op silencioso fora do Capacitor nativo.
 *
 * API pública:
 *   iniciarLiveActivity(numeroCarga, totalCaixas, proximoCliente)
 *   atualizarLiveActivity(caixasEntregues, totalCaixas, proximoCliente, status)
 *   encerrarLiveActivity()
 */
(function (global) {
    'use strict';

    function isNativePlatform() {
        try {
            var Cap = global.Capacitor;
            if (!Cap) return false;
            if (typeof Cap.isNativePlatform === 'function') {
                return !!Cap.isNativePlatform();
            }
            return false;
        } catch (err) {
            return false;
        }
    }

    function getPlugin() {
        try {
            var Cap = global.Capacitor;
            if (!Cap) return null;
            if (Cap.Plugins && Cap.Plugins.DeliveryActivity) {
                return Cap.Plugins.DeliveryActivity;
            }
            if (typeof Cap.registerPlugin === 'function') {
                return Cap.registerPlugin('DeliveryActivity');
            }
        } catch (err) {
            /* silencioso */
        }
        return null;
    }

    function callPlugin(methodNames, payload) {
        if (!isNativePlatform()) {
            return Promise.resolve({ skipped: true, reason: 'not_native' });
        }

        var plugin = getPlugin();
        if (!plugin) {
            return Promise.resolve({ skipped: true, reason: 'no_plugin' });
        }

        var names = Array.isArray(methodNames) ? methodNames : [methodNames];
        var fn = null;
        for (var i = 0; i < names.length; i++) {
            if (typeof plugin[names[i]] === 'function') {
                fn = plugin[names[i]].bind(plugin);
                break;
            }
        }

        if (!fn) {
            return Promise.resolve({ skipped: true, reason: 'no_method' });
        }

        return Promise.resolve()
            .then(function () { return fn(payload || {}); })
            .catch(function () {
                // Sem console.error — evita ruído no Safari/WebView quando plugin falha.
                return { skipped: true, reason: 'plugin_error' };
            });
    }

    function iniciarLiveActivity(numeroCarga, totalCaixas, proximoCliente) {
        return callPlugin(
            ['startDeliveryActivity', 'start'],
            {
                numeroCarga: String(numeroCarga || '—'),
                totalCaixas: Number(totalCaixas) || 0,
                caixasEntregues: 0,
                proximoCliente: String(proximoCliente || '—'),
                status: 'Em rota'
            }
        );
    }

    function atualizarLiveActivity(caixasEntregues, totalCaixas, proximoCliente, status) {
        return callPlugin(
            ['updateDeliveryActivity', 'update'],
            {
                caixasEntregues: Number(caixasEntregues) || 0,
                totalCaixas: Number(totalCaixas) || 0,
                proximoCliente: String(proximoCliente || '—'),
                status: String(status || 'Em rota')
            }
        );
    }

    function encerrarLiveActivity() {
        return callPlugin(
            ['endDeliveryActivity', 'stop'],
            {
                status: 'Concluído',
                dismissalSeconds: 5
            }
        );
    }

    global.iniciarLiveActivity = iniciarLiveActivity;
    global.atualizarLiveActivity = atualizarLiveActivity;
    global.encerrarLiveActivity = encerrarLiveActivity;

    // Compatibilidade com a tela de logística existente
    global.MeninoAlhoDynamicIsland = {
        isAvailable: isNativePlatform,
        startDeliveryActivity: function (options) {
            options = options || {};
            return iniciarLiveActivity(
                options.numeroCarga,
                options.totalCaixas,
                options.proximoCliente
            );
        },
        updateDeliveryActivity: function (options) {
            options = options || {};
            return atualizarLiveActivity(
                options.caixasEntregues,
                options.totalCaixas,
                options.proximoCliente,
                options.status
            );
        },
        stopDeliveryActivity: function (options) {
            options = options || {};
            return callPlugin(
                ['endDeliveryActivity', 'stop'],
                {
                    status: options.status || 'Concluído',
                    caixasEntregues: options.caixasEntregues,
                    totalCaixas: options.totalCaixas,
                    dismissalSeconds: options.dismissalSeconds != null ? options.dismissalSeconds : 5
                }
            );
        },
        lerEstado: function () { return null; },
        gerarNumeroCarga: function () {
            var d = new Date();
            var y = d.getFullYear();
            var m = String(d.getMonth() + 1).padStart(2, '0');
            var day = String(d.getDate()).padStart(2, '0');
            return 'CG-' + y + '-' + m + day;
        }
    };
})(typeof window !== 'undefined' ? window : this);
