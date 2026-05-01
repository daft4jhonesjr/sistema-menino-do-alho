"""Blueprint ``master`` — painel Super Admin do SaaS.

Acesso restrito a usuários com ``perfil=MASTER``. Permite criar novas
Empresas (tenants), instanciar o primeiro Usuário DONO vinculado e
ativar/desativar tenants existentes (suspensão por inadimplência etc).

Cadastro público de empresas NÃO existe — novos tenants nascem só aqui.

Endpoints:
    * master.master_admin                    GET/POST /master-admin
    * master.master_toggle_empresa_ativo     POST     /master-admin/empresa/<id>/toggle_ativo
    * master.diagnosticar_saldos             GET      /admin/diagnosticar_saldos
    * master.recuperar_saldos                GET      /admin/recuperar_saldos
    * master.limpar_valor_pago_fantasma      GET      /admin/limpar_valor_pago_fantasma
    * master.inspect_venda                   GET      /admin/inspect_venda/<venda_id>
"""
import logging
from decimal import Decimal

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
)
from flask_login import login_required
from werkzeug.security import generate_password_hash
from sqlalchemy import func

from models import db, Empresa, Usuario, Venda, LancamentoCaixa, PERFIL_DONO
from services.auth_utils import master_required
from services.db_utils import _safe_db_commit, query_tenant, empresa_id_atual
from services.vendas_services import (
    _resincronizar_pagamento_venda,
    _resincronizar_pagamento_venda_seguro,
)


master_bp = Blueprint('master', __name__)


def _master_validar_form_nova_empresa(form):
    """Valida campos do form de criação de Empresa+Dono.

    Retorna ``(dados_validados, erro)``. Se ``erro``, ``dados_validados`` é None.
    """
    nome_fantasia = (form.get('nome_fantasia') or '').strip()
    cnpj = (form.get('cnpj') or '').strip() or None
    dono_nome = (form.get('dono_nome') or '').strip() or None
    dono_username = (form.get('dono_username') or '').strip()
    dono_email = (form.get('dono_email') or '').strip() or None
    dono_senha = form.get('dono_senha') or ''
    dono_senha_confirmar = form.get('dono_senha_confirmar') or ''

    if not nome_fantasia:
        return None, 'Informe o nome fantasia da empresa.'
    if not dono_username:
        return None, 'Informe o login (username) do Dono da empresa.'
    if not dono_senha:
        return None, 'Informe a senha inicial do Dono.'
    if dono_senha != dono_senha_confirmar:
        return None, 'As senhas nao conferem.'
    if len(dono_senha) < 6:
        return None, 'A senha deve ter pelo menos 6 caracteres.'
    if Usuario.query.filter_by(username=dono_username).first():
        return None, f'Ja existe um usuario com o login "{dono_username}".'
    if cnpj and Empresa.query.filter_by(cnpj=cnpj).first():
        return None, f'Ja existe uma empresa com o CNPJ "{cnpj}".'

    return {
        'nome_fantasia': nome_fantasia,
        'cnpj': cnpj,
        'dono_nome': dono_nome,
        'dono_username': dono_username,
        'dono_email': dono_email,
        'dono_senha': dono_senha,
    }, None


@master_bp.route('/master-admin', methods=['GET', 'POST'])
@login_required
def master_admin():
    """Painel Super Admin: cria novas Empresas + Dono inicial."""
    @master_required
    def _master_admin():
        if request.method == 'POST':
            dados, erro = _master_validar_form_nova_empresa(request.form)
            if erro:
                flash(erro, 'error')
                return redirect(url_for('master.master_admin'))

            try:
                nova_empresa = Empresa(
                    nome_fantasia=dados['nome_fantasia'],
                    cnpj=dados['cnpj'],
                    ativo=True,
                )
                db.session.add(nova_empresa)
                db.session.flush()  # garante nova_empresa.id

                dono = Usuario(
                    username=dados['dono_username'],
                    password_hash=generate_password_hash(dados['dono_senha']),
                    role='admin',
                    perfil=PERFIL_DONO,
                    empresa_id=nova_empresa.id,
                    nome=dados['dono_nome'],
                    email=dados['dono_email'],
                )
                db.session.add(dono)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                logging.error('Erro ao criar empresa+dono: %s', e, exc_info=True)
                flash('Erro ao criar empresa. Tente novamente.', 'error')
                return redirect(url_for('master.master_admin'))

            flash(
                f'Empresa "{nova_empresa.nome_fantasia}" criada com sucesso. '
                f'Usuario Dono: {dono.username}.',
                'success',
            )
            return redirect(url_for('master.master_admin'))

        empresas = (
            db.session.query(
                Empresa,
                db.func.count(Usuario.id).label('total_usuarios'),
            )
            .outerjoin(Usuario, Usuario.empresa_id == Empresa.id)
            .group_by(Empresa.id)
            .order_by(Empresa.data_cadastro.desc())
            .all()
        )
        return render_template('admin_master.html', empresas=empresas)

    return _master_admin()


@master_bp.route('/master-admin/empresa/<int:empresa_id>/toggle_ativo', methods=['POST'])
@login_required
def master_toggle_empresa_ativo(empresa_id):
    """Ativa/desativa um tenant (suspensão por inadimplência etc)."""
    @master_required
    def _toggle():
        empresa = Empresa.query.get_or_404(empresa_id)
        empresa.ativo = not bool(empresa.ativo)
        ok, err = _safe_db_commit()
        if not ok:
            flash(err or 'Erro ao atualizar empresa.', 'error')
        else:
            estado = 'ativada' if empresa.ativo else 'desativada'
            flash(f'Empresa "{empresa.nome_fantasia}" {estado}.', 'success')
        return redirect(url_for('master.master_admin'))

    return _toggle()


def _classificar_resync_dry_run(venda):
    """Simula o que ``_resincronizar_pagamento_venda_seguro`` faria SEM
    mutar nada.

    Replica a lógica de mão única em modo estritamente read-only: lê os
    lançamentos, aplica os mesmos guarda-costas (PAGO nunca rebaixa,
    valor_pago só cresce) e devolve a classificação simulada. Não toca
    em ``venda.valor_pago`` nem em ``venda.situacao``.

    Returns:
        tuple: ``(situacao_atual, situacao_calculada, total_pago,
        valor_total, ajusta_valor_pago)`` onde ``ajusta_valor_pago``
        é True quando o resync seguro só corrigiria ``valor_pago``
        (mantendo ``situacao``). Para vendas de perda devolve
        ``(situacao_atual, 'PERDA', None, None, False)``.
    """
    situacao_atual = (venda.situacao or '').strip().upper()

    if str(getattr(venda, 'tipo_operacao', '') or '').strip().upper() == 'PERDA':
        return situacao_atual, 'PERDA', None, None, False

    valor_total = Decimal(str(venda.calcular_total() or Decimal('0.00')))
    valor_pago_atual = Decimal(str(venda.valor_pago or Decimal('0.00')))

    # GUARDA-COSTAS 1 (espelha _resincronizar_pagamento_venda_seguro):
    # PAGO nunca regride. Diferenciamos só dois sub-casos para o
    # diagnóstico: já consistente vs. precisa ajustar valor_pago.
    if situacao_atual == 'PAGO':
        if valor_pago_atual >= (valor_total - Decimal('0.01')):
            return situacao_atual, 'PAGO', valor_pago_atual, valor_total, False
        return situacao_atual, 'PAGO', valor_pago_atual, valor_total, True

    eid = getattr(venda, 'empresa_id', None)
    q = LancamentoCaixa.query.filter(
        LancamentoCaixa.tipo == 'ENTRADA',
        LancamentoCaixa.descricao.like(f"Venda #{venda.id} -%"),
    )
    if eid is not None:
        q = q.filter(LancamentoCaixa.empresa_id == eid)
    total_pago_raw = q.with_entities(
        func.coalesce(func.sum(LancamentoCaixa.valor), 0)
    ).scalar() or 0
    total_pago = Decimal(str(total_pago_raw))
    if total_pago < Decimal('0.00'):
        total_pago = Decimal('0.00')

    # GUARDA-COSTAS 2: valor_pago só cresce.
    novo_valor_pago = max(total_pago, valor_pago_atual)

    if situacao_atual == 'PARCIAL':
        # PARCIAL nunca cai pra PENDENTE; pode subir pra PAGO.
        if novo_valor_pago >= (valor_total - Decimal('0.01')):
            situacao_calc = 'PAGO'
        else:
            situacao_calc = 'PARCIAL'
        return situacao_atual, situacao_calc, novo_valor_pago, valor_total, False

    # PENDENTE (ou status vazio): pode subir livremente.
    if novo_valor_pago <= Decimal('0.01'):
        situacao_calc = 'PENDENTE'
    elif novo_valor_pago < (valor_total - Decimal('0.01')):
        situacao_calc = 'PARCIAL'
    else:
        situacao_calc = 'PAGO'

    return situacao_atual, situacao_calc, novo_valor_pago, valor_total, False


@master_bp.route('/admin/diagnosticar_saldos', methods=['GET'])
@login_required
def diagnosticar_saldos():
    """DRY-RUN MÃO ÚNICA: classifica vendas do tenant em buckets sem
    alterar o banco.

    Simula o que ``_resincronizar_pagamento_venda_seguro`` faria. Como
    a versão segura **nunca rebaixa** uma venda, os buckets de PERIGO
    do diagnóstico antigo (PAGO→PENDENTE etc.) são impossíveis aqui:
    eles aparecem como SEM_MUDANCA ou como PAGO_AJUSTE_VALOR_PAGO
    (quando ``situacao='PAGO'`` e ``valor_pago=0``, o caso clássico de
    venda marcada via badge/CSV legado — só corrige ``valor_pago``).

    NÃO persiste nada no banco — termina com ``rollback()``.

    Output em texto puro, com:
        - total avaliadas
        - contagem por bucket
        - amostra dos primeiros 20 IDs em cada bucket relevante

    Use antes de ``/admin/recuperar_saldos`` para entender o impacto.
    """
    eid = empresa_id_atual()
    vendas = query_tenant(Venda).all()

    buckets = {
        'PENDENTE_PARA_PAGO': [],         # recuperação completa
        'PENDENTE_PARA_PARCIAL': [],      # recuperação parcial
        'PARCIAL_PARA_PAGO': [],          # promoção a PAGO
        'PARCIAL_AJUSTE_VALOR_PAGO': [],  # PARCIAL: só sobe valor_pago
        'PAGO_AJUSTE_VALOR_PAGO': [],     # PAGO mas valor_pago=0 (badge/CSV)
        'SEM_MUDANCA': [],
        'PERDA': [],
        'OUTROS': [],
    }

    try:
        for venda in vendas:
            (sit_atual, sit_calc, total_pago, valor_total,
             ajusta_valor_pago) = _classificar_resync_dry_run(venda)

            if sit_calc == 'PERDA':
                buckets['PERDA'].append(venda.id)
                continue

            if sit_atual == 'PAGO':
                if ajusta_valor_pago:
                    buckets['PAGO_AJUSTE_VALOR_PAGO'].append(venda.id)
                else:
                    buckets['SEM_MUDANCA'].append(venda.id)
                continue

            if sit_atual == 'PARCIAL':
                if sit_calc == 'PAGO':
                    buckets['PARCIAL_PARA_PAGO'].append(venda.id)
                elif sit_calc == 'PARCIAL':
                    valor_pago_atual = Decimal(str(venda.valor_pago or Decimal('0.00')))
                    if total_pago is not None and total_pago > valor_pago_atual:
                        buckets['PARCIAL_AJUSTE_VALOR_PAGO'].append(venda.id)
                    else:
                        buckets['SEM_MUDANCA'].append(venda.id)
                else:
                    buckets['SEM_MUDANCA'].append(venda.id)
                continue

            # PENDENTE (ou status vazio)
            if sit_calc == 'PAGO':
                buckets['PENDENTE_PARA_PAGO'].append(venda.id)
            elif sit_calc == 'PARCIAL':
                buckets['PENDENTE_PARA_PARCIAL'].append(venda.id)
            elif sit_calc == 'PENDENTE':
                buckets['SEM_MUDANCA'].append(venda.id)
            else:
                buckets['OUTROS'].append(
                    f"{venda.id}({sit_atual}->{sit_calc})"
                )
    finally:
        # Defensivo: garantir que o session não fique sujo
        try:
            db.session.rollback()
        except Exception:
            pass

    def _amostra(lista, n=20):
        if not lista:
            return '-'
        head = lista[:n]
        sufixo = f" ... (+{len(lista) - n})" if len(lista) > n else ''
        return ', '.join(str(x) for x in head) + sufixo

    total_promocoes = (
        len(buckets['PENDENTE_PARA_PAGO'])
        + len(buckets['PENDENTE_PARA_PARCIAL'])
        + len(buckets['PARCIAL_PARA_PAGO'])
    )
    total_ajustes = (
        len(buckets['PAGO_AJUSTE_VALOR_PAGO'])
        + len(buckets['PARCIAL_AJUSTE_VALOR_PAGO'])
    )

    linhas = [
        '=== DIAGNÓSTICO DE SALDOS (DRY-RUN — MÃO ÚNICA — sem alterações no banco) ===',
        f'Empresa (tenant): {eid}',
        f'Total de vendas avaliadas: {len(vendas)}',
        '',
        '--- PROMOÇÕES de situação (resync vai SUBIR status) ---',
        f'  PENDENTE -> PAGO         : {len(buckets["PENDENTE_PARA_PAGO"]):>5}  IDs: {_amostra(buckets["PENDENTE_PARA_PAGO"])}',
        f'  PENDENTE -> PARCIAL      : {len(buckets["PENDENTE_PARA_PARCIAL"]):>5}  IDs: {_amostra(buckets["PENDENTE_PARA_PARCIAL"])}',
        f'  PARCIAL  -> PAGO         : {len(buckets["PARCIAL_PARA_PAGO"]):>5}  IDs: {_amostra(buckets["PARCIAL_PARA_PAGO"])}',
        '',
        '--- AJUSTES de valor_pago (status preservado) ---',
        f'  PAGO    + valor_pago=0   : {len(buckets["PAGO_AJUSTE_VALOR_PAGO"]):>5}  IDs: {_amostra(buckets["PAGO_AJUSTE_VALOR_PAGO"])}',
        f'  PARCIAL  sobe valor_pago : {len(buckets["PARCIAL_AJUSTE_VALOR_PAGO"]):>5}  IDs: {_amostra(buckets["PARCIAL_AJUSTE_VALOR_PAGO"])}',
        '',
        '--- INALTERADAS ---',
        f'  Sem mudança             : {len(buckets["SEM_MUDANCA"]):>5}',
        f'  PERDA (ignoradas)       : {len(buckets["PERDA"]):>5}',
        f'  Outros casos            : {len(buckets["OUTROS"]):>5}  Detalhe: {_amostra(buckets["OUTROS"])}',
        '',
        '--- DECISÃO ---',
        f'  Total de promoções     : {total_promocoes}',
        f'  Total de ajustes       : {total_ajustes}',
        '  Modo MÃO ÚNICA: nenhuma venda PAGO/PARCIAL será rebaixada.',
        '  OK aplicar /admin/recuperar_saldos.',
    ]
    output = '\n'.join(linhas)
    logging.info(
        '[DIAG-SALDOS-SEGURO] empresa=%s total=%d promocoes=%d ajustes=%d',
        eid, len(vendas), total_promocoes, total_ajustes,
    )
    return output, 200, {'Content-Type': 'text/plain; charset=utf-8'}


@master_bp.route('/admin/recuperar_saldos', methods=['GET'])
@login_required
def recuperar_saldos():
    """APLICA (MÃO ÚNICA): só promove ``situacao`` (PENDENTE → PARCIAL →
    PAGO). Nunca rebaixa.

    Usa ``_resincronizar_pagamento_venda_seguro``, que respeita o
    histórico legado: vendas já marcadas como PAGO **não** regridem
    para PENDENTE mesmo quando não existem ``LancamentoCaixa`` com
    descrição ``Venda #N -%`` (caso comum em vendas marcadas pelo
    badge ``atualizar_situacao_rapida`` ou importadas via CSV).

    Persiste tudo num único commit no final.

    USO RECOMENDADO: rode antes ``GET /admin/diagnosticar_saldos`` para
    estimar o impacto. Esta rota não pergunta antes de aplicar.

    Multi-tenant: ``query_tenant(Venda)`` garante escopo do usuário logado.
    """
    eid = empresa_id_atual()
    vendas = query_tenant(Venda).all()

    alteradas = 0
    for venda in vendas:
        situacao_antes = venda.situacao
        valor_pago_antes = venda.valor_pago
        if _resincronizar_pagamento_venda_seguro(venda):
            if (venda.situacao != situacao_antes
                    or venda.valor_pago != valor_pago_antes):
                alteradas += 1

    ok, err = _safe_db_commit()
    if not ok:
        logging.error(
            '[RECUPERAR-SALDOS-SEGURO] commit falhou empresa=%s err=%s',
            eid, err,
        )
        return (
            f'ERRO ao gravar correções: {err or "erro desconhecido"}. '
            f'{len(vendas)} vendas processadas mas não persistidas.',
            500,
            {'Content-Type': 'text/plain; charset=utf-8'},
        )

    logging.info(
        '[RECUPERAR-SALDOS-SEGURO] empresa=%s total=%d alteradas=%d',
        eid, len(vendas), alteradas,
    )
    return (
        f'Sucesso (modo MÃO ÚNICA)! {len(vendas)} vendas recalculadas. '
        f'{alteradas} tiveram a situação ou valor_pago alterado. '
        f'Nenhuma venda PAGO foi rebaixada.',
        200,
        {'Content-Type': 'text/plain; charset=utf-8'},
    )


@master_bp.route('/admin/limpar_valor_pago_fantasma', methods=['GET'])
@login_required
def limpar_valor_pago_fantasma():
    """FAXINA: zera o ``valor_pago`` de Vendas PENDENTE com ``valor_pago > 0``.

    Cenário-alvo: venda foi marcada PAGO em algum momento (gerou ou não
    ``LancamentoCaixa``), depois algum fluxo destrutivo rebaixou para
    PENDENTE sem ressincronizar ``valor_pago`` — ex.: ``editar_venda``
    deletando lançamentos antes da blindagem desta safra. O resultado é
    uma venda com ``situacao='PENDENTE'`` e ``valor_pago > 0``, que entope
    a UI da listagem (Total a Receber subestimado, badge laranja
    "Pago: X" sem contraparte no caixa).

    Estratégia: aplica ``_resincronizar_pagamento_venda`` (versão ORIGINAL,
    bidirecional). Para essas vendas a soma dos lançamentos no caixa é
    zero (ou bem menor que ``valor_pago``), então o resync vai zerar
    ``valor_pago`` e manter ``situacao='PENDENTE'`` (não rebaixa nem
    promove — o status já estava PENDENTE).

    Segurança: SÓ toca em vendas PENDENTE com ``valor_pago > 0``.
    Vendas PAGO/PARCIAL não são afetadas (a versão ``_seguro`` em
    ``recuperar_saldos`` continua sendo a rota apropriada para promover
    histórico legado).

    Multi-tenant: ``query_tenant(Venda)`` escopa para o tenant logado.
    """
    eid = empresa_id_atual()
    fantasmas = query_tenant(Venda).filter(
        Venda.situacao == 'PENDENTE',
        Venda.valor_pago > Decimal('0.00'),
    ).all()

    logging.info(
        '[FAXINA-FANTASMA] start empresa=%s candidatas=%d',
        eid, len(fantasmas),
    )

    corrigidas = 0
    detalhes = []
    for venda in fantasmas:
        valor_pago_antes = Decimal(str(venda.valor_pago or Decimal('0.00')))
        try:
            mudou = _resincronizar_pagamento_venda(venda)
        except Exception as e:
            logging.error(
                '[FAXINA-FANTASMA] falha-resync empresa=%s venda_id=%d err=%s',
                eid, venda.id, e,
            )
            db.session.rollback()
            return (
                f'ERRO ao ressincronizar Venda #{venda.id}: {e}. '
                f'Faxina abortada antes do commit. {corrigidas} já estavam '
                f'corrigidas em memória, mas NADA foi persistido (rollback).',
                500,
                {'Content-Type': 'text/plain; charset=utf-8'},
            )

        valor_pago_depois = Decimal(str(venda.valor_pago or Decimal('0.00')))
        if mudou and valor_pago_depois < valor_pago_antes:
            corrigidas += 1
            detalhes.append(
                f"  Venda #{venda.id}: valor_pago R$ {valor_pago_antes:.2f} "
                f"→ R$ {valor_pago_depois:.2f} (situacao={venda.situacao})"
            )

    ok, err = _safe_db_commit()
    if not ok:
        logging.error(
            '[FAXINA-FANTASMA] commit falhou empresa=%s err=%s',
            eid, err,
        )
        return (
            f'ERRO ao gravar faxina: {err or "erro desconhecido"}. '
            f'{len(fantasmas)} vendas analisadas mas não persistidas.',
            500,
            {'Content-Type': 'text/plain; charset=utf-8'},
        )

    logging.info(
        '[FAXINA-FANTASMA] done empresa=%s candidatas=%d corrigidas=%d',
        eid, len(fantasmas), corrigidas,
    )

    linhas = [
        f"Faxina concluída (empresa={eid}).",
        f"Vendas PENDENTE com valor_pago>0 analisadas: {len(fantasmas)}",
        f"Vendas com valor_pago corrigido para zero (ou para a soma real "
        f"dos lançamentos): {corrigidas}",
        "",
    ]
    if detalhes:
        linhas.append("Detalhamento:")
        linhas.extend(detalhes)
    else:
        linhas.append("Nada a corrigir — nenhum fantasma encontrado.")

    return (
        '\n'.join(linhas),
        200,
        {'Content-Type': 'text/plain; charset=utf-8'},
    )


@master_bp.route('/admin/inspect_venda/<int:venda_id>', methods=['GET'])
@login_required
def inspect_venda(venda_id):
    """Dump cru de uma Venda + LancamentoCaixa relacionados.

    Diagnóstico para descobrir o padrão real de ``descricao`` no banco
    quando o regex ``Venda #N -%`` não casa. Faz três buscas:

        1. A venda em si (escopo do tenant atual).
        2. Lançamentos cuja ``descricao`` contém o número da venda como
           substring (case-insensitive, captura variações como
           "Venda #1891", "Venda 1891", "venda1891", "venda nro 1891" etc).
        3. Lançamentos cujo ``valor`` é igual ao total da venda
           (heurística para casos em que a descrição perdeu qualquer
           referência ao ID).

    Retorno: texto puro com TODOS os campos relevantes,
    ``descricao`` impressa com ``repr()`` para revelar caracteres ocultos
    (espaços não-quebráveis, traços diferentes do ASCII, etc).

    Multi-tenant: lê apenas dentro do ``empresa_id`` do usuário logado.
    """
    eid = empresa_id_atual()

    venda = query_tenant(Venda).filter_by(id=venda_id).first()
    if venda is None:
        return (
            f'Venda {venda_id} NAO encontrada no tenant {eid}.',
            404,
            {'Content-Type': 'text/plain; charset=utf-8'},
        )

    valor_total = Decimal(str(venda.calcular_total() or Decimal('0.00')))

    # 1) por substring do ID na descrição (varia: "1891", "#1891", " 1891 " etc)
    lancs_por_substring = (
        LancamentoCaixa.query
        .filter(LancamentoCaixa.empresa_id == eid)
        .filter(LancamentoCaixa.descricao.ilike(f'%{venda_id}%'))
        .order_by(LancamentoCaixa.data.desc(), LancamentoCaixa.id.desc())
        .all()
    )

    # 2) por valor exato (qualquer tipo, qualquer descricao)
    lancs_por_valor = (
        LancamentoCaixa.query
        .filter(LancamentoCaixa.empresa_id == eid)
        .filter(LancamentoCaixa.valor == valor_total)
        .order_by(LancamentoCaixa.data.desc(), LancamentoCaixa.id.desc())
        .limit(50)
        .all()
    )

    # 3) por venda_id direto (FK, se preenchida)
    try:
        lancs_por_fk = (
            LancamentoCaixa.query
            .filter(LancamentoCaixa.empresa_id == eid)
            .filter(LancamentoCaixa.venda_id == venda_id)
            .all()
        )
    except Exception:
        lancs_por_fk = []

    def _fmt_lanc(lc):
        return (
            f'  id={lc.id}'
            f' | data={lc.data}'
            f' | tipo={lc.tipo!r}'
            f' | valor={lc.valor}'
            f' | forma={lc.forma_pagamento!r}'
            f' | categoria={lc.categoria!r}'
            f' | venda_id={getattr(lc, "venda_id", None)}'
            f'\n      descricao_repr={lc.descricao!r}'
        )

    linhas = [
        f'=== INSPECT Venda #{venda_id} (tenant {eid}) ===',
        '',
        '--- VENDA (campos brutos) ---',
        f'  id              = {venda.id}',
        f'  empresa_id      = {venda.empresa_id}',
        f'  cliente_id      = {venda.cliente_id}',
        f'  produto_id      = {venda.produto_id}',
        f'  preco_venda     = {venda.preco_venda}',
        f'  quantidade      = {venda.quantidade_venda}',
        f'  valor_total     = {valor_total}',
        f'  valor_pago      = {venda.valor_pago}',
        f'  situacao        = {venda.situacao!r}',
        f'  forma_pagamento = {venda.forma_pagamento!r}',
        f'  data_venda      = {venda.data_venda}',
        f'  data_vencimento = {venda.data_vencimento}',
        f'  nf              = {venda.nf!r}',
        f'  tipo_operacao   = {venda.tipo_operacao!r}',
        '',
        f'--- LANCAMENTOS por venda_id (FK) ({len(lancs_por_fk)}) ---',
    ]
    if lancs_por_fk:
        for lc in lancs_por_fk:
            linhas.append(_fmt_lanc(lc))
    else:
        linhas.append('  (nenhum)')

    linhas.extend([
        '',
        f'--- LANCAMENTOS com substring "{venda_id}" na descricao ({len(lancs_por_substring)}) ---',
    ])
    if lancs_por_substring:
        for lc in lancs_por_substring:
            linhas.append(_fmt_lanc(lc))
    else:
        linhas.append('  (nenhum)')

    linhas.extend([
        '',
        f'--- LANCAMENTOS com valor == {valor_total} ({len(lancs_por_valor)}) ---',
    ])
    if lancs_por_valor:
        for lc in lancs_por_valor:
            linhas.append(_fmt_lanc(lc))
    else:
        linhas.append('  (nenhum)')

    linhas.extend([
        '',
        '--- DICAS DE INTERPRETACAO ---',
        '  - descricao_repr usa repr() para revelar:',
        '      * aspas/caracteres ocultos',
        '      * traços não-ASCII (— vs - vs –)',
        '      * espaços não-quebráveis (\\xa0)',
        '      * quebras de linha (\\n, \\r)',
        '  - Padrao esperado pelo regex atual: "Venda #N - <cliente>"',
        '    (espaco, traco ASCII, espaco). Se o que aparecer for diferente',
        '    (ex: "Venda#N", "Venda N", "venda nro N", "Venda #N — <cliente>"),',
        '    o regex precisa ser ajustado para tolerar essas variações.',
    ])

    output = '\n'.join(linhas)
    logging.info(
        '[INSPECT-VENDA] empresa=%s id=%s fk=%d substr=%d valor=%d',
        eid, venda_id, len(lancs_por_fk),
        len(lancs_por_substring), len(lancs_por_valor),
    )
    return output, 200, {'Content-Type': 'text/plain; charset=utf-8'}
