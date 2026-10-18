/**
 * Deep links do widget iOS (scheme meninodoalho:) para rotas internas.
 *
 * O app Capacitor carrega o Flask remoto, então o plugin nativo App
 * é usado pelo bridge (`Capacitor.Plugins.App`), o mesmo contrato de
 * `import { App } from '@capacitor/app'`.
 *
 * Em `meninodoalho://vendas/novo` o host é `vendas` e o pathname é
 * `/novo`. A rota interna junta os dois e preserva a query (`?acao=novo`).
 * Se a sessão estiver expirada, o Flask redireciona para `/login?next=`.
 */
(function (global) {
    'use strict';

    // Depuração global no WebView: identifica script/linha do "JS Eval error".
    if (typeof global.addEventListener === 'function' && !global.__meninodoalhoJsErrorHook) {
        global.__meninodoalhoJsErrorHook = true;
        global.addEventListener('error', function (event) {
            console.error(
                'JS Global Error:',
                event && event.message,
                'em',
                event && event.filename,
                'linha:',
                event && event.lineno
            );
        });
        global.addEventListener('unhandledrejection', function (event) {
            var reason = event && event.reason;
            console.error('JS Unhandled Rejection:', reason);
        });
    }

    var CHAVE_LAUNCH = 'meninodoalho_deep_link_launch';
    var listenerRegistrado = false;

    function rotaDoDeepLink(dataUrl) {
        var url = new URL(dataUrl);
        if (url.protocol !== 'meninodoalho:') return '';

        var host = url.hostname || '';
        var path = url.pathname || '';
        var rota;

        if (host) {
            rota = '/' + host;
            if (path && path !== '/') {
                rota += path.charAt(0) === '/' ? path : '/' + path;
            }
        } else {
            rota = path || ('/' + (url.host || '') + path);
        }

        if (!rota || rota.charAt(0) !== '/' || rota.indexOf('//') === 0) return '';
        if (url.search) rota += url.search;
        if (url.hash) rota += url.hash;
        return rota;
    }

    function mesmoDestino(atual, rota) {
        if (!atual || !rota) return false;
        if (atual === rota) return true;
        try {
            var base = (global.location && global.location.origin) || 'https://sistema-menino-do-alho.onrender.com';
            var a = new URL(atual, base);
            var b = new URL(rota, base);
            return a.pathname === b.pathname && a.search === b.search && a.hash === b.hash;
        } catch (err) {
            return false;
        }
    }

    function navegar(data) {
        try {
            if (!data || !data.url) return;
            var url = new URL(data.url);
            if (url.protocol !== 'meninodoalho:') return;

            var rota = rotaDoDeepLink(data.url);
            if (!rota || rota === '/') return;

            var atual = global.location.pathname + global.location.search + global.location.hash;
            if (global.location.pathname === '/login') {
                var next = new URLSearchParams(global.location.search).get('next') || '';
                if (mesmoDestino(next, rota)) return;
            }
            if (atual === rota) {
                if (new URLSearchParams(global.location.search).get('acao') === 'novo') {
                    global.location.reload();
                }
                return;
            }
            global.location.href = rota;
        } catch (e) {
            console.error('[DeepLink] Falha ao processar URL:', e);
        }
    }

    function pluginAppDisponivel(cap) {
        if (!cap) return false;
        try {
            if (typeof cap.isPluginAvailable === 'function') {
                return !!cap.isPluginAvailable('App');
            }
        } catch (err) {
            return false;
        }
        return !!(cap.Plugins && cap.Plugins.App);
    }

    function obterPluginApp(cap) {
        if (!pluginAppDisponivel(cap)) return null;
        try {
            return (cap.Plugins && cap.Plugins.App) || null;
        } catch (err) {
            console.warn('[Capacitor] Erro ao acessar Plugins.App:', err);
            return null;
        }
    }

    function registrar(App) {
        if (!App || typeof App.addListener !== 'function' || listenerRegistrado) return;
        listenerRegistrado = true;

        try {
            App.addListener('appUrlOpen', function (data) {
                try {
                    navegar(data);
                } catch (e) {
                    console.error('[DeepLink] Falha no handler appUrlOpen:', e);
                }
            });
        } catch (err) {
            listenerRegistrado = false;
            console.warn('[Capacitor] Erro ao registrar listener do App:', err);
            return;
        }

        if (typeof App.getLaunchUrl !== 'function') return;
        Promise.resolve(App.getLaunchUrl()).then(function (res) {
            if (!res || !res.url) return;
            try {
                if (global.sessionStorage && global.sessionStorage.getItem(CHAVE_LAUNCH) === res.url) return;
                if (global.sessionStorage) global.sessionStorage.setItem(CHAVE_LAUNCH, res.url);
            } catch (err) {}
            navegar(res);
        }).catch(function (err) {
            console.warn('[Capacitor] getLaunchUrl falhou:', err);
        });
    }

    function tentarRegistrar() {
        try {
            var cap = global.Capacitor;
            if (!cap) return false;
            var App = obterPluginApp(cap);
            if (!App) return false;
            registrar(App);
            return listenerRegistrado;
        } catch (err) {
            console.warn('[Capacitor] Erro ao registrar listener do App:', err);
            return false;
        }
    }

    function iniciarDeepLinks() {
        if (!global.Capacitor) return;

        if (!tentarRegistrar()) {
            var tentativas = 0;
            var timer = global.setInterval(function () {
                tentativas += 1;
                if (tentarRegistrar() || tentativas > 20) global.clearInterval(timer);
            }, 250);
        }
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { rotaDoDeepLink: rotaDoDeepLink, mesmoDestino: mesmoDestino };
    }

    // Aguarda DOM quando ainda estiver carregando; no WebView Capacitor o bridge
    // pode aparecer um pouco depois — o poll em iniciarDeepLinks cobre isso.
    if (global.document && global.document.readyState === 'loading') {
        global.document.addEventListener('DOMContentLoaded', iniciarDeepLinks);
    } else {
        iniciarDeepLinks();
    }
})(typeof window !== 'undefined' ? window : globalThis);
