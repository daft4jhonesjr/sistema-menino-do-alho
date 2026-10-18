/**
 * Persistência de sessão para WebView (Capacitor iOS / WKWebView).
 * Complementa o cookie Flask — não substitui a autenticação server-side.
 * Inclui monitor client-side de inatividade e tempo máximo de uso contínuo.
 */
(function (global) {
    'use strict';

    var STORAGE_KEY = 'menino_alho_sessao';
    var PADRAO_INATIVIDADE_MIN = 15;
    var PADRAO_SESSAO_MAX_HORAS = 4;

    function persistirSessao(dados) {
        if (!dados) return;
        var payload = {
            user_id: dados.user_id || null,
            session_token: dados.session_token || null,
            redirect_url: dados.redirect_url || null,
            saved_at: Date.now()
        };
        try {
            global.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
        } catch (err) {
            console.warn('[Auth] localStorage indisponível:', err);
        }
        var prefs = global.Capacitor && global.Capacitor.Plugins && global.Capacitor.Plugins.Preferences;
        if (prefs && typeof prefs.set === 'function') {
            prefs.set({ key: STORAGE_KEY, value: JSON.stringify(payload) }).catch(function () {});
        }
    }

    function lerSessaoPersistida() {
        try {
            var raw = global.localStorage.getItem(STORAGE_KEY);
            return raw ? JSON.parse(raw) : null;
        } catch (err) {
            return null;
        }
    }

    function limparSessaoPersistida() {
        try {
            global.localStorage.removeItem(STORAGE_KEY);
        } catch (err) {}
        var prefs = global.Capacitor && global.Capacitor.Plugins && global.Capacitor.Plugins.Preferences;
        if (prefs && typeof prefs.remove === 'function') {
            prefs.remove({ key: STORAGE_KEY }).catch(function () {});
        }
    }

    function normalizarUrlRedirect(url) {
        var destino = (url || '/dashboard').trim();
        if (/^https?:\/\//i.test(destino)) {
            return destino;
        }
        var base = global.location.origin || '';
        return base + (destino.charAt(0) === '/' ? destino : '/' + destino);
    }

    function parseIntPositivo(val, fallback) {
        var n = parseInt(val, 10);
        if (!isFinite(n) || n <= 0) return fallback;
        return n;
    }

    function resolverConfigSessao(override) {
        var base = global.SESSION_CONFIG || {};
        var merged = override || base;
        return {
            inatividadeMin: parseIntPositivo(
                merged.inatividadeMin != null ? merged.inatividadeMin : merged.timeout_inatividade_min,
                PADRAO_INATIVIDADE_MIN
            ),
            sessaoMaxHoras: parseIntPositivo(
                merged.sessaoMaxHoras != null ? merged.sessaoMaxHoras : merged.timeout_sessao_max_horas,
                PADRAO_SESSAO_MAX_HORAS
            ),
            loginTimeMs: merged.loginTimeMs || Date.now(),
            logoutUrl: merged.logoutUrl || '/logout'
        };
    }

    function iniciarMonitorExpiracaoSessao(configOverride) {
        var cfg = resolverConfigSessao(configOverride);
        var limiteInatividadeMs = cfg.inatividadeMin * 60 * 1000;
        var limiteAbsolutoMs = cfg.sessaoMaxHoras * 60 * 60 * 1000;
        var logoutUrl = cfg.logoutUrl;
        var loginTimeMs = cfg.loginTimeMs;
        if (!loginTimeMs) loginTimeMs = Date.now();

        var tempoInativo = 0;

        function resetarInatividade() {
            tempoInativo = 0;
        }

        var eventos = ['mousemove', 'mousedown', 'keydown', 'touchstart', 'scroll', 'click'];
        eventos.forEach(function (ev) {
            global.addEventListener(ev, resetarInatividade, { passive: true });
        });

        function encerrarSessaoVisual(mensagem) {
            if (global.sessionExpiredTriggered) return;
            global.sessionExpiredTriggered = true;

            limparSessaoPersistida();

            var Swal = global.Swal;
            if (typeof Swal !== 'undefined') {
                Swal.fire({
                    title: 'Sessão Expirada',
                    text: mensagem,
                    icon: 'info',
                    background: '#1e293b',
                    color: '#f8fafc',
                    confirmButtonColor: '#10b981',
                    confirmButtonText: 'Ir para Login',
                    allowOutsideClick: false,
                    allowEscapeKey: false
                }).then(function () {
                    global.location.href = logoutUrl;
                });
            } else {
                global.alert(mensagem);
                global.location.href = logoutUrl;
            }
        }

        global.setInterval(function () {
            tempoInativo += 10000;
            if (tempoInativo >= limiteInatividadeMs) {
                encerrarSessaoVisual(
                    'Sua sessão foi encerrada por ' + cfg.inatividadeMin +
                    ' minuto(s) de inatividade.'
                );
            }
        }, 10000);

        var restanteAbsoluto = limiteAbsolutoMs - (Date.now() - loginTimeMs);
        if (restanteAbsoluto <= 0) {
            encerrarSessaoVisual(
                'Limite de ' + cfg.sessaoMaxHoras +
                ' hora(s) de uso contínuo atingido. Faça login novamente.'
            );
        } else {
            global.setTimeout(function () {
                encerrarSessaoVisual(
                    'Limite de ' + cfg.sessaoMaxHoras +
                    ' hora(s) de uso contínuo atingido. Faça login novamente.'
                );
            }, restanteAbsoluto);
        }

        if (global.fetch && !global.__meninoAlhoFetchSessionHook) {
            global.__meninoAlhoFetchSessionHook = true;
            var fetchAnterior = global.fetch.bind(global);
            global.fetch = function (input, init) {
                return fetchAnterior(input, init).then(function (response) {
                    if (response.status === 401) {
                        return response.clone().json().then(function (data) {
                            if (data && data.session_expired) {
                                encerrarSessaoVisual(data.message || 'Sua sessão expirou.');
                            }
                            return response;
                        }).catch(function () {
                            return response;
                        });
                    }
                    return response;
                });
            };
        }

        return { encerrarSessaoVisual: encerrarSessaoVisual };
    }

    global.MeninoAlhoAuth = {
        persistirSessao: persistirSessao,
        lerSessaoPersistida: lerSessaoPersistida,
        limparSessaoPersistida: limparSessaoPersistida,
        normalizarUrlRedirect: normalizarUrlRedirect,
        iniciarMonitorExpiracaoSessao: iniciarMonitorExpiracaoSessao,
        PADRAO_INATIVIDADE_MIN: PADRAO_INATIVIDADE_MIN,
        PADRAO_SESSAO_MAX_HORAS: PADRAO_SESSAO_MAX_HORAS
    };
})(typeof window !== 'undefined' ? window : this);
