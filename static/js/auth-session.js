/**
 * Persistência de sessão para WebView (Capacitor iOS / WKWebView).
 * Complementa o cookie Flask — não substitui a autenticação server-side.
 */
(function (global) {
    'use strict';

    var STORAGE_KEY = 'menino_alho_sessao';

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

    global.MeninoAlhoAuth = {
        persistirSessao: persistirSessao,
        lerSessaoPersistida: lerSessaoPersistida,
        limparSessaoPersistida: limparSessaoPersistida,
        normalizarUrlRedirect: normalizarUrlRedirect
    };
})(typeof window !== 'undefined' ? window : this);
