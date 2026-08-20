/**
 * Web Push (PWA) — registro de Service Worker, inscrição e envio ao backend.
 * No Capacitor iOS/Android, usa plugins nativos quando disponíveis
 * (sem tratar WKWebView como "navegador sem suporte").
 * Usado em Configurações e disponível globalmente como window.MeninoAlhoPush.
 */
(function(global) {
    'use strict';

    var SUBSCRIBE_URL = '/api/push/subscribe';
    var UNSUBSCRIBE_URL = '/api/push/unsubscribe';
    var VAPID_URL = '/api/push/vapid-public-key';

    function isCapacitorNative() {
        try {
            var Cap = global.Capacitor;
            if (!Cap) return false;
            if (typeof Cap.isNativePlatform === 'function') {
                return !!Cap.isNativePlatform();
            }
            return true;
        } catch (err) {
            return false;
        }
    }

    function getCapacitorPlugins() {
        var Cap = global.Capacitor;
        return (Cap && Cap.Plugins) ? Cap.Plugins : null;
    }

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

    function isWebPushSupported() {
        return ('serviceWorker' in navigator) && ('PushManager' in global) && ('Notification' in global);
    }

    /** Capaz de ativar alertas neste ambiente (Web Push ou shell nativo). */
    function isSupported() {
        if (isCapacitorNative()) return true;
        return isWebPushSupported();
    }

    async function ativarNativoCapacitor(onStatus) {
        var plugins = getCapacitorPlugins();

        try {
            if (plugins && plugins.PushNotifications) {
                var Push = plugins.PushNotifications;
                var perm = await Push.requestPermissions();
                var receive = (perm && (perm.receive || perm.granted)) || '';
                if (String(receive).toLowerCase() !== 'granted') {
                    if (onStatus) {
                        onStatus('bloqueado', 'Permissão de notificações negada nas configurações do iPhone.');
                    }
                    return false;
                }
                if (typeof Push.register === 'function') {
                    await Push.register();
                }
                global.localStorage.setItem('menino_alho_device_notif', 'true');
                if (onStatus) {
                    onStatus('ativo', 'Notificações nativas ativas neste dispositivo.');
                }
                return true;
            }

            if (plugins && plugins.LocalNotifications) {
                var Local = plugins.LocalNotifications;
                var localPerm = await Local.requestPermissions();
                var display = (localPerm && (localPerm.display || localPerm.granted)) || '';
                if (String(display).toLowerCase() !== 'granted') {
                    if (onStatus) {
                        onStatus('bloqueado', 'Permissão de notificações negada nas configurações do iPhone.');
                    }
                    return false;
                }
                global.localStorage.setItem('menino_alho_device_notif', 'true');
                if (onStatus) {
                    onStatus('ativo', 'Notificações nativas ativas neste dispositivo.');
                }
                return true;
            }
        } catch (err) {
            console.warn('[Push] Plugin nativo falhou, usando fallback:', err);
        }

        // Fallback: API Notification no WebView (quando existir) ou preferência local.
        if ('Notification' in global && typeof Notification.requestPermission === 'function') {
            var webPerm = await Notification.requestPermission();
            if (webPerm !== 'granted') {
                if (onStatus) {
                    onStatus('bloqueado', 'Permissão de notificações negada. Ative em Ajustes > Menino do Alho.');
                }
                return false;
            }
        }

        global.localStorage.setItem('menino_alho_device_notif', 'true');
        if (onStatus) {
            onStatus('ativo', 'Notificações habilitadas no app. Alertas nativos serão usados neste dispositivo.');
        }
        return true;
    }

    /**
     * Solicita permissão, registra SW (se necessário) e inscreve no PushManager.
     * No Capacitor, prioriza plugins nativos.
     * options.onStatus(tipo, mensagem) — callback opcional para UI (configurações).
     */
    async function ativar(options) {
        options = options || {};
        var onStatus = typeof options.onStatus === 'function' ? options.onStatus : null;

        if (isCapacitorNative()) {
            return ativarNativoCapacitor(onStatus);
        }

        if (!isWebPushSupported()) {
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
        if (isCapacitorNative()) {
            return;
        }
        try {
            if (!isWebPushSupported()) return;
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
        if (isCapacitorNative()) {
            return global.localStorage.getItem('menino_alho_device_notif') === 'true';
        }
        if (!isWebPushSupported()) return false;
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

    var TEST_TITLE = 'Menino do Alho';
    var TEST_BODY_WEB = 'Teste Web: auditoria concluída com sucesso!';
    var TEST_BODY_NATIVE = 'Teste nativo: auditoria concluída com sucesso!';
    var TEST_ICON = '/static/images/icon-192x192.png';

    async function dispararTesteNativoCapacitor() {
        var plugins = getCapacitorPlugins();
        if (!plugins) {
            throw new Error('Plugins nativos do Capacitor não estão disponíveis.');
        }

        if (plugins.LocalNotifications) {
            var Local = plugins.LocalNotifications;
            var localPerm = await Local.requestPermissions();
            var display = (localPerm && (localPerm.display || localPerm.granted)) || '';
            if (String(display).toLowerCase() !== 'granted') {
                throw new Error('Permissão de notificações negada nas configurações do aparelho.');
            }

            if (typeof Local.createChannel === 'function') {
                try {
                    await Local.createChannel({
                        id: 'menino_alho_teste',
                        name: 'Testes',
                        importance: 5,
                        visibility: 1,
                        sound: 'default'
                    });
                } catch (channelErr) {
                    console.warn('[Push] createChannel (opcional):', channelErr);
                }
            }

            var notifId = Math.floor(Date.now() % 2147483647);
            await Local.schedule({
                notifications: [{
                    id: notifId,
                    title: TEST_TITLE,
                    body: TEST_BODY_NATIVE,
                    channelId: 'menino_alho_teste',
                    schedule: { at: new Date(Date.now() + 400) }
                }]
            });
            console.log('Auditoria: Notificação nativa agendada via LocalNotifications.');
            return;
        }

        // Fallback: API Notification no WebView, se existir.
        if ('Notification' in global && typeof Notification.requestPermission === 'function') {
            return dispararTesteWeb();
        }

        throw new Error('Nenhum plugin de notificação local disponível neste app.');
    }

    async function dispararTesteWeb() {
        if (!('Notification' in global)) {
            throw new Error('A API de Notificação não é suportada por este navegador.');
        }

        console.log('Auditoria: Estado atual da permissão:', Notification.permission);

        if (Notification.permission === 'denied') {
            throw new Error('A permissão de notificação foi bloqueada nas configurações do navegador.');
        }

        if (Notification.permission !== 'granted') {
            var permission = await Notification.requestPermission();
            console.log('Auditoria: Permissão solicitada. Resultado:', permission);
            if (permission !== 'granted') {
                throw new Error('A permissão não foi concedida pelo usuário.');
            }
        }

        if ('serviceWorker' in navigator) {
            var registration = await navigator.serviceWorker.getRegistration();
            if (registration) {
                console.log('Auditoria: Disparando notificação via Service Worker.');
                await registration.showNotification(TEST_TITLE, {
                    body: TEST_BODY_WEB,
                    icon: TEST_ICON,
                    tag: 'menino-alho-teste-local',
                    renotify: true
                });
                return;
            }
            console.log('Auditoria: Service Worker não encontrado, disparando via API padrão.');
        }

        new Notification(TEST_TITLE, {
            body: TEST_BODY_WEB,
            icon: TEST_ICON
        });
    }

    /**
     * Auditoria local: exibe uma notificação no aparelho sem passar pelo servidor.
     * Capacitor → LocalNotifications (ou Notification no WebView).
     * Web → Service Worker showNotification, com fallback para Notification.
     */
    async function dispararTeste() {
        console.log('Auditoria: Iniciando teste de notificação.',
            isCapacitorNative() ? '(Capacitor)' : '(Web)');
        if (isCapacitorNative()) {
            await dispararTesteNativoCapacitor();
        } else {
            await dispararTesteWeb();
        }
        return true;
    }

    global.MeninoAlhoPush = {
        isSupported: isSupported,
        isCapacitorNative: isCapacitorNative,
        urlBase64ToUint8Array: urlBase64ToUint8Array,
        ativar: ativar,
        desativar: desativar,
        verificarSubscriptionAtiva: verificarSubscriptionAtiva,
        dispararTeste: dispararTeste
    };
})(window);
