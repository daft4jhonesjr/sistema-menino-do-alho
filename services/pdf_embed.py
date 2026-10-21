"""Visualização embutida de PDF no modal do sistema.

O navegador só desenha o PDF dentro do iframe quando a resposta é
same-origin, com ``Content-Type: application/pdf`` e
``Content-Disposition: inline``. Arquivos ``raw`` do Cloudinary chegam
com ``Content-Disposition: attachment`` e o iframe fica em branco ou
dispara download. Estas funções baixam o binário no servidor para a
rota reentregar o PDF inline.

Somente HTTPS de ``res.cloudinary.com`` é aceito, inclusive após
redirecionamento, para o proxy não virar um canal de SSRF.
"""

from __future__ import annotations

import html
import os
import re
import urllib.parse
import urllib.request

HOSTS_PDF_REMOTOS = frozenset({'res.cloudinary.com'})
LIMITE_PDF_EMBED = 20 * 1024 * 1024


class PdfEmbedError(Exception):
    def __init__(self, mensagem, status=502):
        super().__init__(mensagem)
        self.mensagem = mensagem
        self.status = int(status)


def quer_visualizacao_embutida(embed_arg, sec_fetch_dest):
    """True quando o pedido vem do iframe do modal."""
    if str(embed_arg or '').strip() == '1':
        return True
    return str(sec_fetch_dest or '').strip().lower() == 'iframe'


def nome_pdf_seguro(nome, fallback='documento.pdf'):
    bruto = str(nome or '').split('?', 1)[0]
    base = os.path.basename(bruto.replace('\\', '/').strip()) or fallback
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('._') or fallback
    if not base.lower().endswith('.pdf'):
        base += '.pdf'
    return base[:120]


def nome_pdf_de_url(url, fallback='documento.pdf'):
    path = urllib.parse.urlsplit(str(url or '')).path
    return nome_pdf_seguro(os.path.basename(path), fallback)


def url_remota_confiavel(url):
    partes = urllib.parse.urlsplit(str(url or '').strip())
    host = (partes.hostname or '').lower().rstrip('.')
    if partes.username or partes.password:
        return False
    return partes.scheme == 'https' and host in HOSTS_PDF_REMOTOS


def html_embed_erro(mensagem, status=404):
    texto = html.escape(mensagem or 'Documento indisponível.')
    corpo = (
        '<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Documento indisponível</title></head>'
        '<body style="margin:0;background:#030712;color:#e5e7eb;'
        'font-family:Inter,system-ui,sans-serif;display:flex;align-items:center;'
        'justify-content:center;min-height:100vh;padding:24px;box-sizing:border-box;">'
        f'<p style="max-width:28rem;text-align:center;line-height:1.5;margin:0;">{texto}</p>'
        '</body></html>'
    )
    return corpo, int(status), {'Content-Type': 'text/html; charset=utf-8'}


class _RedirectSeguro(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not url_remota_confiavel(newurl):
            raise PdfEmbedError('Redirecionamento de arquivo bloqueado.', 415)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener_seguro():
    return urllib.request.build_opener(_RedirectSeguro)


def baixar_pdf_remoto(url, timeout, limite_bytes=LIMITE_PDF_EMBED, abridor=None):
    """Baixa um PDF remoto permitido. ``abridor`` existe para os testes."""
    if not url_remota_confiavel(url):
        raise PdfEmbedError(
            'Este arquivo não pode ser embutido. Use o botão de abrir em nova aba.',
            415,
        )
    limite = int(limite_bytes)
    if abridor is None:
        opener = _opener_seguro()

        def abridor(req, timeout=None):
            return opener.open(req, timeout=timeout)

    req = urllib.request.Request(url, headers={'User-Agent': 'MeninoDoAlhoPDF/1.0'})
    try:
        remoto = abridor(req, timeout=timeout)
    except PdfEmbedError:
        raise
    except Exception as exc:
        raise PdfEmbedError(
            'Não foi possível carregar o documento para visualização.',
            502,
        ) from exc

    try:
        dados = remoto.read(limite + 1)
    except PdfEmbedError:
        raise
    except Exception as exc:
        raise PdfEmbedError(
            'Não foi possível carregar o documento para visualização.',
            502,
        ) from exc
    finally:
        close = getattr(remoto, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    if not dados:
        raise PdfEmbedError('O arquivo do documento está vazio.', 404)
    if len(dados) > limite:
        raise PdfEmbedError('O arquivo é grande demais para visualização embutida.', 413)
    if not dados.startswith(b'%PDF'):
        raise PdfEmbedError('O arquivo recebido não é um PDF válido.', 415)
    return dados
