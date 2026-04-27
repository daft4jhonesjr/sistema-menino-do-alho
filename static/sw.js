// Service Worker PWA - Menino do Alho
// Cache local de estáticos para carregamento instantâneo
// v3: adicionado suporte a Web Push (eventos push e notificationclick)
// v4 (P0 fix): NÃO interceptar mais navegações HTML (mode === 'navigate').
//     O respondWith em navegação pós-redirect 302 causava abort prematuro
//     no Safari ("Servidor cortou a conexão"), mesmo com o INSERT já
//     comitado no servidor. Versão v4 força clientes em campo a limpar
//     o SW antigo via evento 'activate'.
const CACHE_NAME = 'menino-alho-v4';
const ASSETS_TO_CACHE = [
    '/static/images/logo_menino_do_alho_amarelo1.jpeg',
    '/static/manifest.json',
    'https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/pulltorefreshjs/0.1.22/index.umd.min.js'
];

// 1. Instalação: Baixa os arquivos essenciais
self.addEventListener('install', (event) => {
    event.waitUntil(
        caches.open(CACHE_NAME).then((cache) => {
            return Promise.allSettled(
                ASSETS_TO_CACHE.map((url) => cache.add(url).catch(() => {}))
            );
        })
    );
    self.skipWaiting(); // Ativa imediatamente
});

// 2. Ativação: Limpa caches antigos se mudar a versão
self.addEventListener('activate', (event) => {
    event.waitUntil(
        caches.keys().then((keyList) => {
            return Promise.all(keyList.map((key) => {
                if (key !== CACHE_NAME) {
                    return caches.delete(key);
                }
            }));
        })
    );
    self.clients.claim();
});

// 3. Interceptação de Requisições (Stale-While-Revalidate para estáticos)
self.addEventListener('fetch', (event) => {
    if (event.request.method !== 'GET') return;

    const url = new URL(event.request.url);

    // Rotas que fazem redirect externo (WhatsApp, etc.) — não interceptar
    if (url.pathname.includes('/whatsapp')) return;

    // Navegações HTML (mode === 'navigate'): NÃO interceptar.
    // Deixar o navegador gerenciar nativamente. Em SPAs/PWAs com Safari,
    // o uso de event.respondWith(fetch(...)) em navegação pós-redirect 302
    // pode abortar prematuramente, mostrando "Servidor cortou a conexão"
    // mesmo quando o backend já respondeu OK (ex.: POST /caixa/adicionar
    // → 302 → GET /caixa). Sem respondWith aqui, o navegador faz o fetch
    // direto e a navegação fica robusta.
    if (event.request.mode === 'navigate') {
        return;
    }

    // Estratégia: Stale-While-Revalidate para ESTÁTICOS
    if (event.request.destination === 'image' ||
        event.request.destination === 'style' ||
        event.request.destination === 'script' ||
        event.request.destination === 'font') {
        event.respondWith(
            caches.match(event.request).then((cachedResponse) => {
                const fetchPromise = fetch(event.request).then((networkResponse) => {
                    if (networkResponse && networkResponse.status === 200 && networkResponse.type === 'basic') {
                        const responseToCache = networkResponse.clone();
                        caches.open(CACHE_NAME).then((cache) => {
                            cache.put(event.request, responseToCache);
                        });
                    }
                    return networkResponse;
                }).catch(() => cachedResponse);
                return cachedResponse || fetchPromise;
            })
        );
        return;
    }

    // Estratégia: Network First para DADOS (API/JSON)
    event.respondWith(
        fetch(event.request).catch(() => caches.match(event.request))
    );
});

// Mensagens do cliente
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});

// ─────────────────────────────────────────────────────────────────────────────
// WEB PUSH — Recebe notificações em background (aba fechada / tela bloqueada)
// Requisito do lado do servidor: VAPID + pywebpush + tabela de subscriptions.
// ─────────────────────────────────────────────────────────────────────────────

// 4. Recebe o payload enviado pelo servidor via protocolo Web Push (VAPID)
self.addEventListener('push', function(event) {
    var data = {};
    if (event.data) {
        try {
            data = event.data.json();
        } catch (e) {
            data = { title: 'Menino do Alho', body: event.data.text() };
        }
    }

    var title   = data.title  || 'Menino do Alho 🧄';
    var options = {
        body:    data.body    || 'Você tem uma nova notificação.',
        icon:    data.icon    || '/static/images/logo_menino_do_alho_amarelo1.jpeg',
        badge:   data.badge   || '/static/images/logo_menino_do_alho_amarelo1.jpeg',
        vibrate: [100, 50, 100],
        tag:     data.tag     || 'menino-alho-push',
        renotify: true,
        data: {
            url: data.url || '/'
        }
    };

    event.waitUntil(self.registration.showNotification(title, options));
});

// 5. Clique na notificação: abre/foca o app e fecha o banner
self.addEventListener('notificationclick', function(event) {
    event.notification.close();

    var targetUrl = (event.notification.data && event.notification.data.url)
        ? event.notification.data.url
        : '/';

    event.waitUntil(
        clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(windowClients) {
            // Se já há uma aba aberta com a URL alvo, foca nela
            for (var i = 0; i < windowClients.length; i++) {
                var client = windowClients[i];
                if (client.url === targetUrl && 'focus' in client) {
                    return client.focus();
                }
            }
            // Caso contrário, abre uma nova aba
            if (clients.openWindow) {
                return clients.openWindow(targetUrl);
            }
        })
    );
});

// 6. Notificação ignorada (swipe para fechar) — opcional, útil para analytics
self.addEventListener('notificationclose', function(event) {
    // Nenhuma ação obrigatória; reservado para futura telemetria.
});
