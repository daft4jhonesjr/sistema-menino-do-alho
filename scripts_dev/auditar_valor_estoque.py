"""Auditoria do ativo ``VALOR EM ESTOQUE`` (Balanço Patrimonial Rápido).

Uso (somente leitura por padrão):

    EMPRESA_ID=1 python scripts_dev/auditar_valor_estoque.py

    # Top 30 lotes por valor + anomalias de saldo
    EMPRESA_ID=1 LIMITE=30 python scripts_dev/auditar_valor_estoque.py

    # Exporta CSV com todos os lotes valorizáveis
    EMPRESA_ID=1 CSV_OUT=/tmp/estoque_audit.csv python scripts_dev/auditar_valor_estoque.py

O que imprime:
  1. Total canônico = Σ (estoque_atual × preco_custo) com estoque > 0 e custo > 0
     (mesma regra de ``services.estoque_valoracao.total_valor_estoque``).
  2. Ranking dos lotes pelo valor (maior → menor).
  3. Anomalias: ``delta = estoque_atual − (quantidade_entrada − vendido − devolvido)``.
     delta > 0 sugere estoque inflado ou ``quantidade_entrada`` desatualizada.
  4. SQL equivalente para rodar direto no Postgres/Neon.

NÃO muta o banco. Para corrigir dados, use os fluxos de edição/devolução
ou um script de reparo separado após validar o diagnóstico.
"""
from __future__ import annotations

import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import app  # noqa: E402
from models import Empresa  # noqa: E402
from services.estoque_valoracao import (  # noqa: E402
    listar_valor_estoque_por_lote,
    total_valor_estoque,
)


SQL_EQUIVALENTE = """
-- Total do balanço (por empresa)
SELECT ROUND(COALESCE(SUM(
    CAST(COALESCE(estoque_atual, 0) AS NUMERIC(14,2))
    * CAST(COALESCE(preco_custo, 0) AS NUMERIC(14,2))
), 0), 2) AS total_valor_estoque
FROM produtos
WHERE empresa_id = :empresa_id
  AND COALESCE(estoque_atual, 0) > 0
  AND COALESCE(preco_custo, 0) > 0;

-- Ranking por lote + delta vs baixas
SELECT
  p.id,
  p.nome_produto,
  p.tipo,
  p.quantidade_entrada,
  p.estoque_atual,
  COALESCE(p.quantidade_devolvida, 0) AS qtd_devolvida,
  COALESCE(v.qtd_vendida, 0) AS qtd_vendida,
  (p.quantidade_entrada
     - COALESCE(v.qtd_vendida, 0)
     - COALESCE(p.quantidade_devolvida, 0)) AS saldo_esperado,
  (p.estoque_atual
     - (p.quantidade_entrada
        - COALESCE(v.qtd_vendida, 0)
        - COALESCE(p.quantidade_devolvida, 0))) AS delta_estoque,
  p.preco_custo,
  ROUND(
    CAST(p.estoque_atual AS NUMERIC(14,2))
    * CAST(p.preco_custo AS NUMERIC(14,2)), 2
  ) AS valor_estoque
FROM produtos p
LEFT JOIN (
  SELECT produto_id, SUM(quantidade_venda) AS qtd_vendida
  FROM vendas
  WHERE empresa_id = :empresa_id
  GROUP BY produto_id
) v ON v.produto_id = p.id
WHERE p.empresa_id = :empresa_id
  AND COALESCE(p.estoque_atual, 0) > 0
  AND COALESCE(p.preco_custo, 0) > 0
ORDER BY valor_estoque DESC, p.id ASC;
""".strip()


def _fmt_brl(valor: float) -> str:
    n = float(valor or 0)
    negativo = n < 0
    s = f'{abs(n):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
    return f'-R$ {s}' if negativo else f'R$ {s}'


def _print_secao(titulo: str) -> None:
    print()
    print('=' * 78)
    print(titulo)
    print('=' * 78)


def main() -> int:
    raw_eid = (os.environ.get('EMPRESA_ID') or '').strip()
    if not raw_eid:
        print('Informe EMPRESA_ID=<id> (tenant a auditar).')
        print('Ex.: EMPRESA_ID=1 python scripts_dev/auditar_valor_estoque.py')
        return 2

    try:
        empresa_id = int(raw_eid)
    except ValueError:
        print(f'EMPRESA_ID inválido: {raw_eid!r}')
        return 2

    limite_raw = (os.environ.get('LIMITE') or '').strip()
    limite = int(limite_raw) if limite_raw.isdigit() else None
    csv_out = (os.environ.get('CSV_OUT') or '').strip() or None

    with app.app_context():
        emp = Empresa.query.get(empresa_id)
        if not emp:
            print(f'Empresa #{empresa_id} não encontrada.')
            return 1

        nome = (getattr(emp, 'nome_fantasia', None) or getattr(emp, 'nome', None) or '').strip()
        _print_secao(f'EMPRESA #{empresa_id} — {nome or "(sem nome)"}')

        total = total_valor_estoque(empresa_id)
        lotes = listar_valor_estoque_por_lote(empresa_id, limite=None)
        anomalias = [l for l in lotes if l.get('anomalia')]
        inflacao_unidades = sum(max(0, int(l['delta_estoque'])) for l in anomalias)
        inflacao_valor = sum(
            max(0, int(l['delta_estoque'])) * float(l['preco_custo'])
            for l in anomalias
        )

        print(f'\nTotal VALOR EM ESTOQUE (canônico): {_fmt_brl(total)}')
        print(f'Lotes com saldo > 0 e custo > 0: {len(lotes)}')
        print(f'Lotes com delta de saldo ≠ 0:     {len(anomalias)}')
        print(
            f'Unidades acima do saldo esperado:  {inflacao_unidades} '
            f'(≈ {_fmt_brl(inflacao_valor)} se custo unitário atual)'
        )

        exibir = lotes[:limite] if limite else lotes
        _print_secao(
            f'RANKING POR VALOR ({len(exibir)} de {len(lotes)} lotes'
            + (f', LIMITE={limite}' if limite else '')
            + ')'
        )
        print(
            f'{"ID":>6}  {"VALOR":>14}  {"EST":>5}  {"ENT":>5}  '
            f'{"VND":>5}  {"DEV":>4}  {"Δ":>5}  {"CUSTO":>10}  NOME'
        )
        for l in exibir:
            flag = '!' if l['anomalia'] else ' '
            print(
                f'{flag}{l["id"]:>5}  {_fmt_brl(l["valor_estoque"]):>14}  '
                f'{l["estoque_atual"]:>5}  {l["quantidade_entrada"]:>5}  '
                f'{l["quantidade_vendida"]:>5}  {l["quantidade_devolvida"]:>4}  '
                f'{l["delta_estoque"]:>5}  {_fmt_brl(l["preco_custo"]):>10}  '
                f'{l["nome_produto"][:48]}'
            )

        if anomalias:
            _print_secao(f'ANOMALIAS DE SALDO (top {min(25, len(anomalias))})')
            anomalias_ord = sorted(
                anomalias,
                key=lambda x: abs(int(x['delta_estoque'])) * float(x['preco_custo']),
                reverse=True,
            )
            for l in anomalias_ord[:25]:
                print(
                    f'  #{l["id"]} {l["nome_produto"][:40]:<40} '
                    f'estoque={l["estoque_atual"]} esperado={l["saldo_esperado"]} '
                    f'delta={l["delta_estoque"]:+d} valor={_fmt_brl(l["valor_estoque"])}'
                )
            print(
                '\nInterpretação: delta>0 → estoque_atual acima de '
                '(entrada − vendas − devoluções). Pode ser reimportação/'
                'edição que somou estoque sem atualizar entrada (já corrigido '
                'no código) ou baixa de venda que não debitou.'
            )

        if csv_out:
            with open(csv_out, 'w', newline='', encoding='utf-8') as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        'id', 'nome_produto', 'tipo', 'fornecedor', 'data_chegada',
                        'quantidade_entrada', 'estoque_atual', 'quantidade_vendida',
                        'quantidade_devolvida', 'saldo_esperado', 'delta_estoque',
                        'anomalia', 'preco_custo', 'valor_estoque',
                    ],
                )
                writer.writeheader()
                writer.writerows(lotes)
            print(f'\nCSV escrito em: {csv_out}')

        _print_secao('SQL EQUIVALENTE')
        print(SQL_EQUIVALENTE.replace(':empresa_id', str(empresa_id)))

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
