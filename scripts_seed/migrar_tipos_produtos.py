"""Migração: amplia VARCHAR de tipo de produto e corrige nomes truncados.

Diagnóstico do problema:

- Os models declaram ``Produto.tipo = String(50)`` e ``TipoProduto.nome
  = String(100)``, mas o banco físico em produção foi criado em deploy
  anterior com VARCHAR menores (provavelmente VARCHAR(6)). Postgres
  trunca silenciosamente quando o INSERT/UPDATE excede o tamanho da
  coluna física, mesmo que o ORM declare maior.
- Resultado: tipos como "CHARQUE" e "SACOLAS" gravam como "CHARQU" e
  "SACOLA" (6 chars exatos), e ao tentar editar pela UI o ALTER falha
  porque o backend manda 7 chars contra a coluna VARCHAR(6).

O que este script faz:

1. ``ALTER COLUMN`` em ``produtos.tipo`` e ``tipos_produto.nome`` para
   pelo menos VARCHAR(50). Idempotente: se já estiver maior, no-op.
2. Renomeia ``TipoProduto.nome = 'CHARQU'`` para ``'CHARQUE'`` (e
   atualiza ``Produto.tipo`` correspondente em massa).
3. Opcionalmente trata ``'SACOLA'`` (6 chars) — confirma com o operador
   se deve virar ``'SACOLAS'`` (heurística: se houver 7+ produtos cuja
   marca/nome sugere sacolas, provavelmente SIM; mas aqui pedimos
   confirmação interativa ou via flag).
4. Chama ``limpar_cache_dashboard()`` ao final para sincronizar UI.

Uso:

    # Dry-run (default): só imprime o plano.
    python scripts_seed/migrar_tipos_produtos.py

    # Aplicar de fato no banco apontado por DATABASE_URL:
    python scripts_seed/migrar_tipos_produtos.py --apply

    # Renomear SACOLA -> SACOLAS junto com a migração:
    python scripts_seed/migrar_tipos_produtos.py --apply --renomear-sacola SACOLAS

    # Ou skip (default): mantém SACOLA como está.
    python scripts_seed/migrar_tipos_produtos.py --apply
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from sqlalchemy import text  # noqa: E402

from app import app, db  # noqa: E402
from models import Produto, TipoProduto  # noqa: E402
from services.cache_utils import limpar_cache_dashboard  # noqa: E402


TARGET_LEN = 50


def _detectar_dialeto(engine):
    return (engine.dialect.name or '').lower()


def _tamanho_atual(conn, dialeto, tabela, coluna):
    """Retorna o tamanho VARCHAR atual da coluna, ou None se não suportado."""
    if dialeto == 'postgresql':
        sql = text(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_name = :tabela AND column_name = :coluna"
        )
        row = conn.execute(sql, {'tabela': tabela, 'coluna': coluna}).fetchone()
        return row[0] if row else None
    if dialeto == 'sqlite':
        sql = text(f'PRAGMA table_info("{tabela}")')
        rows = conn.execute(sql).fetchall()
        for r in rows:
            if r[1] == coluna:
                tipo = (r[2] or '').upper()
                if 'VARCHAR(' in tipo:
                    try:
                        return int(tipo.split('VARCHAR(')[1].split(')')[0])
                    except (IndexError, ValueError):
                        return None
                return None
        return None
    return None


def _alterar_coluna(conn, dialeto, tabela, coluna, target_len):
    """Aplica ALTER COLUMN ... TYPE VARCHAR(target_len) de forma idempotente."""
    atual = _tamanho_atual(conn, dialeto, tabela, coluna)
    print(f'  {tabela}.{coluna}: tamanho atual = {atual}, alvo = {target_len}')
    if atual is not None and atual >= target_len:
        print(f'  -> ja esta >= {target_len}, no-op')
        return False
    if dialeto == 'postgresql':
        sql = text(f'ALTER TABLE "{tabela}" ALTER COLUMN "{coluna}" TYPE VARCHAR({target_len})')
        conn.execute(sql)
        print(f'  -> ALTER aplicado em {tabela}.{coluna}')
        return True
    if dialeto == 'sqlite':
        print('  -> SQLite: schema nao restringe VARCHAR; ignorando ALTER (no-op).')
        return False
    print(f'  -> Dialeto {dialeto!r} nao suportado para ALTER automatico. Ajuste manualmente.')
    return False


def _renomear_tipo(empresa_id, antigo, novo, apply_changes):
    """Renomeia TipoProduto e propaga para Produto.tipo. Retorna (tipo_obj, n_produtos)."""
    q = TipoProduto.query.filter_by(nome=antigo)
    if empresa_id is not None:
        q = q.filter_by(empresa_id=empresa_id)
    tipos = q.all()
    if not tipos:
        print(f'  Nenhum TipoProduto com nome={antigo!r} encontrado.')
        return None, 0

    total_produtos = 0
    for tipo_obj in tipos:
        produtos = Produto.query.filter_by(empresa_id=tipo_obj.empresa_id, tipo=antigo).all()
        n = len(produtos)
        total_produtos += n
        print(
            f'  TipoProduto id={tipo_obj.id} empresa={tipo_obj.empresa_id} '
            f'{antigo!r} -> {novo!r}; produtos afetados: {n}'
        )
        if apply_changes:
            tipo_obj.nome = novo
            for p in produtos:
                p.tipo = novo

    return tipos, total_produtos


def main():
    parser = argparse.ArgumentParser(description='Migra schema VARCHAR e corrige tipos truncados.')
    parser.add_argument('--apply', action='store_true', help='Aplica no banco. Sem flag, dry-run.')
    parser.add_argument(
        '--renomear-sacola',
        default=None,
        help='Se informado, renomeia TipoProduto SACOLA para o valor dado (ex: SACOLAS). '
             'Default: nao mexe em SACOLA.',
    )
    parser.add_argument(
        '--empresa',
        type=int,
        default=None,
        help='Restringe a migracao de nome a uma empresa especifica (default: todas).',
    )
    args = parser.parse_args()

    apply_changes = args.apply
    print('=' * 60)
    print(f'Migracao de Tipos de Produto  ({"APPLY" if apply_changes else "DRY-RUN"})')
    print('=' * 60)

    with app.app_context():
        engine = db.engine
        dialeto = _detectar_dialeto(engine)
        print(f'Dialeto detectado: {dialeto}')

        with engine.begin() as conn:
            print('\n[1] Ampliando colunas para VARCHAR({}):'.format(TARGET_LEN))
            _alterar_coluna(conn, dialeto, 'produtos', 'tipo', TARGET_LEN)
            _alterar_coluna(conn, dialeto, 'tipos_produto', 'nome', TARGET_LEN)

        print('\n[2] Renomeando CHARQU -> CHARQUE:')
        _renomear_tipo(args.empresa, 'CHARQU', 'CHARQUE', apply_changes)

        if args.renomear_sacola:
            destino = str(args.renomear_sacola).strip().upper()
            if destino and destino != 'SACOLA':
                print(f'\n[3] Renomeando SACOLA -> {destino}:')
                _renomear_tipo(args.empresa, 'SACOLA', destino, apply_changes)
            else:
                print('\n[3] --renomear-sacola informado mas vazio/igual; ignorando.')
        else:
            print('\n[3] SACOLA: nao foi pedido renomear (passe --renomear-sacola NOVO_NOME se quiser).')

        if apply_changes:
            db.session.commit()
            print('\n[4] db.session.commit() ok. Limpando cache do dashboard...')
            try:
                limpar_cache_dashboard()
                print('  -> limpar_cache_dashboard() ok')
            except Exception as exc:
                print(f'  -> aviso: limpar_cache_dashboard falhou: {exc}')
        else:
            print('\n[4] Modo dry-run: nada foi gravado. Rode com --apply para confirmar.')

    print('\n' + '=' * 60)
    print('Migracao concluida.')
    print('=' * 60)


if __name__ == '__main__':
    main()
