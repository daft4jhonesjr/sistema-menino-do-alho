"""Singletons de extensões Flask — fonte única de verdade.

Este módulo concentra a criação dos objetos das extensões usadas pelo
sistema (``db``, ``login_manager``, ``csrf``, ``cache``, ``limiter``).
Todos os objetos são criados sem aplicação associada (``unbound``) e
depois registrados na aplicação Flask através de ``init_extensions(app)``.

Esse padrão é o oficial do Flask para evitar ciclos de import quando a
aplicação cresce e é fatiada em blueprints. Antes desta fase 4, esses
singletons eram criados (ou inicializados) diretamente em ``app.py``,
forçando os blueprints (``routes/*.py``) a fazer ``from app import ...``
em todos os handlers — o que criava acoplamento e impedia reorganizações
maiores sem refatorar dezenas de imports.

Convenções:

* ``db`` continua sendo definido em ``models.py`` (porque os modelos
  precisam de ``db.Model`` em tempo de import). Aqui apenas re-exportamos.
* ``login_manager``, ``csrf``, ``limiter`` e ``cache`` são criados unbound.
* O cache **defaults** para ``SimpleCache``; quando ``REDIS_URL`` está
  disponível, ``init_extensions`` reconfigura o cache para ``RedisCache``
  antes de chamar ``init_app``.
* O ``Limiter`` usa ``get_remote_address`` como key_func e armazena em
  memória por padrão (em produção pode ser substituído por Redis).

Imports recomendados nos blueprints novos::

    from extensions import db, csrf, limiter, cache, login_manager

Backwards-compat: ``app.py`` continua expondo ``db``, ``csrf``, ``limiter``,
``cache``, ``login_manager`` no nível do módulo (são re-importados de
``extensions``), mantendo todos os ``from app import ...`` legados
funcionando enquanto a refatoração dos blueprints existentes não termina.
"""

from __future__ import annotations

import os

from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# ``db`` permanece em ``models.py`` para evitar ciclos com as classes
# declarativas. Re-exportamos aqui para que blueprints importem de um único
# lugar (``from extensions import db``).
from models import db

login_manager = LoginManager()
csrf = CSRFProtect()
cache = Cache(config={'CACHE_TYPE': 'SimpleCache'})

# Rate Limiting global. Defaults: 200/dia, 50/hora — limites largos por
# IP, suficientes para uso humano normal sem incomodar o usuário, mas que
# barram scrapers e brute-force massivos. Limites mais agressivos (ex.
# ``5 per minute`` em /login POST) são aplicados nas próprias rotas via
# ``@limiter.limit(...)``.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri='memory://',
    default_limits=['200 per day', '50 per hour'],
)


def init_extensions(app):
    """Inicializa todas as extensões na aplicação Flask informada.

    Deve ser chamado UMA vez em ``app.py``, logo após ``app = Flask(...)``
    e a configuração (``app.config.from_object(Config)``).

    A lógica de escolha do backend de cache (Redis em produção,
    SimpleCache em dev/test) é feita aqui, garantindo que o objeto
    ``cache`` exposto por este módulo é o MESMO usado em runtime.
    """
    db.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Faça login para acessar esta página.'

    csrf.init_app(app)

    redis_url = os.environ.get('REDIS_URL')
    if redis_url:
        cache.init_app(app, config={
            'CACHE_TYPE': 'RedisCache',
            'CACHE_REDIS_URL': redis_url,
            'CACHE_DEFAULT_TIMEOUT': 300,
        })
        app.logger.info('🟢 Cache configurado usando Redis Unificado')
    else:
        cache.init_app(app, config={'CACHE_TYPE': 'SimpleCache'})
        app.logger.info('🟡 Cache configurado usando SimpleCache (Memória Local)')

    limiter.init_app(app)


__all__ = [
    'db',
    'login_manager',
    'csrf',
    'cache',
    'limiter',
    'init_extensions',
]
