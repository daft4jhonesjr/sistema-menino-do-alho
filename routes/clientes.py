"""Blueprint ``clientes`` — CRUD de clientes do tenant.

Rotas extraídas do legado ``app.py`` (Fase 2 da refatoração):
    * GET  /clientes                              listar_clientes
    * GET/POST /clientes/novo                     novo_cliente
    * GET/POST /clientes/editar/<id>              editar_cliente
    * POST /clientes/excluir/<id>                 excluir_cliente
    * POST /cliente/<id>/toggle_ativo             toggle_ativo_cliente
    * GET  /clientes/<id>/extrato                 extrato_cliente
    * POST /bulk_delete_clientes                  bulk_delete_clientes
    * GET/POST /clientes/importar                 importar_clientes  (admin)
    * POST /cliente/<id>/receber_lote             receber_lote_cliente

Endpoints novos: prefixo ``clientes.`` (ex.: ``clientes.listar_clientes``).

Proteção automática de tenant
-----------------------------
Toda rota deste blueprint exige ``login_required`` + ``tenant_required``.
Em vez de repetir os decorators em cada handler, aplicamos via
``before_request`` — qualquer rota nova adicionada aqui herda a proteção.

Rotas com necessidade adicional de ``@admin_required`` (ex.: importar)
mantêm o decorator no próprio handler.
"""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import os
import re

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, current_app,
)
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError
import pandas as pd
from werkzeug.utils import secure_filename

from models import db, Cliente, Venda, LancamentoCaixa
from services.auth_utils import tenant_required, admin_required, _is_ajax
from services.db_utils import query_tenant, empresa_id_atual
from services.cache_utils import limpar_cache_dashboard
from services.config_helpers import registrar_log
from services.csv_utils import (
    _msg_linha, _strip_quotes,
    _parse_clientes_raw_tsv, _sanitizar_cnpj_importacao,
)


clientes_bp = Blueprint('clientes', __name__)


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
    ordem_param = (request.args.get('ordem') or '').strip().lower()
    if ordem_param in ('desc', 'id_decrescente'):
        ordem = 'id_decrescente'
        clientes = query_tenant(Cliente).order_by(Cliente.id.desc()).limit(500).all()
    else:
        ordem = 'id_crescente'
        clientes = query_tenant(Cliente).order_by(Cliente.id.asc()).limit(500).all()
    return render_template('clientes/listar.html', clientes=clientes, ordem=ordem)


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

            nome_cliente = (request.form.get('nome_cliente') or '').strip()
            if not nome_cliente:
                msg = 'Nome do cliente é obrigatório.'
                if _is_ajax():
                    return jsonify(ok=False, mensagem=msg), 400
                flash(msg, 'error')
                return render_template('clientes/formulario.html', cliente=None)
            cliente = Cliente(
                nome_cliente=nome_cliente,
                telefone=(request.form.get('telefone', '') or '').strip() or None,
                razao_social=request.form.get('razao_social', ''),
                cnpj=cnpj,
                cidade=request.form.get('cidade', ''),
                endereco=request.form.get('endereco', '') or None,
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
    # first_or_404 precisa propagar para o handler do Flask: fica FORA do try
    # genérico, senão 404 vira 500 e isolamento cross-tenant fica ruidoso.
    cliente = query_tenant(Cliente).filter_by(id=id).first_or_404()
    try:
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

            cliente.nome_cliente = request.form.get('nome_cliente') or cliente.nome_cliente
            cliente.telefone = (request.form.get('telefone', '') or '').strip() or None
            cliente.razao_social = request.form.get('razao_social', '')
            cliente.cnpj = cnpj
            cliente.cidade = request.form.get('cidade', '')
            cliente.endereco = request.form.get('endereco', '') or None
            db.session.commit()
            registrar_log('EDITAR', 'CLIENTES', f"Cliente #{cliente.id} — {cliente.nome_cliente} editado.")
            flash('Cliente atualizado com sucesso!', 'success')
            return redirect(url_for('clientes.listar_clientes'))

        return render_template('clientes/formulario.html', cliente=cliente)

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"ERRO CRÍTICO NA EDIÇÃO DE CLIENTE {id}: {str(e)}")
        flash(f'Erro interno ao processar cliente: {str(e)}', 'error')
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
    """Extrato de cobrança em PDF: vendas pendentes e parciais do cliente."""
    cliente = query_tenant(Cliente).filter_by(id=cliente_id).first_or_404()

    # Filtro: PENDENTE e PARCIAL (saldo devedor); ignora itens de perda/brinde (R$ 0,00)
    vendas_pendentes = query_tenant(Venda).filter(
        Venda.cliente_id == cliente.id,
        Venda.situacao.in_(['PENDENTE', 'PARCIAL']),
        (Venda.preco_venda * Venda.quantidade_venda) > 0
    ).options(joinedload(Venda.produto)).order_by(Venda.data_venda).all()

    # Total devido = soma do saldo restante (valor da nota - já pago) de cada venda
    total_devido = sum(
        Decimal(str(v.calcular_total() or Decimal('0.00'))) - Decimal(str(v.valor_pago or Decimal('0.00')))
        for v in vendas_pendentes
    )
    data_hoje = datetime.now().strftime('%d/%m/%Y')

    return render_template('extrato.html', cliente=cliente, vendas=vendas_pendentes, total=total_devido, data_hoje=data_hoje)


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
        return jsonify({'ok': False, 'mensagem': str(e)}), 500


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
    """Abatimento Inteligente: recebe valor em lote e abate nas vendas pendentes mais antigas."""
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

    # Busca vendas PENDENTES ou PARCIAIS, da mais velha para a mais nova
    vendas_abertas = query_tenant(Venda).filter(
        Venda.cliente_id == id,
        Venda.situacao.in_(['PENDENTE', 'PARCIAL'])
    ).order_by(Venda.data_venda.asc()).all()

    valor_restante = Decimal(str(valor_recebido or Decimal('0.00')))

    for venda in vendas_abertas:
        if valor_restante <= 0:
            break

        venda.valor_pago = Decimal(str(venda.valor_pago or Decimal('0.00')))
        valor_total_venda = Decimal(str(venda.calcular_total() or Decimal('0.00')))
        valor_falta = valor_total_venda - Decimal(str(venda.valor_pago or Decimal('0.00')))

        if valor_restante >= valor_falta:
            valor_abatido = valor_falta
            venda.valor_pago = valor_total_venda
            venda.situacao = 'PAGO'
            valor_restante -= valor_falta
        else:
            valor_abatido = valor_restante
            venda.valor_pago = Decimal(str(venda.valor_pago or Decimal('0.00'))) + Decimal(str(valor_restante))
            venda.situacao = 'PARCIAL'
            valor_restante = 0

        # Respeita a data de pagamento informada (permite lançamentos retroativos).
        data_lancamento_caixa = data_pagamento
        novo_lanc = LancamentoCaixa(
            data=data_lancamento_caixa,
            descricao=f"Venda #{venda.id} - {cliente.nome_cliente} (Abatimento)",
            tipo='ENTRADA',
            categoria='Entrada Cliente',
            forma_pagamento=forma_pgto,
            valor=valor_abatido,
            usuario_id=current_user.id,
            empresa_id=empresa_id_atual(),
        )
        db.session.add(novo_lanc)

        if 'boleto' in forma_pgto.lower():
            repasse_lanc = LancamentoCaixa(
                data=data_lancamento_caixa,
                descricao=f"Venda #{venda.id} - {cliente.nome_cliente} (Repasse Abatimento)",
                tipo='SAIDA',
                categoria='Saída Fornecedor',
                forma_pagamento=forma_pgto,
                valor=valor_abatido,
                usuario_id=current_user.id,
                empresa_id=empresa_id_atual(),
            )
            db.session.add(repasse_lanc)

    db.session.commit()
    limpar_cache_dashboard()
    flash(f'Abatimento de R$ {valor_recebido:,.2f} processado com sucesso para {cliente.nome_cliente}!', 'success')
    return redirect(url_for('clientes.listar_clientes'))
