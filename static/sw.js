// Service Worker para PWA - Menino do Alho
// Versão do cache (incrementar quando houver mudanças significativas)
const CACHE_VERSION = 'v1';
const CACHE_NAME = `menino-alho-${CACHE_VERSION}`;

// Arquivos estáticos que serão cacheados (CSS, JS, Fontes, Imagens)
const STATIC_ASSETS = [
  '/static/icon-192x192.png',
  '/static/icon-512x512.png',
  '/static/manifest.json'
];

// Estratégia: Stale-While-Revalidate para arquivos estáticos
// Serve do cache imediatamente e atualiza em segundo plano
async function staleWhileRevalidate(request) {
  const cache = await caches.open(CACHE_NAME);
  const cachedResponse = await cache.match(request);
  
  // Buscar atualização em segundo plano
  const fetchPromise = fetch(request).then(response => {
    // Se a resposta for válida, atualizar o cache
    if (response.status === 200) {
      cache.put(request, response.clone());
    }
    return response;
  }).catch(() => {
    // Em caso de erro na rede, retornar o cache se existir
    return cachedResponse;
  });
  
  // Retornar cache imediatamente se disponível, senão aguardar a rede
  return cachedResponse || fetchPromise;
}

// Estratégia: Network First para HTML e rotas de dados
// Prioriza dados atualizados (importante para segurança financeira)
async function networkFirst(request) {
  try {
    // Tentar buscar da rede primeiro
    const networkResponse = await fetch(request);
    
    // Se a resposta for válida, atualizar o cache
    if (networkResponse.status === 200) {
      const cache = await caches.open(CACHE_NAME);
      cache.put(request, networkResponse.clone());
    }
    
    return networkResponse;
  } catch (error) {
    // Se a rede falhar, tentar servir do cache
    const cache = await caches.open(CACHE_NAME);
    const cachedResponse = await cache.match(request);
    
    if (cachedResponse) {
      return cachedResponse;
    }
    
    // Se não houver cache, retornar erro
    throw error;
  }
}

// Instalação do Service Worker
self.addEventListener('install', event => {
  console.log('[SW] Service Worker instalando...');
  
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      console.log('[SW] Cache aberto');
      // Pré-cache apenas dos assets críticos
      return cache.addAll(STATIC_ASSETS.map(url => new Request(url, { cache: 'reload' }))).catch(err => {
        console.log('[SW] Erro ao pré-cachear alguns assets:', err);
        // Não falhar a instalação se alguns assets não puderem ser cacheados
      });
    })
  );
  
  // Forçar ativação imediata do novo Service Worker
  self.skipWaiting();
});

// Ativação do Service Worker
self.addEventListener('activate', event => {
  console.log('[SW] Service Worker ativando...');
  
  event.waitUntil(
    caches.keys().then(cacheNames => {
      return Promise.all(
        cacheNames.map(cacheName => {
          // Remover caches antigos que não correspondem à versão atual
          if (cacheName !== CACHE_NAME) {
            console.log('[SW] Removendo cache antigo:', cacheName);
            return caches.delete(cacheName);
          }
        })
      );
    })
  );
  
  // Assumir controle imediatamente de todas as páginas
  return self.clients.claim();
});

// Interceptar requisições
self.addEventListener('fetch', event => {
  const { request } = event;
  const url = new URL(request.url);
  
  // Ignorar requisições que não são GET
  if (request.method !== 'GET') {
    return;
  }
  
  // Ignorar requisições de APIs externas (CDNs, etc)
  if (url.origin !== self.location.origin) {
    return;
  }
  
  // Estratégia para arquivos estáticos (CSS, JS, Imagens, Fontes)
  if (
    url.pathname.startsWith('/static/') ||
    url.pathname.includes('.css') ||
    url.pathname.includes('.js') ||
    url.pathname.includes('.png') ||
    url.pathname.includes('.jpg') ||
    url.pathname.includes('.jpeg') ||
    url.pathname.includes('.gif') ||
    url.pathname.includes('.svg') ||
    url.pathname.includes('.woff') ||
    url.pathname.includes('.woff2') ||
    url.pathname.includes('.ttf') ||
    url.pathname.includes('.eot')
  ) {
    event.respondWith(staleWhileRevalidate(request));
    return;
  }
  
  // Estratégia Network First para HTML e rotas de dados
  // Garante que dados financeiros sempre sejam atualizados
  if (
    url.pathname === '/' ||
    url.pathname.startsWith('/dashboard') ||
    url.pathname.startsWith('/vendas') ||
    url.pathname.startsWith('/produtos') ||
    url.pathname.startsWith('/clientes') ||
    url.pathname.endsWith('.html')
  ) {
    event.respondWith(networkFirst(request));
    return;
  }
  
  // Para outras requisições, usar estratégia padrão (Network First)
  event.respondWith(networkFirst(request));
});

// Mensagens do cliente para o Service Worker
self.addEventListener('message', event => {
  if (event.data && event.data.type === 'SKIP_WAITING') {
    self.skipWaiting();
  }
});
