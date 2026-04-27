from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file, Response, current_app
from flask_login import login_user, logout_user, login_required, current_user
from flask_wtf.csrf import generate_csrf
from flask_compress import Compress
# Singletons (db, login_manager, csrf, limiter, cache) vivem em extensions.py
# desde a Fase 4. Re-exportamos no namespace de app.py para preservar
# compatibilidade com blueprints e scripts legados que fazem
# ``from app import db, csrf, limiter, cache, login_manager``.
from extensions import (
    db,
    login_manager,
    csrf,
    cache,
    limiter,
    init_extensions,
)
from models import (
    Cliente,
    Produto,
    ProdutoFoto,
    Venda,
    Usuario,
    Configuracao,
    Documento,
    LancamentoCaixa,
    ContagemGaveta,
    Fornecedor,
    TipoProduto,
    PushSubscription,
    LogAtividade,
    Empresa,
    PERFIL_MASTER,
    PERFIL_DONO,
    PERFIL_FUNCIONARIO,
)
from quotes import frase_do_dia
from config import Config
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from sqlalchemy import event
from sqlalchemy.engine import Engine
import pytz
from functools import wraps
from sqlalchemy import func, desc, asc, text, or_, extract, case, cast, inspect
from sqlalchemy.orm import joinedload, contains_eager, selectinload
from sqlalchemy.exc import IntegrityError, OperationalError
import pandas as pd
import os
import re
import urllib.parse
import json
import csv
import io
import html
import shutil
import hashlib
import traceback
import urllib.request
import logging
from logging.handlers import RotatingFileHandler
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException
from redis import Redis
from rq import Queue
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.memory import MemoryJobStore
try:
    from apscheduler.jobstores.redis import RedisJobStore as _RedisJobStore
    _HAS_REDIS_JOBSTORE = True
except ImportError:
    _HAS_REDIS_JOBSTORE = False
from werkzeug.security import generate_password_hash, check_password_hash
import pdfplumber
import cloudinary
import cloudinary.uploader
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders


def get_hoje_brasil():
    """Retorna a data de hoje no fuso horário do Brasil (Recife/São Paulo)."""
    try:
        fuso = pytz.timezone('America/Recife')
        return datetime.now(fuso).date()
    except Exception:
        return date.today()


# Extensões aceitas para upload de imagens (perfil, produto, cheque)
_ALLOWED_IMAGE_EXT = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

def _arquivo_imagem_permitido(filename: str) -> bool:
    """Retorna True se a extensão do arquivo é uma imagem permitida."""
    return (
        bool(filename)
        and '.' in filename
        and filename.rsplit('.', 1)[1].lower() in _ALLOWED_IMAGE_EXT
    )


def _is_ajax():
    """Retorna True se a requisição atual veio via XMLHttpRequest (fetch/jQuery).

    Promovido para utility module-level (originalmente nasceu dentro do bloco
    de clientes) porque é consumido por blueprints de clientes, produtos e
    vendas — todos importam via ``from app import _is_ajax``.
    """
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


def registrar_log(acao: str, modulo: str, descricao: str) -> None:
    """
    Persiste uma entrada no log de auditoria do sistema.

    Deve ser chamada APÓS o db.session.commit() da operação principal,
    dentro de um bloco try/except próprio para não bloquear a operação
    original caso o log falhe.

    Args:
        acao: Verbo da ação ('CRIAR', 'EDITAR', 'EXCLUIR', 'INATIVAR', 'ATIVAR', 'PAGAR').
        modulo: Módulo do sistema ('VENDAS', 'CLIENTES', 'PRODUTOS', 'USUARIOS').
        descricao: Texto livre detalhando o que foi feito.
    """
    try:
        usuario_id = current_user.id if current_user and current_user.is_authenticated else None
        ip = request.remote_addr if request else None
        log = LogAtividade(
            usuario_id=usuario_id,
            acao=acao,
            modulo=modulo,
            descricao=descricao,
            ip_address=ip,
        )
        db.session.add(log)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning(f"Falha ao registrar log [{acao}/{modulo}]: {exc}")


def _safe_db_commit() -> tuple[bool, str | None]:
    """
    Executa db.session.commit() com tratamento robusto de erros.

    Returns:
        (sucesso: bool, mensagem_erro: str | None)
    """
    try:
        db.session.commit()
        return True, None
    except (IntegrityError, OperationalError) as e:
        db.session.rollback()
        logging.error("Erro de banco ao commitar: %s", str(e), exc_info=True)
        return False, str(e)
    except Exception as e:
        db.session.rollback()
        logging.error("Erro inesperado ao commitar: %s", str(e), exc_info=True)
        return False, str(e)


# Timeout padrão para chamadas externas (Cloudinary, SMTP, urllib) — evita travar workers
_EXTERNAL_TIMEOUT = 15


def pad_base64(data: str | None) -> str | None:
    """Normaliza Base64/Base64URL adicionando padding '=' quando necessário.

    Alguns geradores de chave VAPID retornam formato Base64URL sem padding.
    A biblioteca cryptography pode falhar ao desserializar sem esse ajuste.
    """
    if not data:
        return data
    # Se vier em PEM (BEGIN/END), não alterar.
    if '-----BEGIN' in data and '-----END' in data:
        return data
    missing_padding = len(data) % 4
    if missing_padding:
        data += '=' * (4 - missing_padding)
    return data


# Mapeamento flexível: nomes possíveis no arquivo -> nomes do banco (models.Produto)
COLUNA_ARQUIVO_PARA_BANCO = {
    'produto': 'nome_produto',
    'nome_produto': 'nome_produto',
    'nome': 'nome_produto',
    'preço': 'preco_custo',
    'preco': 'preco_custo',
    'preco_custo': 'preco_custo',
    'quantidade': 'quantidade_entrada',
    'qtd': 'quantidade_entrada',
    'quantidade_entrada': 'quantidade_entrada',
    'data': 'data_chegada',
    'data_chegada': 'data_chegada',
    'caminhoneiro': 'caminhoneiro',
    'fornecedor': 'fornecedor',
    'tipo': 'tipo',
    'categoria': 'tipo',
    'nacionalidade': 'nacionalidade',
    'origem': 'nacionalidade',
    'tamanho': 'tamanho',
    'classificacao': 'tamanho',
    'marca': 'marca',
}


def _normalizar_nome_coluna(s):
    """Normaliza nome de coluna: strip, lowercase, espaços -> underscore."""
    if pd.isna(s) or s is None:
        return ''
    s = str(s).strip().lower()
    s = re.sub(r'\s+', '_', s)
    # Remove acentos comuns para mapeamento
    s = s.replace('ç', 'c').replace('ã', 'a').replace('á', 'a').replace('à', 'a')
    return s


def _parse_preco(val):
    """Converte valor de preço para float. Remove R$, espaços, aspas; troca vírgula por ponto (formato BR).
    Preserva o sinal de menos para valores negativos (perdas, prejuízos, ajustes).
    Ex: '-R$ 120,00' → -120.0"""
    if pd.isna(val) and val != 0:
        return None
    if val is None:
        return None
    s = str(val).strip().strip('"').strip("'").strip()
    if not s:
        return None
    # Remover R$, espaços; depois detectar e preservar sinal de menos
    s = re.sub(r'R\$\s*', '', s, flags=re.IGNORECASE)
    s = s.replace(' ', '')
    negativo = s.lstrip().startswith('-')
    s = s.lstrip('-').strip()  # Remove o menos para processar; reinstituímos no final
    # Formato BR: 1.234,56 → remover pontos (milhar), vírgula → ponto
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    try:
        n = float(s)
        return -n if negativo else n
    except (ValueError, TypeError):
        return None


def _parse_quantidade(val):
    """Converte valor para inteiro (quantidade)."""
    if pd.isna(val) and val != 0:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


def _strip_quotes(s):
    """Remove aspas duplas/simples e espaços nas bordas. Retorna str. Usado nos valores lidos de CSV/Excel."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ''
    return str(s).strip().strip('"').strip("'").strip()


def _normalizar_nome_busca(s):
    """Normaliza nome para busca tolerante a espaços: strip, colapsa múltiplos espaços, uppercase.
    Usado para encontrar produto/cliente na importação mesmo com espaços duplos ou invisíveis."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return ''
    return ' '.join(str(s).strip().split()).upper()


def _msg_linha(linha_num, contexto, mensagem, fechar=True):
    """Mensagem amigável de erro por linha. contexto pode ser nome do cliente/produto ou vazio."""
    ctx = f" ({contexto})" if contexto else ""
    fim = " Pode conferir na sua planilha?" if fechar else ""
    return f"Ops! Linha {linha_num}{ctx}: {mensagem}.{fim}"


def _parse_data_flex(s):
    """Converte string/data para date. Aceita dd/mm/yyyy, dd/mm/yy (→ 20XX), ISO, etc. Retorna (date ou None, raw)."""
    if s is None or pd.isna(s):
        return None, '' if s is None else str(s)
    raw = str(s).strip().strip('"').strip("'").strip()
    if not raw or raw.lower() in ('nan', 'nat', ''):
        return None, raw
    m = re.match(r'^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$', raw)
    if m:
        d, mo, y = m.groups()
        if len(y) == 2:
            y = '20' + y
        try:
            return date(int(y), int(mo), int(d)), raw
        except ValueError:
            pass
    parsed = pd.to_datetime(raw, dayfirst=True, errors='coerce')
    if pd.isna(parsed):
        return None, raw
    return parsed.date(), raw


# ============================================================================
# FUNÇÕES DE EXTRAÇÃO DE DADOS DE PDF (BOLETOS E NOTAS FISCAIS)
# ============================================================================

# CNPJs de beneficiários/emissores — ignorar ao capturar CNPJ do pagador
CNPJ_PATY = '03.553.665/0002-00'
CNPJ_DESTAK = '30.820.528/0001-78'
CNPJ_EMISSOR_SERVICO = '14.187.040/0001-07'  # CNPJ de serviço/emissor comum
CNPJS_EMISSORES = frozenset({
    CNPJ_PATY,
    CNPJ_DESTAK,
    CNPJ_EMISSOR_SERVICO,
    '14.187.040/0001-07',  # Formato alternativo
})


def _extrair_linha_danfe_nome_cnpj_data(texto):
    """DANFE: tenta capturar linha com 'NOME/RAZÃO SOCIAL + CNPJ + DATA DA EMISSÃO'."""
    if not texto:
        return None, None
    m = re.search(
        r'^([A-Z0-9\s\&\.\-\*]+?)\s+(\d{2}\.\d{3}\.\d{3}/\d{4}\-\d{2})\s+\d{2}/\d{2}/\d{4}',
        texto,
        re.MULTILINE
    )
    if not m:
        return None, None
    nome = (m.group(1) or '').strip()
    cnpj = (m.group(2) or '').strip()
    return nome or None, cnpj or None


def _extrair_cnpj(texto, nome_arquivo=None):
    """Extrai CNPJ do PAGADOR/DESTINATÁRIO. Padrão \\d{2}\\.\\d{3}\\.\\d{3}/\\d{4}-\\d{2}.
    Ignora PATY, DESTAK e emissores conhecidos. Prioriza CNPJ próximo a 'Pagador', 'Destinatário', 'Razão Social'."""
    padrao = r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}'
    todos = re.findall(padrao, texto)
    emissores_encontrados = [c for c in todos if c in CNPJS_EMISSORES]
    candidatos = [c for c in todos if c not in CNPJS_EMISSORES]
    
    # Debug: mostrar todos os CNPJs encontrados
    if nome_arquivo:
        app.logger.debug(f"DEBUG: CNPJs localizados no arquivo {nome_arquivo}: {todos}")
        if emissores_encontrados:
            app.logger.debug(f"DEBUG: CNPJs de emissores ignorados: {emissores_encontrados}")
        if candidatos:
            app.logger.debug(f"DEBUG: CNPJs candidatos (pagador): {candidatos}")
        elif todos:
            app.logger.warning("DEBUG: AVISO - Apenas CNPJs de emissores encontrados. CNPJ do pagador não identificado.")
    
    if not candidatos:
        if todos and nome_arquivo:
            app.logger.error(f"DEBUG: Erro - Apenas CNPJ(s) do emissor localizado(s) em {nome_arquivo}: {emissores_encontrados}")
        return None

    # Prioridade 0 (DANFE): linha "NOME / RAZÃO SOCIAL CNPJ / CPF DATA DA EMISSÃO"
    _, cnpj_danfe = _extrair_linha_danfe_nome_cnpj_data(texto)
    if cnpj_danfe and cnpj_danfe not in CNPJS_EMISSORES:
        return cnpj_danfe
    
    # Prioridade 1: CNPJ após "CNPJ/CPF:" próximo ao Pagador (Itaú/DESTAK)
    m = re.search(r'Pagador[:\s]*[\s\S]{0,200}?CNPJ\s*/\s*CPF\s*[:\s]*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if m:
        cnpj = m.group(1)
        if cnpj not in CNPJS_EMISSORES:
            return cnpj
    
    # Prioridade 2: CNPJ após "Destinatário" (NF-e)
    m = re.search(r'Destinat[áa]rio[:\s]*[\s\S]{0,300}?CNPJ\s*[:\s]*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if m:
        cnpj = m.group(1)
        if cnpj not in CNPJS_EMISSORES:
            return cnpj
    
    # Prioridade 3: CNPJ após "Razão Social" seguido de CNPJ (NF-e)
    m = re.search(r'Raz[ãa]o\s+Social[:\s]*[\s\S]{0,200}?(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if m:
        cnpj = m.group(1)
        if cnpj not in CNPJS_EMISSORES:
            return cnpj
    
    # Prioridade 4: CNPJ após "CPF/CNPJ" (Bradesco)
    m = re.search(r'CPF/CNPJ\s*([\d\.\-\/]{14,18})', texto, re.IGNORECASE)
    if m:
        cnpj = (m.group(1) or '').strip()
        if cnpj and cnpj not in CNPJS_EMISSORES:
            return cnpj

    # Prioridade 4.1: CNPJ após "CPF / CNPJ" com separação por barra e espaços
    m = re.search(r'CPF\s*/\s*CNPJ\s*[\s:]*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})', texto, re.IGNORECASE)
    if m:
        cnpj = (m.group(1) or '').strip()
        if cnpj not in CNPJS_EMISSORES:
            return cnpj
    
    # Prioridade 5: CNPJ após "Nome/Razão Social" (NF-e)
    m = re.search(r'Nome\s*/\s*Raz[ãa]o\s+Social[:\s]*[\s\S]{0,200}?(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})', texto, re.IGNORECASE | re.DOTALL)
    if m:
        cnpj = m.group(1)
        if cnpj not in CNPJS_EMISSORES:
            return cnpj
    
    # Fallback: usar o último CNPJ válido (geralmente o do pagador vem depois do emissor)
    # Se houver múltiplos, preferir o que não está nos emissores conhecidos
    for cnpj in reversed(candidatos):
        if cnpj not in CNPJS_EMISSORES:
            return cnpj
    
    # Último recurso: primeiro candidato válido
    return candidatos[0] if candidatos else None


BLACKLIST_NF = frozenset({
    '40901685',
    '08007701685',
    '08007280728',
    '08005700011',
})


def _normalizar_nf(s):
    """Normaliza NF para comparação: remove prefixos (NF-, NF , NF), só dígitos, remove zeros à esquerda.
    Ex.: '000042234' -> '42234', 'NF-00042234' -> '42234'. Retorna '' se vazio."""
    if not s:
        return ''
    t = str(s).strip()
    for prefix in ('NF-', 'NF ', 'NF:'):
        if t.upper().startswith(prefix.upper()):
            t = t[len(prefix):].strip()
            break
    if t.upper().startswith('NF'):
        t = re.sub(r'^NF\s*', '', t, flags=re.IGNORECASE).strip()
    digs = re.sub(r'\D', '', t)
    if not digs:
        return ''
    return digs.lstrip('0') or '0'


def _nf_match(doc_norm, venda_norm):
    """True se as NFs normalizadas são iguais ou uma é base + sufixo numérico (ex.: 12263 vs 12263-01 → 1226301).
    Sufixo permitido: 2–4 dígitos, para evitar falsos positivos (ex.: 1226 vs 12263)."""
    if doc_norm == venda_norm:
        return True
    if not doc_norm or not venda_norm:
        return False

    def ok_suffix(shorter, longer):
        if not longer.startswith(shorter) or longer == shorter:
            return False
        suf = longer[len(shorter):]
        return suf.isdigit() and 2 <= len(suf) <= 4

    return ok_suffix(doc_norm, venda_norm) or ok_suffix(venda_norm, doc_norm)


def _normalizar_cnpj(s):
    """Retorna só dígitos do CNPJ para comparação. Remove espaços invisíveis, pontos, barras e traços.
    Ex.: '12.345.678/0001-90' -> '12345678000190', ' 14.187.040/0001-07 ' -> '1418704000107'.
    A comparação deve ser feita SEMPRE com ambos no formato 'apenas números'."""
    if not s:
        return ''
    # Remove espaços invisíveis (strip) e todos os não-dígitos
    return re.sub(r'\D', '', str(s).strip())


def _sanitizar_cnpj_importacao(raw):
    """Limpeza pesada de CNPJ na importação: strip, aspas, quebras de linha, só dígitos.
    Retorna string de 14 dígitos ou None se vazio/inválido."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    s = str(raw).strip().replace('"', '').replace("'", '').replace('\n', '').replace('\r', '')
    digits = re.sub(r'\D', '', s)
    return digits if len(digits) == 14 else (None if not digits else None)


def _parse_clientes_raw_tsv(text):
    """Parse texto bruto TSV (uma linha por cliente, campos separados por TAB).
    Mapeamento: 0=Apelido (nome_cliente), 1=Razão Social (se vazio usa Apelido), 2=CNPJ, 3=Cidade.
    Retorna lista de dicts com chaves nome_cliente, razao_social, cnpj, cidade (valores já sanitizados)."""
    if not text or not str(text).strip():
        return []
    out = []
    for line in str(text).strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split('\t')
        apelido = (parts[0] if len(parts) > 0 else '').strip()
        razao = (parts[1] if len(parts) > 1 else '').strip()
        cnpj_raw = (parts[2] if len(parts) > 2 else '').strip()
        cidade = (parts[3] if len(parts) > 3 else '').strip()
        endereco = (parts[4] if len(parts) > 4 else '').strip() or None
        if not apelido:
            continue
        razao_social = razao if razao else apelido
        cnpj_clean = _sanitizar_cnpj_importacao(cnpj_raw)
        out.append({
            'nome_cliente': apelido,
            'razao_social': razao_social,
            'cnpj': cnpj_clean,
            'cidade': cidade,
            'endereco': endereco,
        })
    return out


def _extrair_numero_nf(texto):
    """Extrai número da NF apenas se colado a 'Núm. do documento', 'NF' ou 'Numero Documento'.
    Ignora últimos 25%% da página (feito em _processar_pdf). 4–6 dígitos; blacklist de telefones."""
    # DANFE: priorizar blocos explícitos de NF-e (aceita zeros à esquerda e número mais longo)
    padroes_danfe = [
        r'NFe?\s*N[º°]\s*(\d{3,12})',
        r'N[º°]\s*0*(\d{3,12})',
        r'NFe?\s*N[º°]\s*S[ée]rie[\s\S]{0,40}?(\d{3,12})',
        r'N[ºo]\s*([\d\.]+)',
    ]
    for p in padroes_danfe:
        m = re.search(p, texto, re.IGNORECASE)
        if not m:
            continue
        n = (m.group(1) or '').strip().replace('.', '')
        if not n:
            continue
        if n in BLACKLIST_NF:
            continue
        return n

    padroes = [
        (r'N[úu]m\.?\s*do\s*documento\s*[:\s]*(?:NF[-]?)?\s*(\d+)', False),
        (r'N[úu]mero\s*do\s*documento\s*[:\s]*(?:NF[-]?)?\s*(\d+)', False),
        (r'N[úu]mero\s+Documento\s*[:\s]*(?:NF[-]?)?\s*(\d+)', False),
        (r'(?:Numero Documento|N[úu]mero do Documento)\s*(?:.*?\s+)?(\d+)(?:/\d+)?', False),
        (r'NF-(\d+)', True),
        (r'NF\s+(\d+)', True),
        (r'NF:\s*(\d+)', True),
        (r'NF(\d+)', True),
        (r'(\d+)/\d+\s+DM', False),
    ]
    for p, exige_nf in padroes:
        m = re.search(p, texto, re.IGNORECASE)
        if not m:
            continue
        n = m.group(1)
        if n in BLACKLIST_NF:
            continue
        if len(n) == 8 and not exige_nf:
            continue
        if len(n) < 3 or len(n) > 12:
            continue
        return n
    return None


def _extrair_nf_do_nome_arquivo(nome_arquivo):
    """Extrai número da NF diretamente do nome do arquivo.
    Procura sequências de 4-6 dígitos após termos como 'CB', 'BONIF', 'NF' ou após hífens.
    Exemplos: 'NF - CB - 12244...' → '12244', 'NF-BONIF-12345...' → '12345'
    
    Args:
        nome_arquivo: Nome do arquivo (ex: 'NF - CB - 12244 - CLIENTE.pdf')
    
    Returns:
        String com o número da NF encontrado ou None
    """
    if not nome_arquivo:
        return None
    
    # Padrões para extrair NF do nome do arquivo
    # Procura por: NF - CB - 12244, NF-BONIF-12345, NF - 12244, etc.
    padroes = [
        r'(?:NF\s*[-–—]?\s*)?(?:CB|BONIF)\s*[-–—]\s*(\d{4,6})',  # NF - CB - 12244 ou CB - 12244
        r'NF\s*[-–—]\s*(\d{4,6})',  # NF - 12244
        r'NF\s*[-–—]\s*\d+\s*[-–—]\s*(\d{4,6})',  # NF - 01 - 12244
        r'[-–—]\s*(\d{4,6})\s*[-–—]',  # - 12244 - (entre hífens)
    ]
    
    for padrao in padroes:
        m = re.search(padrao, nome_arquivo, re.IGNORECASE)
        if m:
            nf = m.group(1)
            # Validar que não está na blacklist e tem tamanho adequado
            if nf not in BLACKLIST_NF and 4 <= len(nf) <= 6:
                return nf
    
    # Fallback: primeira sequência numérica no nome (ex: NF3439.pdf -> 3439)
    nf_fallback = _extrair_numero_da_nf(nome_arquivo)
    if nf_fallback and nf_fallback not in BLACKLIST_NF and 4 <= len(nf_fallback) <= 6:
        return nf_fallback
    return None


def _extrair_numero_da_nf(nome_arquivo):
    """Extrai apenas a primeira sequência de dígitos do nome do arquivo.
    Ex: 'NF3439.pdf' -> '3439', 'NF - 12244 - CLIENTE.pdf' -> '12244' (se outros padrões falharem).
    """
    if not nome_arquivo:
        return None
    try:
        nome_limpo = str(nome_arquivo).lower().replace('.pdf', '')
        m = re.search(r'(\d+)', nome_limpo)
        return m.group(1) if m else None
    except Exception:
        return None


def _eh_linha_cabecalho_pagador(s):
    """Retorna True se s for linha de cabeçalho (Numero Documento, Vencimento, etc.)."""
    if not s or len(s) < 3:
        return True
    u = s.upper()
    if 'NUMERO' in u and 'DOCUMENTO' in u:
        return True
    if u.strip() in ('VENCIMENTO', 'CPF', 'CNPJ', 'PAGADOR', 'NOME', 'RAZÃO SOCIAL'):
        return True
    return False


def _limpar_razao_ate_cnpj_ou_data(linha):
    """Remove sufixo tipo '12341/1 26/02/2026' ou 'CNPJ 24.333.585/0001-20 27/01/2026'."""
    # Remove trecho final " NNN/N DD/MM/YYYY" ou " DD/MM/YYYY"
    linha = re.sub(r'\s+\d{1,5}/\d+\s+\d{1,2}/\d{1,2}/\d{2,4}\s*$', '', linha)
    linha = re.sub(r'\s+\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\s+\d{1,2}/\d{1,2}/\d{2,4}\s*$', '', linha)
    linha = re.sub(r'\s+\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\s*$', '', linha)
    return linha.strip()


def _extrair_razao_social(texto):
    """Extrai razão social do PAGADOR (boletos) ou DESTINATÁRIO (NF-e)."""
    # DANFE: priorizar bloco do destinatário para evitar capturar "Transportador / Volumes Transportados"
    bloco_dest = re.search(
        r'Destinat[áa]rio\s*/\s*Remetente([\s\S]{0,1200}?)(?:Endere[cç]o|CNPJ\s*/\s*CPF)',
        texto,
        re.IGNORECASE,
    )
    if bloco_dest:
        trecho = bloco_dest.group(1)
        linhas = [re.sub(r'\s+', ' ', linha).strip() for linha in re.split(r'[\r\n]+', trecho) if linha and linha.strip()]
        # Se houver cabeçalho "Nome/Razão Social", capturar a linha imediatamente abaixo
        for i, linha in enumerate(linhas):
            if re.search(r'Nome\s*/\s*Raz[ãa]o\s+Social', linha, re.IGNORECASE):
                if i + 1 < len(linhas):
                    cand = _limpar_razao_ate_cnpj_ou_data(linhas[i + 1])
                    if cand and len(cand) > 3 and not _eh_linha_cabecalho_pagador(cand):
                        return cand[:200]
        # Fallback no mesmo bloco: primeira linha textual plausível que não seja cabeçalho
        for linha in linhas:
            if re.search(r'Nome\s*/\s*Raz[ãa]o\s+Social|CNPJ|CPF|Insc', linha, re.IGNORECASE):
                continue
            cand = _limpar_razao_ate_cnpj_ou_data(linha)
            if cand and len(cand) > 3 and not _eh_linha_cabecalho_pagador(cand):
                return cand[:200]

    # DANFE (fallback do plano B): Nome + CNPJ + Data na mesma linha
    nome_danfe, _ = _extrair_linha_danfe_nome_cnpj_data(texto)
    if nome_danfe:
        razao = _limpar_razao_ate_cnpj_ou_data(nome_danfe)
        if razao and len(razao) > 2 and not _eh_linha_cabecalho_pagador(razao):
            return razao[:200]

    # Itaú/DESTAK: "Pagador: CAPIM FRIOS EIRELI" (mesma linha ou próxima)
    m = re.search(r'Pagador\s*:\s*([A-ZÁÉÍÓÚÇa-z0-9][A-ZÁÉÍÓÚÇa-z0-9\s&\.\-(),]+?)(?:\s*[\r\n]|CNPJ|CPF|$)', texto, re.IGNORECASE)
    if m:
        razao = _limpar_razao_ate_cnpj_ou_data(m.group(1))
        if razao and len(razao) > 2 and not _eh_linha_cabecalho_pagador(razao):
            return razao[:200]
    # Bradesco/PDF com texto colado: "PagadorS N SOARES... CPF/CNPJ ..."
    m = re.search(r'Pagador\s*([A-Za-z0-9\s\&\.\-\*]+?)\s*CPF/CNPJ', texto, re.IGNORECASE)
    if m:
        razao = _limpar_razao_ate_cnpj_ou_data((m.group(1) or '').strip())
        if razao and len(razao) > 2 and not _eh_linha_cabecalho_pagador(razao):
            return razao[:200]
    # NF-e: "NOME / RAZÃO SOCIAL ..." na linha seguinte "JNS COMERCIO ... LTDA 24.333.585/..."
    m = re.search(r'NOME\s*/\s*RAZ[ÃA]O\s+SOCIAL[\s\S]*?[\r\n]+\s*([A-ZÁÉÍÓÚÇa-z0-9][A-ZÁÉÍÓÚÇa-z0-9\s&\.\-(),]+?)\s+\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}', texto, re.IGNORECASE)
    if m:
        razao = _limpar_razao_ate_cnpj_ou_data(m.group(1))
        if razao and len(razao) > 3:
            return razao[:200]
    # Boletos: Pagador + linhas; pular "Numero Documento Vencimento" e pegar a seguinte
    pos = 0
    while True:
        m = re.search(r'Pagador\s*[\r\n]+', texto[pos:], re.IGNORECASE)
        if not m:
            break
        pos += m.end()
        resto = texto[pos:]
        linhas = re.split(r'[\r\n]+', resto)
        for linha in linhas:
            linha = re.sub(r'\s+', ' ', linha).strip()
            if not linha or _eh_linha_cabecalho_pagador(linha):
                continue
            if len(linha) > 3 and not re.match(r'^\d{2}\.\d{3}\.\d{3}', linha):
                return _limpar_razao_ate_cnpj_ou_data(linha)[:200]
        break
    m = re.search(r'Pagador\s+([A-ZÁÉÍÓÚÇa-z0-9][A-ZÁÉÍÓÚÇa-z0-9\s&\.\-(),]+?)(?:\s{2,}|\d{2}\.\d{3}\.\d{3}|[\r\n]|$)', texto, re.IGNORECASE)
    if m:
        razao = _limpar_razao_ate_cnpj_ou_data(m.group(1))
        if razao and len(razao) > 2 and not _eh_linha_cabecalho_pagador(razao):
            return razao[:200]
    for p in [
        r'Raz[ãa]o\s+Social[:\s]+([A-ZÁÉÍÓÚÇ][A-ZÁÉÍÓÚÇ\s&\.\-]+)',
        r'Nome\s+Empresarial[:\s]+([A-ZÁÉÍÓÚÇ][A-ZÁÉÍÓÚÇ\s&\.\-]+)',
        r'Empresa[:\s]+([A-ZÁÉÍÓÚÇ][A-ZÁÉÍÓÚÇ\s&\.\-]+)',
    ]:
        m = re.search(p, texto, re.IGNORECASE)
        if m:
            razao = m.group(1).strip()
            return razao[:200] if len(razao) > 200 else razao
    return None


def _extrair_data_vencimento(texto, debug_paty=False):
    """Extrai data de vencimento: ao lado de 'Vencimento' ou em PARCELAS (001 DD/MM/YYYY).
    Prioriza padrão dd/mm/aaaa após 'Vencimento' (ex: 05/02/2026, 08/02/2026).
    
    Args:
        texto: Texto extraído do PDF
        debug_paty: Se True, imprime debug detalhado no console (para boletos PATY/Bradesco)
    """
    if debug_paty:
        app.logger.info("\n" + "="*80)
        app.logger.debug("DEBUG EXTRAÇÃO DE VENCIMENTO - BOLETO PATY/BRADESCO")
        app.logger.info("="*80)
        app.logger.info("TEXTO COMPLETO DO PDF (primeiros 2000 caracteres):")
        app.logger.info(texto[:2000])
        app.logger.info("\n" + "-"*80)
    
    # Padrões mais flexíveis e abrangentes
    padroes = [
        # Padrão flexível: aceita "Vencimento", "Data de Vencimento" com separadores variados
        r'(?:Vencimento|Data\s+de\s+Vencimento)[\s\n]*[:\-]?[\s\n]*(\d{2}/\d{2}/\d{4})',
        
        # Padrões específicos mantidos para compatibilidade
        r'Vencimento[\s\n]*(\d{2}/\d{2}/\d{4})',  # ex: Vencimento 05/02/2026 ou Vencimento\n05/02/2026
        r'Vencimento\s*[:\s]+\s*(\d{1,2}/\d{1,2}/\d{2,4})',  # Itaú: Vencimento: 08/02/2026
        r'Vencimento[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})',
        r'Vencimento\s*[\r\n]+\s*(\d{1,2}/\d{1,2}/\d{2,4})',
        r'Venc\.?[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})',
        r'Data\s+de\s+Vencimento[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})',
        
        # Bradesco: data pode aparecer isolada após "Vencimento" em linhas separadas
        r'Vencimento[\s\S]{0,50}?(\d{2}/\d{2}/\d{4})',  # Busca data até 50 chars após "Vencimento"
        
        # PARCELAS (formato de recibo com parcelas)
        r'PARCELAS\s*[\r\n]+\s*\d+\s+(\d{1,2}/\d{1,2}/\d{2,4})\s',  # 001 26/02/2026 R$
        
        # Busca genérica: qualquer data dd/mm/yyyy precedida por "Vencimento" em até 100 caracteres
        r'Vencimento[\s\S]{0,100}?(\d{2}/\d{2}/\d{4})',
    ]
    
    for idx, p in enumerate(padroes):
        m = re.search(p, texto, re.IGNORECASE | re.DOTALL)
        if m:
            data_str = m.group(1)
            if debug_paty:
                app.logger.info(f"\n✓ Match encontrado com padrão {idx}: {p}")
                app.logger.info(f"  Data capturada: {data_str}")
                app.logger.info(f"  Contexto: ...{texto[max(0, m.start()-50):m.end()+50]}...")
            
            data_parsed, _ = _parse_data_flex(data_str)
            if data_parsed:
                if debug_paty:
                    app.logger.info(f"  ✓ Data parseada com sucesso: {data_parsed.strftime('%d/%m/%Y')}")
                    app.logger.info("="*80 + "\n")
                return data_parsed
            elif debug_paty:
                app.logger.error(f"  ✗ Falha ao parsear data: {data_str}")
    
    if debug_paty:
        app.logger.error("\n✗ NENHUMA DATA DE VENCIMENTO ENCONTRADA")
        app.logger.info("="*80 + "\n")
    
    return None


def _detectar_empresa_destak(texto):
    """Detecta se o beneficiário é DESTAK. Retorna True se encontrar 'DESTAK EMBALAGEM LTDA' ou CNPJ 30.820.528/0001-78."""
    if 'DESTAK EMBALAGEM LTDA' in texto.upper() or CNPJ_DESTAK in texto:
        return True
    return False


def _parse_valor_monetario(s):
    """Converte string 'R$ 1.234,56' ou '-R$ 120,00' para float. Retorna None se inválido. Preserva sinal negativo."""
    if not s or not isinstance(s, str):
        return None
    s = str(s).strip()
    negativo = s.lstrip().startswith('-')
    s = re.sub(r'R\$\s*', '', s, flags=re.IGNORECASE).replace(' ', '')
    s = s.lstrip('-').strip()
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    try:
        n = float(s)
        return -n if negativo else n
    except (ValueError, TypeError):
        return None


def _extrair_valor_boleto(texto):
    """Extrai valor principal do boleto (ex.: R$ 2.400,00) e, para DANFE, o Valor Total da Nota."""
    # DANFE: ancorar no rótulo "Valor Total da Nota" para não capturar zeros de impostos
    bloco_total_nota = re.search(
        r'Valor\s+Total\s+da\s+Nota([\s\S]{0,220})',
        texto,
        re.IGNORECASE
    )
    if bloco_total_nota:
        trecho = bloco_total_nota.group(1)
        candidatos = re.findall(r'(\d{1,3}(?:\.\d{3})*,\d{2})', trecho)
        if candidatos:
            # Usa o último valor não-zero do bloco (normalmente o total final da nota)
            for raw in reversed(candidatos):
                v = _parse_valor_monetario(raw)
                if v is not None and v > 0:
                    return v

    padroes = [
        r'Valor\s*(?:do\s*Documento)?\s*[:\s]*R\$\s*([\d\.]+,\d{2})',
        r'Valor\s*[:\s]*R\$\s*([\d\.]+,\d{2})',
        r'Total\s*[:\s]*R\$\s*([\d\.]+,\d{2})',
        r'(?:Valor|Total)\s+([\d\.]+,\d{2})\b',
    ]
    for p in padroes:
        m = re.search(p, texto, re.IGNORECASE)
        if m:
            raw = m.group(1)
            v = _parse_valor_monetario(raw)
            if v is not None and v > 0:
                return v
    for m in re.finditer(r'R\$\s*([\d\.]+,\d{2})', texto):
        v = _parse_valor_monetario(m.group(1))
        if v is not None and v > 0:
            return v
    return None


def _extrair_texto_primeira_pagina(caminho_arquivo):
    """Extrai o texto apenas da primeira página do PDF. Retorna str (vazia se erro ou sem páginas)."""
    try:
        with pdfplumber.open(caminho_arquivo) as pdf:
            if not pdf.pages:
                return ""
            t = pdf.pages[0].extract_text()
            return (t or "").strip()
    except Exception:
        return ""


def _classificar_pdf(texto):
    """Classifica um PDF pelo texto da primeira página.
    Retorna 'NOTA_FISCAL', 'BOLETO' ou 'NAO_IDENTIFICADO'."""
    u = (texto or "").upper()
    if "DANFE" in u or "NOTA FISCAL" in u:
        return "NOTA_FISCAL"
    if "BOLETO" in u or "LINHA DIGITÁVEL" in u or "LINHA DIGITAVEL" in u:
        return "BOLETO"
    if "ITAU" in u or "ITAÚ" in u or "BRADESCO" in u:
        return "BOLETO"
    return "NAO_IDENTIFICADO"


def organizar_arquivos():
    """Organiza PDFs na raiz de documentos_entrada/ movendo para subpastas por tipo.
    
    - NOTA_FISCAL (DANFE / NOTA FISCAL) -> documentos_entrada/notas_fiscais/
    - BOLETO (BOLETO / LINHA DIGITÁVEL / Itaú / Bradesco) -> documentos_entrada/boletos/
    - Resto -> documentos_entrada/nao_identificados/
    
    Usa apenas a primeira página para classificação. shutil.move para mover no Mac.
    Suporta 50+ arquivos em lote sem travar.
    
    Returns:
        dict: {'notas_fiscais': int, 'boletos': int, 'nao_identificados': int, 'erros': int}
    """
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "documentos_entrada")
    root_pdf = [f for f in os.listdir(base) if f.lower().endswith(".pdf")]
    
    notas = os.path.join(base, "notas_fiscais")
    boletos = os.path.join(base, "boletos")
    outros = os.path.join(base, "nao_identificados")
    for d in (notas, boletos, outros):
        os.makedirs(d, exist_ok=True)
    
    bonificacoes = os.path.join(base, "bonificacoes")
    os.makedirs(bonificacoes, exist_ok=True)
    
    out = {"notas_fiscais": 0, "boletos": 0, "nao_identificados": 0, "bonificacoes": 0, "erros": 0}
    
    for nome in root_pdf:
        src = os.path.join(base, nome)
        if not os.path.isfile(src):
            continue
        texto = _extrair_texto_primeira_pagina(src)
        
        # Verificar se é bonificação ANTES de classificar
        if _detectar_bonificacao(texto):
            dst = os.path.join(bonificacoes, nome)
            try:
                shutil.move(src, dst)
                out["bonificacoes"] += 1
                app.logger.info(f"[ORGANIZAR] Bonificação movida: {nome}")
                continue
            except Exception as e:
                app.logger.error(f"[ORGANIZAR] Erro ao mover bonificação {nome}: {e}")
                out["erros"] += 1
                continue
        
        tipo = _classificar_pdf(texto)
        if tipo == "NOTA_FISCAL":
            dst_dir = notas
        elif tipo == "BOLETO":
            dst_dir = boletos
        else:
            dst_dir = outros
        dst = os.path.join(dst_dir, nome)
        try:
            shutil.move(src, dst)
            if tipo == "NOTA_FISCAL":
                out["notas_fiscais"] += 1
            elif tipo == "BOLETO":
                out["boletos"] += 1
            else:
                out["nao_identificados"] += 1
        except Exception:
            out["erros"] += 1
    
    return out


def _detectar_bonificacao(texto):
    """
    Detecta se um documento é uma nota de bonificação/brinde.
    
    Lê o conteúdo do PDF e verifica a "Natureza da Operação" procurando por frases exatas
    que indicam bonificação (baseado nas notas da Paty e Destak).
    
    Args:
        texto: Texto extraído do PDF (primeira página ou completo)
    
    Returns:
        bool: True se for bonificação, False caso contrário
    """
    if not texto:
        return False
    
    # Normalizar texto: remover acentos e converter para maiúsculas
    texto_normalizado = texto.upper()
    
    # Remover acentos básicos
    substituicoes = {
        'Á': 'A', 'À': 'A', 'Â': 'A', 'Ã': 'A',
        'É': 'E', 'Ê': 'E',
        'Í': 'I',
        'Ó': 'O', 'Ô': 'O', 'Õ': 'O',
        'Ú': 'U', 'Ü': 'U',
        'Ç': 'C'
    }
    for acento, sem_acento in substituicoes.items():
        texto_normalizado = texto_normalizado.replace(acento, sem_acento)
    
    # Frases exatas que indicam bonificação (baseado nas notas da Paty e Destak)
    frases_bonificacao = [
        'REMESSA EM BONIFICACAO',
        'REMESSA DE BONIFICACAO',
        'DOACAO',
        'BRINDE'
    ]
    
    # PRIORIDADE 1: Procurar por "Natureza da Operação" seguido de bonificação
    # Padrão flexível para capturar variações: "Natureza da Operação:", "Natureza da Operação", etc.
    padrao_natureza = re.compile(
        r'NATUREZA\s+DA\s+OPERACAO[:\s]*([^\n]{0,300})',
        re.IGNORECASE | re.MULTILINE
    )
    match_natureza = padrao_natureza.search(texto_normalizado)
    
    if match_natureza:
        natureza_texto = match_natureza.group(1).upper()
        # Remover acentos do texto da natureza
        for acento, sem_acento in substituicoes.items():
            natureza_texto = natureza_texto.replace(acento, sem_acento)
        
        # Verificar se contém alguma das frases exatas de bonificação
        for frase in frases_bonificacao:
            if frase in natureza_texto:
                return True
    
    # PRIORIDADE 2: Verificar no texto completo se alguma frase aparece próxima de "Natureza" ou "Operação"
    # Isso captura casos onde a estrutura do PDF pode ter quebrado a linha
    for frase in frases_bonificacao:
        # Procurar a frase no texto
        if frase in texto_normalizado:
            # Verificar se está próxima (até 150 caracteres) de "Natureza" ou "Operação"
            padrao_proximo = re.compile(
                r'(NATUREZA|OPERACAO).{0,150}?' + re.escape(frase),
                re.IGNORECASE | re.DOTALL
            )
            if padrao_proximo.search(texto_normalizado):
                return True
    
    return False


def _mover_para_bonificacoes(caminho_arquivo):
    """
    Move um arquivo PDF (e XML se existir) para a pasta bonificacoes.
    
    Args:
        caminho_arquivo: Caminho completo do arquivo PDF
    
    Returns:
        tuple: (sucesso: bool, caminho_destino: str ou None, mensagem: str)
    """
    try:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        pasta_bonificacoes = os.path.join(base_dir, 'documentos_entrada', 'bonificacoes')
        os.makedirs(pasta_bonificacoes, exist_ok=True)
        
        nome_arquivo = os.path.basename(caminho_arquivo)
        caminho_destino = os.path.join(pasta_bonificacoes, nome_arquivo)
        
        # Mover PDF
        shutil.move(caminho_arquivo, caminho_destino)
        
        # Tentar mover XML correspondente se existir
        nome_base, _ = os.path.splitext(caminho_arquivo)
        caminho_xml = nome_base + '.xml'
        if os.path.exists(caminho_xml):
            nome_xml = os.path.basename(caminho_xml)
            caminho_xml_destino = os.path.join(pasta_bonificacoes, nome_xml)
            shutil.move(caminho_xml, caminho_xml_destino)
        
        return True, caminho_destino, f"Arquivo movido para bonificacoes: {nome_arquivo}"
    except Exception as e:
        return False, None, f"Erro ao mover arquivo para bonificacoes: {str(e)}"


def _processar_pdf(caminho_arquivo, tipo_documento):
    """Processa um arquivo PDF e extrai informações relevantes.
    
    Args:
        caminho_arquivo: Caminho completo do arquivo PDF
        tipo_documento: 'BOLETO' ou 'NOTA_FISCAL'
    
    Returns:
        dict com campos: cnpj, numero_nf, razao_social, data_vencimento, empresa_destak, apenas_emissor (ou None se erro)
    """
    try:
        nome_arquivo = os.path.basename(caminho_arquivo)
        with pdfplumber.open(caminho_arquivo) as pdf:
            texto_completo = ""
            for pagina in pdf.pages:
                h = float(pagina.height) or 842
                w = float(pagina.width) or 595
                crop_bottom = max(0, h * 0.75)
                if crop_bottom <= 0:
                    cropped = pagina
                else:
                    cropped = pagina.crop((0, 0, w, crop_bottom))
                texto_pagina = cropped.extract_text()
                if texto_pagina:
                    texto_completo += texto_pagina + "\n"
        
        # Extrai NF, vencimento, valor do boleto, etc.
        padrao_cnpj = r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}'
        todos_cnpjs = re.findall(padrao_cnpj, texto_completo)
        apenas_emissor = len(todos_cnpjs) > 0 and len([c for c in todos_cnpjs if c not in CNPJS_EMISSORES]) == 0
        
        # Tentar extrair NF do PDF primeiro
        numero_nf = _extrair_numero_nf(texto_completo)
        
        # Se não encontrou no PDF, tentar extrair do nome do arquivo (fallback para arquivos CB/BONIF)
        if not numero_nf:
            numero_nf = _extrair_nf_do_nome_arquivo(nome_arquivo)
            if numero_nf:
                app.logger.debug(f"DEBUG: NF extraída do nome do arquivo: {numero_nf} (arquivo: {nome_arquivo})")
        
        # Detectar se é boleto PATY/Bradesco para ativar debug
        eh_paty_bradesco = (
            'BRADESCO' in texto_completo.upper() or 
            'PATY' in texto_completo.upper() or
            'CNPJ_PATY' in texto_completo.upper() or
            not _detectar_empresa_destak(texto_completo)  # Se não é DESTAK, provavelmente é PATY
        )
        
        debug_vencimento = eh_paty_bradesco and tipo_documento == 'BOLETO'
        
        resultado = {
            'cnpj': _extrair_cnpj(texto_completo, nome_arquivo=nome_arquivo),
            'numero_nf': numero_nf,
            'razao_social': _extrair_razao_social(texto_completo),
            'data_vencimento': _extrair_data_vencimento(texto_completo, debug_paty=debug_vencimento),
            'empresa_destak': _detectar_empresa_destak(texto_completo),
            'valor_boleto': _extrair_valor_boleto(texto_completo),
            'apenas_emissor': apenas_emissor,
        }
        return resultado
    except Exception as e:
        app.logger.error(f"Erro ao processar PDF {caminho_arquivo}: {str(e)}")
        return None


def _processar_documento(caminho_arquivo, user_id_forcado=None):
    """Processa um único documento: move para documentos_entrada, organiza e processa."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "documentos_entrada")
    os.makedirs(base, exist_ok=True)
    nome = os.path.basename(caminho_arquivo)
    destino = os.path.join(base, nome)
    shutil.move(caminho_arquivo, destino)
    organizar_arquivos()
    _processar_documentos_pendentes(user_id_forcado=user_id_forcado)


def _processar_documentos_pendentes(capturar_logs_memoria=False, user_id_forcado=None):
    """Verifica as pastas de documentos e processa novos arquivos PDF que ainda não foram registrados.
    
    Args:
        capturar_logs_memoria: Se True, captura logs em uma lista para retorno (usado pelo endpoint de debug)
        user_id_forcado: Se informado, usa este ID como usuario_id ao criar Documento (ex: da thread background)
    
    Returns:
        dict com: {'processados': int, 'erros': int, 'mensagens': list, 'logs': list (se capturar_logs_memoria=True)}
    """
    # Lista para capturar logs em memória (usado pelo endpoint de debug)
    logs_memoria = []
    
    def _log_detalhado(msg):
        """Imprime no console e captura em memória quando solicitado; sem I/O de arquivo."""
        app.logger.info(msg)
        if capturar_logs_memoria:
            logs_memoria.append(msg)
    
    # Log inicial para confirmar execução
    try:
        _log_detalhado(f"\n{'='*80}")
        _log_detalhado("=== PROCESSAMENTO DE DOCUMENTOS PENDENTES INICIADO ===")
        _log_detalhado(f"Arquivo de log: {log_detalhado_path}")
        _log_detalhado(f"Arquivo existe: {os.path.exists(log_detalhado_path)}")
        _log_detalhado(f"{'='*80}\n")
    except Exception as e:
        app.logger.error(f"ERRO no log inicial: {e}")
        traceback.print_exc()
    
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'documentos_entrada')
    pastas = {
        'BOLETO': os.path.join(base_dir, 'boletos'),
        'NOTA_FISCAL': os.path.join(base_dir, 'notas_fiscais'),
    }
    
    resultado = {'processados': 0, 'erros': 0, 'vinculos_novos': 0, 'mensagens': []}
    
    
    for tipo, pasta in pastas.items():
        if not os.path.exists(pasta):
            os.makedirs(pasta, exist_ok=True)
            continue
        
        # Lista todos os PDFs na pasta
        arquivos_pdf = [f for f in os.listdir(pasta) if f.lower().endswith('.pdf')]
        app.logger.debug(f"DEBUG: Processando {len(arquivos_pdf)} arquivo(s) do tipo {tipo}")
        
        for arquivo in arquivos_pdf:
            caminho_completo = os.path.join(pasta, arquivo)
            caminho_relativo = os.path.join('documentos_entrada', 'boletos' if tipo == 'BOLETO' else 'notas_fiscais', arquivo)
            
            # Verificar se é bonificação ANTES de processar (apenas para notas fiscais)
            if tipo == 'NOTA_FISCAL':
                texto_primeira_pagina = _extrair_texto_primeira_pagina(caminho_completo)
                if _detectar_bonificacao(texto_primeira_pagina):
                    # Mover arquivo para pasta bonificacoes
                    sucesso, caminho_destino, mensagem = _mover_para_bonificacoes(caminho_completo)
                    if sucesso:
                        app.logger.info(f"[BONIFICACAO] {mensagem}")
                        # Se havia documento no banco, removê-lo
                        doc_existente = Documento.query.filter_by(caminho_arquivo=caminho_relativo).first()
                        if doc_existente:
                            _deletar_cloudinary_seguro(
                                public_id=getattr(doc_existente, 'public_id', None),
                                url=getattr(doc_existente, 'url_arquivo', None),
                                resource_type='raw'
                            )
                            db.session.delete(doc_existente)
                            db.session.commit()
                        resultado['mensagens'].append(f"Bonificação movida: {arquivo}")
                    else:
                        app.logger.error(f"[BONIFICACAO] ERRO: {mensagem}")
                        resultado['erros'] += 1
                    continue  # Pular processamento deste arquivo
            
            # Inicializar variáveis
            documento = None
            dados_extraidos = None
            
            # Verifica se já foi processado E vinculado (permite re-processar documentos não vinculados)
            doc_existente = Documento.query.filter_by(caminho_arquivo=caminho_relativo).first()
            # CORREÇÃO: Só pular se documento existe E está vinculado. Permite re-processar documentos não vinculados.
            if doc_existente and doc_existente.venda_id is not None:
                app.logger.debug(f"DEBUG: Arquivo {arquivo} já processado e vinculado (Venda ID {doc_existente.venda_id}), pulando")
                continue
            elif doc_existente and doc_existente.venda_id is None:
                app.logger.debug(f"DEBUG: Documento ID {doc_existente.id} existe mas não está vinculado. Re-processando para tentar vincular.")
                documento = doc_existente
                if not documento.url_arquivo and (os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL')):
                    try:
                        resultado_nuvem = cloudinary.uploader.upload(caminho_completo, resource_type='raw', timeout=_EXTERNAL_TIMEOUT)
                        documento.url_arquivo = resultado_nuvem.get('secure_url')
                        documento.public_id = resultado_nuvem.get('public_id')
                        db.session.flush()
                    except Exception as ex:
                        app.logger.error(f"Erro ao fazer upload para Cloudinary (doc existente): {ex}")
                nf_cached = (getattr(doc_existente, 'nf_extraida', None) or doc_existente.numero_nf)
                nf_cached = (nf_cached or '').strip() or None
                if nf_cached:
                    # Cache OCR: usar nf_extraida/numero_nf armazenado, não rodar OCR de novo
                    dados_extraidos = {
                        'numero_nf': nf_cached,
                        'cnpj': doc_existente.cnpj,
                        'razao_social': doc_existente.razao_social,
                        'data_vencimento': doc_existente.data_vencimento,
                    }
                else:
                    dados_extraidos = _processar_pdf(caminho_completo, tipo)
                    if dados_extraidos is None:
                        resultado['erros'] += 1
                        resultado['mensagens'].append(f"Erro ao re-processar {arquivo}")
                        continue
                    documento.cnpj = dados_extraidos.get('cnpj')
                    documento.numero_nf = dados_extraidos.get('numero_nf')
                    documento.razao_social = dados_extraidos.get('razao_social')
                    documento.data_vencimento = dados_extraidos.get('data_vencimento')
                    nf_val = dados_extraidos.get('numero_nf')
                    documento.nf_extraida = nf_val
                    documento.data_processamento = date.today()
                    db.session.flush()
                # dados_extraidos pronto; continuar com a lógica de vínculo abaixo
            else:
                # Documento não existe, processar PDF normalmente (sempre roda OCR)
                dados_extraidos = _processar_pdf(caminho_completo, tipo)
                if dados_extraidos is None:
                    resultado['erros'] += 1
                    resultado['mensagens'].append(f"Erro ao processar {arquivo}")
                    continue
                # documento será criado abaixo no bloco try
            
            # Vínculo 100% automático APENAS por NF (normalizada, sem zeros à esquerda). NF é a única chave.
            venda_id = None
            venda_match = None
            nf = dados_extraidos.get('numero_nf')
            # LOG DETALHADO: Início da análise
            _log_detalhado(f"\n{'='*80}")
            _log_detalhado(f"--- Analisando Documento: {arquivo} (NF Extraída: '{nf}') ---")
            _log_detalhado(f"{'='*80}")
            _log_detalhado(f"DEBUG: Iniciando busca para NF [{nf}] do arquivo {arquivo}")
            if nf:
                nf_str = str(nf).strip()
                nf_limpa = _normalizar_nf(nf_str)
                # Exceção: NFs inválidas não vinculam automaticamente
                nfs_invalidas = ('S/N', '0', 'Falta_nota', '')
                if nf_limpa and nf_limpa not in nfs_invalidas:
                    # Buscar TODAS as vendas e normalizar suas NFs para comparação
                    # Isso garante que encontramos vendas mesmo com formatos diferentes (zeros à esquerda, prefixos, etc.)
                    todas_vendas = Venda.query.filter(
                        Venda.nf.isnot(None)
                    ).options(
                        joinedload(Venda.cliente), joinedload(Venda.produto)
                    ).all()
                    vendas_candidatas = []
                    for v in todas_vendas:
                        if v.nf:
                            nf_venda_norm = _normalizar_nf(str(v.nf))
                            if nf_venda_norm == nf_limpa:
                                vendas_candidatas.append(v)
                    
                    # Log das variantes tentadas (para debug)
                    variants = [nf_limpa, 'NF-' + nf_limpa, 'NF' + nf_limpa, 'NF ' + nf_limpa]
                    for pad in (4, 5, 6, 7, 8, 9):
                        z = nf_limpa.zfill(pad)
                        if z != nf_limpa:
                            variants.append(z)
                            variants.append('NF-' + z)
                    variants = list(dict.fromkeys(variants))
                    _log_detalhado(f"Variantes tentadas na busca: {variants[:10]}... (total: {len(variants)})")
                    # LOG DETALHADO: Quantidade de vendas encontradas
                    _log_detalhado(f"Vendas localizadas com NF '{nf_limpa}': {len(vendas_candidatas)}")
                    _log_detalhado(f"DEBUG: Vendas encontradas no banco: {[v.id for v in vendas_candidatas]} (NF normalizada: '{nf_limpa}')")
                    
                    # LOG DETALHADO: Detalhes de cada venda encontrada
                    for idx, v in enumerate(vendas_candidatas, 1):
                        caminho_boleto_atual = (v.caminho_boleto or '').strip() if tipo == 'BOLETO' else None
                        caminho_nf_atual = (v.caminho_nf or '').strip() if tipo == 'NOTA_FISCAL' else None
                        caminho_atual = caminho_boleto_atual or caminho_nf_atual or 'Nenhum'
                        doc_existe = None
                        if caminho_atual != 'Nenhum':
                            doc_existe = Documento.query.filter_by(caminho_arquivo=caminho_atual).first()
                        _log_detalhado(f"  [{idx}] ID: {v.id} | Cliente: {v.cliente.nome_cliente} | NF Banco: '{v.nf}' | Já tem doc?: {caminho_atual} | Doc existe?: {doc_existe is not None}")
                    
                    # Filtrar apenas vendas que batem na NF normalizada
                    # PERMITE vincular mesmo se já tiver outro tipo de documento (ex: já tem boleto, mas pode vincular NF)
                    # VERIFICA se o documento vinculado realmente existe no banco antes de bloquear
                    vendas_validas = []
                    _log_detalhado("\n--- Comparação Detalhada de NFs (Detecção de Espaços Invisíveis) ---")
                    for v in vendas_candidatas:
                        nf_venda_raw = v.nf or ''
                        nf_venda_norm = _normalizar_nf(str(nf_venda_raw))
                        # LOG DETALHADO: Comparação com aspas para detectar espaços invisíveis
                        _log_detalhado(f"Comparando Boleto['{nf_str}'] (normalizado: '{nf_limpa}') com Banco['{nf_venda_raw}'] (normalizado: '{nf_venda_norm}')")
                        if nf_venda_norm != nf_limpa:
                            _log_detalhado(f"  ⚠️ DIVERGÊNCIA ENCONTRADA: NF normalizada do boleto ('{nf_limpa}') != NF normalizada do banco ('{nf_venda_norm}')")
                        if nf_venda_norm == nf_limpa:
                            # Verificar se já tem o MESMO tipo de documento vinculado E se o documento existe
                            caminho_existente = None
                            if tipo == 'BOLETO':
                                caminho_existente = (v.caminho_boleto or '').strip()
                            elif tipo == 'NOTA_FISCAL':
                                caminho_existente = (v.caminho_nf or '').strip()
                            
                            # Se tem caminho preenchido, verificar se o documento realmente existe
                            documento_existe = False
                            if caminho_existente:
                                doc_existente = Documento.query.filter_by(caminho_arquivo=caminho_existente).first()
                                documento_existe = (doc_existente is not None)
                                
                                # Se o documento não existe mais, limpar o campo (vínculo fantasma)
                                if not documento_existe:
                                    app.logger.debug(f"DEBUG: Limpando vínculo fantasma: {tipo} '{caminho_existente}' não existe mais no banco")
                                    if tipo == 'BOLETO':
                                        v.caminho_boleto = None
                                    elif tipo == 'NOTA_FISCAL':
                                        v.caminho_nf = None
                                    db.session.flush()
                            
                            # FORÇAR SOBRESCRITA: Se encontrou NF idêntica, sempre permite vincular (substitui o antigo)
                            # Isso resolve o problema de vínculos órfãos e permite atualização automática
                            vendas_validas.append(v)
                            if caminho_existente and documento_existe:
                                app.logger.debug(f"DEBUG: NF idêntica encontrada. Substituindo vínculo antigo: {caminho_existente} → {caminho_relativo}")
                            elif caminho_existente and not documento_existe:
                                app.logger.debug(f"DEBUG: Limpando vínculo órfão e vinculando novo documento: {caminho_relativo}")

                    # Deduplicação defensiva:
                    # 1) por ID da venda (joins podem repetir a mesma linha)
                    # 2) por pedido lógico (cliente + NF), evitando "visão dupla"
                    #    em pedidos com múltiplos itens.
                    vendas_validas = _deduplicar_vendas_por_pedido(vendas_validas)
                    
                    # REGRA ÚNICA: Se houver EXATAMENTE UMA venda válida, vincular IMEDIATAMENTE (SOBRESCRITA AUTOMÁTICA)
                    _log_detalhado(f"\n--- Resultado da Filtragem: {len(vendas_validas)} venda(s) válida(s) encontrada(s) ---")
                    if len(vendas_validas) == 1:
                        venda_match = vendas_validas[0]
                        venda_id = vendas_validas[0].id
                        cliente_nome = venda_match.cliente.nome_cliente
                        
                        _log_detalhado(f"✅ VENDA ÚNICA ENCONTRADA: ID={venda_id}, Cliente='{cliente_nome}', NF='{venda_match.nf}'")
                        
                        # Verificar conflito de empresa (se aplicável) - DESABILITADO: vínculo apenas por NF
                        empresa_doc_bool = dados_extraidos.get('empresa_destak', False)
                        empresa_doc_nome = 'DESTAK' if empresa_doc_bool else 'PATY'
                        empresa_venda = venda_match.empresa_faturadora
                        if empresa_venda:
                            empresa_venda_upper = empresa_venda.upper()
                            empresa_doc_upper = empresa_doc_nome.upper()
                            if empresa_doc_upper != empresa_venda_upper:
                                mensagem_conflito = f"Achei a NF {nf_str}, mas ela pertence à empresa {empresa_venda} e o {tipo.lower()} lido é da {empresa_doc_nome}. VINCULANDO MESMO ASSIM (override por NF)."
                                app.logger.warning(f"DEBUG: ⚠️ CONFLITO DE EMPRESA DETECTADO MAS IGNORADO: {mensagem_conflito}")
                                resultado['mensagens'].append(f"⚠️ {mensagem_conflito}")
                                # Mesmo com conflito, permite vínculo (override) - REGRA: NF é soberana
                                app.logger.warning("DEBUG: ⚠️ SOBRESCREVENDO apesar do conflito de empresa (NF é a única chave)")
                        
                        app.logger.debug(f"DEBUG: ✅ VÍNCULO AUTOMÁTICO FORÇADO (SOBRESCRITA): NF '{nf_limpa}' → Venda {venda_id} (Cliente: {cliente_nome})")
                        # FORÇAR vínculo: não há retorno prematuro, vai direto para o commit
                    elif len(vendas_validas) > 1:
                        # Regra de negócio (1-para-1): com múltiplas vendas para a mesma NF
                        # nunca vincula automaticamente. Exige decisão manual no Dashboard.
                        _log_detalhado(
                            f"⚠️ MÚLTIPLAS VENDAS ENCONTRADAS ({len(vendas_validas)}) para a NF '{nf_limpa}': requer seleção manual."
                        )
                        clientes_lista = [v.cliente.nome_cliente for v in vendas_validas[:3]]
                        mensagem_diag = (
                            f"Achei {len(vendas_validas)} vendas com a NF {nf_str}. "
                            f"Por segurança, escolha manualmente em qual delas devo vincular este {tipo.lower()}."
                        )
                        _log_detalhado(f"DEBUG: ⚠️ AMBIGUIDADE: {mensagem_diag}")
                        resultado['mensagens'].append(
                            f"⚠️ {mensagem_diag} Clientes: {', '.join(clientes_lista)}{'...' if len(vendas_validas) > 3 else ''}"
                        )
                        venda_match = None
                        venda_id = None
                    else:
                        _log_detalhado(f"❌ NENHUMA VENDA VÁLIDA ENCONTRADA para NF '{nf_limpa}'")
                        # Verificar se há vendas com documento já vinculado
                        vendas_com_doc = []
                        for v in vendas_candidatas:
                            nf_venda_raw = v.nf or ''
                            nf_venda_norm = _normalizar_nf(str(nf_venda_raw))
                            if nf_venda_norm == nf_limpa:
                                vendas_com_doc.append(v)
                        
                        if vendas_com_doc:
                            v_com_doc = vendas_com_doc[0]
                            caminho_doc = (v_com_doc.caminho_boleto if tipo == 'BOLETO' else v_com_doc.caminho_nf) or ''
                            doc_existente = Documento.query.filter_by(caminho_arquivo=caminho_doc).first() if caminho_doc else None
                            if doc_existente:
                                mensagem_diag = f"Não vinculei a NF {nf_str} porque a venda ID {v_com_doc.id} já aponta para um documento (ID {doc_existente.id}). Se o ícone não aparece, esse vínculo pode estar órfão ou quebrado."
                            else:
                                mensagem_diag = f"Não vinculei a NF {nf_str} porque a venda ID {v_com_doc.id} já tem um documento vinculado, mas o documento não existe mais no banco de dados (vínculo órfão)."
                            _log_detalhado(f"DEBUG: ⚠️ DOCUMENTO JÁ VINCULADO: {mensagem_diag}")
                            resultado['mensagens'].append(f"ℹ️ {mensagem_diag}")
                        else:
                            # NF foi lida mas não existe venda correspondente
                            mensagem_erro = f"Erro: NF {nf_str} lida, mas esta venda não foi importada na planilha. Verifique se você já importou a planilha de vendas deste período."
                            _log_detalhado(f"DEBUG: ⚠️ NF '{nf_limpa}' não encontrada em nenhuma venda.")
                            resultado['mensagens'].append(f"⚠️ {mensagem_erro}")
            
            # Cria ou atualiza registro no banco
            try:
                # Se documento já existe (não vinculado), foi atualizado acima. Senão, criar novo.
                if documento is None:
                    nf_val = dados_extraidos.get('numero_nf')
                    usuario_id = user_id_forcado if user_id_forcado else (current_user.id if current_user.is_authenticated else None)
                    url_arquivo = None
                    public_id = None

                    # OBRIGATÓRIO: Fazer o upload para a nuvem ANTES de salvar no banco
                    if os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL'):
                        try:
                            resultado_nuvem = cloudinary.uploader.upload(caminho_completo, resource_type='raw', timeout=_EXTERNAL_TIMEOUT)
                            url_arquivo = resultado_nuvem.get('secure_url')
                            public_id = resultado_nuvem.get('public_id')
                            app.logger.info(f"✅ Sucesso Nuvem: {url_arquivo}")
                        except Exception as ex:
                            app.logger.error(f"❌ ERRO GRAVE Nuvem: {ex}")
                    else:
                        app.logger.warning("⚠️ Cloudinary não configurado. Salvando sem URL.")

                    documento = Documento(
                        caminho_arquivo=caminho_relativo,
                        url_arquivo=url_arquivo,
                        public_id=public_id,
                        tipo=tipo,
                        cnpj=dados_extraidos.get('cnpj'),
                        numero_nf=nf_val,
                        nf_extraida=nf_val,
                        razao_social=dados_extraidos.get('razao_social'),
                        data_vencimento=dados_extraidos.get('data_vencimento'),
                        venda_id=venda_id,
                        usuario_id=usuario_id,
                        empresa_id=_empresa_id_para_documento(
                            venda_id=venda_id, fallback_user_id=usuario_id
                        ),
                        data_processamento=date.today()
                    )
                    db.session.add(documento)
                    db.session.flush()
                _log_detalhado(f"DEBUG: Documento {'atualizado' if doc_existente else 'criado'}: ID={documento.id}, venda_id={venda_id}, venda_match={venda_match is not None}")
                
                # FORÇAR VÍNCULO: Se encontrou exatamente 1 venda válida, vincular IMEDIATAMENTE
                if venda_id and venda_match:
                    path_rel = caminho_relativo
                    cliente_nome = venda_match.cliente.nome_cliente
                    nf_doc = dados_extraidos.get('numero_nf') or 'N/A'
                    documento.venda_id = venda_id  # doc existente: garantir venda_id ao vincular
                    
                    # Log de identidade detalhado
                    doc_status = f"ID={documento.id}, tipo={tipo}, caminho={path_rel}"
                    _log_detalhado(f"VINCULANDO: NF {nf_doc} -> Venda ID {venda_id}. Status Documento: {doc_status}")
                    _log_detalhado(f"DEBUG: Iniciando vínculo automático: NF {nf_doc} → Venda {venda_id} (Cliente: {cliente_nome})")
                    
                    vendas_pedido = _vendas_do_pedido(venda_match)
                    _log_detalhado(f"DEBUG: Encontradas {len(vendas_pedido)} venda(s) no pedido")
                    
                    # Prioridade: preenchemos apenas o campo do tipo atual (caminho_boleto ou caminho_nf).
                    # NF sozinha ou boleto sozinho já vincula; se boleto chegar depois, ADICIONA sem alterar caminho_nf.
                    field_set = 'caminho_boleto' if tipo == 'BOLETO' else 'caminho_nf'
                    dv = dados_extraidos.get('data_vencimento')
                    for vv in vendas_pedido:
                        caminho_antigo = None
                        if tipo == 'BOLETO':
                            caminho_antigo = (vv.caminho_boleto or '').strip()
                            vv.caminho_boleto = path_rel
                            if dv is not None:
                                vv.data_vencimento = dv
                            _log_detalhado(f"DEBUG: Vinculando boleto à venda {vv.id}")
                            if caminho_antigo and caminho_antigo != path_rel:
                                _log_detalhado(f"DEBUG: Sobrescrevendo boleto antigo: {caminho_antigo} → {path_rel}")
                        else:
                            caminho_antigo = (vv.caminho_nf or '').strip()
                            vv.caminho_nf = path_rel
                            _log_detalhado(f"DEBUG: Vinculando NF à venda {vv.id}")
                            if caminho_antigo and caminho_antigo != path_rel:
                                _log_detalhado(f"DEBUG: Sobrescrevendo NF antiga: {caminho_antigo} → {path_rel}")
                    
                    # COMMIT IMEDIATO após vínculo (FORÇAR VÍNCULO ÚNICO)
                    _log_detalhado(f"\n--- Tentativa de Vínculo: Venda ID {venda_id} com Documento ID {documento.id} ---")
                    try:
                        _log_detalhado(f"DEBUG: Tentando gravar vínculo Venda ID {venda_id} com Documento ID {documento.id}")
                        _log_detalhado("DEBUG: Estado antes do commit:")
                        _log_detalhado(f"  - Documento.venda_id = {documento.venda_id}")
                        _log_detalhado(f"  - Venda.caminho_boleto = {venda_match.caminho_boleto if tipo == 'BOLETO' else 'N/A'}")
                        _log_detalhado(f"  - Venda.caminho_nf = {venda_match.caminho_nf if tipo == 'NOTA_FISCAL' else 'N/A'}")
                        db.session.commit()
                        _log_detalhado(f"DEBUG: ✅ COMMIT EXECUTADO COM SUCESSO: NF {nf_doc} vinculada à Venda {venda_id} (Cliente: {cliente_nome})")
                        if documento.url_arquivo and os.path.exists(caminho_completo):
                            try:
                                os.remove(caminho_completo)
                            except Exception as rm_err:
                                app.logger.warning(f"Aviso: não foi possível remover arquivo temporário {caminho_completo}: {rm_err}")
                        resultado['processados'] += 1
                        resultado['vinculos_novos'] += 1
                        rotulo = "Nota Fiscal" if tipo == 'NOTA_FISCAL' else "Boleto"
                        resultado['mensagens'].append(f"✅ Sucesso: {rotulo} {nf_doc} vinculada(o) automaticamente ao cliente {cliente_nome}.")
                    except Exception as commit_error:
                        db.session.rollback()
                        # LOG DETALHADO: Erro exato do banco de dados
                        erro_completo = traceback.format_exc()
                        _log_detalhado(f"\n{'='*80}")
                        _log_detalhado("❌ ERRO DE COMMIT DETECTADO (Vínculo Único)")
                        _log_detalhado(f"{'='*80}")
                        _log_detalhado(f"Tipo de Erro: {type(commit_error).__name__}")
                        _log_detalhado(f"Mensagem: {str(commit_error)}")
                        _log_detalhado("\nTraceback Completo:")
                        _log_detalhado(erro_completo)
                        _log_detalhado(f"{'='*80}\n")
                        
                        # Verificar se é erro de chave estrangeira
                        if 'foreign key' in str(commit_error).lower() or 'FOREIGN KEY' in str(commit_error):
                            _log_detalhado("⚠️ ERRO DE CHAVE ESTRANGEIRA DETECTADO:")
                            _log_detalhado(f"   Isso pode indicar que a Venda ID {venda_id} não existe mais no banco.")
                        elif 'integrity' in str(commit_error).lower() or 'INTEGRITY' in str(commit_error):
                            _log_detalhado("⚠️ ERRO DE INTEGRIDADE DETECTADO:")
                            _log_detalhado("   Isso pode indicar violação de constraint única ou chave estrangeira.")
                        elif 'operational' in str(commit_error).lower() or 'OPERATIONAL' in str(commit_error):
                            _log_detalhado("⚠️ ERRO OPERACIONAL DETECTADO:")
                            _log_detalhado("   Isso pode indicar problema de conexão ou estrutura do banco.")
                        
                        mensagem_erro = f"Falha técnica ao vincular NF {nf_doc}: {str(commit_error)}"
                        _log_detalhado(f"DEBUG: ❌ ERRO DE COMMIT: {mensagem_erro}")
                        resultado['erros'] += 1
                        resultado['mensagens'].append(f"❌ {mensagem_erro}")
                else:
                    # Sem vínculo automático (múltiplas vendas ou não encontrada)
                    app.logger.debug(f"DEBUG: Sem vínculo automático: venda_id={venda_id}, venda_match={venda_match is not None}")
                    db.session.commit()
                    if documento.url_arquivo and os.path.exists(caminho_completo):
                        try:
                            os.remove(caminho_completo)
                        except Exception as rm_err:
                            app.logger.warning(f"Aviso: não foi possível remover arquivo temporário {caminho_completo}: {rm_err}")
                    resultado['processados'] += 1
                    resultado['mensagens'].append(f"Processado: {arquivo}")
            except Exception as e:
                db.session.rollback()
                nf_doc = dados_extraidos.get('numero_nf') if dados_extraidos else 'N/A'
                mensagem_erro = f"Falha técnica ao vincular NF {nf_doc}: {str(e)}"
                app.logger.error(f"DEBUG: ❌ ERRO ao processar {arquivo}: {mensagem_erro}")
                app.logger.debug(f"DEBUG: Traceback: {traceback.format_exc()}")
                resultado['erros'] += 1
                resultado['mensagens'].append(f"❌ {mensagem_erro}")
    
    _log_detalhado(f"DEBUG: Processamento finalizado: {resultado['processados']} processados, {resultado['vinculos_novos']} vinculados, {resultado['erros']} erros")
    
    # ── PASSAGEM 2: documentos no BD sem arquivo local (URL-only / Cloudinary) ──
    # _processar_documentos_pendentes itera apenas sobre arquivos físicos em disco.
    # Documentos criados via bot com URL do Cloudinary (sem cópia local) nunca são
    # alcançados pelo loop acima. Esta segunda passagem resolve o vínculo pendente
    # usando apenas os metadados já extraídos (numero_nf, cnpj, etc.) no banco.
    try:
        # Auditoria P0 (A2): escopa Documentos e Vendas pelo tenant atual quando
        # houver request autenticada para evitar cross-match entre empresas.
        # Em jobs/scripts sem request context (ex.: cron), a varredura segue
        # global por compatibilidade — mas o seed de empresa_id em Documento
        # garante que matches dentro do mesmo número_nf/empresa permaneçam corretos.
        eid_atual = None
        try:
            if getattr(current_user, 'is_authenticated', False):
                eid_atual = getattr(current_user, 'empresa_id', None)
        except Exception:
            eid_atual = None

        docs_query = Documento.query.filter(
            Documento.venda_id.is_(None),
            Documento.numero_nf.isnot(None),
            Documento.numero_nf != '',
        )
        if eid_atual is not None:
            docs_query = docs_query.filter(
                or_(Documento.empresa_id == eid_atual, Documento.empresa_id.is_(None))
            )
        docs_sem_arquivo = docs_query.all()

        vendas_query = Venda.query.filter(
            Venda.nf.isnot(None), Venda.nf != ''
        )
        if eid_atual is not None:
            vendas_query = vendas_query.filter(Venda.empresa_id == eid_atual)
        vendas_com_nf_cache = vendas_query.options(joinedload(Venda.cliente)).limit(5000).all()

        for doc_pendente in docs_sem_arquivo:
            # Pular se existe localmente (já foi processado no loop acima)
            if doc_pendente.caminho_arquivo:
                pasta_tipo = 'boletos' if (doc_pendente.tipo or '').upper() == 'BOLETO' else 'notas_fiscais'
                caminho_abs = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    'documentos_entrada', pasta_tipo,
                    os.path.basename(doc_pendente.caminho_arquivo or '')
                )
                if os.path.isfile(caminho_abs):
                    continue  # Arquivo local existe; loop principal já tratou

            nf_doc_raw = (doc_pendente.nf_extraida or doc_pendente.numero_nf or '').strip()
            nf_doc_norm = _normalizar_nf(nf_doc_raw)
            if not nf_doc_norm or nf_doc_norm in ('S/N', '0', ''):
                continue

            vendas_validas_p2 = [
                v for v in vendas_com_nf_cache
                if _nf_match(nf_doc_norm, _normalizar_nf(str(v.nf)))
            ]
            vendas_validas_p2 = _deduplicar_vendas_por_pedido(vendas_validas_p2)
            if len(vendas_validas_p2) != 1:
                continue  # Ambiguidade ou não encontrado → triagem manual

            venda_p2 = vendas_validas_p2[0]
            try:
                doc_pendente.venda_id = venda_p2.id
                path_rel = (doc_pendente.caminho_arquivo or '').strip()
                if path_rel:
                    for vv in _vendas_do_pedido(venda_p2):
                        if (doc_pendente.tipo or '').upper() == 'BOLETO':
                            vv.caminho_boleto = path_rel
                            if doc_pendente.data_vencimento:
                                vv.data_vencimento = doc_pendente.data_vencimento
                        else:
                            vv.caminho_nf = path_rel
                db.session.commit()
                resultado['vinculos_novos'] += 1
                resultado['mensagens'].append(
                    f"✅ [P2] Doc ID {doc_pendente.id} (NF {nf_doc_raw}) vinculado à venda {venda_p2.id}."
                )
                _log_detalhado(
                    f"[P2-url-only] Doc ID {doc_pendente.id} (NF {nf_doc_raw}) "
                    f"→ Venda {venda_p2.id} (Cliente: {venda_p2.cliente.nome_cliente})."
                )
            except Exception as e_p2:
                db.session.rollback()
                _log_detalhado(f"[P2-url-only] ERRO ao vincular doc ID {doc_pendente.id}: {e_p2}")
    except Exception as e_p2_outer:
        db.session.rollback()
        _log_detalhado(f"[P2-url-only] ERRO na passagem 2: {e_p2_outer}")
    # ─────────────────────────────────────────────────────────────────────────

    # Se estiver capturando logs em memória, adicionar ao resultado
    if capturar_logs_memoria:
        resultado['logs'] = logs_memoria

    # Invalida cache do dashboard quando houver mutação de documentos/vínculos.
    if resultado.get('processados', 0) > 0 or resultado.get('vinculos_novos', 0) > 0:
        limpar_cache_dashboard()
    
    return resultado


def _reprocessar_boletos_atualizar_extracao():
    """Re-lê PDFs em documentos_entrada/boletos e atualiza numero_nf, cnpj, razao_social, data_vencimento nos Documentos."""
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'documentos_entrada')
    pasta = os.path.join(base_dir, 'boletos')
    ok, erros = 0, 0
    if not os.path.exists(pasta):
        return {'atualizados': 0, 'erros': 0}
    for nome in os.listdir(pasta):
        if not nome.lower().endswith('.pdf'):
            continue
        path_full = os.path.join(pasta, nome)
        if not os.path.isfile(path_full):
            continue
        path_rel = os.path.join('documentos_entrada', 'boletos', nome).replace(os.sep, '/')
        doc = Documento.query.filter_by(caminho_arquivo=path_rel).first()
        if not doc:
            continue
        dados = _processar_pdf(path_full, 'BOLETO')
        if dados is None:
            erros += 1
            continue
        try:
            doc.numero_nf = dados.get('numero_nf')
            doc.cnpj = dados.get('cnpj')
            doc.razao_social = dados.get('razao_social')
            doc.data_vencimento = dados.get('data_vencimento')
            db.session.commit()
            ok += 1
        except Exception:
            db.session.rollback()
            erros += 1
    return {'atualizados': ok, 'erros': erros}


def _reprocessar_vencimentos_vendas():
    """Percorre vendas com boleto vinculado e extrai data_vencimento do PDF para atualizar a venda.
    Retorna dict com estatísticas: {'total': int, 'atualizados': int, 'sem_data': int, 'erros': int, 'detalhes': list}"""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    resultado = {'total': 0, 'atualizados': 0, 'sem_data': 0, 'erros': 0, 'detalhes': []}
    
    # Buscar vendas com boleto vinculado
    vendas = Venda.query.filter(Venda.caminho_boleto.isnot(None)).all()
    resultado['total'] = len(vendas)
    
    for venda in vendas:
        caminho = (venda.caminho_boleto or '').strip()
        if not caminho:
            continue
        
        path_full = os.path.join(base_dir, caminho)
        if not os.path.isfile(path_full):
            resultado['erros'] += 1
            resultado['detalhes'].append(f"Venda {venda.id}: Arquivo não encontrado: {caminho}")
            continue
        
        try:
            dados = _processar_pdf(path_full, 'BOLETO')
            if dados is None:
                resultado['erros'] += 1
                resultado['detalhes'].append(f"Venda {venda.id}: Erro ao processar PDF")
                continue
            
            dv = dados.get('data_vencimento')
            if dv:
                venda.data_vencimento = dv
                db.session.commit()
                resultado['atualizados'] += 1
                resultado['detalhes'].append(f"Venda {venda.id}: Vencimento atualizado para {dv.strftime('%d/%m/%Y')}")
            else:
                resultado['sem_data'] += 1
                resultado['detalhes'].append(f"Venda {venda.id}: Nenhuma data de vencimento encontrada no PDF")
        except Exception as e:
            db.session.rollback()
            resultado['erros'] += 1
            resultado['detalhes'].append(f"Venda {venda.id}: Exceção: {str(e)}")
    
    # Também atualizar vendas que têm documento vinculado mas não têm data_vencimento ainda
    vendas_sem_dv = Venda.query.filter(
        Venda.data_vencimento.is_(None),
        Venda.caminho_boleto.isnot(None)
    ).all()
    
    for venda in vendas_sem_dv:
        # Tentar obter do Documento se existir
        doc = Documento.query.filter_by(caminho_arquivo=venda.caminho_boleto).first()
        if doc and doc.data_vencimento:
            venda.data_vencimento = doc.data_vencimento
            try:
                db.session.commit()
                resultado['atualizados'] += 1
                resultado['detalhes'].append(f"Venda {venda.id}: Vencimento copiado do Documento: {doc.data_vencimento.strftime('%d/%m/%Y')}")
            except Exception:
                db.session.rollback()
    
    return resultado


def _deduplicar_vendas_por_id(vendas):
    """Remove duplicidades de objetos Venda mantendo IDs únicos.

    Em alguns cenários com joins, a mesma venda pode aparecer repetida na lista.
    """
    unicas = {}
    for venda in vendas or []:
        venda_id = getattr(venda, 'id', None)
        if venda_id is None:
            continue
        unicas[int(venda_id)] = venda
    return list(unicas.values())


def _deduplicar_vendas_por_pedido(vendas):
    """Deduplica vendas por pedido lógico (cliente + NF normalizada).

    Isso evita falsa ambiguidade quando um mesmo pedido possui múltiplos itens
    e cada item vira uma linha na tabela de vendas.
    """
    unicas = {}
    for venda in _deduplicar_vendas_por_id(vendas):
        chave_nf = _normalizar_nf(str(getattr(venda, 'nf', '') or '').strip())
        chave = (getattr(venda, 'cliente_id', None), chave_nf)
        unicas[chave] = venda
    return list(unicas.values())


def _diagnosticar_vinculo_falhou(doc, vendas_com_nf_cache=None):
    """Diagnostica por que um documento não foi vinculado automaticamente.
    Lógica simplificada: compara APENAS NF (normalizada, sem zeros à esquerda).
    Retorna dict com: cenario ('A', 'B', 'C' ou None), mensagem, cliente_id, cliente_nome, nf_tentada."""
    if not doc or doc.venda_id is not None:
        return None
    doc_nf = doc.numero_nf
    if not doc_nf:
        return {
            'cenario': 'C',
            'mensagem': "NF não encontrada no documento. Verifique se o PDF contém o número da nota fiscal.",
            'nf_tentada': '',
            'nf_lida': ''
        }
    nf_str = str(doc_nf).strip()
    nf_limpa = _normalizar_nf(nf_str)
    nfs_invalidas = ('S/N', '0', 'Falta_nota', '')
    if nf_limpa in nfs_invalidas:
        return {
            'cenario': None,
            'mensagem': f"NF '{nf_str}' é inválida para vínculo automático ({', '.join(nfs_invalidas[:3])}).",
            'nf_tentada': nf_limpa,
            'nf_lida': doc_nf
        }
    # Buscar vendas com NF preenchida (evita carregar tabela inteira)
    vendas_validas = []
    vendas_com_nf = vendas_com_nf_cache
    if vendas_com_nf is None:
        vendas_com_nf = Venda.query.filter(
            Venda.nf.isnot(None), Venda.nf != ''
        ).options(joinedload(Venda.cliente)).limit(5000).all()
    for v in vendas_com_nf:
        nf_venda_norm = _normalizar_nf(str(v.nf))
        if _nf_match(nf_limpa, nf_venda_norm):
            vendas_validas.append(v)
    vendas_validas = _deduplicar_vendas_por_pedido(vendas_validas)
    
    if len(vendas_validas) == 0:
        return {
            'cenario': 'C',
            'mensagem': f"NF {doc_nf} não localizada em nenhuma venda. Verifique se você já importou a planilha de vendas deste período.",
            'nf_tentada': nf_limpa,
            'nf_lida': doc_nf
        }
    elif len(vendas_validas) == 1:
        v = vendas_validas[0]
        # Não bloquear pela existência de NF/boletos na venda.
        # Regra: o bloqueio só deve ocorrer se ESTE documento já estiver vinculado (doc.venda_id != None),
        # o que já é tratado no início desta função.
        tipo_doc_lido = 'boleto' if (doc.tipo or '').upper() == 'BOLETO' else 'nota fiscal'
        return {
            'cenario': 'A',
            'mensagem': f"NF {doc_nf} encontrada no sistema (Cliente: {v.cliente.nome_cliente}). O {tipo_doc_lido} pode ser vinculado normalmente a esta venda.",
            'cliente_id': v.cliente.id,
            'cliente_nome': v.cliente.nome_cliente,
            'nf_tentada': nf_limpa,
            'nf_lida': doc_nf,
            'venda_id': v.id,
            'nf_venda': v.nf or ''
        }
    else:
        # Múltiplas vendas com mesma NF: requer intervenção manual
        clientes = [v.cliente.nome_cliente for v in vendas_validas[:3]]
        return {
            'cenario': 'B',
            'mensagem': f"NF {doc_nf} encontrada em {len(vendas_validas)} venda(s) para cliente(s) diferentes ({', '.join(clientes)}{'...' if len(vendas_validas) > 3 else ''}). Selecione manualmente qual venda vincular.",
            'nf_tentada': nf_limpa,
            'nf_lida': doc_nf,
            'vendas_multiplas': [{'id': v.id, 'cliente': v.cliente.nome_cliente} for v in vendas_validas]
        }


def _listar_documentos_recem_chegados():
    """Lista documentos órfãos e pendentes para a fila do Dashboard.

    Regra de negócio da fila "Documentos Recém-Chegados":
    - exibir apenas documentos sem vínculo com venda (``venda_id IS NULL``)

    AUTO-LINK:
    Quando ``_diagnosticar_vinculo_falhou`` retorna cenário 'A' (exatamente uma
    venda correspondente à NF), o vínculo é gravado imediatamente — o documento
    nunca fica preso na fila exibindo a mensagem azul "NF encontrada" sem ser
    resolvido automaticamente.  Isso cobre documentos sem arquivo local (uploads
    via bot/Cloudinary) que o ``_processar_documentos_pendentes`` nunca alcança.
    """
    resultado_processamento = {"sucesso": 0, "falha": 0, "erros": [], "vinculos_novos": 0, "processados": 0}
    # Auditoria P0 (A2): a fila do dashboard é tenant-aware. Apenas documentos
    # do próprio tenant (ou órfãos legados sem empresa_id) entram na fila.
    eid_atual = empresa_id_atual()
    query = Documento.query.filter(Documento.venda_id.is_(None))
    if eid_atual is not None:
        query = query.filter(
            or_(Documento.empresa_id == eid_atual, Documento.empresa_id.is_(None))
        )
    # Para o Dashboard, documentos órfãos devem ser visíveis independentemente de ownership.
    # Mantemos o parâmetro user_id por compatibilidade, mas sem restringir esta listagem.
    # Limite defensivo para evitar timeout de worker em bases grandes.
    docs = query.order_by(Documento.id.desc()).limit(300).all()
    # Cache da consulta de vendas por NF nesta mesma request (evita N x 5000 scans).
    vendas_query = Venda.query.filter(Venda.nf.isnot(None), Venda.nf != '')
    if eid_atual is not None:
        vendas_query = vendas_query.filter(Venda.empresa_id == eid_atual)
    vendas_com_nf_cache = vendas_query.options(joinedload(Venda.cliente)).limit(5000).all()
    documentos = []
    for doc in docs:
        diag = _diagnosticar_vinculo_falhou(doc, vendas_com_nf_cache=vendas_com_nf_cache)

        # ── AUTO-LINK: cenário A = 1 venda única encontrada ───────────────────
        # O diagnóstico já fez a busca e conhece o venda_id correto. Em vez de
        # apenas exibir a mensagem azul esperando clique manual, efetuar o
        # commit aqui mesmo garante que o documento saia da fila imediatamente.
        if diag and diag.get('cenario') == 'A':
            venda_id_diag = diag.get('venda_id')
            if venda_id_diag:
                try:
                    doc.venda_id = venda_id_diag
                    venda_alvo = db.session.get(Venda, venda_id_diag)
                    if venda_alvo and doc.caminho_arquivo:
                        path_rel = doc.caminho_arquivo
                        for vv in _vendas_do_pedido(venda_alvo):
                            if (doc.tipo or '').upper() == 'BOLETO':
                                vv.caminho_boleto = path_rel
                                if doc.data_vencimento:
                                    vv.data_vencimento = doc.data_vencimento
                            else:
                                vv.caminho_nf = path_rel
                    db.session.commit()
                    resultado_processamento['vinculos_novos'] += 1
                    current_app.logger.info(
                        f"[auto-link-dashboard] Documento ID {doc.id} "
                        f"(NF {doc.numero_nf}) vinculado à venda {venda_id_diag}."
                    )
                    # Documento agora tem venda_id → NÃO entra na fila pendente.
                    continue
                except Exception as exc:
                    db.session.rollback()
                    current_app.logger.warning(
                        f"[auto-link-dashboard] Falha ao vincular doc ID {doc.id} "
                        f"→ venda {venda_id_diag}: {exc}"
                    )
                    # Em caso de falha, o documento cai na fila normalmente.
        # ─────────────────────────────────────────────────────────────────────

        nf_nao = diag is not None and diag.get('cenario') == 'C' and 'não localizada' in (diag.get('mensagem') or '')
        documentos.append({
                'doc': doc,
            'nome_arquivo': os.path.basename(doc.caminho_arquivo or ''),
            'leitura_ok': True,
            'nf_nao_encontrada': nf_nao,
            'etiqueta_pasta': doc.tipo or '',
            'diagnostico': diag
        })
    return documentos, resultado_processamento


def _auto_vincular_documentos_pendentes_por_nf(user_id=None):
    """Vincula documentos já salvos no banco (venda_id=None) usando NF extraída.

    Auditoria P0 (A2): escopa Documentos e Vendas pelo tenant atual quando
    houver request autenticada para evitar matches NF cross-tenant. Em
    contextos sem request (ex.: cron), a varredura segue global por
    compatibilidade.
    """
    resultado = {'vinculados': 0, 'erros': 0}
    eid_atual = None
    try:
        if getattr(current_user, 'is_authenticated', False):
            eid_atual = getattr(current_user, 'empresa_id', None)
    except Exception:
        eid_atual = None

    query = Documento.query.filter(Documento.venda_id.is_(None))
    if user_id is not None:
        query = query.filter(Documento.usuario_id == user_id)
    if eid_atual is not None:
        query = query.filter(
            or_(Documento.empresa_id == eid_atual, Documento.empresa_id.is_(None))
        )
    docs = query.all()

    vendas_query = Venda.query.filter(Venda.nf.isnot(None), Venda.nf != '')
    if eid_atual is not None:
        vendas_query = vendas_query.filter(Venda.empresa_id == eid_atual)
    vendas_com_nf = vendas_query.limit(5000).all()

    for doc in docs:
        try:
            nf_origem = (doc.nf_extraida or doc.numero_nf or '').strip()
            nf_doc = _normalizar_nf(nf_origem)
            if not nf_doc or nf_doc in ('S/N', '0'):
                continue

            vendas_candidatas = []
            for venda in vendas_com_nf:
                nf_venda = _normalizar_nf(str(venda.nf or '').strip())
                if _nf_match(nf_doc, nf_venda):
                    vendas_candidatas.append(venda)
            vendas_candidatas = _deduplicar_vendas_por_pedido(vendas_candidatas)
            if len(vendas_candidatas) != 1:
                continue
            venda_match = vendas_candidatas[0]

            # Sem bloqueios adicionais: apenas vincula este documento à venda encontrada.
            doc.venda_id = venda_match.id

            # Mantém compatibilidade com fluxos legados que usam caminho_boleto/caminho_nf.
            path = (doc.caminho_arquivo or '').strip()
            if path:
                vendas_pedido = _vendas_do_pedido(venda_match)
                is_boleto = (doc.tipo or '').upper() == 'BOLETO'
                for vv in vendas_pedido:
                    if is_boleto:
                        vv.caminho_boleto = path
                        if doc.data_vencimento:
                            vv.data_vencimento = doc.data_vencimento
                    else:
                        vv.caminho_nf = path

            resultado['vinculados'] += 1
        except Exception as e:
            resultado['erros'] += 1
            app.logger.error(f"[auto_vinculo_nf] Erro ao vincular documento ID {getattr(doc, 'id', '?')}: {e}")

    try:
        db.session.commit()
        if resultado.get('vinculados', 0) > 0:
            limpar_cache_dashboard()
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"[auto_vinculo_nf] Erro no commit: {e}")
        resultado['erros'] += 1

    return resultado


app = Flask(__name__)
app.config.from_object(Config)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Caixa preta de erros: log rotativo em arquivo
_logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
if not os.path.exists(_logs_dir):
    os.mkdir(_logs_dir)

_logs_file = os.path.join(_logs_dir, 'erros_sistema.log')
file_handler = RotatingFileHandler(_logs_file, maxBytes=1_048_576, backupCount=5)
file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)-8s: %(message)s [%(pathname)s:%(lineno)d]'
))
# Apenas erros reais (ERROR e CRITICAL) são gravados no arquivo de erros críticos
file_handler.setLevel(logging.ERROR)

if not any(isinstance(h, RotatingFileHandler) and getattr(h, 'baseFilename', '') == _logs_file for h in app.logger.handlers):
    app.logger.addHandler(file_handler)
app.logger.setLevel(logging.INFO)

# Sessão e "Lembrar-me": persistir por 30 dias
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

# Configurações para manter a conexão com o banco sempre viva (Blindagem contra EOF Error)
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,   # Testa a conexão antes de usar (evita "SSL SYSCALL error: EOF detected")
    'pool_recycle': 300,     # Recria conexões a cada 5 minutos para evitar timeout do servidor
    'pool_timeout': 30,      # Espera 30s por uma conexão antes de dar erro
    'pool_size': 10,         # Mantém até 10 conexões abertas
    'max_overflow': 20       # Em pico, pode abrir mais 20
}

# Configuração de E-mail (Relatório Mensal)
MAIL_SERVER = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
MAIL_PORT = int(os.environ.get('MAIL_PORT', 587))
MAIL_USERNAME = os.environ.get('MAIL_USERNAME')
MAIL_PASSWORD = os.environ.get('MAIL_PASSWORD')
CRON_SECRET = os.environ.get('CRON_SECRET')

# Chaves VAPID para Web Push — gere com: python -c "from py_vapid import Vapid; v=Vapid(); v.generate_keys(); print('PRIV:', v.private_pem().decode()); print('PUB:', v.public_key.public_bytes(__import__('cryptography.hazmat.primitives.serialization', fromlist=['Encoding','PublicFormat']).Encoding.X962, __import__('cryptography.hazmat.primitives.serialization', fromlist=['PublicFormat']).PublicFormat.UncompressedPoint).hex())"
# Ou via: npx web-push generate-vapid-keys
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY')
VAPID_CLAIM_EMAIL = os.environ.get('VAPID_CLAIM_EMAIL', 'mailto:admin@meninoalho.com.br')

# Configurar compressão Gzip para HTML, CSS, JS e JSON.
# IMPORTANTE: definir COMPRESS_* ANTES de Compress(app), caso contrário a
# extensão lê os defaults na inicialização e ignora os valores customizados.
app.config['COMPRESS_MIMETYPES'] = [
    'text/html',
    'text/css',
    'text/javascript',
    'application/javascript',
    'application/json',
    'text/xml',
    'application/xml',
    'image/svg+xml',
]
app.config['COMPRESS_LEVEL'] = 6  # 1-9, 6 é o equilíbrio CPU x tamanho
app.config['COMPRESS_MIN_SIZE'] = 500  # bytes (não vale comprimir respostas pequenas)
app.config['COMPRESS_ALGORITHM'] = ['br', 'gzip']  # brotli para clientes modernos, gzip como fallback
Compress(app)

# ─────────────────────────────────────────────────────────────────────────────
# REGISTRO DE TODAS AS EXTENSÕES (db, login_manager, csrf, cache, limiter)
# ─────────────────────────────────────────────────────────────────────────────
# init_extensions cuida de:
#   * db.init_app(app)
#   * login_manager.init_app(app) + login_view='auth.login'
#   * csrf.init_app(app)
#   * cache.init_app(app, ...) escolhendo Redis vs SimpleCache automaticamente
#   * limiter.init_app(app)
init_extensions(app)

# Configurar Fila de Tarefas (RQ) — depende de REDIS_URL (mesmo backend usado
# pelo cache). Mantemos isso fora de extensions.py porque ``fila_tarefas`` é
# usado diretamente em handlers legados via ``from app import fila_tarefas``.
redis_url = os.environ.get('REDIS_URL')
redis_conn = Redis.from_url(redis_url) if redis_url else None
fila_tarefas = Queue(connection=redis_conn) if redis_conn else None


def gerar_arquivo_backup_csv() -> tuple[bytes, str]:
    """
    Gera o arquivo CSV de backup das tabelas críticas do sistema.

    Exporta: Vendas (últimas 10 000), Clientes (5 000),
    Produtos (5 000) e Lançamentos de Caixa (5 000).

    Returns:
        (csv_bytes, nome_arquivo): bytes do CSV em UTF-8-BOM e o nome sugerido
        para salvar/anexar (ex: ``backup_menino_do_alho_2026-03-17_23h50.csv``).
    """
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')

    # --- Vendas ---
    writer.writerow(["=== VENDAS ==="])
    writer.writerow([
        "ID", "Data", "Cliente", "Produto", "Qtd", "Preço Unit.",
        "Valor Total", "Custo Total", "Lucro", "Situação",
        "Forma Pagto", "Empresa", "Vencimento", "Status Entrega",
    ])
    vendas = (
        Venda.query
        .options(joinedload(Venda.cliente), joinedload(Venda.produto))
        .order_by(Venda.data_venda.desc())
        .limit(10000).all()
    )
    for v in vendas:
        nome_cli = v.cliente.nome_cliente if v.cliente else "Desconhecido"
        dt       = v.data_venda.strftime('%d/%m/%Y') if v.data_venda else ""
        venc     = v.data_vencimento.strftime('%d/%m/%Y') if v.data_vencimento else ""
        total    = float(v.calcular_total())
        lucro    = float(v.calcular_lucro())
        writer.writerow([
            v.id, dt, nome_cli,
            v.produto.nome_produto if v.produto else "-",
            v.quantidade_venda or 0,
            float(v.preco_venda or 0),
            total, total - lucro, lucro,
            v.situacao or "", v.forma_pagamento or "",
            v.empresa_faturadora or "", venc,
            v.status_entrega or "",
        ])

    # --- Clientes ---
    writer.writerow([])
    writer.writerow(["=== CLIENTES ==="])
    writer.writerow(["ID", "Nome", "Razão Social", "CNPJ", "Cidade", "Endereço", "Ativo"])
    for c in Cliente.query.order_by(Cliente.nome_cliente).limit(5000).all():
        writer.writerow([
            c.id, c.nome_cliente or "", c.razao_social or "",
            c.cnpj or "", c.cidade or "", c.endereco or "",
            "Sim" if c.ativo else "Não",
        ])

    # --- Produtos ---
    writer.writerow([])
    writer.writerow(["=== ESTOQUE / PRODUTOS ==="])
    writer.writerow([
        "ID", "Nome", "Tipo", "Fornecedor", "Marca",
        "Preço Custo", "Qtd Entrada", "Estoque Atual", "Data Chegada",
    ])
    # TODO Fase 3 (multi-tenant): este backup agrega dados de TODOS os tenants.
    # Quando houver mais de uma empresa, considerar gerar um CSV por empresa
    # ou prefixar cada linha com empresa_id. Por ora exportamos tudo porque o
    # backup eh enviado apenas para o email do admin da plataforma (MASTER).
    for p in Produto.query.order_by(Produto.data_chegada.desc()).limit(5000).all():
        dc = p.data_chegada.strftime('%d/%m/%Y') if p.data_chegada else ""
        writer.writerow([
            p.id, p.nome_produto or "", p.tipo or "", p.fornecedor or "",
            p.marca or "", float(p.preco_custo or 0),
            p.quantidade_entrada or 0, p.estoque_atual or 0, dc,
        ])

    # --- Caixa ---
    writer.writerow([])
    writer.writerow(["=== CAIXA (últimos 5 000 lançamentos) ==="])
    writer.writerow(["ID", "Data", "Descrição", "Categoria", "Tipo", "Valor", "Forma Pagamento", "Setor"])
    for lc in LancamentoCaixa.query.order_by(LancamentoCaixa.data.desc()).limit(5000).all():
        dl = lc.data.strftime('%d/%m/%Y') if lc.data else ""
        writer.writerow([
            lc.id, dl, lc.descricao or "", lc.categoria or "",
            lc.tipo or "", float(lc.valor or 0),
            lc.forma_pagamento or "", lc.setor or "",
        ])

    csv_bytes = output.getvalue().encode('utf-8-sig')
    output.close()

    timestamp   = datetime.now().strftime('%Y-%m-%d_%Hh%M')
    nome_arquivo = f"backup_menino_do_alho_{timestamp}.csv"
    return csv_bytes, nome_arquivo


def _enviar_backup_por_email(csv_bytes: bytes, nome_arquivo: str) -> int:
    """
    Envia o arquivo de backup CSV como anexo de e-mail para os destinatários
    configurados.  Retorna o número de destinatários que receberam o e-mail.

    Variáveis de ambiente necessárias:
        MAIL_USERNAME, MAIL_PASSWORD — credenciais do remetente
        MAIL_SERVER / MAIL_PORT     — servidor SMTP (padrão: smtp.gmail.com:587)
        BACKUP_DEST_EMAIL           — (opcional) destinatário fixo; se ausente,
                                      envia para todos os admins com e-mail.

    Raises:
        RuntimeError: se credenciais ausentes ou nenhum destinatário encontrado.
    """
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        raise RuntimeError("Credenciais de e-mail não configuradas (MAIL_USERNAME / MAIL_PASSWORD).")

    dest_fixo = os.environ.get('BACKUP_DEST_EMAIL', '').strip()
    if dest_fixo:
        destinatarios = [dest_fixo]
    else:
        admins = Usuario.query.filter(
            Usuario.role == 'admin',
            Usuario.email.isnot(None),
            Usuario.email != '',
        ).all()
        destinatarios = [a.email for a in admins if a.email]

    if not destinatarios:
        raise RuntimeError("Nenhum destinatário de backup encontrado. Configure BACKUP_DEST_EMAIL.")

    data_hoje = datetime.now().strftime('%d/%m/%Y')
    server = smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=_EXTERNAL_TIMEOUT)
    server.starttls()
    server.login(MAIL_USERNAME, MAIL_PASSWORD)

    enviados = 0
    for email_dest in destinatarios:
        msg             = MIMEMultipart()
        msg['Subject']  = f"📦 Backup Diário - Sistema Menino do Alho [{data_hoje}]"
        msg['From']     = MAIL_USERNAME
        msg['To']       = email_dest

        corpo = MIMEText(
            f"Olá,\n\n"
            f"Segue em anexo o backup automático do Sistema Menino do Alho "
            f"gerado em {data_hoje}.\n\n"
            f"Conteúdo do arquivo:\n"
            f"  • Vendas (até 10.000 registros)\n"
            f"  • Clientes (até 5.000 registros)\n"
            f"  • Estoque / Produtos (até 5.000 registros)\n"
            f"  • Caixa — Lançamentos (até 5.000 registros)\n\n"
            f"Arquivo: {nome_arquivo}\n\n"
            f"Este e-mail é enviado automaticamente pelo agendador interno.\n"
            f"Não responda.\n\n— Sistema Menino do Alho",
            'plain', 'utf-8',
        )
        msg.attach(corpo)

        anexo = MIMEBase('application', 'octet-stream')
        anexo.set_payload(csv_bytes)
        encoders.encode_base64(anexo)
        anexo.add_header('Content-Disposition', f'attachment; filename="{nome_arquivo}"')
        msg.attach(anexo)

        server.send_message(msg)
        enviados += 1

    server.quit()
    return enviados


def _executar_backup_diario_job() -> None:
    """
    Tarefa agendada: gera o CSV de backup e tenta entregá-lo via SMTP.

    Fluxo de entrega:
        1. (Primário) Envia por e-mail como anexo via SMTP.
        2. (Fallback) Se SMTP falhar, faz upload para Cloudinary (pasta
           ``menino_do_alho/backups``) e envia a URL segura como Web Push
           para todos os administradores inscritos, se as chaves VAPID
           estiverem configuradas.

    Deve ser executada dentro de um Flask app_context (o scheduler garante
    isso através do wrapper ``_job_com_contexto``).
    """
    import logging as _logging
    _log = _logging.getLogger('backup_scheduler')

    try:
        csv_bytes, nome_arquivo = gerar_arquivo_backup_csv()
        _log.info(f"[backup] CSV gerado: {nome_arquivo} ({len(csv_bytes)} bytes)")
    except Exception as e:
        _log.error(f"[backup] Falha ao gerar CSV: {e}")
        return

    # --- Tentativa 1: E-mail ---
    try:
        enviados = _enviar_backup_por_email(csv_bytes, nome_arquivo)
        _log.info(f"[backup] ✅ E-mail enviado para {enviados} destinatário(s).")
        return  # Sucesso — não precisa de fallback.
    except Exception as e_smtp:
        _log.warning(f"[backup] SMTP falhou ({e_smtp}). Tentando fallback Cloudinary+Push...")

    # --- Fallback: Cloudinary + Web Push ---
    url_cloudinary = None
    try:
        _cloudinary_configured = (
            os.environ.get('CLOUDINARY_URL')
            or (os.environ.get('CLOUDINARY_CLOUD_NAME')
                and os.environ.get('CLOUDINARY_API_KEY'))
        )
        if not _cloudinary_configured:
            raise RuntimeError("Cloudinary não configurado.")

        upload_result = cloudinary.uploader.upload(
            io.BytesIO(csv_bytes),
            public_id=f"menino_do_alho/backups/{nome_arquivo}",
            resource_type='raw',
            timeout=_EXTERNAL_TIMEOUT,
        )
        url_cloudinary = upload_result.get('secure_url', '')
        _log.info(f"[backup] Arquivo enviado ao Cloudinary: {url_cloudinary}")
    except Exception as e_cloud:
        _log.error(f"[backup] Cloudinary também falhou: {e_cloud}. Backup perdido neste ciclo.")
        return

    # Enviar URL via Web Push para admins inscritos
    if url_cloudinary and VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY:
        try:
            from pywebpush import webpush, WebPushException
            subscriptions = (
                PushSubscription.query
                .join(Usuario, PushSubscription.user_id == Usuario.id)
                .filter(Usuario.role == 'admin')
                .all()
            )
            payload = json.dumps({
                'title': '📦 Backup Diário Disponível',
                'body': f'O backup de hoje foi salvo na nuvem. Acesse para baixar.',
                'url': url_cloudinary,
            })
            for sub in subscriptions:
                try:
                    webpush(
                        subscription_info={
                            'endpoint': sub.endpoint,
                            'keys': {'p256dh': sub.p256dh, 'auth': sub.auth},
                        },
                        data=payload,
                        vapid_private_key=pad_base64(VAPID_PRIVATE_KEY),
                        vapid_claims={'sub': VAPID_CLAIM_EMAIL},
                    )
                except WebPushException as wpe:
                    if wpe.response and wpe.response.status_code in (404, 410):
                        db.session.delete(sub)
                        db.session.commit()
        except Exception as e_push:
            _log.warning(f"[backup] Web Push falhou: {e_push}")


# Criar pasta de uploads se não existir
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Backup SQLite removido: sistema usa PostgreSQL em produção (DATABASE_URL)
# (db.init_app(app) já foi chamado em init_extensions(app) acima)

# ── Agendador de Backup Diário (APScheduler) ─────────────────────────────────
# Proteção multi-worker Gunicorn: o Gunicorn cria N workers (fork). Sem
# proteção cada worker iniciaria seu próprio scheduler e o backup seria
# enviado N vezes.  A solução é iniciar o scheduler apenas no processo
# principal usando a variável WERKZEUG_RUN_MAIN (dev server) OU verificando
# se não somos um worker forked (Gunicorn define `SERVER_SOFTWARE` e lança
# os workers com argparse; o processo pai não herda `_SCHEDULER_STARTED`).
_scheduler: BackgroundScheduler | None = None
_SCHEDULER_ENV_FLAG = '_MENINO_ALHO_SCHEDULER_PID'

def _iniciar_scheduler() -> None:
    """
    Inicializa o BackgroundScheduler com proteção para evitar execuções
    duplicadas em ambientes multi-worker (Gunicorn).

    Usa RedisJobStore quando Redis estiver disponível (distribui o lock entre
    workers), caso contrário cai para MemoryJobStore.

    O job ``backup_diario`` dispara todo dia às 23h50 (fuso de Brasília/Recife).
    """
    global _scheduler

    # Evitar inicialização duplicada dentro do mesmo processo
    if _scheduler is not None and _scheduler.running:
        return

    # Gunicorn: cada worker fork herda o ambiente do pai. Registramos o PID
    # do primeiro processo que chegou aqui; workers forked terão PIDs diferentes
    # mas a variável já estará setada no ambiente compartilhado (via os.environ).
    # Como cada worker tem seu próprio espaço de memória, usamos um arquivo de
    # lock simples no sistema de arquivos efêmero (válido apenas enquanto o pod
    # estiver vivo — o que é suficiente para evitar duplicatas na mesma sessão).
    lock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.scheduler.lock')
    try:
        # Verifica se outro processo deste pod já iniciou o scheduler
        if os.path.exists(lock_file):
            with open(lock_file) as fh:
                pid_existente = int(fh.read().strip() or '0')
            # Se o processo ainda existe, não inicialize de novo
            try:
                os.kill(pid_existente, 0)
                app.logger.info(f"[scheduler] Já ativo no pid {pid_existente}. Pulando.")
                return
            except (ProcessLookupError, OSError):
                pass  # Processo morreu; recriar o scheduler
        with open(lock_file, 'w') as fh:
            fh.write(str(os.getpid()))
    except Exception:
        pass  # Não travar a inicialização do app por falha no lock

    # Configurar jobstore: Redis (distribuído) ou Memory (local)
    jobstores = {}
    if _HAS_REDIS_JOBSTORE and redis_url:
        try:
            jobstores['default'] = _RedisJobStore(jobs_key='menino_alho.scheduler.jobs',
                                                   run_times_key='menino_alho.scheduler.run_times',
                                                   url=redis_url)
            app.logger.info("[scheduler] Usando RedisJobStore (distribuído).")
        except Exception:
            jobstores['default'] = MemoryJobStore()
    else:
        jobstores['default'] = MemoryJobStore()

    _scheduler = BackgroundScheduler(
        jobstores=jobstores,
        job_defaults={'coalesce': True, 'max_instances': 1, 'misfire_grace_time': 3600},
        timezone='America/Recife',
    )

    def _job_com_contexto():
        """Wrapper que garante app_context antes de executar o job de backup."""
        with app.app_context():
            _executar_backup_diario_job()

    _scheduler.add_job(
        _job_com_contexto,
        trigger=CronTrigger(hour=23, minute=50, timezone='America/Recife'),
        id='backup_diario',
        replace_existing=True,
    )
    _scheduler.start()
    app.logger.info(f"[scheduler] BackgroundScheduler iniciado (pid {os.getpid()}). Backup às 23h50 (Recife).")


# Inicializar somente fora do processo de reloader do Flask dev server
# e somente quando o módulo for importado como __main__ ou por Gunicorn.
if not os.environ.get('WERKZEUG_RUN_MAIN'):
    _iniciar_scheduler()
# ─────────────────────────────────────────────────────────────────────────────

# Cloudinary: configurar com variáveis de ambiente ou app.config (para fotos de produtos, documentos, etc.)
_cloudinary_url = os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL')
if _cloudinary_url:
    cloudinary.config(secure=True)  # Usa CLOUDINARY_URL do ambiente
elif app.config.get('CLOUDINARY_CLOUD_NAME') and app.config.get('CLOUDINARY_API_KEY') and app.config.get('CLOUDINARY_API_SECRET'):
    cloudinary.config(
        cloud_name=app.config['CLOUDINARY_CLOUD_NAME'],
        api_key=app.config['CLOUDINARY_API_KEY'],
        api_secret=app.config['CLOUDINARY_API_SECRET'],
        secure=True
    )

# Ativar WAL Mode no SQLite para melhorar concorrência com múltiplos workers
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    """Ativa Write-Ahead Logging (WAL) no SQLite para suportar múltiplos workers simultaneamente."""
    # Verificar se estamos usando SQLite antes de executar comandos PRAGMA
    uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
    if uri and uri.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

# (login_manager / csrf já foram inicializados em init_extensions(app))


# --- VACINA CONTRA TRAVAMENTO DO BANCO ---
@app.before_request
def ensure_clean_connection():
    """Verifica se a conexão está limpa antes de processar qualquer coisa. Evita PendingRollbackError."""
    try:
        db.session.execute(text('SELECT 1'))
    except Exception:
        db.session.rollback()
        db.session.remove()


@app.before_request
def limpar_sessao_anterior():
    """Garante que não haja transações pendentes antes de cada requisição."""
    try:
        db.session.rollback()
    except Exception:
        db.session.remove()


@app.teardown_appcontext
def shutdown_session(exception=None):
    """
    Faxina automática: Toda vez que o site terminar de responder
    (ou se der erro), ele fecha a conexão com o banco e limpa a memória.
    Isso evita o erro 'PendingRollbackError'.
    """
    try:
        if exception:
            db.session.rollback()
        db.session.remove()
    except Exception as e:
        app.logger.error(f"Erro ao fechar conexão DB: {e}")


@app.after_request
def add_cache_control_static(response):
    """Adiciona headers de cache longo para arquivos estáticos."""
    if request.path.startswith('/static/'):
        # Cache de 1 ano para arquivos estáticos (CSS, JS, imagens, fontes)
        response.headers['Cache-Control'] = 'public, max-age=31536000'
        # Adicionar ETag para validação de cache
        if not response.headers.get('ETag'):
            etag = hashlib.md5(response.get_data()).hexdigest()
            response.headers['ETag'] = f'"{etag}"'
    return response


def limpar_cache_dashboard():
    """Invalida o cache do dashboard de forma imediata e consistente.

    Como o dashboard usa ``@cache.cached`` com key_prefix dinâmico, deletar
    apenas uma chave fixa não é suficiente. Esta função usa versionamento de
    chave: ao atualizar ``dashboard_cache_version``, todas as chaves antigas
    deixam de ser reutilizadas instantaneamente.
    """
    try:
        nova_versao = str(int(datetime.utcnow().timestamp() * 1000))
        cache.set('dashboard_cache_version', nova_versao, timeout=0)
        # Compatibilidade retroativa com implementações anteriores.
        cache.delete('view//dashboard')
    except Exception:
        # Se houver erro, limpa todo o cache como fallback
        try:
            cache.clear()
        except Exception:
            pass  # Ignora erros de cache


def _dashboard_cache_version() -> str:
    """Retorna a versão atual da chave de cache do dashboard."""
    try:
        versao = cache.get('dashboard_cache_version')
        if not versao:
            versao = str(int(datetime.utcnow().timestamp() * 1000))
            cache.set('dashboard_cache_version', versao, timeout=0)
        return str(versao)
    except Exception:
        # Em caso de indisponibilidade do backend de cache, evita quebrar a view.
        return 'no-cache'


def _dashboard_cache_key() -> str:
    """Chave de cache do dashboard, segmentada POR TENANT.

    Usa ``empresa_id`` (multi-tenant) em vez de ``user_id`` para permitir
    que vários funcionários da mesma empresa compartilhem o resultado já
    computado — economiza CPU/banco e mantém o isolamento entre empresas.

    Usuários sem tenant (MASTER) caem em uma chave própria identificada
    pelo ``user_id`` para nunca cruzar dados entre tenants.
    """
    versao = _dashboard_cache_version()
    try:
        ano = session.get('ano_ativo') or datetime.now().year
    except Exception:
        ano = datetime.now().year
    try:
        emp = empresa_id_atual()
    except Exception:
        emp = None
    if emp:
        scope = f"emp:{emp}"
    else:
        # MASTER ou usuário fora de empresa: isola pelo id para não vazar.
        scope = f"u:{getattr(current_user, 'id', 'anon')}"
    return f"dashboard:v{versao}:{scope}:ano:{ano}"


@login_manager.user_loader
def load_user(user_id):
    return Usuario.query.get(int(user_id))


@app.template_filter('formato_moeda')
def formato_moeda(value):
    """Formata um número como moeda brasileira: R$ 1.000,00. Aceita negativos (ex: -R$ 120,00)."""
    if value is None:
        return 'R$ 0,00'
    try:
        num = float(value)
        negativo = num < 0
        s = f'R$ {abs(num):,.2f}'.replace(',', 'X').replace('.', ',').replace('X', '.')
        return ('-' + s) if negativo else s
    except (ValueError, TypeError):
        return 'R$ 0,00'


@app.template_filter('format_cnpj')
def format_cnpj(value):
    """Formata CNPJ (14 dígitos) como 00.000.000/0001-00 ou CPF (11 dígitos) como 000.000.000-00.
    Se vazio ou inválido, retorna o valor original."""
    if value is None:
        return ''
    s = str(value).strip()
    if not s:
        return ''
    digits = re.sub(r'\D', '', s)
    if len(digits) == 14:
        return f"{digits[:2]}.{digits[2:5]}.{digits[5:8]}/{digits[8:12]}-{digits[12:]}"
    if len(digits) == 11:
        return f"{digits[:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9:]}"
    return s


@app.template_filter('cloudinary_thumb')
def _cloudinary_thumb_url(url: str, w: int = 300, h: int = 300) -> str:
    """Gera URL de thumbnail Cloudinary inserindo transformações no path.

    Exemplo:
        original:  https://res.cloudinary.com/<cloud>/image/upload/v1/foo/bar.jpg
        thumb:     https://res.cloudinary.com/<cloud>/image/upload/w_300,h_300,c_fill,q_auto,f_auto/v1/foo/bar.jpg

    Reduz dramaticamente o peso de imagens em listagens (até 90% menor que
    o original). Para clientes modernos, ``f_auto`` entrega WebP/AVIF
    automaticamente; ``q_auto`` ajusta a qualidade sem perda perceptível.

    Não-Cloudinary ou strings inválidas: devolve a URL original (no-op).

    Permanece em ``app.py`` (em vez de routes/produtos.py) porque é
    registrado como filtro Jinja global — usado em base.html, perfil.html
    e dentro do blueprint de produtos via ``from app import _cloudinary_thumb_url``.
    """
    if not url or 'res.cloudinary.com' not in url:
        return url
    marker = '/upload/'
    idx = url.find(marker)
    if idx < 0:
        return url
    prefix = url[: idx + len(marker)]
    suffix = url[idx + len(marker):]
    transform = f"w_{w},h_{h},c_fill,q_auto,f_auto/"
    # Evita duplicar a transformação se a URL já tiver uma w_/h_/q_auto manualmente.
    if suffix.startswith(('w_', 'h_', 'c_', 'q_', 'f_')):
        return url
    return f"{prefix}{transform}{suffix}"


@app.context_processor
def inject_count_outros():
    """Disponibiliza count_outros (produtos com tipo OUTROS) em todos os templates.

    Multi-tenant: restringe a contagem ao tenant atual. Usuarios MASTER ou
    nao logados recebem 0 (sem badge).
    """
    try:
        if not current_user.is_authenticated:
            return {'count_outros': 0}
        eid = getattr(current_user, 'empresa_id', None)
        if eid is None:
            return {'count_outros': 0}
        n = Produto.query.filter(
            Produto.empresa_id == eid,
            Produto.tipo == 'OUTROS',
        ).count()
        return {'count_outros': n}
    except Exception:
        return {'count_outros': 0}


@app.context_processor
def inject_ano_ativo():
    """Disponibiliza ano_ativo e anos_disponiveis em todos os templates."""
    ano_atual = datetime.now().year
    # Inicializar sessão se necessário
    if 'ano_ativo' not in session:
        session['ano_ativo'] = ano_atual
    
    ano_ativo = session.get('ano_ativo', ano_atual)
    
    # Gerar lista de anos disponíveis (do ano atual até 2 anos no futuro)
    # Também buscar anos com dados existentes no banco
    anos_disponiveis = set(range(2024, ano_atual + 2))
    
    try:
        # Multi-tenant: so considera anos com dados do tenant atual.
        eid = None
        if current_user.is_authenticated:
            eid = getattr(current_user, 'empresa_id', None)

        # Buscar anos distintos com vendas
        q_vendas = db.session.query(
            func.distinct(extract('year', Venda.data_venda))
        ).filter(Venda.data_venda.isnot(None))
        if eid is not None:
            q_vendas = q_vendas.filter(Venda.empresa_id == eid)
        for (ano,) in q_vendas.all():
            if ano:
                anos_disponiveis.add(int(ano))

        # Buscar anos distintos com produtos
        q_produtos = db.session.query(
            func.distinct(extract('year', Produto.data_chegada))
        ).filter(Produto.data_chegada.isnot(None))
        if eid is not None:
            q_produtos = q_produtos.filter(Produto.empresa_id == eid)
        for (ano,) in q_produtos.all():
            if ano:
                anos_disponiveis.add(int(ano))
    except Exception:
        pass
    
    # Ordenar anos (mais recente primeiro)
    anos_disponiveis = sorted(anos_disponiveis, reverse=True)
    
    return {
        'ano_ativo': ano_ativo,
        'anos_disponiveis': anos_disponiveis
    }


@app.context_processor
def injetar_datas():
    """Disponibiliza hoje e ontem (fuso Brasil) em todos os templates para destaque visual por data."""
    try:
        hoje = date.today()
        ontem = hoje - timedelta(days=1)
        return dict(hoje=hoje, ontem=ontem)
    except Exception:
        hoje = date.today()
        return dict(hoje=hoje, ontem=hoje - timedelta(days=1))


def _contar_cobrancas_pendentes_visiveis():
    """Conta pedidos vencidos visíveis para o usuário atual.

    Optimizações aplicadas (C2):
    - Para admin: uma única query SQL com COUNT + filtro de data (sem carregar objetos).
    - Para usuário comum: carrega apenas os campos essenciais (id, situacao,
      data_vencimento, caminho_boleto) sem joinedload desnecessário, e
      limita a 500 registros defensivamente.

    Multi-tenant: restringe a contagem ao tenant do usuario atual. Usuarios
    MASTER (sem empresa_id) nao veem badge de cobrancas, o que eh intencional.
    """
    try:
        hoje = get_hoje_brasil()
        ano_ativo = session.get('ano_ativo', datetime.now().year)
        eid = empresa_id_atual()
        if eid is None:
            return 0

        if _e_admin_tenant():
            # Caminho rápido para admin: contar via SQL puro usando data_vencimento direta.
            # Vendas sem data_vencimento própria precisariam de JOIN com Documento —
            # aceitamos uma sub-estimativa leve aqui em troca de performance.
            total_direto = db.session.query(func.count(Venda.id)).filter(
                Venda.empresa_id == eid,
                extract('year', Venda.data_venda) == ano_ativo,
                Venda.situacao.in_(['PENDENTE', 'PARCIAL']),
                Venda.data_vencimento.isnot(None),
                Venda.data_vencimento < hoje,
            ).scalar() or 0
            return total_direto

        # Para usuário comum: carrega colunas essenciais (sem usuario_id — coluna inexistente em Venda).
        # Conta todas as cobranças vencidas do período; o badge é informativo para todos os usuários.
        vendas = query_tenant(Venda).with_entities(
            Venda.id, Venda.situacao, Venda.data_vencimento,
            Venda.caminho_boleto
        ).filter(
            extract('year', Venda.data_venda) == ano_ativo,
            Venda.situacao.in_(['PENDENTE', 'PARCIAL']),
        ).limit(500).all()

        # Pré-fetch de documentos via .in_() — UMA query, não N.
        caminhos_boleto = list({
            str(r.caminho_boleto or '').strip()
            for r in vendas
            if str(r.caminho_boleto or '').strip()
        })
        docs_por_caminho: dict = {}
        if caminhos_boleto:
            # Auditoria P0 (A2): escopa Documentos pelo tenant — caminho_arquivo
            # pode coincidir entre empresas e dispararia leitura cross-tenant.
            docs_boleto = query_documentos_tenant().with_entities(
                Documento.caminho_arquivo, Documento.data_vencimento
            ).filter(Documento.caminho_arquivo.in_(caminhos_boleto)).all()
            docs_por_caminho = {str(d.caminho_arquivo or '').strip(): d for d in docs_boleto}

        total = 0
        for r in vendas:
            dv = r.data_vencimento
            if dv is None:
                cb = str(r.caminho_boleto or '').strip()
                doc = docs_por_caminho.get(cb)
                if doc:
                    dv = doc.data_vencimento
            if dv is None:
                continue
            if dv < hoje:
                total += 1
        return total
    except Exception:
        return 0


@app.context_processor
def injetar_alertas():
    """Disponibiliza alertas_sistema em todos os templates.

    Optimização C2: o cálculo pesado só é chamado em requisições HTML de
    páginas completas (não em XHR/API/estáticos). O resultado é guardado na
    sessão Flask com TTL de 60 s para não repetir a query em cliques rápidos.
    """
    alertas = []
    try:
        if not current_user.is_authenticated:
            return dict(alertas_sistema=alertas)

        # Não executar em chamadas AJAX/API (reduz carga em requisições frequentes).
        is_ajax = (
            request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or request.args.get('ajax') == '1'
        )
        if is_ajax:
            return dict(alertas_sistema=alertas)

        if getattr(current_user, 'notifica_boletos', False):
            _cache_key = f'_alertas_boletos_ts'
            _cache_val = f'_alertas_boletos_val'
            _agora = datetime.now().timestamp()
            _ts = session.get(_cache_key, 0)

            if _agora - _ts > 60:
                boletos_vencendo = _contar_cobrancas_pendentes_visiveis()
                session[_cache_key] = _agora
                session[_cache_val] = boletos_vencendo
            else:
                boletos_vencendo = session.get(_cache_val, 0)

            if boletos_vencendo > 0:
                alertas.append({
                    'id': 'alerta_boletos',
                    'titulo': 'Cobranças Pendentes',
                    'mensagem': f'Você tem {boletos_vencendo} boleto(s) vencido(s) para envio ao fornecedor.',
                    'cor': 'red',
                    'cor_border': 'border-red-500',
                    'cor_text': 'text-red-600',
                    'link': url_for('vendas.listar_vendas', filtro_vencidos=1, ordem_data='decrescente')
                })
    except Exception:
        pass
    return dict(alertas_sistema=alertas)


@app.route('/alterar_ano/<int:ano>', methods=['POST'])
@login_required
def alterar_ano(ano):
    """Altera o ano ativo na sessão e redireciona de volta."""
    ano_atual = datetime.now().year
    # Validar ano (permitir de 2020 até ano_atual + 2)
    if 2020 <= ano <= ano_atual + 2:
        session['ano_ativo'] = ano
        flash(f'Ano alterado para {ano}.', 'success')
    else:
        flash(f'Ano inválido: {ano}', 'error')
    
    # Redirecionar de volta para a página anterior ou dashboard
    referrer = request.referrer
    if referrer and _is_safe_next_url(referrer):
        return redirect(referrer)
    return redirect(url_for('dashboard.dashboard'))


def admin_required(f):
    """Redireciona para o dashboard com aviso se o usuário não for admin."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        if not current_user.is_admin():
            flash('Acesso restrito ao Administrador.', 'warning')
            return redirect(url_for('dashboard.dashboard'))
        return f(*args, **kwargs)
    return wrapped


# ============================================================
# MULTI-TENANT — Fase 2
# ============================================================
# Contrato:
#   * empresa_id_atual()   -> int | None
#   * master_required(f)   -> decorator: permite APENAS perfil MASTER
#   * tenant_required(f)   -> decorator: exige empresa_id no usuario;
#                              MASTER eh redirecionado para o painel.
#   * query_tenant(Model)  -> Model.query filtrado por empresa_id do usuario.
#
# Regra de UX definida na Fase 2:
#   MASTER so opera em /master-admin e rotas correlatas. Ao tentar acessar
#   rotas operacionais (produtos, vendas, caixa, etc.), eh redirecionado
#   de volta ao painel. DONO e FUNCIONARIO so veem dados do seu tenant.

def empresa_id_atual():
    """Retorna o empresa_id do usuario logado (ou None).

    NUNCA deve ser chamado por usuario MASTER dentro de rotas operacionais:
    o decorator tenant_required ja faz essa guarda antes de a query rodar.
    """
    if not current_user.is_authenticated:
        return None
    return getattr(current_user, 'empresa_id', None)


def master_required(f):
    """Restringe acesso a usuarios com perfil=MASTER (super admin do SaaS)."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        if not getattr(current_user, 'is_master', lambda: False)():
            # Nao expomos 404/403 especifico: flash neutro para nao vazar
            # a existencia do painel master para contas comuns.
            flash('Acesso restrito.', 'warning')
            return redirect(url_for('dashboard.dashboard'))
        return f(*args, **kwargs)
    return wrapped


def tenant_required(f):
    """Exige usuario logado com empresa_id definido.

    * Usuario MASTER eh redirecionado para o painel /master-admin
      (MASTER nao opera rotas comuns).
    * Usuario sem empresa_id (edge case de dados corrompidos) eh
      deslogado com flash de erro.
    """
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('auth.login'))
        if getattr(current_user, 'is_master', lambda: False)():
            return redirect(url_for('master.master_admin'))
        if not getattr(current_user, 'empresa_id', None):
            flash(
                'Seu usuario nao esta vinculado a nenhuma empresa. '
                'Contate o administrador.',
                'error',
            )
            return redirect(url_for('auth.login'))
        return f(*args, **kwargs)
    return wrapped


def query_tenant(model):
    """Retorna um Query ja filtrado por empresa_id do usuario atual.

    Uso:
        produtos = query_tenant(Produto).all()
        cliente = query_tenant(Cliente).filter_by(id=id).first_or_404()

    Deve ser usado SEMPRE em rotas operacionais ao inves de Model.query.
    Em rotas que ja usam `tenant_required`, o empresa_id eh garantido.
    """
    eid = empresa_id_atual()
    if eid is None:
        # Com tenant_required aplicado isto nunca ocorre em rotas protegidas;
        # para scripts/jobs sem request context retornamos None para forcar
        # erro explicito ao inves de vazar dados.
        return model.query.filter(db.false())
    return model.query.filter_by(empresa_id=eid)


def query_documentos_tenant():
    """Atalho semântico para `query_tenant(Documento)` — usar em rotas que
    listam/filtram documentos do próprio tenant após a auditoria P0 (A2).

    Garante que QUALQUER consulta sobre Documento numa rota operacional
    seja restrita ao empresa_id do usuário atual.
    """
    return query_tenant(Documento)


def _is_safe_next_url(target):
    """Evita open redirect: só permite redirecionamentos internos para o mesmo host."""
    if not target:
        return False
    ref_url = urllib.parse.urlparse(request.host_url)
    test_url = urllib.parse.urlparse(urllib.parse.urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc


def _empresa_id_para_documento(venda_id=None, fallback_user_id=None):
    """Resolve o `empresa_id` que deve ser gravado num novo Documento.

    Ordem de prioridade:
        1. `current_user.empresa_id` se houver request autenticada (caminho
           normal: rotas operacionais com @tenant_required).
        2. Empresa da Venda à qual o documento será vinculado (cobre o
           pipeline de processamento de PDFs que pode rodar fora do tenant
           do uploader — ex.: bot/cron usando user_id_forcado).
        3. Empresa do `fallback_user_id` (caminho de scripts/bots que
           passam um usuário canônico do tenant).
        4. Primeira Empresa ativa cadastrada (último recurso para
           preservar o dado histórico em vez de criar Documento órfão).

    Returns:
        int | None: empresa_id resolvido; None apenas em ambientes vazios
        (sem nenhuma Empresa cadastrada) — caso defensivo.
    """
    eid = None
    try:
        if getattr(current_user, 'is_authenticated', False):
            eid = getattr(current_user, 'empresa_id', None)
    except Exception:
        eid = None
    if eid:
        return int(eid)
    if venda_id:
        try:
            v = Venda.query.with_entities(Venda.empresa_id).filter_by(id=int(venda_id)).first()
            if v and v.empresa_id:
                return int(v.empresa_id)
        except Exception:
            db.session.rollback()
    if fallback_user_id:
        try:
            u = Usuario.query.with_entities(Usuario.empresa_id).filter_by(id=int(fallback_user_id)).first()
            if u and u.empresa_id:
                return int(u.empresa_id)
        except Exception:
            db.session.rollback()
    try:
        empresa = Empresa.query.filter_by(ativo=True).order_by(Empresa.id.asc()).first()
        if empresa:
            return int(empresa.id)
    except Exception:
        db.session.rollback()
    return None


def _e_admin_tenant():
    """True se o usuário atual é admin DENTRO do próprio tenant.

    Inclui MASTER (admin global do SaaS) e DONO da empresa atual. NÃO confere
    nada cross-tenant: para isso, combine com `_mesmo_tenant(recurso)` ou use
    `query_tenant()`.

    Use este helper em rotas que ANTES contavam com `current_user.is_admin()`
    para liberar visões/operações administrativas DO TENANT (relatórios,
    fast-paths SQL, listagens completas dentro do tenant). Após a auditoria,
    `is_admin()` virou MASTER-only; este helper preserva o comportamento
    administrativo do DONO sem reabrir a brecha cross-tenant.
    """
    if not getattr(current_user, 'is_authenticated', False):
        return False
    if getattr(current_user, 'is_master', lambda: False)():
        return True
    return getattr(current_user, 'is_dono', lambda: False)()


def _mesmo_tenant(recurso):
    """Compara o empresa_id do recurso com o empresa_id do usuário atual.

    Multi-tenant guard: a checagem PRIMORDIAL antes de qualquer outra regra
    de permissão por recurso. Se o recurso pertence a outra empresa, bloqueia
    imediatamente — nem mesmo um DONO consegue tocar dados de tenant alheio.

    Regras:
        * recurso é None ............... retorna False (defensivo).
        * recurso.empresa_id é None .... retorna True (legado órfão; será
          adotado pela própria operação ou tratado pelos helpers seed).
        * empresa_id bate com usuário .. retorna True.
        * caso contrário ............... retorna False (cross-tenant).
    """
    if recurso is None:
        return False
    eid_recurso = getattr(recurso, 'empresa_id', None)
    if eid_recurso is None:
        # Legado pré-multi-tenant: deixa passar; criação/migration popula depois.
        return True
    eid_usuario = getattr(current_user, 'empresa_id', None)
    if eid_usuario is None:
        return False
    return int(eid_recurso) == int(eid_usuario)


def _usuario_pode_gerenciar_documento(documento):
    """Permissão por recurso para Documento, blindada por tenant.

    Ordem de checagem (Fase 2 — pós auditoria P0):
        1. MASTER do SaaS: passe livre (única exceção global).
        2. Documento de OUTRA empresa: bloqueio imediato. Nem DONO de tenant
           alheio toca aqui — `is_admin()` agora só é True para MASTER.
        3. Dono/Funcionário do mesmo tenant:
              - DONO da empresa: pode gerenciar qualquer documento do tenant.
              - FUNCIONARIO: só gerencia documentos próprios (usuario_id) ou
                documentos órfãos (sem usuario_id) para preservar o fluxo
                operacional legado.
    """
    if documento is None:
        return False
    # 1) MASTER passa livre.
    if getattr(current_user, 'is_master', lambda: False)():
        return True
    # 2) Guard cross-tenant: bloqueia ANTES de qualquer outra checagem.
    if not _mesmo_tenant(documento):
        return False
    # 3) DONO do tenant gerencia tudo dentro da própria empresa.
    if getattr(current_user, 'is_dono', lambda: False)():
        return True
    # 4) FUNCIONARIO: ownership explícito ou documento órfão (legado).
    uid = getattr(current_user, 'id', None)
    dono_doc = getattr(documento, 'usuario_id', None)
    return dono_doc == uid or dono_doc is None


def _usuario_pode_gerenciar_venda(venda):
    """Permissão por recurso para Venda, blindada por tenant.

    Mesma ordem do `_usuario_pode_gerenciar_documento`:
        1. MASTER passa livre.
        2. Venda de outra empresa => bloqueio imediato.
        3. DONO do tenant => libera tudo dentro do próprio tenant.
        4. FUNCIONARIO => libera se for venda órfã (sem documentos com dono)
           ou se houver documento explicitamente associado a ele.
    """
    if venda is None:
        return False
    # 1) MASTER.
    if getattr(current_user, 'is_master', lambda: False)():
        return True
    # 2) Guard cross-tenant.
    if not _mesmo_tenant(venda):
        return False
    # 3) DONO do tenant: livre dentro da própria empresa.
    if getattr(current_user, 'is_dono', lambda: False)():
        return True
    # 4) FUNCIONARIO: regra de ownership operacional via documentos.
    uid = getattr(current_user, 'id', None)
    docs = getattr(venda, 'documentos', None) or []
    if not docs:
        # Legado: venda antiga sem vínculo de documento dentro do mesmo tenant.
        return True
    if any(getattr(d, 'usuario_id', None) == uid for d in docs):
        return True
    return all(getattr(d, 'usuario_id', None) is None for d in docs)


def _assumir_ownership_venda_orfa(venda):
    """Em vendas órfãs, atribui ao usuário atual os documentos sem dono."""
    if venda is None or _e_admin_tenant():
        return 0
    docs = getattr(venda, 'documentos', None) or []
    uid = getattr(current_user, 'id', None)
    atualizados = 0
    for doc in docs:
        if getattr(doc, 'usuario_id', None) is None:
            doc.usuario_id = uid
            atualizados += 1
    return atualizados


def _resposta_sem_permissao():
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify(ok=False, mensagem='Acesso negado.'), 403
    return "Forbidden", 403


def _produto_com_lock(produto_id):
    """Busca produto com lock pessimista para evitar corrida de estoque."""
    return query_tenant(Produto).filter(Produto.id == int(produto_id)).with_for_update().first()


def _public_id_cloudinary_from_url(url):
    """Extrai public_id de uma URL do Cloudinary (fallback legado)."""
    try:
        parsed = urllib.parse.urlparse(url or '')
        path = (parsed.path or '').strip('/')
        marker = '/upload/'
        if marker not in path:
            return None
        tail = path.split(marker, 1)[1]
        parts = tail.split('/')
        if parts and re.match(r'^v\d+$', parts[0]):
            parts = parts[1:]
        if not parts:
            return None
        joined = '/'.join(parts)
        return re.sub(r'\.[A-Za-z0-9]+$', '', joined)
    except Exception:
        return None


def _deletar_cloudinary_seguro(public_id=None, url=None, resource_type='image'):
    """Tenta deletar recurso no Cloudinary sem interromper a transação."""
    pid = (public_id or '').strip() or _public_id_cloudinary_from_url(url)
    if not pid:
        return False
    if not (os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL')):
        return False
    try:
        cloudinary.uploader.destroy(pid, resource_type=resource_type, timeout=_EXTERNAL_TIMEOUT)
        return True
    except Exception as ex:
        app.logger.error(f"Aviso: falha ao deletar Cloudinary ({pid}): {ex}")
        return False


def _resolver_caminho_documento_seguro(subpasta, nome_arquivo):
    """Monta caminho absoluto seguro dentro de documentos_entrada/<subpasta>."""
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'documentos_entrada')
    base_permitida = os.path.normpath(os.path.join(base_dir, subpasta))
    candidato = os.path.normpath(os.path.join(base_permitida, nome_arquivo or ''))
    if not (candidato == base_permitida or candidato.startswith(base_permitida + os.sep)):
        return None
    return candidato


def popular_fornecedores_iniciais():
    fornecedores_padrao = ['ARMAZEM LACERDA', 'PATY', 'DESTAK', 'SERVE BEM']
    houve_insercao = False
    for nome in fornecedores_padrao:
        nome_norm = str(nome or '').strip().upper()
        if not nome_norm:
            continue
        if not Fornecedor.query.filter(func.upper(Fornecedor.nome) == nome_norm).first():
            db.session.add(Fornecedor(nome=nome_norm))
            houve_insercao = True
    if houve_insercao:
        db.session.commit()


# Bootstrap do banco: NÃO executa na importação se SKIP_DB_BOOTSTRAP=1 (usado pelo migrate_recreate_db.py)
if not os.environ.get('SKIP_DB_BOOTSTRAP'):
    with app.app_context():
        db.create_all()
        try:
            popular_fornecedores_iniciais()
        except Exception:
            db.session.rollback()
        inspector = inspect(db.engine)
        colunas_cache = {}

        def _colunas_tabela(nome_tabela):
            """Retorna set de colunas da tabela (com cache local)."""
            if nome_tabela not in colunas_cache:
                colunas_cache[nome_tabela] = {c['name'] for c in inspector.get_columns(nome_tabela)}
            return colunas_cache[nome_tabela]

        def _adicionar_coluna_se_ausente(nome_tabela, nome_coluna, ddl_coluna):
            """Adiciona coluna apenas quando ausente (compatível SQLite/PostgreSQL)."""
            if nome_coluna in _colunas_tabela(nome_tabela):
                return False
            db.session.execute(text(f'ALTER TABLE {nome_tabela} ADD COLUMN {nome_coluna} {ddl_coluna}'))
            db.session.commit()
            # Atualiza cache local após alteração
            colunas_cache[nome_tabela].add(nome_coluna)
            return True

        try:
            _adicionar_coluna_se_ausente('produtos', 'preco_venda_alvo', 'NUMERIC(10,2)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: adicionar quantidade_entrada e popular com estoque_atual existente
        try:
            _adicionar_coluna_se_ausente('produtos', 'quantidade_entrada', 'INTEGER DEFAULT 0')
            db.session.execute(text('UPDATE produtos SET quantidade_entrada = estoque_atual WHERE quantidade_entrada = 0 OR quantidade_entrada IS NULL'))
            db.session.commit()
        except (OperationalError, Exception):
            try:
                db.session.execute(text('UPDATE produtos SET quantidade_entrada = estoque_atual WHERE quantidade_entrada = 0 OR quantidade_entrada IS NULL'))
                db.session.commit()
            except Exception:
                db.session.rollback()
        # Migração: quantidade_devolvida em produtos (rastro de devolução ao fornecedor)
        try:
            _adicionar_coluna_se_ausente('produtos', 'quantidade_devolvida', 'INTEGER NOT NULL DEFAULT 0')
        except (OperationalError, Exception):
            db.session.rollback()
            # Fallback: alguns SQLites não aceitam NOT NULL sem reescrever a tabela
            try:
                _adicionar_coluna_se_ausente('produtos', 'quantidade_devolvida', 'INTEGER DEFAULT 0')
            except Exception:
                db.session.rollback()
        try:
            db.session.execute(text('UPDATE produtos SET quantidade_devolvida = 0 WHERE quantidade_devolvida IS NULL'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: garantir tabela documentos (cross-database)
        try:
            Documento.__table__.create(bind=db.engine, checkfirst=True)
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração P0 (Auditoria — A2): adicionar empresa_id em documentos.
        # Faz seed retroativo a partir de Venda; órfãos vão para a primeira
        # empresa existente para preservar o dado histórico sem deixar
        # registros invisíveis em produção. Idempotente.
        try:
            criou_coluna = _adicionar_coluna_se_ausente(
                'documentos', 'empresa_id', 'INTEGER REFERENCES empresas(id)'
            )
        except (OperationalError, Exception):
            db.session.rollback()
            # Fallback para SQLite legado que não aceita REFERENCES inline.
            try:
                criou_coluna = _adicionar_coluna_se_ausente(
                    'documentos', 'empresa_id', 'INTEGER'
                )
            except Exception:
                db.session.rollback()
                criou_coluna = False
        try:
            db.session.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_documentos_empresa_id ON documentos(empresa_id)'
            ))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        try:
            # Seed via Venda: documentos vinculados herdam o tenant da Venda.
            db.session.execute(text("""
                UPDATE documentos
                   SET empresa_id = (
                       SELECT v.empresa_id FROM vendas v WHERE v.id = documentos.venda_id
                   )
                 WHERE documentos.empresa_id IS NULL
                   AND documentos.venda_id IS NOT NULL
            """))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        try:
            # Órfãos legados: caem no primeiro tenant disponível para
            # preservar o dado. Em produção, cuidar disso manualmente
            # antes de o segundo cliente entrar.
            empresa_fallback = db.session.execute(
                text('SELECT id FROM empresas ORDER BY id ASC LIMIT 1')
            ).scalar()
            if empresa_fallback:
                db.session.execute(text("""
                    UPDATE documentos
                       SET empresa_id = :eid
                     WHERE empresa_id IS NULL
                """), {"eid": int(empresa_fallback)})
                db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: caminho_pdf em vendas (PDF vinculado) — apenas para DBs antigos
        try:
            _adicionar_coluna_se_ausente('vendas', 'caminho_pdf', 'VARCHAR(500)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: caminho_boleto e caminho_nf em vendas
        for col in ('caminho_boleto', 'caminho_nf'):
            try:
                _adicionar_coluna_se_ausente('vendas', col, 'VARCHAR(500)')
            except (OperationalError, Exception):
                db.session.rollback()
        # Índice em vendas.nf para buscas por NF
        try:
            db.session.execute(text('CREATE INDEX IF NOT EXISTS ix_vendas_nf ON vendas(nf)'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Cache OCR: nf_extraida em documentos (evita re-rodar OCR)
        try:
            _adicionar_coluna_se_ausente('documentos', 'nf_extraida', 'VARCHAR(50)')
        except (OperationalError, Exception):
            db.session.rollback()
        try:
            db.session.execute(text("UPDATE documentos SET nf_extraida = numero_nf WHERE nf_extraida IS NULL AND numero_nf IS NOT NULL"))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: usuario_id em documentos (quem processou/recuperou)
        try:
            _adicionar_coluna_se_ausente('documentos', 'usuario_id', 'INTEGER')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: conteudo_binario em documentos (PDF armazenado no banco)
        try:
            uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
            col_type = 'BYTEA' if 'postgres' in uri.lower() else 'BLOB'
            _adicionar_coluna_se_ausente('documentos', 'conteudo_binario', col_type)
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: url_arquivo e public_id em documentos (Cloudinary)
        for col, col_def in [('url_arquivo', 'VARCHAR(500)'), ('public_id', 'VARCHAR(200)')]:
            try:
                _adicionar_coluna_se_ausente('documentos', col, col_def)
            except (OperationalError, Exception):
                db.session.rollback()
        # Migração: public_id em produto_fotos (Cloudinary)
        try:
            _adicionar_coluna_se_ausente('produto_fotos', 'public_id', 'VARCHAR(200)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: profile_image_url em usuarios (foto de perfil)
        try:
            _adicionar_coluna_se_ausente('usuarios', 'profile_image_url', 'VARCHAR(500)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: nome em usuarios (nome completo/real)
        try:
            _adicionar_coluna_se_ausente('usuarios', 'nome', 'VARCHAR(100)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: email em usuarios
        try:
            _adicionar_coluna_se_ausente('usuarios', 'email', 'VARCHAR(150)')
        except (OperationalError, Exception):
            db.session.rollback()
        # --- MIGRACAO AUTOMATICA: Colunas de notificacao em usuarios (cross-database) ---
        try:
            colunas_notificacao = ('notifica_boletos', 'notifica_radar', 'notifica_logistica', 'notifica_frase')
            adicionadas = []

            for col in colunas_notificacao:
                if _adicionar_coluna_se_ausente('usuarios', col, 'BOOLEAN DEFAULT 1'):
                    adicionadas.append(col)

            if adicionadas:
                app.logger.info(f"Migração: Colunas de notificação adicionadas: {', '.join(adicionadas)}")
            else:
                app.logger.info("Migração: Colunas de notificação já estavam presentes.")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Migração notificação (usuarios) falhou: {e}")
        # Migração: tabela de inscrições Web Push
        try:
            PushSubscription.__table__.create(bind=db.engine, checkfirst=True)
            db.session.commit()
            app.logger.info("Migração: tabela push_subscriptions verificada/criada com sucesso.")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Migração push_subscriptions falhou: {e}")
        # Migração: data_vencimento em vendas (vencimento do boleto extraído do PDF)
        try:
            _adicionar_coluna_se_ausente('vendas', 'data_vencimento', 'DATE')
        except (OperationalError, Exception):
            db.session.rollback()
        # Backfill data_vencimento em vendas a partir dos Documentos (boletos) vinculados
        try:
            for v in Venda.query.filter(Venda.caminho_boleto.isnot(None)).filter(Venda.data_vencimento.is_(None)):
                doc = Documento.query.filter_by(caminho_arquivo=v.caminho_boleto).first()
                if doc and doc.data_vencimento:
                    v.data_vencimento = doc.data_vencimento
            db.session.commit()
        except Exception:
            db.session.rollback()
        # Verificar se caminho_pdf ainda existe (não foi dropado em migração anterior)
        uri = app.config.get("SQLALCHEMY_DATABASE_URI", "")
        tem_caminho_pdf = False
        if uri and uri.startswith("sqlite"):
            colunas_vendas = [r[1] for r in db.session.execute(text('PRAGMA table_info(vendas)')).fetchall()]
            tem_caminho_pdf = 'caminho_pdf' in colunas_vendas
        if tem_caminho_pdf:
            try:
                rp = db.session.execute(text("SELECT id, caminho_pdf FROM vendas WHERE caminho_pdf IS NOT NULL AND trim(caminho_pdf) != ''"))
                for row in rp:
                    vid, path = row[0], (row[1] or '').strip()
                    if not path:
                        continue
                    doc = Documento.query.filter_by(venda_id=vid, caminho_arquivo=path).first()
                    v = Venda.query.get(vid)
                    if not v:
                        continue
                    if doc:
                        if doc.tipo == 'BOLETO':
                            v.caminho_boleto = path
                        else:
                            v.caminho_nf = path
                    else:
                        v.caminho_boleto = path
                db.session.commit()
            except Exception:
                db.session.rollback()
            try:
                db.session.execute(text('ALTER TABLE vendas DROP COLUMN caminho_pdf'))
                db.session.commit()
            except (OperationalError, Exception):
                db.session.rollback()
        # Migração: endereco em clientes (endereço completo para Google Maps)
        try:
            _adicionar_coluna_se_ausente('clientes', 'endereco', 'VARCHAR(255)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: status_entrega em vendas (status logístico independente do financeiro)
        try:
            _adicionar_coluna_se_ausente('vendas', 'status_entrega', "VARCHAR(50) DEFAULT 'PENDENTE'")
        except (OperationalError, Exception):
            db.session.rollback()
        try:
            db.session.execute(text("UPDATE vendas SET status_entrega = 'PENDENTE' WHERE status_entrega IS NULL"))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: valor_pago em vendas (abatimento inteligente / pagamento parcial)
        try:
            _adicionar_coluna_se_ausente('vendas', 'valor_pago', 'FLOAT DEFAULT 0.0')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: forma_pagamento em vendas (Dinheiro, Pix, Boleto, Cheque, etc.)
        try:
            _adicionar_coluna_se_ausente('vendas', 'forma_pagamento', 'VARCHAR(50)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: setor em lancamentos_caixa (GERAL/BACALHAU)
        try:
            _adicionar_coluna_se_ausente('lancamentos_caixa', 'setor', "VARCHAR(50) NOT NULL DEFAULT 'GERAL'")
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: cliente_avulso em vendas (identificação quando cliente é "Desconhecido")
        try:
            _adicionar_coluna_se_ausente('vendas', 'cliente_avulso', 'VARCHAR(100)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: tipo_operacao em vendas (VENDA/PERDA)
        try:
            _adicionar_coluna_se_ausente('vendas', 'tipo_operacao', "VARCHAR(20) NOT NULL DEFAULT 'VENDA'")
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: lucro_percentual em vendas (opcional por item)
        try:
            _adicionar_coluna_se_ausente('vendas', 'lucro_percentual', 'NUMERIC(6,2)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: status_envio em lancamentos_caixa (ciclo de vida de cheques)
        try:
            _adicionar_coluna_se_ausente('lancamentos_caixa', 'status_envio', "VARCHAR(20) DEFAULT 'Não Enviado'")
        except (OperationalError, Exception):
            db.session.rollback()
        try:
            db.session.execute(text("UPDATE lancamentos_caixa SET status_envio = 'Não Enviado' WHERE lower(forma_pagamento) LIKE '%cheque%' AND (status_envio IS NULL OR trim(status_envio) = '')"))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: telefone em clientes (WhatsApp / contato)
        try:
            _adicionar_coluna_se_ausente('clientes', 'telefone', 'VARCHAR(20)')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: ativo em clientes (soft delete / inativação)
        try:
            _adicionar_coluna_se_ausente('clientes', 'ativo', 'BOOLEAN NOT NULL DEFAULT TRUE')
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: tabela de auditoria de ações (log de atividades)
        try:
            LogAtividade.__table__.create(bind=db.engine, checkfirst=True)
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: índices para campos filtráveis (performance em 10k+ registros)
        for idx_sql in [
            'CREATE INDEX IF NOT EXISTS ix_clientes_cnpj ON clientes(cnpj)',
            'CREATE INDEX IF NOT EXISTS ix_vendas_empresa ON vendas(empresa_faturadora)',
            'CREATE INDEX IF NOT EXISTS ix_vendas_forma_pag ON vendas(forma_pagamento)',
            'CREATE INDEX IF NOT EXISTS ix_vendas_status_entrega ON vendas(status_entrega)',
        ]:
            try:
                db.session.execute(text(idx_sql))
                db.session.commit()
            except (OperationalError, Exception):
                db.session.rollback()
        # Jhones sempre admin; criar se não existir
        u = Usuario.query.filter_by(username='Jhones').first()
        if not u:
            import secrets as _secrets
            admin_pass = os.environ.get('ADMIN_INITIAL_PASS')
            if not admin_pass:
                admin_pass = _secrets.token_urlsafe(12)
                app.logger.info(f"\n{'='*60}")
                app.logger.info(f"  ATENÇÃO: Senha do admin gerada automaticamente:")
                app.logger.info(f"  Usuário: Jhones")
                app.logger.info(f"  Senha:   {admin_pass}")
                app.logger.info(f"  Altere imediatamente em Configurações > Usuários.")
                app.logger.info(f"{'='*60}\n")
            u = Usuario(username='Jhones', password_hash=generate_password_hash(admin_pass), role='admin')
            db.session.add(u)
            db.session.commit()




# ========== AUTENTICAÇÃO ==========

def _pos_login_landing(user):
    """Define a pagina inicial apos autenticacao segundo o perfil.

    * MASTER         -> /master-admin (nao opera rotas comuns).
    * DONO/FUNCIONARIO (com empresa_id) -> /dashboard.
    * Qualquer outro (sem empresa_id) -> login com flash; eh um estado
      invalido para rotas operacionais.
    """
    if getattr(user, 'is_master', lambda: False)():
        return url_for('master.master_admin')
    if not getattr(user, 'empresa_id', None):
        return None
    return url_for('dashboard.dashboard')



def get_config(empresa_id=None):
    """Retorna a Configuracao do tenant atual (ou de um empresa_id explicito).

    Multi-tenant: cada empresa tem sua propria Configuracao (codigo_cadastro,
    etc.). Se nao existir, cria com valor padrao para aquele tenant.

    Para chamadas sem request context (ex.: jobs), passe empresa_id explicito.
    """
    if empresa_id is None:
        empresa_id = empresa_id_atual()

    if empresa_id is None:
        # Fallback para contexto sem tenant (ex.: bootstrap legado):
        # retorna a primeira config qualquer. Isso nao deve ser chamado a
        # partir de rotas operacionais apos a Fase 2.
        config = Configuracao.query.first()
    else:
        config = Configuracao.query.filter_by(empresa_id=empresa_id).first()

    if config is None:
        config = Configuracao(
            codigo_cadastro="alho123",
            empresa_id=empresa_id,
        )
        db.session.add(config)
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            logging.error("Erro ao criar Configuracao inicial: %s", str(e), exc_info=True)
            raise RuntimeError("Falha ao criar configuração inicial.") from e
    return config




@app.route('/historico')
@login_required
@tenant_required
@admin_required
def historico_atividades():
    """
    Exibe o log de auditoria de ações do sistema.

    Multi-tenant: LogAtividade nao tem empresa_id proprio; o escopo do tenant
    eh derivado do usuario_id (apenas logs de usuarios do mesmo tenant).

    Parâmetros de URL:
        modulo: Filtrar por módulo (VENDAS, CLIENTES, PRODUTOS, USUARIOS).
        acao: Filtrar por ação (CRIAR, EDITAR, EXCLUIR, PAGAR, INATIVAR, ATIVAR).
        usuario_id: Filtrar por usuário específico.
        pagina: Número da página (paginação).

    Returns:
        render_template: historico.html com lista paginada de logs.
    """
    pagina = request.args.get('pagina', 1, type=int)
    modulo_filtro = request.args.get('modulo', '')
    acao_filtro = request.args.get('acao', '')
    usuario_filtro = request.args.get('usuario_id', '', type=str)

    # Escopo tenant: ids dos usuarios da propria empresa
    ids_do_tenant = [
        uid
        for (uid,) in db.session.query(Usuario.id)
        .filter(Usuario.empresa_id == empresa_id_atual())
        .all()
    ]

    query = LogAtividade.query.filter(LogAtividade.usuario_id.in_(ids_do_tenant or [0]))

    if modulo_filtro:
        query = query.filter(LogAtividade.modulo == modulo_filtro)
    if acao_filtro:
        query = query.filter(LogAtividade.acao == acao_filtro)
    if usuario_filtro and usuario_filtro.isdigit():
        # Restringe tambem ao proprio tenant para evitar vazar outros logs.
        uid_req = int(usuario_filtro)
        if uid_req in ids_do_tenant:
            query = query.filter(LogAtividade.usuario_id == uid_req)

    query = query.order_by(LogAtividade.data_hora.desc())
    total = query.count()
    por_pagina = 50
    logs = query.offset((pagina - 1) * por_pagina).limit(por_pagina).all()

    # Carregar nomes de usuários para exibição
    ids_usuarios = {log.usuario_id for log in logs if log.usuario_id}
    mapa_usuarios = {}
    if ids_usuarios:
        for u in Usuario.query.filter(Usuario.id.in_(ids_usuarios)).all():
            mapa_usuarios[u.id] = u.username

    total_paginas = max(1, (total + por_pagina - 1) // por_pagina)
    todos_usuarios = (
        Usuario.query.filter_by(empresa_id=empresa_id_atual())
        .order_by(Usuario.username)
        .all()
    )

    return render_template(
        'historico.html',
        logs=logs,
        mapa_usuarios=mapa_usuarios,
        pagina=pagina,
        total_paginas=total_paginas,
        total=total,
        modulo_filtro=modulo_filtro,
        acao_filtro=acao_filtro,
        usuario_filtro=usuario_filtro,
        todos_usuarios=todos_usuarios,
    )


# ========== ROTAS PRINCIPAIS ==========

@app.errorhandler(Exception)
def handle_exception(e):
    """Interceptador global de exceções para registrar falhas não tratadas."""
    if isinstance(e, HTTPException):
        return e

    try:
        user_info = current_user.username if current_user.is_authenticated else 'Anonimo'
    except Exception:
        user_info = 'Desconhecido'

    try:
        url_info = request.url
        method_info = request.method
    except Exception:
        url_info = '(sem contexto de requisição)'
        method_info = ''

    app.logger.error(
        f"ERRO 500 | Usuário: {user_info} | {method_info} {url_info}\n"
        f"{traceback.format_exc()}",
        exc_info=False,
    )

    if request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html:
        return jsonify({'erro': 'Erro interno no servidor.'}), 500

    try:
        return render_template('500.html'), 500
    except Exception:
        return "Ocorreu um erro interno. A equipe técnica foi notificada nos logs.", 500


@app.errorhandler(500)
def erro_interno(e):
    """Handler explícito de 500 — cobre erros levantados diretamente pelo Flask."""
    try:
        app.logger.error(f"Erro 500 explícito: {e}", exc_info=True)
        return render_template('500.html'), 500
    except Exception:
        return "Erro interno no servidor.", 500










# ============================================================
# Helpers de Vendas/Pedidos compartilhados — permanecem em app.py
# pois são usados por:
#   * routes/vendas.py     (editar/excluir/atualizar_status)
#   * routes/documentos.py (vincular_documento_venda)
#   * _auto_vincular_documentos_pendentes_por_nf (processamento global)
# Movê-los para um blueprint quebraria os imports de processamento.
# ============================================================
def _vendas_do_pedido(venda):
    """Retorna todas as vendas do mesmo pedido (Cliente+NF+Data ou Cliente+Data se CNPJ 0).

    Multi-tenant: filtra pelo empresa_id da propria venda (preserva funcionamento
    em scripts que passam uma venda ja carregada fora do request context).
    """
    cliente_id = venda.cliente_id
    cnpj_cliente = venda.cliente.cnpj or ''
    nf_pedido = venda.nf
    data_pedido = venda.data_venda
    is_consumidor_final = cnpj_cliente in ('0', '00000000000000', '')
    nf_normalizada = str(nf_pedido).strip() if nf_pedido else ''
    _eid_pedido = getattr(venda, 'empresa_id', None)
    if is_consumidor_final:
        query = Venda.query.filter(
            Venda.empresa_id == _eid_pedido,
            Venda.cliente_id == cliente_id,
            Venda.data_venda == data_pedido
        )
    else:
        query = Venda.query.filter(
            Venda.empresa_id == _eid_pedido,
            Venda.cliente_id == cliente_id,
            Venda.data_venda == data_pedido
        )
        if nf_normalizada:
            query = query.filter(Venda.nf == nf_pedido)
        else:
            query = query.filter((Venda.nf.is_(None)) | (Venda.nf == ''))
    return query.all()


def _apagar_lancamentos_caixa_por_vendas(vendas):
    """Remove lançamentos do caixa vinculados por descrição às vendas informadas.

    Multi-tenant: filtra os lancamentos pelo empresa_id das vendas informadas
    (todas pertencem ao mesmo tenant, pois sao do mesmo pedido).
    """
    venda_ids = sorted({int(v.id) for v in (vendas or []) if getattr(v, 'id', None) is not None})
    if not venda_ids:
        return 0
    filtros = [LancamentoCaixa.descricao.like(f"Venda #{vid} -%") for vid in venda_ids]
    _eid = None
    for v in (vendas or []):
        _eid = getattr(v, 'empresa_id', None)
        if _eid is not None:
            break
    base_q = LancamentoCaixa.query.filter(or_(*filtros))
    if _eid is not None:
        base_q = base_q.filter(LancamentoCaixa.empresa_id == _eid)
    lancamentos = base_q.all()
    for lanc in lancamentos:
        db.session.delete(lanc)
    return len(lancamentos)


def _resincronizar_pagamento_venda(venda):
    """Recalcula ``valor_pago`` e ``situacao`` de uma Venda a partir do zero.

    Esta é a fonte da verdade para sincronizar pagamento ↔ caixa. Use
    sempre que um ``LancamentoCaixa`` da venda for criado, editado ou
    deletado — em vez de tentar somar/subtrair manualmente delta-a-delta
    (que acumula erros de arredondamento e dessincroniza em deletes em
    massa).

    Algoritmo:
        1. Soma TODOS os ``LancamentoCaixa.tipo == 'ENTRADA'`` ainda
           presentes no banco com descrição matching ``Venda #<id>`` (e
           mesmo ``empresa_id`` da venda).
        2. Atribui essa soma a ``venda.valor_pago``.
        3. Reclassifica ``venda.situacao``:
              * ``valor_pago == 0``                       → 'PENDENTE'
              * ``0 < valor_pago < total - 0.01``         → 'PARCIAL'
              * ``valor_pago >= total - 0.01``            → 'PAGO'
        4. Preserva ``situacao == 'PERDA'`` (não é tocado por pagamento).

    Multi-tenant: a query é filtrada por ``empresa_id`` da própria venda
    (defensivo, mesmo que a invocação venha de contexto sem tenant).

    NÃO faz commit — o chamador é responsável por ``_safe_db_commit()``
    ou ``db.session.commit()``. Isto permite agrupar várias mudanças
    (delete de N lançamentos, ressync de M vendas) numa única transação.

    Não levanta exceção em caso de venda inválida (None) — apenas
    retorna False. Em caso de erro de cálculo, deixa a venda inalterada
    e retorna False; chamador decide se faz rollback.

    Returns:
        bool: True se ressincronizou (alterou ou confirmou estado), False
        se a venda é inválida.
    """
    if venda is None or getattr(venda, 'id', None) is None:
        return False
    # Vendas marcadas como PERDA não recebem pagamento por definição;
    # o pipeline de criação garante valor_pago=0 e situacao='PERDA'.
    if str(getattr(venda, 'tipo_operacao', '') or '').strip().upper() == 'PERDA':
        return False
    try:
        eid = getattr(venda, 'empresa_id', None)
        q = LancamentoCaixa.query.filter(
            LancamentoCaixa.tipo == 'ENTRADA',
            LancamentoCaixa.descricao.like(f"Venda #{venda.id} -%"),
        )
        if eid is not None:
            q = q.filter(LancamentoCaixa.empresa_id == eid)
        total_pago = q.with_entities(
            func.coalesce(func.sum(LancamentoCaixa.valor), 0)
        ).scalar() or 0
        total_pago = Decimal(str(total_pago))
        # Quantize para 2 casas para evitar diff espúrio no banco
        # (Numeric(10,2) já trunca, mas Decimal puro pode ter mais casas).
        if total_pago < Decimal('0.00'):
            total_pago = Decimal('0.00')
        venda.valor_pago = total_pago

        valor_total_venda = Decimal(str(venda.calcular_total() or Decimal('0.00')))
        # Tolerância de 1 centavo para evitar PARCIAL por arredondamento.
        if total_pago <= Decimal('0.01'):
            venda.valor_pago = Decimal('0.00')
            venda.situacao = 'PENDENTE'
        elif total_pago < (valor_total_venda - Decimal('0.01')):
            venda.situacao = 'PARCIAL'
        else:
            venda.situacao = 'PAGO'
        return True
    except Exception:
        # Não engole o erro silenciosamente: re-levanta para o caller
        # decidir o rollback. Mas não toca em venda.valor_pago além
        # do que já foi atribuído acima (SQLAlchemy session pode reverter).
        raise


@app.route('/sw.js')
def service_worker():
    """Serve o Service Worker com o tipo MIME correto."""
    return send_file('static/sw.js', mimetype='application/javascript')


@app.route('/api/disparar_relatorio', methods=['POST'])
def disparar_relatorio():
    """Envia relatório financeiro mensal por e-mail para admins cadastrados.
    Protegido por token (header X-CRON-TOKEN ou body/query token)."""
    if not CRON_SECRET:
        return jsonify({'erro': 'CRON_SECRET não configurado no ambiente.'}), 503
    token = (
        request.headers.get('X-CRON-TOKEN')
        or (request.get_json(silent=True) or {}).get('token')
        or request.form.get('token')
        or request.args.get('token')
    )
    if token != CRON_SECRET:
        return jsonify({'erro': 'Acesso negado'}), 403

    if not MAIL_USERNAME or not MAIL_PASSWORD:
        return jsonify({'erro': 'Credenciais de e-mail não configuradas (MAIL_USERNAME / MAIL_PASSWORD)'}), 500

    hoje = datetime.today()
    primeiro_dia_mes_atual = hoje.replace(day=1)
    ultimo_dia_mes_passado = primeiro_dia_mes_atual - timedelta(days=1)
    mes_passado = ultimo_dia_mes_passado.month
    ano_passado = ultimo_dia_mes_passado.year

    meses_pt = {1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril', 5: 'Maio', 6: 'Junho',
                7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'}
    mes_ano_str = f"{meses_pt.get(mes_passado, '')} de {ano_passado}"

    vendas_mes = Venda.query.filter(
        extract('month', Venda.data_venda) == mes_passado,
        extract('year', Venda.data_venda) == ano_passado
    ).options(joinedload(Venda.produto)).all()

    faturamento_total = sum(v.calcular_total() for v in vendas_mes)
    lucro_total = sum(v.calcular_lucro() for v in vendas_mes)
    qtd_vendas = len(vendas_mes)
    ticket_medio = faturamento_total / qtd_vendas if qtd_vendas > 0 else 0

    faturamento_fmt = formato_moeda(faturamento_total)
    lucro_fmt = formato_moeda(lucro_total)
    ticket_fmt = formato_moeda(ticket_medio)

    admins = Usuario.query.filter(
        Usuario.role == 'admin',
        Usuario.email.isnot(None),
        Usuario.email != ''
    ).all()
    admins_jhones = Usuario.query.filter(
        Usuario.username == 'Jhones',
        Usuario.email.isnot(None),
        Usuario.email != ''
    ).all()
    todos_admins = {a.id: a for a in admins + admins_jhones}

    if not todos_admins:
        return jsonify({'msg': 'Nenhum administrador com e-mail cadastrado.'}), 200

    try:
        server = smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=_EXTERNAL_TIMEOUT)
        server.starttls()
        server.login(MAIL_USERNAME, MAIL_PASSWORD)

        enviados = 0
        for admin in todos_admins.values():
            msg = MIMEMultipart('alternative')
            msg['Subject'] = f"📈 Relatório Mensal: {mes_ano_str} — Menino do Alho"
            msg['From'] = MAIL_USERNAME
            msg['To'] = admin.email

            corpo_html = render_template('emails/relatorio.html',
                                         nome=admin.nome or admin.username,
                                         mes_ano=mes_ano_str,
                                         faturamento=faturamento_fmt,
                                         lucro=lucro_fmt,
                                         qtd_vendas=qtd_vendas,
                                         ticket_medio=ticket_fmt)

            msg.attach(MIMEText(corpo_html, 'html'))
            server.send_message(msg)
            enviados += 1

        server.quit()
        return jsonify({'msg': f'Relatórios enviados com sucesso para {enviados} admin(s).'}), 200
    except Exception as e:
        app.logger.error(f"Erro ao enviar relatório por e-mail: {e}")
        return jsonify({'erro': str(e)}), 500


@app.route('/api/backup_diario', methods=['GET', 'POST'])
@limiter.limit("5 per hour")
def backup_diario_email():
    """Gera backup CSV das tabelas críticas e envia por e-mail aos admins.

    Protegido por CRON_SECRET — projetado para ser chamado por um Cron Job
    externo (ex: cron-job.org) diariamente.

    Variáveis de ambiente necessárias na Render:
        CRON_SECRET   – token de autenticação (mesmo usado no relatório mensal)
        MAIL_USERNAME – e-mail remetente (ex: seuemail@gmail.com)
        MAIL_PASSWORD – App Password do Gmail (não a senha normal)
        MAIL_SERVER   – servidor SMTP (padrão: smtp.gmail.com)
        MAIL_PORT     – porta SMTP (padrão: 587)
        BACKUP_DEST_EMAIL – (opcional) e-mail de destino fixo; se ausente,
                            envia para todos os admins com e-mail cadastrado.
    """
    if not CRON_SECRET:
        return jsonify({'erro': 'CRON_SECRET não configurado no ambiente.'}), 503

    token = (
        request.headers.get('X-CRON-TOKEN')
        or (request.get_json(silent=True) or {}).get('token')
        or request.form.get('token')
        or request.args.get('token')
    )
    if token != CRON_SECRET:
        return jsonify({'erro': 'Acesso negado'}), 403

    if not MAIL_USERNAME or not MAIL_PASSWORD:
        return jsonify({'erro': 'Credenciais de e-mail não configuradas (MAIL_USERNAME / MAIL_PASSWORD).'}), 500

    try:
        csv_bytes, nome_arquivo = gerar_arquivo_backup_csv()
        enviados = _enviar_backup_por_email(csv_bytes, nome_arquivo)
        current_app.logger.info(f"Backup diário (via HTTP) enviado para {enviados} destinatário(s).")
        return jsonify({
            'status': 'sucesso',
            'mensagem': f'Backup enviado com sucesso para {enviados} destinatário(s).',
            'arquivo': nome_arquivo,
            'destinatarios': enviados,
        }), 200
    except RuntimeError as e:
        return jsonify({'erro': str(e)}), 404
    except Exception as e:
        current_app.logger.error(f"Erro no backup diário por e-mail: {e}")
        return jsonify({'status': 'erro', 'mensagem': str(e)}), 500


@app.route('/api/vapid-public-key', methods=['GET'])
def vapid_public_key():
    """Retorna a VAPID Public Key em formato ApplicationServerKey para o frontend.

    O frontend usa esta chave ao chamar PushManager.subscribe().
    Returns:
        JSON com o campo ``publicKey``.
    """
    if not VAPID_PUBLIC_KEY:
        return jsonify({'erro': 'VAPID_PUBLIC_KEY não configurada no ambiente.'}), 503
    return jsonify({'publicKey': VAPID_PUBLIC_KEY}), 200


@app.route('/api/subscribe', methods=['POST'])
@login_required
@csrf.exempt
@limiter.limit("20 per hour")
def push_subscribe():
    """Recebe e persiste uma inscrição de Web Push do browser.

    O corpo da requisição deve ser o JSON gerado por PushManager.subscribe(),
    com os campos ``endpoint``, ``keys.p256dh`` e ``keys.auth``.

    Returns:
        JSON indicando se a inscrição foi criada ou já existia.
    """
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint')
    keys = data.get('keys', {})
    p256dh = keys.get('p256dh')
    auth_key = keys.get('auth')

    if not endpoint or not p256dh or not auth_key:
        return jsonify({'erro': 'Dados de inscrição incompletos (endpoint, p256dh, auth obrigatórios).'}), 400

    existing = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if existing:
        # Atualiza as chaves caso o browser as tenha renovado
        existing.p256dh = p256dh
        existing.auth = auth_key
        existing.user_id = current_user.id if current_user.is_authenticated else None
        try:
            db.session.commit()
            return jsonify({'status': 'atualizado'}), 200
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erro ao atualizar PushSubscription: {e}")
            return jsonify({'erro': 'Erro ao atualizar inscrição.'}), 500

    sub = PushSubscription(
        user_id=current_user.id if current_user.is_authenticated else None,
        endpoint=endpoint,
        p256dh=p256dh,
        auth=auth_key
    )
    try:
        db.session.add(sub)
        db.session.commit()
        current_app.logger.info(f"Nova PushSubscription user_id={sub.user_id} endpoint={endpoint[:40]}...")
        return jsonify({'status': 'criado'}), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"Erro ao salvar PushSubscription: {e}")
        return jsonify({'erro': 'Erro ao salvar inscrição.'}), 500


@app.route('/api/unsubscribe', methods=['POST'])
@login_required
@csrf.exempt
def push_unsubscribe():
    """Remove uma inscrição de Web Push do banco de dados.

    Chamado pelo frontend quando o usuário cancela as notificações.
    Body JSON: ``{ "endpoint": "..." }``
    """
    data = request.get_json(silent=True) or {}
    endpoint = data.get('endpoint')
    if not endpoint:
        return jsonify({'erro': 'endpoint obrigatório.'}), 400

    sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
    if sub:
        try:
            db.session.delete(sub)
            db.session.commit()
            return jsonify({'status': 'removido'}), 200
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Erro ao remover PushSubscription: {e}")
            return jsonify({'erro': 'Erro ao remover inscrição.'}), 500

    return jsonify({'status': 'não encontrado'}), 404


@app.route('/api/debug/testar_push', methods=['POST'])
@login_required
@csrf.exempt
@limiter.limit("10 per minute")
def debug_testar_push():
    """Dispara uma notificação Web Push de teste para o dispositivo do usuário atual.

    Busca a subscription mais recente do usuário no banco, envia um payload
    de teste via pywebpush e remove automaticamente subscriptions expiradas
    (HTTP 404/410). Útil para validar o fluxo de ponta a ponta.

    Returns:
        JSON com ``status``, ``mensagem`` e detalhes do dispositivo testado.
    """
    vapid_private_key = pad_base64(VAPID_PRIVATE_KEY)
    vapid_public_key = pad_base64(VAPID_PUBLIC_KEY)

    if not vapid_private_key or not vapid_public_key:
        return jsonify({
            'status': 'erro',
            'mensagem': 'VAPID_PRIVATE_KEY ou VAPID_PUBLIC_KEY não configuradas no ambiente (Render). '
                        'Adicione as variáveis de ambiente e faça o deploy novamente.'
        }), 503

    # Busca a subscription mais recente do usuário logado
    sub = (
        PushSubscription.query
        .filter_by(user_id=current_user.id)
        .order_by(PushSubscription.criado_em.desc())
        .first()
    )

    if not sub:
        return jsonify({
            'status': 'erro',
            'mensagem': 'Nenhuma inscrição Push encontrada para este usuário. '
                        'Ative as notificações na seção "Notificações no Dispositivo" acima e tente novamente.'
        }), 404

    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        return jsonify({
            'status': 'erro',
            'mensagem': 'Biblioteca pywebpush não instalada no servidor. '
                        'Verifique o requirements.txt e aguarde o próximo deploy.'
        }), 503

    payload = json.dumps({
        'title': 'Teste de Conexão 🟢',
        'body': 'Se você está lendo isso, o Menino do Alho está conectado em background no seu aparelho!',
        'icon': '/static/images/logo_menino_do_alho_amarelo1.jpeg',
        'badge': '/static/images/logo_menino_do_alho_amarelo1.jpeg',
        'tag': 'push-teste',
        'url': '/dashboard'
    })

    try:
        webpush(
            subscription_info={
                'endpoint': sub.endpoint,
                'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}
            },
            data=payload,
            vapid_private_key=vapid_private_key,
            vapid_claims={'sub': VAPID_CLAIM_EMAIL},
            timeout=_EXTERNAL_TIMEOUT
        )
        current_app.logger.info(
            f"[DEBUG] Push de teste enviado para user_id={current_user.id} sub_id={sub.id}"
        )
        return jsonify({
            'status': 'ok',
            'mensagem': '✅ Notificação de teste enviada! Verifique seu dispositivo. '
                        'Se não aparecer, certifique-se que o app está fechado e que as permissões estão ativas.',
            'endpoint': sub.endpoint[:50] + '...',
            'sub_id': sub.id
        }), 200

    except WebPushException as ex:
        status_code = ex.response.status_code if ex.response else None
        current_app.logger.warning(
            f"[DEBUG] WebPushException para sub_id={sub.id}: HTTP {status_code} — {ex}"
        )
        # 404/410 = subscription revogada pelo browser; remove do banco
        if status_code in (404, 410):
            try:
                db.session.delete(sub)
                db.session.commit()
            except Exception:
                db.session.rollback()
            return jsonify({
                'status': 'erro',
                'mensagem': f'🔴 Subscription expirada ou revogada pelo navegador (HTTP {status_code}). '
                            'A inscrição foi removida. Desative e reative as notificações para se inscrever novamente.',
                'codigo': status_code
            }), 410
        return jsonify({
            'status': 'erro',
            'mensagem': f'Erro no envio Push (HTTP {status_code}): {str(ex)}',
            'codigo': status_code
        }), 500

    except Exception as ex:
        current_app.logger.error(f"[DEBUG] Erro inesperado no teste push: {ex}")
        return jsonify({
            'status': 'erro',
            'mensagem': f'Erro inesperado: {str(ex)}'
        }), 500


@app.route('/api/cron/enviar_frase_diaria', methods=['GET', 'POST'])
@limiter.limit("5 per hour")
def enviar_frase_diaria():
    """Envia a frase filosófica do dia por e-mail para usuários que optaram por recebê-la.

    Protegido por CRON_SECRET. Projetado para ser disparado diariamente
    às 6h por um cron job externo (cron-job.org).
    """
    if not CRON_SECRET:
        return jsonify({'erro': 'CRON_SECRET não configurado no ambiente.'}), 503

    token = (
        request.headers.get('X-CRON-TOKEN')
        or (request.get_json(silent=True) or {}).get('token')
        or request.form.get('token')
        or request.args.get('token')
    )
    if token != CRON_SECRET:
        return jsonify({'erro': 'Acesso negado'}), 403

    if not MAIL_USERNAME or not MAIL_PASSWORD:
        return jsonify({'erro': 'Credenciais de e-mail não configuradas.'}), 500

    frase = frase_do_dia()

    destinatarios = Usuario.query.filter(
        Usuario.notifica_frase == True,
        Usuario.email.isnot(None),
        Usuario.email != ''
    ).all()

    data_hoje = datetime.now().strftime('%d/%m/%Y')

    # ── 1. Envio por e-mail ─────────────────────────────────────────────────
    emails_enviados = 0
    if destinatarios and MAIL_USERNAME and MAIL_PASSWORD:
        try:
            server = smtplib.SMTP(MAIL_SERVER, MAIL_PORT, timeout=_EXTERNAL_TIMEOUT)
            server.starttls()
            server.login(MAIL_USERNAME, MAIL_PASSWORD)

            for user in destinatarios:
                msg = MIMEMultipart('alternative')
                msg['Subject'] = f"🏛️ Sabedoria do Dia — {frase['autor']} [{data_hoje}]"
                msg['From'] = MAIL_USERNAME
                msg['To'] = user.email

                nome = user.nome or user.username or 'colega'
                corpo_html = f"""
                <div style="font-family: Georgia, 'Times New Roman', serif; max-width: 520px; margin: 0 auto; padding: 32px 24px;">
                    <p style="color: #6b7280; font-size: 12px; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 24px;">Sabedoria do Dia</p>
                    <blockquote style="margin: 0; padding: 0 0 0 20px; border-left: 3px solid #059669; color: #1f2937; font-size: 18px; line-height: 1.7; font-style: italic;">
                        &ldquo;{frase['texto']}&rdquo;
                    </blockquote>
                    <p style="text-align: right; color: #6b7280; font-size: 14px; margin-top: 16px; font-weight: 600;">&mdash; {frase['autor']}</p>
                    <hr style="border: none; border-top: 1px solid #e5e7eb; margin: 32px 0 16px;">
                    <p style="color: #9ca3af; font-size: 11px;">
                        Olá, {nome}. Esta mensagem é enviada automaticamente pelo Sistema Menino do Alho.
                        Para deixar de receber, desative &ldquo;Frase Motivacional Diária&rdquo; nas Configurações.
                    </p>
                </div>
                """
                msg.attach(MIMEText(corpo_html, 'html', 'utf-8'))
                server.send_message(msg)
                emails_enviados += 1

            server.quit()
            current_app.logger.info(f"Frase diária enviada por e-mail para {emails_enviados} usuário(s).")
        except Exception as e:
            current_app.logger.error(f"Erro ao enviar frase diária por e-mail: {e}")

    # ── 2. Envio por Web Push (pywebpush) ────────────────────────────────────
    push_enviados = 0
    push_erros = 0
    vapid_private_key = pad_base64(VAPID_PRIVATE_KEY)
    vapid_public_key = pad_base64(VAPID_PUBLIC_KEY)
    if vapid_private_key and vapid_public_key:
        try:
            from pywebpush import webpush, WebPushException
        except ImportError:
            current_app.logger.warning("pywebpush não instalado; pulando Web Push.")
            webpush = None
            WebPushException = Exception

        if webpush:
            # Obtém IDs dos usuários que querem a frase
            ids_notifica = {u.id for u in destinatarios}

            # Busca todas as subscriptions de usuários que opted-in
            # Inclui também subscriptions cujo user_id não está na lista (null ou não encontrado)
            subs = PushSubscription.query.filter(
                PushSubscription.user_id.in_(ids_notifica)
            ).all()

            payload = json.dumps({
                'title': 'Sabedoria do Dia 🏛️',
                'body': f'"{frase["texto"]}" — {frase["autor"]}',
                'icon': '/static/images/logo_menino_do_alho_amarelo1.jpeg',
                'badge': '/static/images/logo_menino_do_alho_amarelo1.jpeg',
                'tag': 'frase-diaria',
                'url': '/'
            })

            subs_para_remover = []
            for sub in subs:
                try:
                    webpush(
                        subscription_info={
                            'endpoint': sub.endpoint,
                            'keys': {'p256dh': sub.p256dh, 'auth': sub.auth}
                        },
                        data=payload,
                        vapid_private_key=vapid_private_key,
                        vapid_claims={'sub': VAPID_CLAIM_EMAIL},
                        timeout=_EXTERNAL_TIMEOUT
                    )
                    push_enviados += 1
                except WebPushException as ex:
                    push_erros += 1
                    # 410 Gone = browser revogou a subscription; remove do banco
                    if ex.response and ex.response.status_code in (404, 410):
                        subs_para_remover.append(sub)
                    else:
                        current_app.logger.warning(f"Web Push falhou para sub {sub.id}: {ex}")
                except Exception as ex:
                    push_erros += 1
                    current_app.logger.warning(f"Erro genérico no Web Push sub {sub.id}: {ex}")

            # Remove subscriptions expiradas
            for sub in subs_para_remover:
                try:
                    db.session.delete(sub)
                except Exception:
                    pass
            if subs_para_remover:
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            current_app.logger.info(
                f"Web Push frase diária: {push_enviados} ok, {push_erros} erros, "
                f"{len(subs_para_remover)} subscriptions removidas."
            )

    total_enviados = emails_enviados + push_enviados
    if total_enviados == 0 and not destinatarios:
        return jsonify({'status': 'ok', 'mensagem': 'Nenhum usuário optou por receber a frase diária.', 'enviados': 0}), 200

    return jsonify({
        'status': 'ok',
        'mensagem': f'Frase diária enviada: {emails_enviados} e-mail(s), {push_enviados} push(es).',
        'frase': frase['texto'],
        'autor': frase['autor'],
        'emails_enviados': emails_enviados,
        'push_enviados': push_enviados,
        'push_erros': push_erros
    }), 200


@app.route('/api/backup/excel')
@login_required
@master_required
def backup_excel():
    """Cofre de Dados: exporta Vendas, Clientes, Produtos e Caixa para download CSV.

    MASTER-only (auditoria P0): a função `gerar_arquivo_backup_csv()` consulta
    Venda/Cliente/Produto/LancamentoCaixa SEM filtro de tenant — abrir para
    DONO permitiria vazar dados de outros tenants. A versão tenant-aware
    deve ser construída em separado quando for necessário expor a feature
    para administradores de empresa.
    """
    csv_bytes, nome_arquivo = gerar_arquivo_backup_csv()
    return send_file(
        io.BytesIO(csv_bytes),
        download_name=nome_arquivo,
        as_attachment=True,
        mimetype='text/csv',
    )


# ============================================================
# REGISTRO DE BLUEPRINTS — refatoração estrutural (Fases 1 → 4)
# ============================================================
# IMPORTANTE: o import abaixo é deliberadamente colocado no FINAL
# de app.py. Os blueprints (routes/*.py) fazem ``from app import ...``
# para reusar helpers/decorators (tenant_required, master_required,
# _safe_db_commit, get_config, _logs_file, _pos_login_landing,
# query_tenant, query_documentos_tenant, empresa_id_atual, registrar_log,
# _is_ajax, _cloudinary_thumb_url, _produto_com_lock,
# _deletar_cloudinary_seguro, limpar_cache_dashboard,
# _arquivo_imagem_permitido, _processar_documento, _processar_pdf,
# _processar_documentos_pendentes, _vendas_do_pedido,
# _apagar_lancamentos_caixa_por_vendas, _usuario_pode_gerenciar_*,
# _resposta_sem_permissao, _empresa_id_para_documento, get_hoje_brasil,
# _EXTERNAL_TIMEOUT, COLUNA_ARQUIVO_PARA_BANCO etc.). Singletons
# (db, login_manager, csrf, cache, limiter) já vêm de extensions.py.
#
# IMPORTANTE — Fase 4: ``_limpar_valor_moeda`` migrou para ``routes/caixa.py``.
# Re-exportamos abaixo para que blueprints legados (vendas/produtos) que
# fazem ``from app import _limpar_valor_moeda`` continuem funcionando.
#
# Todos os blueprints de domínio (``produtos_bp``, ``clientes_bp``,
# ``vendas_bp``, ``documentos_bp``, ``dashboard_bp``, ``caixa_bp``) aplicam
# ``tenant_required`` automaticamente via ``before_request``. ``dashboard_bp``
# exempta apenas ``/`` (que só redireciona). ``documentos_bp`` mantém uma
# allowlist de endpoints públicos para o bot externo (token-based).
from routes.caixa import _limpar_valor_moeda  # re-export para vendas/produtos
from routes import (
    auth_bp, master_bp, clientes_bp, produtos_bp,
    vendas_bp, documentos_bp, dashboard_bp, caixa_bp,
)

app.register_blueprint(auth_bp)
app.register_blueprint(master_bp)
app.register_blueprint(clientes_bp)
app.register_blueprint(produtos_bp)
app.register_blueprint(vendas_bp)
app.register_blueprint(documentos_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(caixa_bp)

# CSRF exemption CIRÚRGICA — somente endpoints chamados por bots externos
# (autenticação via token no header Authorization). Todas as demais rotas
# de ``documentos_bp`` continuam protegidas pelo CSRF padrão da aplicação.
#
# Originalmente, no monólito ``app.py``, estas três rotas tinham o
# decorator ``@csrf.exempt`` aplicado diretamente. Em blueprints, o padrão
# oficial Flask-WTF é referenciar a view function via ``view_functions``
# após ``register_blueprint`` (que é o que fazemos abaixo).
for _endpoint in ('documentos.upload_documento',
                  'documentos.api_receber_automatico',
                  'documentos.api_bot_upload'):
    _vf = app.view_functions.get(_endpoint)
    if _vf is not None:
        csrf.exempt(_vf)
del _endpoint, _vf


if __name__ == '__main__':
    import os
    # Pega a porta da variável de ambiente do servidor (Render usa a variável PORT) ou usa 5000 como fallback local
    port = int(os.environ.get("PORT", 5000))
    # O host '0.0.0.0' é OBRIGATÓRIO para a Render conseguir acessar o app externamente
    app.run(host='0.0.0.0', port=port, debug=False)
