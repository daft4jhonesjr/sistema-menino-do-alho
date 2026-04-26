"""Blueprint ``produtos`` — CRUD de produtos, fornecedores e tipos.

Rotas extraídas do legado ``app.py`` (Fase 2 da refatoração):

Listagem & relatórios
    * GET  /produtos                              listar_produtos
    * POST /produtos/exportar_relatorio           exportar_relatorio_produtos

Fornecedores
    * POST /fornecedores/novo                     novo_fornecedor
    * POST /fornecedores/<id>/editar              editar_fornecedor
    * POST /fornecedores/<id>/editar_ajax         editar_fornecedor_ajax
    * POST /fornecedores/<id>/excluir             excluir_fornecedor

Tipos de produto (admin nas operações destrutivas)
    * POST /tipos/novo                            novo_tipo_produto
    * POST /tipos/deletar/<id>                    deletar_tipo  (admin)
    * POST /tipos/editar/<id>                     editar_tipo   (admin)

Produtos (CRUD)
    * GET/POST /produtos/novo                     novo_produto
    * GET/POST /produtos/editar/<id>              editar_produto
    * POST /produtos/excluir/<id>                 excluir_produto
    * POST /produto/<id>/devolver                 devolver_produto
    * POST /bulk_delete_produtos                  bulk_delete_produtos
    * POST /produtos/atualizar_tipo_batch         produtos_atualizar_tipo_batch
    * GET/POST /produtos/importar                 importar_produtos  (admin)

API
    * GET  /api/produtos/<id>/fotos               get_fotos_produto
    * GET  /api/produto/<id>                      api_produto

Endpoints novos: prefixo ``produtos.`` (ex.: ``produtos.listar_produtos``).

Proteção automática de tenant
-----------------------------
Toda rota deste blueprint exige ``login_required`` + ``tenant_required``.
Aplicamos via ``before_request`` para que rotas novas herdem a proteção
sem risco de esquecer um decorator. Rotas que precisam adicionalmente
de ``@admin_required`` (importar, deletar/editar tipo) mantêm o decorator
no próprio handler.
"""
from datetime import date, datetime
from decimal import Decimal
import csv
import io
import os
import re

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, current_app, session, Response,
)
from sqlalchemy import asc, case, desc, extract, func
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
import pandas as pd
from werkzeug.utils import secure_filename
import cloudinary.uploader

from models import db, Produto, ProdutoFoto, Fornecedor, TipoProduto, Venda
from services.auth_utils import (
    tenant_required, admin_required, _is_ajax,
)
from services.db_utils import query_tenant, empresa_id_atual, _safe_db_commit
from services.cache_utils import limpar_cache_dashboard
from services.config_helpers import (
    registrar_log, _EXTERNAL_TIMEOUT,
)
from services.files_utils import (
    _arquivo_imagem_permitido, _deletar_cloudinary_seguro,
    _cloudinary_thumb_url,
)
from services.vendas_services import _produto_com_lock
from services.csv_utils import (
    _msg_linha, _strip_quotes, _normalizar_nome_coluna,
    _normalizar_nome_busca, _parse_preco, _parse_quantidade,
    _parse_data_flex, COLUNA_ARQUIVO_PARA_BANCO,
)
# ``_limpar_valor_moeda`` foi extraído para ``routes/caixa.py`` (helper
# nativo do livro caixa, mas reutilizado aqui em formulários monetários).
from routes.caixa import _limpar_valor_moeda


produtos_bp = Blueprint('produtos', __name__)


# ============================================================
# Proteção automática de tenant para todo o blueprint
# ============================================================
@produtos_bp.before_request
def _exigir_tenant_em_todas_rotas():
    """Roda antes de cada handler do blueprint — equivale a aplicar
    ``@login_required`` + ``@tenant_required`` em todas as rotas sem
    precisar repetir decorators. Reusa ``tenant_required`` de ``app.py``
    para centralizar as regras de redirecionamento (MASTER → /master-admin,
    sem empresa → /login com flash).
    """
    @tenant_required
    def _ok():
        return None

    return _ok()


# ============================================================
# Helpers exclusivos de produtos
# ============================================================

def _normalizar_tipo_ui(s):
    """Normaliza tipo para agrupamento na UI: CAFÉ -> CAFE. Retorna ALHO, SACOLA, CAFE, BACALHAU ou OUTROS."""
    if not s:
        return 'OUTROS'
    t = str(s).strip().upper()
    t = t.replace('É', 'E').replace('Ê', 'E').replace('Á', 'A').replace('À', 'A').replace('Ã', 'A').replace('Â', 'A')
    t = t.replace('Í', 'I').replace('Ó', 'O').replace('Ô', 'O').replace('Õ', 'O').replace('Ú', 'U').replace('Ç', 'C')
    if t == 'ALHO':
        return 'ALHO'
    if t == 'SACOLA':
        return 'SACOLA'
    if t == 'CAFE':
        return 'CAFE'
    if t == 'BACALHAU':
        return 'BACALHAU'
    return 'OUTROS'


def _is_placeholder_nome(valor):
    """Retorna True se o valor é vazio ou placeholder não desejado no nome."""
    txt = str(valor or '').strip().upper()
    return txt in {'', 'S/N', 'N/A', 'NA', 'NENHUMA'}


def _montar_nome_produto(*partes):
    """Monta nome do produto ignorando placeholders e espaços extras."""
    partes_validas = []
    for parte in partes:
        txt = str(parte or '').strip()
        if _is_placeholder_nome(txt):
            continue
        partes_validas.append(txt)
    return " ".join(partes_validas)


def gerar_nome_produto(tipo, nacionalidade, marca, data_chegada, tamanho):
    """Gera nome do produto ignorando placeholders como N/A e S/N.

    Exposto module-level para que rotas legadas em ``app.py`` (ex.: vendas
    que reaproveitam a montagem em fluxos antigos) consigam importar via
    ``from routes.produtos import gerar_nome_produto`` se necessário.
    """
    if isinstance(data_chegada, date):
        data_formatada = data_chegada.strftime('%d/%m/%y')
    elif isinstance(data_chegada, str):
        try:
            data_obj = date.fromisoformat(data_chegada)
            data_formatada = data_obj.strftime('%d/%m/%y')
        except Exception:
            data_formatada = date.today().strftime('%d/%m/%y')
    else:
        data_formatada = date.today().strftime('%d/%m/%y')

    tipo_txt = str(tipo or '').strip().upper()
    nacionalidade_txt = str(nacionalidade or '').strip().upper()
    marca_txt = str(marca or '').strip().upper()
    tamanho_txt = str(tamanho or '').strip().upper()
    return _montar_nome_produto(tipo_txt, nacionalidade_txt, marca_txt, data_formatada, tamanho_txt)


def _validar_sacola(tipo, nacionalidade, marca, tamanho):
    """Valida e normaliza campos conforme o tipo. Retorna (nacionalidade, marca, tamanho) ou levanta ValueError."""
    t = (tipo or '').strip().upper()
    nac = (nacionalidade or '').strip().upper().replace('N/A', 'NA')
    tam = (tamanho or '').strip().upper().replace('N/A', 'NA')

    if t == 'SACOLA':
        nacionalidade = 'N/A'
        marca = 'SOPACK'
        tam_clean = tam.replace(' ', '')
        if tam_clean not in ('P', 'M', 'G', 'S/N'):
            raise ValueError('Para SACOLA, tamanho deve ser P, M, G ou S/N.')
        return nacionalidade, marca, tam_clean

    if t == 'ALHO':
        if nac not in ('ARGENTINO', 'NACIONAL', 'CHINES'):
            raise ValueError('Nacionalidade deve ser ARGENTINO, NACIONAL ou CHINES.')
        tamanhos_ok = ['4', '5', '6', '7', '8', '9', '10']
        if tam not in tamanhos_ok:
            raise ValueError('Tamanho deve ser 4, 5, 6, 7, 8, 9 ou 10.')
        return (nacionalidade or '').strip(), (marca or '').strip(), tam

    if nac in ('NA', 'N/A', ''):
        nac = 'N/A'
    if tam in ('NA', 'N/A', ''):
        tam = 'N/A'
    return nac, (marca or '').strip(), tam


def _extrair_config_atributos_form(form):
    """Lê campos do formulário e devolve dict compatível com TipoProduto.set_config."""
    def _bool(key):
        v = form.get(key)
        return str(v or '').strip().lower() in ('1', 'on', 'true', 'yes', 'sim')

    return {
        'usa_nacionalidade': _bool('usa_nacionalidade'),
        'usa_caminhoneiro': _bool('usa_caminhoneiro'),
        'usa_tamanho': _bool('usa_tamanho'),
        'tamanhos_opcoes': str(form.get('tamanhos_opcoes') or '').strip(),
        'usa_marca': _bool('usa_marca'),
        'marcas_opcoes': str(form.get('marcas_opcoes') or '').strip(),
    }


def _row_get(row, *keys):
    for k in keys:
        if k not in row:
            continue
        v = row[k]
        if pd.isna(v):
            continue
        s = str(v).strip()
        if s != '':
            return v
    return None


# Ordem posicional para importação "raw" (sem cabeçalho): coluna 3 = Valor Total (ignorada)
_RAW_IMPORT_MAP = [
    ('nome_produto', 0),       # Produto
    ('preco_custo', 1),        # Preço Custo (string suja -> float no tratamento)
    ('quantidade_entrada', 2), # Quantidade
    None,                      # Index 3: Valor Total (ignorar)
    ('data_chegada', 4),       # Data Chegada
    ('tipo', 5),
    ('fornecedor', 6),
    ('nacionalidade', 7),
    ('tamanho', 8),
    ('marca', 9),
    ('caminhoneiro', 10),
]


def _load_csv_produtos_flexible(filepath):
    """Carrega CSV de importação de produtos com detecção de formato.

    - Se não houver vírgulas na primeira linha, usa Tab (\\t) como separador.
    - Se a primeira linha contiver 'R$', assume formato raw (sem cabeçalho)
      e mapeamento posicional.

    Retorna ``(df, is_raw)``. Em modo raw, ``df`` já tem colunas canônicas.
    """
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
            for i, entry in enumerate(_RAW_IMPORT_MAP):
                if entry is None:
                    continue
                key, idx = entry
                d[key] = row[idx] if idx < len(row) else ''
            rows.append(d)
        df = pd.DataFrame(rows)
        return df, True
    df = pd.read_csv(io.StringIO(content), sep=sep, engine='python', quoting=csv.QUOTE_MINIMAL, on_bad_lines='warn')
    return df, False


# ============================================================
# Rotas
# ============================================================

@produtos_bp.route('/produtos')
def listar_produtos():
    """
    Lista produtos do ano ativo com totais por tipo e paginação.

    Query params: ordem_data (crescente|decrescente), pagina, por_pagina.
    """
    ano_ativo = session.get('ano_ativo', datetime.now().year)

    ordem_data = (request.args.get('ordem_data') or 'crescente').strip().lower()
    if ordem_data not in ('crescente', 'decrescente'):
        ordem_data = 'crescente'

    # Query base com filtro por ano (data_chegada) e eager loading para evitar Query N+1
    # selectinload(Produto.fotos) evita N+1 ao exibir galeria de fotos no modal
    query_base = query_tenant(Produto).options(selectinload(Produto.fotos)).filter(
        extract('year', Produto.data_chegada) == ano_ativo
    )

    # Ordenação primária: produtos com estoque > 0 primeiro, zerados/negativos depois.
    ordem_estoque = case(
        (Produto.estoque_atual > 0, 0),
        else_=1
    )

    if ordem_data == 'crescente':
        query_ordenada = query_base.order_by(ordem_estoque, asc(Produto.data_chegada), asc(Produto.id))
    else:
        query_ordenada = query_base.order_by(ordem_estoque, desc(Produto.data_chegada), desc(Produto.id))

    # TOTAIS GLOBAIS: Calcular usando TODOS os produtos (sem paginação)
    produtos_todos = query_ordenada.all()

    # Otimização: Calcular quantidade_vendida para todos os produtos de uma vez usando query agregada
    # Isso evita Query N+1 ao chamar produto.quantidade_vendida() para cada produto
    quantidade_vendida_por_produto = {}
    if produtos_todos:
        produto_ids = [p.id for p in produtos_todos]
        vendas_agregadas = db.session.query(
            Venda.produto_id,
            func.sum(Venda.quantidade_venda).label('total_vendido')
        ).filter(Venda.produto_id.in_(produto_ids))\
         .group_by(Venda.produto_id).all()

        for produto_id, total_vendido in vendas_agregadas:
            quantidade_vendida_por_produto[produto_id] = int(total_vendido) if total_vendido else 0

    # Calcular quantidade_entrada_real para TODOS os produtos (para totais globais)
    produtos_com_entrada_todos = []
    for produto in produtos_todos:
        quantidade_vendida = quantidade_vendida_por_produto.get(produto.id, 0)
        if produto.quantidade_entrada == 0 or produto.quantidade_entrada < (produto.estoque_atual + quantidade_vendida):
            quantidade_entrada_exibicao = produto.estoque_atual + quantidade_vendida
        else:
            quantidade_entrada_exibicao = produto.quantidade_entrada
        produtos_com_entrada_todos.append({
            'produto': produto,
            'quantidade_entrada_exibicao': quantidade_entrada_exibicao
        })

    # Agrupar TODOS os produtos por tipo para calcular totais globais
    produtos_por_tipo_todos = {}
    reverse_order = (ordem_data == 'decrescente')
    for item in produtos_com_entrada_todos:
        tipo_key = _normalizar_tipo_ui(item['produto'].tipo)
        if tipo_key not in produtos_por_tipo_todos:
            produtos_por_tipo_todos[tipo_key] = []
        produtos_por_tipo_todos[tipo_key].append(item)

    # Otimização: Calcular lucro_realizado para todos os produtos de uma vez usando query agregada
    lucro_realizado_por_produto = {}
    if produtos_todos:
        produto_ids = [p.id for p in produtos_todos]
        lucros_agregados = db.session.query(
            Venda.produto_id,
            func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_total')
        ).join(Produto, Venda.produto_id == Produto.id)\
         .filter(Venda.produto_id.in_(produto_ids))\
         .group_by(Venda.produto_id).all()

        for produto_id, lucro_total in lucros_agregados:
            lucro_realizado_por_produto[produto_id] = float(lucro_total) if lucro_total else 0.0

    # Lucro médio por unidade: derivado dos dois dicts já agregados (sem novas queries).
    lucro_medio_por_produto = {}
    for pid, lucro in lucro_realizado_por_produto.items():
        qtd = quantidade_vendida_por_produto.get(pid, 0)
        lucro_medio_por_produto[pid] = (lucro / qtd) if qtd else 0.0

    # Calcular totais globais por tipo (usando TODOS os produtos)
    totais_por_tipo = {}
    for tipo, itens in produtos_por_tipo_todos.items():
        investimento_paty = 0.0
        investimento_destak = 0.0
        for it in itens:
            valor = float(it['produto'].preco_custo) * it['quantidade_entrada_exibicao']
            f = (it['produto'].fornecedor or '').upper()
            if f == 'PATY':
                investimento_paty += valor
            elif 'DESTAK' in f or f == 'DESTAK':
                investimento_destak += valor
        totais_por_tipo[tipo] = {
            'total_investido': sum(
                float(it['produto'].preco_custo) * it['quantidade_entrada_exibicao']
                for it in itens
            ),
            'investimento_paty': investimento_paty,
            'investimento_destak': investimento_destak,
            'total_qtd_entrada': sum(it['quantidade_entrada_exibicao'] for it in itens),
            'total_estoque_atual': sum(it['produto'].estoque_atual for it in itens),
            'total_valor_estoque_atual': sum(
                float(it['produto'].preco_custo) * float(it['produto'].estoque_atual or 0)
                for it in itens
            ),
            'total_lucro_realizado': sum(lucro_realizado_por_produto.get(it['produto'].id, 0.0) for it in itens),
        }

    # PAGINAÇÃO: aplicar somente na listagem exibida (totais continuam com todos os produtos).
    is_ajax = request.args.get('ajax', type=int) == 1
    page = request.args.get('page', 1, type=int) or 1
    per_page = 20
    if page < 1:
        page = 1

    pagination = query_ordenada.paginate(page=page, per_page=per_page, error_out=False)

    # Em requisições normais, se vier página além do fim, clamp para a última existente.
    if not is_ajax and pagination.pages and page > pagination.pages:
        page = pagination.pages
        pagination = query_ordenada.paginate(page=page, per_page=per_page, error_out=False)

    # Em requisições AJAX, página além do fim deve retornar vazio para evitar duplicação.
    if is_ajax and not pagination.items:
        return jsonify({'html': '', 'has_next': False, 'page': page})

    produtos_paginados = pagination.items

    # Calcular quantidade_entrada_real apenas para produtos paginados
    produtos_com_entrada_real = []
    for produto in produtos_paginados:
        quantidade_vendida = quantidade_vendida_por_produto.get(produto.id, 0)
        if produto.quantidade_entrada == 0 or produto.quantidade_entrada < (produto.estoque_atual + quantidade_vendida):
            quantidade_entrada_exibicao = produto.estoque_atual + quantidade_vendida
        else:
            quantidade_entrada_exibicao = produto.quantidade_entrada
        produtos_com_entrada_real.append({
            'produto': produto,
            'quantidade_entrada_exibicao': quantidade_entrada_exibicao
        })

    # Agrupar apenas produtos paginados para exibição
    produtos_por_tipo = {}
    for item in produtos_com_entrada_real:
        tipo_key = _normalizar_tipo_ui(item['produto'].tipo)
        if tipo_key not in produtos_por_tipo:
            produtos_por_tipo[tipo_key] = []
        produtos_por_tipo[tipo_key].append(item)

    # Ordenar produtos dentro de cada tipo mantendo prioridade de estoque (>0 antes de 0).
    for tipo in produtos_por_tipo:
        produtos_por_tipo[tipo].sort(
            key=lambda x: (
                0 if (x['produto'].estoque_atual or 0) > 0 else 1,
                x['produto'].data_chegada.date() if hasattr(x['produto'].data_chegada, 'date') else x['produto'].data_chegada
            ),
            reverse=False
        )
        if reverse_order:
            ativos = [it for it in produtos_por_tipo[tipo] if (it['produto'].estoque_atual or 0) > 0]
            inativos = [it for it in produtos_por_tipo[tipo] if (it['produto'].estoque_atual or 0) <= 0]
            ativos.reverse()
            inativos.reverse()
            produtos_por_tipo[tipo] = ativos + inativos

    # Ordem dinâmica multi-tenant: primeiro os TipoProduto cadastrados da empresa
    # (na ordem alfabética do cadastro), depois quaisquer tipos que apareçam em
    # produtos mas não estejam cadastrados (legado), e por fim "OUTROS" como
    # categoria especial coringa no final.
    tipos_cadastrados = [
        (t.nome or '').strip().upper()
        for t in query_tenant(TipoProduto).order_by(TipoProduto.nome).all()
    ]
    tipos_cadastrados = [t for t in tipos_cadastrados if t]

    tipos_em_produtos = [k for k in produtos_por_tipo.keys() if k]
    nao_cadastrados = sorted(
        t for t in tipos_em_produtos
        if t not in tipos_cadastrados and t != 'OUTROS'
    )

    tipos_ordenados = []
    for t in tipos_cadastrados + nao_cadastrados:
        if t not in tipos_ordenados and t != 'OUTROS':
            tipos_ordenados.append(t)
    # Sempre coloca OUTROS no final, mesmo vazio (mantém botão "Corrigir Categoria" acessível).
    tipos_ordenados.append('OUTROS')

    produtos_agrupados = {}
    for tipo in tipos_ordenados:
        produtos_agrupados[tipo] = produtos_por_tipo.get(tipo, [])

    bacalhaus = [it['produto'] for it in produtos_agrupados.get('BACALHAU', [])]

    outros_itens = produtos_agrupados.get('OUTROS', [])
    produtos_outros = [{'id': it['produto'].id, 'nome_produto': it['produto'].nome_produto} for it in outros_itens]

    if is_ajax:
        # Retorna HTML + metadados para o frontend controlar paginação sem duplicar itens.
        html_linhas = render_template(
            '_linhas_entrada.html',
            produtos_agrupados=produtos_agrupados,
            current_page=page,
            quantidade_vendida_por_produto=quantidade_vendida_por_produto,
            lucro_realizado_por_produto=lucro_realizado_por_produto,
            lucro_medio_por_produto=lucro_medio_por_produto,
        )
        return jsonify({'html': html_linhas, 'has_next': pagination.has_next, 'page': page})

    fornecedores = query_tenant(Fornecedor).order_by(Fornecedor.nome).all()
    tipos_produto = query_tenant(TipoProduto).order_by(TipoProduto.nome).all()

    # Mapeia id -> config_atributos normalizado para o JS do formulário dinâmico.
    tipos_config_map = {str(t.id): t.get_config() for t in tipos_produto}

    return render_template(
        'produtos/listar.html',
        produtos_agrupados=produtos_agrupados,
        bacalhaus=bacalhaus,
        produtos_com_entrada=produtos_com_entrada_real,
        produtos=produtos_paginados,
        produtos_outros=produtos_outros,
        fornecedores=fornecedores,
        tipos_produto=tipos_produto,
        tipos=tipos_produto,
        tipos_config_map=tipos_config_map,
        ordem_data=ordem_data,
        totais_por_tipo=totais_por_tipo,
        pagination=pagination,
        quantidade_vendida_por_produto=quantidade_vendida_por_produto,
        lucro_realizado_por_produto=lucro_realizado_por_produto,
        lucro_medio_por_produto=lucro_medio_por_produto,
    )


@produtos_bp.route('/produtos/exportar_relatorio', methods=['POST'])
def exportar_relatorio_produtos():
    ano_ativo = session.get('ano_ativo', datetime.now().year)
    filtro_fornecedor = (request.form.get('filtro_fornecedor') or 'TODOS').strip().upper()
    filtro_tipo = (request.form.get('filtro_tipo') or 'TODOS').strip().upper()
    filtro_nacionalidade = (request.form.get('filtro_nacionalidade') or 'TODAS').strip().upper()
    colunas_solicitadas = request.form.getlist('colunas')

    colunas_disponiveis = {
        'produto': 'Produto',
        'preco': 'Preco',
        'qtd_entrada': 'Qtd Entrada',
        'valor_total': 'Valor Total',
        'estoque_atual': 'Estoque Atual',
        'lucro_realizado': 'Lucro Realizado',
        'data_chegada': 'Data Chegada',
        'tipo': 'Tipo',
        'fornecedor': 'Fornecedor',
        'nacionalidade': 'Nacionalidade',
    }
    ordem_padrao_colunas = [
        'produto', 'preco', 'qtd_entrada', 'valor_total', 'estoque_atual',
        'lucro_realizado', 'data_chegada', 'tipo', 'fornecedor', 'nacionalidade'
    ]

    colunas = [c for c in ordem_padrao_colunas if c in colunas_solicitadas and c in colunas_disponiveis]
    if not colunas:
        colunas = ordem_padrao_colunas

    query = query_tenant(Produto).filter(extract('year', Produto.data_chegada) == ano_ativo)
    if filtro_fornecedor != 'TODOS':
        query = query.filter(func.upper(func.coalesce(Produto.fornecedor, 'NENHUM')) == filtro_fornecedor)
    if filtro_tipo != 'TODOS':
        query = query.filter(func.upper(func.coalesce(Produto.tipo, 'OUTROS')) == filtro_tipo)
    if filtro_nacionalidade != 'TODAS':
        query = query.filter(func.upper(func.coalesce(Produto.nacionalidade, 'N/A')) == filtro_nacionalidade)

    produtos = query.order_by(asc(Produto.data_chegada), asc(Produto.id)).all()
    produto_ids = [p.id for p in produtos]

    quantidade_vendida_por_produto = {}
    lucro_realizado_por_produto = {}
    if produto_ids:
        vendas_agregadas = db.session.query(
            Venda.produto_id,
            func.sum(Venda.quantidade_venda).label('total_vendido')
        ).filter(Venda.produto_id.in_(produto_ids)).group_by(Venda.produto_id).all()
        for produto_id, total_vendido in vendas_agregadas:
            quantidade_vendida_por_produto[produto_id] = int(total_vendido or 0)

        lucros_agregados = db.session.query(
            Venda.produto_id,
            func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_total')
        ).join(Produto, Venda.produto_id == Produto.id)\
         .filter(Venda.produto_id.in_(produto_ids))\
         .group_by(Venda.produto_id).all()
        for produto_id, lucro_total in lucros_agregados:
            lucro_realizado_por_produto[produto_id] = Decimal(str(lucro_total or 0))

    def _qtd_entrada_exibicao(produto):
        qtd_vendida = quantidade_vendida_por_produto.get(produto.id, 0)
        if produto.quantidade_entrada == 0 or produto.quantidade_entrada < (produto.estoque_atual + qtd_vendida):
            return int((produto.estoque_atual or 0) + qtd_vendida)
        return int(produto.quantidade_entrada or 0)

    def _fmt_num(valor):
        try:
            numero = Decimal(str(valor or 0))
        except Exception:
            numero = Decimal('0.00')
        return f"{numero:,.2f}".replace(',', 'X').replace('.', ',').replace('X', '.')

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

    soma_qtd_entrada = 0
    soma_valor_total = Decimal('0.0')
    soma_estoque = 0
    soma_lucro = Decimal('0.0')

    for produto in produtos:
        qtd_entrada = _qtd_entrada_exibicao(produto)
        preco = Decimal(str(produto.preco_custo or 0))
        valor_total = preco * Decimal(str(qtd_entrada))
        estoque_atual = int(produto.estoque_atual or 0)
        lucro_realizado = Decimal(str(lucro_realizado_por_produto.get(produto.id, Decimal('0.0'))))

        soma_qtd_entrada += qtd_entrada
        soma_valor_total += valor_total
        soma_estoque += estoque_atual
        soma_lucro += lucro_realizado

        linha = {
            'produto': _csv_safe(produto.nome_produto or ''),
            'preco': _fmt_num(preco),
            'qtd_entrada': str(qtd_entrada),
            'valor_total': _fmt_num(valor_total),
            'estoque_atual': str(estoque_atual),
            'lucro_realizado': _fmt_num(lucro_realizado),
            'data_chegada': _fmt_data(produto.data_chegada),
            'tipo': _csv_safe(produto.tipo or ''),
            'fornecedor': _csv_safe(produto.fornecedor or 'NENHUM'),
            'nacionalidade': _csv_safe(produto.nacionalidade or 'N/A'),
        }
        writer.writerow([linha[c] for c in colunas])

    linha_total = [''] * len(colunas)
    if linha_total:
        linha_total[0] = 'TOTAL GERAL'
    if 'qtd_entrada' in colunas:
        linha_total[colunas.index('qtd_entrada')] = str(soma_qtd_entrada)
    if 'valor_total' in colunas:
        linha_total[colunas.index('valor_total')] = _fmt_num(soma_valor_total)
    if 'estoque_atual' in colunas:
        linha_total[colunas.index('estoque_atual')] = str(soma_estoque)
    if 'lucro_realizado' in colunas:
        linha_total[colunas.index('lucro_realizado')] = _fmt_num(soma_lucro)
    writer.writerow(linha_total)

    csv_content = output.getvalue()
    output.close()

    data_hoje = datetime.now().strftime('%d-%m-%Y')
    partes = ['relatorio_produtos', data_hoje]

    def _normalizar_nome_arquivo(parte):
        txt = str(parte or '').strip().upper().replace(' ', '_')
        txt = re.sub(r'[^A-Z0-9_\-]', '', txt)
        return txt

    if filtro_fornecedor and filtro_fornecedor != 'TODOS':
        partes.append(_normalizar_nome_arquivo(filtro_fornecedor))
    if filtro_tipo and filtro_tipo != 'TODOS':
        partes.append(_normalizar_nome_arquivo(filtro_tipo))
    if filtro_nacionalidade and filtro_nacionalidade != 'TODAS':
        partes.append(_normalizar_nome_arquivo(filtro_nacionalidade))

    nome_arquivo = f"{'_'.join([p for p in partes if p])}.csv"

    return Response(
        csv_content,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={nome_arquivo}'}
    )


# ============================================================
# Fornecedores
# ============================================================

@produtos_bp.route('/fornecedores/novo', methods=['POST'])
def novo_fornecedor():
    nome = str(request.form.get('nome') or '').strip().upper()
    razao_social = str(request.form.get('razao_social') or '').strip().upper() or None
    cnpj = str(request.form.get('cnpj') or '').strip() or None
    endereco = str(request.form.get('endereco') or '').strip() or None
    tipos_ids_raw = request.form.getlist('tipos_produtos')
    tipos_ids = []
    for tid in tipos_ids_raw:
        try:
            tipos_ids.append(int(tid))
        except (TypeError, ValueError):
            continue
    tipos_selecionados = query_tenant(TipoProduto).filter(TipoProduto.id.in_(tipos_ids)).all() if tipos_ids else []

    if not nome:
        flash('Nome do fornecedor é obrigatório.', 'error')
        return redirect(url_for('produtos.listar_produtos'))

    if query_tenant(Fornecedor).filter(func.upper(Fornecedor.nome) == nome).first():
        flash('Fornecedor já cadastrado.', 'warning')
        return redirect(url_for('produtos.listar_produtos'))

    try:
        fornecedor = Fornecedor(nome=nome, razao_social=razao_social, cnpj=cnpj, endereco=endereco, empresa_id=empresa_id_atual())
        fornecedor.tipos_produtos = tipos_selecionados
        db.session.add(fornecedor)
        db.session.commit()
        flash('Fornecedor cadastrado com sucesso!', 'success')
    except Exception:
        db.session.rollback()
        flash('Erro ao cadastrar fornecedor.', 'error')
    return redirect(url_for('produtos.listar_produtos'))


@produtos_bp.route('/fornecedores/<int:id>/editar', methods=['POST'])
def editar_fornecedor(id):
    fornecedor = query_tenant(Fornecedor).filter_by(id=id).first_or_404()
    nome = str(request.form.get('nome') or '').strip().upper()
    razao_social = str(request.form.get('razao_social') or '').strip().upper() or None
    cnpj = str(request.form.get('cnpj') or '').strip() or None
    endereco = str(request.form.get('endereco') or '').strip() or None
    tipos_ids_raw = request.form.getlist('tipos_produtos')
    tipos_ids = []
    for tid in tipos_ids_raw:
        try:
            tipos_ids.append(int(tid))
        except (TypeError, ValueError):
            continue
    tipos_selecionados = query_tenant(TipoProduto).filter(TipoProduto.id.in_(tipos_ids)).all() if tipos_ids else []

    if not nome:
        flash('Nome do fornecedor é obrigatório.', 'error')
        return redirect(url_for('produtos.listar_produtos'))

    ja_existe = query_tenant(Fornecedor).filter(
        func.upper(Fornecedor.nome) == nome,
        Fornecedor.id != fornecedor.id
    ).first()
    if ja_existe:
        flash('Já existe outro fornecedor com este nome.', 'warning')
        return redirect(url_for('produtos.listar_produtos'))

    try:
        fornecedor.nome = nome
        fornecedor.razao_social = razao_social
        fornecedor.cnpj = cnpj
        fornecedor.endereco = endereco
        fornecedor.tipos_produtos = tipos_selecionados
        db.session.commit()
        flash('Fornecedor atualizado com sucesso!', 'success')
    except Exception:
        db.session.rollback()
        flash('Erro ao atualizar fornecedor.', 'error')

    return redirect(url_for('produtos.listar_produtos'))


@produtos_bp.route('/fornecedores/<int:id>/editar_ajax', methods=['POST'])
def editar_fornecedor_ajax(id):
    fornecedor = query_tenant(Fornecedor).filter_by(id=id).first_or_404()
    try:
        novo_nome = str(request.form.get('nome') or '').strip().upper()
        nova_razao = str(request.form.get('razao_social') or '').strip().upper() or None
        novo_cnpj = str(request.form.get('cnpj') or '').strip() or None
        tipos_ids_raw = request.form.getlist('tipos_produtos')
        tipos_ids = []
        for tid in tipos_ids_raw:
            try:
                tipos_ids.append(int(tid))
            except (TypeError, ValueError):
                continue
        tipos_selecionados = query_tenant(TipoProduto).filter(TipoProduto.id.in_(tipos_ids)).all() if tipos_ids else []

        if not novo_nome:
            return jsonify({'success': False, 'error': 'O Nome Fantasia é obrigatório.'}), 400

        ja_existe = query_tenant(Fornecedor).filter(
            func.upper(Fornecedor.nome) == novo_nome,
            Fornecedor.id != fornecedor.id
        ).first()
        if ja_existe:
            return jsonify({'success': False, 'error': 'Já existe outro fornecedor com este nome.'}), 400

        fornecedor.nome = novo_nome
        fornecedor.razao_social = nova_razao
        fornecedor.cnpj = novo_cnpj
        fornecedor.tipos_produtos = tipos_selecionados
        db.session.commit()
        return jsonify({'success': True, 'tipos': [t.nome for t in tipos_selecionados]})
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'error': 'Erro no banco de dados. Verifique se o nome já existe.'}), 500


@produtos_bp.route('/fornecedores/<int:id>/excluir', methods=['POST'])
def excluir_fornecedor(id):
    fornecedor = query_tenant(Fornecedor).filter_by(id=id).first_or_404()
    try:
        db.session.delete(fornecedor)
        db.session.commit()
        return jsonify({'success': True})
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': 'Erro ao excluir fornecedor.'}), 500


# ============================================================
# Tipos de produto
# ============================================================

@produtos_bp.route('/tipos/novo', methods=['POST'])
def novo_tipo_produto():
    nome = str(request.form.get('nome') or '').strip().upper()

    if not nome:
        flash('Nome do tipo é obrigatório.', 'error')
        return redirect(url_for('produtos.listar_produtos'))

    if query_tenant(TipoProduto).filter(func.upper(TipoProduto.nome) == nome).first():
        flash('Tipo já cadastrado.', 'warning')
        return redirect(url_for('produtos.listar_produtos'))

    try:
        novo = TipoProduto(nome=nome, empresa_id=empresa_id_atual())
        novo.set_config(_extrair_config_atributos_form(request.form))
        db.session.add(novo)
        db.session.commit()
        flash('Tipo cadastrado com sucesso!', 'success')
    except Exception:
        db.session.rollback()
        flash('Erro ao cadastrar tipo.', 'error')
    return redirect(url_for('produtos.listar_produtos'))


@produtos_bp.route('/tipos/deletar/<int:id>', methods=['POST'])
def deletar_tipo(id):
    """Remoção de tipo de produto. Exige admin além do tenant guard global."""
    @admin_required
    def _deletar():
        tipo = query_tenant(TipoProduto).filter_by(id=id).first_or_404()
        tipo_nome = str(tipo.nome or '').strip().upper()
        em_uso = query_tenant(Produto).filter(func.upper(func.coalesce(Produto.tipo, '')) == tipo_nome).first()
        if em_uso:
            flash('Este tipo está em uso e não pode ser apagado', 'error')
            return redirect(url_for('produtos.listar_produtos'))
        try:
            db.session.delete(tipo)
            db.session.commit()
            flash('Tipo removido com sucesso!', 'success')
        except Exception:
            db.session.rollback()
            flash('Erro ao remover tipo.', 'error')
        return redirect(url_for('produtos.listar_produtos'))

    return _deletar()


@produtos_bp.route('/tipos/editar/<int:id>', methods=['POST'])
def editar_tipo(id):
    """Edição de tipo de produto. Exige admin além do tenant guard global."""
    @admin_required
    def _editar():
        tipo = query_tenant(TipoProduto).filter_by(id=id).first_or_404()
        novo_nome = str(request.form.get('novo_nome') or '').strip().upper()

        if not novo_nome:
            flash('Nome do tipo é obrigatório.', 'error')
            return redirect(url_for('produtos.listar_produtos'))

        duplicado = query_tenant(TipoProduto).filter(
            func.upper(TipoProduto.nome) == novo_nome,
            TipoProduto.id != tipo.id
        ).first()
        if duplicado:
            flash('Já existe outro tipo com este nome.', 'warning')
            return redirect(url_for('produtos.listar_produtos'))

        try:
            tipo.nome = novo_nome
            # Só altera config se o form trouxer os campos novos (permite rotas legadas
            # que ainda enviam só 'novo_nome' a continuarem funcionando).
            if 'usa_nacionalidade' in request.form or 'usa_tamanho' in request.form \
                    or 'usa_marca' in request.form or 'usa_caminhoneiro' in request.form:
                tipo.set_config(_extrair_config_atributos_form(request.form))
            db.session.commit()
            flash('Tipo atualizado com sucesso!', 'success')
        except Exception:
            db.session.rollback()
            flash('Erro ao atualizar tipo.', 'error')
        return redirect(url_for('produtos.listar_produtos'))

    return _editar()


# ============================================================
# Produtos (CRUD)
# ============================================================

@produtos_bp.route('/produtos/novo', methods=['GET', 'POST'])
def novo_produto():
    if request.method == 'POST':
        tamanhos_bacalhau_validos = {'7/9', '10/12', '13/15', '16/20', 'DESFIADO'}
        fornecedor = request.form.get('fornecedor', '').strip()
        preco_custo = request.form.get('preco_custo', '').strip()
        caminhoneiro = request.form.get('caminhoneiro', '').strip()

        if not fornecedor:
            msg = '❌ Ops! O campo Fornecedor é obrigatório. Preencha e tente novamente.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)
        if not preco_custo:
            msg = '❌ Ops! O campo Preço de Custo é obrigatório. Preencha e tente novamente.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)
        if not caminhoneiro:
            msg = '❌ Ops! O campo Caminhoneiro é obrigatório. Preencha e tente novamente.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)

        tipo = (request.form.get('tipo') or '').strip()
        tipo_upper = tipo.upper()
        nacionalidade = request.form.get('nacionalidade', '').strip()
        marca = request.form.get('marca', '').strip()
        tamanho = request.form.get('tamanho', '').strip()
        tamanho_bacalhau = (request.form.get('tamanho', '') or '').strip().upper()
        try:
            quantidade_entrada = int(request.form.get('quantidade_entrada', 0))
        except (ValueError, TypeError):
            quantidade_entrada = 0
        data_chegada_raw = request.form.get('data_chegada')
        data_chegada = date.fromisoformat(data_chegada_raw) if data_chegada_raw else date.today()
        if not tipo:
            msg = '❌ Ops! O campo Tipo do produto é obrigatório. Selecione uma opção.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)
        if quantidade_entrada <= 0:
            msg = '❌ Ops! A quantidade de entrada deve ser maior que zero.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)
        if tipo_upper == 'BACALHAU':
            if not marca:
                marca = 'NORGY'
            if not fornecedor:
                fornecedor = 'ARMAZEM LACERDA'
            if tamanho_bacalhau not in tamanhos_bacalhau_validos:
                msg = '❌ Ops! Selecione um tamanho válido para BACALHAU (7/9, 10/12, 13/15, 16/20 ou DESFIADO).'
                if _is_ajax():
                    return jsonify(ok=False, mensagem=msg), 400
                flash(msg, 'error')
                return render_template('produtos/formulario.html', produto=None)
            tamanho = tamanho_bacalhau
        try:
            nacionalidade, marca, tamanho = _validar_sacola(tipo, nacionalidade, marca, tamanho)
        except ValueError as e:
            if _is_ajax():
                return jsonify(ok=False, mensagem=str(e)), 400
            flash(str(e), 'error')
            return render_template('produtos/formulario.html', produto=None)

        if tipo_upper == 'BACALHAU':
            nome_produto = _montar_nome_produto(tipo_upper, (marca or 'NORGY').strip().upper(), tamanho)
        else:
            nome_produto = gerar_nome_produto(tipo_upper, nacionalidade, marca, data_chegada, tamanho)
        produto = Produto(
            tipo=tipo_upper,
            nacionalidade=nacionalidade,
            marca=marca,
            tamanho=tamanho,
            fornecedor=fornecedor,
            caminhoneiro=caminhoneiro,
            preco_custo=Decimal(str(_limpar_valor_moeda(preco_custo))),
            preco_venda_alvo=None,
            quantidade_entrada=quantidade_entrada,  # Quantidade original que entrou
            estoque_atual=quantidade_entrada,
            data_chegada=data_chegada,
            nome_produto=nome_produto,
            empresa_id=empresa_id_atual()
        )
        db.session.add(produto)
        ok, err = _safe_db_commit()
        if not ok:
            msg = err or "Erro ao cadastrar produto. Tente novamente."
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 500
            flash(msg, "error")
            return render_template("produtos/formulario.html", produto=None)

        # Upload de fotos para Cloudinary (até 5)
        if os.environ.get('CLOUDINARY_URL') or current_app.config.get('CLOUDINARY_URL') or (current_app.config.get('CLOUDINARY_CLOUD_NAME') and current_app.config.get('CLOUDINARY_API_KEY')):
            fotos = request.files.getlist('fotos')
            for foto in fotos[:5]:
                if foto and foto.filename:
                    if not _arquivo_imagem_permitido(foto.filename):
                        current_app.logger.info(f"Upload de foto ignorado (extensão inválida): {foto.filename}")
                        continue
                    try:
                        upload_result = cloudinary.uploader.upload(foto, folder="menino_do_alho/produtos", timeout=_EXTERNAL_TIMEOUT)
                        url_segura = upload_result.get('secure_url')
                        public_id_foto = upload_result.get('public_id')
                        if url_segura:
                            nova_foto = ProdutoFoto(produto_id=produto.id, arquivo=url_segura, public_id=public_id_foto)
                            db.session.add(nova_foto)
                    except Exception as e:
                        current_app.logger.error(f"Erro ao fazer upload para o Cloudinary (produto {produto.id}): {e}")
        ok2, err2 = _safe_db_commit()
        if not ok2:
            msg = err2 or "Produto criado, mas falha ao salvar fotos. Edite o produto para adicionar fotos."
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 500
            flash(msg, "warning")
            return redirect(url_for("produtos.listar_produtos"))

        limpar_cache_dashboard()
        registrar_log('CRIAR', 'PRODUTOS', f"Produto #{produto.id} — {nome_produto} criado ({quantidade_entrada} un., custo R$ {produto.preco_custo}).")
        msg_sucesso = f'✅ Que maravilha! A entrada de {nome_produto} ({quantidade_entrada} un.) foi registrada no estoque com sucesso.'
        if _is_ajax():
            return jsonify(ok=True, mensagem=msg_sucesso)
        flash(msg_sucesso, 'success')
        return redirect(url_for('produtos.listar_produtos'))

    return render_template('produtos/formulario.html', produto=None)


@produtos_bp.route('/produtos/editar/<int:id>', methods=['GET', 'POST'])
def editar_produto(id):
    produto = query_tenant(Produto).filter_by(id=id).first_or_404()
    if request.method == 'POST':
        tamanhos_bacalhau_validos = {'7/9', '10/12', '13/15', '16/20', 'DESFIADO'}
        fornecedor = request.form.get('fornecedor', '').strip()
        preco_custo = request.form.get('preco_custo', '').strip()
        caminhoneiro = request.form.get('caminhoneiro', '').strip()

        if not fornecedor:
            flash('❌ Ops! O campo Fornecedor é obrigatório. Preencha e tente novamente.', 'error')
            return redirect(url_for('produtos.listar_produtos'))
        if not preco_custo:
            flash('❌ Ops! O campo Preço de Custo é obrigatório. Preencha e tente novamente.', 'error')
            return redirect(url_for('produtos.listar_produtos'))
        if not caminhoneiro:
            flash('❌ Ops! O campo Caminhoneiro é obrigatório. Preencha e tente novamente.', 'error')
            return redirect(url_for('produtos.listar_produtos'))

        tipo = (request.form.get('tipo') or '').strip()
        tipo_upper = tipo.upper()
        nacionalidade = request.form.get('nacionalidade', '').strip()
        marca = request.form.get('marca', '').strip()
        tamanho = request.form.get('tamanho', '').strip()
        tamanho_bacalhau = (request.form.get('tamanho', '') or '').strip().upper()
        try:
            quantidade_entrada = int(request.form.get('quantidade_entrada', 0))
        except (ValueError, TypeError):
            quantidade_entrada = 0
        try:
            nacionalidade, marca, tamanho = _validar_sacola(tipo, nacionalidade, marca, tamanho)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('produtos.listar_produtos'))
        if tipo_upper == 'BACALHAU':
            if not marca:
                marca = 'NORGY'
            if not fornecedor:
                fornecedor = 'ARMAZEM LACERDA'
            if tamanho_bacalhau not in tamanhos_bacalhau_validos:
                flash('Selecione um tamanho válido para BACALHAU (7/9, 10/12, 13/15, 16/20 ou DESFIADO).', 'error')
                return redirect(url_for('produtos.listar_produtos'))
            tamanho = tamanho_bacalhau

        # Atualiza data de chegada se fornecida, senão mantém a atual
        data_chegada_raw = request.form.get('data_chegada')
        if data_chegada_raw:
            data_chegada = date.fromisoformat(data_chegada_raw)
        else:
            data_chegada = produto.data_chegada

        # EDIÇÃO MANUAL: Sempre regerar nome_produto automaticamente via concatenação
        if tipo_upper == 'BACALHAU':
            nome_produto = _montar_nome_produto(tipo_upper, (marca or 'NORGY').strip().upper(), tamanho)
        else:
            nome_produto = gerar_nome_produto(tipo_upper, nacionalidade, marca, data_chegada, tamanho)

        # Lock pessimista antes de alterar estoque para evitar race condition.
        if quantidade_entrada > 0:
            produto = _produto_com_lock(produto.id)
            produto.estoque_atual += quantidade_entrada

        produto.tipo = tipo_upper
        produto.nacionalidade = nacionalidade
        produto.marca = marca
        produto.tamanho = tamanho
        produto.fornecedor = fornecedor
        produto.caminhoneiro = caminhoneiro
        produto.preco_custo = Decimal(str(_limpar_valor_moeda(preco_custo)))
        produto.data_chegada = data_chegada
        produto.nome_produto = nome_produto
        # Upload de fotos adicionais para Cloudinary (até 5 no total)
        fotos_existentes = ProdutoFoto.query.filter_by(produto_id=produto.id).count()
        slots_disponiveis = max(0, 5 - fotos_existentes)
        if slots_disponiveis > 0 and (os.environ.get('CLOUDINARY_URL') or current_app.config.get('CLOUDINARY_URL') or (current_app.config.get('CLOUDINARY_CLOUD_NAME') and current_app.config.get('CLOUDINARY_API_KEY'))):
            fotos = request.files.getlist('fotos')
            for foto in fotos[:slots_disponiveis]:
                if foto and foto.filename:
                    if not _arquivo_imagem_permitido(foto.filename):
                        current_app.logger.info(f"Upload de foto ignorado (extensão inválida): {foto.filename}")
                        continue
                    try:
                        upload_result = cloudinary.uploader.upload(foto, folder="menino_do_alho/produtos", timeout=_EXTERNAL_TIMEOUT)
                        url_segura = upload_result.get('secure_url')
                        public_id_foto = upload_result.get('public_id')
                        if url_segura:
                            nova_foto = ProdutoFoto(produto_id=produto.id, arquivo=url_segura, public_id=public_id_foto)
                            db.session.add(nova_foto)
                    except Exception as e:
                        current_app.logger.error(f"Erro ao fazer upload para o Cloudinary (produto {produto.id}): {e}")

        ok, err = _safe_db_commit()
        if not ok:
            flash(err or "Erro ao atualizar produto. Tente novamente.", "error")
            return redirect(url_for("produtos.listar_produtos"))
        limpar_cache_dashboard()
        registrar_log('EDITAR', 'PRODUTOS', f"Produto #{produto.id} — {nome_produto} editado.")
        flash(f'✅ Produto {nome_produto} atualizado com sucesso!', 'success')
        return redirect(url_for('produtos.listar_produtos'))

    return render_template('produtos/formulario.html', produto=produto)


@produtos_bp.route('/produtos/excluir/<int:id>', methods=['POST'])
def excluir_produto(id):
    produto = query_tenant(Produto).filter_by(id=id).first_or_404()
    try:
        nome = produto.nome_produto or f'#{produto.id}'
        for foto in list(getattr(produto, 'fotos', []) or []):
            _deletar_cloudinary_seguro(
                public_id=getattr(foto, 'public_id', None),
                url=getattr(foto, 'arquivo', None),
                resource_type='image'
            )
        db.session.delete(produto)
        db.session.commit()
        limpar_cache_dashboard()
        registrar_log('EXCLUIR', 'PRODUTOS', f"Produto #{id} — {nome} excluído.")
        flash(f'🗑️ Produto {nome} excluído com sucesso.', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('❌ Ops! Esse produto não pode ser excluído porque já existem vendas registradas nele.', 'error')

    return redirect(url_for('produtos.listar_produtos'))


@produtos_bp.route('/produto/<int:id>/devolver', methods=['POST'])
def devolver_produto(id):
    """
    Registra devolução de mercadorias ao fornecedor.

    Regras:
        * Valida tenant: produto precisa pertencer à empresa do usuário (query_tenant).
        * Lock pessimista no produto antes de alterar o estoque (evita race condition).
        * Quantidade deve ser >= 1 e <= estoque_atual.
        * Subtrai a quantidade do estoque_atual do produto.
        * Audita a ação no LogAtividade com tipo "DEVOLUCAO" e o motivo informado.
    """
    produto = query_tenant(Produto).filter_by(id=id).first_or_404()
    nome_produto = produto.nome_produto or f'#{produto.id}'
    fornecedor_nome = produto.fornecedor or '—'

    try:
        quantidade = int((request.form.get('quantidade') or '0').strip())
    except (ValueError, TypeError):
        quantidade = 0
    motivo = (request.form.get('motivo') or '').strip()

    if quantidade < 1:
        flash('❌ Ops! Informe uma quantidade válida (mínimo 1) para devolver.', 'error')
        return redirect(url_for('produtos.listar_produtos'))

    produto_lock = _produto_com_lock(produto.id)
    if produto_lock is None:
        db.session.rollback()
        flash('❌ Não foi possível localizar o produto para devolução.', 'error')
        return redirect(url_for('produtos.listar_produtos'))

    estoque_antes = int(produto_lock.estoque_atual or 0)
    if quantidade > estoque_antes:
        db.session.rollback()
        flash(
            f'❌ Quantidade a devolver ({quantidade}) é maior que o estoque atual '
            f'({estoque_antes}) do produto {nome_produto}.',
            'error',
        )
        return redirect(url_for('produtos.listar_produtos'))

    produto_lock.estoque_atual = estoque_antes - quantidade
    devolvido_antes = int(produto_lock.quantidade_devolvida or 0)
    produto_lock.quantidade_devolvida = devolvido_antes + quantidade
    estoque_depois = produto_lock.estoque_atual
    devolvido_total = produto_lock.quantidade_devolvida

    ok, err = _safe_db_commit()
    if not ok:
        flash(err or '❌ Erro ao registrar a devolução. Tente novamente.', 'error')
        return redirect(url_for('produtos.listar_produtos'))

    limpar_cache_dashboard()

    motivo_log = motivo if motivo else 'sem motivo informado'
    descricao_log = (
        f"DEVOLUCAO — Produto #{produto.id} ({nome_produto}) | "
        f"Fornecedor: {fornecedor_nome} | "
        f"Qtd devolvida: {quantidade} | "
        f"Estoque {estoque_antes} → {estoque_depois} | "
        f"Total devolvido acumulado: {devolvido_total} | "
        f"Motivo: {motivo_log}"
    )
    registrar_log('DEVOLUCAO', 'PRODUTOS', descricao_log)

    flash(
        f'✅ Devolução registrada: {quantidade} un de "{nome_produto}" '
        f'devolvidas ao fornecedor {fornecedor_nome}. '
        f'Estoque atualizado para {estoque_depois} un.',
        'success',
    )
    return redirect(url_for('produtos.listar_produtos'))


@produtos_bp.route('/bulk_delete_produtos', methods=['POST'])
def bulk_delete_produtos():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'ok': False, 'mensagem': '❌ Nenhum produto selecionado para exclusão.'}), 400
    excluidos = 0
    ids_erro = []
    for id_ in ids:
        produto = query_tenant(Produto).filter_by(id=id_).first()
        if not produto:
            continue
        try:
            for foto in list(getattr(produto, 'fotos', []) or []):
                _deletar_cloudinary_seguro(
                    public_id=getattr(foto, 'public_id', None),
                    url=getattr(foto, 'arquivo', None),
                    resource_type='image'
                )
            db.session.delete(produto)
            db.session.commit()
            excluidos += 1
        except IntegrityError:
            db.session.rollback()
            ids_erro.append(id_)
    if excluidos > 0:
        limpar_cache_dashboard()
    if ids_erro and not excluidos:
        return jsonify({
            'ok': False,
            'mensagem': f'❌ Nenhum produto excluído. Os IDs {ids_erro} possuem vendas vinculadas e não podem ser removidos.',
            'excluidos': 0,
            'ids_erro': ids_erro
        })
    if ids_erro:
        return jsonify({
            'ok': True,
            'mensagem': f'⚠️ {excluidos} produto(s) excluído(s), mas os IDs {ids_erro} não puderam ser removidos (vendas vinculadas).',
            'excluidos': excluidos,
            'ids_erro': ids_erro
        })
    return jsonify({'ok': True, 'mensagem': f'🗑️ {excluidos} produto(s) excluído(s) com sucesso!', 'excluidos': excluidos})


@produtos_bp.route('/produtos/atualizar_tipo_batch', methods=['POST'])
def produtos_atualizar_tipo_batch():
    """Atualiza o campo tipo de vários produtos (usado em 'Corrigir Categoria' para OUTROS)."""
    data = request.get_json(silent=True) or {}
    updates = data.get('updates', [])
    if not updates:
        return jsonify({'ok': False, 'mensagem': 'Nenhuma alteração informada.'}), 400
    permitidos = {'ALHO', 'SACOLA', 'CAFE', 'BACALHAU'}
    ok_count = 0
    for u in updates:
        pid = u.get('id')
        novoTipo = (u.get('tipo') or '').strip().upper()
        if pid is None or novoTipo not in permitidos:
            continue
        p = query_tenant(Produto).filter_by(id=pid).first()
        if not p or p.tipo != 'OUTROS':
            continue
        p.tipo = novoTipo
        ok_count += 1
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'mensagem': str(e)}), 500
    return jsonify({'ok': True, 'mensagem': f'{ok_count} produto(s) atualizado(s).', 'atualizados': ok_count})


@produtos_bp.route('/produtos/importar', methods=['GET', 'POST'])
def importar_produtos():
    """Importação em lote de produtos. Exige admin além do tenant guard."""
    @admin_required
    def _importar():
        if request.method == 'POST':
            if 'arquivo' not in request.files:
                return render_template('produtos/importar.html', erros_detalhados=['Nenhum arquivo selecionado. Escolha um arquivo e tente novamente.'], sucesso=0, erros=1)
            arquivo = request.files['arquivo']
            if arquivo.filename == '':
                return render_template('produtos/importar.html', erros_detalhados=['Nenhum arquivo selecionado. Escolha um arquivo e tente novamente.'], sucesso=0, erros=1)
            filepath = None
            try:
                filename = secure_filename(arquivo.filename)
                filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
                arquivo.save(filepath)
                is_raw = False
                if filename.endswith('.csv'):
                    df, is_raw = _load_csv_produtos_flexible(filepath)
                    if df is None:
                        return render_template('produtos/importar.html', erros_detalhados=['O arquivo CSV está vazio ou não pôde ser lido.'], sucesso=0, erros=1)
                else:
                    df = pd.read_excel(filepath)
                if not is_raw:
                    rename_dict = {}
                    seen_canonical = set()
                    for col in list(df.columns):
                        n = _normalizar_nome_coluna(col)
                        can = COLUNA_ARQUIVO_PARA_BANCO.get(n)
                        if can and can not in seen_canonical:
                            rename_dict[col] = can
                            seen_canonical.add(can)
                    df = df.rename(columns=rename_dict)
                sucesso = 0
                erros = 0
                ignorados = 0
                erros_detalhados = []
                outros_nomes = []
                for idx, row in df.iterrows():
                    linha_num = (idx + 1) if is_raw else (idx + 2)
                    v = _row_get(row, 'nome_produto', 'produto', 'nome')
                    nome_produto_arquivo = _strip_quotes(v) if v is not None else None
                    if nome_produto_arquivo == '':
                        nome_produto_arquivo = None
                    try:
                        tipo_raw = _strip_quotes(_row_get(row, 'tipo', 'categoria') or '').upper()
                        tipo = _normalizar_tipo_ui(tipo_raw)
                        nacionalidade = _strip_quotes(_row_get(row, 'nacionalidade', 'origem') or '')
                        marca = _strip_quotes(_row_get(row, 'marca') or '')
                        tamanho_raw = _row_get(row, 'tamanho', 'classificacao')
                        tamanho = _strip_quotes(tamanho_raw or '').upper() if tamanho_raw is not None else ''
                        tamanho = (tamanho or '').strip()
                        contexto = nome_produto_arquivo or f'{tipo} {marca} {tamanho}'.strip() or 'linha'
                        contexto = (contexto[:45] + '...') if len(contexto) > 45 else contexto

                        if tipo == 'SACOLA':
                            nacionalidade = 'N/A'
                            marca = 'SOPACK'
                            t_norm = tamanho.replace(' ', '')
                            if t_norm not in ('P', 'M', 'G', 'S/N'):
                                erros_detalhados.append(_msg_linha(linha_num, contexto, "Para SACOLA, o tamanho deve ser P, M, G ou S/N. Valor informado inválido ou vazio", True))
                                erros += 1
                                continue
                            tamanho = t_norm
                        else:
                            nacionalidade = nacionalidade or ''
                            tamanho = tamanho or ''
                            if not marca:
                                erros_detalhados.append(_msg_linha(linha_num, contexto, "O campo 'marca' está vazio. Preencha com o nome da marca (ex: IMPORFOZ)", True))
                                erros += 1
                                continue

                        quantidade = _parse_quantidade(_row_get(row, 'quantidade_entrada', 'quantidade', 'qtd'))
                        if quantidade is None or quantidade < 0:
                            qraw = _row_get(row, 'quantidade_entrada', 'quantidade', 'qtd')
                            erros_detalhados.append(_msg_linha(linha_num, contexto, f"A quantidade está vazia ou inválida ({qraw}). Use um número inteiro (ex: 10)", True))
                            erros += 1
                            continue
                        fornecedor_valor = _strip_quotes(_row_get(row, 'fornecedor') or '')
                        preco_raw = _row_get(row, 'preco_custo', 'preco', 'preço')
                        preco_custo_valor = _parse_preco(preco_raw)
                        caminhoneiro_valor = _strip_quotes(_row_get(row, 'caminhoneiro') or '')
                        if not fornecedor_valor:
                            erros_detalhados.append(_msg_linha(linha_num, contexto, "O campo 'fornecedor' está vazio. Use DESTAK ou PATY", True))
                            erros += 1
                            continue
                        if preco_custo_valor is None:
                            txt = f"O preço '{preco_raw}' não pôde ser convertido. Use formato brasileiro (ex: 143,00 ou -120,00 para ajustes) ou use ponto como decimal" if preco_raw else "O campo 'preco_custo' (ou 'preco') está vazio"
                            erros_detalhados.append(_msg_linha(linha_num, contexto, txt, True))
                            erros += 1
                            continue
                        if not caminhoneiro_valor:
                            erros_detalhados.append(_msg_linha(linha_num, contexto, "O campo 'caminhoneiro' está vazio. Informe o nome do caminhoneiro", True))
                            erros += 1
                            continue
                        fornecedor_valor = fornecedor_valor.upper()
                        data_chegada_valor = _row_get(row, 'data_chegada', 'data')
                        if data_chegada_valor is not None and pd.notna(data_chegada_valor):
                            data_parsed, _ = _parse_data_flex(data_chegada_valor)
                            data_chegada = data_parsed if data_parsed else date.today()
                        else:
                            data_chegada = date.today()
                        if nome_produto_arquivo:
                            nome_produto = nome_produto_arquivo
                        else:
                            nome_produto = gerar_nome_produto(tipo, nacionalidade, marca, data_chegada, tamanho)
                        dup = query_tenant(Produto).filter(
                            Produto.nome_produto == nome_produto,
                            Produto.data_chegada == data_chegada,
                            Produto.quantidade_entrada == quantidade,
                            Produto.fornecedor == fornecedor_valor
                        ).first()
                        if dup:
                            ignorados += 1
                            continue
                        produto_existente = query_tenant(Produto).filter_by(nome_produto=nome_produto).first()
                        if produto_existente:
                            produto_existente.estoque_atual += quantidade
                            produto_existente.preco_custo = Decimal(str(preco_custo_valor))
                            produto_existente.fornecedor = fornecedor_valor
                            produto_existente.caminhoneiro = caminhoneiro_valor
                            db.session.commit()
                            sucesso += 1
                        else:
                            produto = Produto(
                                tipo=tipo,
                                nacionalidade=nacionalidade,
                                marca=marca,
                                tamanho=tamanho,
                                fornecedor=fornecedor_valor,
                                caminhoneiro=caminhoneiro_valor,
                                preco_custo=Decimal(str(preco_custo_valor)),
                                quantidade_entrada=quantidade,
                                estoque_atual=quantidade,
                                data_chegada=data_chegada,
                                nome_produto=nome_produto,
                                empresa_id=empresa_id_atual()
                            )
                            db.session.add(produto)
                            db.session.commit()
                            sucesso += 1
                            if tipo == 'OUTROS':
                                outros_nomes.append(nome_produto)
                    except Exception as e:
                        db.session.rollback()
                        ctx = (nome_produto_arquivo or f'linha {linha_num}')
                        ctx = (ctx[:45] + '...') if len(ctx) > 45 else ctx
                        erros_detalhados.append(_msg_linha(linha_num, ctx, str(e), True))
                        erros += 1
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
                if erros > 0:
                    return render_template('produtos/importar.html', erros_detalhados=erros_detalhados, sucesso=sucesso, erros=erros, ignorados=ignorados)
                msg = f'Importação concluída: {sucesso} novo(s).'
                if ignorados > 0:
                    msg += f' {ignorados} ignorado(s) por já existirem.'
                flash(msg, 'success')
                if outros_nomes:
                    nomes_lista = ', '.join(outros_nomes[:20])
                    if len(outros_nomes) > 20:
                        nomes_lista += f' e mais {len(outros_nomes) - 20}.'
                    flash(f'Atenção: {len(outros_nomes)} produto(s) foram movidos para "OUTROS" por falta de categoria: {nomes_lista}', 'warning')
                return redirect(url_for('produtos.listar_produtos'))
            except Exception as e:
                db.session.rollback()
                if filepath and os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                return render_template('produtos/importar.html', erros_detalhados=[f'Erro ao processar o arquivo: {str(e)}'], sucesso=0, erros=1)
        return render_template('produtos/importar.html')

    return _importar()


# ============================================================
# API endpoints
# ============================================================

@produtos_bp.route('/api/produtos/<int:produto_id>/fotos')
def get_fotos_produto(produto_id):
    """Retorna lista de fotos do produto.

    Cada item tem ``thumb`` (versão leve para a grade do modal) e ``full``
    (versão original para abrir em nova aba/lightbox). Em Cloudinary, o
    ``thumb`` aplica ``w_300,h_300,c_fill,q_auto,f_auto`` para reduzir
    drasticamente o peso de transferência na listagem.

    Mantém retrocompatibilidade: clientes antigos podem iterar como string
    via ``arr.map(o => o.full)``.
    """
    # Multi-tenant: valida que o produto pertence ao tenant do usuário antes de
    # expor suas fotos (ProdutoFoto não tem empresa_id, herda via produto).
    produto = query_tenant(Produto).filter_by(id=produto_id).first_or_404()
    fotos = ProdutoFoto.query.filter_by(produto_id=produto.id).all()
    items = []
    for f in fotos:
        if f.arquivo and (f.arquivo.startswith('http://') or f.arquivo.startswith('https://')):
            full = f.arquivo
        elif f.arquivo:
            full = url_for('static', filename=f'uploads/{f.arquivo}')
        else:
            continue
        items.append({
            'thumb': _cloudinary_thumb_url(full, w=300, h=300),
            'full': full,
        })
    return jsonify(items)


@produtos_bp.route('/api/produto/<int:id>')
def api_produto(id):
    produto = query_tenant(Produto).filter_by(id=id).first_or_404()
    return jsonify({
        'nome': produto.nome_produto,
        'estoque': produto.estoque_atual,
        'preco_custo': float(produto.preco_custo)
    })
