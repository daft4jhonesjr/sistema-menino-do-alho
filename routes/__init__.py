"""Pacote de Blueprints (refatoração incremental).

Estado atual:
    * ``auth_bp``        → /login, /logout, /cadastro, /perfil, /configuracoes,
                           /gerenciar_usuarios/*, /api/logs/*
    * ``master_bp``      → /master-admin/*
    * ``clientes_bp``    → /clientes, /clientes/novo, /clientes/editar, /clientes/excluir,
                           /cliente/<id>/toggle_ativo, /clientes/<id>/extrato,
                           /clientes/importar, /bulk_delete_clientes,
                           /cliente/<id>/receber_lote
    * ``produtos_bp``    → /produtos, /produtos/exportar_relatorio, /produtos/novo,
                           /produtos/editar, /produtos/excluir, /produtos/importar,
                           /produto/<id>/devolver, /produtos/atualizar_tipo_batch,
                           /bulk_delete_produtos, /fornecedores/*, /tipos/*,
                           /api/produtos/<id>/fotos, /api/produto/<id>
    * ``vendas_bp``      → /vendas, /vendas/novo, /vendas/editar, /vendas/excluir,
                           /vendas/importar, /vendas/exportar_relatorio,
                           /vendas/<id>/atualizar_situacao_rapida,
                           /venda/excluir_item, /venda/atualizar_status,
                           /venda/adicionar_item, /venda/recibo,
                           /processar_carrinho, /add_venda, /api/pedidos,
                           /vendas/deletar_massa, /logistica, /logistica/toggle,
                           /logistica/bulk_update
    * ``documentos_bp``  → /upload, /processar_documentos, /reprocessar_boletos,
                           /documento/visualizar, /documento/<id>/vincular,
                           /arquivo/<id>/deletar, /arquivos/deletar_em_massa,
                           /arquivos/upload_massa, /arquivos/<id>/debug_texto,
                           /admin/arquivos, /admin/arquivos/deletar_massa,
                           /admin/reprocessar-vencimentos, /admin/raio_x,
                           /admin/resgatar_orfaos, /admin/forcar_leitura_pasta,
                           /admin/limpar_fantasmas, /admin/limpar_vinculos_quebrados,
                           /api/receber_automatico, /api/bot/upload,
                           /venda/<id>/ver_boleto, /venda/<id>/whatsapp,
                           /venda/<id>/ver_nf, /debug/testar_log, /debug-vincular

Próximas fases:
    * dashboard_bp, caixa_bp.

Convenção:
    Cada blueprint expõe uma única variável module-level ``<nome>_bp`` que
    o ``app.py`` registra via ``app.register_blueprint(...)`` no fim do
    bootstrap. Os handlers continuam reutilizando os helpers definidos em
    ``app.py`` (decorators ``tenant_required``, ``master_required``,
    ``_safe_db_commit``, etc.) via late imports — esses helpers serão
    movidos para um pacote ``services/`` em uma fase futura.

Proteção de tenant:
    Os blueprints de domínio (``produtos_bp``, ``clientes_bp``, ``vendas_bp``,
    ``documentos_bp``) aplicam ``@tenant_required`` automaticamente via
    ``before_request``, eliminando o risco de esquecer o decorator em rotas
    novas. ``documentos_bp`` mantém endpoints públicos token-based em uma
    allowlist explícita (bot externo).
"""

from .auth import auth_bp
from .master import master_bp
from .clientes import clientes_bp
from .produtos import produtos_bp
from .vendas import vendas_bp
from .documentos import documentos_bp

__all__ = [
    'auth_bp',
    'master_bp',
    'clientes_bp',
    'produtos_bp',
    'vendas_bp',
    'documentos_bp',
]
