"""Helpers de banco multi-tenant.

Re-exporta funções definidas em ``app.py`` para que os blueprints possam
fazer ``from services.db_utils import query_tenant, empresa_id_atual,
_safe_db_commit, query_documentos_tenant`` no topo do arquivo, sem
precisar de late imports dentro de cada handler.

Funções:
    * ``query_tenant(model)`` — base query já filtrada por
      ``empresa_id == empresa_id_atual()``. **Nunca** consultar
      diretamente ``Model.query`` em rotas de tenant — sempre via
      ``query_tenant`` para preservar isolamento.
    * ``query_documentos_tenant()`` — variante específica para
      ``Documento`` (que pode ter ``empresa_id`` NULL em registros
      legados; aceita órfãos do próprio tenant).
    * ``empresa_id_atual()`` — id da empresa do ``current_user`` (None
      se MASTER ou anônimo).
    * ``_safe_db_commit() -> (ok, msg_erro)`` — commit blindado contra
      ``IntegrityError``/``OperationalError`` com rollback automático.
"""

from app import (
    query_tenant,
    query_documentos_tenant,
    empresa_id_atual,
    _safe_db_commit,
)

__all__ = [
    'query_tenant',
    'query_documentos_tenant',
    'empresa_id_atual',
    '_safe_db_commit',
]
