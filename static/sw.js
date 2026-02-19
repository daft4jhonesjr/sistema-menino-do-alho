// Service Worker PWA - Menino do Alho
// Cache local de estáticos para carregamento instantâneo
const CACHE_NAME = 'menino-alho-v1';
const ASSETS_TO_CACHE = [
    '/static/images/logo_menino_do_alho_amarelo1.jpeg',
    '/static/manifest.json',
    '/static/icon-192x192.png',
    '/static/icon-512x512.png',
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

    // Estratégia: Stale-While-Revalidate para ESTÁTICOS
    if (event.request.destination === 'image' ||
        event.request.destination === 'style' ||
        event.request.destination === 'script' ||
        event.request.destination === 'font') {
        event.respondWith(
            caches.match(event.request).then((cachedResponse) => {
                const fetchPromise = fetch(event.request).then((networkResponse) => {
                    if (networkResponse.status === 200) {
                        caches.open(CACHE_NAME).then((cache) => {
                            cache.put(event.request, networkResponse.clone());
                        });
                    }
                    return networkResponse;
                });
                return cachedResponse || fetchPromise;
            })
        );
        return;
    }

    // Estratégia: Network First para DADOS (HTML/API)
    event.respondWith(
        fetch(event.request).catch(() => {
            return caches.match(event.request);
        })
    );
});

// Mensagens do cliente
self.addEventListener('message', (event) => {
    if (event.data && event.data.type === 'SKIP_WAITING') {
        self.skipWaiting();
    }
});
