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
            if (!rota) return;

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
        } catch (err) {
            console.error('Erro ao processar Deep Link do Widget:', err);
        }
    }

    function registrar(App) {
        if (!App || typeof App.addListener !== 'function' || listenerRegistrado) return;
        listenerRegistrado = true;

        App.addListener('appUrlOpen', function (data) {
            navegar(data);
        });

        if (typeof App.getLaunchUrl !== 'function') return;
        Promise.resolve(App.getLaunchUrl()).then(function (res) {
            if (!res || !res.url) return;
            try {
                if (global.sessionStorage && global.sessionStorage.getItem(CHAVE_LAUNCH) === res.url) return;
                if (global.sessionStorage) global.sessionStorage.setItem(CHAVE_LAUNCH, res.url);
            } catch (err) {}
            navegar(res);
        }).catch(function () {});
    }

    function tentarRegistrar() {
        var cap = global.Capacitor;
        var App = cap && cap.Plugins && cap.Plugins.App;
        if (!App) return false;
        registrar(App);
        return true;
    }

    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { rotaDoDeepLink: rotaDoDeepLink, mesmoDestino: mesmoDestino };
    }

    if (!global.Capacitor) return;

    if (!tentarRegistrar()) {
        var tentativas = 0;
        var timer = global.setInterval(function () {
            tentativas += 1;
            if (tentarRegistrar() || tentativas > 20) global.clearInterval(timer);
        }, 250);
    }
})(typeof window !== 'undefined' ? window : globalThis);
