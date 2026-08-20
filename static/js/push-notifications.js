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

    /**
     * Exibe uma notificação local (não Web Push do servidor).
     * Capacitor → LocalNotifications; Web → SW showNotification / Notification.
     */
    async function exibirNotificacaoLocal(titulo, corpo, opcoes) {
        opcoes = opcoes || {};
        var icon = opcoes.icon || TEST_ICON;
        var tag = opcoes.tag || ('menino-alho-' + Date.now());

        if (isCapacitorNative()) {
            var plugins = getCapacitorPlugins();
            if (plugins && plugins.LocalNotifications) {
                var Local = plugins.LocalNotifications;
                var permStatus = await Local.checkPermissions();
                if (!permStatus || String(permStatus.display || '').toLowerCase() !== 'granted') {
                    permStatus = await Local.requestPermissions();
                }
                if (!permStatus || String(permStatus.display || '').toLowerCase() !== 'granted') {
                    throw new Error('Permissão de notificação negada no dispositivo.');
                }
                if (typeof Local.createChannel === 'function') {
                    try {
                        await Local.createChannel({
                            id: 'menino_alho_alertas',
                            name: 'Alertas do Sistema',
                            importance: 5,
                            visibility: 1,
                            sound: 'default'
                        });
                    } catch (channelErr) { /* canal opcional */ }
                }
                var notifId = Math.floor((Date.now() + Math.random() * 1000) % 2147483647);
                await Local.schedule({
                    notifications: [{
                        id: notifId,
                        title: titulo,
                        body: corpo,
                        channelId: 'menino_alho_alertas',
                        schedule: { at: new Date(Date.now() + 500) }
                    }]
                });
                return true;
            }
        }

        if (!('Notification' in global)) {
            throw new Error('Notificações não suportadas neste ambiente.');
        }
        if (Notification.permission === 'denied') {
            throw new Error('Permissão de notificação bloqueada.');
        }
        if (Notification.permission !== 'granted') {
            var perm = await Notification.requestPermission();
            if (perm !== 'granted') {
                throw new Error('Permissão de notificação não concedida.');
            }
        }

        if ('serviceWorker' in navigator) {
            try {
                var registration = await navigator.serviceWorker.ready;
                if (registration && typeof registration.showNotification === 'function') {
                    await registration.showNotification(titulo, {
                        body: corpo,
                        icon: icon,
                        tag: tag,
                        renotify: true,
                        data: { url: opcoes.url || '/' }
                    });
                    return true;
                }
            } catch (swErr) { /* cai no fallback */ }
        }

        new Notification(titulo, { body: corpo, icon: icon, tag: tag });
        return true;
    }

    function _chaveSilencioDiario(tipo) {
        var hoje = new Date().toDateString();
        return 'menino_alho_notif_' + tipo + '_' + hoje;
    }

    var IDS_AGENDAMENTO_FIXOS = [9101, 9102, 9103, 9104];

    function _parseHorario(hhmm) {
        var parts = String(hhmm || '08:00').split(':');
        var h = parseInt(parts[0], 10);
        var m = parseInt(parts[1], 10);
        if (isNaN(h) || h < 0 || h > 23) h = 8;
        if (isNaN(m) || m < 0 || m > 59) m = 0;
        return { hour: h, minute: m };
    }

    function _proximaOcorrencia(hour, minute) {
        var agora = new Date();
        var alvo = new Date(
            agora.getFullYear(),
            agora.getMonth(),
            agora.getDate(),
            hour,
            minute,
            0,
            0
        );
        if (alvo.getTime() <= agora.getTime() + 5000) {
            alvo.setDate(alvo.getDate() + 1);
        }
        return alvo;
    }

    /**
     * Agenda notificações locais no Capacitor para os horários escolhidos.
     *
     * A) Cancela IDs fixos anteriores (evita duplicatas).
     * B) Lê pendências/frase da API.
     * C) Schedule diário (repeats) ou próxima ocorrência.
     *
     * WEB: agendamento local com aba fechada não é confiável — o disparo
     * em horário fica a cargo do CRON + Web Push no backend
     * (``/api/cron/enviar_alertas_pendencias`` e ``/api/cron/enviar_frase_diaria``).
     */
    async function agendarNotificacoesLocais() {
        if (global.localStorage.getItem('menino_alho_device_notif') !== 'true') {
            return { ok: false, motivo: 'dispositivo_inativo' };
        }

        var resp = await fetch('/api/notificacoes/verificar_pendencias', {
            credentials: 'same-origin',
            headers: { 'Accept': 'application/json' }
        });
        if (!resp.ok) {
            return { ok: false, motivo: 'http_' + resp.status };
        }
        var data = await resp.json();
        if (!data || !data.ok) {
            return { ok: false, motivo: 'payload_invalido' };
        }

        var agendamentos = data.agendamentos || [];

        // --- Capacitor (iOS/Android): schedule nativo confiável em background ---
        if (isCapacitorNative()) {
            var plugins = getCapacitorPlugins();
            if (!plugins || !plugins.LocalNotifications) {
                return { ok: false, motivo: 'sem_local_notifications' };
            }
            var Local = plugins.LocalNotifications;

            var permStatus = await Local.checkPermissions();
            if (!permStatus || String(permStatus.display || '').toLowerCase() !== 'granted') {
                permStatus = await Local.requestPermissions();
            }
            if (!permStatus || String(permStatus.display || '').toLowerCase() !== 'granted') {
                throw new Error('Permissão de notificação negada no dispositivo.');
            }

            if (typeof Local.createChannel === 'function') {
                try {
                    await Local.createChannel({
                        id: 'menino_alho_alertas',
                        name: 'Alertas do Sistema',
                        importance: 5,
                        visibility: 1,
                        sound: 'default'
                    });
                } catch (channelErr) { /* opcional */ }
            }

            // A) Cancela agendamentos anteriores dos IDs fixos
            try {
                await Local.cancel({
                    notifications: IDS_AGENDAMENTO_FIXOS.map(function(id) {
                        return { id: id };
                    })
                });
            } catch (cancelErr) {
                console.warn('[Push] cancel agendamentos:', cancelErr);
            }

            var paraAgendar = [];
            for (var i = 0; i < agendamentos.length; i++) {
                var item = agendamentos[i];
                if (!item || !item.ativo || !item.titulo) continue;
                var hm = _parseHorario(item.horario);
                var notif = {
                    id: item.id_local || IDS_AGENDAMENTO_FIXOS[i],
                    title: item.titulo,
                    body: item.mensagem || '',
                    channelId: 'menino_alho_alertas',
                    extra: { tipo: item.tipo, url: item.link || '/' }
                };
                // Preferência: recorrência diária no horário escolhido
                if (item.recorrente_diario) {
                    notif.schedule = {
                        on: { hour: hm.hour, minute: hm.minute },
                        repeats: true,
                        allowWhileIdle: true
                    };
                } else {
                    notif.schedule = {
                        at: _proximaOcorrencia(hm.hour, hm.minute),
                        allowWhileIdle: true
                    };
                }
                paraAgendar.push(notif);
            }

            if (paraAgendar.length) {
                try {
                    await Local.schedule({ notifications: paraAgendar });
                } catch (schedErr) {
                    // Fallback: algumas versões do plugin exigem `at` em vez de `on`
                    console.warn('[Push] schedule repeats falhou, tentando at:', schedErr);
                    paraAgendar = paraAgendar.map(function(n) {
                        var hm2 = (n.schedule && n.schedule.on) ? n.schedule.on : { hour: 8, minute: 0 };
                        return Object.assign({}, n, {
                            schedule: {
                                at: _proximaOcorrencia(hm2.hour, hm2.minute),
                                allowWhileIdle: true
                            }
                        });
                    });
                    await Local.schedule({ notifications: paraAgendar });
                }
            }

            console.log('[Push] Agendadas', paraAgendar.length, 'notificações locais.');
            return { ok: true, plataforma: 'capacitor', agendadas: paraAgendar.length };
        }

        // --- WEB / PWA ---
        // Fundação: não dispara imediatamente. O horário é honrado pelo
        // backend via CRON + Web Push (aba fechada). Aqui só registramos
        // as preferências em localStorage para auditoria/debug.
        try {
            global.localStorage.setItem(
                'menino_alho_notif_agenda',
                JSON.stringify({
                    salvo_em: new Date().toISOString(),
                    horarios: data.horarios || {},
                    preferencias: data.preferencias || {},
                    agendamentos: agendamentos.map(function(a) {
                        return { tipo: a.tipo, ativo: a.ativo, horario: a.horario };
                    })
                })
            );
        } catch (lsErr) { /* ignore */ }

        console.log(
            '[Push] Web: horários salvos. Disparo em background via CRON + Web Push ' +
            '(agendamento local no browser não é confiável com a aba fechada).'
        );
        return { ok: true, plataforma: 'web_cron', agendadas: 0 };
    }

    /**
     * @deprecated Preferir agendarNotificacoesLocais — não dispara mais ao abrir o app.
     */
    async function verificarPendenciasENotificar() {
        return agendarNotificacoesLocais();
    }

    global.MeninoAlhoPush = {
        isSupported: isSupported,
        isCapacitorNative: isCapacitorNative,
        urlBase64ToUint8Array: urlBase64ToUint8Array,
        ativar: ativar,
        desativar: desativar,
        verificarSubscriptionAtiva: verificarSubscriptionAtiva,
        dispararTeste: dispararTeste,
        exibirNotificacaoLocal: exibirNotificacaoLocal,
        agendarNotificacoesLocais: agendarNotificacoesLocais,
        verificarPendenciasENotificar: verificarPendenciasENotificar
    };
})(window);
