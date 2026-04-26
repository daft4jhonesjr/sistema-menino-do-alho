"""Pacote de Blueprints (Fase 1 — refatoração incremental).

Estado atual:
    * ``auth_bp``    → /login, /logout, /cadastro, /perfil, /configuracoes,
                       /gerenciar_usuarios/*, /api/logs/*
    * ``master_bp``  → /master-admin/*

Próximas fases (a serem extraídas):
    * dashboard_bp, vendas_bp, produtos_bp, documentos_bp, caixa_bp, clientes_bp.

Convenção:
    Cada blueprint expõe uma única variável module-level ``<nome>_bp`` que
    o ``app.py`` registra via ``app.register_blueprint(...)`` no fim do
    bootstrap. Os handlers continuam reutilizando os helpers definidos em
    ``app.py`` (decorators ``tenant_required``, ``master_required``,
    ``_safe_db_commit``, etc.) — esses helpers serão movidos para um
    pacote ``services/`` em uma fase futura, quando todos os blueprints
    estiverem extraídos.
"""

from .auth import auth_bp
from .master import master_bp

__all__ = ['auth_bp', 'master_bp']
