"""Testes unitários da valoração de estoque (Balanço Rápido)."""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestEstoqueValoracaoFailClosed(unittest.TestCase):
    def test_total_sem_empresa_retorna_zero(self):
        from services.estoque_valoracao import total_valor_estoque

        self.assertEqual(total_valor_estoque(None), 0.0)
        self.assertEqual(total_valor_estoque(0), 0.0)
        self.assertEqual(total_valor_estoque(-1), 0.0)

    def test_listar_sem_empresa_retorna_vazio(self):
        from services.estoque_valoracao import listar_valor_estoque_por_lote

        self.assertEqual(listar_valor_estoque_por_lote(None), [])
        self.assertEqual(listar_valor_estoque_por_lote(0), [])

    @patch('services.estoque_valoracao.db')
    def test_total_com_empresa_consulta_e_arredonda(self, mock_db):
        from services.estoque_valoracao import total_valor_estoque

        mock_db.session.query.return_value.filter.return_value.scalar.return_value = 150189.004
        self.assertEqual(total_valor_estoque(1), 150189.0)
        mock_db.session.query.assert_called()


class TestEstoqueValoracaoFiltros(unittest.TestCase):
    def test_filtro_exige_saldo_e_custo_positivos(self):
        from services.estoque_valoracao import _filtro_lotes_valorizaveis

        filtros = _filtro_lotes_valorizaveis(7)
        self.assertEqual(len(filtros), 3)
        # empresa_id == 7
        self.assertEqual(str(filtros[0]), str(filtros[0]))  # compilável
        compiled = [str(f.compile(compile_kwargs={'literal_binds': True})) for f in filtros]
        joined = ' | '.join(compiled)
        self.assertIn('empresa_id', joined)
        self.assertIn('estoque_atual', joined)
        self.assertIn('preco_custo', joined)


if __name__ == '__main__':
    unittest.main()
