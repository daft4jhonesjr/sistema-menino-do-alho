"""Modal de PDF: gatilhos da listagem e proxy inline seguro."""
from __future__ import annotations

import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from services.pdf_embed import (
    LIMITE_PDF_EMBED,
    PdfEmbedError,
    _RedirectSeguro,
    baixar_pdf_remoto,
    html_embed_erro,
    nome_pdf_de_url,
    nome_pdf_seguro,
    quer_visualizacao_embutida,
    url_remota_confiavel,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / 'templates'


def _ler(relativo):
    return (TEMPLATES / relativo).read_text(encoding='utf-8')


def _ancoras(texto):
    return re.findall(r'<a\b[^>]*>', texto, flags=re.I | re.S)


class _Onclick(HTMLParser):
    def __init__(self):
        super().__init__()
        self.onclick = None

    def handle_starttag(self, tag, attrs):
        self.onclick = dict(attrs).get('onclick')


def test_nome_pdf_rejeita_travessia_e_aspas():
    assert nome_pdf_seguro('../../etc/passwd') == 'passwd.pdf'
    assert nome_pdf_seguro('nota "fiscal".pdf') == 'nota_fiscal_.pdf'
    assert nome_pdf_seguro('boleto.pdf?token=1') == 'boleto.pdf'
    assert nome_pdf_de_url(
        'https://res.cloudinary.com/demo/raw/upload/v1/nf-10.pdf'
    ) == 'nf-10.pdf'


def test_url_remota_so_aceita_cloudinary_https():
    ok = 'https://res.cloudinary.com/demo/raw/upload/v1/boleto.pdf'
    assert url_remota_confiavel(ok)
    assert not url_remota_confiavel('http://res.cloudinary.com/demo/raw/upload/a.pdf')
    assert not url_remota_confiavel('https://evil.example/boleto.pdf')
    assert not url_remota_confiavel('https://res.cloudinary.com.evil.test/a.pdf')
    assert not url_remota_confiavel('https://user:senha@res.cloudinary.com/a.pdf')
    assert not url_remota_confiavel('file:///tmp/segredo.pdf')
    assert not url_remota_confiavel('')


def test_redirect_para_host_estranho_e_bloqueado():
    try:
        _RedirectSeguro().redirect_request(
            None, None, 302, 'Found', {}, 'https://127.0.0.1/interno.pdf'
        )
    except PdfEmbedError as exc:
        assert exc.status == 415
    else:
        raise AssertionError('redirecionamento interno deveria ser bloqueado')


def test_embed_reconhece_query_e_iframe():
    assert quer_visualizacao_embutida('1', None)
    assert quer_visualizacao_embutida(None, 'iframe')
    assert not quer_visualizacao_embutida(None, 'document')
    assert not quer_visualizacao_embutida('', '')


def test_html_de_erro_escapa_markup():
    corpo, status, headers = html_embed_erro('<script>alert(1)</script>', 404)
    assert status == 404
    assert headers['Content-Type'].startswith('text/html')
    assert '<script>' not in corpo
    assert '&lt;script&gt;' in corpo


class _Resp:
    def __init__(self, dados):
        self._dados = dados

    def read(self, n=-1):
        if n is None or n < 0:
            dados, self._dados = self._dados, b''
            return dados
        dados, self._dados = self._dados[:n], self._dados[n:]
        return dados

    def close(self):
        return None


def test_download_remoto_exige_assinatura_pdf_e_limite():
    url = 'https://res.cloudinary.com/demo/raw/upload/v1/nf.pdf'

    def abre_pdf(req, timeout=None):
        assert req.full_url == url
        return _Resp(b'%PDF-1.4 conteudo')

    assert baixar_pdf_remoto(url, timeout=3, abridor=abre_pdf).startswith(b'%PDF')

    def abre_html(req, timeout=None):
        return _Resp(b'<html>nao pdf</html>')

    try:
        baixar_pdf_remoto(url, timeout=3, abridor=abre_html)
    except PdfEmbedError as exc:
        assert exc.status == 415
    else:
        raise AssertionError('HTML não pode ser servido como PDF')

    def abre_grande(req, timeout=None):
        return _Resp(b'%PDF' + b'x' * (LIMITE_PDF_EMBED + 5))

    try:
        baixar_pdf_remoto(url, timeout=3, limite_bytes=8, abridor=abre_grande)
    except PdfEmbedError as exc:
        assert exc.status == 413
    else:
        raise AssertionError('PDF acima do limite deveria falhar')

    try:
        baixar_pdf_remoto('https://evil.example/a.pdf', timeout=3, abridor=abre_pdf)
    except PdfEmbedError as exc:
        assert exc.status == 415
    else:
        raise AssertionError('host fora da allowlist deveria falhar')


def test_templates_do_viewer_compilam():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    for nome in (
        'base.html',
        '_linhas_venda.html',
        '_cards_venda.html',
        'vendas/listar.html',
        'gerenciar_arquivos.html',
    ):
        env.parse(env.loader.get_source(env, nome)[0])


def test_ancoras_de_documento_abrem_o_modal():
    for relativo in ('_linhas_venda.html', '_cards_venda.html'):
        tags = _ancoras(_ler(relativo))
        nf = [t for t in tags if '_url_nf' in t and 'abrirViewerPDF' in t]
        boleto = [t for t in tags if '_url_boleto' in t and 'abrirViewerPDF' in t]
        assert len(nf) == 1, relativo
        assert len(boleto) == 1, relativo
        for tag in nf + boleto:
            assert 'target="_blank"' not in tag
            assert '_blank' not in tag
        recibos = [t for t in tags if 'recibo_venda' in t]
        assert recibos and all('target="_blank"' in t for t in recibos)

    lista = _ler('vendas/listar.html')
    vis = [t for t in _ancoras(lista) if 'abrirViewerPDF' in t and '_url_vis' in t]
    assert len(vis) == 1 and 'target="_blank"' not in vis[0]
    debug = [t for t in _ancoras(lista) if 'debug_texto_arquivo' in t]
    assert debug and 'target="_blank"' in debug[0]

    idx = lista.find('if (item.arquivo_boleto)')
    trecho = lista[idx:idx + 1200]
    assert 'abrirViewerPDF' in trecho
    assert 'target="_blank"' not in trecho
    assert "JSON.stringify(item.arquivo_boleto)" in trecho

    arquivos = _ler('gerenciar_arquivos.html')
    tags = _ancoras(arquivos)
    viewer = [t for t in tags if 'abrirViewerPDF' in t]
    assert len(viewer) == 1
    assert 'target="_blank"' not in arquivos

    base = _ler('base.html')
    assert 'id="modal-viewer-pdf"' in base
    assert 'id="viewer-pdf-frame"' in base
    assert 'id="viewer-pdf-titulo"' in base
    assert 'fecharViewerPDF()' in base
    assert 'window.abrirViewerPDF' in base


def _executar_onclick(onclick):
    script = (
        'const event = {metaKey:false, ctrlKey:false, shiftKey:false, altKey:false, '
        'preventDefault(){ this.evitou = true; }, stopPropagation(){}};\n'
        'let recebido;\n'
        'function abrirViewerPDF(url, titulo){ recebido = [url, titulo]; }\n'
        'function disparar(){\n'
        + onclick
        + '\n}\ndisparar();\nprocess.stdout.write(JSON.stringify(recebido));'
    )
    saida = subprocess.check_output(['node', '-e', script], text=True)
    return json.loads(saida)


def test_botao_do_calendario_escapa_titulo_com_aspas():
    def esc(valor):
        return (
            str(valor)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;')
            .replace('"', '&quot;')
            .replace("'", '&#39;')
        )

    url = '/venda/4/ver_boleto'
    titulo = 'Boleto - Padaria "O Alho" (NF 8)'
    html = (
        '<button type="button" onclick="event.preventDefault(); event.stopPropagation(); '
        'abrirViewerPDF('
        + esc(json.dumps(url))
        + ', '
        + esc(json.dumps(titulo, ensure_ascii=False))
        + ')"></button>'
    )
    parser = _Onclick()
    parser.feed(html)
    assert _executar_onclick(parser.onclick) == [url, titulo]


def test_onclick_com_aspas_no_titulo_continua_javascript_valido():
    env = Environment(autoescape=True)
    html = env.from_string(
        '<a href="{{ url }}" onclick="if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return true; '
        'event.preventDefault(); abrirViewerPDF({{ url|tojson|forceescape }}, {{ titulo|tojson|forceescape }});">'
    ).render(url="/venda/7/ver_boleto", titulo="Boleto - D'Avila & Filhos <NF> (NF 12)")
    parser = _Onclick()
    parser.feed(html)
    assert parser.onclick
    assert 'target=' not in html
    assert _executar_onclick(parser.onclick) == [
        '/venda/7/ver_boleto',
        "Boleto - D'Avila & Filhos <NF> (NF 12)",
    ]


def test_script_do_modal_abre_e_fecha_sem_navegar():
    base = _ler('base.html')
    inicio = base.find('<script id="script-viewer-pdf">')
    fim = base.find('</script>', inicio)
    assert inicio != -1 and fim != -1
    script = base[inicio:fim]
    script = script.split('>', 1)[1]
    harness = r"""
const fs = require('fs');
const codigo = fs.readFileSync(process.argv[1], 'utf8');

function Lista(inicial) {
  this._s = new Set(inicial || []);
}
Lista.prototype.add = function(c) { this._s.add(c); };
Lista.prototype.remove = function(c) { this._s.delete(c); };
Lista.prototype.contains = function(c) { return this._s.has(c); };
Lista.prototype.toggle = function(c, on) { if (on) this._s.add(c); else this._s.delete(c); };

function El(id, classes) {
  this.id = id;
  this.classList = new Lista(classes);
  this.attrs = {};
  this._src = 'about:blank';
  this.onload = null;
  this.focused = false;
}
El.prototype.setAttribute = function(k, v) { this.attrs[k] = v; if (k === 'src') this._src = v; };
El.prototype.getAttribute = function(k) { return k === 'src' ? this._src : (this.attrs[k] || null); };
Object.defineProperty(El.prototype, 'src', {
  get() { return this._src; },
  set(v) { this._src = v; this.attrs.src = v; }
});
El.prototype.focus = function() { this.focused = true; };
El.prototype.querySelector = function(sel) {
  if (sel === 'button[aria-label="Fechar"]') return this.fechar || null;
  return null;
};

const modal = new El('modal-viewer-pdf', ['hidden', 'flex']);
modal.fechar = new El('fechar', []);
const frame = new El('viewer-pdf-frame', []);
frame.src = 'about:blank';
const titulo = new El('viewer-pdf-titulo', []);
titulo.innerText = '';
const novaAba = new El('viewer-pdf-nova-aba', []);
novaAba.href = '#';
const loader = new El('viewer-pdf-loader', ['hidden', 'flex']);
const icone = new El('viewer-pdf-icone', ['text-blue-400']);
const painel = new El('viewer-pdf-painel', ['animate-in']);
const body = new El('body', []);
const mapa = {
  'modal-viewer-pdf': modal,
  'viewer-pdf-frame': frame,
  'viewer-pdf-titulo': titulo,
  'viewer-pdf-nova-aba': novaAba,
  'viewer-pdf-loader': loader,
  'viewer-pdf-icone': icone,
  'viewer-pdf-painel': painel,
};
const teclas = [];
global.window = global;
global.document = {
  getElementById(id) { return mapa[id] || null; },
  addEventListener(tipo, fn) { if (tipo === 'keydown') teclas.push(fn); },
  body: body,
};
global.location = { href: 'http://localhost/vendas', origin: 'http://localhost' };
global.travarFundo = function() { body.classList.add('modal-aberto'); global.travou = true; };
global.liberarFundo = function() { body.classList.remove('modal-aberto'); global.liberou = true; };
global.setTimeout = function(fn, ms) { if (!ms) fn(); return 1; };
global.clearTimeout = function() {};
global.alert = function(msg) { global.alertou = msg; };

eval(codigo);

if (typeof abrirViewerPDF !== 'function' || typeof fecharViewerPDF !== 'function') {
  throw new Error('funcoes do viewer ausentes');
}

abrirViewerPDF('', 'Nota Fiscal');
if (!modal.classList.contains('hidden')) throw new Error('url vazia abriu o modal');
if (!global.alertou) throw new Error('url vazia nao avisou');

abrirViewerPDF('/venda/3/ver_nf', 'Nota Fiscal - NF 99');
if (modal.classList.contains('hidden')) throw new Error('modal continuou oculto');
if (frame.src !== '/venda/3/ver_nf?embed=1') throw new Error('src do iframe: ' + frame.src);
if (novaAba.href !== '/venda/3/ver_nf') throw new Error('nova aba deveria manter a url original');
if (titulo.innerText !== 'Nota Fiscal - NF 99') throw new Error('titulo: ' + titulo.innerText);
if (!body.classList.contains('overflow-hidden')) throw new Error('scroll da pagina nao foi travado');
if (!global.travou) throw new Error('travarFundo nao foi chamado');
if (loader.classList.contains('hidden')) throw new Error('loader sumiu antes do load');
if (!icone.classList.contains('text-blue-400')) throw new Error('icone da NF deveria ser azul');

frame.onload();
if (!loader.classList.contains('hidden')) throw new Error('loader nao saiu depois do load');

abrirViewerPDF('/venda/3/ver_boleto', 'Boleto - Padaria (NF 99)');
if (!icone.classList.contains('text-amber-400')) throw new Error('icone do boleto deveria ser ambar');
if (frame.src !== '/venda/3/ver_boleto?embed=1') throw new Error('boleto src: ' + frame.src);

fecharViewerPDF();
if (!modal.classList.contains('hidden')) throw new Error('modal nao fechou');
if (frame.src !== 'about:blank') throw new Error('iframe nao foi esvaziado');
if (body.classList.contains('overflow-hidden')) throw new Error('overflow-hidden permaneceu');
if (!global.liberou) throw new Error('liberarFundo nao foi chamado');

const antes = frame.src;
teclas[0]({ key: 'Escape' });
if (frame.src !== antes) throw new Error('ESC com modal fechado alterou o iframe');

abrirViewerPDF('https://res.cloudinary.com/demo/raw/upload/v1/nf.pdf', 'Nota Fiscal - NF 1');
if (frame.src !== 'https://res.cloudinary.com/demo/raw/upload/v1/nf.pdf') {
  throw new Error('url externa nao deveria ganhar embed: ' + frame.src);
}
fecharViewerPDF();

abrirViewerPDF('javascript:alert(1)', 'Nota Fiscal');
if (!modal.classList.contains('hidden')) throw new Error('javascript: abriu o modal');

teclas[0]({ key: 'Enter' });
process.stdout.write('ok');
"""
    caminho = ROOT / 'tests' / '_viewer_pdf_extraido.js'
    caminho.write_text(script, encoding='utf-8')
    try:
        saida = subprocess.check_output(
            ['node', '-e', harness, str(caminho)],
            text=True,
        )
    finally:
        caminho.unlink(missing_ok=True)
    assert saida.strip() == 'ok'
