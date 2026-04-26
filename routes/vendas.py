"""Blueprint ``vendas`` — CRUD operacional de Vendas/Pedidos.

Rotas extraídas do legado ``app.py`` (Fase 3 da refatoração):
    * GET  /vendas                                  listar_vendas
    * POST /vendas/exportar_relatorio               exportar_relatorio_vendas
    * GET  /logistica                               logistica
    * POST /logistica/toggle/<venda_id>             toggle_entrega
    * POST /logistica/bulk_update                   logistica_bulk_update
    * GET/POST /vendas/novo                         nova_venda
    * POST /add_venda                               add_venda             (alias AJAX)
    * POST /processar_carrinho                      processar_carrinho
    * POST /venda/adicionar_item                    venda_adicionar_item
    * GET/POST /vendas/editar/<id>                  editar_venda
    * POST /venda/excluir_item/<id>                 excluir_item_venda
    * POST /vendas/excluir/<id>                     excluir_venda
    * POST /venda/atualizar_status/<id_venda>       atualizar_status_venda
    * POST /vendas/<id>/atualizar_situacao_rapida   atualizar_situacao_rapida
    * GET  /venda/recibo/<id>                       recibo_venda
    * GET  /api/pedidos                             api_pedidos
    * POST /vendas/deletar_massa                    vendas_deletar_massa
    * GET/POST /vendas/importar                     importar_vendas       (admin)

Endpoints novos: prefixo ``vendas.`` (ex.: ``vendas.listar_vendas``).

Proteção automática de tenant
-----------------------------
Toda rota deste blueprint exige ``login_required`` + ``tenant_required``,
aplicados via ``before_request``. Rotas que precisam de privilégio extra
(ex.: ``importar_vendas``) mantêm o ``@admin_required`` no handler.

Helpers compartilhados (``_vendas_do_pedido``, ``_apagar_lancamentos_caixa_por_vendas``)
permanecem em ``app.py`` e são importados via late import porque são usados
por documentos e pelo módulo de processamento automático de PDFs.
"""
from datetime import date, datetime
from decimal import Decimal
from math import ceil
import csv
import io
import os
import re

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, session, current_app, Response,
)
from flask_login import current_user
from sqlalchemy import asc, desc, extract, func, or_
from sqlalchemy.orm import joinedload
import pandas as pd
from werkzeug.utils import secure_filename

from models import db, Cliente, Produto, Venda, Documento, LancamentoCaixa
from services.auth_utils import (
    tenant_required, admin_required, _is_ajax,
    _e_admin_tenant, _usuario_pode_gerenciar_venda,
    _resposta_sem_permissao, _assumir_ownership_venda_orfa,
)
from services.db_utils import (
    query_tenant, query_documentos_tenant, empresa_id_atual,
)
from services.cache_utils import limpar_cache_dashboard
from services.config_helpers import (
    registrar_log, get_hoje_brasil,
)
from services.files_utils import _deletar_cloudinary_seguro
from services.vendas_services import (
    _vendas_do_pedido, _apagar_lancamentos_caixa_por_vendas,
    _produto_com_lock,
)
from services.csv_utils import (
    _msg_linha, _strip_quotes,
    _normalizar_nome_busca, _parse_preco, _parse_quantidade,
    _parse_data_flex,
)
# ``_limpar_valor_moeda`` é helper nativo do livro caixa, reutilizado
# aqui em formulários monetários.
from routes.caixa import _limpar_valor_moeda


vendas_bp = Blueprint('vendas', __name__)


# ============================================================
# Proteção automática de tenant para todo o blueprint
# ============================================================
@vendas_bp.before_request
def _exigir_tenant_em_todas_rotas():
    """Aplica ``@login_required`` + ``@tenant_required`` em todas as rotas
    deste blueprint via hook centralizado."""
    @tenant_required
    def _ok():
        return None

    return _ok()


# ============================================================
# Helpers exclusivos de vendas (importação CSV/TSV)
# ============================================================

# Mapeamento posicional para importação de vendas em formato TSV/raw
# (sem cabeçalho). Index 4 = Valor Total (ignorado).
_VENDAS_RAW_IMPORT_MAP = [
    ('cliente', 0),
    ('nf', 1),
    ('preco_venda', 2),
    ('quantidade', 3),
    None,  # Index 4: Valor Total (ignorar)
    ('produto', 5),
    ('data_venda', 6),
    ('empresa', 7),
    ('situacao', 8),
    ('forma_pagamento', 9),
]


def _parse_nf_vendas(raw):
    """Converte valor de NF para string armazenável.
    S/N, Falta_nota, vazio ou não numérico → '0'."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return '0'
    s = str(raw).strip().upper()
    if not s or s in ('S/N', 'FALTA_NOTA', 'FALTA NOTA'):
        return '0'
    only_digits = re.sub(r'[\s.,]', '', s)
    if only_digits.isdigit():
        return only_digits.lstrip('0') or '0'
    return '0'


def _normalizar_situacao_vendas(s):
    """Correção automática: PENDETE -> PENDENTE."""
    if not s or (isinstance(s, float) and pd.isna(s)):
        return ''
    u = str(s).strip().upper()
    if u == 'PENDETE':
        return 'PENDENTE'
    return u


def _load_csv_vendas_flexible(filepath):
    """Carrega CSV/TSV de importação de vendas com detecção de formato.
    - Se a primeira linha contiver TAB, usa TSV (tab). Caso contrário, usa vírgula.
    - Se a primeira linha contiver 'R$', assume formato raw (sem cabeçalho) e
      mapeamento posicional.
    Retorna (df, is_raw). Em modo raw, df já tem colunas canônicas e
    NF/situação normalizados."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception:
        return None, False
    lines = [ln.strip() for ln in content.strip().splitlines() if ln.strip()]
    if not lines:
        return None, False
    first_line = lines[0]
    sep = '\t' if '\t' in first_line else ','
    is_raw = 'R$' in first_line
    if is_raw:
        rows = []
        reader = csv.reader(io.StringIO(content), delimiter=sep, quotechar='"')
        for row in reader:
            if not row:
                continue
            d = {}
            for entry in _VENDAS_RAW_IMPORT_MAP:
                if entry is None:
                    continue
                key, idx = entry
                raw_val = row[idx] if idx < len(row) else ''
                if key == 'nf':
                    d[key] = _parse_nf_vendas(raw_val)
                elif key == 'situacao':
                    d[key] = _normalizar_situacao_vendas(raw_val)
                else:
                    d[key] = raw_val
            rows.append(d)
        df = pd.DataFrame(rows)
        return df, True
    df = pd.read_csv(io.StringIO(content), sep=sep, engine='python', quoting=csv.QUOTE_MINIMAL, on_bad_lines='warn')
    return df, False


# ============================================================
# Rotas
# ============================================================

@vendas_bp.route('/vendas')
def listar_vendas():
    """
    Lista vendas do ano ativo com filtros e agrupamento por pedido.

    Query params: produto_id, cliente_id, filtro (geral|bacalhau),
    ordenar_por, ordem_data, filtro_vencidos, forma_pagto.
    """
    produto_id = request.args.get('produto_id', type=int)
    cliente_id = request.args.get('cliente_id', type=int)
    filtro_vencidos = request.args.get('filtro_vencidos', type=int) == 1
    filtro = (request.args.get('filtro') or 'geral').strip().lower()
    forma_pagto = (request.args.get('forma_pagto') or 'Todas').strip().upper()
    if forma_pagto in ('', 'TODAS'):
        forma_pagto = 'TODAS'
    if filtro not in ('geral', 'bacalhau'):
        filtro = 'geral'

    ordenar_por = request.args.get('ordenar_por')
    sort = (request.args.get('sort') or '').strip().lower()
    if sort not in ('cliente_asc', 'cliente_desc'):
        sort = ''
    ordem_data = (request.args.get('ordem_data') or 'decrescente').strip().lower()
    if ordem_data not in ('crescente', 'decrescente', 'vencimento_crescente', 'vencimento_decrescente'):
        ordem_data = 'decrescente'

    ano_ativo = session.get('ano_ativo', datetime.now().year)

    subq_ids = db.session.query(Venda.id).filter(
        Venda.empresa_id == empresa_id_atual(),
        extract('year', Venda.data_venda) == ano_ativo,
    )
    filtro_bacalhau_expr = or_(
        Produto.nome_produto.ilike('%bacalhau%'),
        Produto.tipo.ilike('%bacalhau%'),
    )
    subq_ids = subq_ids.join(Produto, Venda.produto_id == Produto.id)
    if filtro == 'bacalhau':
        subq_ids = subq_ids.filter(filtro_bacalhau_expr)
    else:
        subq_ids = subq_ids.filter(~filtro_bacalhau_expr)
    if produto_id:
        subq_ids = subq_ids.filter(Venda.produto_id == produto_id)
    if cliente_id:
        subq_ids = subq_ids.filter(Venda.cliente_id == cliente_id)
    subq_ids_select = subq_ids.order_by(desc(Venda.data_venda), desc(Venda.id)).limit(1000).with_entities(Venda.id)

    query = query_tenant(Venda).options(
        joinedload(Venda.cliente),
        joinedload(Venda.produto),
    ).filter(Venda.id.in_(subq_ids_select))
    if forma_pagto != 'TODAS':
        query = query.filter(func.upper(func.coalesce(Venda.forma_pagamento, '')) == forma_pagto)

    if ordem_data == 'crescente':
        vendas_raw = query.order_by(
            Venda.cliente_id,
            asc(Venda.data_venda),
            Venda.nf,
            asc(Venda.id),
        ).all()
    else:
        vendas_raw = query.order_by(
            Venda.cliente_id,
            desc(Venda.data_venda),
            Venda.nf,
            desc(Venda.id),
        ).all()

    pedidos_dict = {}

    def _nome_cliente_exibicao(venda_obj):
        nome = str(venda_obj.cliente.nome_cliente if venda_obj.cliente else 'Cliente').strip()
        avulso = str(getattr(venda_obj, 'cliente_avulso', '') or '').strip()
        if 'DESCONHECIDO' in nome.upper() and avulso:
            return f'{nome} ({avulso})'
        return nome

    for venda in vendas_raw:
        cnpj_cliente = venda.cliente.cnpj or ''
        is_consumidor_final = cnpj_cliente in ('0', '00000000000000', '')
        data_venda_normalizada = venda.data_venda.date() if hasattr(venda.data_venda, 'date') else venda.data_venda
        if is_consumidor_final:
            avulso_norm = str(getattr(venda, 'cliente_avulso', '') or '').strip().upper()
            pedido_key = (venda.cliente_id, data_venda_normalizada, avulso_norm)
        else:
            nf_normalizada = str(venda.nf).strip() if venda.nf else ''
            pedido_key = (venda.cliente_id, nf_normalizada, data_venda_normalizada)

        if pedido_key not in pedidos_dict:
            pedidos_dict[pedido_key] = {
                'key': pedido_key,
                'cliente_id': venda.cliente_id,
                'cliente_nome': _nome_cliente_exibicao(venda),
                'cliente_cnpj': cnpj_cliente,
                'nf': venda.nf or '-',
                'data_venda': venda.data_venda,
                'empresa_faturadora': venda.empresa_faturadora,
                'situacao': venda.situacao,
                'forma_pagamento': getattr(venda, 'forma_pagamento', None),
                'is_consumidor_final': is_consumidor_final,
                'vendas': [],
                'total_quantidade': 0,
                'total_valor': 0,
                'total_lucro': 0,
                'primeira_venda_id': venda.id,
            }

        pedidos_dict[pedido_key]['vendas'].append(venda)
        pedidos_dict[pedido_key]['total_quantidade'] += venda.quantidade_venda
        pedidos_dict[pedido_key]['total_valor'] += float(venda.calcular_total())
        pedidos_dict[pedido_key]['total_lucro'] += float(venda.calcular_lucro())
        pedidos_dict[pedido_key]['total_valor_pago'] = pedidos_dict[pedido_key].get('total_valor_pago', 0) + float(getattr(venda, 'valor_pago', None) or 0)

    pedidos_agrupados = []
    pedidos_keys_vistos = set()
    for venda in vendas_raw:
        cnpj_cliente = venda.cliente.cnpj or ''
        is_consumidor_final = cnpj_cliente in ('0', '00000000000000', '')
        data_venda_normalizada = venda.data_venda.date() if hasattr(venda.data_venda, 'date') else venda.data_venda
        if is_consumidor_final:
            avulso_norm = str(getattr(venda, 'cliente_avulso', '') or '').strip().upper()
            pedido_key = (venda.cliente_id, data_venda_normalizada, avulso_norm)
        else:
            nf_normalizada = str(venda.nf).strip() if venda.nf else ''
            pedido_key = (venda.cliente_id, nf_normalizada, data_venda_normalizada)

        if pedido_key not in pedidos_keys_vistos:
            pedidos_agrupados.append(pedidos_dict[pedido_key])
            pedidos_keys_vistos.add(pedido_key)

    if not ordenar_por and ordem_data in ('crescente', 'decrescente'):
        reverse_order = (ordem_data == 'decrescente')
        pedidos_agrupados.sort(
            key=lambda x: x['data_venda'].date() if hasattr(x['data_venda'], 'date') else x['data_venda'],
            reverse=reverse_order,
        )

    docs_por_venda = {}
    all_venda_ids = [vv.id for pedido in pedidos_agrupados for vv in pedido.get('vendas', [])]
    if all_venda_ids:
        # Auditoria P0 (A2): escopo explícito ao tenant atual.
        docs_vinculados = query_documentos_tenant().filter(
            Documento.venda_id.in_(all_venda_ids)
        ).order_by(desc(Documento.id)).all()
        for doc in docs_vinculados:
            docs_por_venda.setdefault(doc.venda_id, []).append(doc)

    _todos_caminhos_boleto = set()
    _todos_caminhos_nf = set()
    for _pedido in pedidos_agrupados:
        for _v in _pedido.get('vendas', []):
            _cb = (_v.caminho_boleto or '').strip()
            _cn = (_v.caminho_nf or '').strip()
            if _cb:
                _todos_caminhos_boleto.add(_cb)
            if _cn:
                _todos_caminhos_nf.add(_cn)

    _todos_caminhos = _todos_caminhos_boleto | _todos_caminhos_nf
    _docs_por_caminho: dict = {}
    if _todos_caminhos:
        _docs_pre = query_documentos_tenant().filter(
            Documento.caminho_arquivo.in_(list(_todos_caminhos))
        ).all()
        _docs_por_caminho = {(d.caminho_arquivo or '').strip(): d for d in _docs_pre}

    for pedido in pedidos_agrupados:
        cb, cn = None, None
        doc_boleto, doc_nf = None, None

        for v in pedido.get('vendas', []):
            caminho_b = (v.caminho_boleto or '').strip()
            if caminho_b:
                doc = _docs_por_caminho.get(caminho_b)
                if doc:
                    cb = caminho_b
                    doc_boleto = doc
                    break
                else:
                    v.caminho_boleto = None
                    db.session.flush()

        for v in pedido.get('vendas', []):
            caminho_n = (v.caminho_nf or '').strip()
            if caminho_n:
                doc = _docs_por_caminho.get(caminho_n)
                if doc:
                    cn = caminho_n
                    doc_nf = doc
                    break
                else:
                    v.caminho_nf = None
                    db.session.flush()

        pedido['caminho_boleto'] = cb
        pedido['caminho_nf'] = cn
        pedido['doc_boleto'] = doc_boleto
        pedido['doc_nf'] = doc_nf
        docs_do_pedido = []
        for vv in pedido.get('vendas', []):
            docs_do_pedido.extend(docs_por_venda.get(vv.id, []))
        pedido['tem_documentos'] = bool(docs_do_pedido or doc_boleto or doc_nf or cb or cn)
        pedido['primeiro_documento_id'] = docs_do_pedido[0].id if docs_do_pedido else (doc_nf.id if doc_nf else (doc_boleto.id if doc_boleto else None))
        vendas_do_pedido = pedido.get('vendas', [])
        pedido['tem_item_perda'] = any(
            str(getattr(v, 'tipo_operacao', 'VENDA') or 'VENDA').upper() == 'PERDA'
            for v in vendas_do_pedido
        )
        pedido['tem_item_venda'] = any(
            str(getattr(v, 'tipo_operacao', 'VENDA') or 'VENDA').upper() != 'PERDA'
            for v in vendas_do_pedido
        )
        total_valor_pedido = float(pedido.get('total_valor') or 0)
        if total_valor_pedido <= 0 and pedido['tem_item_perda']:
            pedido['situacao'] = 'PERDA'
        else:
            situacoes_financeiras = [
                str(v.situacao or '').strip().upper()
                for v in vendas_do_pedido
                if str(getattr(v, 'tipo_operacao', 'VENDA') or 'VENDA').upper() != 'PERDA'
            ]
            if not situacoes_financeiras:
                situacoes_financeiras = [str(v.situacao or '').strip().upper() for v in vendas_do_pedido]
            if any(s == 'PENDENTE' for s in situacoes_financeiras):
                pedido['situacao'] = 'PENDENTE'
            elif any(s == 'PARCIAL' for s in situacoes_financeiras):
                pedido['situacao'] = 'PARCIAL'
            else:
                pedido['situacao'] = 'PAGO'
        if 'total_valor_pago' not in pedido:
            pedido['total_valor_pago'] = sum(float(getattr(v, 'valor_pago', None) or 0) for v in pedido.get('vendas', []))
        dv = None
        for vv in pedido.get('vendas', []):
            if getattr(vv, 'data_vencimento', None) is not None:
                dv = vv.data_vencimento
                break
        if dv is None and doc_boleto and getattr(doc_boleto, 'data_vencimento', None) is not None:
            dv = doc_boleto.data_vencimento
        pedido['data_vencimento'] = dv
        hoje = get_hoje_brasil()
        pedido['is_vencido'] = (
            pedido.get('situacao') in ('PENDENTE', 'PARCIAL') and
            dv is not None and
            dv < hoje
        )
        pedido['is_vencido_para_abatimento'] = (
            pedido.get('situacao') in ('PENDENTE', 'PARCIAL') and
            dv is not None and
            dv < hoje
        )

    n_nf = sum(1 for p in pedidos_agrupados if (p.get('caminho_nf') or '').strip())  # noqa: F841
    n_boleto = sum(1 for p in pedidos_agrupados if (p.get('caminho_boleto') or '').strip())  # noqa: F841

    if sort == 'cliente_asc':
        pedidos_agrupados.sort(key=lambda x: str(x.get('cliente_nome') or '').upper())
    elif sort == 'cliente_desc':
        pedidos_agrupados.sort(key=lambda x: str(x.get('cliente_nome') or '').upper(), reverse=True)
    elif ordenar_por == 'situacao':
        pedidos_agrupados.sort(
            key=lambda x: (x.get('data_venda').date() if hasattr(x.get('data_venda'), 'date') else x.get('data_venda') or date.min),
            reverse=True,
        )
        pedidos_agrupados.sort(key=lambda x: str(x.get('situacao') or ''))
    elif ordenar_por == 'forma_pagamento':
        pedidos_agrupados.sort(
            key=lambda x: (x.get('data_venda').date() if hasattr(x.get('data_venda'), 'date') else x.get('data_venda') or date.min),
            reverse=True,
        )
        pedidos_agrupados.sort(key=lambda x: str(x.get('forma_pagamento') or ''))
    elif ordem_data == 'vencimento_crescente':
        pedidos_agrupados.sort(
            key=lambda x: (x.get('data_vencimento') is None, x.get('data_vencimento') or date.max)
        )
        pedidos_agrupados.sort(
            key=lambda x: 1 if str(x.get('situacao') or '').upper() == 'PAGO' else 0
        )
    elif ordem_data == 'vencimento_decrescente':
        pedidos_agrupados.sort(
            key=lambda x: (x.get('data_vencimento') is None, x.get('data_vencimento') or date.min),
            reverse=True,
        )
        pedidos_agrupados.sort(
            key=lambda x: 1 if str(x.get('situacao') or '').upper() == 'PAGO' else 0
        )

    if filtro_vencidos:
        pedidos_agrupados = [p for p in pedidos_agrupados if p.get('is_vencido_para_abatimento')]

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    page = request.args.get('page', 1, type=int)
    per_page = 20

    total_pedidos = len(pedidos_agrupados)
    total_pages = ceil(total_pedidos / per_page) if total_pedidos > 0 else 1

    if page < 1:
        page = 1
    elif page > total_pages and total_pages > 0:
        page = total_pages

    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    pedidos_paginados = pedidos_agrupados[start_idx:end_idx]

    class Pagination:
        def __init__(self, page, per_page, total, items):
            self.page = page
            self.per_page = per_page
            self.total = total
            self.items = items
            self.pages = total_pages
            self.has_prev = page > 1
            self.has_next = page < total_pages
            self.prev_num = page - 1 if self.has_prev else None
            self.next_num = page + 1 if self.has_next else None

    pagination = Pagination(page, per_page, total_pedidos, pedidos_paginados)

    is_ajax = request.args.get('ajax', type=int) == 1

    if is_ajax:
        rows_html = render_template('_linhas_venda.html', pedidos=pedidos_paginados, current_page=page)
        cards_html = render_template('_cards_venda.html', pedidos=pedidos_paginados)
        return jsonify(rows=rows_html, cards=cards_html)

    produto_filtro = None
    cliente_filtro = None

    if produto_id:
        produto_filtro = query_tenant(Produto).filter_by(id=produto_id).first()

    if cliente_id:
        cliente_filtro = query_tenant(Cliente).filter_by(id=cliente_id).first()

    clientes = query_tenant(Cliente).filter(Cliente.ativo.is_(True)).order_by(Cliente.nome_cliente).limit(500).all()
    produtos = query_tenant(Produto).filter(Produto.estoque_atual > 0).order_by(Produto.nome_produto).limit(500).all()
    todos_clientes = query_tenant(Cliente).order_by(Cliente.nome_cliente).limit(500).all()
    todos_produtos = query_tenant(Produto).order_by(Produto.nome_produto).limit(500).all()

    graficos_data = {'situacao': {}, 'pagamento': {}, 'empresa': {}}
    for v in vendas_raw:
        total_venda = float(v.calcular_total() or 0)

        situacao = str(v.situacao or '').strip()
        if situacao:
            bucket = graficos_data['situacao'].setdefault(situacao, {'count': 0, 'total': 0.0})
            bucket['count'] += 1
            bucket['total'] += total_venda

        forma_pag = str(v.forma_pagamento or '').strip()
        if forma_pag:
            bucket = graficos_data['pagamento'].setdefault(forma_pag, {'count': 0, 'total': 0.0})
            bucket['count'] += 1
            bucket['total'] += total_venda

        empresa = str(v.empresa_faturadora or '').strip()
        if empresa:
            bucket = graficos_data['empresa'].setdefault(empresa, {'count': 0, 'total': 0.0})
            bucket['count'] += 1
            bucket['total'] += total_venda

    return render_template(
        'vendas/listar.html',
        pedidos=pedidos_paginados,
        pagination=pagination,
        produto_filtro=produto_filtro,
        cliente_filtro=cliente_filtro,
        clientes=clientes,
        produtos=produtos,
        todos_clientes=todos_clientes,
        todos_produtos=todos_produtos,
        ordem_data=ordem_data,
        ordenar_por=ordenar_por,
        sort=sort,
        forma_pagto=forma_pagto,
        filtro=filtro,
        filtro_vencidos=filtro_vencidos,
        graficos_data=graficos_data,
    )


@vendas_bp.route('/vendas/exportar_relatorio', methods=['POST'])
def exportar_relatorio_vendas():
    ano_ativo = session.get('ano_ativo', datetime.now().year)
    filtro_empresa = (request.form.get('filtro_empresa') or 'TODAS').strip().upper()
    filtro_situacao = (request.form.get('filtro_situacao') or 'TODAS').strip().upper()
    filtro_forma_pagamento = (request.form.get('filtro_forma_pagamento') or 'TODAS').strip().upper()
    filtro_mes_raw = (request.form.get('filtro_mes') or '').strip()
    filtro_mes = None
    if filtro_mes_raw:
        try:
            mes_int = int(filtro_mes_raw)
            if 1 <= mes_int <= 12:
                filtro_mes = mes_int
        except (TypeError, ValueError):
            filtro_mes = None
    colunas_solicitadas = request.form.getlist('colunas')

    colunas_disponiveis = {
        'data': 'Data',
        'cliente': 'Cliente',
        'nf': 'NF',
        'preco_unit': 'Preco Unit.',
        'qtd': 'Qtd',
        'valor_total': 'Valor Total',
        'lucro': 'Lucro',
        'vencimento': 'Vencimento',
        'empresa': 'Empresa',
        'situacao': 'Situacao',
        'forma_pagto': 'Forma Pagto',
    }
    ordem_padrao_colunas = [
        'data', 'cliente', 'nf', 'preco_unit', 'qtd', 'valor_total',
        'lucro', 'vencimento', 'empresa', 'situacao', 'forma_pagto',
    ]

    colunas = [c for c in ordem_padrao_colunas if c in colunas_solicitadas and c in colunas_disponiveis]
    if not colunas:
        colunas = ordem_padrao_colunas

    query = query_tenant(Venda).options(
        joinedload(Venda.cliente),
        joinedload(Venda.produto),
    ).filter(
        extract('year', Venda.data_venda) == ano_ativo
    )

    if filtro_empresa != 'TODAS':
        query = query.filter(func.upper(func.coalesce(Venda.empresa_faturadora, 'NENHUM')) == filtro_empresa)
    if filtro_situacao != 'TODAS':
        query = query.filter(func.upper(func.coalesce(Venda.situacao, '')) == filtro_situacao)
    if filtro_forma_pagamento != 'TODAS':
        query = query.filter(func.upper(func.coalesce(Venda.forma_pagamento, '')) == filtro_forma_pagamento)
    if filtro_mes is not None:
        query = query.filter(extract('month', Venda.data_venda) == filtro_mes)

    vendas = query.order_by(Venda.data_venda.desc(), Venda.id.desc()).all()
    if not _e_admin_tenant():
        vendas = [v for v in vendas if _usuario_pode_gerenciar_venda(v)]

    def _fmt_num(valor):
        try:
            numero = Decimal(str(valor or 0))
        except Exception:
            numero = Decimal('0.00')
        return f"{numero:.2f}".replace('.', ',')

    def _fmt_data(valor):
        if not valor:
            return ''
        return valor.strftime('%d/%m/%Y') if hasattr(valor, 'strftime') else str(valor)

    def _csv_safe(valor):
        s = '' if valor is None else str(valor)
        return "'" + s if s[:1] in ('=', '+', '-', '@') else s

    output = io.StringIO()
    output.write('\ufeff')
    writer = csv.writer(output, delimiter=';')
    writer.writerow([colunas_disponiveis[c] for c in colunas])

    soma_qtd = 0
    soma_valor_total = Decimal('0.0')
    soma_lucro = Decimal('0.0')

    for venda in vendas:
        qtd_venda = int(getattr(venda, 'quantidade_venda', 0) or 0)
        try:
            valor_total_venda = Decimal(str(venda.calcular_total() or Decimal('0.00')))
        except Exception:
            valor_total_venda = Decimal('0.00')
        try:
            lucro_venda = Decimal(str(venda.calcular_lucro() or Decimal('0.00')))
        except Exception:
            lucro_venda = Decimal('0.00')

        soma_qtd += qtd_venda
        soma_valor_total += valor_total_venda
        soma_lucro += lucro_venda

        cliente_nome = venda.cliente.nome_cliente if venda.cliente else (getattr(venda, 'cliente_avulso', None) or '-')
        linha = {
            'data': _fmt_data(venda.data_venda),
            'cliente': _csv_safe(cliente_nome),
            'nf': _csv_safe(venda.nf or '-'),
            'preco_unit': _fmt_num(getattr(venda, 'preco_venda', 0)),
            'qtd': str(qtd_venda),
            'valor_total': _fmt_num(valor_total_venda),
            'lucro': _fmt_num(lucro_venda),
            'vencimento': _fmt_data(getattr(venda, 'data_vencimento', None)),
            'empresa': _csv_safe(venda.empresa_faturadora or 'NENHUM'),
            'situacao': _csv_safe(venda.situacao or ''),
            'forma_pagto': _csv_safe(venda.forma_pagamento or ''),
        }
        writer.writerow([linha[c] for c in colunas])

    def _fmt_total_br(valor):
        try:
            numero = Decimal(str(valor or 0))
        except Exception:
            numero = Decimal('0.00')
        return f"{numero:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

    linha_total = [''] * len(colunas)
    if linha_total:
        linha_total[0] = 'TOTAL GERAL'
    if 'qtd' in colunas:
        linha_total[colunas.index('qtd')] = str(soma_qtd)
    if 'valor_total' in colunas:
        linha_total[colunas.index('valor_total')] = _fmt_total_br(soma_valor_total)
    if 'lucro' in colunas:
        linha_total[colunas.index('lucro')] = _fmt_total_br(soma_lucro)
    writer.writerow(linha_total)

    csv_content = output.getvalue()
    output.close()

    data_hoje = datetime.now().strftime('%d-%m-%Y')
    partes_nome = ['relatorio_vendas', data_hoje]

    def _normalizar_nome_arquivo(parte):
        txt = str(parte or '').strip().upper().replace(' ', '_')
        txt = re.sub(r'[^A-Z0-9_\-]', '', txt)
        return txt

    if filtro_empresa and filtro_empresa != 'TODAS':
        partes_nome.append(_normalizar_nome_arquivo(filtro_empresa))
    if filtro_situacao and filtro_situacao != 'TODAS':
        partes_nome.append(_normalizar_nome_arquivo(filtro_situacao))
    if filtro_forma_pagamento and filtro_forma_pagamento != 'TODAS':
        partes_nome.append(_normalizar_nome_arquivo(filtro_forma_pagamento))
    if filtro_mes is not None:
        partes_nome.append(f"MES_{filtro_mes:02d}")

    nome_arquivo = f"{'_'.join([p for p in partes_nome if p])}.csv"

    return Response(
        csv_content,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={nome_arquivo}'},
    )


@vendas_bp.route('/logistica')
def logistica():
    """Roteirizador de Entregas: lista cada venda individualmente por status de entrega."""
    filtro_status = request.args.get('status', 'PENDENTE')
    if filtro_status not in ('PENDENTE', 'ENTREGUE'):
        filtro_status = 'PENDENTE'

    page = request.args.get('page', 1, type=int)
    per_page = 20
    is_ajax = (
        request.args.get('ajax') == '1'
        or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    )

    vendas = query_tenant(Venda).filter_by(status_entrega=filtro_status).options(
        joinedload(Venda.cliente),
        joinedload(Venda.produto),
    ).order_by(Venda.data_venda.desc()).all()

    pedidos_dict = {}
    pedidos_ordenados_keys = []

    for v in vendas:
        cliente = v.cliente
        if not cliente:
            continue

        cnpj_cliente = (cliente.cnpj or '').strip()
        is_consumidor_final = cnpj_cliente in ('0', '00000000000000', '')
        data_venda_normalizada = v.data_venda.date() if hasattr(v.data_venda, 'date') else v.data_venda
        if is_consumidor_final:
            pedido_key = (v.cliente_id, data_venda_normalizada)
        else:
            nf_normalizada = str(v.nf).strip() if v.nf else ''
            pedido_key = (v.cliente_id, nf_normalizada, data_venda_normalizada)

        if pedido_key not in pedidos_dict:
            pedidos_dict[pedido_key] = {
                'pedido_key': str(pedido_key),
                'ids': [],
                'venda_id': v.id,
                'data': v.data_venda.strftime('%d/%m/%Y'),
                'cliente_nome': cliente.nome_cliente or 'Sem Nome',
                'endereco': cliente.endereco or '',
                'produtos': [],
                'total': 0.0,
                'status_entrega': v.status_entrega or 'PENDENTE',
            }
            pedidos_ordenados_keys.append(pedido_key)

        produto_nome = v.produto.nome_produto if v.produto else 'Item'
        pedidos_dict[pedido_key]['ids'].append(v.id)
        pedidos_dict[pedido_key]['produtos'].append(f"{v.quantidade_venda}x {produto_nome}")
        pedidos_dict[pedido_key]['total'] += float(v.calcular_total())

    pedidos_agrupados = [pedidos_dict[k] for k in pedidos_ordenados_keys]
    total_pedidos = len(pedidos_agrupados)
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    entregas = pedidos_agrupados[start_idx:end_idx]
    has_next = end_idx < total_pedidos

    if is_ajax:
        return jsonify({
            'success': True,
            'entregas': entregas,
            'has_next': has_next,
            'page': page,
            'status': filtro_status,
        })

    return render_template(
        'logistica.html',
        entregas=entregas,
        filtro_status=filtro_status,
        has_next_logistica=has_next,
    )


@vendas_bp.route('/logistica/toggle/<int:venda_id>', methods=['POST'])
def toggle_entrega(venda_id):
    """Alterna o status de entrega entre PENDENTE e ENTREGUE."""
    ids_raw = (request.form.get('ids') or '').strip()
    ids = []
    if ids_raw:
        for p in ids_raw.split(','):
            p = p.strip()
            if not p:
                continue
            try:
                ids.append(int(p))
            except ValueError:
                continue
    if not ids:
        ids = [venda_id]

    venda_ref = query_tenant(Venda).filter_by(id=ids[0]).first_or_404()
    if not _e_admin_tenant() and not _usuario_pode_gerenciar_venda(venda_ref):
        flash('Você não tem permissão para alterar o status desta venda.', 'error')
        return redirect(url_for('vendas.logistica'))
    status = request.form.get('status', request.args.get('status', 'PENDENTE'))
    try:
        novo_status = 'ENTREGUE' if (venda_ref.status_entrega or 'PENDENTE') == 'PENDENTE' else 'PENDENTE'
        query_tenant(Venda).filter(Venda.id.in_(ids)).update({'status_entrega': novo_status}, synchronize_session=False)
        db.session.commit()
        flash('Status de entrega atualizado com sucesso!', 'success')
    except Exception:
        db.session.rollback()
        flash('Erro ao atualizar status de entrega. Tente novamente.', 'error')
    return redirect(url_for('vendas.logistica', status=status))


@vendas_bp.route('/logistica/bulk_update', methods=['POST'])
def logistica_bulk_update():
    """Atualiza status de entrega de vários pedidos de uma vez (ação em massa)."""
    dados = request.get_json() or {}
    ids_raw = dados.get('ids', [])
    novo_status = dados.get('status')

    if not ids_raw or not novo_status:
        return jsonify({'success': False, 'message': 'Dados inválidos.'}), 400

    try:
        ids = [int(x) for x in ids_raw]
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'IDs inválidos.'}), 400
    if novo_status not in ('PENDENTE', 'ENTREGUE'):
        return jsonify({'success': False, 'message': 'Status inválido.'}), 400

    try:
        vendas_solicitadas = query_tenant(Venda).filter(Venda.id.in_(ids)).all()

        if _e_admin_tenant():
            ids_permitidos = [v.id for v in vendas_solicitadas]
        else:
            ids_permitidos = [
                v.id for v in vendas_solicitadas
                if _usuario_pode_gerenciar_venda(v)
            ]

        if not ids_permitidos:
            return jsonify({'success': False, 'message': 'Sem permissão para alterar estas vendas.'}), 403

        atualizados = query_tenant(Venda).filter(Venda.id.in_(ids_permitidos)).update(
            {'status_entrega': novo_status}, synchronize_session=False
        )
        db.session.commit()
        flash(f'{atualizados} pedido(s) atualizado(s) com sucesso!', 'success')
        return jsonify({'success': True, 'atualizados': atualizados})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


@vendas_bp.route('/vendas/novo', methods=['GET', 'POST'])
def nova_venda():
    # B1 (auditoria): Carregar listas de clientes ativos e produtos com estoque
    # APENAS UMA VEZ por request. Antes, cada validação que falhava recarregava
    # ambas as listas, causando até 8 round-trips ao banco em fluxos de erro.
    _cache_form = {}

    def _carregar_listas_form():
        if 'clientes' not in _cache_form:
            _cache_form['clientes'] = (
                query_tenant(Cliente)
                .filter(Cliente.ativo.is_(True))
                .order_by(Cliente.nome_cliente)
                .limit(1000)
                .all()
            )
            _cache_form['produtos'] = (
                query_tenant(Produto)
                .filter(Produto.estoque_atual > 0)
                .order_by(Produto.nome_produto)
                .limit(500)
                .all()
            )
        return _cache_form['clientes'], _cache_form['produtos']

    def _render_form():
        clientes, produtos = _carregar_listas_form()
        return render_template('vendas/formulario.html', venda=None, clientes=clientes, produtos=produtos)

    if request.method == 'POST':
        try:
            produto_id = int(request.form.get('produto_id', 0))
            quantidade_venda = int(request.form.get('quantidade_venda', 0))
        except (ValueError, TypeError):
            msg = 'Produto e quantidade são obrigatórios.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            return _render_form()

        if quantidade_venda <= 0:
            msg = 'A quantidade deve ser maior que zero (mesmo para perdas).'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return _render_form()

        produto = _produto_com_lock(produto_id)
        if not produto:
            msg = 'Produto não encontrado.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 404
            flash(msg, 'error')
            return redirect(url_for('vendas.listar_vendas'))
        if produto.estoque_atual < quantidade_venda:
            msg = f'Estoque insuficiente! Disponível: {produto.estoque_atual}'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            return _render_form()

        cliente_id_raw = request.form.get('cliente_id')
        data_venda_raw = request.form.get('data_venda')
        empresa_faturadora = request.form.get('empresa_faturadora', 'PATY')
        situacao = request.form.get('situacao', 'PENDENTE')
        try:
            cliente_id = int(cliente_id_raw) if cliente_id_raw else None
        except (ValueError, TypeError):
            cliente_id = None
        cliente_avulso_raw = (request.form.get('cliente_avulso') or '').strip()
        if not cliente_id:
            msg = 'Cliente é obrigatório.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            return _render_form()
        cliente_obj = query_tenant(Cliente).filter_by(id=cliente_id).first()
        if not cliente_obj:
            msg = 'Cliente não encontrado.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return _render_form()
        cliente_avulso = cliente_avulso_raw if 'DESCONHECIDO' in str(cliente_obj.nome_cliente or '').upper() else None
        forma_pagamento = (request.form.get('forma_pagamento') or '').strip() or None
        tipo_operacao = (request.form.get('tipo_operacao') or 'VENDA').strip().upper()
        if tipo_operacao not in ('VENDA', 'PERDA'):
            tipo_operacao = 'VENDA'
        if tipo_operacao != 'PERDA' and not forma_pagamento:
            msg = 'Selecione a forma de pagamento.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return _render_form()
        lucro_percentual_raw = (request.form.get('lucro_percentual') or '').strip()
        lucro_percentual = None
        if lucro_percentual_raw:
            try:
                lucro_percentual = Decimal(str(lucro_percentual_raw).replace(',', '.'))
                if lucro_percentual < 0:
                    lucro_percentual = Decimal('0')
            except Exception:
                lucro_percentual = None
        preco_venda = Decimal(str(_limpar_valor_moeda(request.form.get('preco_venda', 0))))
        if tipo_operacao == 'PERDA':
            preco_venda = Decimal('0')
            situacao = 'PERDA'
            forma_pagamento = None
            lucro_percentual = None
        venda = Venda(
            cliente_id=cliente_id,
            cliente_avulso=cliente_avulso,
            produto_id=produto_id,
            nf=request.form.get('nf', ''),
            preco_venda=preco_venda,
            quantidade_venda=quantidade_venda,
            data_venda=date.fromisoformat(data_venda_raw) if data_venda_raw else date.today(),
            empresa_faturadora=empresa_faturadora,
            situacao=situacao,
            forma_pagamento=forma_pagamento,
            tipo_operacao=tipo_operacao,
            lucro_percentual=lucro_percentual,
            empresa_id=empresa_id_atual(),
        )
        db.session.add(venda)
        novo_estoque = int(produto.estoque_atual) - int(quantidade_venda)
        if novo_estoque < 0:
            db.session.rollback()
            msg = f'Estoque insuficiente! Disponível: {produto.estoque_atual}'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return redirect(url_for('vendas.listar_vendas'))
        produto.estoque_atual = novo_estoque
        db.session.flush()
        # --- INTEGRAÇÃO COM CAIXA (PILOTO AUTOMÁTICO V4) ---
        if tipo_operacao != 'PERDA' and str(venda.situacao or '').strip().upper() in ('PAGO', 'CONCLUÍDO'):
            lancamentos_existentes = query_tenant(LancamentoCaixa).filter(
                LancamentoCaixa.descricao.like(f"Venda #{venda.id} -%")
            ).all()
            if not lancamentos_existentes:
                cliente = query_tenant(Cliente).filter_by(id=venda.cliente_id).first()
                nome_cliente = cliente.nome_cliente if cliente else "Cliente Avulso"
                forma_pgto = request.form.get('forma_pagamento', 'Dinheiro') or 'Dinheiro'
                valor_venda = Decimal(str(venda.calcular_total() or Decimal('0.00')))
                forma_pgto_upper = str(forma_pgto or '').upper()
                data_venc = getattr(venda, 'data_vencimento', None)
                if 'BOLETO' in forma_pgto_upper and data_venc:
                    data_lancamento_caixa = data_venc
                else:
                    data_lancamento_caixa = date.today()
                novo_lanc = LancamentoCaixa(
                    data=data_lancamento_caixa,
                    descricao=f"Venda #{venda.id} - {nome_cliente}",
                    tipo='ENTRADA',
                    categoria='Entrada Cliente',
                    forma_pagamento=forma_pgto,
                    valor=valor_venda,
                    usuario_id=current_user.id,
                    empresa_id=empresa_id_atual(),
                )
                db.session.add(novo_lanc)
                if 'boleto' in forma_pgto.lower():
                    repasse_lanc = LancamentoCaixa(
                        data=data_lancamento_caixa,
                        descricao=f"Venda #{venda.id} - {nome_cliente} (Repasse Fornecedor)",
                        tipo='SAIDA',
                        categoria='Saída Fornecedor',
                        forma_pagamento=forma_pgto,
                        valor=valor_venda,
                        usuario_id=current_user.id,
                        empresa_id=empresa_id_atual(),
                    )
                    db.session.add(repasse_lanc)
        db.session.commit()
        limpar_cache_dashboard()
        _nome_cli_log = venda.cliente.nome_cliente if venda.cliente else (venda.cliente_avulso or 'Avulso')
        registrar_log(
            'CRIAR', 'VENDAS',
            f"Venda #{venda.id} — {venda.quantidade_venda} un. de {venda.produto.nome_produto} "
            f"para {_nome_cli_log}. NF: {venda.nf or '-'}, Total: R$ {venda.calcular_total():.2f}, "
            f"Situação: {venda.situacao}.",
        )

        if _is_ajax():
            return jsonify(
                ok=True,
                mensagem='Venda registrada com sucesso!',
                venda={
                    'id': venda.id,
                    'cliente_nome': f"{venda.cliente.nome_cliente} ({venda.cliente_avulso})" if (venda.cliente and venda.cliente_avulso and 'DESCONHECIDO' in str(venda.cliente.nome_cliente or '').upper()) else venda.cliente.nome_cliente,
                    'produto_nome': venda.produto.nome_produto,
                    'nf': venda.nf or '-',
                    'quantidade_venda': venda.quantidade_venda,
                    'preco_venda': float(venda.preco_venda),
                    'total': float(venda.preco_venda * venda.quantidade_venda),
                    'lucro': float(venda.calcular_lucro()),
                    'data_venda': venda.data_venda.strftime('%d/%m/%Y'),
                    'empresa_faturadora': venda.empresa_faturadora,
                    'situacao': venda.situacao,
                    'tipo_operacao': venda.tipo_operacao,
                },
            )
        flash('Venda registrada com sucesso!', 'success')
        return redirect(url_for('vendas.listar_vendas'))

    return _render_form()


@vendas_bp.route('/add_venda', methods=['POST'])
def add_venda():
    """Alias para criação de venda via AJAX (listar). Sempre retorna JSON."""
    return nova_venda()


@vendas_bp.route('/processar_carrinho', methods=['POST'])
def processar_carrinho():
    """Processa itens do carrinho em lote: cria Venda e atualiza estoque
    em uma única transação."""
    data = request.get_json(silent=True) or {}
    itens = data.get('itens', [])
    if not itens:
        return jsonify(ok=False, mensagem='Carrinho vazio. Adicione itens antes de finalizar.'), 400

    try:
        processados = 0
        for obj in itens:
            try:
                cliente_id = int(obj.get('cliente_id'))
                produto_id = int(obj.get('produto_id'))
                quantidade_venda = int(obj.get('quantidade_venda', 0))
                preco_venda = Decimal(str(_limpar_valor_moeda(obj.get('preco_venda', 0))))
                empresa_faturadora = (obj.get('empresa_faturadora') or '').strip() or None
                situacao = (obj.get('situacao') or 'PENDENTE').strip()
                forma_pagamento = (obj.get('forma_pagamento') or '').strip() or None
                tipo_operacao = (obj.get('tipo_operacao') or 'VENDA').strip().upper()
                lucro_percentual = None
                lucro_percentual_raw = obj.get('lucro_percentual')
                if lucro_percentual_raw not in (None, ''):
                    try:
                        lucro_percentual = Decimal(str(lucro_percentual_raw).replace(',', '.'))
                        if lucro_percentual < 0:
                            lucro_percentual = Decimal('0')
                    except Exception:
                        lucro_percentual = None
                nf = (obj.get('nf') or '').strip() or None
                data_venda_raw = obj.get('data_venda')
                if data_venda_raw:
                    data_venda = date.fromisoformat(data_venda_raw)
                else:
                    data_venda = date.today()
            except (TypeError, ValueError) as e:
                return jsonify(ok=False, mensagem=f'Dados inválidos em um item: {e}'), 400

            if quantidade_venda < 1:
                return jsonify(ok=False, mensagem='Quantidade deve ser maior que zero.'), 400
            if tipo_operacao not in ('VENDA', 'PERDA'):
                tipo_operacao = 'VENDA'
            if not empresa_faturadora or empresa_faturadora not in ('DESTAK', 'PATY', 'NENHUM', 'ARMAZEM LACERDA'):
                return jsonify(ok=False, mensagem='Empresa faturadora inválida.'), 400

            produto = _produto_com_lock(produto_id)
            if not produto:
                return jsonify(ok=False, mensagem=f'Produto ID {produto_id} não encontrado.'), 400
            if produto.estoque_atual < quantidade_venda:
                return jsonify(
                    ok=False,
                    mensagem=f'Estoque insuficiente para "{produto.nome_produto}". Disponível: {produto.estoque_atual}.',
                ), 400

            cliente = query_tenant(Cliente).filter_by(id=cliente_id).first()
            if not cliente:
                return jsonify(ok=False, mensagem=f'Cliente ID {cliente_id} não encontrado.'), 400
            cliente_avulso_raw = (obj.get('cliente_avulso') or '').strip()
            cliente_avulso = cliente_avulso_raw if 'DESCONHECIDO' in str(cliente.nome_cliente or '').upper() else None
            if tipo_operacao == 'PERDA':
                preco_venda = Decimal('0')
                situacao = 'PERDA'
                forma_pagamento = None
                lucro_percentual = None

            venda = Venda(
                cliente_id=cliente_id,
                cliente_avulso=cliente_avulso,
                produto_id=produto_id,
                nf=nf,
                preco_venda=preco_venda,
                quantidade_venda=quantidade_venda,
                data_venda=data_venda,
                empresa_faturadora=empresa_faturadora,
                situacao=situacao,
                forma_pagamento=forma_pagamento,
                tipo_operacao=tipo_operacao,
                lucro_percentual=lucro_percentual,
                empresa_id=empresa_id_atual(),
            )
            db.session.add(venda)
            novo_estoque = int(produto.estoque_atual) - int(quantidade_venda)
            if novo_estoque < 0:
                raise ValueError(f'Estoque insuficiente para "{produto.nome_produto}".')
            produto.estoque_atual = novo_estoque
            processados += 1

        db.session.commit()
        limpar_cache_dashboard()
        return jsonify(ok=True, mensagem=f'{processados} venda(s) registrada(s) com sucesso.', processados=processados)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, mensagem=str(e)), 500


@vendas_bp.route('/venda/adicionar_item', methods=['POST'])
def venda_adicionar_item():
    """Adiciona um novo item (produto) a um pedido/venda existente.
    Baixa estoque e mantém dados do pedido."""
    venda_id = request.form.get('venda_id')
    produto_id = request.form.get('produto_id')
    quantidade_venda = request.form.get('quantidade_venda')
    preco_venda_raw = request.form.get('preco_venda')

    if not venda_id or not produto_id or not quantidade_venda or not preco_venda_raw:
        flash('Preencha todos os campos obrigatórios.', 'error')
        return redirect(url_for('vendas.listar_vendas'))

    try:
        venda_id = int(venda_id)
        produto_id = int(produto_id)
        quantidade_venda = int(quantidade_venda)
    except (ValueError, TypeError):
        flash('Dados inválidos.', 'error')
        return redirect(url_for('vendas.listar_vendas'))

    venda_existente = query_tenant(Venda).filter_by(id=venda_id).first_or_404()
    if not _usuario_pode_gerenciar_venda(venda_existente):
        return _resposta_sem_permissao()
    _assumir_ownership_venda_orfa(venda_existente)
    produto = _produto_com_lock(produto_id)
    if not produto:
        flash('Produto não encontrado.', 'error')
        return redirect(url_for('vendas.listar_vendas'))

    preco_venda = _limpar_valor_moeda(preco_venda_raw)
    tipo_operacao = str(getattr(venda_existente, 'tipo_operacao', 'VENDA') or 'VENDA').strip().upper()
    if tipo_operacao not in ('VENDA', 'PERDA'):
        tipo_operacao = 'VENDA'
    if tipo_operacao != 'PERDA' and preco_venda <= 0:
        flash('Preço unitário inválido.', 'error')
        return redirect(url_for('vendas.listar_vendas'))
    if tipo_operacao == 'PERDA':
        preco_venda = 0

    if produto.estoque_atual < quantidade_venda:
        flash(f'Estoque insuficiente! Disponível: {produto.estoque_atual}', 'error')
        return redirect(url_for('vendas.listar_vendas'))

    novo_estoque = int(produto.estoque_atual) - int(quantidade_venda)
    if novo_estoque < 0:
        flash(f'Estoque insuficiente! Disponível: {produto.estoque_atual}', 'error')
        return redirect(url_for('vendas.listar_vendas'))
    produto.estoque_atual = novo_estoque

    nova_venda_obj = Venda(
        cliente_id=venda_existente.cliente_id,
        cliente_avulso=venda_existente.cliente_avulso,
        produto_id=produto_id,
        nf=venda_existente.nf or '',
        preco_venda=Decimal(str(preco_venda)),
        quantidade_venda=quantidade_venda,
        data_venda=venda_existente.data_venda,
        empresa_faturadora=venda_existente.empresa_faturadora,
        situacao='PERDA' if tipo_operacao == 'PERDA' else venda_existente.situacao,
        forma_pagamento=None if tipo_operacao == 'PERDA' else venda_existente.forma_pagamento,
        tipo_operacao=tipo_operacao,
        empresa_id=empresa_id_atual(),
    )
    db.session.add(nova_venda_obj)
    db.session.commit()
    limpar_cache_dashboard()
    flash('Produto adicionado ao pedido com sucesso!', 'success')
    return redirect(url_for('vendas.listar_vendas'))


@vendas_bp.route('/vendas/editar/<int:id>', methods=['GET', 'POST'])
def editar_venda(id):
    venda = query_tenant(Venda).filter_by(id=id).first_or_404()
    if not _usuario_pode_gerenciar_venda(venda):
        return _resposta_sem_permissao()
    produto_original = venda.produto
    quantidade_original = venda.quantidade_venda
    vendas_do_pedido_alvo = _vendas_do_pedido(venda)

    if request.method == 'POST':
        def _clean_nullable_text(value):
            txt = str(value or '').strip()
            return None if txt == '' or txt.lower() in ('none', 'null', 'undefined') else txt

        try:
            _assumir_ownership_venda_orfa(venda)

            def safe_float(value, default=0.0):
                if value is None:
                    return default
                raw = str(value).strip()
                if raw == '' or raw.lower() == 'none':
                    return default
                try:
                    if ',' in raw and '.' in raw:
                        raw = raw.replace('.', '').replace(',', '.')
                    else:
                        raw = raw.replace(',', '.')
                    return float(raw)
                except (ValueError, TypeError):
                    return default

            def safe_int(value, default=0):
                try:
                    return int(float(safe_float(value, default=float(default))))
                except (ValueError, TypeError):
                    return default

            produto_id = safe_int(request.form.get('produto_id'), default=0)
            quantidade_val = safe_float(
                request.form.get('quantidade_venda', request.form.get('quantidade')),
                default=1.0,
            )
            quantidade_venda = safe_int(quantidade_val, default=1)

            if quantidade_venda < 1:
                quantidade_venda = 1

            if not produto_id:
                flash('Produto é obrigatório.', 'error')
                return redirect(url_for('vendas.listar_vendas'))

            produto = query_tenant(Produto).filter(Produto.id == produto_id).with_for_update().first()
            if not produto:
                flash('Produto não encontrado.', 'error')
                return redirect(url_for('vendas.listar_vendas'))

            if produto.id == produto_original.id:
                estoque_disponivel = produto.estoque_atual + quantidade_original
            else:
                estoque_disponivel = produto.estoque_atual

            if estoque_disponivel < quantidade_venda:
                flash(f'Estoque insuficiente! Disponível: {estoque_disponivel}', 'error')
                return redirect(url_for('vendas.listar_vendas'))

            if produto.id != produto_original.id:
                db.session.refresh(produto_original, with_for_update=True)

            if produto.id == produto_original.id:
                produto.estoque_atual = produto.estoque_atual + quantidade_original - quantidade_venda
            else:
                produto_original.estoque_atual += quantidade_original
                produto.estoque_atual -= quantidade_venda

            cliente_id = safe_int(request.form.get('cliente_id'), default=venda.cliente_id or 0)
            data_venda_raw = _clean_nullable_text(request.form.get('data_venda'))
            cliente_id_novo = cliente_id if cliente_id else venda.cliente_id
            nf_nova = _clean_nullable_text(request.form.get('nf'))
            empresa_nova = _clean_nullable_text(request.form.get('empresa_faturadora')) or (venda.empresa_faturadora or 'PATY')
            situacao_nova = _clean_nullable_text(request.form.get('situacao')) or (venda.situacao or 'PENDENTE')
            fp = _clean_nullable_text(request.form.get('forma_pagamento'))
            forma_pagamento_nova = fp
            data_venda_nova = None
            if data_venda_raw:
                try:
                    data_venda_nova = date.fromisoformat(data_venda_raw)
                except Exception:
                    flash('Data da venda inválida.', 'error')
                    return redirect(url_for('vendas.listar_vendas'))

            for v_pedido in vendas_do_pedido_alvo:
                v_pedido.cliente_id = cliente_id_novo
                v_pedido.nf = nf_nova
                if data_venda_nova:
                    v_pedido.data_venda = data_venda_nova
                v_pedido.empresa_faturadora = empresa_nova
                if str(getattr(v_pedido, 'tipo_operacao', 'VENDA') or 'VENDA').upper() == 'PERDA':
                    v_pedido.situacao = 'PERDA'
                    v_pedido.forma_pagamento = None
                else:
                    v_pedido.situacao = situacao_nova
                    v_pedido.forma_pagamento = forma_pagamento_nova

            venda.produto_id = produto_id
            preco_venda = safe_float(
                request.form.get('preco_venda', request.form.get('preco_unitario')),
                default=0.0,
            )
            venda.preco_venda = Decimal(str(preco_venda))
            venda.quantidade_venda = quantidade_venda
            lucro_percentual_float = safe_float(request.form.get('lucro_percentual'), default=0.0)
            lucro_percentual = Decimal(str(lucro_percentual_float))
            if lucro_percentual < 0:
                lucro_percentual = Decimal('0')
            tipo_operacao = (_clean_nullable_text(request.form.get('tipo_operacao')) or venda.tipo_operacao or 'VENDA').strip().upper()
            if tipo_operacao not in ('VENDA', 'PERDA'):
                tipo_operacao = 'VENDA'
            if tipo_operacao != 'PERDA' and not forma_pagamento_nova:
                flash('Selecione a forma de pagamento.', 'error')
                return redirect(request.referrer or url_for('vendas.listar_vendas'))
            venda.tipo_operacao = tipo_operacao
            venda.lucro_percentual = lucro_percentual if (lucro_percentual is not None and lucro_percentual > 0) else None
            if tipo_operacao == 'PERDA':
                venda.preco_venda = Decimal('0')
                venda.situacao = 'PERDA'
                venda.forma_pagamento = None
                venda.lucro_percentual = None

            # --- INTEGRAÇÃO COM CAIXA (PILOTO AUTOMÁTICO V4) ---
            vendas_do_pedido = vendas_do_pedido_alvo
            venda_id_busca = vendas_do_pedido[0].id if vendas_do_pedido else venda.id
            lancamentos_existentes = query_tenant(LancamentoCaixa).filter(
                LancamentoCaixa.descricao.like(f"Venda #{venda_id_busca} -%")
            ).all()
            status_atual = str(venda.situacao).strip().upper() if venda.situacao else ''
            status_pago = status_atual in ('PAGO', 'CONCLUÍDO', 'PARCIAL')
            eh_perda = tipo_operacao == 'PERDA'
            if status_pago and not eh_perda and not lancamentos_existentes:
                cliente = query_tenant(Cliente).filter_by(id=venda.cliente_id).first()
                nome_cliente = cliente.nome_cliente if cliente else "Cliente Avulso"
                forma_pgto = _clean_nullable_text(request.form.get('forma_pagamento')) or 'Dinheiro'
                valor_pedido = sum(float(v.calcular_total()) for v in vendas_do_pedido)
                forma_pgto_upper = str(forma_pgto or '').upper()
                data_venc = None
                for v in vendas_do_pedido:
                    dv = getattr(v, 'data_vencimento', None)
                    if dv:
                        data_venc = dv
                        break
                if 'BOLETO' in forma_pgto_upper and data_venc:
                    data_lancamento_caixa = data_venc
                else:
                    data_lancamento_caixa = date.today()
                novo_lanc = LancamentoCaixa(
                    data=data_lancamento_caixa,
                    descricao=f"Venda #{venda_id_busca} - {nome_cliente}",
                    tipo='ENTRADA',
                    categoria='Entrada Cliente',
                    forma_pagamento=forma_pgto,
                    valor=valor_pedido,
                    usuario_id=current_user.id,
                    empresa_id=empresa_id_atual(),
                )
                db.session.add(novo_lanc)
                if 'boleto' in forma_pgto.lower():
                    repasse_lanc = LancamentoCaixa(
                        data=data_lancamento_caixa,
                        descricao=f"Venda #{venda_id_busca} - {nome_cliente} (Repasse Fornecedor)",
                        tipo='SAIDA',
                        categoria='Saída Fornecedor',
                        forma_pagamento=forma_pgto,
                        valor=valor_pedido,
                        usuario_id=current_user.id,
                        empresa_id=empresa_id_atual(),
                    )
                    db.session.add(repasse_lanc)
            elif not status_pago and lancamentos_existentes:
                for lanc in lancamentos_existentes:
                    db.session.delete(lanc)

            db.session.commit()

            limpar_cache_dashboard()
            _venda_editada = query_tenant(Venda).filter_by(id=venda.id).first()
            if _venda_editada:
                _cli_edit = query_tenant(Cliente).filter_by(id=_venda_editada.cliente_id).first()
                registrar_log(
                    'EDITAR', 'VENDAS',
                    f"Venda #{_venda_editada.id} editada — Cliente: "
                    f"{_cli_edit.nome_cliente if _cli_edit else 'N/A'}, NF: {_venda_editada.nf or '-'}, "
                    f"Total: R$ {_venda_editada.calcular_total():.2f}.",
                )
            flash('Venda atualizada com sucesso!', 'success')
            return redirect(url_for('vendas.listar_vendas'))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"ERRO AO EDITAR VENDA: {str(e)}")
            current_app.logger.exception('Falha inesperada na edição da venda')
            flash('Erro ao salvar: verifique os dados preenchidos.', 'error')
            return redirect(request.referrer or url_for('vendas.listar_vendas'))

    clientes = query_tenant(Cliente).order_by(Cliente.nome_cliente).limit(1000).all()
    produtos = query_tenant(Produto).order_by(Produto.nome_produto).limit(1000).all()
    venda_nf = venda.nf if venda.nf else ''
    return render_template('vendas/formulario.html', venda=venda, venda_nf=venda_nf, clientes=clientes, produtos=produtos)


@vendas_bp.route('/venda/excluir_item/<int:id>', methods=['POST'])
def excluir_item_venda(id):
    """Exclui um item individual de venda (uma linha), devolvendo estoque."""
    venda = query_tenant(Venda).filter_by(id=id).first_or_404()
    if not _usuario_pode_gerenciar_venda(venda):
        return _resposta_sem_permissao()
    try:
        for doc in list(getattr(venda, 'documentos', []) or []):
            _deletar_cloudinary_seguro(
                public_id=getattr(doc, 'public_id', None),
                url=getattr(doc, 'url_arquivo', None),
                resource_type='raw',
            )
        _apagar_lancamentos_caixa_por_vendas([venda])
        if venda.produto_id:
            produto = _produto_com_lock(venda.produto_id)
            if produto:
                produto.estoque_atual += venda.quantidade_venda
        db.session.delete(venda)
        db.session.commit()
        limpar_cache_dashboard()
        if _is_ajax():
            return jsonify(ok=True, mensagem='Item removido com sucesso.')
        flash('Item removido com sucesso.', 'success')
    except Exception as e:
        db.session.rollback()
        if _is_ajax():
            return jsonify(ok=False, mensagem=str(e)), 500
        flash('Erro ao remover item.', 'error')
    return redirect(url_for('vendas.listar_vendas'))


@vendas_bp.route('/vendas/excluir/<int:id>', methods=['POST'])
def excluir_venda(id):
    """Exclui uma venda e todas as outras vendas do mesmo pedido.
    Regra A (CNPJ preenchido): Cliente + NF + Data
    Regra B (CNPJ = '0' ou '00000000000000'): Cliente + Data (ignora NF)
    """
    venda = query_tenant(Venda).filter_by(id=id).first_or_404()
    if not _usuario_pode_gerenciar_venda(venda):
        return _resposta_sem_permissao()

    nome_cliente = venda.cliente.nome_cliente
    cliente_id = venda.cliente_id
    cnpj_cliente = venda.cliente.cnpj or ''
    nf_pedido = venda.nf
    data_pedido = venda.data_venda

    is_consumidor_final = cnpj_cliente in ('0', '00000000000000', '')
    nf_normalizada = str(nf_pedido).strip() if nf_pedido else ''

    if is_consumidor_final:
        query = query_tenant(Venda).filter(
            Venda.cliente_id == cliente_id,
            Venda.data_venda == data_pedido,
        )
    else:
        query = query_tenant(Venda).filter(
            Venda.cliente_id == cliente_id,
            Venda.data_venda == data_pedido,
        )
        if nf_normalizada:
            query = query.filter(Venda.nf == nf_pedido)
        else:
            query = query.filter((Venda.nf.is_(None)) | (Venda.nf == ''))

    vendas_do_pedido = query.all()

    try:
        lancamentos_removidos = _apagar_lancamentos_caixa_por_vendas(vendas_do_pedido)

        logs = []
        for v in vendas_do_pedido:
            for doc in list(getattr(v, 'documentos', []) or []):
                _deletar_cloudinary_seguro(
                    public_id=getattr(doc, 'public_id', None),
                    url=getattr(doc, 'url_arquivo', None),
                    resource_type='raw',
                )
            produto = _produto_com_lock(v.produto_id) if v.produto_id else None
            quantidade = v.quantidade_venda
            nome_produto = produto.nome_produto if produto else 'Desconhecido'
            if produto:
                produto.estoque_atual += quantidade
            logs.append(f"{quantidade} unidades devolvidas ao produto [{nome_produto}]")
            db.session.delete(v)

        db.session.commit()
        limpar_cache_dashboard()

        current_app.logger.info(
            f"Pedido excluído (Cliente: {nome_cliente}, NF: {nf_pedido or 'N/A'}, "
            f"Data: {data_pedido.strftime('%d/%m/%Y')}):"
        )
        for log in logs:
            current_app.logger.info(f"  - {log}")
        if lancamentos_removidos:
            current_app.logger.info(f"  - {lancamentos_removidos} lançamento(s) de caixa removido(s).")

        registrar_log(
            'EXCLUIR', 'VENDAS',
            f"Pedido excluído — Cliente: {nome_cliente}, NF: {nf_pedido or 'N/A'}, "
            f"Data: {data_pedido.strftime('%d/%m/%Y')}, {len(vendas_do_pedido)} item(ns).",
        )
        flash(f'Pedido completo excluído com sucesso! {len(vendas_do_pedido)} item(ns) removido(s) e {lancamentos_removidos} lançamento(s) de caixa removido(s).', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao deletar venda: {e}")
        flash('Erro ao deletar a venda. Tente novamente.', 'error')
    return redirect(url_for('vendas.listar_vendas'))


@vendas_bp.route('/venda/atualizar_status/<int:id_venda>', methods=['POST'])
def atualizar_status_venda(id_venda):
    """Alterna o status do pedido: PENDENTE ↔ PAGO. Aplica a todos os itens do grupo."""
    venda = query_tenant(Venda).filter_by(id=id_venda).first_or_404()
    if not _usuario_pode_gerenciar_venda(venda):
        return _resposta_sem_permissao()
    _assumir_ownership_venda_orfa(venda)
    vendas_do_pedido = _vendas_do_pedido(venda)
    vendas_financeiras = [
        v for v in vendas_do_pedido
        if str(getattr(v, 'tipo_operacao', 'VENDA') or 'VENDA').upper() != 'PERDA'
    ]
    valor_total_financeiro = sum(float(v.calcular_total()) for v in vendas_financeiras)
    if not vendas_financeiras or valor_total_financeiro <= 0:
        msg_perda = 'Pedidos de PERDA/QUEBRA não geram cobrança nem movimentação no caixa.'
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify(ok=False, mensagem=msg_perda), 400
        flash(msg_perda, 'warning')
        return redirect(url_for('vendas.listar_vendas'))
    atual = vendas_financeiras[0].situacao if vendas_financeiras else 'PENDENTE'
    novo = 'PAGO' if atual == 'PENDENTE' else 'PENDENTE'
    for v in vendas_financeiras:
        v.situacao = novo
    # --- INTEGRAÇÃO COM CAIXA (PILOTO AUTOMÁTICO V4) ---
    lancamentos_existentes = query_tenant(LancamentoCaixa).filter(
        LancamentoCaixa.descricao.like(f"Venda #{venda.id} -%")
    ).all()
    eh_bacalhau = any(
        (getattr(vv, 'produto', None) is not None) and
        ('BACALHAU' in str(getattr(vv.produto, 'tipo', '') or '').upper())
        for vv in vendas_do_pedido
    )
    setor_destino = 'BACALHAU' if eh_bacalhau else 'GERAL'
    status_pago = novo and novo.upper() in ('PAGO', 'CONCLUÍDO', 'PARCIAL')
    if status_pago and not lancamentos_existentes:
        cliente = query_tenant(Cliente).filter_by(id=venda.cliente_id).first()
        nome_cliente = cliente.nome_cliente if cliente else "Cliente Avulso"
        forma_pgto = request.form.get('forma_pagamento') or (request.get_json(silent=True) or {}).get('forma_pagamento', 'Dinheiro') or 'Dinheiro'
        valor_pedido = valor_total_financeiro
        forma_pgto_upper = str(forma_pgto or '').upper()
        data_venc = None
        for v in vendas_financeiras:
            dv = getattr(v, 'data_vencimento', None)
            if dv:
                data_venc = dv
                break
        if 'BOLETO' in forma_pgto_upper and data_venc:
            data_lancamento_caixa = data_venc
        else:
            data_lancamento_caixa = date.today()
        novo_lancamento = LancamentoCaixa(
            data=data_lancamento_caixa,
            descricao=f"Venda #{venda.id} - {nome_cliente}",
            tipo='ENTRADA',
            categoria='Entrada Cliente',
            forma_pagamento=forma_pgto,
            valor=valor_pedido,
            setor=setor_destino,
            usuario_id=current_user.id,
            empresa_id=empresa_id_atual(),
        )
        db.session.add(novo_lancamento)
        if 'boleto' in forma_pgto.lower():
            repasse_lanc = LancamentoCaixa(
                data=data_lancamento_caixa,
                descricao=f"Venda #{venda.id} - {nome_cliente} (Repasse Fornecedor)",
                tipo='SAIDA',
                categoria='Saída Fornecedor',
                forma_pagamento=forma_pgto,
                valor=valor_pedido,
                setor=setor_destino,
                usuario_id=current_user.id,
                empresa_id=empresa_id_atual(),
            )
            db.session.add(repasse_lanc)
    elif not status_pago and lancamentos_existentes:
        for lanc in lancamentos_existentes:
            db.session.delete(lanc)

    db.session.commit()
    limpar_cache_dashboard()
    nf = venda.nf or '-'
    _cli_nome = query_tenant(Cliente).filter_by(id=venda.cliente_id).first()
    registrar_log(
        'PAGAR', 'VENDAS',
        f"Status do pedido NF: {nf} (Cliente: {_cli_nome.nome_cliente if _cli_nome else 'N/A'}) "
        f"alterado de {atual} para {novo}.",
    )
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify(ok=True, novo_status=novo, mensagem=f'Pedido (NF: {nf}) atualizado para {novo}.')
    flash(f'Pedido (NF: {nf}) atualizado para {novo}.', 'success')
    return redirect(url_for('vendas.listar_vendas'))


@vendas_bp.route('/vendas/<int:id>/atualizar_situacao_rapida', methods=['POST'])
def atualizar_situacao_rapida(id):
    try:
        data = request.get_json(silent=True) or {}
        nova_situacao = str(data.get('situacao') or '').strip().upper()
        if nova_situacao not in ('PENDENTE', 'PAGO', 'PARCIAL', 'PERDA'):
            return jsonify({'status': 'erro', 'mensagem': 'Situação inválida.'}), 400

        venda = query_tenant(Venda).filter_by(id=id).first_or_404()
        if not _usuario_pode_gerenciar_venda(venda):
            return jsonify({'status': 'erro', 'mensagem': 'Acesso negado.'}), 403
        _assumir_ownership_venda_orfa(venda)
        venda.situacao = nova_situacao
        if nova_situacao == 'PERDA':
            venda.forma_pagamento = None
            venda.preco_venda = Decimal('0')
            venda.tipo_operacao = 'PERDA'

        db.session.commit()
        limpar_cache_dashboard()
        return jsonify({'status': 'sucesso'}), 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('Falha ao atualizar situação rápida da venda')
        return jsonify({'status': 'erro', 'mensagem': str(e)}), 500


@vendas_bp.route('/venda/recibo/<int:id>')
def recibo_venda(id):
    """Gera recibo de venda em formato de impressão (uma página A4).
    Agrupa itens da mesma compra (cliente, data, NF)."""
    venda_base = query_tenant(Venda).options(joinedload(Venda.cliente)).filter_by(id=id).first_or_404()
    cliente = venda_base.cliente

    vendas_agrupadas = query_tenant(Venda).filter_by(
        cliente_id=venda_base.cliente_id,
        data_venda=venda_base.data_venda,
        nf=venda_base.nf,
    ).options(joinedload(Venda.produto)).order_by(Venda.id).all()

    total_recibo = sum(float(v.calcular_total()) for v in vendas_agrupadas)
    data_emissao = date.today()

    return render_template(
        'vendas/recibo.html',
        cliente=cliente,
        venda_base=venda_base,
        vendas=vendas_agrupadas,
        total_recibo=total_recibo,
        data_emissao=data_emissao,
    )


@vendas_bp.route('/api/pedidos')
def api_pedidos():
    """Lista pedidos recentes para o modal Vincular à Venda.
    Retorna {id, label} por pedido."""
    vendas = query_tenant(Venda).order_by(Venda.id.desc()).limit(200).all()
    seen = set()
    pedidos = []
    for v in vendas:
        cnpj = (v.cliente.cnpj or '').strip()
        is_cf = cnpj in ('0', '00000000000000', '')
        d = v.data_venda.date() if hasattr(v.data_venda, 'date') else v.data_venda
        key = (v.cliente_id, d) if is_cf else (v.cliente_id, (v.nf or '').strip(), d)
        if key in seen:
            continue
        seen.add(key)
        label = f"{v.cliente.nome_cliente} | NF {v.nf or '-'} | {d.strftime('%d/%m/%Y')}"
        pedidos.append({'id': v.id, 'label': label})
    return jsonify(pedidos=pedidos)


@vendas_bp.route('/vendas/deletar_massa', methods=['POST'])
def vendas_deletar_massa():
    """Exclusão em massa de vendas. Recebe JSON { ids: [1,2,...] }.
    Deleta em uma transação e restaura estoque."""
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'ok': False, 'mensagem': 'Nenhum ID informado.'}), 400
    try:
        ids = list({int(x) for x in ids if x is not None})
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'mensagem': 'IDs inválidos.'}), 400
    if not ids:
        return jsonify({'ok': False, 'mensagem': 'Nenhum ID informado.'}), 400
    vendas = query_tenant(Venda).filter(Venda.id.in_(ids)).all()
    if not vendas:
        return jsonify({'ok': False, 'mensagem': 'Nenhum registro encontrado.'}), 404
    if len(vendas) != len(ids):
        return jsonify({'ok': False, 'mensagem': 'Alguns IDs não existem. Nenhuma exclusão realizada.'}), 400
    try:
        logs = []
        lancamentos_removidos = _apagar_lancamentos_caixa_por_vendas(vendas)
        for v in vendas:
            produto = _produto_com_lock(v.produto_id) if v.produto_id else None
            qty = v.quantidade_venda
            nome = produto.nome_produto if produto else 'Desconhecido'
            if produto:
                produto.estoque_atual += qty
            logs.append(f"Venda {v.id}: {qty} un. devolvidas ao produto [{nome}].")
            db.session.delete(v)
        db.session.commit()
        limpar_cache_dashboard()
        for msg in logs:
            current_app.logger.info(msg)
        return jsonify({'ok': True, 'mensagem': f'{len(vendas)} registro(s) excluído(s). Estoque restaurado e {lancamentos_removidos} lançamento(s) de caixa removido(s).', 'excluidos': len(vendas)})
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro na exclusão em massa de vendas: {e}")
        return jsonify({'ok': False, 'mensagem': str(e)}), 500


@vendas_bp.route('/vendas/importar', methods=['GET', 'POST'])
def importar_vendas():
    """Importação de vendas via CSV/TSV/XLSX. Apenas admin do tenant."""
    @admin_required
    def _impl():
        if request.method == 'POST':
            if 'arquivo' not in request.files:
                return render_template('vendas/importar.html', erros_detalhados=['Nenhum arquivo selecionado. Escolha um arquivo e tente novamente.'], sucesso=0, erros=1)
            arquivo = request.files['arquivo']
            if arquivo.filename == '':
                return render_template('vendas/importar.html', erros_detalhados=['Nenhum arquivo selecionado. Escolha um arquivo e tente novamente.'], sucesso=0, erros=1)
            filepath = None
            try:
                filename = secure_filename(arquivo.filename)
                filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
                arquivo.save(filepath)
                is_raw = False
                if filename.endswith('.csv'):
                    df, is_raw = _load_csv_vendas_flexible(filepath)
                    if df is None:
                        return render_template('vendas/importar.html', erros_detalhados=['O arquivo CSV/TSV está vazio ou não pôde ser lido.'], sucesso=0, erros=1)
                else:
                    df = pd.read_excel(filepath)
                if not is_raw:
                    df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_')
                vendas_novas = 0
                vendas_ignoradas = 0
                erros = 0
                erros_detalhados = []
                _produtos_por_nome_normalizado = {
                    _normalizar_nome_busca(p.nome_produto): p
                    for p in query_tenant(Produto).limit(5000).all()
                }
                first_iter = True
                for idx, row in df.iterrows():
                    if first_iter:
                        first_iter = False
                    linha_num = (idx + 1) if is_raw else (idx + 2)
                    nome_cliente = _strip_quotes(row.get('cliente', row.get('nome_cliente', '')))
                    nome_produto = _strip_quotes(row.get('produto', row.get('nome_produto', '')))
                    contexto = f"{nome_cliente or '?'} / {nome_produto or '?'}"[:50]
                    try:
                        cnpj_cliente = _strip_quotes(row.get('cnpj', '')) or None
                        cliente = None
                        if cnpj_cliente:
                            cliente = query_tenant(Cliente).filter_by(cnpj=cnpj_cliente).first()
                        if not cliente and nome_cliente:
                            cliente = query_tenant(Cliente).filter(func.lower(Cliente.nome_cliente) == nome_cliente.lower()).first()
                        if not cliente:
                            erros_detalhados.append(_msg_linha(linha_num, nome_cliente or 'vazio', "O cliente não foi encontrado. Verifique se está cadastrado com esse nome exato (ou use o CNPJ)", True))
                            erros += 1
                            continue
                        if not nome_produto:
                            erros_detalhados.append(_msg_linha(linha_num, contexto, "O campo 'produto' (ou 'nome_produto') está vazio", True))
                            erros += 1
                            continue
                        nome_produto_clean = _normalizar_nome_busca(nome_produto)
                        produto_cache = _produtos_por_nome_normalizado.get(nome_produto_clean)
                        if not produto_cache:
                            erros_detalhados.append(_msg_linha(linha_num, nome_produto, "O produto não foi encontrado. Verifique se está cadastrado (o nome é comparado ignorando espaços extras e maiúsculas/minúsculas)", True))
                            erros += 1
                            continue
                        qtd_raw = row.get('quantidade', row.get('quantidade_venda', row.get('qtd', 0)))
                        quantidade_venda = _parse_quantidade(qtd_raw)
                        if quantidade_venda is None or quantidade_venda <= 0:
                            erros_detalhados.append(_msg_linha(linha_num, contexto, f"A quantidade está vazia ou inválida ({qtd_raw}). Use um número inteiro (ex: 5)", True))
                            erros += 1
                            continue
                        produto = _produto_com_lock(produto_cache.id)
                        if produto.estoque_atual < quantidade_venda:
                            erros_detalhados.append(_msg_linha(linha_num, nome_produto, f"Estoque insuficiente. Disponível: {produto.estoque_atual} unidades, solicitado: {quantidade_venda}. Ajuste a quantidade ou o estoque", True))
                            erros += 1
                            continue
                        preco_raw = row.get('preco_venda', row.get('preco', 0))
                        preco_venda = _parse_preco(preco_raw)
                        if preco_venda is None:
                            txt = f"O preço '{preco_raw}' não pôde ser convertido. Use formato brasileiro (ex: 143,00 ou -120,00 para perdas) ou use ponto como decimal" if preco_raw and str(preco_raw).strip() else "O campo 'preco_venda' (ou 'preco') está vazio"
                            erros_detalhados.append(_msg_linha(linha_num, contexto, txt, True))
                            erros += 1
                            continue
                        if preco_venda < 0:
                            preco_venda = 0.0
                        data_raw = row.get('data_venda', row.get('data', ''))
                        data_venda, raw_used = _parse_data_flex(data_raw)
                        if raw_used and raw_used.strip() and data_venda is None:
                            erros_detalhados.append(_msg_linha(linha_num, contexto, f"O formato da data '{raw_used}' é inválido. Use dd/mm/aaaa ou dd/mm/yy (ex: 01/01/2026 ou 01/01/26)", True))
                            erros += 1
                            continue
                        if data_venda is None:
                            data_venda = date.today()
                        nf_raw = _strip_quotes(row.get('nf', row.get('nota_fiscal', '')))
                        nf_val = (nf_raw or '').strip()
                        nf_sn_zero = (
                            nf_val.upper() in ('S/N', '0', '0.0') or nf_val == '' or
                            (nf_val.replace('.', '').replace(',', '').strip() == '0')
                        )
                        base_dup = query_tenant(Venda).filter(
                            Venda.cliente_id == cliente.id,
                            Venda.produto_id == produto_cache.id,
                            Venda.data_venda == data_venda,
                        )
                        if nf_sn_zero:
                            base_dup = base_dup.filter(
                                or_(
                                    Venda.nf.is_(None),
                                    Venda.nf == '',
                                    Venda.nf == '0',
                                    Venda.nf == '0.0',
                                    func.lower(Venda.nf) == 's/n',
                                )
                            )
                            base_dup = base_dup.filter(
                                Venda.preco_venda == Decimal(str(preco_venda)),
                                Venda.quantidade_venda == quantidade_venda,
                            )
                        else:
                            base_dup = base_dup.filter(
                                Venda.nf == nf_val,
                                Venda.preco_venda == Decimal(str(preco_venda)),
                                Venda.quantidade_venda == quantidade_venda,
                            )
                        if base_dup.first():
                            vendas_ignoradas += 1
                            continue
                        empresa_raw = row.get('empresa', row.get('empresa_faturadora', ''))
                        empresa_val = _strip_quotes(empresa_raw).upper().strip() if empresa_raw else ''
                        if empresa_val not in ('PATY', 'DESTAK', 'NENHUM'):
                            empresa_val = 'DESTAK'

                        situacao_crua = str(row.get('situacao', row.get('situação', row.get('status', 'PENDENTE')))).strip().upper()
                        situacao_crua = _strip_quotes(situacao_crua) if situacao_crua else ''
                        if 'PAGO' in situacao_crua:
                            situacao_val = 'PAGO'
                        elif 'PEND' in situacao_crua:
                            situacao_val = 'PENDENTE'
                        else:
                            situacao_val = 'PENDENTE'
                        forma_pagamento_raw = _strip_quotes(row.get('forma_pagamento', row.get('forma', '')))
                        forma_pagamento_val = (forma_pagamento_raw or '').strip() or None
                        venda = Venda(
                            cliente_id=cliente.id,
                            produto_id=produto.id,
                            nf=nf_val if nf_val else None,
                            preco_venda=Decimal(str(preco_venda)),
                            quantidade_venda=quantidade_venda,
                            data_venda=data_venda,
                            empresa_faturadora=empresa_val,
                            situacao=situacao_val,
                            forma_pagamento=forma_pagamento_val,
                            empresa_id=empresa_id_atual(),
                        )
                        db.session.add(venda)
                        produto.estoque_atual -= quantidade_venda
                        db.session.commit()
                        vendas_novas += 1
                    except Exception as e:
                        db.session.rollback()
                        erros_detalhados.append(_msg_linha(linha_num, contexto, f"Erro inesperado: {str(e)}", True))
                        erros += 1
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
                if erros > 0:
                    return render_template('vendas/importar.html', erros_detalhados=erros_detalhados, sucesso=vendas_novas, erros=erros, ignorados=vendas_ignoradas)
                if vendas_novas > 0 or vendas_ignoradas > 0:
                    mensagem = f'🎉 Tudo pronto! Salvamos {vendas_novas} vendas novas no sistema.'
                    if vendas_ignoradas > 0:
                        mensagem += f' Ah, e encontramos {vendas_ignoradas} vendas que já estavam cadastradas e pulamos elas para não duplicar nada! 😉'
                    flash(mensagem, 'success')
                else:
                    flash('A planilha estava vazia ou não encontramos dados válidos.', 'warning')
                return redirect(url_for('vendas.listar_vendas'))
            except Exception as e:
                db.session.rollback()
                if filepath and os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                return render_template('vendas/importar.html', erros_detalhados=[f'Erro ao processar o arquivo: {str(e)}'], sucesso=0, erros=1)
        return render_template('vendas/importar.html')

    return _impl()
