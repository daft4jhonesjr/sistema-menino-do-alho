"""Geração do feed ICS (Webcal) do calendário de entregas/boletos/lembretes/orçamento.

O Apple Calendar / Google Calendar sincronizam via GET periódico sem cookies
de sessão Flask. Por isso o feed é autenticado por token HMAC por tenant:

    token = ``{empresa_id}.{assinatura}``

A assinatura é derivada de ``SECRET_KEY`` + ``empresa_id`` — sem coluna extra
no banco e sem vazar dados entre tenants.
"""
from __future__ import annotations

import calendar
import hashlib
import hmac
from datetime import date, datetime, timedelta
from decimal import Decimal

from flask import current_app
from icalendar import Calendar, Event
from sqlalchemy.orm import joinedload
from sqlalchemy import func

from models import db, Venda, Lembrete, Empresa, ItemOrcamento, PagamentoOrcamento


def gerar_token_calendario(empresa_id: int) -> str:
    """Gera token estável para o feed ICS do tenant."""
    secret = _secret_bytes()
    sig = hmac.new(
        secret,
        f'calendario-feed:{int(empresa_id)}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f'{int(empresa_id)}.{sig}'


def validar_token_calendario(token: str | None) -> int | None:
    """Valida o token e devolve ``empresa_id``, ou ``None`` se inválido."""
    if not token or '.' not in token:
        return None
    eid_raw, sig = token.split('.', 1)
    try:
        empresa_id = int(eid_raw)
    except (TypeError, ValueError):
        return None
    if empresa_id <= 0 or not sig:
        return None
    esperado = gerar_token_calendario(empresa_id)
    if not hmac.compare_digest(token, esperado):
        return None
    return empresa_id


def montar_feed_ics(empresa_id: int, inicio: date | None = None) -> bytes:
    """Consulta entregas, boletos, lembretes e orçamento a partir de ``inicio`` e monta ICS."""
    if inicio is None:
        hoje = date.today()
        inicio = date(hoje.year, hoje.month, 1)

    fim_ics = _fim_horizonte_feed(inicio)

    empresa = Empresa.query.filter_by(id=empresa_id, ativo=True).first()
    nome_cal = (empresa.nome_fantasia if empresa else None) or 'Menino do Alho'
    cal_name = f'{nome_cal} — Agenda'

    cal = Calendar()
    cal.add('prodid', '-//Menino do Alho//Calendario//PT')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', cal_name)
    cal.add('x-wr-timezone', 'America/Sao_Paulo')

    _adicionar_entregas(cal, empresa_id, inicio)
    _adicionar_boletos(cal, empresa_id, inicio)
    _adicionar_lembretes(cal, empresa_id, inicio)
    _adicionar_orcamentos(cal, empresa_id, inicio, fim_ics)

    return cal.to_ical()


def _secret_bytes() -> bytes:
    key = current_app.config.get('SECRET_KEY') or current_app.secret_key or ''
    if isinstance(key, bytes):
        return key
    return str(key).encode('utf-8')


def _fmt_moeda(valor) -> str:
    try:
        num = float(valor or 0)
    except (TypeError, ValueError):
        num = 0.0
    negativo = num < 0
    s = f'R$ {abs(num):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return ('-' + s) if negativo else s


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _fim_horizonte_feed(inicio: date, meses: int = 24) -> date:
    """Último dia do mês ``meses`` meses à frente do mês de ``inicio`` (inclusivo)."""
    y, m = inicio.year, inicio.month
    for _ in range(max(meses - 1, 0)):
        m += 1
        if m > 12:
            m = 1
            y += 1
    return date(y, m, calendar.monthrange(y, m)[1])


def _iter_meses_entre(inicio: date, fim: date):
    """Gera pares (ano, mês) do mês de ``inicio`` até o de ``fim`` (inclusivos)."""
    y, m = inicio.year, inicio.month
    fy, fm = fim.year, fim.month
    while (y, m) <= (fy, fm):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def _projeta_dia_mensal(data_base: date, ano: int, mes: int) -> date:
    """Projeta o dia de ``data_base`` em outro mês/ano (ajusta meses curtos)."""
    ultimo = calendar.monthrange(ano, mes)[1]
    return date(ano, mes, min(data_base.day, ultimo))


def _titulo_evento_orcamento(item: ItemOrcamento) -> str:
    desc = (item.descricao or 'Item').strip()
    return f'[Orçamento] {desc} - {_fmt_moeda(item.valor)}'


def _query_itens_orcamento_agenda(empresa_id: int) -> list[ItemOrcamento]:
    return (
        ItemOrcamento.query
        .filter(
            ItemOrcamento.empresa_id == empresa_id,
            ItemOrcamento.data_vencimento.isnot(None),
        )
        .order_by(ItemOrcamento.descricao.asc(), ItemOrcamento.id.asc())
        .all()
    )


def _pagamentos_orcamento_no_intervalo(
    item_ids: list[int],
    inicio: date,
    fim: date,
) -> set[tuple[int, str]]:
    """Conjunto de (item_id, mes_ano) marcados como pagos no intervalo."""
    if not item_ids:
        return set()
    meses = {f'{y:04d}-{m:02d}' for y, m in _iter_meses_entre(inicio, fim)}
    if not meses:
        return set()
    rows = (
        db.session.query(PagamentoOrcamento.item_id, PagamentoOrcamento.mes_ano)
        .filter(
            PagamentoOrcamento.item_id.in_(item_ids),
            PagamentoOrcamento.mes_ano.in_(meses),
        )
        .all()
    )
    return {(int(r[0]), str(r[1])) for r in rows}


def agrupar_orcamentos_por_data(
    empresa_id: int,
    inicio: date,
    fim: date,
) -> dict[str, list[dict]]:
    """Mapa ISO → eventos de orçamento (projeção mensal recorrente no intervalo)."""
    if fim < inicio:
        return {}

    itens = _query_itens_orcamento_agenda(empresa_id)
    pagos = _pagamentos_orcamento_no_intervalo([i.id for i in itens], inicio, fim)

    resultado: dict[str, list[dict]] = {}
    for item in itens:
        base = _as_date(item.data_vencimento)
        if not base:
            continue
        desc = (item.descricao or 'Item').strip()
        valor_fmt = _fmt_moeda(item.valor)
        titulo = _titulo_evento_orcamento(item)
        cat = (item.categoria or '').strip()
        forma = (item.forma_pagamento or '').strip()
        for y, m in _iter_meses_entre(inicio, fim):
            dia = _projeta_dia_mensal(base, y, m)
            if dia < inicio or dia > fim:
                continue
            mes_ano = f'{y:04d}-{m:02d}'
            iso = dia.isoformat()
            resultado.setdefault(iso, []).append({
                'titulo': titulo,
                'valor': valor_fmt,
                'descricao': desc,
                'categoria': cat,
                'forma_pagamento': forma,
                'item_id': item.id,
                'pago': (item.id, mes_ano) in pagos,
            })

    for itens in resultado.values():
        itens.sort(key=lambda x: (x.get('descricao') or '', x.get('item_id') or 0))
    return dict(sorted(resultado.items()))


def _add_all_day(cal: Calendar, *, uid: str, dia: date, summary: str, description: str = '') -> None:
    event = Event()
    event.add('uid', uid)
    event.add('dtstamp', datetime.utcnow())
    event.add('dtstart', dia)
    event.add('dtend', dia + timedelta(days=1))
    event.add('summary', summary)
    if description:
        event.add('description', description)
    event.add('transp', 'TRANSPARENT')
    cal.add_component(event)


def _adicionar_entregas(cal: Calendar, empresa_id: int, inicio: date) -> None:
    vendas = (
        Venda.query
        .options(joinedload(Venda.cliente), joinedload(Venda.produto))
        .filter(
            Venda.empresa_id == empresa_id,
            Venda.data_venda.isnot(None),
            Venda.data_venda >= inicio,
            func.upper(func.coalesce(Venda.status_entrega, 'PENDENTE')) != 'ENTREGUE',
            func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA',
        )
        .order_by(Venda.data_venda.asc(), Venda.cliente_id.asc(), Venda.id.asc())
        .limit(3000)
        .all()
    )

    pedidos = {}
    for v in vendas:
        dia = _as_date(v.data_venda)
        if not dia or dia < inicio:
            continue
        iso = dia.isoformat()
        cnpj = (v.cliente.cnpj if v.cliente else '') or ''
        is_cf = cnpj in ('0', '00000000000000', '')
        if is_cf:
            avulso = str(getattr(v, 'cliente_avulso', '') or '').strip().upper()
            pkey = (iso, v.cliente_id, avulso)
        else:
            nf = str(v.nf).strip() if v.nf else ''
            pkey = (iso, v.cliente_id, nf)

        if pkey not in pedidos:
            nome = str(
                (v.cliente.nome_cliente if v.cliente else None)
                or getattr(v, 'cliente_avulso', None)
                or 'Cliente'
            ).strip()
            pedidos[pkey] = {
                'dia': dia,
                'id': v.id,
                'cliente': nome,
                'itens': [],
                'valor': Decimal('0.00'),
            }

        prod = v.produto.nome_produto if v.produto else 'Produto'
        qtd = int(getattr(v, 'quantidade_venda', 0) or 0)
        pedidos[pkey]['itens'].append(f'{qtd}x {prod}')
        try:
            pedidos[pkey]['valor'] += Decimal(str(v.calcular_total() or 0))
        except Exception:
            pass

    for p in pedidos.values():
        itens = ' · '.join(p['itens']) if p['itens'] else '—'
        desc = f"Entrega pendente\nItens: {itens}\nValor: {_fmt_moeda(p['valor'])}"
        _add_all_day(
            cal,
            uid=f"entrega-{p['id']}@menino-do-alho",
            dia=p['dia'],
            summary=f"[ENTREGA] {p['cliente']}",
            description=desc,
        )


def _adicionar_boletos(cal: Calendar, empresa_id: int, inicio: date) -> None:
    vendas = (
        Venda.query
        .options(joinedload(Venda.cliente))
        .filter(
            Venda.empresa_id == empresa_id,
            Venda.data_vencimento.isnot(None),
            Venda.data_vencimento >= inicio,
            func.upper(func.coalesce(Venda.situacao, 'PENDENTE')).in_(
                ['PENDENTE', 'PARCIAL']
            ),
            func.upper(func.coalesce(Venda.tipo_operacao, 'VENDA')) != 'PERDA',
        )
        .order_by(Venda.data_vencimento.asc(), Venda.cliente_id.asc(), Venda.id.asc())
        .limit(2000)
        .all()
    )

    pedidos = {}
    for v in vendas:
        dia = _as_date(v.data_vencimento)
        if not dia or dia < inicio:
            continue
        iso = dia.isoformat()
        cnpj = (v.cliente.cnpj if v.cliente else '') or ''
        is_cf = cnpj in ('0', '00000000000000', '')
        nf = str(v.nf).strip() if v.nf else ''
        if is_cf:
            avulso = str(getattr(v, 'cliente_avulso', '') or '').strip().upper()
            pkey = (iso, v.cliente_id, avulso)
        else:
            pkey = (iso, v.cliente_id, nf)

        if pkey not in pedidos:
            nome = str(
                (v.cliente.nome_cliente if v.cliente else None)
                or getattr(v, 'cliente_avulso', None)
                or 'Cliente'
            ).strip()
            pedidos[pkey] = {
                'dia': dia,
                'id': v.id,
                'titulo': nome,
                'nf': nf,
                'valor': Decimal('0.00'),
                'status': str(v.situacao or 'PENDENTE').upper(),
            }

        try:
            pedidos[pkey]['valor'] += Decimal(str(v.calcular_total() or 0))
            if str(v.situacao or '').upper() == 'PARCIAL':
                pedidos[pkey]['status'] = 'PARCIAL'
        except Exception:
            pass
        if nf and not pedidos[pkey].get('nf'):
            pedidos[pkey]['nf'] = nf

    for p in pedidos.values():
        nf_txt = f"NF {p['nf']}" if p.get('nf') else 'Sem NF'
        desc = (
            f"Boleto a vencer ({p['status']})\n"
            f"{nf_txt}\n"
            f"Valor: {_fmt_moeda(p['valor'])}"
        )
        _add_all_day(
            cal,
            uid=f"boleto-{p['id']}@menino-do-alho",
            dia=p['dia'],
            summary=f"[BOLETO] {p['titulo']}",
            description=desc,
        )


def _adicionar_lembretes(cal: Calendar, empresa_id: int, inicio: date) -> None:
    lembretes = (
        Lembrete.query
        .filter(
            Lembrete.empresa_id == empresa_id,
            Lembrete.data >= inicio,
            Lembrete.concluido.is_(False),
        )
        .order_by(Lembrete.data.asc(), Lembrete.ordem.asc(), Lembrete.criado_em.asc())
        .limit(1000)
        .all()
    )
    for lem in lembretes:
        dia = _as_date(lem.data)
        if not dia:
            continue
        titulo = (lem.descricao or 'Lembrete').strip()
        horario = (getattr(lem, 'horario', None) or '').strip()
        recorrencia = (getattr(lem, 'recorrencia', None) or 'nunca').strip()
        descricao = titulo
        if recorrencia and recorrencia != 'nunca':
            descricao = f'{titulo}\nRepetição: {recorrencia}'
        partes = horario.split(':') if horario else []
        if len(partes) == 2 and partes[0].isdigit() and partes[1].isdigit():
            inicio_dt = datetime(dia.year, dia.month, dia.day, int(partes[0]), int(partes[1]))
            event = Event()
            event.add('uid', f'lembrete-{lem.id}@menino-do-alho')
            event.add('dtstamp', datetime.utcnow())
            event.add('dtstart', inicio_dt)
            event.add('dtend', inicio_dt + timedelta(minutes=30))
            event.add('summary', f'[LEMBRETE] {titulo}')
            event.add('description', descricao)
            event.add('transp', 'TRANSPARENT')
            cal.add_component(event)
            continue
        _add_all_day(
            cal,
            uid=f"lembrete-{lem.id}@menino-do-alho",
            dia=dia,
            summary=f"[LEMBRETE] {titulo}",
            description=descricao,
        )


def _adicionar_orcamentos(
    cal: Calendar,
    empresa_id: int,
    inicio: date,
    fim: date,
) -> None:
    for iso, ocorrencias in agrupar_orcamentos_por_data(empresa_id, inicio, fim).items():
        for occ in ocorrencias:
            dia = date.fromisoformat(iso)
            item_id = occ.get('item_id')
            desc = occ.get('descricao') or 'Item'
            cat = occ.get('categoria') or ''
            forma = occ.get('forma_pagamento') or ''
            linhas = [f'Despesa de orçamento\nValor: {occ.get("valor") or _fmt_moeda(0)}']
            if cat:
                linhas.append(f'Categoria: {cat}')
            if forma:
                linhas.append(f'Forma: {forma}')
            if occ.get('pago'):
                linhas.append('Status: Pago')
            titulo_ics = occ.get('titulo') or f'[Orçamento] {desc}'
            if occ.get('pago'):
                titulo_ics = f'[PAGO] {titulo_ics}'
            event = Event()
            event.add('uid', f'orcamento-{item_id}-{iso}@menino-do-alho')
            event.add('dtstamp', datetime.utcnow())
            event.add('dtstart', dia)
            event.add('dtend', dia + timedelta(days=1))
            event.add('summary', titulo_ics)
            event.add('description', '\n'.join(linhas))
            event.add('categories', ['Orçamento', 'Despesa'])
            event.add('transp', 'TRANSPARENT')
            cal.add_component(event)
