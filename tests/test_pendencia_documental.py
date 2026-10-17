"""Regras da tag de pendência documental na listagem de vendas."""

from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from services.pendencia_documental import (
    TEXTO_FALTA_AMBOS,
    TEXTO_FALTA_BOLETO,
    TEXTO_FALTA_NF,
    texto_pendencia_documental,
)


def _venda(**kwargs):
    dados = {
        'forma_pagamento': None,
        'empresa_faturadora': 'NENHUM',
        'nf': None,
        'caminho_nf': None,
        'caminho_boleto': None,
        'tipo_operacao': 'VENDA',
    }
    dados.update(kwargs)
    return SimpleNamespace(**dados)


def _pedido(forma='BOLETO', empresa='NENHUM', nf=None, caminho_nf=None,
            caminho_boleto=None, situacao='PENDENTE', vendas=None, **extra):
    if vendas is None:
        vendas = [_venda(
            forma_pagamento=forma,
            empresa_faturadora=empresa,
            nf=nf,
            caminho_nf=caminho_nf,
            caminho_boleto=caminho_boleto,
        )]
    pedido = {
        'forma_pagamento': forma,
        'empresa_faturadora': empresa,
        'nf': nf or '-',
        'caminho_nf': caminho_nf,
        'caminho_boleto': caminho_boleto,
        'situacao': situacao,
        'vendas': vendas,
    }
    pedido.update(extra)
    return pedido


def test_boleto_sem_nf_e_sem_boleto_mostra_os_dois():
    assert texto_pendencia_documental(_pedido()) == TEXTO_FALTA_AMBOS


def test_boleto_com_arquivo_de_boleto_e_sem_nf_mostra_falta_nf():
    pedido = _pedido(caminho_boleto='boletos/jns.pdf')
    assert texto_pendencia_documental(pedido) == TEXTO_FALTA_NF


def test_boleto_com_numero_de_nf_e_sem_arquivo_de_boleto_mostra_falta_boleto():
    pedido = _pedido(nf='45821')
    assert texto_pendencia_documental(pedido) == TEXTO_FALTA_BOLETO


def test_boleto_com_pdf_de_nf_e_boleto_nao_mostra_tag():
    pedido = _pedido(caminho_nf='nfs/45821.pdf', caminho_boleto='boletos/jns.pdf')
    assert texto_pendencia_documental(pedido) is None


def test_numero_e_arquivos_vinculados_ocultam_a_tag():
    pedido = _pedido(nf='45821', caminho_nf='nfs/45821.pdf', caminho_boleto='boletos/jns.pdf')
    assert texto_pendencia_documental(pedido) is None


def test_dinheiro_pix_cheque_e_transferencia_nao_exigem_documentos():
    for forma in ('DINHEIRO', 'PIX', 'pix', 'CHEQUE', 'TRANSFERENCIA'):
        pedido = _pedido(forma=forma, empresa='PATY')
        assert texto_pendencia_documental(pedido, cliente_tem_historico_nf=True) is None, forma


def test_paty_ou_destak_sem_forma_e_sem_docs_mostra_a_tag():
    assert texto_pendencia_documental(_pedido(forma=None, empresa='PATY')) == TEXTO_FALTA_AMBOS
    assert texto_pendencia_documental(_pedido(forma='', empresa='destak')) == TEXTO_FALTA_AMBOS


def test_paty_com_boleto_vinculado_mostra_so_falta_nf():
    pedido = _pedido(forma=None, empresa='PATY', caminho_boleto='boletos/paty.pdf')
    assert texto_pendencia_documental(pedido) == TEXTO_FALTA_NF


def test_historico_de_nf_sem_forma_isenta_mostra_a_tag():
    pedido = _pedido(forma=None, empresa='NENHUM')
    assert texto_pendencia_documental(pedido, cliente_tem_historico_nf=True) == TEXTO_FALTA_AMBOS


def test_sem_boleto_sem_empresa_faturadora_e_sem_historico_nao_mostra_tag():
    pedido = _pedido(forma=None, empresa='NENHUM')
    assert texto_pendencia_documental(pedido, cliente_tem_historico_nf=False) is None
    pedido_dinheiro = _pedido(forma='DINHEIRO', empresa='NENHUM')
    assert texto_pendencia_documental(pedido_dinheiro) is None


def test_perda_nao_recebe_tag_mesmo_com_boleto():
    assert texto_pendencia_documental(_pedido(situacao='PERDA')) is None
    vendas = [_venda(forma_pagamento='BOLETO', tipo_operacao='PERDA')]
    pedido = _pedido(vendas=vendas, situacao='PENDENTE')
    assert texto_pendencia_documental(pedido) is None


def test_linha_boleto_junto_de_pix_ainda_exige_documentos():
    vendas = [
        _venda(forma_pagamento='PIX', empresa_faturadora='NENHUM'),
        _venda(forma_pagamento='BOLETO', empresa_faturadora='NENHUM'),
    ]
    pedido = _pedido(forma='PIX', vendas=vendas)
    assert texto_pendencia_documental(pedido) == TEXTO_FALTA_AMBOS


def test_caminhos_em_branco_contam_como_ausentes():
    pedido = _pedido(nf='  ', caminho_nf='   ', caminho_boleto='  ')
    assert texto_pendencia_documental(pedido) == TEXTO_FALTA_AMBOS


def test_arquivo_so_na_linha_do_pedido_encerra_a_pendencia():
    vendas = [_venda(forma_pagamento='BOLETO', caminho_nf='nfs/a.pdf', caminho_boleto='boletos/a.pdf')]
    pedido = _pedido(nf=None, caminho_nf=None, caminho_boleto=None, vendas=vendas)
    assert texto_pendencia_documental(pedido) is None


def test_template_da_tag_usa_o_estilo_amber_e_some_sem_pendencia():
    env = Environment(loader=FileSystemLoader('templates'), autoescape=True)
    template = env.get_template('_badge_pendencia_documental.html')

    html = template.render(pedido={'pendencia_documental': 'Faltando NF e Boleto'})
    assert 'Faltando NF e Boleto' in html
    assert 'bg-amber-100' in html
    assert 'text-amber-800' in html
    assert 'border-amber-300' in html
    assert 'rounded-full' in html
    assert 'font-medium' in html
    assert 'shadow-sm' in html

    vazio = template.render(pedido={'pendencia_documental': None})
    assert vazio.strip() == ''

    falta_nf = template.render(pedido={'pendencia_documental': 'Falta NF'})
    assert 'Falta NF' in falta_nf
    assert 'Faltando NF e Boleto' not in falta_nf
