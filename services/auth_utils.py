"""Helpers de autenticação, autorização e perfis.

Re-exporta de ``app.py`` os decorators e helpers usados pelos blueprints.

Decorators:
    * ``tenant_required`` — bloqueia rotas operacionais para usuários sem
      ``empresa_id`` (não-MASTER) e redireciona MASTER para o painel
      master. **Aplicado globalmente** via ``before_request`` em todos os
      blueprints de domínio (``produtos``, ``clientes``, ``vendas``,
      ``documentos``, ``caixa``, ``dashboard``).
    * ``admin_required`` — exige ``perfil == DONO`` (ou ``MASTER``).
    * ``master_required`` — exige ``perfil == MASTER``.

Helpers de propriedade (ownership):
    * ``_e_admin_tenant()`` — current_user é DONO/MASTER no tenant atual.
    * ``_usuario_pode_gerenciar_venda(v)`` — venda pertence ao usuário ou
      o usuário é admin do tenant.
    * ``_usuario_pode_gerenciar_documento(d)`` — idem para documentos.
    * ``_assumir_ownership_venda_orfa(v)`` — adota uma venda sem
      ``usuario_id`` para o ``current_user`` (útil em legados).
    * ``_resposta_sem_permissao()`` — 403 padronizado (HTML/JSON).

Helpers de request:
    * ``_is_ajax()`` — heurística para distinguir XHR de navegação.
    * ``_is_safe_next_url(url)`` — valida ``next=`` para evitar open
      redirect.
    * ``_pos_login_landing(user)`` — retorna a rota destino apropriada
      para o perfil do usuário (DONO → /dashboard, MASTER → /master-admin,
      etc.).
"""

from app import (
    tenant_required,
    admin_required,
    master_required,
    _e_admin_tenant,
    _usuario_pode_gerenciar_venda,
    _usuario_pode_gerenciar_documento,
    _assumir_ownership_venda_orfa,
    _resposta_sem_permissao,
    _is_ajax,
    _is_safe_next_url,
    _pos_login_landing,
)

__all__ = [
    'tenant_required',
    'admin_required',
    'master_required',
    '_e_admin_tenant',
    '_usuario_pode_gerenciar_venda',
    '_usuario_pode_gerenciar_documento',
    '_assumir_ownership_venda_orfa',
    '_resposta_sem_permissao',
    '_is_ajax',
    '_is_safe_next_url',
    '_pos_login_landing',
]
