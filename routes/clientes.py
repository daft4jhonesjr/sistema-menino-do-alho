"""Blueprint ``clientes`` — CRUD de clientes do tenant.

Rotas extraídas do legado ``app.py`` (Fase 2 da refatoração):
    * GET  /clientes                              listar_clientes
    * POST /api/clientes/padronizar_maiusculas    padronizar_clientes_maiusculas
    * GET/POST /clientes/novo                     novo_cliente
    * GET/POST /clientes/editar/<id>              editar_cliente
    * POST /clientes/excluir/<id>                 excluir_cliente
    * POST /cliente/<id>/toggle_ativo             toggle_ativo_cliente
    * GET  /clientes/<id>/extrato                 extrato_cliente
    * POST /clientes/<id>/extrato/whatsapp        extrato_whatsapp
    * POST /bulk_delete_clientes                  bulk_delete_clientes
    * GET/POST /clientes/importar                 importar_clientes  (admin)
    * POST /cliente/<id>/receber_lote             receber_lote_cliente
    * GET  /api/clientes/<id>/prazo-padrao        prazo_padrao_cliente

Endpoints novos: prefixo ``clientes.`` (ex.: ``clientes.listar_clientes``).

Proteção automática de tenant
-----------------------------
Toda rota deste blueprint exige ``login_required`` + ``tenant_required``.
Em vez de repetir os decorators em cada handler, aplicamos via
``before_request`` — qualquer rota nova adicionada aqui herda a proteção.

Rotas com necessidade adicional de ``@admin_required`` (ex.: importar)
mantêm o decorator no próprio handler.
"""
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import io
import os
import re
import urllib.parse

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, current_app,
)
from flask_login import current_user
from sqlalchemy import func, or_, desc
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError, OperationalError, TimeoutError as SATimeoutError
import pandas as pd
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename

from models import db, Cliente, Venda, LancamentoCaixa, Produto
from services.auth_utils import tenant_required, admin_required, _is_ajax
from services.db_utils import query_tenant, empresa_id_atual
from services.cache_utils import limpar_cache_dashboard
from services.error_utils import erro_json, erro_flash
from services.config_helpers import registrar_log, _EXTERNAL_TIMEOUT
from services.vendas_services import _resincronizar_pagamento_venda
from services.csv_utils import (
    _msg_linha, _strip_quotes,
    _parse_clientes_raw_tsv, _sanitizar_cnpj_importacao,
)


clientes_bp = Blueprint('clientes', __name__)


def _vendas_pendentes_e_total(cliente_id):
    """Retorna (lista de vendas com saldo, total devido Decimal)."""
    saldo_sql = (Venda.preco_venda * Venda.quantidade_venda) - func.coalesce(Venda.valor_pago, 0)
    vendas_pendentes = query_tenant(Venda).filter(
        Venda.cliente_id == cliente_id,
        func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA',
        (Venda.preco_venda * Venda.quantidade_venda) > 0,
        or_(
            Venda.situacao.in_(['PENDENTE', 'PARCIAL']),
            saldo_sql > Decimal('0.01'),
        ),
    ).options(joinedload(Venda.produto)).order_by(Venda.data_venda).all()

    total_devido = sum(
        (
            Decimal(str(v.calcular_total() or Decimal('0.00')))
            - Decimal(str(v.valor_pago or Decimal('0.00')))
        )
        for v in vendas_pendentes
    )
    if total_devido < Decimal('0.00'):
        total_devido = Decimal('0.00')
    return vendas_pendentes, total_devido


def _telefone_whatsapp_cliente(cliente):
    """Normaliza telefone do cliente para wa.me (com DDI 55)."""
    telefone = (getattr(cliente, 'telefone', None) or getattr(cliente, 'telefone_secundario', None) or '').strip()
    if not telefone:
        return None
    telefone_limpo = re.sub(r'\D', '', telefone)
    if not telefone_limpo:
        return None
    if len(telefone_limpo) <= 11:
        telefone_limpo = '55' + telefone_limpo
    return telefone_limpo


def _fmt_brl(valor):
    return (
        f"{float(valor):,.2f}"
        .replace(',', 'X')
        .replace('.', ',')
        .replace('X', '.')
    )


# ============================================================
# Proteção automática de tenant para todo o blueprint
# ============================================================
@clientes_bp.before_request
def _exigir_tenant_em_todas_rotas():
    """Roda antes de cada handler do blueprint — equivale a aplicar
    ``@login_required`` + ``@tenant_required`` em todas as rotas sem
    precisar repetir decorators.

    Reusa o decorator ``tenant_required`` definido em ``app.py`` para
    centralizar as regras (MASTER → /master-admin, sem empresa_id →
    /login com flash). Retornar uma response aborta o handler; retornar
    ``None`` continua o pipeline.
    """
    @tenant_required
    def _ok():
        return None

    return _ok()


# ============================================================
# Helpers exclusivos de clientes
# ============================================================

def _processar_linhas_clientes_upsert(linhas, erros_detalhados, sucesso_ref, erros_ref, linha_offset=0):
    """Processa lista de dicts (nome_cliente, razao_social, cnpj, cidade,
    endereco, telefone). Upsert por ``nome_cliente`` (Apelido).

    Atualiza ``sucesso_ref[0]`` e ``erros_ref[0]`` (passados como listas
    para emular passagem por referência) e faz append em ``erros_detalhados``.
    """
    for idx, row in enumerate(linhas):
        linha_num = linha_offset + idx + 1
        nome = (row.get('nome_cliente') or '').strip()
        razao_social = (row.get('razao_social') or '').strip() or nome
        cnpj = row.get('cnpj')
        cidade = (row.get('cidade') or '').strip()
        contexto = (nome[:40] + '...') if nome and len(nome) > 40 else (nome or 'sem nome')
        try:
            if not nome:
                erros_detalhados.append(_msg_linha(linha_num, '', "O campo Apelido (nome) está vazio", True))
                erros_ref[0] += 1
                continue
            endereco = (row.get('endereco') or '').strip() or None
            telefone_tsv = (row.get('telefone') or row.get('whatsapp') or '').strip() or None
            cliente = (
                Cliente.query
                .filter_by(empresa_id=empresa_id_atual())
                .filter(func.lower(Cliente.nome_cliente) == nome.lower())
                .first()
            )
            if cliente:
                cliente.razao_social = razao_social or None
                cliente.cnpj = cnpj
                cliente.cidade = cidade or None
                cliente.endereco = endereco
                if telefone_tsv:
                    cliente.telefone = telefone_tsv
                db.session.commit()
                sucesso_ref[0] += 1
            else:
                if cnpj and Cliente.query.filter_by(empresa_id=empresa_id_atual(), cnpj=cnpj).first():
                    erros_detalhados.append(_msg_linha(linha_num, nome, "O CNPJ já está cadastrado para outro cliente. Use um CNPJ único.", True))
                    erros_ref[0] += 1
                    continue
                cliente = Cliente(
                    nome_cliente=nome,
                    telefone=telefone_tsv,
                    razao_social=razao_social or None,
                    cnpj=cnpj,
                    cidade=cidade or None,
                    endereco=endereco,
                    empresa_id=empresa_id_atual(),
                )
                db.session.add(cliente)
                db.session.commit()
                sucesso_ref[0] += 1
        except IntegrityError as e:
            db.session.rollback()
            erros_detalhados.append(_msg_linha(linha_num, contexto, f"CNPJ duplicado ou conflito: {str(e)}", True))
            erros_ref[0] += 1
        except Exception as e:
            db.session.rollback()
            erros_detalhados.append(_msg_linha(linha_num, contexto, str(e), True))
            erros_ref[0] += 1


# ============================================================
# Rotas
# ============================================================

@clientes_bp.route('/clientes')
def listar_clientes():
    """Listagem de clientes com paginação SQL + busca server-side opcional.

    Mudanças de performance:
    * Paginação backend via ``?page=`` e ``?per_page=`` (default 200,
      máx 500). Antes carregava 500 clientes a cada visita,
      independentemente do que o usuário ia ver.
    * Busca server-side via ``?q=`` (ilike) — usa o índice em
      ``Cliente.nome_cliente`` e em ``Cliente.razao_social``. Antes a
      única busca era client-side em JS, que dependia de ter os 500
      clientes carregados no HTML.

    O template antigo continua funcional: se o caller não passar
    ``?q=`` nem ``?page=``, recebe a primeira página (200 clientes)
    com a mesma ordenação default, e a busca client-side já existente
    funciona dentro desses 200.
    """
    ordem_param = (request.args.get('ordem') or '').strip().lower()
    q = (request.args.get('q') or '').strip()
    try:
        page = max(1, int(request.args.get('page', 1) or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = int(request.args.get('per_page', 200) or 200)
    except (TypeError, ValueError):
        per_page = 200
    per_page = max(20, min(per_page, 500))

    base = query_tenant(Cliente)
    if q:
        like = f"%{q}%"
        base = base.filter(or_(
            Cliente.nome_cliente.ilike(like),
            Cliente.razao_social.ilike(like),
            Cliente.cnpj.ilike(like),
        ))

    if ordem_param in ('desc', 'id_decrescente'):
        ordem = 'id_decrescente'
        base = base.order_by(Cliente.id.desc())
    elif ordem_param in ('asc', 'id_crescente'):
        ordem = 'id_crescente'
        base = base.order_by(Cliente.id.asc())
    else:
        # Padrão: alfabética por nome (agrupa filiais com mesmo prefixo, ex. COMAL_*)
        ordem = 'nome_asc'
        base = base.order_by(Cliente.nome_cliente.asc())

    clientes = base.limit(per_page).offset((page - 1) * per_page).all()

    # Top 10 clientes por lucro total (histórico do tenant).
    # Mesma fórmula do dashboard: (preço venda − custo) × quantidade.
    # Sem coluna persistida — só ranking dinâmico para a estrela VIP.
    top_10_ids = []
    try:
        lucro_expr = (Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda
        top_rows = (
            query_tenant(Venda)
            .with_entities(
                Venda.cliente_id,
                func.sum(lucro_expr).label('total_lucro'),
            )
            .join(Produto, Venda.produto_id == Produto.id)
            .filter(
                Venda.cliente_id.isnot(None),
                func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA',
            )
            .group_by(Venda.cliente_id)
            .order_by(desc('total_lucro'))
            .limit(10)
            .all()
        )
        top_10_ids = [int(row.cliente_id) for row in top_rows if row.cliente_id]
    except Exception as e_top:
        current_app.logger.warning(f'[clientes] Falha ao calcular top 10 lucro: {e_top}')
        top_10_ids = []

    return render_template(
        'clientes/listar.html',
        clientes=clientes,
        ordem=ordem,
        q=q,
        page=page,
        per_page=per_page,
        has_next=len(clientes) >= per_page,
        top_10_ids=top_10_ids,
    )


def _prazo_efetivo_venda(venda):
    """Prazo em dias da venda: coluna explícita ou (vencimento - data da venda)."""
    prazo = getattr(venda, 'prazo_dias', None)
    try:
        if prazo is not None:
            n = int(prazo)
            if n >= 0:
                return n
    except (TypeError, ValueError):
        pass
    data_venda = getattr(venda, 'data_venda', None)
    data_venc = getattr(venda, 'data_vencimento', None)
    if data_venda and data_venc:
        try:
            delta = (data_venc - data_venda).days
        except Exception:
            return None
        if delta >= 0:
            return int(delta)
    return None


@clientes_bp.route('/api/clientes/<int:cliente_id>/prazo-padrao', methods=['GET'])
def prazo_padrao_cliente(cliente_id):
    """Infere o prazo (dias) mais comum nas vendas recentes do cliente.

    Usa as últimas 3 a 5 *pedidos* (agrupados por data+NF) que tenham
    ``prazo_dias`` ou vencimento preenchido. Empate: o mais recente.
    Sem histórico: ``{"prazo_dias": null}``.
    """
    cliente = query_tenant(Cliente).filter_by(id=cliente_id).first()
    if not cliente:
        return jsonify({'prazo_dias': None}), 404

    vendas = (
        query_tenant(Venda)
        .filter(
            Venda.cliente_id == cliente_id,
            func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA',
        )
        .order_by(Venda.data_venda.desc(), Venda.id.desc())
        .limit(40)
        .all()
    )

    vistos = set()
    prazos = []
    for venda in vendas:
        nf_norm = (venda.nf or '').strip()
        chave = (venda.data_venda, nf_norm)
        if chave in vistos:
            continue
        vistos.add(chave)
        prazo = _prazo_efetivo_venda(venda)
        if prazo is None:
            continue
        prazos.append(prazo)
        if len(prazos) >= 5:
            break

    if not prazos:
        return jsonify({'prazo_dias': None})

    contagem = Counter(prazos)
    max_freq = max(contagem.values())
    # Empate: o mais recente (primeiro da lista, já ordenada desc).
    escolhido = next(p for p in prazos if contagem[p] == max_freq)
    return jsonify({'prazo_dias': int(escolhido)})


@clientes_bp.route('/api/clientes/padronizar_maiusculas', methods=['POST'])
def padronizar_clientes_maiusculas():
    """Converte campos textuais dos clientes do tenant atual para MAIÚSCULAS.

    Operação isolada: só altera textos livres (nome, razão, cidade, endereço,
    contatos, UF). Não toca em id, CNPJ, telefone ou e-mail. Em qualquer
    falha faz ``rollback`` para preservar a integridade.
    """
    def _upper_se_texto(valor):
        if valor is None:
            return None, False
        original = str(valor)
        if not original.strip():
            return valor, False
        novo = original.upper()
        return novo, novo != original

    try:
        clientes = query_tenant(Cliente).all()
        alterados = 0
        campos_tocados = 0

        for cliente in clientes:
            cliente_mudou = False

            for attr, max_len in (
                ('nome_cliente', 200),
                ('razao_social', 200),
                ('cidade', 100),
                ('bairro', 100),
                ('endereco', 255),
                ('nome_contato', 100),
                ('nome_contato_secundario', 100),
                ('estado', 2),
            ):
                atual = getattr(cliente, attr, None)
                novo, mudou = _upper_se_texto(atual)
                if not mudou:
                    continue
                if max_len is not None and novo is not None:
                    novo = novo[:max_len]
                setattr(cliente, attr, novo)
                cliente_mudou = True
                campos_tocados += 1

            if cliente_mudou:
                alterados += 1

        if alterados:
            db.session.commit()
            registrar_log(
                'EDITAR',
                'CLIENTES',
                f'Padronização maiúsculas: {alterados} cliente(s), {campos_tocados} campo(s).',
            )
        else:
            db.session.rollback()

        return jsonify({
            'ok': True,
            'alterados': alterados,
            'campos': campos_tocados,
            'mensagem': (
                f'{alterados} cliente(s) atualizado(s) ({campos_tocados} campo(s)).'
                if alterados
                else 'Todos os clientes já estavam em maiúsculas.'
            ),
        })
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(
            f'[padronizar_clientes_maiusculas] falha: {e}',
            exc_info=True,
        )
        return jsonify({
            'ok': False,
            'alterados': 0,
            'mensagem': 'Falha ao padronizar clientes. Nenhuma alteração foi salva.',
        }), 500


@clientes_bp.route('/clientes/novo', methods=['GET', 'POST'])
def novo_cliente():
    """
    Cria um novo cliente.

    GET: Exibe formulário vazio.
    POST: Recebe nome_cliente, cnpj, telefone, etc. e persiste no banco.
    """
    if request.method == 'POST':
        try:
            cnpj = re.sub(r'\D', '', request.form.get('cnpj', '').strip()) or None
            if cnpj:
                cliente_existente = query_tenant(Cliente).filter_by(cnpj=cnpj).first()
                if cliente_existente:
                    msg = f'CNPJ {cnpj} já está cadastrado para o cliente {cliente_existente.nome_cliente}'
                    if _is_ajax():
                        return jsonify(ok=False, mensagem=msg), 400
                    flash(msg, 'error')
                    return render_template('clientes/formulario.html', cliente=None)

            nome_cliente = (request.form.get('nome_cliente') or '').strip().upper()
            if not nome_cliente:
                msg = 'Nome do cliente é obrigatório.'
                if _is_ajax():
                    return jsonify(ok=False, mensagem=msg), 400
                flash(msg, 'error')
                return render_template('clientes/formulario.html', cliente=None)
            cliente = Cliente(
                nome_cliente=nome_cliente,
                telefone=(request.form.get('telefone', '') or '').strip() or None,
                nome_contato=(request.form.get('nome_contato', '') or '').strip().upper()[:100] or None,
                telefone_secundario=(request.form.get('telefone_secundario', '') or '').strip()[:20] or None,
                nome_contato_secundario=(request.form.get('nome_contato_secundario', '') or '').strip().upper()[:100] or None,
                razao_social=(request.form.get('razao_social', '') or '').strip().upper(),
                cnpj=cnpj,
                cidade=(request.form.get('cidade', '') or '').strip().upper(),
                estado=(request.form.get('estado') or '').strip().upper()[:2] or None,
                bairro=(request.form.get('bairro', '') or '').strip().upper()[:100] or None,
                endereco=(request.form.get('endereco', '') or '').strip().upper() or None,
                empresa_id=empresa_id_atual(),
            )
            db.session.add(cliente)
            db.session.commit()
            registrar_log('CRIAR', 'CLIENTES', f"Cliente #{cliente.id} — {cliente.nome_cliente} criado.")
            if _is_ajax():
                return jsonify(ok=True, mensagem='Cliente cadastrado com sucesso!')
            flash('Cliente cadastrado com sucesso!', 'success')
            return redirect(url_for('clientes.listar_clientes'))
        except Exception as e:
            db.session.rollback()
            msg = f'Erro ao cadastrar cliente: {str(e)}'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 500
            flash(msg, 'error')
    return render_template('clientes/formulario.html', cliente=None)


@clientes_bp.route('/clientes/editar/<int:id>', methods=['GET', 'POST'])
def editar_cliente(id):
    try:
        # P0 (laudo de contenção no pool de conexões): o checkout da conexão
        # para este SELECT pode estourar `pool_timeout`/`lock_timeout` sob
        # concorrência. Antes esta linha ficava FORA do try, então um
        # OperationalError/TimeoutError aqui escapava "cru" para o handler
        # global do Flask (que por sua vez reencadeia novas queries via
        # context_processors ao renderizar a página de erro). Agora entra
        # no try — o 404 genuíno (Cliente inexistente/outro tenant) continua
        # propagando normalmente via o `except HTTPException: raise` abaixo.
        cliente = query_tenant(Cliente).filter_by(id=id).first_or_404()
        if request.method == 'POST':
            cnpj_raw = request.form.get('cnpj', '').strip() or None
            cnpj = None
            if cnpj_raw:
                cnpj_limpo = re.sub(r'\D', '', cnpj_raw)
                cnpj = cnpj_limpo if len(cnpj_limpo) == 14 else None
            if cnpj and cnpj != (cliente.cnpj or ''):
                cliente_existente = query_tenant(Cliente).filter_by(cnpj=cnpj).first()
                if cliente_existente and cliente_existente.id != cliente.id:
                    flash(f'CNPJ já está cadastrado para o cliente {cliente_existente.nome_cliente}', 'error')
                    return render_template('clientes/formulario.html', cliente=cliente)

            nome_raw = ((request.form.get('nome_cliente') or '').strip() or (cliente.nome_cliente or '')).upper()
            razao_raw = (request.form.get('razao_social') or '').strip().upper()
            cidade_raw = (request.form.get('cidade') or '').strip().upper()
            estado_raw = (request.form.get('estado') or '').strip().upper()[:2] or None
            bairro_raw = (request.form.get('bairro') or '').strip().upper() or None
            endereco_raw = (request.form.get('endereco') or '').strip().upper() or None
            telefone_raw = (request.form.get('telefone', '') or '').strip() or None
            nome_contato_raw = (request.form.get('nome_contato', '') or '').strip().upper() or None
            telefone_secundario_raw = (request.form.get('telefone_secundario', '') or '').strip() or None
            nome_contato_secundario_raw = (request.form.get('nome_contato_secundario', '') or '').strip().upper() or None

            cliente.nome_cliente = nome_raw[:200]
            cliente.telefone = telefone_raw[:20] if telefone_raw else None
            cliente.nome_contato = nome_contato_raw[:100] if nome_contato_raw else None
            cliente.telefone_secundario = telefone_secundario_raw[:20] if telefone_secundario_raw else None
            cliente.nome_contato_secundario = nome_contato_secundario_raw[:100] if nome_contato_secundario_raw else None
            cliente.razao_social = razao_raw[:200] if razao_raw else ''
            cliente.cnpj = cnpj
            cliente.cidade = cidade_raw[:100] if cidade_raw else ''
            cliente.estado = estado_raw
            cliente.bairro = bairro_raw[:100] if bairro_raw else None
            cliente.endereco = endereco_raw[:255] if endereco_raw else None
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                if _is_ajax():
                    return jsonify(ok=False, mensagem='Erro: Este CNPJ já está cadastrado no sistema.'), 400
                flash('Erro: Este CNPJ já está cadastrado no sistema.', 'error')
                return redirect(url_for('clientes.listar_clientes'))
            registrar_log('EDITAR', 'CLIENTES', f"Cliente #{cliente.id} — {cliente.nome_cliente} editado.")
            if _is_ajax():
                return jsonify(ok=True, mensagem='Cliente atualizado com sucesso!')
            flash('Cliente atualizado com sucesso!', 'success')
            return redirect(url_for('clientes.listar_clientes'))

        return render_template('clientes/formulario.html', cliente=cliente)

    except IntegrityError:
        db.session.rollback()
        if _is_ajax():
            return jsonify(ok=False, mensagem='Erro: Este CNPJ já está cadastrado no sistema.'), 400
        flash('Erro: Este CNPJ já está cadastrado no sistema.', 'error')
        return redirect(url_for('clientes.listar_clientes'))
    except (OperationalError, SATimeoutError) as e:
        # P0 (laudo de contenção no pool de conexões): timeout/queda de
        # conexão ao resgatar ou salvar o cliente (pool esgotado, lock_timeout
        # do Postgres, etc.). Captura amigável em vez de deixar a exceção
        # crua estourar e reencadear novas queries no handler global de erro.
        db.session.rollback()
        current_app.logger.warning(f'editar_cliente:{id} — banco ocupado: {e}')
        flash('O banco de dados está ocupado, tente novamente em alguns instantes.', 'warning')
        return redirect(url_for('clientes.listar_clientes'))
    except HTTPException:
        # Preserva o 404 genuíno do first_or_404() (cliente inexistente ou
        # de outro tenant) — não deve virar um flash genérico de erro 500.
        raise
    except Exception as e:
        db.session.rollback()
        erro_flash(e, 'Erro interno ao processar cliente. Tente novamente.', contexto=f'editar_cliente:{id}')
        return redirect(url_for('clientes.listar_clientes'))


@clientes_bp.route('/clientes/excluir/<int:id>', methods=['POST'])
def excluir_cliente(id):
    cliente = query_tenant(Cliente).filter_by(id=id).first_or_404()
    nome_cliente_del = cliente.nome_cliente
    try:
        db.session.delete(cliente)
        db.session.commit()
        registrar_log('EXCLUIR', 'CLIENTES', f"Cliente #{id} — {nome_cliente_del} excluído permanentemente.")
        flash('Cliente excluído com sucesso!', 'success')
    except Exception:
        db.session.rollback()
        flash('Não é possível excluir este cliente, pois ele possui vínculos no sistema.', 'error')
    return redirect(url_for('clientes.listar_clientes'))


@clientes_bp.route('/cliente/<int:id>/toggle_ativo', methods=['POST'])
def toggle_ativo_cliente(id: int):
    """Alterna o status ativo/inativo de um cliente (soft delete)."""
    cliente = query_tenant(Cliente).filter_by(id=id).first_or_404()
    try:
        cliente.ativo = not cliente.ativo
        db.session.commit()
        estado = 'ativado' if cliente.ativo else 'inativado'
        acao_log = 'ATIVAR' if cliente.ativo else 'INATIVAR'
        registrar_log(acao_log, 'CLIENTES', f"Cliente #{cliente.id} — {cliente.nome_cliente} {estado}.")
        if _is_ajax():
            return jsonify(ok=True, ativo=cliente.ativo, mensagem=f'Cliente {cliente.nome_cliente} {estado} com sucesso.')
        flash(f'Cliente {cliente.nome_cliente} {estado} com sucesso.', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao alternar status do cliente {id}: {e}")
        if _is_ajax():
            return jsonify(ok=False, mensagem='Erro ao alterar status do cliente.'), 500
        flash('Erro ao alterar status do cliente.', 'error')
    return redirect(url_for('clientes.listar_clientes'))


@clientes_bp.route('/clientes/<int:cliente_id>/extrato')
def extrato_cliente(cliente_id):
    """Extrato de cobrança: vendas com saldo devedor do cliente.

    Critério de inclusão (defensivo, em camadas):
        1. ``situacao IN ('PENDENTE', 'PARCIAL')`` — caminho normal
           pós-ressincronização do caixa.
        2. **OU** ``valor_pago < calcular_total - 0.01`` — captura
           qualquer venda que esteja marcada como PAGO mas cujo
           ``valor_pago`` divirja do total (sintoma de desync histórico).
           Calcula via SQL para evitar carregar todas as vendas e
           filtrar em Python.

    Sempre filtra fora vendas de valor zero (perdas/brindes) e do
    tipo PERDA. Cálculo do total devido usa estritamente
    ``calcular_total() - valor_pago`` por venda (sem cache).
    """
    cliente = query_tenant(Cliente).filter_by(id=cliente_id).first_or_404()
    vendas_pendentes, total_devido = _vendas_pendentes_e_total(cliente.id)
    data_hoje = datetime.now().strftime('%d/%m/%Y')

    return render_template(
        'extrato.html',
        cliente=cliente,
        vendas=vendas_pendentes,
        total=total_devido,
        data_hoje=data_hoje,
        para_arquivo=False,
    )


@clientes_bp.route('/clientes/<int:cliente_id>/extrato/whatsapp', methods=['POST'])
def extrato_whatsapp(cliente_id):
    """Gera o extrato, salva no Histórico de Ações e abre o WhatsApp com o link."""
    cliente = query_tenant(Cliente).filter_by(id=cliente_id).first_or_404()
    telefone_limpo = _telefone_whatsapp_cliente(cliente)
    if not telefone_limpo:
        return jsonify({
            'ok': False,
            'erro': 'Este cliente não possui um telefone cadastrado.',
        }), 400

    vendas_pendentes, total_devido = _vendas_pendentes_e_total(cliente.id)
    data_hoje = datetime.now().strftime('%d/%m/%Y')
    agora = datetime.now()
    nome_safe = re.sub(r'[^\w\-]+', '_', (cliente.nome_cliente or f'cliente_{cliente.id}'))[:40]
    nome_arquivo = f"extrato_{nome_safe}_{agora.strftime('%Y_%m_%d_%H%M%S')}.html"
    eid = empresa_id_atual() or 0

    html_bytes = render_template(
        'extrato.html',
        cliente=cliente,
        vendas=vendas_pendentes,
        total=total_devido,
        data_hoje=data_hoje,
        para_arquivo=True,
    ).encode('utf-8')

    arquivo_anexo = None
    # 1) Disco local
    try:
        pasta_rel = os.path.join('extratos', str(eid))
        pasta_abs = os.path.join(current_app.root_path, pasta_rel)
        os.makedirs(pasta_abs, exist_ok=True)
        caminho_abs = os.path.join(pasta_abs, nome_arquivo)
        with open(caminho_abs, 'wb') as fh:
            fh.write(html_bytes)
        arquivo_anexo = os.path.join(pasta_rel, nome_arquivo).replace('\\', '/')
    except Exception as e_disk:
        current_app.logger.warning(f'[extrato-wa] Falha ao salvar extrato local: {e_disk}')

    # 2) Cloudinary (link público para o destinatário do WhatsApp)
    url_publica = None
    try:
        import cloudinary.uploader

        _cloudinary_configured = (
            os.environ.get('CLOUDINARY_URL')
            or (os.environ.get('CLOUDINARY_CLOUD_NAME')
                and os.environ.get('CLOUDINARY_API_KEY'))
        )
        if _cloudinary_configured:
            public_id = f"menino_do_alho/extratos/emp_{eid}/{nome_arquivo.replace('.html', '')}"
            upload_result = cloudinary.uploader.upload(
                io.BytesIO(html_bytes),
                public_id=public_id,
                resource_type='raw',
                format='html',
                timeout=_EXTERNAL_TIMEOUT,
            )
            url_cloud = (upload_result.get('secure_url') or upload_result.get('url') or '').strip()
            if url_cloud:
                url_publica = url_cloud
                arquivo_anexo = url_cloud
    except Exception as e_cloud:
        current_app.logger.warning(
            f'[extrato-wa] Upload Cloudinary falhou (mantendo cópia local se houver): {e_cloud}'
        )

    total_fmt = _fmt_brl(total_devido)
    descricao = (
        f'Extrato enviado via WhatsApp — {cliente.nome_cliente} '
        f'(cliente #{cliente.id}, total R$ {total_fmt}, {len(vendas_pendentes)} item(ns)): {nome_arquivo}'
    )
    log_id = registrar_log('WHATSAPP', 'CLIENTES', descricao, arquivo_anexo=arquivo_anexo)

    if url_publica:
        link_extrato = url_publica
    elif log_id:
        link_extrato = url_for('baixar_backup_historico', log_id=log_id, _external=True)
    else:
        link_extrato = url_for('clientes.extrato_cliente', cliente_id=cliente.id, _external=True)

    mensagem = (
        f"Olá, tudo bem? 🧄\n\n"
        f"Segue o extrato de cobrança de *{cliente.nome_cliente}* "
        f"com total devido de R$ {total_fmt}.\n\n"
        f"📄 Acesse ou baixe o extrato aqui:\n{link_extrato}\n\n"
        f"Qualquer dúvida, estamos à disposição!"
    )
    url_whatsapp = f"https://wa.me/{telefone_limpo}?text={urllib.parse.quote(mensagem)}"

    return jsonify({'ok': True, 'wa_url': url_whatsapp})


@clientes_bp.route('/bulk_delete_clientes', methods=['POST'])
def bulk_delete_clientes():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'ok': False, 'mensagem': 'Nenhum ID informado.'}), 400
    try:
        for id_ in ids:
            cliente = query_tenant(Cliente).filter_by(id=id_).first()
            if cliente:
                db.session.delete(cliente)
        db.session.commit()
        return jsonify({'ok': True, 'mensagem': f'{len(ids)} cliente(s) excluído(s) com sucesso.', 'excluidos': len(ids)})
    except Exception as e:
        db.session.rollback()
        return erro_json(
            e,
            'Falha ao excluir clientes em massa.',
            extras={'ok': False},
            chave_mensagem='mensagem',
            contexto='bulk_delete_clientes',
        )


@clientes_bp.route('/clientes/importar', methods=['GET', 'POST'])
def importar_clientes():
    """Importação em lote de clientes (CSV/Excel/TSV).

    Exige ``admin_required`` adicional além do tenant guard global.
    """
    @admin_required
    def _importar():
        if request.method == 'POST':
            lista_raw = (request.form.get('lista_raw') or '').strip()
            tem_arquivo = 'arquivo' in request.files and request.files['arquivo'] and request.files['arquivo'].filename
            if not lista_raw and not tem_arquivo:
                return render_template('clientes/importar.html', erros_detalhados=['Cole a lista (TAB) no campo de texto ou selecione um arquivo.'], sucesso=0, erros=1)
            filepath = None
            try:
                sucesso = 0
                erros = 0
                erros_detalhados = []
                sucesso_ref = [0]
                erros_ref = [0]

                if lista_raw:
                    linhas = _parse_clientes_raw_tsv(lista_raw)
                    if not linhas:
                        return render_template('clientes/importar.html', erros_detalhados=['Nenhuma linha válida encontrada. Use uma linha por cliente, campos separados por TAB: Apelido, Razão Social, CNPJ, Cidade.'], sucesso=0, erros=1)
                    _processar_linhas_clientes_upsert(linhas, erros_detalhados, sucesso_ref, erros_ref, linha_offset=0)
                    sucesso, erros = sucesso_ref[0], erros_ref[0]
                else:
                    arquivo = request.files['arquivo']
                    filename = secure_filename(arquivo.filename)
                    filepath = os.path.join(current_app.config['UPLOAD_FOLDER'], filename)
                    arquivo.save(filepath)
                    content = None
                    if filename.endswith('.csv'):
                        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                            content = f.read()
                    processado_raw = False
                    if content and content.splitlines():
                        first_line = content.splitlines()[0]
                        if '\t' in first_line:
                            linhas = _parse_clientes_raw_tsv(content)
                            if linhas:
                                _processar_linhas_clientes_upsert(linhas, erros_detalhados, sucesso_ref, erros_ref, linha_offset=0)
                                sucesso, erros = sucesso_ref[0], erros_ref[0]
                                processado_raw = True
                    if not processado_raw:
                        if filename.endswith('.csv'):
                            df = pd.read_csv(filepath, sep=None, engine='python', quoting=3, on_bad_lines='warn')
                        else:
                            df = pd.read_excel(filepath)
                        df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_')
                        first_iter = True
                        for idx, row in df.iterrows():
                            if first_iter:
                                first_iter = False
                            linha_num = idx + 2
                            nome = _strip_quotes(row.get('nome_cliente', row.get('nome', '')))
                            contexto = (nome[:40] + '...') if nome and len(nome) > 40 else (nome or 'sem nome')
                            try:
                                if not nome:
                                    erros_detalhados.append(_msg_linha(linha_num, '', "O campo 'nome_cliente' (ou 'nome') está vazio", True))
                                    erros += 1
                                    continue
                                cnpj_raw = _strip_quotes(row.get('cnpj', '')) or None
                                cnpj = _sanitizar_cnpj_importacao(cnpj_raw) if cnpj_raw else None
                                if cnpj and query_tenant(Cliente).filter_by(cnpj=cnpj).first():
                                    existente = query_tenant(Cliente).filter_by(cnpj=cnpj).first()
                                    erros_detalhados.append(_msg_linha(linha_num, nome, f"O CNPJ já está cadastrado para o cliente '{existente.nome_cliente}'. Use um CNPJ único.", True))
                                    erros += 1
                                    continue
                                endereco = _strip_quotes(row.get('endereco', '')) or None
                                cliente = query_tenant(Cliente).filter(func.lower(Cliente.nome_cliente) == nome.lower()).first()
                                telefone_imp = _strip_quotes(row.get('telefone', row.get('whatsapp', ''))) or None
                                if cliente:
                                    cliente.razao_social = _strip_quotes(row.get('razao_social', row.get('razao', ''))) or nome
                                    cliente.cnpj = cnpj
                                    cliente.cidade = _strip_quotes(row.get('cidade', '')) or None
                                    cliente.endereco = endereco
                                    if telefone_imp:
                                        cliente.telefone = telefone_imp
                                    db.session.commit()
                                    sucesso += 1
                                else:
                                    cliente = Cliente(
                                        nome_cliente=nome,
                                        telefone=telefone_imp,
                                        razao_social=_strip_quotes(row.get('razao_social', row.get('razao', ''))) or None,
                                        cnpj=cnpj,
                                        cidade=_strip_quotes(row.get('cidade', '')) or None,
                                        endereco=endereco,
                                        empresa_id=empresa_id_atual(),
                                    )
                                    db.session.add(cliente)
                                    db.session.commit()
                                    sucesso += 1
                            except Exception as e:
                                db.session.rollback()
                                erros_detalhados.append(_msg_linha(linha_num, contexto, str(e), True))
                                erros += 1

                if filepath and os.path.exists(filepath):
                    os.remove(filepath)
                if erros > 0:
                    return render_template('clientes/importar.html', erros_detalhados=erros_detalhados, sucesso=sucesso, erros=erros)
                flash(f'Importação concluída com sucesso! {sucesso} cliente(s) importado(s).', 'success')
                return redirect(url_for('clientes.listar_clientes'))
            except Exception as e:
                db.session.rollback()
                if filepath and os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
                return render_template('clientes/importar.html', erros_detalhados=[f'Erro ao processar: {str(e)}'], sucesso=0, erros=1)
        return render_template('clientes/importar.html')

    return _importar()


@clientes_bp.route('/cliente/<int:id>/receber_lote', methods=['POST'])
def receber_lote_cliente(id):
    """Abatimento Inteligente: recebe valor em lote e abate nas vendas pendentes mais antigas.

    Fluxo:
        1. Busca vendas PENDENTE/PARCIAL do cliente, da mais antiga para a
           mais nova (FIFO). Pula vendas marcadas como PERDA.
        2. Para cada venda enquanto sobrar dinheiro, calcula o quanto pode
           ser abatido (até o saldo devedor) e cria um ``LancamentoCaixa``
           ENTRADA com descrição
           ``Venda #N - <cliente> (Lote: R$ <valor_total_pago>)``.
           Esse padrão de descrição é o que ``_resincronizar_pagamento_venda``
           usa (regex ``Venda #(\\d+)``) para somar o ``valor_pago`` da
           venda — por isso é OBRIGATÓRIO manter o prefixo ``Venda #N -``
           exato. O sufixo ``(Lote: R$ X)`` é puramente para auditoria
           visual no Caixa Diário: quando um pagamento de R$ 20.000 é
           fatiado entre N vendas, todos os lançamentos exibem o valor
           original do lote, dando rastreabilidade ao operador.
        3. Se a forma é BOLETO, cria também o ``Repasse Lote: R$ X``
           (SAIDA por venda) — EXCETO quando a triangulação está
           ativa (ver passo 5), pra não duplicar saídas.
        4. Faz ``flush()`` para que os lançamentos estejam visíveis na query
           do resync, e chama ``_resincronizar_pagamento_venda(venda)`` para
           CADA venda afetada — assim o ``valor_pago`` e a ``situacao``
           viram PARCIAL/PAGO automaticamente, ativando o badge laranja
           "Saldo devedor" na tela de Vendas.
        5. Triangulação opcional (``repassar_fornecedor=1``): cliente
           pagou direto na conta do fornecedor do usuário. Cria UMA
           SAIDA única do valor total efetivamente aplicado, com
           descrição ``Repasse Direto (Origem: Lote <cliente>) -
           Fornecedor: <nome>``. Como as ENTRADAs por venda continuam
           (necessárias pro resync), entrada e saída se cancelam no
           saldo — o cliente fica quitado e o caixa do usuário fica
           neutro, refletindo a realidade.

    Não atualiza ``venda.valor_pago`` nem ``venda.situacao`` manualmente:
    delega tudo ao resync, que é a fonte única da verdade.
    """
    valor_raw = (request.form.get('valor_recebido') or '').strip()
    valor_str = valor_raw.replace('.', '').replace(',', '.')
    try:
        valor_recebido = Decimal(valor_str) if valor_str else Decimal('0.00')
    except (InvalidOperation, ValueError):
        flash('Valor recebido inválido. Use o formato 1.000,00.', 'error')
        return redirect(url_for('clientes.listar_clientes'))
    if valor_recebido <= Decimal('0.00'):
        flash('Informe um valor recebido maior que zero.', 'error')
        return redirect(url_for('clientes.listar_clientes'))
    forma_pgto = request.form.get('forma_pagamento', 'Dinheiro')

    # Triangulação (Repasse Direto para Fornecedor): cliente pagou via PIX
    # diretamente na conta do fornecedor do usuário. As vendas precisam
    # ser quitadas (ENTRADA por venda, padrão de sempre), MAS o saldo em
    # conta do usuário não pode inflar — então geramos UMA SAIDA única
    # do valor total. Quando essa flag está ativa, suprimimos o repasse
    # automático por-venda do fluxo Boleto, porque seriam duas saídas
    # para o mesmo dinheiro.
    repassar_fornecedor = (request.form.get('repassar_fornecedor') or '').strip() in ('1', 'on', 'true')
    fornecedor_repasse = (request.form.get('fornecedor_repasse') or '').strip()
    if repassar_fornecedor and not fornecedor_repasse:
        flash('Informe o nome do fornecedor para o repasse direto.', 'error')
        return redirect(url_for('clientes.listar_clientes'))

    # Data do pagamento (retroativa): permite lançamentos de valores que entraram no caixa em datas anteriores.
    data_pagamento_raw = (request.form.get('data_pagamento') or '').strip()
    if data_pagamento_raw:
        try:
            data_pagamento = date.fromisoformat(data_pagamento_raw)
        except ValueError:
            flash('Data do pagamento inválida. Use o formato AAAA-MM-DD.', 'error')
            return redirect(url_for('clientes.listar_clientes'))
    else:
        data_pagamento = date.today()

    cliente = query_tenant(Cliente).filter_by(id=id).first_or_404()

    vendas_abertas = query_tenant(Venda).filter(
        Venda.cliente_id == id,
        Venda.situacao.in_(['PENDENTE', 'PARCIAL']),
    ).order_by(Venda.data_venda.asc(), Venda.id.asc()).all()

    valor_restante = Decimal(str(valor_recebido or Decimal('0.00')))
    vendas_afetadas = []

    try:
        # Pré-formata o valor TOTAL pago pelo cliente em formato BR ("20.000,00").
        # Esse marcador "(Lote: R$ X,XX)" entra na descrição de TODOS os
        # LancamentoCaixa gerados nesta requisição — independentemente do
        # fatiamento do dinheiro entre N vendas. Permite ao operador, ao
        # olhar o Caixa Diário, lembrar que esses lançamentos vieram de UM
        # único pagamento real do cliente. Não interfere no regex
        # ``_RE_MARCADOR_VENDA = r'Venda #(\d+)'`` (que casa apenas o prefixo).
        # Dentro do try para qualquer falha cair no except tratado (evita 500 puro).
        _valor_lote_int, _valor_lote_dec = divmod(int((valor_recebido * 100)), 100)
        _valor_lote_fmt = f"{_valor_lote_int:,}".replace(',', '.') + f",{_valor_lote_dec:02d}"
        _marcador_lote = f"(Lote: R$ {_valor_lote_fmt})"

        for venda in vendas_abertas:
            if valor_restante <= Decimal('0.00'):
                break

            if str(getattr(venda, 'tipo_operacao', 'VENDA') or 'VENDA').upper() == 'PERDA':
                continue

            valor_pago_atual = Decimal(str(venda.valor_pago or Decimal('0.00')))
            valor_total_venda = Decimal(str(venda.calcular_total() or Decimal('0.00')))
            valor_falta = valor_total_venda - valor_pago_atual
            if valor_falta <= Decimal('0.00'):
                continue

            valor_abatido = min(valor_restante, valor_falta)
            valor_restante -= valor_abatido

            novo_lanc = LancamentoCaixa(
                data=data_pagamento,
                descricao=f"Venda #{venda.id} - {cliente.nome_cliente} {_marcador_lote}"[:200],
                tipo='ENTRADA',
                categoria='Entrada Cliente',
                forma_pagamento=forma_pgto,
                valor=valor_abatido,
                usuario_id=current_user.id,
                empresa_id=empresa_id_atual(),
                venda_id=venda.id,
            )
            db.session.add(novo_lanc)

            # Repasse automático do Boleto (saída por-venda).
            # IMPORTANTE: se "Repassar valor direto para Fornecedor"
            # estiver marcado, NÃO criamos esse repasse por-venda — a
            # SAIDA única do valor total (criada após este loop) já
            # cobre o caso, e duplicar inflaria o saldo negativamente.
            if 'boleto' in forma_pgto.lower() and not repassar_fornecedor:
                repasse_lanc = LancamentoCaixa(
                    data=data_pagamento,
                    descricao=f"Venda #{venda.id} - {cliente.nome_cliente} (Repasse {_marcador_lote[1:-1]})"[:200],
                    tipo='SAIDA',
                    categoria='Saída Fornecedor',
                    forma_pagamento=forma_pgto,
                    valor=valor_abatido,
                    usuario_id=current_user.id,
                    empresa_id=empresa_id_atual(),
                    venda_id=venda.id,
                )
                db.session.add(repasse_lanc)

            vendas_afetadas.append(venda)

        # Triangulação: o cliente pagou direto na conta do fornecedor,
        # então o dinheiro nunca entrou de fato no caixa do usuário.
        # Para que as vendas sejam quitadas (entradas por venda) MAS o
        # saldo em conta não infle, registramos UMA saída única do
        # valor total efetivamente aplicado. Usamos `valor_aplicado`
        # (não `valor_recebido`) para evitar saída maior que entradas
        # caso parte do dinheiro tenha "sobrado" sem venda para
        # quitar — nesse caso o usuário pode complementar o lançamento
        # manualmente no Caixa.
        if repassar_fornecedor and vendas_afetadas:
            valor_aplicado_repasse = valor_recebido - valor_restante
            if valor_aplicado_repasse > Decimal('0.00'):
                saida_repasse = LancamentoCaixa(
                    data=data_pagamento,
                    descricao=(
                        f"Repasse Direto (Origem: Lote {cliente.nome_cliente}) "
                        f"- Fornecedor: {fornecedor_repasse}"
                    )[:200],
                    tipo='SAIDA',
                    categoria='Saída Fornecedor',
                    forma_pagamento=forma_pgto,
                    valor=valor_aplicado_repasse,
                    usuario_id=current_user.id,
                    empresa_id=empresa_id_atual(),
                )
                db.session.add(saida_repasse)

        # Flush é OBRIGATÓRIO antes do resync: o resync soma os LancamentoCaixa
        # do banco via query, então os INSERTs precisam estar visíveis.
        db.session.flush()

        # Resync de TODAS as vendas afetadas: fonte única da verdade para
        # valor_pago e situacao. Garante que o badge laranja "Saldo devedor"
        # apareça na listagem de Vendas para PARCIAIS e que PAGOs sumam do
        # filtro "Pendentes".
        for venda in vendas_afetadas:
            _resincronizar_pagamento_venda(venda)

        db.session.commit()
        limpar_cache_dashboard()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception('Falha em receber_lote_cliente')
        flash(f'Erro ao processar abatimento: {exc}', 'error')
        return redirect(url_for('clientes.listar_clientes'))

    if not vendas_afetadas:
        flash(
            f'Nenhuma venda em aberto encontrada para {cliente.nome_cliente}. '
            'Nenhum lançamento foi criado.',
            'warning',
        )
        return redirect(url_for('clientes.listar_clientes'))

    valor_aplicado = valor_recebido - valor_restante
    qtd_pagas = sum(1 for v in vendas_afetadas if (v.situacao or '').upper() == 'PAGO')
    qtd_parciais = sum(1 for v in vendas_afetadas if (v.situacao or '').upper() == 'PARCIAL')

    detalhe_status = []
    if qtd_pagas:
        detalhe_status.append(f'{qtd_pagas} liquidada(s)')
    if qtd_parciais:
        detalhe_status.append(f'{qtd_parciais} parcial(is)')
    sufixo_status = f' ({", ".join(detalhe_status)})' if detalhe_status else ''

    sufixo_repasse = (
        f' Saída de R$ {valor_aplicado:,.2f} registrada para "{fornecedor_repasse}" (triangulação).'
        if repassar_fornecedor and valor_aplicado > Decimal('0.00')
        else ''
    )

    if valor_restante > Decimal('0.00'):
        flash(
            f'Abatimento de R$ {valor_aplicado:,.2f} aplicado em '
            f'{len(vendas_afetadas)} venda(s){sufixo_status}. '
            f'Sobrou R$ {valor_restante:,.2f} (não havia mais saldo devedor).'
            f'{sufixo_repasse}',
            'success',
        )
    else:
        flash(
            f'Abatimento de R$ {valor_aplicado:,.2f} aplicado em '
            f'{len(vendas_afetadas)} venda(s){sufixo_status} para '
            f'{cliente.nome_cliente}.{sufixo_repasse}',
            'success',
        )

    log_repasse = (
        f" Triangulação: SAIDA R$ {valor_aplicado:.2f} para '{fornecedor_repasse}'."
        if repassar_fornecedor and valor_aplicado > Decimal('0.00')
        else ''
    )
    registrar_log(
        'PAGAR', 'CLIENTES',
        f"Abatimento em lote: R$ {valor_aplicado:.2f} ({forma_pgto}) "
        f"distribuído em {len(vendas_afetadas)} venda(s) do cliente "
        f"{cliente.nome_cliente} (#{cliente.id}). "
        f"Liquidadas: {qtd_pagas}, Parciais: {qtd_parciais}, "
        f"Sobra: R$ {valor_restante:.2f}.{log_repasse}",
    )

    return redirect(url_for('clientes.listar_clientes'))
