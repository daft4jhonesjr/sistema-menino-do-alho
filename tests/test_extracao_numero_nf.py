"""Testes da extração/normalização do número da NF (DANFE pontuado)."""
from __future__ import annotations

import os
import unittest

os.environ.setdefault('SKIP_DB_BOOTSTRAP', '1')

from app import (  # noqa: E402
    _extrair_nf_da_chave_acesso,
    _extrair_numero_nf,
    _limpar_digitos_nf,
    _normalizar_nf,
    _normalizar_numero_nf,
)


class TestLimparDigitosNf(unittest.TestCase):
    def test_pontuado_com_zeros(self):
        self.assertEqual(_limpar_digitos_nf('000.000.244'), '244')

    def test_somente_zeros_retorna_none(self):
        self.assertIsNone(_limpar_digitos_nf('000'))
        self.assertIsNone(_limpar_digitos_nf('0'))
        self.assertIsNone(_limpar_digitos_nf('000.000.000'))

    def test_com_espacos(self):
        self.assertEqual(_limpar_digitos_nf('000 000 244'), '244')


class TestExtrairNumeroNfDanfe(unittest.TestCase):
    TEXTO_COMERCIAL_MF = """
    DANFE Documento Auxiliar da Nota Fiscal Eletrônica
    NF-e
    N° 000.000.244
    Série 001
    COMERCIAL MF LTDA
    """

    def test_n_grau_pontuado_vira_244(self):
        self.assertEqual(_extrair_numero_nf(self.TEXTO_COMERCIAL_MF), '244')

    def test_n_ordinal_pontuado(self):
        texto = "NF-e\nNº 000.000.244\nSérie 001"
        self.assertEqual(_extrair_numero_nf(texto), '244')

    def test_normalizar_numero_nf_alias(self):
        self.assertEqual(
            _normalizar_numero_nf("N° 000.000.244"),
            '244',
        )

    def test_nunca_retorna_000(self):
        # Texto ambíguo que o parser antigo capturava como '000'
        texto = "N° 000.000.244"
        nf = _extrair_numero_nf(texto)
        self.assertNotEqual(nf, '000')
        self.assertNotEqual(nf, '0')
        self.assertEqual(nf, '244')

    def test_nf_sem_pontos_mantem_significativos(self):
        texto = "Núm. do documento: 12244"
        self.assertEqual(_extrair_numero_nf(texto), '12244')

    def test_vinculo_automatico_aceita_244(self):
        """Após normalização, '244' não cai na lista de NFs inválidas."""
        nf_limpa = _normalizar_nf(_extrair_numero_nf(self.TEXTO_COMERCIAL_MF))
        nfs_invalidas = ('S/N', '0', 'Falta_nota', '')
        self.assertNotIn(nf_limpa, nfs_invalidas)
        self.assertEqual(nf_limpa, '244')


class TestChaveAcesso(unittest.TestCase):
    def test_extrai_nnf_das_posicoes_26_a_34(self):
        # 44 dígitos: nNF = posições 26-34 → 000000244
        chave = (
            '26'          # cUF
            + '2601'      # AAMM
            + '12345678000190'  # CNPJ 14
            + '55'        # mod
            + '001'       # série
            + '000000244'  # nNF (9)
            + '1'         # tpEmis
            + '12345678'  # cNF
            + '9'         # cDV
        )
        self.assertEqual(len(chave), 44)
        self.assertEqual(_extrair_nf_da_chave_acesso(chave), '244')

    def test_fallback_chave_no_texto(self):
        chave = (
            '26260112345678000190550010000002441123456789'
        )
        self.assertEqual(len(chave), 44)
        texto = f"CHAVE DE ACESSO\n{chave}\n"
        # Sem bloco N°, usa chave
        self.assertEqual(_extrair_numero_nf(texto), '244')


if __name__ == '__main__':
    unittest.main()
