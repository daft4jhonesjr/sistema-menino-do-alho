"""Testes da extração de NF e valor em boletos (caso Comercial Paty, NF 13084)."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import (  # noqa: E402
    _extrair_nf_boleto,
    _extrair_valor_boleto,
    _parse_valor_boleto_seguro,
    _valor_da_linha_digitavel,
)

LINHA_DIGITAVEL = '23790.12345 67890.123456 78901.234567 8 16490000190000'

TEXTO_PATY = f"""
Bradesco 237-2 {LINHA_DIGITAVEL}
Local de Pagamento Vencimento
Pagável preferencialmente na Rede Bradesco 03/12/2026
Beneficiário Agência/Código Beneficiário
COMERCIAL PATY IMPORTACAO LTDA 1234-5/0012345-6
Data do Documento Numero Documento Espécie Doc. Aceite Data Processamento
03/11/2026 13084/1 DM N 03/11/2026
(=) Valor do Documento
1.900,00
Instruções (Texto de responsabilidade do beneficiário)
NF: 13084
Após o vencimento cobrar juros de R$ 0,63 ao dia
Pagador J E COMERCIO DE PRODUTOS ALIMENTICIOS LTDA
"""


class TestNfBoleto(unittest.TestCase):
    def test_instrucao_nf_tem_prioridade(self):
        self.assertEqual(_extrair_nf_boleto(TEXTO_PATY), '13084')

    def test_numero_documento_com_parcela(self):
        texto = (
            "Data do Documento Numero Documento Espécie Doc. Aceite\n"
            "03/11/2026 13084/1 DM N 03/11/2026\n"
        )
        self.assertEqual(_extrair_nf_boleto(texto), '13084')

    def test_numero_documento_mesma_linha_hifen(self):
        self.assertEqual(_extrair_nf_boleto('Nº Documento: 13084-1'), '13084')

    def test_nota_fiscal_por_extenso(self):
        self.assertEqual(_extrair_nf_boleto('Ref. Nota Fiscal 13084'), '13084')

    def test_dia_da_data_nao_vira_nf(self):
        texto = (
            "Data do Documento Numero Documento Espécie\n"
            "03/11/2026 DM N\n"
        )
        self.assertIsNone(_extrair_nf_boleto(texto))

    def test_nf_curta_descartada(self):
        self.assertIsNone(_extrair_nf_boleto('NF: 3'))


class TestValorBoleto(unittest.TestCase):
    def test_rotulo_valor_do_documento(self):
        self.assertEqual(_extrair_valor_boleto(TEXTO_PATY), 1900.0)

    def test_ignora_juros_sem_rotulo_nem_linha_digitavel(self):
        texto = "Instruções\nApós o vencimento cobrar juros de R$ 0,63 ao dia\n"
        self.assertIsNone(_extrair_valor_boleto(texto))

    def test_linha_digitavel_quando_sem_rotulo(self):
        texto = f"{LINHA_DIGITAVEL}\nCobrar juros de R$ 0,63 ao dia\n"
        self.assertEqual(_extrair_valor_boleto(texto), 1900.0)

    def test_valor_da_linha_digitavel(self):
        self.assertEqual(_valor_da_linha_digitavel(LINHA_DIGITAVEL), 1900.0)

    def test_valor_do_codigo_de_barras_44(self):
        barras = '23791' + '1649' + '0000190000' + '0' * 25
        self.assertEqual(len(barras), 44)
        self.assertEqual(_valor_da_linha_digitavel(barras), 1900.0)

    def test_parse_seguro(self):
        self.assertEqual(_parse_valor_boleto_seguro('1.900,00'), 1900.0)
        self.assertEqual(_parse_valor_boleto_seguro('1900.00'), 1900.0)
        self.assertEqual(_parse_valor_boleto_seguro('1.900'), 1900.0)
        self.assertEqual(_parse_valor_boleto_seguro('0,63'), 0.63)
        self.assertIsNone(_parse_valor_boleto_seguro('abc'))


if __name__ == '__main__':
    unittest.main()
