"""Pacote de Blueprints (refatoração incremental).

Estado atual:
    * ``auth_bp``      → /login, /logout, /cadastro, /perfil, /configuracoes,
                         /gerenciar_usuarios/*, /api/logs/*
    * ``master_bp``    → /master-admin/*
    * ``clientes_bp``  → /clientes, /clientes/novo, /clientes/editar, /clientes/excluir,
                         /cliente/<id>/toggle_ativo, /clientes/<id>/extrato,
                         /clientes/importar, /bulk_delete_clientes,
                         /cliente/<id>/receber_lote
    * ``produtos_bp``  → /produtos, /produtos/exportar_relatorio, /produtos/novo,
                         /produtos/editar, /produtos/excluir, /produtos/importar,
                         /produto/<id>/devolver, /produtos/atualizar_tipo_batch,
                         /bulk_delete_produtos, /fornecedores/*, /tipos/*,
                         /api/produtos/<id>/fotos, /api/produto/<id>

Próximas fases:
    * dashboard_bp, vendas_bp, documentos_bp, caixa_bp.

Convenção:
    Cada blueprint expõe uma única variável module-level ``<nome>_bp`` que
    o ``app.py`` registra via ``app.register_blueprint(...)`` no fim do
    bootstrap. Os handlers continuam reutilizando os helpers definidos em
    ``app.py`` (decorators ``tenant_required``, ``master_required``,
    ``_safe_db_commit``, etc.) via late imports — esses helpers serão
    movidos para um pacote ``services/`` em uma fase futura.

Proteção de tenant:
    Os blueprints de domínio (``produtos_bp``, ``clientes_bp``) aplicam
    ``@tenant_required`` automaticamente via ``before_request``, eliminando
    o risco de esquecer o decorator em rotas novas.
"""

from .auth import auth_bp
from .master import master_bp
from .clientes import clientes_bp
from .produtos import produtos_bp

__all__ = ['auth_bp', 'master_bp', 'clientes_bp', 'produtos_bp']
