/**
 * Web Push (PWA) — registro de Service Worker, inscrição e envio ao backend.
 * Usado em Configurações e disponível globalmente como window.MeninoAlhoPush.
 */
(function(global) {
    'use strict';

    var SUBSCRIBE_URL = '/api/push/subscribe';
    var UNSUBSCRIBE_URL = '/api/push/unsubscribe';
    var VAPID_URL = '/api/push/vapid-public-key';

    function urlBase64ToUint8Array(base64String) {
        var padding = '='.repeat((4 - (base64String.length % 4)) % 4);
        var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
        var rawData = global.atob(base64);
        var outputArray = new Uint8Array(rawData.length);
        for (var i = 0; i < rawData.length; ++i) {
            outputArray[i] = rawData.charCodeAt(i);
        }
        return outputArray;
    }

    function buildHeaders() {
        var headers = { 'Content-Type': 'application/json' };
        if (typeof global.getCsrfHeaders === 'function') {
            var csrfH = global.getCsrfHeaders();
            for (var k in csrfH) {
                if (Object.prototype.hasOwnProperty.call(csrfH, k)) {
                    headers[k] = csrfH[k];
                }
            }
        }
        return headers;
    }

    function isSupported() {
        return ('serviceWorker' in navigator) && ('PushManager' in global);
    }

    /**
     * Solicita permissão, registra SW (se necessário) e inscreve no PushManager.
     * options.onStatus(tipo, mensagem) — callback opcional para UI (configurações).
     */
    async function ativar(options) {
        options = options || {};
        var onStatus = typeof options.onStatus === 'function' ? options.onStatus : null;

        if (!isSupported()) {
            if (onStatus) onStatus('aviso', 'Seu navegador não suporta Push Notifications em segundo plano.');
            return false;
        }

        var permissao = await Notification.requestPermission();
        if (permissao !== 'granted') {
            if (onStatus) {
                onStatus('bloqueado', 'Permissão negada. Toque no cadeado na barra de endereço > Notificações > Permitir.');
            }
            return false;
        }

        try {
            var keyResp = await fetch(VAPID_URL, { credentials: 'same-origin' });
            if (!keyResp.ok) {
                global.localStorage.setItem('menino_alho_device_notif', 'true');
                if (onStatus) {
                    onStatus('ativo', 'Notificações locais ativas (Push background não configurado ainda).');
                }
                return true;
            }

            var keyData = await keyResp.json();
            var applicationServerKey = urlBase64ToUint8Array(keyData.publicKey);

            var reg = await navigator.serviceWorker.ready;
            var subscription = await reg.pushManager.subscribe({
                userVisibleOnly: true,
                applicationServerKey: applicationServerKey
            });

            var subResp = await fetch(SUBSCRIBE_URL, {
                method: 'POST',
                headers: buildHeaders(),
                credentials: 'same-origin',
                body: JSON.stringify(subscription.toJSON())
            });

            if (subResp.ok) {
                global.localStorage.setItem('menino_alho_device_notif', 'true');
                if (onStatus) {
                    onStatus('ativo', 'Push Notifications ativas! Você receberá alertas mesmo com o app fechado.');
                }
                return true;
            }

            if (onStatus) {
                onStatus('aviso', 'Inscrito no browser, mas erro ao salvar no servidor. Tente novamente.');
            }
            return false;
        } catch (err) {
            if (onStatus) {
                onStatus('aviso', 'Erro ao ativar push: ' + (err && err.message ? err.message : 'desconhecido'));
            }
            return false;
        }
    }

    async function desativar() {
        global.localStorage.setItem('menino_alho_device_notif', 'false');
        try {
            if (!isSupported()) return;
            var reg = await navigator.serviceWorker.ready;
            var sub = await reg.pushManager.getSubscription();
            if (sub) {
                await fetch(UNSUBSCRIBE_URL, {
                    method: 'POST',
                    headers: buildHeaders(),
                    credentials: 'same-origin',
                    body: JSON.stringify({ endpoint: sub.endpoint })
                });
                await sub.unsubscribe();
            }
        } catch (err) {
            console.warn('[Push] Erro ao cancelar subscription:', err);
        }
    }

    async function verificarSubscriptionAtiva() {
        if (!isSupported()) return false;
        try {
            var reg = await navigator.serviceWorker.ready;
            var sub = await reg.pushManager.getSubscription();
            if (sub) {
                global.localStorage.setItem('menino_alho_device_notif', 'true');
                return true;
            }
        } catch (e) { /* silencia */ }
        return false;
    }

    global.MeninoAlhoPush = {
        isSupported: isSupported,
        urlBase64ToUint8Array: urlBase64ToUint8Array,
        ativar: ativar,
        desativar: desativar,
        verificarSubscriptionAtiva: verificarSubscriptionAtiva
    };
})(window);
