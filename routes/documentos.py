"""Blueprint ``documentos`` — Upload, OCR e gestão de Boletos/Notas Fiscais.

Rotas extraídas do legado ``app.py`` (Fase 3 da refatoração):
    * GET  /documento/visualizar/<id>            visualizar_documento
    * GET  /arquivos/<id>/debug_texto            debug_texto_arquivo  (master)
    * POST /arquivo/<id>/deletar                 deletar_arquivo_dashboard
    * POST /arquivos/deletar_em_massa            deletar_arquivos_massa
    * GET  /venda/<id>/ver_boleto                ver_boleto_venda
    * GET  /venda/<id>/whatsapp                  enviar_whatsapp_boleto
    * GET  /venda/<id>/ver_nf                    ver_nf_venda
    * POST /upload                               upload_documento     (csrf-exempt + token)
    * POST /api/receber_automatico               api_receber_automatico (público + token)
    * POST /api/bot/upload                       api_bot_upload       (público + token)
    * POST /processar_documentos                 processar_documentos
    * POST /reprocessar_boletos                  reprocessar_boletos
    * GET  /admin/arquivos                       admin_arquivos
    * POST /arquivos/upload_massa                upload_massa_arquivos
    * POST /admin/arquivos/deletar_massa         admin_arquivos_deletar_massa
    * GET/POST /admin/reprocessar-vencimentos    admin_reprocessar_vencimentos (master)
    * POST /documento/<id>/vincular              vincular_documento_venda
    * GET  /admin/raio_x                         raio_x               (master)
    * POST /admin/resgatar_orfaos                resgatar_orfaos      (master)
    * POST /admin/forcar_leitura_pasta           forcar_leitura_pasta (master)
    * POST /admin/limpar_fantasmas               limpar_fantasmas     (master)
    * POST /admin/limpar_vinculos_quebrados      limpar_vinculos_quebrados (master)
    * POST /debug/testar_log                     debug_testar_log     (master)
    * GET  /debug-vincular                       debug_vincular       (admin)

Endpoints novos: prefixo ``documentos.`` (ex.: ``documentos.upload_documento``).

Proteção automática de tenant
-----------------------------
Toda rota deste blueprint exige ``login_required`` + ``tenant_required``,
EXCETO as rotas públicas marcadas explicitamente no ``before_request``:
    * ``api_receber_automatico`` — token em Authorization (bot externo)
    * ``api_bot_upload``         — token em X-API-KEY (bot externo)

Auditoria P0: TODAS as queries usam ``query_documentos_tenant()`` ou
``query_tenant(Venda)``, garantindo isolamento cross-tenant. Mantemos o
``@master_required`` nas rotas administrativas que tocam dados de outros
tenants (debug de OCR, raio-x, limpeza global).
"""
from datetime import date, datetime
import html
import io
import os
import re
import traceback
import urllib.parse
import urllib.request

from flask import (
    Blueprint, render_template, request, redirect, url_for,
    flash, jsonify, send_file, current_app,
)
from flask_login import current_user
from flask_wtf.csrf import generate_csrf
from sqlalchemy import or_
from sqlalchemy.orm import joinedload, contains_eager
from werkzeug.utils import secure_filename

import cloudinary  # noqa: F401
import cloudinary.uploader
import pdfplumber

from models import db, Documento, Venda, Cliente, Usuario


documentos_bp = Blueprint('documentos', __name__)


# ============================================================
# Proteção automática de tenant para todo o blueprint
# ============================================================
# Endpoints públicos (autenticados via token, sem sessão):
_ENDPOINTS_PUBLICOS = {
    'documentos.api_receber_automatico',
    'documentos.api_bot_upload',
}


@documentos_bp.before_request
def _exigir_tenant_em_todas_rotas():
    """Equivale a aplicar ``@login_required`` + ``@tenant_required`` em
    todas as rotas, exceto as públicas (token-based).

    Reusa o decorator ``tenant_required`` definido em ``app.py`` para
    centralizar as regras (MASTER → /master-admin, sem empresa_id →
    /login com flash).
    """
    if request.endpoint in _ENDPOINTS_PUBLICOS:
        return None
    from app import tenant_required

    @tenant_required
    def _ok():
        return None

    return _ok()


# ============================================================
# Helpers exclusivos de documentos
# ============================================================

def _extrair_texto_raw_pdfplumber(arquivo_pdf):
    """Extrai texto com a mesma abordagem usada no processamento:
    pdfplumber + crop superior (75%)."""
    texto_completo = ""
    with pdfplumber.open(arquivo_pdf) as pdf:
        for pagina in pdf.pages:
            h = float(pagina.height) or 842
            w = float(pagina.width) or 595
            crop_bottom = max(0, h * 0.75)
            cropped = pagina.crop((0, 0, w, crop_bottom)) if crop_bottom > 0 else pagina
            texto_pagina = cropped.extract_text()
            if texto_pagina:
                texto_completo += texto_pagina + "\n"
    return texto_completo


def _token_upload_required(f):
    """Permite autenticação via token no header Authorization (para robô)."""
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        token_esperado = os.environ.get('API_TOKEN')
        if not token_esperado:
            return jsonify({'mensagem': 'API_TOKEN não configurado no ambiente.'}), 503
        auth = request.headers.get('Authorization', '')
        if auth == token_esperado or auth == f'Bearer {token_esperado}':
            return f(*args, **kwargs)
        return jsonify({'mensagem': 'Token inválido ou ausente.'}), 403
    return decorated


# ============================================================
# Rotas
# ============================================================

@documentos_bp.route('/documento/visualizar/<int:id>')
def visualizar_documento(id):
    """Redireciona para o PDF na nuvem (Cloudinary)."""
    from app import (
        query_documentos_tenant, _usuario_pode_gerenciar_documento,
        _resposta_sem_permissao,
    )
    documento = query_documentos_tenant().filter_by(id=id).first_or_404()
    if not _usuario_pode_gerenciar_documento(documento):
        return _resposta_sem_permissao()
    if documento.url_arquivo:
        return redirect(documento.url_arquivo)
    flash('Link do arquivo não encontrado na nuvem. Faça o upload novamente.', 'error')
    return redirect(request.referrer or url_for('dashboard'))


@documentos_bp.route('/arquivos/<int:id>/debug_texto', methods=['GET'])
def debug_texto_arquivo(id):
    """Raio-X: exibe o texto bruto extraído do PDF para depuração de regex.

    MASTER-only por design: a ferramenta acessa qualquer documento do SaaS
    para debug de OCR/regex; abrir para DONO reabriria a brecha cross-tenant.
    """
    from app import master_required, _EXTERNAL_TIMEOUT

    @master_required
    def _impl():
        documento = Documento.query.get_or_404(id)
        try:
            texto_extraido = ""
            if documento.url_arquivo:
                with urllib.request.urlopen(documento.url_arquivo, timeout=_EXTERNAL_TIMEOUT) as resp:
                    conteudo_pdf = resp.read()
                pdf_buffer = io.BytesIO(conteudo_pdf)
                try:
                    texto_extraido = _extrair_texto_raw_pdfplumber(pdf_buffer)
                finally:
                    pdf_buffer.close()
            else:
                path = (documento.caminho_arquivo or '').strip()
                if not path:
                    return "<html><body><h3>Debug</h3><p>Documento sem URL/Caminho de arquivo.</p></body></html>", 404
                nome_seguro = os.path.basename(path)
                if os.path.isfile(path):
                    caminho_local = path
                else:
                    base_dir = os.path.join(current_app.root_path, 'documentos_entrada')
                    candidatos = [
                        os.path.join(base_dir, 'boletos', nome_seguro),
                        os.path.join(base_dir, 'notas_fiscais', nome_seguro),
                        os.path.join(base_dir, 'bonificacoes', nome_seguro),
                    ]
                    caminho_local = next((c for c in candidatos if os.path.isfile(c)), None)
                if not caminho_local:
                    return "<html><body><h3>Debug</h3><p>Arquivo PDF não encontrado localmente.</p></body></html>", 404
                texto_extraido = _extrair_texto_raw_pdfplumber(caminho_local)

            texto_escapado = html.escape(texto_extraido or "(sem texto extraído)")
            return (
                "<html><body>"
                "<h3>Texto Extraído (Raio-X)</h3>"
                f"<p><strong>Documento ID:</strong> {documento.id}</p>"
                f"<p><strong>Tipo:</strong> {html.escape(str(documento.tipo or '-'))}</p>"
                f"<pre style='white-space: pre-wrap; word-break: break-word;'>{texto_escapado}</pre>"
                "</body></html>"
            )
        except Exception as e:
            return (
                "<html><body>"
                "<h3>Erro no Debug de Texto</h3>"
                f"<pre>{html.escape(str(e))}</pre>"
                "</body></html>"
            ), 500

    return _impl()


@documentos_bp.route('/arquivo/<int:id>/deletar', methods=['POST'])
def deletar_arquivo_dashboard(id):
    """Exclui documento com tolerância a falhas na remoção física."""
    from app import (
        query_documentos_tenant, _usuario_pode_gerenciar_documento,
        _deletar_cloudinary_seguro, limpar_cache_dashboard,
    )
    doc_id = int(id)
    documento = query_documentos_tenant().filter_by(id=doc_id).first_or_404()
    if not _usuario_pode_gerenciar_documento(documento):
        return jsonify(ok=False, mensagem='Acesso negado.'), 403

    try:
        if documento.public_id or documento.url_arquivo:
            _deletar_cloudinary_seguro(
                public_id=documento.public_id,
                url=documento.url_arquivo,
                resource_type='raw',
            )
        caminho_rel = (documento.caminho_arquivo or '').strip()
        if caminho_rel:
            base_path = current_app.root_path
            caminho_abs = os.path.join(base_path, caminho_rel)
            if os.path.isfile(caminho_abs):
                os.remove(caminho_abs)
    except Exception as e:
        current_app.logger.warning(f"Não foi possível excluir fisicamente o documento {doc_id}: {e}")

    try:
        db.session.delete(documento)
        db.session.commit()
        limpar_cache_dashboard()
        return jsonify(ok=True, mensagem='Documento removido.')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao excluir documento {doc_id} do banco: {e}")
        return jsonify(ok=False, mensagem='Erro ao excluir documento no banco de dados.'), 500


@documentos_bp.route('/arquivos/deletar_em_massa', methods=['POST'])
def deletar_arquivos_massa():
    """Exclui múltiplos documentos (Cloudinary + banco) da seção de documentos recentes."""
    from app import (
        query_documentos_tenant, _usuario_pode_gerenciar_documento,
        limpar_cache_dashboard, _EXTERNAL_TIMEOUT,
    )
    data = request.get_json(silent=True) or {}
    ids_para_deletar = data.get('ids', [])

    if not isinstance(ids_para_deletar, list) or not ids_para_deletar:
        return jsonify(ok=False, status='erro', mensagem='Nenhum ID fornecido.'), 400

    try:
        ids = list({int(x) for x in ids_para_deletar if x is not None and str(x).strip()})
    except (TypeError, ValueError):
        return jsonify(ok=False, status='erro', mensagem='IDs inválidos.'), 400

    if not ids:
        return jsonify(ok=False, status='erro', mensagem='Nenhum ID válido informado.'), 400

    documentos = query_documentos_tenant().filter(Documento.id.in_(ids)).all()
    if not documentos:
        return jsonify(ok=False, status='erro', mensagem='Nenhum documento encontrado para exclusão.'), 404
    if any(not _usuario_pode_gerenciar_documento(doc) for doc in documentos):
        return jsonify(ok=False, status='erro', mensagem='Acesso negado para um ou mais documentos.'), 403

    try:
        for documento in documentos:
            if documento.public_id and (os.environ.get('CLOUDINARY_URL') or current_app.config.get('CLOUDINARY_URL')):
                try:
                    cloudinary.uploader.destroy(documento.public_id, resource_type='raw', timeout=_EXTERNAL_TIMEOUT)
                except Exception as ex:
                    current_app.logger.error(f"Erro ao excluir do Cloudinary {documento.public_id}: {ex}")
            db.session.delete(documento)

        db.session.commit()
        limpar_cache_dashboard()
        return jsonify(ok=True, status='sucesso', mensagem='Documentos excluídos com sucesso.', total=len(documentos))
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao excluir documentos em massa: {e}")
        return jsonify(ok=False, status='erro', mensagem='Erro ao excluir documentos em massa.'), 500


@documentos_bp.route('/venda/<int:id>/ver_boleto')
def ver_boleto_venda(id):
    """Abre o PDF do boleto vinculado ao pedido em nova aba.
    Prioriza URL Cloudinary; fallback para arquivo local com guarda contra path traversal."""
    from app import (
        query_tenant, query_documentos_tenant, _resolver_caminho_documento_seguro,
    )
    venda = query_tenant(Venda).filter_by(id=id).first_or_404()
    path = (venda.caminho_boleto or '').strip()
    if not path:
        flash('Boleto não vinculado a este pedido.', 'error')
        return redirect(url_for('vendas.listar_vendas'))
    doc = query_documentos_tenant().filter(
        or_(Documento.caminho_arquivo == path, Documento.url_arquivo == path)
    ).first()
    if doc and doc.url_arquivo:
        return redirect(doc.url_arquivo)
    nome_seguro = os.path.basename(path)
    full = _resolver_caminho_documento_seguro('boletos', nome_seguro)
    if not full:
        return "Forbidden", 403
    if not os.path.isfile(full):
        flash('Arquivo do boleto não encontrado no servidor.', 'error')
        return redirect(request.referrer or url_for('vendas.listar_vendas'))
    return send_file(full, mimetype='application/pdf')


@documentos_bp.route('/venda/<int:id>/whatsapp')
def enviar_whatsapp_boleto(id):
    """Redireciona para o WhatsApp com mensagem de cobrança e link do boleto."""
    from app import query_tenant
    venda = query_tenant(Venda).options(joinedload(Venda.cliente)).filter_by(id=id).first_or_404()
    if not (venda.caminho_boleto or '').strip():
        flash('Boleto não vinculado a este pedido. Vincule um boleto antes de enviar cobrança.', 'error')
        return redirect(request.referrer or url_for('vendas.listar_vendas'))
    if not venda.cliente:
        flash('Cliente não encontrado para esta venda.', 'error')
        return redirect(request.referrer or url_for('vendas.listar_vendas'))
    telefone = getattr(venda.cliente, 'telefone', None) or ''
    telefone = (telefone or '').strip()
    if not telefone:
        flash('Este cliente não possui um telefone cadastrado.', 'error')
        return redirect(request.referrer or url_for('vendas.listar_vendas'))
    telefone_limpo = re.sub(r'\D', '', telefone)
    if len(telefone_limpo) <= 11:
        telefone_limpo = '55' + telefone_limpo
    link_boleto = url_for('documentos.ver_boleto_venda', id=venda.id, _external=True)
    vendas_pedido = query_tenant(Venda).filter_by(
        cliente_id=venda.cliente_id,
        data_venda=venda.data_venda,
        nf=venda.nf,
    ).all()
    valor_total = sum(float(v.calcular_total()) for v in vendas_pedido)
    mensagem = (
        f"Olá, tudo bem? 🧄\n\n"
        f"Segue o link do seu boleto referente à NF {venda.nf or 'S/N'} "
        f"no valor de R$ {valor_total:,.2f}.\n\n"
        f"📄 Acesse ou baixe seu boleto aqui:\n{link_boleto}\n\n"
        f"Qualquer dúvida, estamos à disposição!"
    )
    mensagem_codificada = urllib.parse.quote(mensagem)
    url_whatsapp = f"https://wa.me/{telefone_limpo}?text={mensagem_codificada}"
    return redirect(url_whatsapp)


@documentos_bp.route('/venda/<int:id>/ver_nf')
def ver_nf_venda(id):
    """Abre o PDF da nota fiscal vinculada ao pedido em nova aba."""
    from app import (
        query_tenant, query_documentos_tenant, _resolver_caminho_documento_seguro,
    )
    venda = query_tenant(Venda).filter_by(id=id).first_or_404()
    path = (venda.caminho_nf or '').strip()
    if not path:
        flash('Nota fiscal não vinculada a este pedido.', 'error')
        return redirect(url_for('vendas.listar_vendas'))

    doc = query_documentos_tenant().filter(
        or_(Documento.caminho_arquivo == path, Documento.url_arquivo == path)
    ).first()
    if not doc:
        venda.caminho_nf = None
        db.session.commit()
        flash('Nota fiscal não encontrada no banco de dados. Vínculo removido.', 'error')
        return redirect(url_for('vendas.listar_vendas'))
    if doc.url_arquivo:
        return redirect(doc.url_arquivo)
    nome_seguro = os.path.basename(path)
    full = _resolver_caminho_documento_seguro('notas_fiscais', nome_seguro)
    if not full:
        return "Forbidden", 403
    if not os.path.isfile(full):
        flash('Arquivo da nota fiscal não encontrado no servidor.', 'error')
        return redirect(request.referrer or url_for('vendas.listar_vendas'))
    return send_file(full, mimetype='application/pdf')


@documentos_bp.route('/upload', methods=['POST'])
@_token_upload_required
def upload_documento():
    """
    Rota para o bot enviar arquivos. Salva na sala de espera (documentos_entrada).
    O upload para Cloudinary e criação do Documento ocorrem em
    ``_processar_documentos_pendentes`` (Organizar).
    Campo ``tipo``: 'boleto' -> boletos ; 'nfe' -> notas_fiscais
    """
    from app import (
        _processar_documento, _empresa_id_para_documento,
        limpar_cache_dashboard,
    )
    # CSRF está exempt para esta rota — exemption aplicada no app.py após
    # ``register_blueprint(documentos_bp)`` via ``csrf.exempt(view_func)``.
    # Bot externo se autentica via header Authorization (token).

    arquivo = request.files.get('file') or request.files.get('arquivo') or request.files.get('documento')
    if not arquivo or not arquivo.filename:
        return jsonify({'mensagem': 'Nenhum arquivo enviado.'}), 400
    nome_arquivo = secure_filename(arquivo.filename or '')
    extensao = os.path.splitext(nome_arquivo)[1].lower()
    extensoes_permitidas = {'.pdf', '.png', '.jpg', '.jpeg'}
    mimes_permitidos = {'application/pdf', 'image/png', 'image/jpeg'}
    mime = (getattr(arquivo, 'mimetype', '') or '').lower()
    if extensao not in extensoes_permitidas:
        return jsonify({'mensagem': 'Extensão de arquivo não permitida.'}), 400
    if mime not in mimes_permitidos:
        return jsonify({'mensagem': 'Tipo MIME inválido para upload.'}), 400

    tipo = (request.form.get('tipo') or request.form.get('type') or '').strip().lower()
    if tipo == 'boleto':
        subpasta = 'boletos'
    elif tipo == 'nfe':
        subpasta = 'notas_fiscais'
    else:
        return jsonify({'mensagem': "Campo 'tipo' inválido. Use 'boleto' ou 'nfe'."}), 400

    try:
        base_dir = os.path.join(current_app.root_path, 'documentos_entrada')
        caminho_final = os.path.join(base_dir, subpasta)
        os.makedirs(caminho_final, exist_ok=True)
        caminho_completo = os.path.join(caminho_final, nome_arquivo)
        arquivo.save(caminho_completo)

        uid = current_user.id if current_user.is_authenticated else None
        _processar_documento(caminho_completo, user_id_forcado=uid)
        limpar_cache_dashboard()
        caminho_relativo = os.path.join('documentos_entrada', subpasta, nome_arquivo).replace('\\', '/')
        doc_criado = Documento.query.filter_by(caminho_arquivo=caminho_relativo).order_by(Documento.id.desc()).first()
        if not doc_criado:
            tipo_doc = 'BOLETO' if subpasta == 'boletos' else 'NOTA_FISCAL'
            doc_criado = Documento(
                caminho_arquivo=caminho_relativo,
                tipo=tipo_doc,
                usuario_id=uid,
                empresa_id=_empresa_id_para_documento(fallback_user_id=uid),
                venda_id=None,
                data_processamento=date.today(),
            )
            db.session.add(doc_criado)
            db.session.commit()
            limpar_cache_dashboard()
        return jsonify({'mensagem': 'Sucesso'}), 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao guardar ficheiro em documentos_entrada/{subpasta}: {e}")
        return jsonify({'mensagem': str(e)}), 500


@documentos_bp.route('/api/receber_automatico', methods=['POST'])
def api_receber_automatico():
    """API para receber arquivos automaticamente. Requer token em Authorization.

    Endpoint público (token-based). O ``before_request`` deste blueprint
    está configurado para NÃO exigir login_required nesta rota.
    """
    from app import (
        limiter, _empresa_id_para_documento, limpar_cache_dashboard,
        _EXTERNAL_TIMEOUT,
    )

    @limiter.limit("10 per minute")
    def _impl():
        token_esperado = os.environ.get('API_RECEBER_AUTOMATICO_TOKEN')
        if not token_esperado:
            return jsonify({'status': 'erro', 'mensagem': 'API_RECEBER_AUTOMATICO_TOKEN não configurado no ambiente.'}), 503
        auth = request.headers.get('Authorization', '')
        if auth != token_esperado and auth != f'Bearer {token_esperado}':
            return jsonify({'status': 'erro', 'mensagem': 'Token inválido ou ausente.'}), 403

        arquivo = request.files.get('file') or request.files.get('arquivo') or request.files.get('documento')
        if not arquivo or not arquivo.filename:
            return jsonify({'status': 'erro', 'mensagem': 'Arquivo vazio ou inexistente.'}), 400

        try:
            filename = secure_filename(arquivo.filename)
            if not filename:
                return jsonify({'status': 'erro', 'mensagem': 'Nome de arquivo inválido.'}), 400

            extensao = os.path.splitext(filename)[1].lower()
            if extensao not in {'.pdf', '.png', '.jpg', '.jpeg'}:
                return jsonify({'status': 'erro', 'mensagem': 'Extensão de arquivo não permitida.'}), 400

            tipo_bruto = (request.form.get('tipo') or request.form.get('type') or '').strip().lower()
            if tipo_bruto in ('boleto', 'boletos'):
                tipo_documento = 'BOLETO'
            elif tipo_bruto in ('nfe', 'nf', 'nota_fiscal', 'nota fiscal', 'notas_fiscais'):
                tipo_documento = 'NOTA_FISCAL'
            else:
                tipo_documento = 'NOTA_FISCAL' if ('nfe' in filename.lower() or 'nota' in filename.lower()) else 'BOLETO'

            user_id = None
            if current_user.is_authenticated:
                user_id = current_user.id
            else:
                primeiro_user = Usuario.query.first()
                if primeiro_user:
                    user_id = primeiro_user.id

            url_arquivo = None
            public_id = None
            if os.environ.get('CLOUDINARY_URL') or current_app.config.get('CLOUDINARY_URL'):
                try:
                    arquivo.stream.seek(0)
                    resultado_nuvem = cloudinary.uploader.upload(arquivo, resource_type='raw', timeout=_EXTERNAL_TIMEOUT)
                    url_arquivo = resultado_nuvem.get('secure_url')
                    public_id = resultado_nuvem.get('public_id')
                except Exception as e:
                    return jsonify({'status': 'erro', 'mensagem': f'Falha no upload Cloudinary: {str(e)}'}), 500

            caminho_relativo = None
            if not url_arquivo:
                pasta = current_app.config['UPLOAD_FOLDER']
                os.makedirs(pasta, exist_ok=True)
                caminho = os.path.join(pasta, filename)
                arquivo.stream.seek(0)
                arquivo.save(caminho)
                caminho_relativo = os.path.relpath(caminho, current_app.root_path)

            novo_documento = Documento(
                url_arquivo=url_arquivo,
                public_id=public_id,
                caminho_arquivo=caminho_relativo,
                tipo=tipo_documento,
                numero_nf=(request.form.get('numero_nf') or request.form.get('nf') or None),
                razao_social=(request.form.get('razao_social') or request.form.get('pagador') or None),
                usuario_id=user_id,
                empresa_id=_empresa_id_para_documento(fallback_user_id=user_id),
                venda_id=None,
                data_processamento=date.today(),
            )
            db.session.add(novo_documento)
            db.session.commit()
            limpar_cache_dashboard()
            return jsonify({
                'status': 'success',
                'mensagem': 'Arquivo recebido',
                'documento_id': novo_documento.id,
                'url_arquivo': novo_documento.url_arquivo,
            }), 200
        except Exception as e:
            db.session.rollback()
            return jsonify({'status': 'erro', 'mensagem': str(e)}), 500

    return _impl()


@documentos_bp.route('/api/bot/upload', methods=['POST'])
def api_bot_upload():
    """Rota dedicada para o bot externo (Node.js) enviar boletos/NFes.

    Autenticação via header X-API-KEY validado contra a variável de ambiente
    API_BOT_TOKEN. Aceita campos: file/arquivo/documento (arquivo),
    tipo (boleto|nfe), numero_nf, razao_social.
    """
    from app import (
        limiter, _processar_documento, _empresa_id_para_documento,
        limpar_cache_dashboard,
    )

    @limiter.limit("10 per minute")
    def _impl():
        token_enviado = request.headers.get('X-API-KEY')
        token_verdadeiro = os.environ.get('API_BOT_TOKEN')

        if not token_verdadeiro or token_enviado != token_verdadeiro:
            return jsonify({'erro': 'Acesso negado. Token inválido ou ausente.'}), 403

        arquivo = request.files.get('file') or request.files.get('arquivo') or request.files.get('documento')
        if not arquivo or not arquivo.filename:
            return jsonify({'erro': 'Nenhum arquivo enviado.'}), 400

        nome_arquivo = secure_filename(arquivo.filename or '')
        if not nome_arquivo:
            return jsonify({'erro': 'Nome de arquivo inválido.'}), 400

        extensao = os.path.splitext(nome_arquivo)[1].lower()
        extensoes_permitidas = {'.pdf', '.png', '.jpg', '.jpeg'}
        mimes_permitidos = {'application/pdf', 'image/png', 'image/jpeg'}
        mime = (getattr(arquivo, 'mimetype', '') or '').lower()
        if extensao not in extensoes_permitidas:
            return jsonify({'erro': 'Extensão de arquivo não permitida.'}), 400
        if mime not in mimes_permitidos:
            return jsonify({'erro': 'Tipo MIME inválido para upload.'}), 400

        tipo = (request.form.get('tipo') or request.form.get('type') or '').strip().lower()
        if tipo == 'boleto':
            subpasta = 'boletos'
        elif tipo == 'nfe':
            subpasta = 'notas_fiscais'
        else:
            return jsonify({'erro': "Campo 'tipo' inválido. Use 'boleto' ou 'nfe'."}), 400

        primeiro_user = Usuario.query.first()
        user_id = primeiro_user.id if primeiro_user else None

        try:
            base_dir = os.path.join(current_app.root_path, 'documentos_entrada')
            caminho_final = os.path.join(base_dir, subpasta)
            os.makedirs(caminho_final, exist_ok=True)
            caminho_completo = os.path.join(caminho_final, nome_arquivo)

            arquivo.stream.seek(0)
            arquivo.save(caminho_completo)
            _processar_documento(caminho_completo, user_id_forcado=user_id)
            limpar_cache_dashboard()

            caminho_relativo = os.path.join('documentos_entrada', subpasta, nome_arquivo).replace('\\', '/')
            doc_criado = Documento.query.filter_by(caminho_arquivo=caminho_relativo).order_by(Documento.id.desc()).first()
            if not doc_criado:
                tipo_doc = 'BOLETO' if subpasta == 'boletos' else 'NOTA_FISCAL'
                doc_criado = Documento(
                    caminho_arquivo=caminho_relativo,
                    tipo=tipo_doc,
                    usuario_id=user_id,
                    empresa_id=_empresa_id_para_documento(fallback_user_id=user_id),
                    venda_id=None,
                    data_processamento=date.today(),
                )
                db.session.add(doc_criado)
                db.session.commit()

            return jsonify({
                'status': 'success',
                'mensagem': 'Arquivo recebido e processado com sucesso.',
                'documento_id': doc_criado.id if doc_criado else None,
                'tipo': doc_criado.tipo if doc_criado else ('BOLETO' if subpasta == 'boletos' else 'NOTA_FISCAL'),
                'numero_nf': getattr(doc_criado, 'numero_nf', None),
                'cnpj': getattr(doc_criado, 'cnpj', None),
                'data_vencimento': doc_criado.data_vencimento.strftime('%Y-%m-%d') if getattr(doc_criado, 'data_vencimento', None) else None,
                'url_arquivo': getattr(doc_criado, 'url_arquivo', None),
            }), 200

        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erro no bot upload: {e}")
            return jsonify({'erro': str(e)}), 500

    return _impl()


@documentos_bp.route('/processar_documentos', methods=['POST'])
def processar_documentos():
    """Rota para processar documentos manualmente (opcional, via AJAX)."""
    from app import _processar_documentos_pendentes
    resultado = _processar_documentos_pendentes()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True, **resultado)
    flash(f"Processados {resultado['processados']} documento(s).", 'success')
    if resultado['erros'] > 0:
        flash(f"Erros: {resultado['erros']}.", 'error')
    return redirect(url_for('dashboard'))


@documentos_bp.route('/reprocessar_boletos', methods=['POST'])
def reprocessar_boletos():
    """Re-lê os PDFs em documentos_entrada/boletos e atualiza numero_nf
    (e demais campos) nos Documentos."""
    from app import _reprocessar_boletos_atualizar_extracao
    r = _reprocessar_boletos_atualizar_extracao()
    flash(f"Boletos reprocessados: {r['atualizados']} atualizado(s).", 'success')
    if r['erros'] > 0:
        flash(f"Erros ao reprocessar: {r['erros']}.", 'error')
    return redirect(url_for('dashboard'))


@documentos_bp.route('/admin/arquivos')
def admin_arquivos():
    """Lista os documentos do tenant atual, ordenados pelos mais recentes.

    Restrito a admins do tenant (DONO/MASTER) — funcionário comum vê apenas
    a fila do dashboard com os documentos atribuídos a ele.
    """
    from app import _e_admin_tenant, query_documentos_tenant
    if not _e_admin_tenant():
        flash('Acesso negado. Apenas administradores podem gerenciar arquivos.', 'error')
        return redirect(url_for('dashboard'))
    busca = (request.args.get('busca') or '').strip()
    query = query_documentos_tenant()
    if busca:
        termo = f"%{busca}%"
        filtros = [
            Documento.numero_nf.ilike(termo),
            Documento.razao_social.ilike(termo),
            Venda.nf.ilike(termo),
            Cliente.nome_cliente.ilike(termo),
        ]
        if busca.isdigit():
            busca_id = int(busca)
            filtros.extend([
                Documento.id == busca_id,
                Documento.venda_id == busca_id,
                Venda.id == busca_id,
            ])
        query = query.outerjoin(Documento.venda)\
                     .outerjoin(Venda.cliente)\
                     .options(
                         contains_eager(Documento.venda).contains_eager(Venda.cliente)
                     )\
                     .filter(or_(*filtros))\
                     .distinct()
    else:
        query = query.options(
            joinedload(Documento.venda).joinedload(Venda.cliente)
        )
    documentos = query.order_by(Documento.data_processamento.desc(), Documento.id.desc()).all()
    return render_template('gerenciar_arquivos.html', documentos=documentos, busca=busca)


@documentos_bp.route('/arquivos/upload_massa', methods=['POST'])
def upload_massa_arquivos():
    from app import _e_admin_tenant, _processar_documento, limpar_cache_dashboard
    if not _e_admin_tenant():
        return jsonify({'success': False, 'error': 'Apenas administradores podem importar arquivos.'}), 403

    arquivos = request.files.getlist('arquivos[]')
    if not arquivos or all((not arq) or (not arq.filename) for arq in arquivos):
        return jsonify({'success': False, 'error': 'Nenhum arquivo selecionado.'}), 400

    if len(arquivos) > 20:
        return jsonify({'success': False, 'error': 'Selecione no máximo 20 arquivos por envio.'}), 400

    arquivos_salvos = 0
    arquivos_processados = 0
    erros_processamento = []
    documentos_ids = []
    try:
        for arquivo in arquivos:
            if not arquivo or not arquivo.filename:
                continue

            nome_seguro = secure_filename(arquivo.filename)
            if not nome_seguro:
                continue

            pasta_upload = current_app.config['UPLOAD_FOLDER']
            os.makedirs(pasta_upload, exist_ok=True)
            caminho_temporario = os.path.join(pasta_upload, nome_seguro)
            arquivo.save(caminho_temporario)

            try:
                _processar_documento(caminho_temporario, user_id_forcado=current_user.id)
                arquivos_processados += 1
                doc_criado = Documento.query.filter(
                    Documento.caminho_arquivo.ilike(f"%/{nome_seguro}")
                ).order_by(Documento.id.desc()).first()
                if doc_criado:
                    documentos_ids.append(doc_criado.id)
            except Exception as e:
                erros_processamento.append(f'{nome_seguro}: {str(e)}')
            arquivos_salvos += 1

        if arquivos_salvos == 0:
            return jsonify({'success': False, 'error': 'Nenhum arquivo válido foi enviado.'}), 400

        if arquivos_processados == 0 and erros_processamento:
            return jsonify({'success': False, 'error': 'Nenhum arquivo pôde ser processado. Verifique o formato dos documentos.'}), 500

        msg = f'{arquivos_processados} arquivo(s) importado(s) e enviado(s) para extração automática.'
        if erros_processamento:
            msg += f' {len(erros_processamento)} arquivo(s) com falha foram ignorados.'
        limpar_cache_dashboard()
        return jsonify({'success': True, 'mensagem': msg, 'erros': erros_processamento})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@documentos_bp.route('/admin/arquivos/deletar_massa', methods=['POST'])
def admin_arquivos_deletar_massa():
    """Exclusão em massa de documentos do tenant. Recebe lista de IDs via form ou JSON.

    Pós-auditoria P0: filtra exclusivamente pelo tenant do usuário (DONO/MASTER).
    Funcionário comum não tem permissão administrativa de massa.
    """
    from app import _e_admin_tenant, query_documentos_tenant, _EXTERNAL_TIMEOUT
    if not _e_admin_tenant():
        flash('Acesso negado. Apenas administradores podem gerenciar arquivos.', 'error')
        return redirect(url_for('dashboard'))
    ids_raw = request.form.getlist('ids[]') or request.form.getlist('ids') or (request.get_json(silent=True) or {}).get('ids', [])
    if not ids_raw:
        flash('Nenhum arquivo selecionado.', 'warning')
        return redirect(url_for('documentos.admin_arquivos'))
    try:
        ids = list({int(x) for x in ids_raw if x is not None and str(x).strip()})
    except (TypeError, ValueError):
        flash('IDs inválidos.', 'error')
        return redirect(url_for('documentos.admin_arquivos'))
    if not ids:
        flash('Nenhum arquivo selecionado.', 'warning')
        return redirect(url_for('documentos.admin_arquivos'))
    try:
        docs = query_documentos_tenant().filter(Documento.id.in_(ids)).all()
        for d in docs:
            if d.public_id and (os.environ.get('CLOUDINARY_URL') or current_app.config.get('CLOUDINARY_URL')):
                try:
                    cloudinary.uploader.destroy(d.public_id, resource_type='raw', timeout=_EXTERNAL_TIMEOUT)
                except Exception as ex:
                    current_app.logger.error(f"Erro ao excluir do Cloudinary {d.public_id}: {ex}")
            db.session.delete(d)
        db.session.commit()
        flash(f'{len(docs)} documento(s) excluído(s) com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao deletar documentos em massa: {e}")
        flash('Erro ao excluir documentos. Tente novamente.', 'error')
    return redirect(url_for('documentos.admin_arquivos'))


@documentos_bp.route('/admin/reprocessar-vencimentos', methods=['GET', 'POST'])
def admin_reprocessar_vencimentos():
    """Reprocessa todos os PDFs de boletos vinculados às vendas para
    extrair/atualizar data_vencimento.

    MASTER-only: a operação varre TODOS os boletos do SaaS para extrair
    data_vencimento; abrir para DONO reabriria a brecha cross-tenant.
    GET: exibe página de confirmação com preview
    POST: executa o reprocessamento
    """
    from app import master_required, _reprocessar_vencimentos_vendas

    @master_required
    def _impl():
        if request.method == 'GET':
            total_com_boleto = Venda.query.filter(Venda.caminho_boleto.isnot(None)).count()
            total_sem_vencimento = Venda.query.filter(
                Venda.caminho_boleto.isnot(None),
                Venda.data_vencimento.is_(None),
            ).count()
            return f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reprocessar Vencimentos</title>
    <link rel="stylesheet" href="/static/css/output.css">
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center">
    <div class="bg-white rounded-xl shadow-lg p-8 max-w-lg w-full">
        <h1 class="text-2xl font-bold text-emerald-700 mb-4">Reprocessar Vencimentos</h1>
        <p class="text-gray-700 mb-4">
            Esta ação irá re-ler todos os PDFs de boletos vinculados às vendas e extrair a <strong>data de vencimento</strong> de cada um.
        </p>
        <div class="bg-emerald-50 border border-emerald-200 rounded-lg p-4 mb-6">
            <p class="text-sm text-emerald-800"><strong>Total de vendas com boleto:</strong> {total_com_boleto}</p>
            <p class="text-sm text-emerald-800"><strong>Vendas sem data de vencimento:</strong> {total_sem_vencimento}</p>
        </div>
        <form method="POST" class="flex gap-3">
            <input type="hidden" name="csrf_token" value="{generate_csrf()}"/>
            <button type="submit" class="bg-emerald-700 text-white px-6 py-3 rounded-xl hover:bg-emerald-600 transition font-semibold">
                Executar Reprocessamento
            </button>
            <a href="/vendas" class="bg-gray-200 text-gray-700 px-6 py-3 rounded-xl hover:bg-gray-300 transition font-medium">
                Cancelar
            </a>
        </form>
    </div>
</body>
</html>'''

        resultado = _reprocessar_vencimentos_vendas()

        return f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Resultado do Reprocessamento</title>
    <link rel="stylesheet" href="/static/css/output.css">
</head>
<body class="bg-gray-100 min-h-screen flex items-center justify-center p-4">
    <div class="bg-white rounded-xl shadow-lg p-8 max-w-2xl w-full">
        <h1 class="text-2xl font-bold text-emerald-700 mb-4">Reprocessamento Concluído</h1>
        <div class="grid grid-cols-2 gap-4 mb-6">
            <div class="bg-blue-50 border border-blue-200 rounded-lg p-4 text-center">
                <p class="text-3xl font-bold text-blue-700">{resultado['total']}</p>
                <p class="text-sm text-blue-600">Total com boleto</p>
            </div>
            <div class="bg-emerald-50 border border-emerald-200 rounded-lg p-4 text-center">
                <p class="text-3xl font-bold text-emerald-700">{resultado['atualizados']}</p>
                <p class="text-sm text-emerald-600">Atualizados</p>
            </div>
            <div class="bg-amber-50 border border-amber-200 rounded-lg p-4 text-center">
                <p class="text-3xl font-bold text-amber-700">{resultado['sem_data']}</p>
                <p class="text-sm text-amber-600">Sem data no PDF</p>
            </div>
            <div class="bg-red-50 border border-red-200 rounded-lg p-4 text-center">
                <p class="text-3xl font-bold text-red-700">{resultado['erros']}</p>
                <p class="text-sm text-red-600">Erros</p>
            </div>
        </div>
        <details class="mb-6">
            <summary class="cursor-pointer text-sm font-medium text-gray-700 hover:text-emerald-700">Ver detalhes ({len(resultado['detalhes'])} registros)</summary>
            <div class="mt-2 bg-gray-50 rounded-lg p-4 max-h-64 overflow-y-auto text-xs font-mono">
                {"<br>".join(resultado['detalhes']) if resultado['detalhes'] else "Nenhum detalhe disponível."}
            </div>
        </details>
        <a href="/vendas" class="inline-block bg-emerald-700 text-white px-6 py-3 rounded-xl hover:bg-emerald-600 transition font-semibold">
            Voltar para Vendas
        </a>
    </div>
</body>
</html>'''

    return _impl()


@documentos_bp.route('/documento/<int:id>/vincular', methods=['POST'])
def vincular_documento_venda(id):
    """Associa o documento a um pedido (venda). Espera venda_id (primeira_venda_id do pedido)."""
    from app import (
        query_documentos_tenant, query_tenant,
        _usuario_pode_gerenciar_documento, _usuario_pode_gerenciar_venda,
        _resposta_sem_permissao, _processar_pdf, _vendas_do_pedido,
        limpar_cache_dashboard,
    )
    documento = query_documentos_tenant().filter_by(id=id).first_or_404()
    if not _usuario_pode_gerenciar_documento(documento):
        return _resposta_sem_permissao()
    venda_id = request.form.get('venda_id') or (request.get_json(silent=True) or {}).get('venda_id')
    if not venda_id:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, mensagem='Informe o pedido (venda_id).'), 400
        flash('Informe o pedido para vincular.', 'error')
        return redirect(url_for('dashboard'))
    try:
        venda_id = int(venda_id)
    except (TypeError, ValueError):
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, mensagem='venda_id inválido.'), 400
        flash('Pedido inválido.', 'error')
        return redirect(url_for('dashboard'))
    venda = query_tenant(Venda).filter_by(id=venda_id).first()
    if not venda:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, mensagem='Pedido não encontrado.'), 404
        flash('Pedido não encontrado.', 'error')
        return redirect(url_for('dashboard'))
    if not _usuario_pode_gerenciar_venda(venda):
        return _resposta_sem_permissao()
    try:
        documento.venda_id = venda_id
        path = documento.caminho_arquivo
        vendas_pedido = _vendas_do_pedido(venda)
        is_boleto = (documento.tipo or '').upper() == 'BOLETO'

        data_venc_boleto = None
        if is_boleto and documento.data_vencimento:
            data_venc_boleto = documento.data_vencimento
        elif is_boleto:
            path_full = os.path.join(current_app.root_path, path)
            if os.path.isfile(path_full):
                dados_pdf = _processar_pdf(path_full, 'BOLETO')
                if dados_pdf and dados_pdf.get('data_vencimento'):
                    data_venc_boleto = dados_pdf['data_vencimento']
                    documento.data_vencimento = data_venc_boleto

        for vv in vendas_pedido:
            if is_boleto:
                vv.caminho_boleto = path
                if data_venc_boleto:
                    vv.data_vencimento = data_venc_boleto
            else:
                vv.caminho_nf = path
        db.session.commit()
        limpar_cache_dashboard()
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"[vincular_documento_venda] Erro ao vincular documento ID {id} na venda {venda_id}: {e}")
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, mensagem='Erro ao salvar vínculo do documento.'), 500
        flash('Erro ao salvar vínculo do documento.', 'error')
        return redirect(url_for('dashboard'))

    c = venda.cliente
    rs = (c.razao_social or '').strip()
    label_cliente = f"{c.nome_cliente} ({rs})" if rs else c.nome_cliente
    tipo_doc = (documento.tipo or '').upper()
    if tipo_doc == 'BOLETO':
        msg = f'Boleto vinculado ao cliente: {label_cliente}.'
    else:
        msg = f'Documento vinculado ao pedido (Cliente: {label_cliente}, NF: {venda.nf or "-"}).'
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True, sucesso=True, mensagem=msg, doc_id=documento.id)
    flash(msg, 'success')
    return redirect(url_for('dashboard'))


@documentos_bp.route('/admin/raio_x', methods=['GET'])
def raio_x():
    """Diagnóstico: últimos 5 documentos cadastrados e ID do usuário atual.

    MASTER-only: a query lê documentos cross-tenant para diagnóstico.
    """
    from app import master_required

    @master_required
    def _impl():
        docs = Documento.query.order_by(Documento.id.desc()).limit(5).all()
        page = '''<!DOCTYPE html>
<html lang="pt-BR">
<head><meta charset="UTF-8"><title>Raio-X Documentos</title>
<style>body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:1rem;background:#f5f5f5;}h1{color:#0d9488;}table{border-collapse:collapse;width:100%;background:white;box-shadow:0 1px 3px rgba(0,0,0,.1);}th,td{padding:.75rem;text-align:left;border-bottom:1px solid #e5e7eb;}th{background:#0d9488;color:white;}tr:hover{background:#f0fdfa;}p.info{background:#e0f2fe;padding:1rem;border-radius:8px;margin-bottom:1.5rem;}</style>
</head>
<body>
<h1>🔍 Raio-X Documentos</h1>
<p class="info"><strong>Seu ID atual:</strong> ''' + str(current_user.id) + ''' (usuário: ''' + str(current_user.username) + ''')</p>
<h2>Últimos 5 documentos</h2>
<table>
<tr><th>ID</th><th>Nome do Arquivo</th><th>ID Dono (usuario_id)</th><th>Status</th><th>Data de Upload</th></tr>'''
        for d in docs:
            nome = os.path.basename(d.caminho_arquivo or '')
            status = 'Vinculado' if d.venda_id else 'Sem vínculo'
            usuario_id_str = str(d.usuario_id) if d.usuario_id is not None else '<em>NULL</em>'
            data_str = d.data_processamento.strftime('%d/%m/%Y') if d.data_processamento else '-'
            page += f'<tr><td>{d.id}</td><td>{nome}</td><td>{usuario_id_str}</td><td>{status}</td><td>{data_str}</td></tr>'
        page += '''</table>
</body></html>'''
        return page

    return _impl()


@documentos_bp.route('/admin/resgatar_orfaos', methods=['POST'])
def resgatar_orfaos():
    """Atribui ao usuário atual (MASTER) todos os documentos com usuario_id NULL.

    MASTER-only: ação varre documentos sem owner em todo o SaaS.
    """
    from app import master_required

    @master_required
    def _impl():
        db.session.rollback()
        try:
            orfaos = Documento.query.filter(Documento.usuario_id.is_(None)).all()
            count = len(orfaos)
            for doc in orfaos:
                doc.usuario_id = current_user.id
            db.session.commit()
            flash(f'Recuperados {count} documento(s) órfão(s).', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao resgatar órfãos: {str(e)}', 'error')
        return redirect(url_for('dashboard'))

    return _impl()


@documentos_bp.route('/admin/forcar_leitura_pasta', methods=['POST'])
def forcar_leitura_pasta():
    """Rota de emergência: lê PDFs em boletos e notas_fiscais, cria registros
    Documento para os que não existem no banco.

    MASTER-only: opera diretamente sobre o filesystem do servidor.
    """
    from app import master_required, _empresa_id_para_documento, _EXTERNAL_TIMEOUT

    @master_required
    def _impl():
        db.session.rollback()
        base_dir = os.path.join(current_app.root_path, 'documentos_entrada')
        pastas = {
            'BOLETO': os.path.join(base_dir, 'boletos'),
            'NOTA_FISCAL': os.path.join(base_dir, 'notas_fiscais'),
        }
        ressuscitados = 0
        try:
            for tipo, pasta in pastas.items():
                if not os.path.exists(pasta):
                    continue
                for nome in os.listdir(pasta):
                    if not nome.lower().endswith('.pdf'):
                        continue
                    caminho_relativo = os.path.join(
                        'documentos_entrada',
                        'boletos' if tipo == 'BOLETO' else 'notas_fiscais',
                        nome,
                    ).replace(os.sep, '/')
                    doc_existente = Documento.query.filter_by(caminho_arquivo=caminho_relativo).first()
                    if not doc_existente:
                        caminho_full = os.path.join(
                            base_dir,
                            'boletos' if tipo == 'BOLETO' else 'notas_fiscais',
                            nome,
                        )
                        url_arquivo = None
                        public_id = None
                        if os.environ.get('CLOUDINARY_URL') or current_app.config.get('CLOUDINARY_URL'):
                            try:
                                resultado_nuvem = cloudinary.uploader.upload(
                                    caminho_full, resource_type='raw', timeout=_EXTERNAL_TIMEOUT,
                                )
                                url_arquivo = resultado_nuvem.get('secure_url')
                                public_id = resultado_nuvem.get('public_id')
                            except Exception as ex:
                                current_app.logger.error(f"Erro Cloudinary (forcar_leitura): {ex}")
                        doc = Documento(
                            caminho_arquivo=caminho_relativo,
                            url_arquivo=url_arquivo,
                            public_id=public_id,
                            tipo=tipo,
                            usuario_id=current_user.id,
                            empresa_id=_empresa_id_para_documento(fallback_user_id=current_user.id),
                            data_processamento=date.today(),
                        )
                        db.session.add(doc)
                        ressuscitados += 1
            db.session.commit()
            for tipo, pasta in pastas.items():
                if not os.path.exists(pasta):
                    continue
                for nome in os.listdir(pasta):
                    if not nome.lower().endswith('.pdf'):
                        continue
                    caminho_full = os.path.join(pasta, nome)
                    doc = Documento.query.filter_by(
                        caminho_arquivo=os.path.join(
                            'documentos_entrada',
                            'boletos' if tipo == 'BOLETO' else 'notas_fiscais',
                            nome,
                        ).replace(os.sep, '/')
                    ).first()
                    if doc and doc.url_arquivo and os.path.exists(caminho_full):
                        try:
                            os.remove(caminho_full)
                        except Exception as rm_err:
                            current_app.logger.warning(f"Aviso: não foi possível remover {caminho_full}: {rm_err}")
        except Exception as e:
            db.session.rollback()
            return jsonify({'erro': str(e), 'ressuscitados': 0}), 500
        return jsonify({
            'ressuscitados': ressuscitados,
            'mensagem': f'{ressuscitados} arquivo(s) ressuscitado(s) e inserido(s) no banco.',
        })

    return _impl()


@documentos_bp.route('/admin/limpar_fantasmas', methods=['POST'])
def limpar_fantasmas():
    """Remove da tabela Documento os registros cujo arquivo físico não existe mais.

    MASTER-only: limpeza global do banco/filesystem.
    """
    from app import master_required

    @master_required
    def _impl():
        db.session.rollback()
        base_path = current_app.root_path
        removidos = 0
        try:
            docs = Documento.query.filter(Documento.url_arquivo.is_(None)).limit(2000).all()
            for doc in docs:
                caminho_full = os.path.join(base_path, doc.caminho_arquivo or '')
                if doc.caminho_arquivo and not os.path.exists(caminho_full):
                    db.session.delete(doc)
                    removidos += 1
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({'erro': str(e), 'removidos': 0}), 500
        return jsonify({'removidos': removidos, 'mensagem': f'{removidos} fantasma(s) removido(s) do banco.'})

    return _impl()


@documentos_bp.route('/admin/limpar_vinculos_quebrados', methods=['POST'])
def limpar_vinculos_quebrados():
    """Limpa todos os vínculos quebrados:
    1. caminho_boleto/caminho_nf que apontam para documentos inexistentes
    2. Documentos com venda_id apontando para vendas inexistentes

    MASTER-only: operação de manutenção global.
    """
    from app import master_required

    @master_required
    def _impl():
        try:
            limpos_boleto = 0
            limpos_nf = 0
            limpos_docs = 0

            vendas_com_boleto = Venda.query.filter(Venda.caminho_boleto.isnot(None)).all()
            for v in vendas_com_boleto:
                caminho = (v.caminho_boleto or '').strip()
                if caminho:
                    doc = Documento.query.filter(or_(Documento.caminho_arquivo == caminho, Documento.url_arquivo == caminho)).first()
                    if not doc:
                        v.caminho_boleto = None
                        limpos_boleto += 1

            vendas_com_nf = Venda.query.filter(Venda.caminho_nf.isnot(None)).all()
            for v in vendas_com_nf:
                caminho = (v.caminho_nf or '').strip()
                if caminho:
                    doc = Documento.query.filter(or_(Documento.caminho_arquivo == caminho, Documento.url_arquivo == caminho)).first()
                    if not doc:
                        v.caminho_nf = None
                        limpos_nf += 1

            documentos_com_venda = Documento.query.filter(Documento.venda_id.isnot(None)).all()
            for doc in documentos_com_venda:
                venda = Venda.query.get(doc.venda_id)
                if not venda:
                    doc.venda_id = None
                    limpos_docs += 1

            db.session.commit()
            total = limpos_boleto + limpos_nf + limpos_docs
            flash(f'✅ Limpeza concluída: {limpos_boleto} vínculo(s) de boleto, {limpos_nf} vínculo(s) de NF e {limpos_docs} documento(s) órfão(s) removidos ({total} total).', 'success')
            current_app.logger.debug(f"DEBUG LIMPEZA: {limpos_boleto} boletos, {limpos_nf} NFs e {limpos_docs} documentos órfãos limpos")
        except Exception as e:
            db.session.rollback()
            flash(f'Erro ao limpar vínculos: {str(e)}', 'error')
            current_app.logger.error(f"DEBUG LIMPEZA ERRO: {str(e)}")

        return redirect(url_for('dashboard'))

    return _impl()


@documentos_bp.route('/debug/testar_log', methods=['POST'])
def debug_testar_log():
    """Endpoint de debug para testar criação de arquivo de log.

    MASTER-only: escreve arquivo no filesystem do servidor.
    """
    from app import master_required

    @master_required
    def _impl():
        try:
            log_path = os.path.join(current_app.root_path, 'vinculo_detalhado.log')
            with open(log_path, 'w', encoding='utf-8') as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - TESTE DE CRIAÇÃO DE ARQUIVO\n")
            return jsonify({
                'sucesso': True,
                'arquivo_criado': os.path.exists(log_path),
                'caminho': log_path,
                'tamanho': os.path.getsize(log_path) if os.path.exists(log_path) else 0,
            })
        except Exception as e:
            return jsonify({'sucesso': False, 'erro': str(e), 'traceback': traceback.format_exc()}), 500

    return _impl()


@documentos_bp.route('/debug-vincular')
def debug_vincular():
    """Endpoint de debug para diagnóstico de vínculos - retorna todos os logs em JSON.

    Restrito a admin do tenant (decorator ``admin_required`` aplicado dinamicamente).
    """
    from app import admin_required, _processar_documentos_pendentes

    @admin_required
    def _impl():
        try:
            resultado = _processar_documentos_pendentes(capturar_logs_memoria=True)
            resposta = {
                'sucesso': True,
                'timestamp': datetime.now().isoformat(),
                'estatisticas': {
                    'processados': resultado.get('processados', 0),
                    'vinculos_novos': resultado.get('vinculos_novos', 0),
                    'erros': resultado.get('erros', 0),
                },
                'mensagens': resultado.get('mensagens', []),
                'logs_completos': resultado.get('logs', []),
            }
            return jsonify(resposta)
        except Exception as e:
            db.session.rollback()
            return jsonify({
                'sucesso': False,
                'erro': str(e),
                'traceback': traceback.format_exc(),
                'timestamp': datetime.now().isoformat(),
            }), 500

    return _impl()
