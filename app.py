from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, send_file, current_app
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_compress import Compress
from flask_caching import Cache
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from models import db, Cliente, Produto, ProdutoFoto, Venda, Usuario, Configuracao, Documento, LancamentoCaixa
from config import Config
from datetime import date, datetime, timedelta
from decimal import Decimal
from sqlalchemy import event
from sqlalchemy.engine import Engine
import pytz
from utils import otimizar_imagem, otimizar_imagem_em_memoria

def get_hoje_brasil():
    """Retorna a data de hoje no fuso horário do Brasil (Recife/São Paulo)."""
    try:
        fuso = pytz.timezone('America/Recife')
        return datetime.now(fuso).date()
    except Exception:
        return date.today()
from functools import wraps
from sqlalchemy import func, desc, asc, text, or_, extract
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError, OperationalError
import pandas as pd
import os
import re
import tempfile
import json
import csv
import io
import shutil
import hashlib
import socket
import traceback
import threading
import time
from werkzeug.utils import secure_filename
from redis import Redis
from rq import Queue
from werkzeug.security import generate_password_hash, check_password_hash
import pdfplumber
import cloudinary
import cloudinary.uploader

# #region agent log
_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cursor')
DEBUG_LOG_PATH = os.path.join(_log_dir, 'debug.log')
def _debug_sanitize(obj):
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return [_debug_sanitize(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _debug_sanitize(v) for k, v in obj.items()}
    return str(obj)
def _debug_log(location, message, data, hypothesis_id, run_id="run1"):
    try:
        os.makedirs(_log_dir, exist_ok=True)
        safe = _debug_sanitize(data) if data is not None else {}
        payload = {"location": location, "message": message, "data": safe, "hypothesisId": hypothesis_id, "timestamp": int(__import__("time").time() * 1000), "sessionId": "debug-session", "runId": run_id}
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            f.flush()
    except Exception as e:
        print(f"DEBUG LOG ERROR: {e}")
# #endregion

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
    'preco': 'preco_custo',
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


def _extrair_cnpj(texto, nome_arquivo=None):
    """Extrai CNPJ do PAGADOR/DESTINATÁRIO. Padrão \\d{2}\\.\\d{3}\\.\\d{3}/\\d{4}-\\d{2}.
    Ignora PATY, DESTAK e emissores conhecidos. Prioriza CNPJ próximo a 'Pagador', 'Destinatário', 'Razão Social'."""
    padrao = r'\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}'
    todos = re.findall(padrao, texto)
    emissores_encontrados = [c for c in todos if c in CNPJS_EMISSORES]
    candidatos = [c for c in todos if c not in CNPJS_EMISSORES]
    
    # Debug: mostrar todos os CNPJs encontrados
    if nome_arquivo:
        print(f"DEBUG: CNPJs localizados no arquivo {nome_arquivo}: {todos}")
        if emissores_encontrados:
            print(f"DEBUG: CNPJs de emissores ignorados: {emissores_encontrados}")
        if candidatos:
            print(f"DEBUG: CNPJs candidatos (pagador): {candidatos}")
        elif todos:
            print(f"DEBUG: AVISO - Apenas CNPJs de emissores encontrados. CNPJ do pagador não identificado.")
    
    if not candidatos:
        if todos and nome_arquivo:
            print(f"DEBUG: Erro - Apenas CNPJ(s) do emissor localizado(s) em {nome_arquivo}: {emissores_encontrados}")
        return None
    
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
    m = re.search(r'CPF\s*/\s*CNPJ\s*[\s:]*(\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2})', texto, re.IGNORECASE)
    if m:
        cnpj = m.group(1)
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


def _cliente_from_documento(cnpj, razao_social):
    """Encontra Cliente a partir de dados do documento (boleto/NF).
    Prioridade 1: CNPJ (igual, ignorando formatação).
    Prioridade 2: razao_social (igual ou contém, case-insensitive).
    NÃO usa nome_cliente. Retorna Cliente ou None."""
    if cnpj and (cnpj_limpo := _normalizar_cnpj(cnpj)):
        for c in Cliente.query.all():
            if c.cnpj and _normalizar_cnpj(c.cnpj) == cnpj_limpo:
                return c
    razao = (razao_social or '').strip()
    if not razao:
        return None
    razao_upper = razao.upper()
    for c in Cliente.query.all():
        rs = (c.razao_social or '').strip()
        if not rs:
            continue
        rs_upper = rs.upper()
        if rs_upper == razao_upper:
            return c
        if razao_upper in rs_upper or rs_upper in razao_upper:
            return c
    return None


def _extrair_numero_nf(texto):
    """Extrai número da NF apenas se colado a 'Núm. do documento', 'NF' ou 'Numero Documento'.
    Ignora últimos 25%% da página (feito em _processar_pdf). 4–6 dígitos; blacklist de telefones."""
    padroes = [
        (r'N[úu]m\.?\s*do\s*documento\s*[:\s]*(?:NF[-]?)?\s*(\d+)', False),
        (r'N[úu]mero\s*do\s*documento\s*[:\s]*(?:NF[-]?)?\s*(\d+)', False),
        (r'N[úu]mero\s+Documento\s*[:\s]*(?:NF[-]?)?\s*(\d+)', False),
        (r'NF-(\d+)', True),
        (r'NF\s+(\d+)', True),
        (r'NF:\s*(\d+)', True),
        (r'NF(\d+)', True),
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
        if len(n) < 4 or len(n) > 6:
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
    # Itaú/DESTAK: "Pagador: CAPIM FRIOS EIRELI" (mesma linha ou próxima)
    m = re.search(r'Pagador\s*:\s*([A-ZÁÉÍÓÚÇa-z0-9][A-ZÁÉÍÓÚÇa-z0-9\s&\.\-(),]+?)(?:\s*[\r\n]|CNPJ|CPF|$)', texto, re.IGNORECASE)
    if m:
        razao = _limpar_razao_ate_cnpj_ou_data(m.group(1))
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
        print("\n" + "="*80)
        print("DEBUG EXTRAÇÃO DE VENCIMENTO - BOLETO PATY/BRADESCO")
        print("="*80)
        print("TEXTO COMPLETO DO PDF (primeiros 2000 caracteres):")
        print(texto[:2000])
        print("\n" + "-"*80)
    
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
                print(f"\n✓ Match encontrado com padrão {idx}: {p}")
                print(f"  Data capturada: {data_str}")
                print(f"  Contexto: ...{texto[max(0, m.start()-50):m.end()+50]}...")
            
            data_parsed, _ = _parse_data_flex(data_str)
            if data_parsed:
                if debug_paty:
                    print(f"  ✓ Data parseada com sucesso: {data_parsed.strftime('%d/%m/%Y')}")
                    print("="*80 + "\n")
                return data_parsed
            elif debug_paty:
                print(f"  ✗ Falha ao parsear data: {data_str}")
    
    if debug_paty:
        print("\n✗ NENHUMA DATA DE VENCIMENTO ENCONTRADA")
        print("="*80 + "\n")
    
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
    """Extrai valor principal do boleto (ex.: R$ 2.400,00). Procura padrões próximos a Valor/Total."""
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
                print(f"[ORGANIZAR] Bonificação movida: {nome}")
                continue
            except Exception as e:
                print(f"[ORGANIZAR] Erro ao mover bonificação {nome}: {e}")
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
        emissores_encontrados = [c for c in todos_cnpjs if c in CNPJS_EMISSORES]
        apenas_emissor = len(todos_cnpjs) > 0 and len([c for c in todos_cnpjs if c not in CNPJS_EMISSORES]) == 0
        
        # Tentar extrair NF do PDF primeiro
        numero_nf = _extrair_numero_nf(texto_completo)
        
        # Se não encontrou no PDF, tentar extrair do nome do arquivo (fallback para arquivos CB/BONIF)
        if not numero_nf:
            numero_nf = _extrair_nf_do_nome_arquivo(nome_arquivo)
            if numero_nf:
                print(f"DEBUG: NF extraída do nome do arquivo: {numero_nf} (arquivo: {nome_arquivo})")
        
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
        print(f"Erro ao processar PDF {caminho_arquivo}: {str(e)}")
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
    
    # Arquivo de log detalhado para análise (usando diretório raiz do projeto com permissão de escrita)
    base_path = os.path.dirname(os.path.abspath(__file__))
    log_detalhado_path = os.path.join(base_path, 'vinculo_detalhado.log')
    try:
        # Criar arquivo vazio se não existir
        if not os.path.exists(log_detalhado_path):
            with open(log_detalhado_path, 'w', encoding='utf-8') as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - === INÍCIO DO LOG DE VÍNCULO DETALHADO ===\n")
    except Exception as e:
        print(f"ERRO ao criar arquivo de log: {e}")
        import traceback
        traceback.print_exc()
    
    def _log_detalhado(msg):
        """Escreve log detalhado tanto no console quanto no arquivo, e opcionalmente em memória"""
        print(msg)
        if capturar_logs_memoria:
            logs_memoria.append(msg)
        try:
            with open(log_detalhado_path, 'a', encoding='utf-8') as f:
                f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {msg}\n")
                f.flush()  # Forçar escrita imediata
        except Exception as e:
            print(f"ERRO ao escrever no log: {e}")
    
    # Log inicial para confirmar execução
    try:
        _log_detalhado(f"\n{'='*80}")
        _log_detalhado(f"=== PROCESSAMENTO DE DOCUMENTOS PENDENTES INICIADO ===")
        _log_detalhado(f"Arquivo de log: {log_detalhado_path}")
        _log_detalhado(f"Arquivo existe: {os.path.exists(log_detalhado_path)}")
        _log_detalhado(f"{'='*80}\n")
    except Exception as e:
        print(f"ERRO no log inicial: {e}")
        import traceback
        traceback.print_exc()
    
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'documentos_entrada')
    pastas = {
        'BOLETO': os.path.join(base_dir, 'boletos'),
        'NOTA_FISCAL': os.path.join(base_dir, 'notas_fiscais'),
    }
    
    resultado = {'processados': 0, 'erros': 0, 'vinculos_novos': 0, 'mensagens': []}
    
    # #region agent log
    _debug_log("app.py:570", "Iniciando processamento de documentos pendentes", {"resultado_inicial": resultado}, "ALL")
    # #endregion
    
    for tipo, pasta in pastas.items():
        if not os.path.exists(pasta):
            os.makedirs(pasta, exist_ok=True)
            continue
        
        # Lista todos os PDFs na pasta
        arquivos_pdf = [f for f in os.listdir(pasta) if f.lower().endswith('.pdf')]
        # #region agent log
        _debug_log("app.py:588", "Arquivos PDF encontrados na pasta", {"tipo": tipo, "pasta": pasta, "total_arquivos": len(arquivos_pdf)}, "ALL")
        # #endregion
        print(f"DEBUG: Processando {len(arquivos_pdf)} arquivo(s) do tipo {tipo}")
        
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
                        print(f"[BONIFICACAO] {mensagem}")
                        # Se havia documento no banco, removê-lo
                        doc_existente = Documento.query.filter_by(caminho_arquivo=caminho_relativo).first()
                        if doc_existente:
                            db.session.delete(doc_existente)
                            db.session.commit()
                        resultado['mensagens'].append(f"Bonificação movida: {arquivo}")
                    else:
                        print(f"[BONIFICACAO] ERRO: {mensagem}")
                        resultado['erros'] += 1
                    continue  # Pular processamento deste arquivo
            
            # Inicializar variáveis
            documento = None
            dados_extraidos = None
            
            # Verifica se já foi processado E vinculado (permite re-processar documentos não vinculados)
            doc_existente = Documento.query.filter_by(caminho_arquivo=caminho_relativo).first()
            # #region agent log
            _debug_log("app.py:602", "Verificando se arquivo já foi processado", {"arquivo": arquivo, "doc_existente": doc_existente is not None, "doc_id": doc_existente.id if doc_existente else None, "doc_venda_id": doc_existente.venda_id if doc_existente else None}, "F")
            # #endregion
            # CORREÇÃO: Só pular se documento existe E está vinculado. Permite re-processar documentos não vinculados.
            if doc_existente and doc_existente.venda_id is not None:
                # #region agent log
                _debug_log("app.py:605", "Arquivo já processado E vinculado, pulando", {"arquivo": arquivo, "doc_id": doc_existente.id, "venda_id": doc_existente.venda_id}, "F")
                # #endregion
                print(f"DEBUG: Arquivo {arquivo} já processado e vinculado (Venda ID {doc_existente.venda_id}), pulando")
                continue
            elif doc_existente and doc_existente.venda_id is None:
                # #region agent log
                _debug_log("app.py:612", "Documento existe mas não vinculado, re-processando", {"arquivo": arquivo, "doc_id": doc_existente.id}, "F")
                # #endregion
                print(f"DEBUG: Documento ID {doc_existente.id} existe mas não está vinculado. Re-processando para tentar vincular.")
                documento = doc_existente
                if not documento.url_arquivo and (os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL')):
                    try:
                        resultado_nuvem = cloudinary.uploader.upload(caminho_completo, resource_type='raw')
                        documento.url_arquivo = resultado_nuvem.get('secure_url')
                        documento.public_id = resultado_nuvem.get('public_id')
                        db.session.flush()
                    except Exception as ex:
                        print(f"Erro ao fazer upload para Cloudinary (doc existente): {ex}")
                nf_cached = (getattr(doc_existente, 'nf_extraida', None) or doc_existente.numero_nf)
                nf_cached = (nf_cached or '').strip() or None
                if nf_cached:
                    # Cache OCR: usar nf_extraida/numero_nf armazenado, não rodar OCR de novo
                    # #region agent log
                    _debug_log("app.py:ocr-cache", "OCR skip: usando nf_extraida", {"arquivo": arquivo, "doc_id": doc_existente.id, "nf_extraida": nf_cached, "tipo": tipo}, "H2")
                    # #endregion
                    dados_extraidos = {
                        'numero_nf': nf_cached,
                        'cnpj': doc_existente.cnpj,
                        'razao_social': doc_existente.razao_social,
                        'data_vencimento': doc_existente.data_vencimento,
                    }
                else:
                    dados_extraidos = _processar_pdf(caminho_completo, tipo)
                    # #region agent log
                    _debug_log("app.py:ocr-run", "OCR executado (reprocess doc existente)", {"arquivo": arquivo, "doc_id": doc_existente.id, "tipo": tipo}, "H2")
                    # #endregion
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
                # #region agent log
                _debug_log("app.py:ocr-run-new", "OCR executado (documento novo)", {"arquivo": arquivo, "tipo": tipo}, "H2")
                # #endregion
                if dados_extraidos is None:
                    _debug_log("app.py:640", "Erro ao processar PDF", {"arquivo": arquivo, "tipo": tipo}, "A")
                    resultado['erros'] += 1
                    resultado['mensagens'].append(f"Erro ao processar {arquivo}")
                    continue
                # documento será criado abaixo no bloco try
            
            # Vínculo 100% automático APENAS por NF (normalizada, sem zeros à esquerda). NF é a única chave.
            venda_id = None
            venda_match = None
            nf = dados_extraidos.get('numero_nf')
            # #region agent log
            _debug_log("app.py:660", "NF extraída do documento", {"arquivo": arquivo, "nf": nf, "tipo": tipo}, "D")
            # #endregion
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
                    todas_vendas = Venda.query.all()
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
                    # #region agent log
                    _debug_log("app.py:677", "Vendas encontradas no banco", {"nf_limpa": nf_limpa, "nf_str": nf_str, "variants": variants[:5], "total_candidatas": len(vendas_candidatas), "ids_candidatas": [v.id for v in vendas_candidatas[:10]]}, "D")
                    # #endregion
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
                    _log_detalhado(f"\n--- Comparação Detalhada de NFs (Detecção de Espaços Invisíveis) ---")
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
                                    print(f"DEBUG: Limpando vínculo fantasma: {tipo} '{caminho_existente}' não existe mais no banco")
                                    if tipo == 'BOLETO':
                                        v.caminho_boleto = None
                                    elif tipo == 'NOTA_FISCAL':
                                        v.caminho_nf = None
                                    db.session.flush()
                            
                            # FORÇAR SOBRESCRITA: Se encontrou NF idêntica, sempre permite vincular (substitui o antigo)
                            # Isso resolve o problema de vínculos órfãos e permite atualização automática
                            vendas_validas.append(v)
                            if caminho_existente and documento_existe:
                                print(f"DEBUG: NF idêntica encontrada. Substituindo vínculo antigo: {caminho_existente} → {caminho_relativo}")
                            elif caminho_existente and not documento_existe:
                                print(f"DEBUG: Limpando vínculo órfão e vinculando novo documento: {caminho_relativo}")
                    
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
                        # #region agent log
                        _debug_log("app.py:732", "Verificação de empresa", {"venda_id": venda_id, "empresa_doc": empresa_doc_nome, "empresa_venda": empresa_venda, "nf": nf_str}, "C")
                        # #endregion
                        if empresa_venda:
                            empresa_venda_upper = empresa_venda.upper()
                            empresa_doc_upper = empresa_doc_nome.upper()
                            if empresa_doc_upper != empresa_venda_upper:
                                mensagem_conflito = f"Achei a NF {nf_str}, mas ela pertence à empresa {empresa_venda} e o {tipo.lower()} lido é da {empresa_doc_nome}. VINCULANDO MESMO ASSIM (override por NF)."
                                print(f"DEBUG: ⚠️ CONFLITO DE EMPRESA DETECTADO MAS IGNORADO: {mensagem_conflito}")
                                resultado['mensagens'].append(f"⚠️ {mensagem_conflito}")
                                # Mesmo com conflito, permite vínculo (override) - REGRA: NF é soberana
                                print(f"DEBUG: ⚠️ SOBRESCREVENDO apesar do conflito de empresa (NF é a única chave)")
                        
                        print(f"DEBUG: ✅ VÍNCULO AUTOMÁTICO FORÇADO (SOBRESCRITA): NF '{nf_limpa}' → Venda {venda_id} (Cliente: {cliente_nome})")
                        # FORÇAR vínculo: não há retorno prematuro, vai direto para o commit
                    elif len(vendas_validas) > 1:
                        # Verificar se todas as vendas são do mesmo cliente
                        clientes_unicos = set(v.cliente_id for v in vendas_validas)
                        if len(clientes_unicos) == 1:
                            # Todas as vendas são do mesmo cliente → vincular automaticamente em todas
                            _log_detalhado(f"⚠️ MÚLTIPLAS VENDAS ENCONTRADAS ({len(vendas_validas)}) mas todas do mesmo cliente → VINCULANDO AUTOMATICAMENTE")
                            venda_match = vendas_validas[0]  # Usar a primeira como referência
                            venda_id = vendas_validas[0].id
                            cliente_nome = venda_match.cliente.nome_cliente
                            _log_detalhado(f"✅ VINCULANDO EM TODAS AS {len(vendas_validas)} VENDAS DO CLIENTE '{cliente_nome}'")
                            # Continuar com o fluxo normal de vínculo (será vinculado em todas as vendas do pedido abaixo)
                        else:
                            # Segurança: múltiplas vendas com mesma NF para clientes diferentes → requer seleção manual
                            _log_detalhado(f"⚠️ MÚLTIPLAS VENDAS ENCONTRADAS ({len(vendas_validas)}) para CLIENTES DIFERENTES: Requer seleção manual")
                            clientes_lista = [v.cliente.nome_cliente for v in vendas_validas[:3]]
                            mensagem_diag = f"Achei {len(vendas_validas)} vendas com a NF {nf_str}. Por segurança, escolha manualmente em qual delas devo 'pendurar' este {tipo.lower()}."
                            _log_detalhado(f"DEBUG: ⚠️ AMBIGUIDADE: {mensagem_diag}")
                            resultado['mensagens'].append(f"⚠️ {mensagem_diag} Clientes: {', '.join(clientes_lista)}{'...' if len(vendas_validas) > 3 else ''}")
                            venda_match = None  # Não vincular automaticamente
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
                            resultado_nuvem = cloudinary.uploader.upload(caminho_completo, resource_type='raw')
                            url_arquivo = resultado_nuvem.get('secure_url')
                            public_id = resultado_nuvem.get('public_id')
                            print(f"✅ Sucesso Nuvem: {url_arquivo}")
                        except Exception as ex:
                            print(f"❌ ERRO GRAVE Nuvem: {ex}")
                    else:
                        print("⚠️ Cloudinary não configurado. Salvando sem URL.")

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
                        data_processamento=date.today()
                    )
                    db.session.add(documento)
                    db.session.flush()
                # #region agent log
                _debug_log("app.py:800", "DEPOIS de criar/atualizar documento", {"documento_id": documento.id, "venda_id": venda_id, "venda_match_exists": venda_match is not None}, "E")
                # #endregion
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
                    # #region agent log
                    _debug_log("app.py:link-field", "Vínculo: campo gravado", {"tipo": tipo, "arquivo": arquivo, "venda_id": venda_id, "field_set": field_set}, "H4")
                    # #endregion
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
                        # #region agent log
                        _debug_log("app.py:836", "ANTES do commit", {"venda_id": venda_id, "documento_id": documento.id, "nf": nf_doc, "processados_antes": resultado['processados']}, "B")
                        # #endregion
                        _log_detalhado(f"DEBUG: Tentando gravar vínculo Venda ID {venda_id} com Documento ID {documento.id}")
                        _log_detalhado(f"DEBUG: Estado antes do commit:")
                        _log_detalhado(f"  - Documento.venda_id = {documento.venda_id}")
                        _log_detalhado(f"  - Venda.caminho_boleto = {venda_match.caminho_boleto if tipo == 'BOLETO' else 'N/A'}")
                        _log_detalhado(f"  - Venda.caminho_nf = {venda_match.caminho_nf if tipo == 'NOTA_FISCAL' else 'N/A'}")
                        db.session.commit()
                        # #region agent log
                        _debug_log("app.py:843", "DEPOIS do commit (sucesso)", {"venda_id": venda_id, "documento_id": documento.id, "nf": nf_doc, "processados_depois": resultado['processados']+1}, "B")
                        # #endregion
                        _log_detalhado(f"DEBUG: ✅ COMMIT EXECUTADO COM SUCESSO: NF {nf_doc} vinculada à Venda {venda_id} (Cliente: {cliente_nome})")
                        if documento.url_arquivo and os.path.exists(caminho_completo):
                            try:
                                os.remove(caminho_completo)
                            except Exception as rm_err:
                                print(f"Aviso: não foi possível remover arquivo temporário {caminho_completo}: {rm_err}")
                        resultado['processados'] += 1
                        resultado['vinculos_novos'] += 1
                        rotulo = "Nota Fiscal" if tipo == 'NOTA_FISCAL' else "Boleto"
                        resultado['mensagens'].append(f"✅ Sucesso: {rotulo} {nf_doc} vinculada(o) automaticamente ao cliente {cliente_nome}.")
                    except Exception as commit_error:
                        import traceback
                        # #region agent log
                        _debug_log("app.py:853", "ERRO detectado na gravação", {"venda_id": venda_id, "documento_id": documento.id, "nf": nf_doc, "erro": str(commit_error), "traceback": traceback.format_exc()[:500]}, "B")
                        # #endregion
                        db.session.rollback()
                        # LOG DETALHADO: Erro exato do banco de dados
                        erro_completo = traceback.format_exc()
                        _log_detalhado(f"\n{'='*80}")
                        _log_detalhado(f"❌ ERRO DE COMMIT DETECTADO (Vínculo Único)")
                        _log_detalhado(f"{'='*80}")
                        _log_detalhado(f"Tipo de Erro: {type(commit_error).__name__}")
                        _log_detalhado(f"Mensagem: {str(commit_error)}")
                        _log_detalhado(f"\nTraceback Completo:")
                        _log_detalhado(erro_completo)
                        _log_detalhado(f"{'='*80}\n")
                        
                        # Verificar se é erro de chave estrangeira
                        if 'foreign key' in str(commit_error).lower() or 'FOREIGN KEY' in str(commit_error):
                            _log_detalhado(f"⚠️ ERRO DE CHAVE ESTRANGEIRA DETECTADO:")
                            _log_detalhado(f"   Isso pode indicar que a Venda ID {venda_id} não existe mais no banco.")
                        elif 'integrity' in str(commit_error).lower() or 'INTEGRITY' in str(commit_error):
                            _log_detalhado(f"⚠️ ERRO DE INTEGRIDADE DETECTADO:")
                            _log_detalhado(f"   Isso pode indicar violação de constraint única ou chave estrangeira.")
                        elif 'operational' in str(commit_error).lower() or 'OPERATIONAL' in str(commit_error):
                            _log_detalhado(f"⚠️ ERRO OPERACIONAL DETECTADO:")
                            _log_detalhado(f"   Isso pode indicar problema de conexão ou estrutura do banco.")
                        
                        mensagem_erro = f"Falha técnica ao vincular NF {nf_doc}: {str(commit_error)}"
                        _log_detalhado(f"DEBUG: ❌ ERRO DE COMMIT: {mensagem_erro}")
                        resultado['erros'] += 1
                        resultado['mensagens'].append(f"❌ {mensagem_erro}")
                else:
                    # Sem vínculo automático (múltiplas vendas ou não encontrada)
                    # #region agent log
                    _debug_log("app.py:864", "Sem vínculo automático", {"arquivo": arquivo, "venda_id": venda_id, "venda_match_exists": venda_match is not None, "nf": dados_extraidos.get('numero_nf')}, "E")
                    # #endregion
                    print(f"DEBUG: Sem vínculo automático: venda_id={venda_id}, venda_match={venda_match is not None}")
                    db.session.commit()
                    if documento.url_arquivo and os.path.exists(caminho_completo):
                        try:
                            os.remove(caminho_completo)
                        except Exception as rm_err:
                            print(f"Aviso: não foi possível remover arquivo temporário {caminho_completo}: {rm_err}")
                    resultado['processados'] += 1
                    resultado['mensagens'].append(f"Processado: {arquivo}")
            except Exception as e:
                db.session.rollback()
                import traceback
                nf_doc = dados_extraidos.get('numero_nf') if dados_extraidos else 'N/A'
                # #region agent log
                _debug_log("app.py:870", "EXCEÇÃO no nível superior do loop", {"arquivo": arquivo, "nf": nf_doc, "erro": str(e), "traceback": traceback.format_exc()[:1000]}, "A")
                # #endregion
                mensagem_erro = f"Falha técnica ao vincular NF {nf_doc}: {str(e)}"
                print(f"DEBUG: ❌ ERRO ao processar {arquivo}: {mensagem_erro}")
                print(f"DEBUG: Traceback: {traceback.format_exc()}")
                resultado['erros'] += 1
                resultado['mensagens'].append(f"❌ {mensagem_erro}")
    
    # #region agent log
    _debug_log("app.py:877", "FINAL do processamento", {"resultado_final": resultado}, "ALL")
    # #endregion
    _log_detalhado(f"DEBUG: Processamento finalizado: {resultado['processados']} processados, {resultado['vinculos_novos']} vinculados, {resultado['erros']} erros")
    
    # Se estiver capturando logs em memória, adicionar ao resultado
    if capturar_logs_memoria:
        resultado['logs'] = logs_memoria
    
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


def _vendas_com_documento():
    """Vendas consideradas VINCULADAS: possuem boleto OU nota fiscal (qualquer um basta).
    Regra: preenchido caminho_nf OU caminho_boleto. NF sozinha já vale como documento principal."""
    return Venda.query.filter(
        or_(Venda.caminho_nf.isnot(None), Venda.caminho_boleto.isnot(None))
    ).all()


def _diagnosticar_vinculo_falhou(doc):
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
    # Buscar todas as vendas e filtrar por _nf_match (exact ou base + sufixo numérico)
    vendas_validas = []
    for v in Venda.query.all():
        if not v.nf:
            continue
        nf_venda_norm = _normalizar_nf(str(v.nf))
        if _nf_match(nf_limpa, nf_venda_norm):
            vendas_validas.append(v)
    
    if len(vendas_validas) == 0:
        return {
            'cenario': 'C',
            'mensagem': f"NF {doc_nf} não localizada em nenhuma venda. Verifique se você já importou a planilha de vendas deste período.",
            'nf_tentada': nf_limpa,
            'nf_lida': doc_nf
        }
    elif len(vendas_validas) == 1:
        v = vendas_validas[0]
        ja_tem_boleto = (v.caminho_boleto or '').strip()
        ja_tem_nf = (v.caminho_nf or '').strip()
        if ja_tem_boleto or ja_tem_nf:
            return {
                'cenario': 'A',
                'mensagem': f"NF {doc_nf} encontrada e já vinculada à venda do cliente {v.cliente.nome_cliente}.",
                'cliente_id': v.cliente.id,
                'cliente_nome': v.cliente.nome_cliente,
                'nf_tentada': nf_limpa,
                'nf_lida': doc_nf,
                'venda_id': v.id,
                'nf_venda': v.nf or ''
            }
        # Se chegou aqui, a venda já tem documento do mesmo tipo vinculado
        # Mas ainda pode vincular manualmente se necessário
        tipo_doc_atual = 'boleto' if (v.caminho_boleto or '').strip() else 'nota fiscal'
        return {
            'cenario': 'A',
            'mensagem': f"NF {doc_nf} encontrada no sistema (Cliente: {v.cliente.nome_cliente}). Esta venda já possui {tipo_doc_atual} vinculado(a).",
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


def _listar_documentos_recem_chegados(user_id=None):
    """Lista documentos da tabela Documento onde venda_id é None (arquivos soltos, sem vínculo).
    Mostra todos os documentos órfãos, independente de status. Filtra por usuario_id se informado."""
    resultado_processamento = {"sucesso": 0, "falha": 0, "erros": [], "vinculos_novos": 0, "processados": 0}
    query = Documento.query.filter(Documento.venda_id.is_(None))
    if user_id is not None:
        query = query.filter(Documento.usuario_id == user_id)
    docs = query.order_by(Documento.data_processamento.desc()).limit(5).all()
    documentos = []
    for doc in docs:
        diag = _diagnosticar_vinculo_falhou(doc)
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


app = Flask(__name__)
app.config.from_object(Config)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size

# Sessão e "Lembrar-me": persistir por 30 dias
app.config['REMEMBER_COOKIE_DURATION'] = timedelta(days=30)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=30)
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SECURE'] = True  # Segurança extra (HTTPS)

# Configurações para manter a conexão com o banco sempre viva (Blindagem contra EOF Error)
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,   # Testa a conexão antes de usar (evita "SSL SYSCALL error: EOF detected")
    'pool_recycle': 300,     # Recria conexões a cada 5 minutos para evitar timeout do servidor
    'pool_timeout': 30,      # Espera 30s por uma conexão antes de dar erro
    'pool_size': 10,         # Mantém até 10 conexões abertas
    'max_overflow': 20       # Em pico, pode abrir mais 20
}

# Configurar Rate Limiting para proteção contra brute force
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    storage_uri="memory://",
    default_limits=["200 per day", "50 per hour"]
)

# Configurar compressão Gzip para HTML, CSS, JS e JSON
Compress(app)
app.config['COMPRESS_MIMETYPES'] = ['text/html', 'text/css', 'text/javascript', 'application/javascript', 'application/json', 'text/xml', 'application/xml']
app.config['COMPRESS_LEVEL'] = 6  # Nível de compressão (1-9, 6 é um bom equilíbrio)
app.config['COMPRESS_MIN_SIZE'] = 500  # Comprimir apenas arquivos maiores que 500 bytes

# Configurar cache (Redis em produção, SimpleCache local)
redis_url = os.environ.get('REDIS_URL')
if redis_url:
    cache = Cache(config={
        'CACHE_TYPE': 'RedisCache',
        'CACHE_REDIS_URL': redis_url,
        'CACHE_DEFAULT_TIMEOUT': 300  # 5 minutos
    })
    print("🟢 Cache configurado usando Redis Unificado")
else:
    cache = Cache(config={'CACHE_TYPE': 'SimpleCache'})
    print("🟡 Cache configurado usando SimpleCache (Memória Local)")
cache.init_app(app)

# Configurar Fila de Tarefas (RQ)
redis_conn = Redis.from_url(redis_url) if redis_url else None
fila_tarefas = Queue(connection=redis_conn) if redis_conn else None


def background_organizar_tudo(usuario_id):
    """Trabalho pesado executado pelo Worker do RQ em segundo plano."""
    from app import app, db, _reprocessar_boletos_atualizar_extracao, organizar_arquivos, _processar_documentos_pendentes
    with app.app_context():
        try:
            print("🤖 [WORKER] Iniciando leitura pesada de PDFs...")
            _reprocessar_boletos_atualizar_extracao()
            organizar_arquivos()
            _processar_documentos_pendentes(user_id_forcado=usuario_id)
            print("🤖 [WORKER] PDFs lidos, vinculados e enviados para a nuvem com sucesso!")
        except Exception as e:
            db.session.rollback()
            print(f"🤖 [WORKER] ERRO FATAL: {str(e)}")


# Criar pasta de uploads se não existir
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Backup SQLite removido: sistema usa PostgreSQL em produção (DATABASE_URL)

db.init_app(app)

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

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Faça login para acessar esta página.'


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
        print(f"Erro ao fechar conexão DB: {e}")


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
    """Limpa o cache do dashboard quando há mudanças em vendas ou produtos."""
    try:
        # Limpa o cache específico da rota dashboard
        cache.delete('view//dashboard')
    except Exception:
        # Se houver erro, limpa todo o cache como fallback
        try:
            cache.clear()
        except Exception:
            pass  # Ignora erros de cache


def salvar_arquivo_com_otimizacao(arquivo_upload, pasta_destino=None):
    """
    Salva um arquivo de upload, otimizando automaticamente se for uma imagem.
    
    Args:
        arquivo_upload: Objeto FileStorage do Flask (request.files['arquivo'])
        pasta_destino (str): Pasta de destino (padrão: app.config['UPLOAD_FOLDER'])
    
    Returns:
        tuple: (caminho_completo, nome_arquivo) ou (None, None) em caso de erro
    """
    if not arquivo_upload or not arquivo_upload.filename:
        return None, None
    
    # Determinar pasta de destino
    if pasta_destino is None:
        pasta_destino = app.config['UPLOAD_FOLDER']
    
    # Verificar se é uma imagem
    extensoes_imagem = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp')
    nome_arquivo_original = arquivo_upload.filename
    nome_arquivo_lower = nome_arquivo_original.lower()
    is_imagem = any(nome_arquivo_lower.endswith(ext) for ext in extensoes_imagem)
    
    if is_imagem:
        # Otimizar imagem em memória antes de salvar
        arquivo_otimizado = otimizar_imagem_em_memoria(arquivo_upload)
        
        if arquivo_otimizado:
            # Gerar nome de arquivo seguro
            nome_base, _ = os.path.splitext(secure_filename(nome_arquivo_original))
            filename = nome_base + '.jpg'  # Sempre salvar como JPEG após otimização
            filepath = os.path.join(pasta_destino, filename)
            
            # Salvar arquivo otimizado
            with open(filepath, 'wb') as f:
                f.write(arquivo_otimizado.read())
            
            return filepath, filename
        else:
            # Se falhou a otimização, salvar normalmente
            filename = secure_filename(nome_arquivo_original)
            filepath = os.path.join(pasta_destino, filename)
            arquivo_upload.seek(0)
            arquivo_upload.save(filepath)
            # Tentar otimizar após salvar
            otimizar_imagem(filepath)
            return filepath, filename
    else:
        # Não é imagem, salvar normalmente
        filename = secure_filename(nome_arquivo_original)
        filepath = os.path.join(pasta_destino, filename)
        arquivo_upload.save(filepath)
        return filepath, filename


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


@app.context_processor
def inject_count_outros():
    """Disponibiliza count_outros (produtos com tipo OUTROS) em todos os templates."""
    try:
        n = Produto.query.filter(Produto.tipo == 'OUTROS').count()
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
        # Buscar anos distintos com vendas
        anos_vendas = db.session.query(
            func.distinct(extract('year', Venda.data_venda))
        ).filter(Venda.data_venda.isnot(None)).all()
        for (ano,) in anos_vendas:
            if ano:
                anos_disponiveis.add(int(ano))
        
        # Buscar anos distintos com produtos
        anos_produtos = db.session.query(
            func.distinct(extract('year', Produto.data_chegada))
        ).filter(Produto.data_chegada.isnot(None)).all()
        for (ano,) in anos_produtos:
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


@app.route('/alterar_ano/<int:ano>')
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
    if referrer:
        return redirect(referrer)
    return redirect(url_for('dashboard'))


def admin_required(f):
    """Redireciona para o dashboard com aviso se o usuário não for admin."""
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for('login'))
        if not current_user.is_admin():
            flash('Acesso restrito ao Administrador.', 'warning')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return wrapped


# Bootstrap do banco: NÃO executa na importação se SKIP_DB_BOOTSTRAP=1 (usado pelo migrate_recreate_db.py)
if not os.environ.get('SKIP_DB_BOOTSTRAP'):
    with app.app_context():
        db.create_all()
        try:
            db.session.execute(text('ALTER TABLE produtos ADD COLUMN preco_venda_alvo NUMERIC(10,2)'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: adicionar quantidade_entrada e popular com estoque_atual existente
        try:
            db.session.execute(text('ALTER TABLE produtos ADD COLUMN quantidade_entrada INTEGER DEFAULT 0'))
            db.session.commit()
            db.session.execute(text('UPDATE produtos SET quantidade_entrada = estoque_atual WHERE quantidade_entrada = 0 OR quantidade_entrada IS NULL'))
            db.session.commit()
        except (OperationalError, Exception):
            try:
                db.session.execute(text('UPDATE produtos SET quantidade_entrada = estoque_atual WHERE quantidade_entrada = 0 OR quantidade_entrada IS NULL'))
                db.session.commit()
            except Exception:
                db.session.rollback()
        # Migração: criar tabela documentos se não existir
        try:
            db.session.execute(text('''
                CREATE TABLE IF NOT EXISTS documentos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    caminho_arquivo VARCHAR(500) NOT NULL UNIQUE,
                    tipo VARCHAR(20) NOT NULL,
                    cnpj VARCHAR(18),
                    numero_nf VARCHAR(50),
                    razao_social VARCHAR(200),
                    data_vencimento DATE,
                    venda_id INTEGER,
                    data_processamento DATE NOT NULL,
                    FOREIGN KEY (venda_id) REFERENCES vendas(id)
                )
            '''))
            db.session.commit()
        except (OperationalError, Exception) as e:
            db.session.rollback()
        # Migração: caminho_pdf em vendas (PDF vinculado) — apenas para DBs antigos
        try:
            db.session.execute(text('ALTER TABLE vendas ADD COLUMN caminho_pdf VARCHAR(500)'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: caminho_boleto e caminho_nf em vendas
        for col in ('caminho_boleto', 'caminho_nf'):
            try:
                db.session.execute(text(f'ALTER TABLE vendas ADD COLUMN {col} VARCHAR(500)'))
                db.session.commit()
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
            db.session.execute(text('ALTER TABLE documentos ADD COLUMN nf_extraida VARCHAR(50)'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        try:
            db.session.execute(text("UPDATE documentos SET nf_extraida = numero_nf WHERE nf_extraida IS NULL AND numero_nf IS NOT NULL"))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: usuario_id em documentos (quem processou/recuperou)
        try:
            db.session.execute(text('ALTER TABLE documentos ADD COLUMN usuario_id INTEGER'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: conteudo_binario em documentos (PDF armazenado no banco)
        try:
            uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
            col_type = 'BYTEA' if 'postgres' in uri.lower() else 'BLOB'
            db.session.execute(text(f'ALTER TABLE documentos ADD COLUMN conteudo_binario {col_type}'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: url_arquivo e public_id em documentos (Cloudinary)
        for col, col_def in [('url_arquivo', 'VARCHAR(500)'), ('public_id', 'VARCHAR(200)')]:
            try:
                db.session.execute(text(f'ALTER TABLE documentos ADD COLUMN {col} {col_def}'))
                db.session.commit()
            except (OperationalError, Exception):
                db.session.rollback()
        # Migração: profile_image_url em usuarios (foto de perfil)
        try:
            db.session.execute(text('ALTER TABLE usuarios ADD COLUMN profile_image_url VARCHAR(500)'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: nome em usuarios (nome completo/real)
        try:
            db.session.execute(text('ALTER TABLE usuarios ADD COLUMN nome VARCHAR(100)'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # --- MIGRACAO AUTOMATICA: Colunas de notificacao em usuarios (PostgreSQL) ---
        try:
            # ADD COLUMN IF NOT EXISTS garante que só cria na primeira vez (PostgreSQL)
            db.session.execute(text('ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS notifica_boletos BOOLEAN DEFAULT TRUE'))
            db.session.execute(text('ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS notifica_radar BOOLEAN DEFAULT TRUE'))
            db.session.execute(text('ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS notifica_logistica BOOLEAN DEFAULT TRUE'))
            db.session.commit()
            print("Migração: Colunas de notificação verificadas/adicionadas com sucesso.")
        except Exception as e:
            db.session.rollback()
            # Fallback para SQLite (não suporta IF NOT EXISTS em ADD COLUMN)
            try:
                for col in ('notifica_boletos', 'notifica_radar', 'notifica_logistica'):
                    db.session.execute(text(f'ALTER TABLE usuarios ADD COLUMN {col} BOOLEAN DEFAULT 1'))
                    db.session.commit()
            except (OperationalError, Exception):
                db.session.rollback()
            print(f"Migração ignorada (provavelmente banco SQLite local ou erro): {e}")
        # Migração: data_vencimento em vendas (vencimento do boleto extraído do PDF)
        try:
            db.session.execute(text('ALTER TABLE vendas ADD COLUMN data_vencimento DATE'))
            db.session.commit()
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
            db.session.execute(text('ALTER TABLE clientes ADD COLUMN endereco VARCHAR(255)'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: status_entrega em vendas (status logístico independente do financeiro)
        try:
            db.session.execute(text("ALTER TABLE vendas ADD COLUMN status_entrega VARCHAR(50) DEFAULT 'PENDENTE'"))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        try:
            db.session.execute(text("UPDATE vendas SET status_entrega = 'PENDENTE' WHERE status_entrega IS NULL"))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Migração: valor_pago em vendas (abatimento inteligente / pagamento parcial)
        try:
            db.session.execute(text('ALTER TABLE vendas ADD COLUMN valor_pago FLOAT DEFAULT 0.0'))
            db.session.commit()
        except (OperationalError, Exception):
            db.session.rollback()
        # Jhones sempre admin; criar se não existir
        u = Usuario.query.filter_by(username='Jhones').first()
        if not u:
            u = Usuario(username='Jhones', password_hash=generate_password_hash('admin123'), role='admin')
            db.session.add(u)
            db.session.commit()
        try:
            _debug_log("app.py:bootstrap", "App started, debug.log active", {"path": DEBUG_LOG_PATH}, "ALL", run_id="bootstrap")
        except Exception:
            pass


@app.before_request
def _log_vendas_login_hits():
    # #region agent log
    path = getattr(request, "path", None) or ""
    method = getattr(request, "method", None) or ""
    if path not in ("/", "/login", "/dashboard", "/vendas", "/debug-ping"):
        return
    try:
        _debug_log("app.py:before_request", "request hit", {"path": path, "method": method}, "H3")
    except Exception:
        try:
            import time as _t
            os.makedirs(_log_dir, exist_ok=True)
            line = json.dumps({"location": "app.py:before_request", "message": "before_request_error", "data": {"error": "debug_log_failed"}, "hypothesisId": "H3", "timestamp": int(_t.time() * 1000), "sessionId": "debug-session", "runId": "run1"}, ensure_ascii=False) + "\n"
            with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        except Exception as fallback_err:
            print("[DEBUG] Falha ao registrar log de fallback: " + str(fallback_err))
    # #endregion


# ========== AUTENTICAÇÃO ==========

@app.route('/login', methods=['GET', 'POST'])
@limiter.limit("5 per minute")  # Proteção contra brute force: máximo 5 tentativas por minuto
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        if not username or not password:
            flash('Preencha usuário e senha.', 'error')
            return render_template('auth/login.html')
        try:
            user = Usuario.query.filter_by(username=username).first()
        except Exception:
            db.session.rollback()
            try:
                user = Usuario.query.filter_by(username=username).first()
            except Exception:
                flash('Erro no sistema, tente novamente.', 'error')
                return render_template('auth/login.html')
        if not user or not check_password_hash(user.password_hash, password):
            flash('Usuário ou senha inválidos.', 'error')
            return render_template('auth/login.html')
        remember = True if request.form.get('remember') else False
        login_user(user, remember=remember)
        # #region agent log
        try:
            _debug_log("app.py:login", "login success", {"user": user.username}, "H3")
        except Exception:
            pass
        # #endregion
        next_url = request.form.get('next') or request.args.get('next') or url_for('dashboard')
        return redirect(next_url)
    return render_template('auth/login.html')


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    usuario = current_user
    if request.method == 'POST':
        usuario.notifica_boletos = 'notifica_boletos' in request.form
        usuario.notifica_radar = 'notifica_radar' in request.form
        usuario.notifica_logistica = 'notifica_logistica' in request.form
        db.session.commit()
        flash('Configurações de notificação atualizadas com sucesso!', 'success')
        return redirect(url_for('configuracoes'))
    return render_template('configuracoes.html', usuario=usuario)


@app.route('/perfil', methods=['GET', 'POST'])
@login_required
def perfil():
    if request.method == 'POST':
        novo_nome_real = request.form.get('nome', '').strip()
        novo_username = request.form.get('username', '').strip()
        imagem = request.files.get('profile_image')
        # Atualizar Nome Real/Completo
        current_user.nome = novo_nome_real if novo_nome_real else None
        # Atualizar Username (com verificação de duplicidade)
        if novo_username and novo_username != current_user.username:
            if Usuario.query.filter_by(username=novo_username).first():
                flash('Este nome de usuário já está em uso.', 'error')
            else:
                current_user.username = novo_username
                flash('Nome de usuário atualizado!', 'success')
        # Atualizar Foto de Perfil (Upload para Cloudinary)
        if imagem and imagem.filename != '':
            if os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL'):
                try:
                    upload_result = cloudinary.uploader.upload(
                        imagem,
                        folder="perfis_usuarios",
                        public_id=f"user_{current_user.id}_profile",
                        overwrite=True,
                        resource_type="image"
                    )
                    current_user.profile_image_url = upload_result['secure_url']
                    flash('Foto de perfil atualizada com sucesso!', 'success')
                except Exception as e:
                    flash(f'Erro ao fazer upload da imagem: {str(e)}', 'error')
            else:
                flash('Cloudinary não configurado. Não foi possível enviar a foto.', 'error')
        db.session.commit()
        flash('Perfil atualizado com sucesso!', 'success')
        return redirect(url_for('perfil'))
    return render_template('auth/perfil.html', user=current_user)


def get_config():
    """Retorna o registro único de Configuracao. Cria com código padrão se a tabela estiver vazia."""
    config = Configuracao.query.first()
    if config is None:
        config = Configuracao(codigo_cadastro='alho123')
        db.session.add(config)
        db.session.commit()
    return config


@app.route('/cadastro', methods=['GET', 'POST'])
def cadastro():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    erro = None
    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        senha = request.form.get('password') or ''
        confirmar = request.form.get('confirmar') or ''
        codigo_seguranca = (request.form.get('codigo_seguranca') or '').strip()
        config = get_config()
        if not username:
            erro = 'Informe o usuário.'
        elif not senha:
            erro = 'Informe a senha.'
        elif senha != confirmar:
            erro = 'As senhas não coincidem.'
        elif codigo_seguranca != (config.codigo_cadastro or ''):
            erro = 'Código de segurança inválido!'
        elif Usuario.query.filter_by(username=username).first():
            erro = 'Este usuário já está em uso.'
        else:
            u = Usuario(username=username, password_hash=generate_password_hash(senha), role='user')
            db.session.add(u)
            db.session.commit()
            flash('Cadastro realizado! Faça login.', 'success')
            return redirect(url_for('login'))
        if erro:
            flash(erro, 'error')
    return render_template('auth/cadastro.html')


@app.route('/gerenciar_usuarios')
@login_required
@admin_required
def gerenciar_usuarios():
    usuarios = Usuario.query.order_by(Usuario.username).all()
    config = get_config()
    return render_template('auth/gerenciar_usuarios.html', usuarios=usuarios, config=config)


@app.route('/gerenciar_usuarios/atualizar_codigo', methods=['POST'])
@login_required
@admin_required
def atualizar_codigo_cadastro():
    """Atualiza o código de segurança exigido no cadastro de novos usuários."""
    novo_codigo = (request.form.get('codigo_cadastro') or '').strip()
    confirmar = (request.form.get('confirmar_codigo') or '').strip()
    if not novo_codigo:
        flash('Informe o novo código de segurança.', 'error')
        return redirect(url_for('gerenciar_usuarios'))
    if novo_codigo != confirmar:
        flash('Os códigos não conferem!', 'error')
        return redirect(url_for('gerenciar_usuarios'))
    config = get_config()
    config.codigo_cadastro = novo_codigo
    db.session.commit()
    flash('Código de cadastro atualizado com sucesso.', 'success')
    return redirect(url_for('gerenciar_usuarios'))


@app.route('/gerenciar_usuarios/editar_completo/<int:id>', methods=['POST'])
@login_required
@admin_required
def editar_usuario_completo(id):
    u = Usuario.query.get_or_404(id)
    novo_nome = request.form.get('username', '').strip()
    nova_senha = request.form.get('password', '')
    novo_role = request.form.get('role')
    # Lógica para alterar o Nome de Usuário
    if novo_nome and novo_nome != u.username:
        existe = Usuario.query.filter_by(username=novo_nome).first()
        if existe:
            flash(f'Erro: O nome {novo_nome} já está em uso por outro usuário.', 'error')
            return redirect(url_for('gerenciar_usuarios'))
        u.username = novo_nome
    # Atualiza senha apenas se algo for digitado
    if nova_senha:
        u.password_hash = generate_password_hash(nova_senha)
    # Atualiza nível (Proteção para o Jhones não se auto-rebaixar)
    if novo_role in ('admin', 'user'):
        if u.username == 'Jhones' and novo_role == 'user':
            flash('Atenção: O administrador principal não pode ser alterado para usuário comum.', 'warning')
        else:
            u.role = novo_role
    db.session.commit()
    flash(f'Usuário {u.username} atualizado com sucesso!', 'success')
    return redirect(url_for('gerenciar_usuarios'))


@app.route('/gerenciar_usuarios/alterar_role/<int:id>', methods=['POST'])
@login_required
@admin_required
def alterar_role_usuario(id):
    u = Usuario.query.get_or_404(id)
    novo_role = request.form.get('role')
    if novo_role not in ('admin', 'user'):
        flash('Nível inválido.', 'error')
        return redirect(url_for('gerenciar_usuarios'))
    if u.username == 'Jhones':
        flash('O administrador principal (Jhones) não pode ser alterado.', 'warning')
        return redirect(url_for('gerenciar_usuarios'))
    u.role = novo_role
    db.session.commit()
    flash(f'Nível de "{u.username}" alterado para {novo_role}.', 'success')
    return redirect(url_for('gerenciar_usuarios'))


# ========== ROTAS PRINCIPAIS ==========

@app.errorhandler(500)
def erro_interno(e):
    """Página amigável para erros 500. Evita exibir traceback no navegador."""
    return render_template('500.html'), 500


@app.route('/')
def index():
    return redirect(url_for('dashboard'))


def get_radar_recompra():
    """Calcula alertas de recompra por cliente e produto. A média de dias entre compras é calculada
    individualmente por produto para cada cliente (evita falsos positivos ao misturar Alho e Sacola)."""
    from datetime import datetime, timedelta
    clientes = Cliente.query.all()
    alertas = []
    hoje = datetime.now().date()

    for cliente in clientes:
        vendas = Venda.query.filter_by(cliente_id=cliente.id).order_by(Venda.data_venda.asc()).all()

        # Agrupar datas de venda por categoria mestra (ALHO, SACOLA, CAFÉ, etc.)
        # Agrupa marcas e tamanhos diferentes do mesmo tipo na mesma 'gaveta'
        vendas_por_produto = {}
        for venda in vendas:
            nome_produto_bruto = str(venda.produto.nome_produto if venda.produto else 'Produto Desconhecido').upper()
            # Define a categoria mestra baseada em palavras-chave
            if 'ALHO' in nome_produto_bruto:
                categoria = 'ALHO'
            elif 'SACOLA' in nome_produto_bruto:
                categoria = 'SACOLA'
            elif 'CAFÉ' in nome_produto_bruto or 'CAFE' in nome_produto_bruto:
                categoria = 'CAFÉ'
            else:
                # Se não for nenhum dos principais, agrupa pela primeira palavra (ex: 'CEBOLA')
                palavras = nome_produto_bruto.split()
                categoria = palavras[0] if palavras else 'OUTROS'

            if categoria not in vendas_por_produto:
                vendas_por_produto[categoria] = []
            vendas_por_produto[categoria].append(venda.data_venda)

        # Calcular média individualmente para cada categoria
        for categoria, datas in vendas_por_produto.items():
            if len(datas) >= 2:
                intervalos = []
                for i in range(1, len(datas)):
                    dias = (datas[i] - datas[i - 1]).days
                    if dias > 0:  # Ignora compras no mesmo dia
                        intervalos.append(dias)

                if intervalos:
                    media_dias = sum(intervalos) / len(intervalos)
                    ultima_venda = datas[-1]
                    proxima_compra = ultima_venda + timedelta(days=media_dias)
                    dias_para_comprar = (proxima_compra - hoje).days

                    if dias_para_comprar <= 4:
                        if dias_para_comprar < 0:
                            status = 'Atrasado'
                            cor = 'text-red-600 dark:text-red-400 bg-red-100 dark:bg-red-900/30'
                        elif dias_para_comprar == 0:
                            status = 'É Hoje!'
                            cor = 'text-orange-600 dark:text-orange-400 bg-orange-100 dark:bg-orange-900/30'
                        else:
                            status = f'Em {dias_para_comprar} dias'
                            cor = 'text-yellow-600 dark:text-yellow-400 bg-yellow-100 dark:bg-yellow-900/30'

                        alertas.append({
                            'cliente_nome': cliente.nome_cliente,
                            'produto': categoria,
                            'ultima_venda': ultima_venda.strftime('%d/%m/%Y'),
                            'media_dias': round(media_dias),
                            'status': status,
                            'cor': cor,
                            'dias_restantes': dias_para_comprar
                        })

    alertas.sort(key=lambda x: x['dias_restantes'])
    return alertas


@app.route('/dashboard')
@login_required
@cache.cached(timeout=300)  # Cache por 5 minutos (300 segundos)
def dashboard():
    # Obter ano ativo da sessão
    ano_ativo = session.get('ano_ativo', datetime.now().year)
    
    # Filtro base para vendas do ano ativo
    filtro_ano_venda = extract('year', Venda.data_venda) == ano_ativo
    
    # Lista documentos sem vínculo (venda_id=None), filtrados pelo usuário atual
    documentos_recem_chegados, resultado_processamento = _listar_documentos_recem_chegados(user_id=current_user.id)
    vinculos_novos = resultado_processamento.get('vinculos_novos', 0)
    pendentes = len(documentos_recem_chegados)
    processados = resultado_processamento.get('processados', 0)
    erros = resultado_processamento.get('erros', [])  # erros é uma lista, não um número
    
    # Estatísticas de saúde do sistema de documentos
    total_documentos = Documento.query.count()
    documentos_vinculados = Documento.query.filter(Documento.venda_id.isnot(None)).count()
    documentos_sem_vinculo = total_documentos - documentos_vinculados
    
    if vinculos_novos > 0:
        flash(f"✅ Sucesso: {vinculos_novos} documento(s) vinculado(s) automaticamente pela NF.", 'success')
    elif pendentes > 0:
        flash(f"Processamento concluído: {processados} documento(s) processado(s), {pendentes} boleto(s) ainda pendente(s) de correção.", 'warning')
    if len(erros) > 0:
        flash(f"Erro ao processar {len(erros)} documento(s).", 'error')

    # KPI 1: Top 10 Clientes por Lucro (rentabilidade) - FILTRADO POR ANO
    vendas_por_cliente = db.session.query(
        Cliente.nome_cliente,
        func.sum(Venda.preco_venda * Venda.quantidade_venda).label('total_vendido'),
        func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_total')
    ).join(Venda, Cliente.id == Venda.cliente_id)\
     .join(Produto, Venda.produto_id == Produto.id)\
     .filter(filtro_ano_venda)\
     .group_by(Cliente.id, Cliente.nome_cliente)\
     .order_by(desc('lucro_total'))\
     .limit(10).all()
    
    # KPI 2: Top 10 Produtos por Lucro (rentabilidade) - FILTRADO POR ANO
    vendas_por_produto = db.session.query(
        Produto.nome_produto,
        func.sum(Venda.quantidade_venda).label('quantidade'),
        func.sum(Venda.preco_venda * Venda.quantidade_venda).label('total_vendido'),
        func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_total')
    ).join(Venda, Produto.id == Venda.produto_id)\
     .filter(filtro_ano_venda)\
     .group_by(Produto.id, Produto.nome_produto)\
     .order_by(desc('lucro_total'))\
     .limit(10).all()
    
    # KPI 3: Financeiro - Pendente - FILTRADO POR ANO
    total_pendente = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(Venda.situacao == 'PENDENTE', filtro_ano_venda).scalar() or 0
    
    # KPI 4: Financeiro - Pago - FILTRADO POR ANO
    total_pago = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(Venda.situacao == 'PAGO', filtro_ano_venda).scalar() or 0
    
    # KPI 5: Lucro total do período - FILTRADO POR ANO
    total_lucro = db.session.query(
        func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda)
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id)\
     .filter(filtro_ano_venda).scalar() or 0
    
    # KPI 5b: Prejuízo/Perdas - vendas com lucro negativo - FILTRADO POR ANO
    prejuizo_expr = (Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda
    total_prejuizo = db.session.query(
        func.sum(func.abs(prejuizo_expr))
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id)\
     .filter(prejuizo_expr < 0, filtro_ano_venda).scalar() or 0
    qtd_caixas_prejuizo = db.session.query(
        func.sum(Venda.quantidade_venda)
    ).select_from(Venda).join(Produto, Venda.produto_id == Produto.id)\
     .filter(prejuizo_expr < 0, filtro_ano_venda).scalar() or 0

    # Detalhes para o modal de prejuízos (vendas com lucro negativo, ordenadas da mais recente)
    vendas_com_prejuizo = Venda.query.options(
        joinedload(Venda.cliente), joinedload(Venda.produto)
    ).join(Produto, Venda.produto_id == Produto.id)\
     .filter(prejuizo_expr < 0, filtro_ano_venda)\
     .order_by(Venda.data_venda.desc()).all()
    detalhes_prejuizo = []
    for v in vendas_com_prejuizo:
        nome_cliente = v.cliente.nome_cliente if v.cliente else "Desconhecido"
        produto_nome = v.produto.nome_produto if v.produto else "-"
        detalhes_prejuizo.append({
            'data': v.data_venda.strftime('%d/%m/%Y') if v.data_venda else '-',
            'cliente': nome_cliente,
            'produto': produto_nome,
            'qtd': v.quantidade_venda,
            'prejuizo_valor': abs(v.calcular_lucro())
        })

    # KPI 6: Faturamento por Empresa - FILTRADO POR ANO
    total_paty = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(Venda.empresa_faturadora == 'PATY', filtro_ano_venda).scalar() or 0
    
    total_destak = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(Venda.empresa_faturadora == 'DESTAK', filtro_ano_venda).scalar() or 0
    
    # Separação PATY e DESTAK por situação (Pago vs Pendente)
    paty_pago = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(Venda.empresa_faturadora == 'PATY', Venda.situacao == 'PAGO', filtro_ano_venda).scalar() or 0
    paty_pendente = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(Venda.empresa_faturadora == 'PATY', Venda.situacao == 'PENDENTE', filtro_ano_venda).scalar() or 0
    destak_pago = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(Venda.empresa_faturadora == 'DESTAK', Venda.situacao == 'PAGO', filtro_ano_venda).scalar() or 0
    destak_pendente = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(Venda.empresa_faturadora == 'DESTAK', Venda.situacao == 'PENDENTE', filtro_ano_venda).scalar() or 0
    
    # Vendas sem empresa (NENHUM ou string vazia) - FILTRADO POR ANO
    total_nenhum = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(
        ~Venda.empresa_faturadora.in_(['PATY', 'DESTAK']),
        filtro_ano_venda
    ).scalar() or 0
    
    # KPI 7: Total de Vendas - FILTRADO POR ANO
    total_vendas = db.session.query(
        func.sum(Venda.preco_venda * Venda.quantidade_venda)
    ).filter(filtro_ano_venda).scalar() or 0
    
    # KPI 8: Margem de Lucro (%) = (total_lucro / total_vendas) * 100
    # Proteção contra divisão por zero: total_vendas 0 ou negativo → margem = 0
    margem_porcentagem = (float(total_lucro) / float(total_vendas) * 100) if total_vendas and float(total_vendas) > 0 else 0
    
    # KPI 9: Total de Pedidos - FILTRADO POR ANO
    total_pedidos = db.session.query(
        func.count(func.distinct(
            func.concat(Venda.cliente_id, '-', Venda.nf, '-', func.date(Venda.data_venda))
        ))
    ).filter(filtro_ano_venda).scalar() or 0
    
    # KPI 10: Ticket Médio = total_vendas / total_pedidos
    # Proteção contra divisão por zero
    ticket_medio = (float(total_vendas) / float(total_pedidos)) if total_pedidos and total_pedidos > 0 else 0
    
    # KPI 11: Evolução Mensal (Lucro vs. Volume) - FILTRADO POR ANO
    # Verifica se é Postgres ou SQLite para escolher a função certa
    uri = app.config.get('SQLALCHEMY_DATABASE_URI', '')
    if 'postgres' in uri.lower():
        # Versão PostgreSQL - usa to_char
        coluna_mes = func.to_char(Venda.data_venda, 'YYYY-MM')
    else:
        # Versão SQLite - usa strftime
        coluna_mes = func.strftime('%Y-%m', Venda.data_venda)
    
    evolucao_mensal = db.session.query(
        coluna_mes.label('mes_ano'),
        func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_mensal'),
        func.sum(Venda.quantidade_venda).label('quantidade_mensal')
    ).join(Produto, Venda.produto_id == Produto.id)\
     .filter(filtro_ano_venda)\
     .group_by(coluna_mes)\
     .order_by(coluna_mes).all()
    
    # Preparar dados para Chart.js
    labels_meses = []
    data_lucro = []
    data_caixas = []
    
    for mes_ano, lucro, quantidade in evolucao_mensal:
        # Converter '2026-01' para 'Jan/26'
        try:
            ano, mes = mes_ano.split('-')
            meses_pt = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']
            mes_nome = meses_pt[int(mes) - 1]
            labels_meses.append(f"{mes_nome}/{ano[2:]}")
        except (ValueError, IndexError):
            labels_meses.append(mes_ano)
        
        data_lucro.append(float(lucro) if lucro else 0)
        data_caixas.append(int(quantidade) if quantidade else 0)
    
    # Detalhamento mensal para exibir acima do gráfico (lista de {mes, lucro, ano, mes_numero})
    detalhamento_mensal = []
    for mes_ano, lucro, quantidade in evolucao_mensal:
        try:
            ano_str, mes_str = mes_ano.split('-')  # mes_ano é string no formato '2026-01'
            ano_completo = int(ano_str)
            mes_numero = int(mes_str)
            meses_pt = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']
            mes_nome = meses_pt[mes_numero - 1]
            label = f"{mes_nome}/{ano_str[2:]}"
            detalhamento_mensal.append({
                'mes': label,
                'lucro': float(lucro) if lucro else 0,
                'ano': ano_completo,
                'mes_numero': mes_numero
            })
        except (ValueError, IndexError, AttributeError):
            # Fallback se houver erro no parsing
            detalhamento_mensal.append({
                'mes': str(mes_ano),
                'lucro': float(lucro) if lucro else 0,
                'ano': ano_ativo,
                'mes_numero': 1
            })
    
    faturamento_total = float(total_pendente) + float(total_pago)

    # Radar de Recompra: alertas por cliente/produto (média calculada por produto)
    alertas_recompra = get_radar_recompra()
    
    return render_template('dashboard.html',
                         vendas_por_cliente=vendas_por_cliente,
                         vendas_por_produto=vendas_por_produto,
                         faturamento_total=faturamento_total,
                         total_pendente=float(total_pendente),
                         total_pago=float(total_pago),
                         total_lucro=float(total_lucro),
                         total_prejuizo=float(total_prejuizo),
                         qtd_caixas_prejuizo=int(qtd_caixas_prejuizo),
                         detalhes_prejuizo=detalhes_prejuizo,
                         total_paty=float(total_paty),
                         total_destak=float(total_destak),
                         paty_pago=float(paty_pago),
                         paty_pendente=float(paty_pendente),
                         destak_pago=float(destak_pago),
                         destak_pendente=float(destak_pendente),
                         total_nenhum=float(total_nenhum),
                         margem_porcentagem=float(margem_porcentagem),
                         ticket_medio=float(ticket_medio),
                         documentos_recem_chegados=documentos_recem_chegados,
                         # Estatísticas de saúde do sistema
                         total_documentos=total_documentos,
                         documentos_vinculados=documentos_vinculados,
                         documentos_sem_vinculo=documentos_sem_vinculo,
                         processados=processados,
                         vinculos_novos=vinculos_novos,
                         erros=len(erros),  # Passar número de erros para o template
                         # Evolução mensal para gráfico
                         labels_meses=labels_meses,
                         data_lucro=data_lucro,
                         data_caixas=data_caixas,
                         detalhamento_mensal=detalhamento_mensal,
                         alertas_recompra=alertas_recompra)


# ========== MÓDULO CAIXA (LIVRO CAIXA) ==========

@app.route('/caixa')
@login_required
def caixa():
    # Totais via agregados (rápido, sem carregar todas as linhas)
    total_entradas = db.session.query(func.coalesce(func.sum(LancamentoCaixa.valor), 0)).filter(
        LancamentoCaixa.tipo == 'ENTRADA'
    ).scalar() or 0.0
    total_saida_pessoal = db.session.query(func.coalesce(func.sum(LancamentoCaixa.valor), 0)).filter(
        LancamentoCaixa.tipo == 'SAIDA', LancamentoCaixa.categoria.like('%Pessoal%')
    ).scalar() or 0.0
    total_saida_fornecedor = db.session.query(func.coalesce(func.sum(LancamentoCaixa.valor), 0)).filter(
        LancamentoCaixa.tipo == 'SAIDA', LancamentoCaixa.categoria.like('%Fornecedor%')
    ).scalar() or 0.0
    total_saidas = db.session.query(func.coalesce(func.sum(LancamentoCaixa.valor), 0)).filter(
        LancamentoCaixa.tipo == 'SAIDA'
    ).scalar() or 0.0
    saldo_atual = float(total_entradas) - float(total_saidas)

    # Limitar a 500 lançamentos mais recentes para exibição (índices em data/tipo/categoria)
    lancamentos = LancamentoCaixa.query.order_by(
        LancamentoCaixa.data.desc(), LancamentoCaixa.id.desc()
    ).limit(500).all()
    meses_pt = {1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril', 5: 'Maio', 6: 'Junho', 7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'}
    lancamentos_agrupados = {}
    for l in lancamentos:
        chave_mes = l.data.strftime('%Y-%m')
        if chave_mes not in lancamentos_agrupados:
            lancamentos_agrupados[chave_mes] = {
                'titulo': f"{meses_pt[l.data.month]}",
                'id_html': f"mes-{chave_mes}",
                'itens': [],
                'entradas_mes': 0.0,
                'saidas_mes': 0.0,
                'saidas_fornecedor_mes': 0.0,
                'saidas_pessoal_mes': 0.0,
                'saldo_dinheiro': 0.0,
                'saldo_cheque': 0.0,
                'saldo_pix': 0.0,
                'saldo_boleto': 0.0,
                'entradas_dinheiro': 0.0,
                'entradas_cheque': 0.0,
                'entradas_pix': 0.0,
                'entradas_boleto': 0.0
            }
        lancamentos_agrupados[chave_mes]['itens'].append(l)
        # Cálculo por forma de pagamento (Saldo líquido: Entradas - Saídas)
        valor_sinal = l.valor if l.tipo == 'ENTRADA' else -l.valor
        forma = str(l.forma_pagamento or '').lower()
        if 'dinheiro' in forma:
            lancamentos_agrupados[chave_mes]['saldo_dinheiro'] += valor_sinal
        elif 'cheque' in forma:
            lancamentos_agrupados[chave_mes]['saldo_cheque'] += valor_sinal
        elif 'pix' in forma or 'transfer' in forma:
            lancamentos_agrupados[chave_mes]['saldo_pix'] += valor_sinal
        elif 'boleto' in forma:
            lancamentos_agrupados[chave_mes]['saldo_boleto'] += valor_sinal
        # Cálculo de Entradas e Saídas
        if l.tipo == 'ENTRADA':
            lancamentos_agrupados[chave_mes]['entradas_mes'] += l.valor
            if 'dinheiro' in forma:
                lancamentos_agrupados[chave_mes]['entradas_dinheiro'] += l.valor
            elif 'cheque' in forma:
                lancamentos_agrupados[chave_mes]['entradas_cheque'] += l.valor
            elif 'pix' in forma or 'transfer' in forma:
                lancamentos_agrupados[chave_mes]['entradas_pix'] += l.valor
            elif 'boleto' in forma:
                lancamentos_agrupados[chave_mes]['entradas_boleto'] += l.valor
        else:
            lancamentos_agrupados[chave_mes]['saidas_mes'] += l.valor
            if l.categoria and 'Fornecedor' in l.categoria:
                lancamentos_agrupados[chave_mes]['saidas_fornecedor_mes'] += l.valor
            elif l.categoria and 'Pessoal' in l.categoria:
                lancamentos_agrupados[chave_mes]['saidas_pessoal_mes'] += l.valor
    # Calcula o saldo de cada mês isolado
    for chave, grupo in lancamentos_agrupados.items():
        grupo['saldo_mes'] = grupo['entradas_mes'] - grupo['saidas_mes']
    # Lógica do Saldo Transitado (Acumulado) de um mês para o outro
    # Ordena do mês mais antigo para o mais novo (Ex: '2026-01' -> '2026-02')
    chaves_ordenadas = sorted(lancamentos_agrupados.keys())
    saldo_acumulado = 0.0
    for chave in chaves_ordenadas:
        grupo = lancamentos_agrupados[chave]
        grupo['saldo_anterior'] = saldo_acumulado
        saldo_acumulado += grupo['saldo_mes']
        grupo['saldo_final'] = saldo_acumulado
    lancamentos_agrupados = dict(sorted(lancamentos_agrupados.items(), key=lambda x: x[0], reverse=True))
    mes_atual_str = date.today().strftime('%Y-%m')
    hoje = date.today()
    ontem = hoje - timedelta(days=1)

    return render_template('caixa.html',
                         lancamentos_agrupados=lancamentos_agrupados,
                         mes_atual_str=mes_atual_str,
                         total_entradas=total_entradas,
                         total_saida_pessoal=total_saida_pessoal,
                         total_saida_fornecedor=total_saida_fornecedor,
                         saldo_atual=saldo_atual,
                         data_hoje=hoje.strftime('%Y-%m-%d'),
                         hoje=hoje,
                         ontem=ontem)


def _limpar_valor_moeda(v):
    """Converte string BRL (R$ 1.000,00 ou 1.000,00) ou número (300.5) para float.
    Remove R$, espaços. Se tem vírgula: formato BR (remove pontos de milhar, vírgula→ponto).
    Se não tem vírgula: mantém ponto como decimal (ex: 300.5)."""
    if not v:
        return 0.0
    try:
        s = str(v).strip().replace('R$', '').replace(' ', '').strip()
        if not s:
            return 0.0
        # Se tem vírgula, assume formato BR (1.234,56)
        if ',' in s:
            s = s.replace('.', '').replace(',', '.')
        # Caso contrário mantém ponto como decimal (300.5)
        return float(s)
    except (ValueError, AttributeError):
        return 0.0


@app.route('/caixa/adicionar', methods=['POST'])
@login_required
def adicionar_caixa():
    nova_data = datetime.strptime(request.form.get('data'), '%Y-%m-%d').date()
    descricao_base = (request.form.get('descricao') or '').strip()
    tipo = request.form.get('tipo')
    categoria = request.form.get('categoria')

    if request.form.get('is_split') == 'true':
        # Modo dividido: dois lançamentos com formas de pagamento diferentes
        valor1 = _limpar_valor_moeda(request.form.get('valor1'))
        valor2 = _limpar_valor_moeda(request.form.get('valor2'))
        forma1 = request.form.get('forma1') or 'Dinheiro'
        forma2 = request.form.get('forma2') or 'Dinheiro'
        if valor1 <= 0 and valor2 <= 0:
            flash('Informe pelo menos um valor nos pagamentos divididos.', 'error')
            return redirect(url_for('caixa'))
        lancamentos = []
        if valor1 > 0:
            lancamentos.append(LancamentoCaixa(
                data=nova_data,
                descricao=f"{descricao_base} (Parte 1)",
                tipo=tipo,
                categoria=categoria,
                forma_pagamento=forma1,
                valor=valor1,
                usuario_id=current_user.id
            ))
        if valor2 > 0:
            lancamentos.append(LancamentoCaixa(
                data=nova_data,
                descricao=f"{descricao_base} (Parte 2)",
                tipo=tipo,
                categoria=categoria,
                forma_pagamento=forma2,
                valor=valor2,
                usuario_id=current_user.id
            ))
        for lanc in lancamentos:
            db.session.add(lanc)
        db.session.commit()
        flash('Lançamentos divididos adicionados com sucesso!', 'success')
    else:
        # Modo simples: um único lançamento
        novo_valor = _limpar_valor_moeda(request.form.get('valor'))
        novo_lancamento = LancamentoCaixa(
            data=nova_data,
            descricao=descricao_base,
            tipo=tipo,
            categoria=categoria,
            forma_pagamento=request.form.get('forma_pagamento'),
            valor=novo_valor,
            usuario_id=current_user.id
        )
        db.session.add(novo_lancamento)
        db.session.commit()
        flash(f'Lançamento adicionado com sucesso!|UNDO_CAIXA_{novo_lancamento.id}', 'success')
    return redirect(url_for('caixa'))


@app.route('/caixa/editar/<int:id>', methods=['POST'])
@login_required
def editar_lancamento_caixa(id):
    """Atualiza um lançamento existente no caixa."""
    lancamento = LancamentoCaixa.query.get_or_404(id)
    try:
        lancamento.data = datetime.strptime(request.form.get('data'), '%Y-%m-%d').date()
        lancamento.valor = _limpar_valor_moeda(request.form.get('valor'))
        lancamento.descricao = (request.form.get('descricao') or '').strip()
        lancamento.tipo = request.form.get('tipo') or lancamento.tipo
        lancamento.categoria = request.form.get('categoria') or lancamento.categoria
        lancamento.forma_pagamento = request.form.get('forma_pagamento') or lancamento.forma_pagamento
        db.session.commit()
        flash('Lançamento atualizado com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao atualizar lançamento: {str(e)}', 'error')
        print(f"Erro no banco (editar_lancamento_caixa): {e}")
    return redirect(url_for('caixa'))


@app.route('/desfazer_caixa/<int:id>', methods=['POST'])
@login_required
def desfazer_caixa(id):
    """Rota genérica para desfazer lançamento do caixa (retorna JSON para Undo via Toast)."""
    try:
        lancamento = LancamentoCaixa.query.get_or_404(id)
        # --- Estorno reverso (Caixa -> Venda) ---
        match = re.search(r'Venda #(\d+)', lancamento.descricao or '')
        if match and lancamento.tipo == 'ENTRADA':
            venda_id = int(match.group(1))
            venda = Venda.query.get(venda_id)
            if venda:
                venda.valor_pago = (venda.valor_pago or 0.0) - lancamento.valor
                if venda.valor_pago <= 0.01:
                    venda.valor_pago = 0.0
                    venda.situacao = 'PENDENTE'
                else:
                    valor_total_venda = float(venda.calcular_total())
                    if venda.valor_pago < (valor_total_venda - 0.01):
                        venda.situacao = 'PARCIAL'
                    else:
                        venda.situacao = 'PAGO'
        db.session.delete(lancamento)
        db.session.commit()
        return jsonify({"status": "success", "message": "Lançamento desfeito com sucesso."})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/caixa/deletar/<int:id>', methods=['POST'])
@login_required
def deletar_caixa(id):
    try:
        lancamento = LancamentoCaixa.query.get_or_404(id)

        # --- INÍCIO DO ESTORNO REVERSO (CAIXA -> VENDA) ---
        match = re.search(r'Venda #(\d+)', lancamento.descricao or '')

        if match and lancamento.tipo == 'ENTRADA':
            venda_id = int(match.group(1))
            venda = Venda.query.get(venda_id)

            if venda:
                venda.valor_pago = (venda.valor_pago or 0.0) - lancamento.valor

                if venda.valor_pago <= 0.01:
                    venda.valor_pago = 0.0
                    venda.situacao = 'PENDENTE'
                else:
                    valor_total_venda = float(venda.calcular_total())
                    if venda.valor_pago < (valor_total_venda - 0.01):
                        venda.situacao = 'PARCIAL'
                    else:
                        venda.situacao = 'PAGO'
        # --- FIM DO ESTORNO REVERSO ---

        db.session.delete(lancamento)
        db.session.commit()
        flash('Lançamento removido do caixa.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao remover lançamento: {str(e)}', 'error')
    return redirect(url_for('caixa'))


@app.route('/caixa/deletar_massa', methods=['POST'])
@login_required
def deletar_massa_caixa():
    ids = request.form.getlist('lancamento_ids')
    if not ids:
        flash('Nenhum lançamento selecionado para exclusão.', 'error')
        return redirect(url_for('caixa'))
    try:
        ids_int = [int(x) for x in ids]
        LancamentoCaixa.query.filter(LancamentoCaixa.id.in_(ids_int)).delete(synchronize_session=False)
        db.session.commit()
        flash(f'{len(ids_int)} lançamentos apagados com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao excluir lançamentos: {str(e)}', 'error')
    return redirect(url_for('caixa'))


@app.route('/caixa/importar', methods=['POST'])
@login_required
def importar_caixa():
    if 'arquivo' not in request.files:
        flash('Nenhum arquivo enviado.', 'error')
        return redirect(url_for('caixa'))

    arquivo = request.files['arquivo']
    if arquivo.filename == '':
        flash('Nenhum arquivo selecionado.', 'error')
        return redirect(url_for('caixa'))

    fn = arquivo.filename.lower()
    if arquivo and (fn.endswith('.csv') or fn.endswith('.tsv') or fn.endswith('.txt')):
        try:
            raw = arquivo.stream.read()
            try:
                conteudo = raw.decode('utf-8-sig', errors='replace')
            except Exception:
                conteudo = raw.decode('latin-1', errors='replace')

            stream = io.StringIO(conteudo, newline=None)
            primeira_linha = stream.readline()
            if '\t' in primeira_linha:
                delimitador = '\t'
            elif ';' in primeira_linha:
                delimitador = ';'
            else:
                delimitador = ','
            stream.seek(0)

            leitor = csv.reader(stream, delimiter=delimitador)
            linhas_sucesso = 0
            linhas_duplicadas = 0
            erros = []

            for i, linha in enumerate(leitor, start=1):
                if not linha or all(c.strip() == '' for c in linha):
                    continue

                if 'data' in str(linha).lower() or 'valor' in str(linha).lower() or (linha and 'descri' in str(linha[0]).lower()):
                    continue

                if len(linha) < 5:
                    erros.append(f"Linha {i}: Faltam colunas.")
                    continue

                try:
                    descricao = str(linha[0]).strip()
                    valor_raw = str(linha[1]).strip()
                    data_str = str(linha[2]).strip()
                    categoria = str(linha[3]).strip() or 'Outros'
                    forma_pagamento = str(linha[4]).strip() or 'Dinheiro'

                    s = data_str.split()[0]
                    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y'):
                        try:
                            data_lanc = datetime.strptime(s, fmt).date()
                            break
                        except ValueError:
                            continue
                    else:
                        erros.append(f"Linha {i}: Data inválida '{data_str}'.")
                        continue

                    is_saida = '-' in valor_raw or 'saída' in categoria.lower() or 'saida' in categoria.lower()
                    tipo_lancamento = 'SAIDA' if is_saida else 'ENTRADA'

                    v_str = valor_raw.replace('R$', '').replace('-', '').replace(' ', '').strip()
                    if ',' in v_str:
                        v_str = v_str.replace('.', '').replace(',', '.')
                    else:
                        if '.' in v_str and len(v_str.split('.')[-1]) == 3:
                            v_str = v_str.replace('.', '')
                    valor = float(v_str) if v_str else 0.0

                    ja_existe = LancamentoCaixa.query.filter_by(
                        data=data_lanc,
                        descricao=descricao,
                        tipo=tipo_lancamento,
                        categoria=categoria,
                        forma_pagamento=forma_pagamento,
                        valor=abs(valor),
                        usuario_id=current_user.id
                    ).first()

                    if ja_existe:
                        linhas_duplicadas += 1
                        continue

                    novo_lancamento = LancamentoCaixa(
                        data=data_lanc,
                        descricao=descricao,
                        tipo=tipo_lancamento,
                        categoria=categoria,
                        forma_pagamento=forma_pagamento,
                        valor=abs(valor),
                        usuario_id=current_user.id
                    )
                    db.session.add(novo_lancamento)
                    linhas_sucesso += 1

                except Exception as e:
                    erros.append(f"Linha {i}: Erro nos dados -> {str(e)}")
                    continue

            if linhas_sucesso > 0:
                db.session.commit()
                msg = f'{linhas_sucesso} novos lançamentos importados!'
                if linhas_duplicadas > 0:
                    msg += f' ({linhas_duplicadas} ignorados pois já existiam).'
                if erros:
                    msg += f' (Com {len(erros)} erros de formatação).'
                flash(msg, 'success')
            elif linhas_duplicadas > 0:
                flash(f'Nenhum dado novo. Todos os {linhas_duplicadas} lançamentos da planilha já estavam no sistema!', 'info')
            else:
                db.session.rollback()
                msg_erro = erros[0] if erros else "Formato de colunas inválido. Esperado: Descrição, Valor, Data, Categoria, Forma (5 colunas)."
                flash(f'Falha na importação. {msg_erro}', 'error')
                if len(erros) > 1:
                    flash('Detalhes: ' + '; '.join(erros[:3]) + ('...' if len(erros) > 3 else ''), 'warning')

        except Exception as e:
            db.session.rollback()
            flash(f'Erro fatal ao processar o arquivo: {str(e)}', 'error')
    else:
        flash('Por favor, envie um arquivo .csv, .tsv ou .txt válido.', 'error')

    return redirect(url_for('caixa'))


# ========== MÓDULO CLIENTES ==========

@app.route('/clientes')
@login_required
def listar_clientes():
    # Limitar a 500 clientes mais recentes para carregamento inicial rápido (busca no frontend filtra dentro deles)
    clientes = Cliente.query.order_by(Cliente.id.desc()).limit(500).all()
    return render_template('clientes/listar.html', clientes=clientes)


def _is_ajax():
    return request.headers.get('X-Requested-With') == 'XMLHttpRequest'


@app.route('/clientes/novo', methods=['GET', 'POST'])
@login_required
def novo_cliente():
    if request.method == 'POST':
        try:
            cnpj = request.form.get('cnpj', '').strip() or None
            if cnpj:
                cliente_existente = Cliente.query.filter_by(cnpj=cnpj).first()
                if cliente_existente:
                    msg = f'CNPJ {cnpj} já está cadastrado para o cliente {cliente_existente.nome_cliente}'
                    if _is_ajax():
                        return jsonify(ok=False, mensagem=msg), 400
                    flash(msg, 'error')
                    return render_template('clientes/formulario.html', cliente=None)

            nome_cliente = (request.form.get('nome_cliente') or '').strip()
            if not nome_cliente:
                msg = 'Nome do cliente é obrigatório.'
                if _is_ajax():
                    return jsonify(ok=False, mensagem=msg), 400
                flash(msg, 'error')
                return render_template('clientes/formulario.html', cliente=None)
            cliente = Cliente(
                nome_cliente=nome_cliente,
                razao_social=request.form.get('razao_social', ''),
                cnpj=cnpj,
                cidade=request.form.get('cidade', ''),
                endereco=request.form.get('endereco', '') or None
            )
            db.session.add(cliente)
            db.session.commit()
            if _is_ajax():
                return jsonify(ok=True, mensagem='Cliente cadastrado com sucesso!')
            flash('Cliente cadastrado com sucesso!', 'success')
            return redirect(url_for('listar_clientes'))
        except Exception as e:
            db.session.rollback()
            msg = f'Erro ao cadastrar cliente: {str(e)}'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 500
            flash(msg, 'error')
    return render_template('clientes/formulario.html', cliente=None)


@app.route('/clientes/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_cliente(id):
    try:
        cliente = Cliente.query.get_or_404(id)

        if request.method == 'POST':
            cnpj_raw = request.form.get('cnpj', '').strip() or None
            cnpj = None
            if cnpj_raw:
                cnpj_limpo = re.sub(r'\D', '', cnpj_raw)
                cnpj = cnpj_limpo if len(cnpj_limpo) == 14 else None
            if cnpj and cnpj != (cliente.cnpj or ''):
                cliente_existente = Cliente.query.filter_by(cnpj=cnpj).first()
                if cliente_existente and cliente_existente.id != cliente.id:
                    flash(f'CNPJ já está cadastrado para o cliente {cliente_existente.nome_cliente}', 'error')
                    return render_template('clientes/formulario.html', cliente=cliente)

            cliente.nome_cliente = request.form.get('nome_cliente') or cliente.nome_cliente
            cliente.razao_social = request.form.get('razao_social', '')
            cliente.cnpj = cnpj
            cliente.cidade = request.form.get('cidade', '')
            cliente.endereco = request.form.get('endereco', '') or None
            db.session.commit()
            flash('Cliente atualizado com sucesso!', 'success')
            return redirect(url_for('listar_clientes'))

        return render_template('clientes/formulario.html', cliente=cliente)

    except Exception as e:
        db.session.rollback()
        print(f"ERRO CRÍTICO NA EDIÇÃO DE CLIENTE {id}: {str(e)}")
        flash(f'Erro interno ao processar cliente: {str(e)}', 'error')
        return redirect(url_for('listar_clientes'))


@app.route('/clientes/excluir/<int:id>', methods=['POST'])
@login_required
def excluir_cliente(id):
    cliente = Cliente.query.get_or_404(id)
    try:
        db.session.delete(cliente)
        db.session.commit()
        flash('Cliente excluído com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Não é possível excluir este cliente, pois ele possui vínculos no sistema.', 'error')
    return redirect(url_for('listar_clientes'))


@app.route('/clientes/<int:cliente_id>/extrato')
@login_required
def extrato_cliente(cliente_id):
    """Extrato de cobrança em PDF: vendas pendentes e parciais do cliente."""
    cliente = Cliente.query.get_or_404(cliente_id)

    # Filtro: PENDENTE e PARCIAL (saldo devedor); ignora itens de perda/brinde (R$ 0,00)
    vendas_pendentes = Venda.query.filter(
        Venda.cliente_id == cliente.id,
        Venda.situacao.in_(['PENDENTE', 'PARCIAL']),
        (Venda.preco_venda * Venda.quantidade_venda) > 0
    ).options(joinedload(Venda.produto)).order_by(Venda.data_venda).all()

    # Total devido = soma do saldo restante (valor da nota - já pago) de cada venda
    total_devido = sum(float(v.calcular_total()) - (v.valor_pago or 0.0) for v in vendas_pendentes)
    data_hoje = datetime.now().strftime('%d/%m/%Y')

    return render_template('extrato.html', cliente=cliente, vendas=vendas_pendentes, total=total_devido, data_hoje=data_hoje)


@app.route('/bulk_delete_clientes', methods=['POST'])
@login_required
def bulk_delete_clientes():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'ok': False, 'mensagem': 'Nenhum ID informado.'}), 400
    try:
        for id_ in ids:
            cliente = Cliente.query.get(id_)
            if cliente:
                db.session.delete(cliente)
        db.session.commit()
        return jsonify({'ok': True, 'mensagem': f'{len(ids)} cliente(s) excluído(s) com sucesso.', 'excluidos': len(ids)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'mensagem': str(e)}), 500


def _processar_linhas_clientes_upsert(linhas, erros_detalhados, sucesso_ref, erros_ref, linha_offset=0):
    """Processa lista de dicts com nome_cliente, razao_social, cnpj, cidade. Upsert por nome_cliente (Apelido).
    Atualiza sucesso_ref[0] e erros_ref[0] e append em erros_detalhados."""
    for idx, row in enumerate(linhas):
        linha_num = linha_offset + idx + 1
        nome = (row.get('nome_cliente') or '').strip()
        razao_social = (row.get('razao_social') or '').strip() or nome
        cnpj = row.get('cnpj')
        cidade = (row.get('cidade') or '').strip()
        contexto = (nome[:40] + '...') if nome and len(nome) > 40 else (nome or 'sem nome')
        try:
            if not nome:
                erros_detalhados.append(_msg_linha(linha_num, '', "O campo Apelido (nome) está vazio", True))
                erros_ref[0] += 1
                continue
            endereco = (row.get('endereco') or '').strip() or None
            cliente = Cliente.query.filter(func.lower(Cliente.nome_cliente) == nome.lower()).first()
            if cliente:
                cliente.razao_social = razao_social or None
                cliente.cnpj = cnpj
                cliente.cidade = cidade or None
                cliente.endereco = endereco
                db.session.commit()
                sucesso_ref[0] += 1
            else:
                if cnpj and Cliente.query.filter_by(cnpj=cnpj).first():
                    erros_detalhados.append(_msg_linha(linha_num, nome, f"O CNPJ já está cadastrado para outro cliente. Use um CNPJ único.", True))
                    erros_ref[0] += 1
                    continue
                cliente = Cliente(
                    nome_cliente=nome,
                    razao_social=razao_social or None,
                    cnpj=cnpj,
                    cidade=cidade or None,
                    endereco=endereco
                )
                db.session.add(cliente)
                db.session.commit()
                sucesso_ref[0] += 1
        except IntegrityError as e:
            db.session.rollback()
            erros_detalhados.append(_msg_linha(linha_num, contexto, f"CNPJ duplicado ou conflito: {str(e)}", True))
            erros_ref[0] += 1
        except Exception as e:
            db.session.rollback()
            erros_detalhados.append(_msg_linha(linha_num, contexto, str(e), True))
            erros_ref[0] += 1


@app.route('/clientes/importar', methods=['GET', 'POST'])
@login_required
@admin_required
def importar_clientes():
    if request.method == 'POST':
        lista_raw = (request.form.get('lista_raw') or '').strip()
        tem_arquivo = 'arquivo' in request.files and request.files['arquivo'] and request.files['arquivo'].filename
        if not lista_raw and not tem_arquivo:
            return render_template('clientes/importar.html', erros_detalhados=['Cole a lista (TAB) no campo de texto ou selecione um arquivo.'], sucesso=0, erros=1)
        filepath = None
        try:
            _debug_log("app.py:importar_clientes", "import start", {"route": "importar_clientes", "lista_raw_len": len(lista_raw), "tem_arquivo": tem_arquivo}, "H1")
            sucesso = 0
            erros = 0
            erros_detalhados = []
            sucesso_ref = [0]
            erros_ref = [0]

            if lista_raw:
                linhas = _parse_clientes_raw_tsv(lista_raw)
                if not linhas:
                    return render_template('clientes/importar.html', erros_detalhados=['Nenhuma linha válida encontrada. Use uma linha por cliente, campos separados por TAB: Apelido, Razão Social, CNPJ, Cidade.'], sucesso=0, erros=1)
                _processar_linhas_clientes_upsert(linhas, erros_detalhados, sucesso_ref, erros_ref, linha_offset=0)
                sucesso, erros = sucesso_ref[0], erros_ref[0]
            else:
                arquivo = request.files['arquivo']
                filename = secure_filename(arquivo.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                arquivo.save(filepath)
                content = None
                if filename.endswith('.csv'):
                    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read()
                processado_raw = False
                if content and content.splitlines():
                    first_line = content.splitlines()[0]
                    if '\t' in first_line:
                        linhas = _parse_clientes_raw_tsv(content)
                        if linhas:
                            _processar_linhas_clientes_upsert(linhas, erros_detalhados, sucesso_ref, erros_ref, linha_offset=0)
                            sucesso, erros = sucesso_ref[0], erros_ref[0]
                            processado_raw = True
                if not processado_raw:
                    if filename.endswith('.csv'):
                        df = pd.read_csv(filepath, sep=None, engine='python', quoting=3, on_bad_lines='warn')
                    else:
                        df = pd.read_excel(filepath)
                    df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_')
                    first_iter = True
                    for idx, row in df.iterrows():
                        if first_iter:
                            _debug_log("app.py:importar_clientes", "first row idx", {"type_idx": type(idx).__name__, "idx_repr": repr(idx)}, "H1")
                            first_iter = False
                        linha_num = idx + 2
                        nome = _strip_quotes(row.get('nome_cliente', row.get('nome', '')))
                        contexto = (nome[:40] + '...') if nome and len(nome) > 40 else (nome or 'sem nome')
                        try:
                            if not nome:
                                erros_detalhados.append(_msg_linha(linha_num, '', "O campo 'nome_cliente' (ou 'nome') está vazio", True))
                                erros += 1
                                continue
                            cnpj_raw = _strip_quotes(row.get('cnpj', '')) or None
                            cnpj = _sanitizar_cnpj_importacao(cnpj_raw) if cnpj_raw else None
                            if cnpj and Cliente.query.filter_by(cnpj=cnpj).first():
                                existente = Cliente.query.filter_by(cnpj=cnpj).first()
                                erros_detalhados.append(_msg_linha(linha_num, nome, f"O CNPJ já está cadastrado para o cliente '{existente.nome_cliente}'. Use um CNPJ único.", True))
                                erros += 1
                                continue
                            endereco = _strip_quotes(row.get('endereco', '')) or None
                            cliente = Cliente.query.filter(func.lower(Cliente.nome_cliente) == nome.lower()).first()
                            if cliente:
                                cliente.razao_social = _strip_quotes(row.get('razao_social', row.get('razao', ''))) or nome
                                cliente.cnpj = cnpj
                                cliente.cidade = _strip_quotes(row.get('cidade', '')) or None
                                cliente.endereco = endereco
                                db.session.commit()
                                sucesso += 1
                            else:
                                cliente = Cliente(
                                    nome_cliente=nome,
                                    razao_social=_strip_quotes(row.get('razao_social', row.get('razao', ''))) or None,
                                    cnpj=cnpj,
                                    cidade=_strip_quotes(row.get('cidade', '')) or None,
                                    endereco=endereco
                                )
                                db.session.add(cliente)
                                db.session.commit()
                                sucesso += 1
                        except Exception as e:
                            db.session.rollback()
                            erros_detalhados.append(_msg_linha(linha_num, contexto, str(e), True))
                            erros += 1

            if filepath and os.path.exists(filepath):
                os.remove(filepath)
            if erros > 0:
                return render_template('clientes/importar.html', erros_detalhados=erros_detalhados, sucesso=sucesso, erros=erros)
            flash(f'Importação concluída com sucesso! {sucesso} cliente(s) importado(s).', 'success')
            return redirect(url_for('listar_clientes'))
        except Exception as e:
            db.session.rollback()
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            _debug_log("app.py:importar_clientes", "outer except", {"route": "importar_clientes", "exc_type": type(e).__name__, "exc_msg": str(e), "tb": traceback.format_exc()}, "H1")
            return render_template('clientes/importar.html', erros_detalhados=[f'Erro ao processar: {str(e)}'], sucesso=0, erros=1)
    return render_template('clientes/importar.html')


# ========== MÓDULO PRODUTOS ==========

def _normalizar_tipo_ui(s):
    """Normaliza tipo para agrupamento na UI: CAFÉ -> CAFE, etc. Retorna ALHO, SACOLA, CAFE ou OUTROS."""
    if not s:
        return 'OUTROS'
    t = str(s).strip().upper()
    t = t.replace('É', 'E').replace('Ê', 'E').replace('Á', 'A').replace('À', 'A').replace('Ã', 'A').replace('Â', 'A')
    t = t.replace('Í', 'I').replace('Ó', 'O').replace('Ô', 'O').replace('Õ', 'O').replace('Ú', 'U').replace('Ç', 'C')
    if t == 'ALHO':
        return 'ALHO'
    if t == 'SACOLA':
        return 'SACOLA'
    if t == 'CAFE':
        return 'CAFE'
    return 'OUTROS'


def gerar_nome_produto(tipo, nacionalidade, marca, data_chegada, tamanho):
    """Gera o nome do produto automaticamente no formato: {tipo} {nacionalidade} {marca} {data} {tamanho}"""
    # Formatar data como dd/mm/yy
    if isinstance(data_chegada, date):
        data_formatada = data_chegada.strftime('%d/%m/%y')
    elif isinstance(data_chegada, str):
        try:
            data_obj = date.fromisoformat(data_chegada)
            data_formatada = data_obj.strftime('%d/%m/%y')
        except Exception:
            data_formatada = date.today().strftime('%d/%m/%y')
    else:
        data_formatada = date.today().strftime('%d/%m/%y')
    
    # SACOLA: nacionalidade N/A (não capitalizar)
    if (tipo or '').upper() == 'SACOLA' or (nacionalidade or '').upper() == 'N/A':
        nacionalidade_capitalizada = 'N/A'
    else:
        nacionalidade_capitalizada = (nacionalidade or '').capitalize()
    
    marca_capitalizada = (marca or '').capitalize()
    
    return f"{tipo} {nacionalidade_capitalizada} {marca_capitalizada} {data_formatada} {tamanho}"


def _validar_sacola(tipo, nacionalidade, marca, tamanho):
    """Valida e normaliza campos conforme o tipo. Retorna (nacionalidade, marca, tamanho) ou levanta ValueError."""
    t = (tipo or '').strip().upper()
    nac = (nacionalidade or '').strip().upper().replace('N/A', 'NA')
    tam = (tamanho or '').strip().upper().replace('N/A', 'NA')

    # SACOLA: nacionalidade N/A, marca SOPACK, tamanho P/M/G ou S/N
    if t == 'SACOLA':
        nacionalidade = 'N/A'
        marca = 'SOPACK'
        tam_clean = tam.replace(' ', '')
        if tam_clean not in ('P', 'M', 'G', 'S/N'):
            raise ValueError('Para SACOLA, tamanho deve ser P, M, G ou S/N.')
        return nacionalidade, marca, tam_clean

    # ALHO: exige nacionalidade (ARGENTINO/NACIONAL/CHINES) e tamanho numérico
    if t == 'ALHO':
        if nac not in ('ARGENTINO', 'NACIONAL', 'CHINES'):
            raise ValueError('Nacionalidade deve ser ARGENTINO, NACIONAL ou CHINES.')
        tamanhos_ok = ['4', '5', '6', '7', '8', '9', '10']
        if tam not in tamanhos_ok:
            raise ValueError('Tamanho deve ser 4, 5, 6, 7, 8, 9 ou 10.')
        return (nacionalidade or '').strip(), (marca or '').strip(), tam

    # CAFE e outros tipos: aceita N/A para nacionalidade e tamanho
    if nac in ('NA', 'N/A', ''):
        nac = 'N/A'
    if tam in ('NA', 'N/A', ''):
        tam = 'N/A'
    return nac, (marca or '').strip(), tam


@app.route('/produtos')
@login_required
def listar_produtos():
    # Obter ano ativo da sessão
    ano_ativo = session.get('ano_ativo', datetime.now().year)
    
    ordem_data = request.args.get('ordem_data') or session.get('ordem_data_produtos', 'crescente')
    if ordem_data not in ('crescente', 'decrescente'):
        ordem_data = 'crescente'
    
    # Query base com filtro por ano (data_chegada) e eager loading para evitar Query N+1
    # Nota: Não usamos joinedload para vendas aqui porque quantidade_vendida() e lucro_realizado() 
    # fazem cálculos que podem ser otimizados separadamente se necessário
    query_base = Produto.query.filter(extract('year', Produto.data_chegada) == ano_ativo)
    
    # Query ordenada para paginação
    if ordem_data == 'crescente':
        session['ordem_data_produtos'] = 'crescente'
        query_ordenada = query_base.order_by(asc(Produto.data_chegada), asc(Produto.id))
    else:
        query_ordenada = query_base.order_by(desc(Produto.data_chegada), desc(Produto.id))
    
    # TOTAIS GLOBAIS: Calcular usando TODOS os produtos (sem paginação)
    produtos_todos = query_ordenada.all()
    
    # Otimização: Calcular quantidade_vendida para todos os produtos de uma vez usando query agregada
    # Isso evita Query N+1 ao chamar produto.quantidade_vendida() para cada produto
    quantidade_vendida_por_produto = {}
    if produtos_todos:
        produto_ids = [p.id for p in produtos_todos]
        vendas_agregadas = db.session.query(
            Venda.produto_id,
            func.sum(Venda.quantidade_venda).label('total_vendido')
        ).filter(Venda.produto_id.in_(produto_ids))\
         .group_by(Venda.produto_id).all()
        
        for produto_id, total_vendido in vendas_agregadas:
            quantidade_vendida_por_produto[produto_id] = int(total_vendido) if total_vendido else 0
    
    # Calcular quantidade_entrada_real para TODOS os produtos (para totais globais)
    produtos_com_entrada_todos = []
    for produto in produtos_todos:
        quantidade_vendida = quantidade_vendida_por_produto.get(produto.id, 0)
        # Se quantidade_entrada está zerada ou muito menor que o esperado, calcular
        # Caso contrário, usar o valor do banco (produtos novos salvos corretamente)
        if produto.quantidade_entrada == 0 or produto.quantidade_entrada < (produto.estoque_atual + quantidade_vendida):
            # Reconstruir: estoque_atual + quantidade_vendida = quantidade_entrada_original
            quantidade_entrada_exibicao = produto.estoque_atual + quantidade_vendida
        else:
            # Usar valor do banco (produto novo ou já corrigido)
            quantidade_entrada_exibicao = produto.quantidade_entrada
        produtos_com_entrada_todos.append({
            'produto': produto,
            'quantidade_entrada_exibicao': quantidade_entrada_exibicao
        })
    
    # Agrupar TODOS os produtos por tipo para calcular totais globais
    produtos_por_tipo_todos = {}
    reverse_order = (ordem_data == 'decrescente')
    for item in produtos_com_entrada_todos:
        tipo_key = _normalizar_tipo_ui(item['produto'].tipo)
        if tipo_key not in produtos_por_tipo_todos:
            produtos_por_tipo_todos[tipo_key] = []
        produtos_por_tipo_todos[tipo_key].append(item)
    
    # Otimização: Calcular lucro_realizado para todos os produtos de uma vez usando query agregada
    # Isso evita Query N+1 ao chamar produto.lucro_realizado() para cada produto
    lucro_realizado_por_produto = {}
    if produtos_todos:
        produto_ids = [p.id for p in produtos_todos]
        lucros_agregados = db.session.query(
            Venda.produto_id,
            func.sum((Venda.preco_venda - Produto.preco_custo) * Venda.quantidade_venda).label('lucro_total')
        ).join(Produto, Venda.produto_id == Produto.id)\
         .filter(Venda.produto_id.in_(produto_ids))\
         .group_by(Venda.produto_id).all()
        
        for produto_id, lucro_total in lucros_agregados:
            lucro_realizado_por_produto[produto_id] = float(lucro_total) if lucro_total else 0.0
    
    # Calcular totais globais por tipo (usando TODOS os produtos)
    totais_por_tipo = {}
    for tipo, itens in produtos_por_tipo_todos.items():
        investimento_paty = 0.0
        investimento_destak = 0.0
        for it in itens:
            valor = float(it['produto'].preco_custo) * it['quantidade_entrada_exibicao']
            f = (it['produto'].fornecedor or '').upper()
            if f == 'PATY':
                investimento_paty += valor
            elif 'DESTAK' in f or f == 'DESTAK':
                investimento_destak += valor
        totais_por_tipo[tipo] = {
            'total_investido': sum(
                float(it['produto'].preco_custo) * it['quantidade_entrada_exibicao']
                for it in itens
            ),
            'investimento_paty': investimento_paty,
            'investimento_destak': investimento_destak,
            'total_qtd_entrada': sum(it['quantidade_entrada_exibicao'] for it in itens),
            'total_estoque_atual': sum(it['produto'].estoque_atual for it in itens),
            'total_lucro_realizado': sum(lucro_realizado_por_produto.get(it['produto'].id, 0.0) for it in itens),
        }
    
    # PAGINAÇÃO: Aplicar apenas na lista de produtos para exibição
    from math import ceil
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    total_produtos = len(produtos_todos)
    total_pages = ceil(total_produtos / per_page) if total_produtos > 0 else 1
    
    if page < 1:
        page = 1
    elif page > total_pages and total_pages > 0:
        page = total_pages
    
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    produtos_paginados = produtos_todos[start_idx:end_idx]
    
    # Calcular quantidade_entrada_real apenas para produtos paginados
    # Reutilizar quantidade_vendida_por_produto já calculado acima
    produtos_com_entrada_real = []
    for produto in produtos_paginados:
        quantidade_vendida = quantidade_vendida_por_produto.get(produto.id, 0)
        if produto.quantidade_entrada == 0 or produto.quantidade_entrada < (produto.estoque_atual + quantidade_vendida):
            quantidade_entrada_exibicao = produto.estoque_atual + quantidade_vendida
        else:
            quantidade_entrada_exibicao = produto.quantidade_entrada
        produtos_com_entrada_real.append({
            'produto': produto,
            'quantidade_entrada_exibicao': quantidade_entrada_exibicao
        })
    
    # Agrupar apenas produtos paginados para exibição
    produtos_por_tipo = {}
    for item in produtos_com_entrada_real:
        tipo_key = _normalizar_tipo_ui(item['produto'].tipo)
        if tipo_key not in produtos_por_tipo:
            produtos_por_tipo[tipo_key] = []
        produtos_por_tipo[tipo_key].append(item)

    # Ordenar produtos dentro de cada tipo por data
    for tipo in produtos_por_tipo:
        produtos_por_tipo[tipo].sort(
            key=lambda x: x['produto'].data_chegada.date() if hasattr(x['produto'].data_chegada, 'date') else x['produto'].data_chegada,
            reverse=reverse_order
        )

    # Tipos em ordem preferencial
    preferidos = ['ALHO', 'SACOLA', 'CAFE', 'OUTROS']
    restantes = sorted(k for k in produtos_por_tipo if k not in preferidos)
    tipos_ordenados = [t for t in preferidos if t in produtos_por_tipo and produtos_por_tipo[t]] + restantes

    produtos_agrupados = {}
    for tipo in tipos_ordenados:
        produtos_agrupados[tipo] = produtos_por_tipo[tipo]

    outros_itens = produtos_agrupados.get('OUTROS', [])
    produtos_outros = [{'id': it['produto'].id, 'nome_produto': it['produto'].nome_produto} for it in outros_itens]

    # Criar objeto pagination simulado
    class Pagination:
        def __init__(self, page, per_page, total):
            self.page = page
            self.per_page = per_page
            self.total = total
            self.pages = total_pages
            self.has_prev = page > 1
            self.has_next = page < total_pages
            self.prev_num = page - 1 if self.has_prev else None
            self.next_num = page + 1 if self.has_next else None
    
    pagination = Pagination(page, per_page, total_produtos)
    
    # Verificar se é requisição AJAX
    is_ajax = request.args.get('ajax', type=int) == 1
    
    if is_ajax:
        # Renderizar apenas o template parcial com as linhas
        return render_template('_linhas_entrada.html', 
                             produtos_agrupados=produtos_agrupados,
                             current_page=page)

    return render_template(
        'produtos/listar.html',
        produtos_agrupados=produtos_agrupados,
        produtos_com_entrada=produtos_com_entrada_real,
        produtos=produtos_paginados,
        produtos_outros=produtos_outros,
        ordem_data=ordem_data,
        totais_por_tipo=totais_por_tipo,
        pagination=pagination,
    )


@app.route('/produtos/novo', methods=['GET', 'POST'])
@login_required
def novo_produto():
    if request.method == 'POST':
        fornecedor = request.form.get('fornecedor', '').strip()
        preco_custo = request.form.get('preco_custo', '').strip()
        caminhoneiro = request.form.get('caminhoneiro', '').strip()

        if not fornecedor:
            msg = 'Fornecedor é obrigatório!'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)
        if not preco_custo:
            msg = 'Preço de custo é obrigatório!'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)
        if not caminhoneiro:
            msg = 'Caminhoneiro é obrigatório!'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)

        tipo = (request.form.get('tipo') or '').strip()
        nacionalidade = request.form.get('nacionalidade', '').strip()
        marca = request.form.get('marca', '').strip()
        tamanho = request.form.get('tamanho', '').strip()
        try:
            quantidade_entrada = int(request.form.get('quantidade_entrada', 0))
        except (ValueError, TypeError):
            quantidade_entrada = 0
        data_chegada_raw = request.form.get('data_chegada')
        data_chegada = date.fromisoformat(data_chegada_raw) if data_chegada_raw else date.today()
        if not tipo:
            msg = 'Tipo é obrigatório!'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)
        if quantidade_entrada <= 0:
            msg = 'Quantidade de entrada deve ser maior que zero.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            return render_template('produtos/formulario.html', produto=None)
        try:
            nacionalidade, marca, tamanho = _validar_sacola(tipo, nacionalidade, marca, tamanho)
        except ValueError as e:
            if _is_ajax():
                return jsonify(ok=False, mensagem=str(e)), 400
            flash(str(e), 'error')
            return render_template('produtos/formulario.html', produto=None)

        nome_produto = gerar_nome_produto(tipo, nacionalidade, marca, data_chegada, tamanho)
        produto = Produto(
            tipo=tipo,
            nacionalidade=nacionalidade,
            marca=marca,
            tamanho=tamanho,
            fornecedor=fornecedor,
            caminhoneiro=caminhoneiro,
            preco_custo=Decimal(str(_limpar_valor_moeda(preco_custo))),
            preco_venda_alvo=None,
            quantidade_entrada=quantidade_entrada,  # Quantidade original que entrou
            estoque_atual=quantidade_entrada,  # Inicia com a mesma quantidade
            data_chegada=data_chegada,
            nome_produto=nome_produto
        )
        db.session.add(produto)
        db.session.commit()

        # Upload de fotos para Cloudinary (até 5)
        if os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL') or (app.config.get('CLOUDINARY_CLOUD_NAME') and app.config.get('CLOUDINARY_API_KEY')):
            fotos = request.files.getlist('fotos')
            for foto in fotos[:5]:
                if foto and foto.filename:
                    try:
                        upload_result = cloudinary.uploader.upload(foto, folder="menino_do_alho/produtos")
                        url_segura = upload_result.get('secure_url')
                        if url_segura:
                            nova_foto = ProdutoFoto(produto_id=produto.id, arquivo=url_segura)
                            db.session.add(nova_foto)
                    except Exception as e:
                        print(f"Erro ao fazer upload para o Cloudinary (produto {produto.id}): {e}")
        db.session.commit()

        limpar_cache_dashboard()  # Limpar cache após nova entrada de produto
        if _is_ajax():
            return jsonify(ok=True, mensagem='Produto cadastrado com sucesso!')
        flash('Produto cadastrado com sucesso!', 'success')
        return redirect(url_for('listar_produtos'))

    return render_template('produtos/formulario.html', produto=None)


@app.route('/produtos/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_produto(id):
    produto = Produto.query.get_or_404(id)
    if request.method == 'POST':
        # Validação de campos obrigatórios
        fornecedor = request.form.get('fornecedor', '').strip()
        preco_custo = request.form.get('preco_custo', '').strip()
        caminhoneiro = request.form.get('caminhoneiro', '').strip()
        
        if not fornecedor:
            flash('Fornecedor é obrigatório!', 'error')
            return redirect(url_for('listar_produtos'))
        if not preco_custo:
            flash('Preço de custo é obrigatório!', 'error')
            return redirect(url_for('listar_produtos'))
        if not caminhoneiro:
            flash('Caminhoneiro é obrigatório!', 'error')
            return redirect(url_for('listar_produtos'))
        
        tipo = (request.form.get('tipo') or '').strip()
        nacionalidade = request.form.get('nacionalidade', '').strip()
        marca = request.form.get('marca', '').strip()
        tamanho = request.form.get('tamanho', '').strip()
        try:
            quantidade_entrada = int(request.form.get('quantidade_entrada', 0))
        except (ValueError, TypeError):
            quantidade_entrada = 0
        try:
            nacionalidade, marca, tamanho = _validar_sacola(tipo, nacionalidade, marca, tamanho)
        except ValueError as e:
            flash(str(e), 'error')
            return redirect(url_for('listar_produtos'))
        
        # Atualizar data de chegada se fornecida, senão manter a atual
        data_chegada_raw = request.form.get('data_chegada')
        if data_chegada_raw:
            data_chegada = date.fromisoformat(data_chegada_raw)
        else:
            data_chegada = produto.data_chegada
        
        # EDIÇÃO MANUAL: Sempre gerar nome_produto automaticamente via concatenação
        nome_produto = gerar_nome_produto(tipo, nacionalidade, marca, data_chegada, tamanho)
        
        # Se houver nova entrada, somar ao estoque atual
        if quantidade_entrada > 0:
            produto.estoque_atual += quantidade_entrada

        produto.tipo = tipo
        produto.nacionalidade = nacionalidade
        produto.marca = marca
        produto.tamanho = tamanho
        produto.fornecedor = fornecedor
        produto.caminhoneiro = caminhoneiro
        produto.preco_custo = Decimal(str(_limpar_valor_moeda(preco_custo)))
        produto.data_chegada = data_chegada
        produto.nome_produto = nome_produto
        
        # Upload de fotos adicionais para Cloudinary (até 5 no total)
        fotos_existentes = ProdutoFoto.query.filter_by(produto_id=produto.id).count()
        slots_disponiveis = max(0, 5 - fotos_existentes)
        if slots_disponiveis > 0 and (os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL') or (app.config.get('CLOUDINARY_CLOUD_NAME') and app.config.get('CLOUDINARY_API_KEY'))):
            fotos = request.files.getlist('fotos')
            for foto in fotos[:slots_disponiveis]:
                if foto and foto.filename:
                    try:
                        upload_result = cloudinary.uploader.upload(foto, folder="menino_do_alho/produtos")
                        url_segura = upload_result.get('secure_url')
                        if url_segura:
                            nova_foto = ProdutoFoto(produto_id=produto.id, arquivo=url_segura)
                            db.session.add(nova_foto)
                    except Exception as e:
                        print(f"Erro ao fazer upload para o Cloudinary (produto {produto.id}): {e}")

        db.session.commit()
        limpar_cache_dashboard()  # Limpar cache após editar produto
        flash('Produto atualizado com sucesso!', 'success')
        return redirect(url_for('listar_produtos'))
    
    return render_template('produtos/formulario.html', produto=produto)


@app.route('/produtos/excluir/<int:id>', methods=['POST'])
@login_required
def excluir_produto(id):
    produto = Produto.query.get_or_404(id)
    try:
        db.session.delete(produto)
        db.session.commit()
        limpar_cache_dashboard()  # Limpar cache após excluir produto
        flash('Produto excluído com sucesso!', 'success')
    except IntegrityError:
        db.session.rollback()
        flash('ERRO: Esse produto não pode ser excluído pois já existem vendas registradas nele.', 'error')
    
    return redirect(url_for('listar_produtos'))


@app.route('/bulk_delete_produtos', methods=['POST'])
@login_required
def bulk_delete_produtos():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'ok': False, 'mensagem': 'Nenhum ID informado.'}), 400
    excluidos = 0
    ids_erro = []
    for id_ in ids:
        produto = Produto.query.get(id_)
        if not produto:
            continue
        try:
            db.session.delete(produto)
            db.session.commit()
            excluidos += 1
        except IntegrityError:
            db.session.rollback()
            ids_erro.append(id_)
    if excluidos > 0:
        limpar_cache_dashboard()  # Limpar cache após excluir produtos em massa (se houver exclusões)
    if ids_erro and not excluidos:
        return jsonify({
            'ok': False,
            'mensagem': f'Nenhum produto excluído. Os IDs {ids_erro} possuem vendas vinculadas e não podem ser removidos.',
            'excluidos': 0,
            'ids_erro': ids_erro
        })
    if ids_erro:
        return jsonify({
            'ok': True,
            'mensagem': f'{excluidos} produto(s) excluído(s). Os IDs {ids_erro} não puderam ser excluídos (vendas vinculadas).',
            'excluidos': excluidos,
            'ids_erro': ids_erro
        })
    return jsonify({'ok': True, 'mensagem': f'{excluidos} produto(s) excluído(s) com sucesso.', 'excluidos': excluidos})


@app.route('/produtos/atualizar_tipo_batch', methods=['POST'])
@login_required
def produtos_atualizar_tipo_batch():
    """Atualiza o campo tipo de vários produtos (usado em 'Corrigir Categoria' para OUTROS)."""
    data = request.get_json(silent=True) or {}
    updates = data.get('updates', [])
    if not updates:
        return jsonify({'ok': False, 'mensagem': 'Nenhuma alteração informada.'}), 400
    permitidos = {'ALHO', 'SACOLA', 'CAFE'}
    ok_count = 0
    for u in updates:
        pid = u.get('id')
        novoTipo = (u.get('tipo') or '').strip().upper()
        if pid is None or novoTipo not in permitidos:
            continue
        p = Produto.query.get(pid)
        if not p or p.tipo != 'OUTROS':
            continue
        p.tipo = novoTipo
        ok_count += 1
    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'mensagem': str(e)}), 500
    return jsonify({'ok': True, 'mensagem': f'{ok_count} produto(s) atualizado(s).', 'atualizados': ok_count})


def _row_get(row, *keys):
    for k in keys:
        if k not in row:
            continue
        v = row[k]
        if pd.isna(v):
            continue
        s = str(v).strip()
        if s != '':
            return v
    return None


# Ordem posicional para importação "raw" (sem cabeçalho): coluna 3 = Valor Total (ignorada)
_RAW_IMPORT_MAP = [
    ('nome_produto', 0),      # Produto
    ('preco_custo', 1),       # Preço Custo (string suja -> float no tratamento)
    ('quantidade_entrada', 2), # Quantidade
    None,                      # Index 3: Valor Total (ignorar)
    ('data_chegada', 4),      # Data Chegada
    ('tipo', 5),
    ('fornecedor', 6),
    ('nacionalidade', 7),
    ('tamanho', 8),
    ('marca', 9),
    ('caminhoneiro', 10),
]


def _load_csv_produtos_flexible(filepath):
    """Carrega CSV de importação de produtos com detecção de formato.
    - Se não houver vírgulas na primeira linha, usa Tab (\\t) como separador.
    - Se a primeira linha contiver 'R$', assume formato raw (sem cabeçalho) e mapeamento posicional.
    Retorna (df, is_raw). Em modo raw, df já tem colunas canônicas (nome_produto, preco_custo, etc.)."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception as e:
        return None, False
    lines = [ln.strip() for ln in content.strip().splitlines() if ln.strip()]
    if not lines:
        return None, False
    first_line = lines[0]
    # Detecção de separador: se não tiver vírgulas, usar tabulação
    sep = '\t' if '\t' in first_line else ','
    is_raw = 'R$' in first_line
    if is_raw:
        rows = []
        reader = csv.reader(io.StringIO(content), delimiter=sep, quotechar='"')
        for row in reader:
            if not row:
                continue
            d = {}
            for i, entry in enumerate(_RAW_IMPORT_MAP):
                if entry is None:
                    continue
                key, idx = entry
                d[key] = row[idx] if idx < len(row) else ''
            rows.append(d)
        df = pd.DataFrame(rows)
        return df, True
    df = pd.read_csv(io.StringIO(content), sep=sep, engine='python', quoting=csv.QUOTE_MINIMAL, on_bad_lines='warn')
    return df, False


@app.route('/produtos/importar', methods=['GET', 'POST'])
@login_required
@admin_required
def importar_produtos():
    if request.method == 'POST':
        if 'arquivo' not in request.files:
            return render_template('produtos/importar.html', erros_detalhados=['Nenhum arquivo selecionado. Escolha um arquivo e tente novamente.'], sucesso=0, erros=1)
        arquivo = request.files['arquivo']
        if arquivo.filename == '':
            return render_template('produtos/importar.html', erros_detalhados=['Nenhum arquivo selecionado. Escolha um arquivo e tente novamente.'], sucesso=0, erros=1)
        filepath = None
        try:
            filename = secure_filename(arquivo.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            arquivo.save(filepath)
            is_raw = False
            if filename.endswith('.csv'):
                df, is_raw = _load_csv_produtos_flexible(filepath)
                if df is None:
                    return render_template('produtos/importar.html', erros_detalhados=['O arquivo CSV está vazio ou não pôde ser lido.'], sucesso=0, erros=1)
            else:
                df = pd.read_excel(filepath)
            if not is_raw:
                rename_dict = {}
                seen_canonical = set()
                for col in list(df.columns):
                    n = _normalizar_nome_coluna(col)
                    can = COLUNA_ARQUIVO_PARA_BANCO.get(n)
                    if can and can not in seen_canonical:
                        rename_dict[col] = can
                        seen_canonical.add(can)
                df = df.rename(columns=rename_dict)
            sucesso = 0
            erros = 0
            ignorados = 0
            erros_detalhados = []
            outros_nomes = []
            for idx, row in df.iterrows():
                linha_num = (idx + 1) if is_raw else (idx + 2)
                v = _row_get(row, 'nome_produto', 'produto', 'nome')
                nome_produto_arquivo = _strip_quotes(v) if v is not None else None
                if nome_produto_arquivo == '':
                    nome_produto_arquivo = None
                try:
                    tipo_raw = _strip_quotes(_row_get(row, 'tipo', 'categoria') or '').upper()
                    tipo = _normalizar_tipo_ui(tipo_raw)
                    nacionalidade = _strip_quotes(_row_get(row, 'nacionalidade', 'origem') or '')
                    marca = _strip_quotes(_row_get(row, 'marca') or '')
                    tamanho_raw = _row_get(row, 'tamanho', 'classificacao')
                    tamanho = _strip_quotes(tamanho_raw or '').upper() if tamanho_raw is not None else ''
                    tamanho = (tamanho or '').strip()
                    contexto = nome_produto_arquivo or f'{tipo} {marca} {tamanho}'.strip() or 'linha'
                    contexto = (contexto[:45] + '...') if len(contexto) > 45 else contexto

                    if tipo == 'SACOLA':
                        nacionalidade = 'N/A'
                        marca = 'SOPACK'
                        t_norm = tamanho.replace(' ', '')
                        if t_norm not in ('P', 'M', 'G', 'S/N'):
                            erros_detalhados.append(_msg_linha(linha_num, contexto, "Para SACOLA, o tamanho deve ser P, M, G ou S/N. Valor informado inválido ou vazio", True))
                            erros += 1
                            continue
                        tamanho = t_norm
                    else:
                        nacionalidade = nacionalidade or ''
                        tamanho = tamanho or ''
                        if not marca:
                            erros_detalhados.append(_msg_linha(linha_num, contexto, "O campo 'marca' está vazio. Preencha com o nome da marca (ex: IMPORFOZ)", True))
                            erros += 1
                            continue

                    quantidade = _parse_quantidade(_row_get(row, 'quantidade_entrada', 'quantidade', 'qtd'))
                    if quantidade is None or quantidade < 0:
                        qraw = _row_get(row, 'quantidade_entrada', 'quantidade', 'qtd')
                        erros_detalhados.append(_msg_linha(linha_num, contexto, f"A quantidade está vazia ou inválida ({qraw}). Use um número inteiro (ex: 10)", True))
                        erros += 1
                        continue
                    fornecedor_valor = _strip_quotes(_row_get(row, 'fornecedor') or '')
                    preco_raw = _row_get(row, 'preco_custo', 'preco', 'preço')
                    preco_custo_valor = _parse_preco(preco_raw)
                    caminhoneiro_valor = _strip_quotes(_row_get(row, 'caminhoneiro') or '')
                    if not fornecedor_valor:
                        erros_detalhados.append(_msg_linha(linha_num, contexto, "O campo 'fornecedor' está vazio. Use DESTAK ou PATY", True))
                        erros += 1
                        continue
                    if preco_custo_valor is None:
                        txt = f"O preço '{preco_raw}' não pôde ser convertido. Use formato brasileiro (ex: 143,00 ou -120,00 para ajustes) ou use ponto como decimal" if preco_raw else "O campo 'preco_custo' (ou 'preco') está vazio"
                        erros_detalhados.append(_msg_linha(linha_num, contexto, txt, True))
                        erros += 1
                        continue
                    if not caminhoneiro_valor:
                        erros_detalhados.append(_msg_linha(linha_num, contexto, "O campo 'caminhoneiro' está vazio. Informe o nome do caminhoneiro", True))
                        erros += 1
                        continue
                    fornecedor_valor = fornecedor_valor.upper()
                    data_chegada_valor = _row_get(row, 'data_chegada', 'data')
                    if data_chegada_valor is not None and pd.notna(data_chegada_valor):
                        data_parsed, _ = _parse_data_flex(data_chegada_valor)
                        data_chegada = data_parsed if data_parsed else date.today()
                    else:
                        data_chegada = date.today()
                    if nome_produto_arquivo:
                        nome_produto = nome_produto_arquivo
                    else:
                        nome_produto = gerar_nome_produto(tipo, nacionalidade, marca, data_chegada, tamanho)
                    dup = Produto.query.filter(
                        Produto.nome_produto == nome_produto,
                        Produto.data_chegada == data_chegada,
                        Produto.quantidade_entrada == quantidade,
                        Produto.fornecedor == fornecedor_valor
                    ).first()
                    if dup:
                        ignorados += 1
                        continue
                    produto_existente = Produto.query.filter_by(nome_produto=nome_produto).first()
                    if produto_existente:
                        produto_existente.estoque_atual += quantidade
                        produto_existente.preco_custo = Decimal(str(preco_custo_valor))
                        produto_existente.fornecedor = fornecedor_valor
                        produto_existente.caminhoneiro = caminhoneiro_valor
                        db.session.commit()
                        sucesso += 1
                    else:
                        produto = Produto(
                            tipo=tipo,
                            nacionalidade=nacionalidade,
                            marca=marca,
                            tamanho=tamanho,
                            fornecedor=fornecedor_valor,
                            caminhoneiro=caminhoneiro_valor,
                            preco_custo=Decimal(str(preco_custo_valor)),
                            quantidade_entrada=quantidade,
                            estoque_atual=quantidade,
                            data_chegada=data_chegada,
                            nome_produto=nome_produto
                        )
                        db.session.add(produto)
                        db.session.commit()
                        sucesso += 1
                        if tipo == 'OUTROS':
                            outros_nomes.append(nome_produto)
                except Exception as e:
                    db.session.rollback()
                    ctx = (nome_produto_arquivo or f'linha {linha_num}')
                    ctx = (ctx[:45] + '...') if len(ctx) > 45 else ctx
                    erros_detalhados.append(_msg_linha(linha_num, ctx, str(e), True))
                    erros += 1
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
            if erros > 0:
                return render_template('produtos/importar.html', erros_detalhados=erros_detalhados, sucesso=sucesso, erros=erros, ignorados=ignorados)
            msg = f'Importação concluída: {sucesso} novo(s).'
            if ignorados > 0:
                msg += f' {ignorados} ignorado(s) por já existirem.'
            flash(msg, 'success')
            if outros_nomes:
                nomes_lista = ', '.join(outros_nomes[:20])
                if len(outros_nomes) > 20:
                    nomes_lista += f' e mais {len(outros_nomes) - 20}.'
                flash(f'Atenção: {len(outros_nomes)} produto(s) foram movidos para "OUTROS" por falta de categoria: {nomes_lista}', 'warning')
            return redirect(url_for('listar_produtos'))
        except Exception as e:
            db.session.rollback()
            # #region agent log
            _debug_log("app.py:importar_produtos", "outer except", {"route": "importar_produtos", "exc_type": type(e).__name__, "exc_msg": str(e), "tb": traceback.format_exc()}, "H1")
            # #endregion
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            return render_template('produtos/importar.html', erros_detalhados=[f'Erro ao processar o arquivo: {str(e)}'], sucesso=0, erros=1)
    return render_template('produtos/importar.html')


# ========== MÓDULO VENDAS ==========

@app.route('/api/produtos/<int:produto_id>/fotos')
@login_required
def get_fotos_produto(produto_id):
    """Retorna URLs das fotos do produto para a galeria no modal. Cloudinary: URL completa em arquivo. Local: fallback para static/uploads/."""
    fotos = ProdutoFoto.query.filter_by(produto_id=produto_id).all()
    urls = []
    for f in fotos:
        if f.arquivo and (f.arquivo.startswith('http://') or f.arquivo.startswith('https://')):
            urls.append(f.arquivo)
        elif f.arquivo:
            urls.append(url_for('static', filename=f'uploads/{f.arquivo}'))
    return jsonify(urls)


@app.route('/api/vendas_por_filtro')
@login_required
def api_vendas_por_filtro():
    """Retorna vendas em JSON filtradas por produto_id ou cliente_id com paginação e totais."""
    produto_id = request.args.get('produto_id', type=int)
    cliente_id = request.args.get('cliente_id', type=int)
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)
    
    if not produto_id and not cliente_id:
        return jsonify({'erro': 'Informe produto_id ou cliente_id'}), 400
    
    query = Venda.query
    if produto_id:
        query = query.filter(Venda.produto_id == produto_id)
    if cliente_id:
        query = query.filter(Venda.cliente_id == cliente_id)
    
    # Calcular totais ANTES de paginar (para clientes e produtos)
    total_vendido = None
    total_lucro = None
    total_qtd = None
    if cliente_id:
        # Query para calcular totais de TODAS as vendas do cliente
        vendas_totais = query.all()
        total_vendido = sum(float(v.preco_venda * v.quantidade_venda) for v in vendas_totais)
        total_lucro = sum(float(v.calcular_lucro()) for v in vendas_totais)
    elif produto_id:
        # Totais de TODAS as vendas do produto
        vendas_totais = query.all()
        total_qtd = sum(v.quantidade_venda for v in vendas_totais)
        total_vendido = sum(float(v.preco_venda * v.quantidade_venda) for v in vendas_totais)
        total_lucro = sum(float(v.calcular_lucro()) for v in vendas_totais)
    
    # Ordenar por data (decrescente), NF e ID para agrupar visualmente
    query_ordenada = query.order_by(desc(Venda.data_venda), Venda.nf, desc(Venda.id))
    
    # Aplicar paginação
    pagination = query_ordenada.paginate(page=page, per_page=per_page, error_out=False)
    vendas = pagination.items
    
    titulo = None
    cliente_info = None
    if produto_id:
        p = Produto.query.get(produto_id)
        titulo = f"Vendas do Produto {p.nome_produto}" if p else "Vendas do Produto"
    elif cliente_id:
        c = Cliente.query.get(cliente_id)
        titulo = f"Vendas do Cliente {c.nome_cliente}" if c else "Vendas do Cliente"
        if c:
            cliente_info = {
                'cnpj': c.cnpj or '-',
                'razao_social': c.razao_social or '-'
            }
    
    # Processar vendas e adicionar propriedade grupo_cor para agrupamento visual
    lista = []
    grupo_atual = 1  # Começa com grupo 1 (par - cinza claro)
    nf_anterior = None
    
    for v in vendas:
        # Normalizar NF: None ou string vazia vira '-'
        nf_atual = (v.nf or '-').strip() if v.nf else '-'
        
        # Se a NF mudou em relação à anterior, alterna o grupo
        if nf_anterior is not None and nf_atual != nf_anterior:
            grupo_atual = 2 if grupo_atual == 1 else 1
        
        lista.append({
            'data': v.data_venda.strftime('%d/%m/%Y'),
            'nf': nf_atual,
            'produto': v.produto.nome_produto if v.produto else '-',
            'preco_unitario': float(v.preco_venda),
            'quantidade': v.quantidade_venda,
            'valor': float(v.preco_venda * v.quantidade_venda),
            'lucro': float(v.calcular_lucro()),
            'empresa': v.empresa_faturadora or '-',
            'situacao': v.situacao,
            'grupo_cor': grupo_atual,  # 1 = par (cinza claro), 2 = ímpar (branco)
        })
        
        nf_anterior = nf_atual
    
    resposta = {
        'titulo': titulo,
        'vendas': lista,
        'pagination': {
            'page': pagination.page,
            'per_page': pagination.per_page,
            'total': pagination.total,
            'pages': pagination.pages,
            'has_next': pagination.has_next,
            'has_prev': pagination.has_prev
        }
    }
    
    # Adicionar totais para clientes e produtos
    if cliente_id and total_vendido is not None:
        resposta['totais'] = {
            'total_vendido': total_vendido,
            'total_lucro': total_lucro
        }
    elif produto_id and total_vendido is not None:
        resposta['totais'] = {
            'total_qtd': total_qtd or 0,
            'total_vendido': total_vendido,
            'total_lucro': total_lucro
        }
    
    if cliente_info:
        resposta['cliente_info'] = cliente_info
    
    return jsonify(resposta)


@app.route('/api/dashboard/detalhes/<filtro>')
@login_required
def api_dashboard_detalhes(filtro):
    """Retorna lista de vendas filtradas por pendente, pago, avulsa, paty ou destak."""
    filtros_validos = ('pendente', 'pago', 'avulsa', 'paty', 'destak')
    if filtro not in filtros_validos:
        return jsonify({'erro': f'Filtro inválido. Use: {", ".join(filtros_validos)}'}), 400
    try:
        ano_ativo = session.get('ano_ativo', datetime.now().year)
        filtro_ano_venda = extract('year', Venda.data_venda) == ano_ativo

        query = Venda.query.filter(filtro_ano_venda)
        if filtro == 'pendente':
            query = query.filter(Venda.situacao == 'PENDENTE')
        elif filtro == 'pago':
            query = query.filter(Venda.situacao == 'PAGO')
        elif filtro == 'avulsa':
            query = query.filter(~Venda.empresa_faturadora.in_(['PATY', 'DESTAK']))
        elif filtro == 'paty':
            query = query.filter(Venda.empresa_faturadora == 'PATY')
        elif filtro == 'destak':
            query = query.filter(Venda.empresa_faturadora == 'DESTAK')

        vendas = query.order_by(Venda.data_venda.desc(), Venda.id.desc()).all()
        vendas_lista = []
        for venda in vendas:
            vendas_lista.append({
                'id': venda.id,
                'cliente': venda.cliente.nome_cliente if venda.cliente else 'Cliente Desconhecido',
                'descricao': venda.produto.nome_produto if venda.produto else 'Produto Desconhecido',
                'data': venda.data_venda.strftime('%d/%m/%Y'),
                'valor': float(venda.preco_venda * venda.quantidade_venda),
                'status': venda.situacao,
            })
        return jsonify({'vendas': vendas_lista})
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'erro': str(e)}), 500


@app.route('/api/cobrancas_pendentes')
@login_required
def api_cobrancas_pendentes():
    """Retorna se há cobranças pendentes (vendas não pagas) para notificações no dispositivo."""
    try:
        ano_ativo = session.get('ano_ativo', datetime.now().year)
        filtro_ano = extract('year', Venda.data_venda) == ano_ativo
        total = db.session.query(
            func.sum(Venda.preco_venda * Venda.quantidade_venda)
        ).filter(Venda.situacao == 'PENDENTE', filtro_ano).scalar() or 0
        return jsonify({'has_pendentes': float(total) > 0, 'total': float(total)})
    except Exception:
        db.session.rollback()
        return jsonify({'has_pendentes': False, 'total': 0})


@app.route('/api/dashboard/detalhes_mes/<int:ano>/<int:mes>')
@login_required
def api_detalhes_mes(ano, mes):
    """Retorna detalhes completos de um mês específico: totais, top clientes e lista de vendas."""
    try:
        # Validar mês (1-12)
        if mes < 1 or mes > 12:
            return jsonify({'erro': 'Mês inválido. Use valores de 1 a 12.'}), 400
        
        # Filtrar vendas do mês e ano específicos
        vendas_mes = Venda.query.filter(
            extract('year', Venda.data_venda) == ano,
            extract('month', Venda.data_venda) == mes
        ).order_by(Venda.data_venda, Venda.id).all()
        
        if not vendas_mes:
            meses_pt = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho', 
                        'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
            return jsonify({
                'erro': f'Nenhuma venda encontrada para {meses_pt[mes-1]}/{ano}',
                'totais': {'total_vendido': 0, 'total_lucro': 0},
                'top_clientes': [],
                'vendas': []
            })
        
        # Calcular totais
        total_vendido = sum(float(v.preco_venda * v.quantidade_venda) for v in vendas_mes)
        total_lucro = sum(float(v.calcular_lucro()) for v in vendas_mes)
        
        # Agrupar por cliente e calcular totais por cliente
        clientes_dict = {}
        for venda in vendas_mes:
            cliente_id = venda.cliente_id
            cliente_nome = venda.cliente.nome_cliente if venda.cliente else 'Cliente Desconhecido'
            
            if cliente_id not in clientes_dict:
                clientes_dict[cliente_id] = {
                    'nome': cliente_nome,
                    'qtd_compras': 0,
                    'total_gasto': 0.0
                }
            
            clientes_dict[cliente_id]['qtd_compras'] += 1
            clientes_dict[cliente_id]['total_gasto'] += float(venda.preco_venda * venda.quantidade_venda)
        
        # Converter para lista e ordenar por total_gasto (decrescente)
        top_clientes = [
            {
                'nome': dados['nome'],
                'qtd_compras': dados['qtd_compras'],
                'total_gasto': dados['total_gasto']
            }
            for cliente_id, dados in clientes_dict.items()
        ]
        top_clientes.sort(key=lambda x: x['total_gasto'], reverse=True)
        
        # Preparar lista de vendas
        vendas_lista = []
        for venda in vendas_mes:
            vendas_lista.append({
                'id': venda.id,
                'data': venda.data_venda.strftime('%d/%m/%Y'),
                'cliente': venda.cliente.nome_cliente if venda.cliente else 'Cliente Desconhecido',
                'produto': venda.produto.nome_produto if venda.produto else 'Produto Desconhecido',
                'quantidade': venda.quantidade_venda,
                'preco_unitario': float(venda.preco_venda),
                'valor_total': float(venda.preco_venda * venda.quantidade_venda),
                'lucro': float(venda.calcular_lucro()),
                'nf': venda.nf or '-',
                'empresa': venda.empresa_faturadora or '-',
                'situacao': venda.situacao
            })
        
        # Nome do mês em português
        meses_pt = ['Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho', 
                    'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
        mes_nome = meses_pt[mes - 1]
        
        return jsonify({
            'ano': ano,
            'mes': mes,
            'mes_nome': mes_nome,
            'totais': {
                'total_vendido': total_vendido,
                'total_lucro': total_lucro
            },
            'top_clientes': top_clientes,
            'vendas': vendas_lista,
            'total_vendas': len(vendas_lista)
        })
        
    except Exception as e:
        db.session.rollback()
        import traceback
        traceback.print_exc()
        return jsonify({'erro': f'Erro ao processar dados do mês: {str(e)}'}), 500


@app.route('/vendas')
@login_required
def listar_vendas():
    # #region agent log
    _debug_log("app.py:listar-vendas", "listar_vendas entered", {"route": "/vendas"}, "H3")
    # #endregion
    # Aceitar filtros via query string
    produto_id = request.args.get('produto_id', type=int)
    cliente_id = request.args.get('cliente_id', type=int)
    filtro_vencidos = request.args.get('filtro_vencidos', type=int) == 1

    # Ordenação por data ou vencimento (padrão: decrescente)
    ordem_data = request.args.get('ordem_data') or session.get('ordem_data_vendas', 'decrescente')
    if ordem_data not in ('crescente', 'decrescente', 'vencimento_crescente', 'vencimento_decrescente'):
        ordem_data = 'decrescente'
    session['ordem_data_vendas'] = ordem_data
    
    # Obter ano ativo da sessão
    ano_ativo = session.get('ano_ativo', datetime.now().year)
    
    # Subquery: IDs das 1000 vendas mais recentes (evita carregar todo o histórico)
    subq_ids = db.session.query(Venda.id).filter(
        extract('year', Venda.data_venda) == ano_ativo
    )
    if produto_id:
        subq_ids = subq_ids.filter(Venda.produto_id == produto_id)
    if cliente_id:
        subq_ids = subq_ids.filter(Venda.cliente_id == cliente_id)
    subq_ids = subq_ids.order_by(desc(Venda.data_venda), desc(Venda.id)).limit(1000).subquery()

    # Query base com eager loading (evita N+1) e limite de registros
    query = Venda.query.options(
        joinedload(Venda.cliente),
        joinedload(Venda.produto)
    ).filter(Venda.id.in_(subq_ids))
    
    # Ordenar por Cliente, Data (conforme escolha do usuário), NF e ID para agrupar pedidos visualmente
    # (Consumidores finais serão agrupados apenas por Cliente+Data, ignorando NF no template)
    if ordem_data == 'crescente':
        vendas_raw = query.order_by(
            Venda.cliente_id,
            asc(Venda.data_venda),
            Venda.nf,
            asc(Venda.id)
        ).all()
    else:  # decrescente (padrão)
        vendas_raw = query.order_by(
            Venda.cliente_id,
            desc(Venda.data_venda),
            Venda.nf,
            desc(Venda.id)
        ).all()
    
    # Agrupar vendas por pedido usando dicionário
    pedidos_dict = {}
    
    for venda in vendas_raw:
        cnpj_cliente = venda.cliente.cnpj or ''
        is_consumidor_final = cnpj_cliente in ('0', '00000000000000', '')
        
        # Normalizar data para comparação (garantir que seja date, não datetime)
        data_venda_normalizada = venda.data_venda.date() if hasattr(venda.data_venda, 'date') else venda.data_venda
        
        # Criar chave do pedido
        if is_consumidor_final:
            # CNPJ == '0': agrupar por (id_cliente, data_venda)
            pedido_key = (venda.cliente_id, data_venda_normalizada)
        else:
            # CNPJ != '0': agrupar por (id_cliente, nf, data_venda)
            nf_normalizada = str(venda.nf).strip() if venda.nf else ''
            pedido_key = (venda.cliente_id, nf_normalizada, data_venda_normalizada)
        
        # Se é um novo pedido, criar entrada no dicionário
        if pedido_key not in pedidos_dict:
            pedidos_dict[pedido_key] = {
                'key': pedido_key,
                'cliente_id': venda.cliente_id,
                'cliente_nome': venda.cliente.nome_cliente,
                'cliente_cnpj': cnpj_cliente,
                'nf': venda.nf or '-',
                'data_venda': venda.data_venda,
                'empresa_faturadora': venda.empresa_faturadora,
                'situacao': venda.situacao,
                'is_consumidor_final': is_consumidor_final,
                'vendas': [],
                'total_quantidade': 0,
                'total_valor': 0,
                'total_lucro': 0,
                'primeira_venda_id': venda.id,
            }
        
        # Adicionar venda ao pedido e acumular totais
        pedidos_dict[pedido_key]['vendas'].append(venda)
        pedidos_dict[pedido_key]['total_quantidade'] += venda.quantidade_venda
        pedidos_dict[pedido_key]['total_valor'] += float(venda.calcular_total())
        pedidos_dict[pedido_key]['total_lucro'] += float(venda.calcular_lucro())
        pedidos_dict[pedido_key]['total_valor_pago'] = pedidos_dict[pedido_key].get('total_valor_pago', 0) + float(getattr(venda, 'valor_pago', None) or 0)
    
    # Converter dicionário para lista mantendo a ordem original (já ordenada pela query)
    pedidos_agrupados = []
    pedidos_keys_vistos = set()
    for venda in vendas_raw:
        cnpj_cliente = venda.cliente.cnpj or ''
        is_consumidor_final = cnpj_cliente in ('0', '00000000000000', '')
        
        # Normalizar data para comparação
        data_venda_normalizada = venda.data_venda.date() if hasattr(venda.data_venda, 'date') else venda.data_venda
        
        if is_consumidor_final:
            pedido_key = (venda.cliente_id, data_venda_normalizada)
        else:
            nf_normalizada = str(venda.nf).strip() if venda.nf else ''
            pedido_key = (venda.cliente_id, nf_normalizada, data_venda_normalizada)
        
        if pedido_key not in pedidos_keys_vistos:
            pedidos_agrupados.append(pedidos_dict[pedido_key])
            pedidos_keys_vistos.add(pedido_key)
    
    # Ordenar pedidos por data_venda conforme escolha do usuário (exceto se for por vencimento)
    # A ordenação por vencimento é feita após o loop que define data_vencimento
    if ordem_data in ('crescente', 'decrescente'):
        reverse_order = (ordem_data == 'decrescente')
        # Normalizar data_venda para comparação (garantir que seja date, não datetime)
        pedidos_agrupados.sort(
            key=lambda x: x['data_venda'].date() if hasattr(x['data_venda'], 'date') else x['data_venda'],
            reverse=reverse_order
        )
    
    # Caminhos de boleto/NF: usar qualquer venda do pedido que tenha
    # Verificar se os documentos realmente existem no banco
    # #region agent log
    _debug_log("app.py:listar-pedidos-built", "Pedidos built", {"total_pedidos": len(pedidos_agrupados)}, "H3")
    # #endregion
    listar_pedido_sample = 0
    for pedido in pedidos_agrupados:
        cb, cn = None, None
        doc_boleto, doc_nf = None, None
        
        for v in pedido.get('vendas', []):
            caminho_b = (v.caminho_boleto or '').strip()
            if caminho_b:
                # Verificar se documento existe
                doc = Documento.query.filter_by(caminho_arquivo=caminho_b).first()
                if doc:
                    cb = caminho_b
                    doc_boleto = doc
                    break
                else:
                    # Limpar vínculo órfão
                    v.caminho_boleto = None
                    db.session.flush()
        
        for v in pedido.get('vendas', []):
            caminho_n = (v.caminho_nf or '').strip()
            if caminho_n:
                # Verificar se documento existe
                doc = Documento.query.filter_by(caminho_arquivo=caminho_n).first()
                if doc:
                    cn = caminho_n
                    doc_nf = doc
                    break
                else:
                    # Limpar vínculo órfão
                    v.caminho_nf = None
                    db.session.flush()
        
        pedido['caminho_boleto'] = cb
        pedido['caminho_nf'] = cn
        pedido['doc_boleto'] = doc_boleto  # None se não existir
        pedido['doc_nf'] = doc_nf  # None se não existir
        # Situação: pior entre as vendas (PENDENTE > PARCIAL > PAGO)
        situacoes = [str(v.situacao or '').strip().upper() for v in pedido.get('vendas', [])]
        if any(s == 'PENDENTE' for s in situacoes):
            pedido['situacao'] = 'PENDENTE'
        elif any(s == 'PARCIAL' for s in situacoes):
            pedido['situacao'] = 'PARCIAL'
        else:
            pedido['situacao'] = 'PAGO'
        # total_valor_pago já acumulado no loop anterior
        if 'total_valor_pago' not in pedido:
            pedido['total_valor_pago'] = sum(float(getattr(v, 'valor_pago', None) or 0) for v in pedido.get('vendas', []))
        # Data vencimento do boleto (primeira venda do pedido que tiver)
        dv = None
        for vv in pedido.get('vendas', []):
            if getattr(vv, 'data_vencimento', None) is not None:
                dv = vv.data_vencimento
                break
        if dv is None and doc_boleto and getattr(doc_boleto, 'data_vencimento', None) is not None:
            dv = doc_boleto.data_vencimento
        pedido['data_vencimento'] = dv
        hoje = get_hoje_brasil()
        # is_vencido: qualquer boleto vencido (vencimento < hoje) - para destacar em vermelho
        pedido['is_vencido'] = (
            pedido.get('situacao') == 'PENDENTE' and
            dv is not None and
            hoje > dv
        )
        # is_vencido_para_abatimento: vencido há mais de 1 dia - para filtro "Enviar para Fornecedor"
        venc_limite = (dv + timedelta(days=1)) if dv else None
        pedido['is_vencido_para_abatimento'] = (
            pedido.get('situacao') == 'PENDENTE' and
            venc_limite is not None and
            hoje > venc_limite
        )
        # #region agent log
        listar_pedido_sample += 1
        if listar_pedido_sample <= 8:
            _debug_log("app.py:listar-pedido", "Pedido caminho_nf/doc_nf", {"pedido_key": str(pedido.get('key')), "cn_set": cn is not None, "doc_nf_found": doc_nf is not None, "cb_set": cb is not None}, "H3")
        # #endregion
    
    # #region agent log
    n_nf = sum(1 for p in pedidos_agrupados if (p.get('caminho_nf') or '').strip())
    n_boleto = sum(1 for p in pedidos_agrupados if (p.get('caminho_boleto') or '').strip())
    _debug_log("app.py:listar-summary", "Pedidos com NF/boleto", {"total_pedidos": len(pedidos_agrupados), "com_nf": n_nf, "com_boleto": n_boleto}, "H3")
    # #endregion
    
    # Ordenação por vencimento (se solicitado)
    # Pedidos sem vencimento ficam no final
    if ordem_data == 'vencimento_crescente':
        pedidos_agrupados.sort(
            key=lambda x: (x.get('data_vencimento') is None, x.get('data_vencimento') or date.max)
        )
    elif ordem_data == 'vencimento_decrescente':
        pedidos_agrupados.sort(
            key=lambda x: (x.get('data_vencimento') is None, x.get('data_vencimento') or date.min),
            reverse=True
        )
    
    # Filtro "Ver Vencidos (Enviar para Fornecedor)": apenas pedidos vencidos há mais de 1 dia
    if filtro_vencidos:
        pedidos_agrupados = [p for p in pedidos_agrupados if p.get('is_vencido_para_abatimento')]
    # Commit das limpezas de vínculos órfãos
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    
    # Paginação: aplicar na lista de pedidos agrupados
    from math import ceil
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    # Criar objeto de paginação manual (já que não estamos usando query.paginate diretamente)
    total_pedidos = len(pedidos_agrupados)
    total_pages = ceil(total_pedidos / per_page) if total_pedidos > 0 else 1
    
    # Validar página
    if page < 1:
        page = 1
    elif page > total_pages and total_pages > 0:
        page = total_pages
    
    # Calcular índices para slice
    start_idx = (page - 1) * per_page
    end_idx = start_idx + per_page
    pedidos_paginados = pedidos_agrupados[start_idx:end_idx]
    
    # Criar objeto pagination simulado (compatível com Flask-SQLAlchemy)
    class Pagination:
        def __init__(self, page, per_page, total, items):
            self.page = page
            self.per_page = per_page
            self.total = total
            self.items = items
            self.pages = total_pages
            self.has_prev = page > 1
            self.has_next = page < total_pages
            self.prev_num = page - 1 if self.has_prev else None
            self.next_num = page + 1 if self.has_next else None
    
    pagination = Pagination(page, per_page, total_pedidos, pedidos_paginados)
    
    # Verificar se é requisição AJAX
    is_ajax = request.args.get('ajax', type=int) == 1
    
    if is_ajax:
        # Renderizar linhas (tabela) e cards (grade) para paginação em ambos os modos
        rows_html = render_template('_linhas_venda.html', pedidos=pedidos_paginados, current_page=page)
        cards_html = render_template('_cards_venda.html', pedidos=pedidos_paginados)
        return jsonify(rows=rows_html, cards=cards_html)
    
    # Buscar informações do filtro para exibição
    produto_filtro = None
    cliente_filtro = None
    
    if produto_id:
        produto_filtro = Produto.query.get(produto_id)
    
    if cliente_id:
        cliente_filtro = Cliente.query.get(cliente_id)

    clientes = Cliente.query.order_by(Cliente.nome_cliente).limit(500).all()
    produtos = Produto.query.filter(Produto.estoque_atual > 0).order_by(Produto.nome_produto).limit(500).all()
    todos_clientes = Cliente.query.order_by(Cliente.nome_cliente).limit(500).all()
    todos_produtos = Produto.query.order_by(Produto.nome_produto).limit(500).all()
    
    return render_template('vendas/listar.html', 
                         pedidos=pedidos_paginados,
                         pagination=pagination,
                         produto_filtro=produto_filtro,
                         cliente_filtro=cliente_filtro,
                         clientes=clientes,
                         produtos=produtos,
                         todos_clientes=todos_clientes,
                         todos_produtos=todos_produtos,
                         ordem_data=ordem_data,
                         filtro_vencidos=filtro_vencidos)


@app.route('/logistica')
@login_required
def logistica():
    """Roteirizador de Entregas: lista cada venda individualmente por status de entrega."""
    filtro_status = request.args.get('status', 'PENDENTE')
    if filtro_status not in ('PENDENTE', 'ENTREGUE'):
        filtro_status = 'PENDENTE'

    vendas = Venda.query.filter_by(status_entrega=filtro_status).options(
        joinedload(Venda.cliente),
        joinedload(Venda.produto)
    ).order_by(Venda.data_venda.desc()).all()

    entregas = []
    for v in vendas:
        cliente = v.cliente
        if not cliente:
            continue
        produto_nome = v.produto.nome_produto if v.produto else 'Item'
        entregas.append({
            'venda_id': v.id,
            'data': v.data_venda.strftime('%d/%m/%Y'),
            'cliente_nome': cliente.nome_cliente or 'Sem Nome',
            'endereco': cliente.endereco or '',
            'produto': f"{v.quantidade_venda}x {produto_nome}",
            'total': float(v.calcular_total()),
            'status_entrega': v.status_entrega or 'PENDENTE'
        })

    return render_template('logistica.html', entregas=entregas, filtro_status=filtro_status)


@app.route('/logistica/toggle/<int:venda_id>', methods=['POST'])
@login_required
def toggle_entrega(venda_id):
    """Alterna o status de entrega entre PENDENTE e ENTREGUE."""
    venda = Venda.query.get_or_404(venda_id)
    status = request.form.get('status', request.args.get('status', 'PENDENTE'))
    try:
        venda.status_entrega = 'ENTREGUE' if (venda.status_entrega or 'PENDENTE') == 'PENDENTE' else 'PENDENTE'
        db.session.commit()
        flash('Status de entrega atualizado com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        flash('Erro ao atualizar status de entrega. Tente novamente.', 'error')
    return redirect(url_for('logistica', status=status))


@app.route('/logistica/bulk_update', methods=['POST'])
@login_required
def logistica_bulk_update():
    """Atualiza status de entrega de vários pedidos de uma vez (ação em massa)."""
    dados = request.get_json() or {}
    ids_raw = dados.get('ids', [])
    novo_status = dados.get('status')

    if not ids_raw or not novo_status:
        return jsonify({'success': False, 'message': 'Dados inválidos.'}), 400

    try:
        ids = [int(x) for x in ids_raw]
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'IDs inválidos.'}), 400
    if novo_status not in ('PENDENTE', 'ENTREGUE'):
        return jsonify({'success': False, 'message': 'Status inválido.'}), 400

    try:
        Venda.query.filter(Venda.id.in_(ids)).update({'status_entrega': novo_status}, synchronize_session=False)
        db.session.commit()
        flash(f'{len(ids)} pedidos atualizados com sucesso!', 'success')
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': str(e)}), 500


@app.route('/vendas/novo', methods=['GET', 'POST'])
@login_required
def nova_venda():
    if request.method == 'POST':
        try:
            produto_id = int(request.form.get('produto_id', 0))
            quantidade_venda = int(request.form.get('quantidade_venda', 0))
        except (ValueError, TypeError):
            msg = 'Produto e quantidade são obrigatórios.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            clientes = Cliente.query.all()
            produtos = Produto.query.filter(Produto.estoque_atual > 0).all()
            return render_template('vendas/formulario.html', venda=None, clientes=clientes, produtos=produtos)

        # Validação de quantidade (deve ser positiva mesmo para perdas)
        if quantidade_venda <= 0:
            msg = 'A quantidade deve ser maior que zero (mesmo para perdas).'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            flash(msg, 'error')
            clientes = Cliente.query.all()
            produtos = Produto.query.filter(Produto.estoque_atual > 0).all()
            return render_template('vendas/formulario.html', venda=None, clientes=clientes, produtos=produtos)

        produto = Produto.query.get_or_404(produto_id)
        if produto.estoque_atual < quantidade_venda:
            msg = f'Estoque insuficiente! Disponível: {produto.estoque_atual}'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            clientes = Cliente.query.all()
            produtos = Produto.query.filter(Produto.estoque_atual > 0).all()
            return render_template('vendas/formulario.html', venda=None, clientes=clientes, produtos=produtos)

        # ✅ Valores negativos no preço são permitidos (para ajustes, perdas, etc.)
        # Não há validação que impeça preços negativos

        cliente_id_raw = request.form.get('cliente_id')
        data_venda_raw = request.form.get('data_venda')
        empresa_faturadora = request.form.get('empresa_faturadora', 'PATY')
        situacao = request.form.get('situacao', 'PENDENTE')
        try:
            cliente_id = int(cliente_id_raw) if cliente_id_raw else None
        except (ValueError, TypeError):
            cliente_id = None
        if not cliente_id:
            msg = 'Cliente é obrigatório.'
            if _is_ajax():
                return jsonify(ok=False, mensagem=msg), 400
            clientes = Cliente.query.all()
            produtos = Produto.query.filter(Produto.estoque_atual > 0).all()
            return render_template('vendas/formulario.html', venda=None, clientes=clientes, produtos=produtos)
        venda = Venda(
            cliente_id=cliente_id,
            produto_id=produto_id,
            nf=request.form.get('nf', ''),
            preco_venda=Decimal(str(_limpar_valor_moeda(request.form.get('preco_venda', 0)))),
            quantidade_venda=quantidade_venda,
            data_venda=date.fromisoformat(data_venda_raw) if data_venda_raw else date.today(),
            empresa_faturadora=empresa_faturadora,
            situacao=situacao
        )
        db.session.add(venda)
        produto.estoque_atual -= quantidade_venda
        db.session.flush()
        # --- INÍCIO DA INTEGRAÇÃO COM CAIXA (PILOTO AUTOMÁTICO V4) ---
        if str(venda.situacao or '').strip().upper() in ('PAGO', 'CONCLUÍDO'):
            lancamentos_existentes = LancamentoCaixa.query.filter(
                LancamentoCaixa.descricao.like(f"Venda #{venda.id} -%")
            ).all()
            if not lancamentos_existentes:
                cliente = Cliente.query.get(venda.cliente_id)
                nome_cliente = cliente.nome_cliente if cliente else "Cliente Avulso"
                forma_pgto = request.form.get('forma_pagamento', 'Dinheiro') or 'Dinheiro'
                valor_venda = float(venda.calcular_total())
                forma_pgto_upper = str(forma_pgto or '').upper()
                data_venc = getattr(venda, 'data_vencimento', None)
                if 'BOLETO' in forma_pgto_upper and data_venc:
                    data_lancamento_caixa = data_venc
                else:
                    data_lancamento_caixa = date.today()
                novo_lanc = LancamentoCaixa(
                    data=data_lancamento_caixa,
                    descricao=f"Venda #{venda.id} - {nome_cliente}",
                    tipo='ENTRADA',
                    categoria='Entrada Cliente',
                    forma_pagamento=forma_pgto,
                    valor=valor_venda,
                    usuario_id=current_user.id
                )
                db.session.add(novo_lanc)
                if 'boleto' in forma_pgto.lower():
                    repasse_lanc = LancamentoCaixa(
                        data=data_lancamento_caixa,
                        descricao=f"Venda #{venda.id} - {nome_cliente} (Repasse Fornecedor)",
                        tipo='SAIDA',
                        categoria='Saída Fornecedor',
                        forma_pagamento=forma_pgto,
                        valor=valor_venda,
                        usuario_id=current_user.id
                    )
                    db.session.add(repasse_lanc)
        # --- FIM DA INTEGRAÇÃO ---
        db.session.commit()
        limpar_cache_dashboard()  # Limpar cache após nova venda

        if _is_ajax():
            return jsonify(
                ok=True,
                mensagem='Venda registrada com sucesso!',
                venda={
                    'id': venda.id,
                    'cliente_nome': venda.cliente.nome_cliente,
                    'produto_nome': venda.produto.nome_produto,
                    'nf': venda.nf or '-',
                    'quantidade_venda': venda.quantidade_venda,
                    'preco_venda': float(venda.preco_venda),
                    'total': float(venda.preco_venda * venda.quantidade_venda),
                    'lucro': float(venda.calcular_lucro()),
                    'data_venda': venda.data_venda.strftime('%d/%m/%Y'),
                    'empresa_faturadora': venda.empresa_faturadora,
                    'situacao': venda.situacao,
                },
            )
        flash('Venda registrada com sucesso!', 'success')
        return redirect(url_for('listar_vendas'))

    clientes = Cliente.query.all()
    produtos = Produto.query.filter(Produto.estoque_atual > 0).all()
    return render_template('vendas/formulario.html', venda=None, clientes=clientes, produtos=produtos)


@app.route('/add_venda', methods=['POST'])
@login_required
def add_venda():
    """Alias para criação de venda via AJAX (listar). Sempre retorna JSON."""
    return nova_venda()


@app.route('/processar_carrinho', methods=['POST'])
@login_required
def processar_carrinho():
    """Processa itens do carrinho em lote: cria Venda e atualiza estoque em uma única transação."""
    data = request.get_json(silent=True) or {}
    itens = data.get('itens', [])
    if not itens:
        return jsonify(ok=False, mensagem='Carrinho vazio. Adicione itens antes de finalizar.'), 400

    try:
        processados = 0
        for obj in itens:
            try:
                cliente_id = int(obj.get('cliente_id'))
                produto_id = int(obj.get('produto_id'))
                quantidade_venda = int(obj.get('quantidade_venda', 0))
                preco_venda = Decimal(str(_limpar_valor_moeda(obj.get('preco_venda', 0))))
                empresa_faturadora = (obj.get('empresa_faturadora') or '').strip() or None
                situacao = (obj.get('situacao') or 'PENDENTE').strip()
                nf = (obj.get('nf') or '').strip() or None
                data_venda_raw = obj.get('data_venda')
                if data_venda_raw:
                    data_venda = date.fromisoformat(data_venda_raw)
                else:
                    data_venda = date.today()
            except (TypeError, ValueError) as e:
                return jsonify(ok=False, mensagem=f'Dados inválidos em um item: {e}'), 400

            if quantidade_venda < 1:
                return jsonify(ok=False, mensagem='Quantidade deve ser maior que zero.'), 400
            if not empresa_faturadora or empresa_faturadora not in ('DESTAK', 'PATY', 'NENHUM'):
                return jsonify(ok=False, mensagem='Empresa faturadora inválida.'), 400

            produto = Produto.query.get(produto_id)
            if not produto:
                return jsonify(ok=False, mensagem=f'Produto ID {produto_id} não encontrado.'), 400
            if produto.estoque_atual < quantidade_venda:
                return jsonify(
                    ok=False,
                    mensagem=f'Estoque insuficiente para "{produto.nome_produto}". Disponível: {produto.estoque_atual}.'
                ), 400

            cliente = Cliente.query.get(cliente_id)
            if not cliente:
                return jsonify(ok=False, mensagem=f'Cliente ID {cliente_id} não encontrado.'), 400

            venda = Venda(
                cliente_id=cliente_id,
                produto_id=produto_id,
                nf=nf,
                preco_venda=preco_venda,
                quantidade_venda=quantidade_venda,
                data_venda=data_venda,
                empresa_faturadora=empresa_faturadora,
                situacao=situacao,
            )
            db.session.add(venda)
            produto.estoque_atual -= quantidade_venda
            processados += 1

        db.session.commit()
        limpar_cache_dashboard()  # Limpar cache após processar carrinho
        return jsonify(ok=True, mensagem=f'{processados} venda(s) registrada(s) com sucesso.', processados=processados)
    except Exception as e:
        db.session.rollback()
        return jsonify(ok=False, mensagem=str(e)), 500


@app.route('/venda/adicionar_item', methods=['POST'])
@login_required
def venda_adicionar_item():
    """Adiciona um novo item (produto) a um pedido/venda existente. Baixa estoque e mantém dados do pedido."""
    venda_id = request.form.get('venda_id')
    produto_id = request.form.get('produto_id')
    quantidade_venda = request.form.get('quantidade_venda')
    preco_venda_raw = request.form.get('preco_venda')

    if not venda_id or not produto_id or not quantidade_venda or not preco_venda_raw:
        flash('Preencha todos os campos obrigatórios.', 'error')
        return redirect(url_for('listar_vendas'))

    try:
        venda_id = int(venda_id)
        produto_id = int(produto_id)
        quantidade_venda = int(quantidade_venda)
    except (ValueError, TypeError):
        flash('Dados inválidos.', 'error')
        return redirect(url_for('listar_vendas'))

    venda_existente = Venda.query.get_or_404(venda_id)
    produto = Produto.query.get_or_404(produto_id)

    preco_venda = _limpar_valor_moeda(preco_venda_raw)
    if preco_venda <= 0:
        flash('Preço unitário inválido.', 'error')
        return redirect(url_for('listar_vendas'))

    if produto.estoque_atual < quantidade_venda:
        flash(f'Estoque insuficiente! Disponível: {produto.estoque_atual}', 'error')
        return redirect(url_for('listar_vendas'))

    nova_venda = Venda(
        cliente_id=venda_existente.cliente_id,
        produto_id=produto_id,
        nf=venda_existente.nf or '',
        preco_venda=Decimal(str(preco_venda)),
        quantidade_venda=quantidade_venda,
        data_venda=venda_existente.data_venda,
        empresa_faturadora=venda_existente.empresa_faturadora,
        situacao=venda_existente.situacao,
    )
    db.session.add(nova_venda)
    produto.estoque_atual -= quantidade_venda
    db.session.commit()
    limpar_cache_dashboard()
    flash('Produto adicionado ao pedido com sucesso!', 'success')
    return redirect(url_for('listar_vendas'))


@app.route('/vendas/editar/<int:id>', methods=['GET', 'POST'])
@login_required
def editar_venda(id):
    venda = Venda.query.get_or_404(id)
    produto_original = venda.produto
    quantidade_original = venda.quantidade_venda
    
    if request.method == 'POST':
        try:
            produto_id = int(request.form.get('produto_id', 0))
            quantidade_venda = int(request.form.get('quantidade_venda', 0))
        except (ValueError, TypeError):
            flash('Produto e quantidade são obrigatórios.', 'error')
            return redirect(url_for('listar_vendas'))
        if not produto_id:
            flash('Produto é obrigatório.', 'error')
            return redirect(url_for('listar_vendas'))
        
        produto = Produto.query.get_or_404(produto_id)
        
        # Calcular estoque disponível considerando a devolução da quantidade original
        if produto.id == produto_original.id:
            # Mesmo produto: estoque disponível = estoque atual + quantidade original (que será devolvida)
            estoque_disponivel = produto.estoque_atual + quantidade_original
        else:
            # Produto diferente: precisa ter estoque suficiente
            estoque_disponivel = produto.estoque_atual
        
        # Validar estoque
        if estoque_disponivel < quantidade_venda:
            flash(f'Estoque insuficiente! Disponível: {estoque_disponivel}', 'error')
            return redirect(url_for('listar_vendas'))
        
        # Atualizar estoque ANTES de atualizar a venda
        if produto.id == produto_original.id:
            # Mesmo produto: devolver quantidade original e subtrair nova quantidade
            produto.estoque_atual = produto.estoque_atual + quantidade_original - quantidade_venda
        else:
            # Produto diferente: devolver ao original e subtrair do novo
            produto_original.estoque_atual += quantidade_original
            produto.estoque_atual -= quantidade_venda
        
        # Atualizar venda
        try:
            cliente_id = int(request.form.get('cliente_id', 0))
        except (ValueError, TypeError):
            cliente_id = venda.cliente_id
        data_venda_raw = request.form.get('data_venda')
        venda.cliente_id = cliente_id if cliente_id else venda.cliente_id
        venda.produto_id = produto_id
        venda.nf = request.form.get('nf', '')
        venda.preco_venda = Decimal(str(_limpar_valor_moeda(request.form.get('preco_venda', 0))))
        venda.quantidade_venda = quantidade_venda
        if data_venda_raw:
            venda.data_venda = date.fromisoformat(data_venda_raw)
        venda.empresa_faturadora = request.form.get('empresa_faturadora', venda.empresa_faturadora or 'PATY')
        venda.situacao = request.form.get('situacao', venda.situacao or 'PENDENTE')
        
        # --- INÍCIO DA INTEGRAÇÃO COM CAIXA (PILOTO AUTOMÁTICO V4) ---
        vendas_do_pedido = _vendas_do_pedido(venda)
        venda_id_busca = vendas_do_pedido[0].id if vendas_do_pedido else venda.id
        lancamentos_existentes = LancamentoCaixa.query.filter(
            LancamentoCaixa.descricao.like(f"Venda #{venda_id_busca} -%")
        ).all()
        status_atual = str(venda.situacao).strip().upper() if venda.situacao else ''
        status_pago = status_atual in ('PAGO', 'CONCLUÍDO', 'PARCIAL')
        if status_pago and not lancamentos_existentes:
            cliente = Cliente.query.get(venda.cliente_id)
            nome_cliente = cliente.nome_cliente if cliente else "Cliente Avulso"
            forma_pgto = request.form.get('forma_pagamento', 'Dinheiro') or 'Dinheiro'
            valor_pedido = sum(float(v.calcular_total()) for v in vendas_do_pedido)
            forma_pgto_upper = str(forma_pgto or '').upper()
            data_venc = None
            for v in vendas_do_pedido:
                dv = getattr(v, 'data_vencimento', None)
                if dv:
                    data_venc = dv
                    break
            if 'BOLETO' in forma_pgto_upper and data_venc:
                data_lancamento_caixa = data_venc
            else:
                data_lancamento_caixa = date.today()
            novo_lanc = LancamentoCaixa(
                data=data_lancamento_caixa,
                descricao=f"Venda #{venda_id_busca} - {nome_cliente}",
                tipo='ENTRADA',
                categoria='Entrada Cliente',
                forma_pagamento=forma_pgto,
                valor=valor_pedido,
                usuario_id=current_user.id
            )
            db.session.add(novo_lanc)
            if 'boleto' in forma_pgto.lower():
                repasse_lanc = LancamentoCaixa(
                    data=data_lancamento_caixa,
                    descricao=f"Venda #{venda_id_busca} - {nome_cliente} (Repasse Fornecedor)",
                    tipo='SAIDA',
                    categoria='Saída Fornecedor',
                    forma_pagamento=forma_pgto,
                    valor=valor_pedido,
                    usuario_id=current_user.id
                )
                db.session.add(repasse_lanc)
        elif not status_pago and lancamentos_existentes:
            for lanc in lancamentos_existentes:
                db.session.delete(lanc)
        # --- FIM DA INTEGRAÇÃO ---
        
        db.session.commit()
        limpar_cache_dashboard()  # Limpar cache após editar venda
        flash('Venda atualizada com sucesso!', 'success')
        return redirect(url_for('listar_vendas'))
    
    clientes = Cliente.query.all()
    produtos = Produto.query.all()
    return render_template('vendas/formulario.html', venda=venda, clientes=clientes, produtos=produtos)


@app.route('/vendas/excluir/<int:id>', methods=['POST'])
@login_required
def excluir_venda(id):
    """Exclui uma venda e todas as outras vendas do mesmo pedido.
    Regra A (CNPJ preenchido): Cliente + NF + Data
    Regra B (CNPJ = '0' ou '00000000000000'): Cliente + Data (ignora NF)
    """
    venda = Venda.query.get_or_404(id)
    
    # Salvar dados necessários ANTES de deletar (evita DetachedInstanceError)
    nome_cliente = venda.cliente.nome_cliente
    cliente_id = venda.cliente_id
    cnpj_cliente = venda.cliente.cnpj or ''
    nf_pedido = venda.nf
    data_pedido = venda.data_venda
    
    # Determinar lógica de agrupamento baseada no CNPJ
    is_consumidor_final = cnpj_cliente in ('0', '00000000000000', '')
    
    # Normalizar NF para comparação (string vazia se None)
    nf_normalizada = str(nf_pedido).strip() if nf_pedido else ''
    
    # Buscar todas as vendas do mesmo pedido
    # Regra: Se CNPJ == '0', agrupar apenas por Cliente + Data (ignora NF)
    #        Se CNPJ != '0', agrupar por Cliente + NF + Data
    if is_consumidor_final:
        # Consumidor final: apenas Cliente + Data (ignora NF completamente)
        query = Venda.query.filter(
            Venda.cliente_id == cliente_id,
            Venda.data_venda == data_pedido
        )
    else:
        # Cliente com CNPJ: Cliente + NF + Data
        query = Venda.query.filter(
            Venda.cliente_id == cliente_id,
            Venda.data_venda == data_pedido
        )
        # Filtrar por NF (tratando None como string vazia)
        if nf_normalizada:
            query = query.filter(Venda.nf == nf_pedido)
        else:
            query = query.filter((Venda.nf == None) | (Venda.nf == ''))
    
    vendas_do_pedido = query.all()
    
    try:
        # Restaurar estoque de todos os produtos do pedido
        logs = []
        for v in vendas_do_pedido:
            produto = v.produto
            quantidade = v.quantidade_venda
            nome_produto = produto.nome_produto  # Salvar antes de deletar
            produto.estoque_atual += quantidade
            logs.append(f"{quantidade} unidades devolvidas ao produto [{nome_produto}]")
            db.session.delete(v)

        db.session.commit()
        limpar_cache_dashboard()  # Limpar cache após excluir venda

        # Log detalhado usando variáveis salvas
        print(f"Pedido excluído (Cliente: {nome_cliente}, NF: {nf_pedido or 'N/A'}, Data: {data_pedido.strftime('%d/%m/%Y')}):")
        for log in logs:
            print(f"  - {log}")

        flash(f'Pedido completo excluído com sucesso! {len(vendas_do_pedido)} item(ns) removido(s).', 'success')
    except Exception as e:
        db.session.rollback()  # OBRIGATÓRIO para destravar o sistema em caso de erro
        print(f"Erro ao deletar venda: {e}")
        flash('Erro ao deletar a venda. Tente novamente.', 'error')
    return redirect(url_for('listar_vendas'))


def _vendas_do_pedido(venda):
    """Retorna todas as vendas do mesmo pedido (Cliente+NF+Data ou Cliente+Data se CNPJ 0)."""
    cliente_id = venda.cliente_id
    cnpj_cliente = venda.cliente.cnpj or ''
    nf_pedido = venda.nf
    data_pedido = venda.data_venda
    is_consumidor_final = cnpj_cliente in ('0', '00000000000000', '')
    nf_normalizada = str(nf_pedido).strip() if nf_pedido else ''
    if is_consumidor_final:
        query = Venda.query.filter(
            Venda.cliente_id == cliente_id,
            Venda.data_venda == data_pedido
        )
    else:
        query = Venda.query.filter(
            Venda.cliente_id == cliente_id,
            Venda.data_venda == data_pedido
        )
        if nf_normalizada:
            query = query.filter(Venda.nf == nf_pedido)
        else:
            query = query.filter((Venda.nf == None) | (Venda.nf == ''))
    return query.all()


@app.route('/venda/atualizar_status/<int:id_venda>', methods=['POST'])
@login_required
def atualizar_status_venda(id_venda):
    """Alterna o status do pedido: PENDENTE ↔ PAGO. Aplica a todos os itens do grupo."""
    venda = Venda.query.get_or_404(id_venda)
    vendas_do_pedido = _vendas_do_pedido(venda)
    atual = vendas_do_pedido[0].situacao if vendas_do_pedido else 'PENDENTE'
    novo = 'PAGO' if atual == 'PENDENTE' else 'PENDENTE'
    for v in vendas_do_pedido:
        v.situacao = novo
    # --- INÍCIO DA INTEGRAÇÃO COM CAIXA (PILOTO AUTOMÁTICO V4) ---
    lancamentos_existentes = LancamentoCaixa.query.filter(
        LancamentoCaixa.descricao.like(f"Venda #{venda.id} -%")
    ).all()
    status_pago = novo and novo.upper() in ('PAGO', 'CONCLUÍDO', 'PARCIAL')
    if status_pago and not lancamentos_existentes:
        cliente = Cliente.query.get(venda.cliente_id)
        nome_cliente = cliente.nome_cliente if cliente else "Cliente Avulso"
        forma_pgto = request.form.get('forma_pagamento') or (request.get_json(silent=True) or {}).get('forma_pagamento', 'Dinheiro') or 'Dinheiro'
        valor_pedido = sum(float(v.calcular_total()) for v in vendas_do_pedido)
        forma_pgto_upper = str(forma_pgto or '').upper()
        data_venc = None
        for v in vendas_do_pedido:
            dv = getattr(v, 'data_vencimento', None)
            if dv:
                data_venc = dv
                break
        if 'BOLETO' in forma_pgto_upper and data_venc:
            data_lancamento_caixa = data_venc
        else:
            data_lancamento_caixa = date.today()
        novo_lancamento = LancamentoCaixa(
            data=data_lancamento_caixa,
            descricao=f"Venda #{venda.id} - {nome_cliente}",
            tipo='ENTRADA',
            categoria='Entrada Cliente',
            forma_pagamento=forma_pgto,
            valor=valor_pedido,
            usuario_id=current_user.id
        )
        db.session.add(novo_lancamento)
        if 'boleto' in forma_pgto.lower():
            repasse_lanc = LancamentoCaixa(
                data=data_lancamento_caixa,
                descricao=f"Venda #{venda.id} - {nome_cliente} (Repasse Fornecedor)",
                tipo='SAIDA',
                categoria='Saída Fornecedor',
                forma_pagamento=forma_pgto,
                valor=valor_pedido,
                usuario_id=current_user.id
            )
            db.session.add(repasse_lanc)
    elif not status_pago and lancamentos_existentes:
        for lanc in lancamentos_existentes:
            db.session.delete(lanc)
    # --- FIM DA INTEGRAÇÃO ---
    db.session.commit()
    limpar_cache_dashboard()  # Limpar cache após atualizar status da venda
    nf = venda.nf or '-'
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify(ok=True, novo_status=novo, mensagem=f'Pedido (NF: {nf}) atualizado para {novo}.')
    flash(f'Pedido (NF: {nf}) atualizado para {novo}.', 'success')
    return redirect(url_for('listar_vendas'))


@app.route('/venda/recibo/<int:id>')
@login_required
def recibo_venda(id):
    """Gera recibo de venda em formato de impressão (uma página A4). Agrupa itens da mesma compra (cliente, data, NF)."""
    venda_base = Venda.query.options(joinedload(Venda.cliente)).get_or_404(id)
    cliente = venda_base.cliente

    # Busca todas as linhas que pertencem a esta mesma "compra" (mesmo cliente, data e NF)
    vendas_agrupadas = Venda.query.filter_by(
        cliente_id=venda_base.cliente_id,
        data_venda=venda_base.data_venda,
        nf=venda_base.nf
    ).options(joinedload(Venda.produto)).order_by(Venda.id).all()

    total_recibo = sum(float(v.calcular_total()) for v in vendas_agrupadas)
    data_emissao = date.today()

    return render_template('vendas/recibo.html',
                          cliente=cliente,
                          venda_base=venda_base,
                          vendas=vendas_agrupadas,
                          total_recibo=total_recibo,
                          data_emissao=data_emissao)


# ============================================================================
# ROTAS DE DOCUMENTOS (BOLETOS E NOTAS FISCAIS)
# ============================================================================

@app.route('/documento/visualizar/<int:id>')
@login_required
def visualizar_documento(id):
    """Redireciona para o PDF na nuvem (Cloudinary)."""
    documento = Documento.query.get_or_404(id)
    if documento.url_arquivo:
        return redirect(documento.url_arquivo)
    flash('Link do arquivo não encontrado na nuvem. Faça o upload novamente.', 'error')
    return redirect(request.referrer or url_for('dashboard'))


@app.route('/venda/<int:id>/ver_boleto')
@login_required
def ver_boleto_venda(id):
    """Abre o PDF do boleto vinculado ao pedido em nova aba. Prioriza conteudo_binario do banco se arquivo físico não existir."""
    venda = Venda.query.get_or_404(id)
    path = (venda.caminho_boleto or '').strip()
    if not path:
        flash('Boleto não vinculado a este pedido.', 'error')
        return redirect(url_for('listar_vendas'))
    doc = Documento.query.filter(or_(Documento.caminho_arquivo == path, Documento.url_arquivo == path)).first()
    if doc and doc.url_arquivo:
        return redirect(doc.url_arquivo)
    full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if os.path.exists(full):
        return send_file(full, mimetype='application/pdf')
    flash('Arquivo do boleto não encontrado no servidor.', 'error')
    return redirect(request.referrer or url_for('listar_vendas'))


@app.route('/venda/<int:id>/ver_nf')
@login_required
def ver_nf_venda(id):
    """Abre o PDF da nota fiscal vinculada ao pedido em nova aba."""
    venda = Venda.query.get_or_404(id)
    path = (venda.caminho_nf or '').strip()
    if not path:
        flash('Nota fiscal não vinculada a este pedido.', 'error')
        return redirect(url_for('listar_vendas'))
    
    doc = Documento.query.filter(or_(Documento.caminho_arquivo == path, Documento.url_arquivo == path)).first()
    if not doc:
        venda.caminho_nf = None
        db.session.commit()
        flash('Nota fiscal não encontrada no banco de dados. Vínculo removido.', 'error')
        return redirect(url_for('listar_vendas'))
    if doc.url_arquivo:
        return redirect(doc.url_arquivo)
    full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if os.path.exists(full):
        return send_file(full, mimetype='application/pdf')
    flash('Arquivo da nota fiscal não encontrado no servidor.', 'error')
    return redirect(request.referrer or url_for('listar_vendas'))


@app.route('/upload', methods=['POST'])
def upload_documento():
    """
    Rota para o bot enviar arquivos. Salva na sala de espera (documentos_entrada).
    O upload para Cloudinary e criação do Documento ocorrem em _processar_documentos_pendentes (Organizar).
    Campo tipo: 'boleto' -> boletos ; 'nfe' -> notas_fiscais
    """
    arquivo = request.files.get('file') or request.files.get('arquivo') or request.files.get('documento')
    if not arquivo or not arquivo.filename:
        return jsonify({'mensagem': 'Nenhum arquivo enviado.'}), 400

    tipo = (request.form.get('tipo') or request.form.get('type') or '').strip().lower()
    if tipo == 'boleto':
        subpasta = 'boletos'
    elif tipo == 'nfe':
        subpasta = 'notas_fiscais'
    else:
        return jsonify({'mensagem': "Campo 'tipo' inválido. Use 'boleto' ou 'nfe'."}), 400

    try:
        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'documentos_entrada')
        caminho_final = os.path.join(base_dir, subpasta)
        os.makedirs(caminho_final, exist_ok=True)
        nome_arquivo = secure_filename(arquivo.filename)
        caminho_completo = os.path.join(caminho_final, nome_arquivo)
        arquivo.save(caminho_completo)
        return jsonify({'mensagem': 'Sucesso'}), 200
    except Exception as e:
        print(f"Erro ao guardar ficheiro em documentos_entrada/{subpasta}: {e}")
        return jsonify({'mensagem': str(e)}), 500


@app.route('/api/receber_automatico', methods=['POST'])
def api_receber_automatico():
    """API para receber arquivos automaticamente. Requer token em Authorization."""
    token_esperado = 'SEGREDDO_DO_ALHO_2026'
    auth = request.headers.get('Authorization', '')
    if auth != token_esperado and auth != f'Bearer {token_esperado}':
        return jsonify({'status': 'erro', 'mensagem': 'Token inválido ou ausente.'}), 403
    if 'file' not in request.files:
        return jsonify({'status': 'erro', 'mensagem': 'Nenhum arquivo enviado.'}), 400
    arquivo = request.files['file']
    if not arquivo or not arquivo.filename:
        return jsonify({'status': 'erro', 'mensagem': 'Arquivo vazio ou inexistente.'}), 400
    filename = secure_filename(arquivo.filename)
    pasta = app.config['UPLOAD_FOLDER']
    os.makedirs(pasta, exist_ok=True)
    caminho = os.path.join(pasta, filename)
    arquivo.save(caminho)

    try:
        user_id = None
        if current_user.is_authenticated:
            user_id = current_user.id
        else:
            # Fallback para o robô: pega o primeiro usuário (Admin)
            primeiro_user = Usuario.query.first()
            if primeiro_user:
                user_id = primeiro_user.id

        def processar_background(app, filepath, uid):
            try:
                with app.app_context():
                    try:
                        if uid:
                            usuario = Usuario.query.get(uid)
                            if usuario:
                                login_user(usuario)
                        _processar_documento(filepath, user_id_forcado=uid)
                        print(f"[receber_automatico] Processamento concluído: {filepath}")
                    except Exception as e:
                        db.session.rollback()
                        print(f"[receber_automatico] ERRO ao processar {filepath}: {type(e).__name__}: {e}")
                        traceback.print_exc()
                    finally:
                        db.session.remove()
            except Exception as e:
                db.session.rollback()
                print(f'Erro no processamento: {e}')
                print(f"[receber_automatico] ERRO ao processar {filepath}: {type(e).__name__}: {e}")
                traceback.print_exc()

        thread = threading.Thread(target=processar_background, args=(current_app._get_current_object(), caminho, user_id))
        thread.start()
        return jsonify({'message': 'Processamento iniciado em segundo plano'}), 202
    except Exception as e:
        db.session.rollback()
        return jsonify({'status': 'erro', 'mensagem': str(e)}), 500


@app.route('/processar_documentos', methods=['POST'])
@login_required
def processar_documentos():
    """Rota para processar documentos manualmente (opcional, via AJAX)."""
    resultado = _processar_documentos_pendentes()
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True, **resultado)
    flash(f"Processados {resultado['processados']} documento(s).", 'success')
    if resultado['erros'] > 0:
        flash(f"Erros: {resultado['erros']}.", 'error')
    return redirect(url_for('dashboard'))


@app.route('/reprocessar_boletos', methods=['POST'])
@login_required
def reprocessar_boletos():
    """Re-lê os PDFs em documentos_entrada/boletos e atualiza numero_nf (e demais campos) nos Documentos."""
    r = _reprocessar_boletos_atualizar_extracao()
    flash(f"Boletos reprocessados: {r['atualizados']} atualizado(s).", 'success')
    if r['erros'] > 0:
        flash(f"Erros ao reprocessar: {r['erros']}.", 'error')
    return redirect(url_for('dashboard'))


@app.route('/admin/arquivos')
@login_required
def admin_arquivos():
    """Lista todos os documentos salvos no banco, ordenados pelos mais recentes."""
    if not current_user.is_admin():
        flash('Acesso negado. Apenas administradores podem gerenciar arquivos.', 'error')
        return redirect(url_for('dashboard'))
    documentos = Documento.query.order_by(Documento.data_processamento.desc()).all()
    return render_template('gerenciar_arquivos.html', documentos=documentos)


@app.route('/admin/arquivos/deletar_massa', methods=['POST'])
@login_required
def admin_arquivos_deletar_massa():
    """Exclusão em massa de documentos. Recebe lista de IDs via form ou JSON."""
    if not current_user.is_admin():
        flash('Acesso negado. Apenas administradores podem gerenciar arquivos.', 'error')
        return redirect(url_for('dashboard'))
    ids_raw = request.form.getlist('ids[]') or request.form.getlist('ids') or (request.get_json(silent=True) or {}).get('ids', [])
    if not ids_raw:
        flash('Nenhum arquivo selecionado.', 'warning')
        return redirect(url_for('admin_arquivos'))
    try:
        ids = list({int(x) for x in ids_raw if x is not None and str(x).strip()})
    except (TypeError, ValueError):
        flash('IDs inválidos.', 'error')
        return redirect(url_for('admin_arquivos'))
    if not ids:
        flash('Nenhum arquivo selecionado.', 'warning')
        return redirect(url_for('admin_arquivos'))
    try:
        docs = Documento.query.filter(Documento.id.in_(ids)).all()
        for d in docs:
            if d.public_id and (os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL')):
                try:
                    cloudinary.uploader.destroy(d.public_id, resource_type='raw')
                except Exception as ex:
                    print(f"Erro ao excluir do Cloudinary {d.public_id}: {ex}")
            db.session.delete(d)
        db.session.commit()
        flash(f'{len(docs)} documento(s) excluído(s) com sucesso!', 'success')
    except Exception as e:
        db.session.rollback()
        print(f"Erro ao deletar documentos em massa: {e}")
        flash('Erro ao excluir documentos. Tente novamente.', 'error')
    return redirect(url_for('admin_arquivos'))


@app.route('/admin/reprocessar-vencimentos', methods=['GET', 'POST'])
@login_required
def admin_reprocessar_vencimentos():
    """Reprocessa todos os PDFs de boletos vinculados às vendas para extrair/atualizar data_vencimento.
    GET: exibe página de confirmação com preview
    POST: executa o reprocessamento
    """
    if not current_user.is_admin():
        flash('Acesso restrito a administradores.', 'error')
        return redirect(url_for('dashboard'))
    
    if request.method == 'GET':
        # Preview: quantas vendas com boleto existem
        total_com_boleto = Venda.query.filter(Venda.caminho_boleto.isnot(None)).count()
        total_sem_vencimento = Venda.query.filter(
            Venda.caminho_boleto.isnot(None),
            Venda.data_vencimento.is_(None)
        ).count()
        return f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reprocessar Vencimentos</title>
    <script src="https://cdn.tailwindcss.com"></script>
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
    
    # POST: executar reprocessamento
    resultado = _reprocessar_vencimentos_vendas()
    
    return f'''<!DOCTYPE html>
<html lang="pt-BR">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Resultado do Reprocessamento</title>
    <script src="https://cdn.tailwindcss.com"></script>
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


@app.route('/organizar_e_vincular', methods=['POST'])
@login_required
def organizar_e_vincular():
    """Terceiriza o processamento para o robô de background."""
    if fila_tarefas:
        fila_tarefas.enqueue(background_organizar_tudo, current_user.id)
        flash("⏳ O robô começou a ler os PDFs nos bastidores! Pode continuar navegando, os links aparecerão em breve.", 'info')
    else:
        # Fallback de segurança se o Redis falhar
        background_organizar_tudo(current_user.id)
        flash("Sucesso! Documentos processados.", 'success')
    return redirect(url_for('dashboard'))


@app.route('/documento/<int:id>/vincular', methods=['POST'])
@login_required
def vincular_documento_venda(id):
    """Associa o documento a um pedido (venda). Espera venda_id (primeira_venda_id do pedido)."""
    documento = Documento.query.get_or_404(id)
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
    venda = Venda.query.get(venda_id)
    if not venda:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, mensagem='Pedido não encontrado.'), 404
        flash('Pedido não encontrado.', 'error')
        return redirect(url_for('dashboard'))
    documento.venda_id = venda_id
    path = documento.caminho_arquivo
    vendas_pedido = _vendas_do_pedido(venda)
    is_boleto = (documento.tipo or '').upper() == 'BOLETO'
    
    # Se for boleto, extrair/atualizar data de vencimento
    data_venc_boleto = None
    if is_boleto and documento.data_vencimento:
        data_venc_boleto = documento.data_vencimento
    elif is_boleto:
        # Re-extrair data do PDF se não tiver no documento
        path_full = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
        if os.path.isfile(path_full):
            dados_pdf = _processar_pdf(path_full, 'BOLETO')
            if dados_pdf and dados_pdf.get('data_vencimento'):
                data_venc_boleto = dados_pdf['data_vencimento']
                documento.data_vencimento = data_venc_boleto  # Atualizar também o documento
    
    for vv in vendas_pedido:
        if is_boleto:
            vv.caminho_boleto = path
            if data_venc_boleto:
                vv.data_vencimento = data_venc_boleto
        else:
            vv.caminho_nf = path
    db.session.commit()
    c = venda.cliente
    rs = (c.razao_social or '').strip()
    label_cliente = f"{c.nome_cliente} ({rs})" if rs else c.nome_cliente
    tipo_doc = (documento.tipo or '').upper()
    if tipo_doc == 'BOLETO':
        msg = f'Boleto vinculado ao cliente: {label_cliente}.'
    else:
        msg = f'Documento vinculado ao pedido (Cliente: {label_cliente}, NF: {venda.nf or "-"}).'
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True, mensagem=msg)
    flash(msg, 'success')
    return redirect(url_for('dashboard'))


@app.route('/api/pedidos')
@login_required
def api_pedidos():
    """Lista pedidos recentes para o modal Vincular à Venda. Retorna {id, label} por pedido."""
    vendas = Venda.query.order_by(Venda.id.desc()).limit(200).all()
    seen = set()
    pedidos = []
    for v in vendas:
        cnpj = (v.cliente.cnpj or '').strip()
        is_cf = cnpj in ('0', '00000000000000', '')
        d = v.data_venda.date() if hasattr(v.data_venda, 'date') else v.data_venda
        key = (v.cliente_id, d) if is_cf else (v.cliente_id, (v.nf or '').strip(), d)
        if key in seen:
            continue
        seen.add(key)
        label = f"{v.cliente.nome_cliente} | NF {v.nf or '-'} | {d.strftime('%d/%m/%Y')}"
        pedidos.append({'id': v.id, 'label': label})
    return jsonify(pedidos=pedidos)


@app.route('/vendas/deletar_massa', methods=['POST'])
@login_required
def vendas_deletar_massa():
    """Exclusão em massa de vendas. Recebe JSON { ids: [1,2,...] }. Deleta em uma transação e restaura estoque."""
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'ok': False, 'mensagem': 'Nenhum ID informado.'}), 400
    try:
        ids = list({int(x) for x in ids if x is not None})
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'mensagem': 'IDs inválidos.'}), 400
    if not ids:
        return jsonify({'ok': False, 'mensagem': 'Nenhum ID informado.'}), 400
    vendas = Venda.query.filter(Venda.id.in_(ids)).all()
    if not vendas:
        return jsonify({'ok': False, 'mensagem': 'Nenhum registro encontrado.'}), 404
    if len(vendas) != len(ids):
        return jsonify({'ok': False, 'mensagem': 'Alguns IDs não existem. Nenhuma exclusão realizada.'}), 400
    logs = []
    for v in vendas:
        produto = v.produto
        qty = v.quantidade_venda
        nome = produto.nome_produto
        produto.estoque_atual += qty
        logs.append(f"Venda {v.id}: {qty} un. devolvidas ao produto [{nome}].")
        db.session.delete(v)
    db.session.commit()
    limpar_cache_dashboard()  # Limpar cache após exclusão em massa de vendas
    for msg in logs:
        print(msg)
    return jsonify({'ok': True, 'mensagem': f'{len(vendas)} registro(s) excluído(s). Estoque restaurado.', 'excluidos': len(vendas)})


@app.route('/bulk_delete_vendas', methods=['POST'])
@login_required
def bulk_delete_vendas():
    data = request.get_json(silent=True) or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'ok': False, 'mensagem': 'Nenhum ID informado.'}), 400
    try:
        logs = []
        for id_ in ids:
            venda = Venda.query.get(id_)
            if venda:
                produto = venda.produto
                quantidade_venda = venda.quantidade_venda
                produto.estoque_atual += quantidade_venda
                logs.append(f"Venda deletada: {quantidade_venda} unidades devolvidas ao produto [{produto.nome_produto}].")
                db.session.delete(venda)
        db.session.commit()
        limpar_cache_dashboard()  # Limpar cache após exclusão em massa de vendas
        for msg in logs:
            print(msg)
        return jsonify({'ok': True, 'mensagem': f'{len(ids)} venda(s) excluída(s) com sucesso. Estoque restaurado.', 'excluidos': len(ids)})
    except Exception as e:
        db.session.rollback()
        return jsonify({'ok': False, 'mensagem': str(e)}), 500


# Mapeamento posicional para importação de vendas em formato TSV/raw (sem cabeçalho). Index 4 = Valor Total (ignorado).
_VENDAS_RAW_IMPORT_MAP = [
    ('cliente', 0),
    ('nf', 1),
    ('preco_venda', 2),
    ('quantidade', 3),
    None,  # Index 4: Valor Total (ignorar)
    ('produto', 5),
    ('data_venda', 6),
    ('empresa', 7),
    ('situacao', 8),
]


def _parse_nf_vendas(raw):
    """Converte valor de NF para string armazenável. S/N, Falta_nota, vazio ou não numérico → '0'."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return '0'
    s = str(raw).strip().upper()
    if not s or s in ('S/N', 'FALTA_NOTA', 'FALTA NOTA'):
        return '0'
    # Se for numérico (apenas dígitos), retorna como está (ex: "12267")
    only_digits = re.sub(r'[\s.,]', '', s)
    if only_digits.isdigit():
        return only_digits.lstrip('0') or '0'
    return '0'


def _normalizar_situacao_vendas(s):
    """Correção automática: PENDETE -> PENDENTE."""
    if not s or (isinstance(s, float) and pd.isna(s)):
        return ''
    u = str(s).strip().upper()
    if u == 'PENDETE':
        return 'PENDENTE'
    return u


def _load_csv_vendas_flexible(filepath):
    """Carrega CSV/TSV de importação de vendas com detecção de formato.
    - Se a primeira linha contiver TAB, usa TSV (tab). Caso contrário, usa vírgula.
    - Se a primeira linha contiver 'R$', assume formato raw (sem cabeçalho) e mapeamento posicional.
    Retorna (df, is_raw). Em modo raw, df já tem colunas canônicas e NF/situação normalizados."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
    except Exception:
        return None, False
    lines = [ln.strip() for ln in content.strip().splitlines() if ln.strip()]
    if not lines:
        return None, False
    first_line = lines[0]
    sep = '\t' if '\t' in first_line else ','
    is_raw = 'R$' in first_line
    if is_raw:
        rows = []
        reader = csv.reader(io.StringIO(content), delimiter=sep, quotechar='"')
        for row in reader:
            if not row:
                continue
            d = {}
            for entry in _VENDAS_RAW_IMPORT_MAP:
                if entry is None:
                    continue
                key, idx = entry
                raw_val = row[idx] if idx < len(row) else ''
                if key == 'nf':
                    d[key] = _parse_nf_vendas(raw_val)
                elif key == 'situacao':
                    d[key] = _normalizar_situacao_vendas(raw_val)
                else:
                    d[key] = raw_val
            rows.append(d)
        df = pd.DataFrame(rows)
        return df, True
    df = pd.read_csv(io.StringIO(content), sep=sep, engine='python', quoting=csv.QUOTE_MINIMAL, on_bad_lines='warn')
    return df, False


@app.route('/vendas/importar', methods=['GET', 'POST'])
@login_required
@admin_required
def importar_vendas():
    if request.method == 'POST':
        if 'arquivo' not in request.files:
            return render_template('vendas/importar.html', erros_detalhados=['Nenhum arquivo selecionado. Escolha um arquivo e tente novamente.'], sucesso=0, erros=1)
        arquivo = request.files['arquivo']
        if arquivo.filename == '':
            return render_template('vendas/importar.html', erros_detalhados=['Nenhum arquivo selecionado. Escolha um arquivo e tente novamente.'], sucesso=0, erros=1)
        filepath = None
        try:
            # #region agent log
            _debug_log("app.py:importar_vendas", "import start", {"route": "importar_vendas"}, "H1")
            # #endregion
            filename = secure_filename(arquivo.filename)
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            arquivo.save(filepath)
            is_raw = False
            if filename.endswith('.csv'):
                df, is_raw = _load_csv_vendas_flexible(filepath)
                if df is None:
                    return render_template('vendas/importar.html', erros_detalhados=['O arquivo CSV/TSV está vazio ou não pôde ser lido.'], sucesso=0, erros=1)
            else:
                df = pd.read_excel(filepath)
            if not is_raw:
                df.columns = df.columns.str.strip().str.lower().str.replace(' ', '_')
            vendas_novas = 0
            vendas_ignoradas = 0
            erros = 0
            erros_detalhados = []
            # Mapa nome normalizado -> Produto para busca tolerante a espaços (evita rejeitar por espaços duplos/invisíveis)
            _produtos_por_nome_normalizado = {_normalizar_nome_busca(p.nome_produto): p for p in Produto.query.all()}
            first_iter = True
            for idx, row in df.iterrows():
                # #region agent log
                if first_iter:
                    _debug_log("app.py:importar_vendas", "first row idx", {"type_idx": type(idx).__name__, "idx_repr": repr(idx)}, "H1")
                    first_iter = False
                # #endregion
                linha_num = (idx + 1) if is_raw else (idx + 2)
                nome_cliente = _strip_quotes(row.get('cliente', row.get('nome_cliente', '')))
                nome_produto = _strip_quotes(row.get('produto', row.get('nome_produto', '')))
                contexto = f"{nome_cliente or '?'} / {nome_produto or '?'}"[:50]
                try:
                    cnpj_cliente = _strip_quotes(row.get('cnpj', '')) or None
                    cliente = None
                    if cnpj_cliente:
                        cliente = Cliente.query.filter_by(cnpj=cnpj_cliente).first()
                    if not cliente and nome_cliente:
                        cliente = Cliente.query.filter(func.lower(Cliente.nome_cliente) == nome_cliente.lower()).first()
                    if not cliente:
                        erros_detalhados.append(_msg_linha(linha_num, nome_cliente or 'vazio', "O cliente não foi encontrado. Verifique se está cadastrado com esse nome exato (ou use o CNPJ)", True))
                        erros += 1
                        continue
                    if not nome_produto:
                        erros_detalhados.append(_msg_linha(linha_num, contexto, "O campo 'produto' (ou 'nome_produto') está vazio", True))
                        erros += 1
                        continue
                    nome_produto_clean = _normalizar_nome_busca(nome_produto)
                    produto = _produtos_por_nome_normalizado.get(nome_produto_clean)
                    if not produto:
                        erros_detalhados.append(_msg_linha(linha_num, nome_produto, "O produto não foi encontrado. Verifique se está cadastrado (o nome é comparado ignorando espaços extras e maiúsculas/minúsculas)", True))
                        erros += 1
                        continue
                    qtd_raw = row.get('quantidade', row.get('quantidade_venda', row.get('qtd', 0)))
                    quantidade_venda = _parse_quantidade(qtd_raw)
                    if quantidade_venda is None or quantidade_venda <= 0:
                        erros_detalhados.append(_msg_linha(linha_num, contexto, f"A quantidade está vazia ou inválida ({qtd_raw}). Use um número inteiro (ex: 5)", True))
                        erros += 1
                        continue
                    if produto.estoque_atual < quantidade_venda:
                        erros_detalhados.append(_msg_linha(linha_num, nome_produto, f"Estoque insuficiente. Disponível: {produto.estoque_atual} unidades, solicitado: {quantidade_venda}. Ajuste a quantidade ou o estoque", True))
                        erros += 1
                        continue
                    preco_raw = row.get('preco_venda', row.get('preco', 0))
                    preco_venda = _parse_preco(preco_raw)
                    if preco_venda is None:
                        txt = f"O preço '{preco_raw}' não pôde ser convertido. Use formato brasileiro (ex: 143,00 ou -120,00 para perdas) ou use ponto como decimal" if preco_raw and str(preco_raw).strip() else "O campo 'preco_venda' (ou 'preco') está vazio"
                        erros_detalhados.append(_msg_linha(linha_num, contexto, txt, True))
                        erros += 1
                        continue
                    # Regra de negócio: valores negativos (perdas) são registrados como R$ 0,00
                    if preco_venda < 0:
                        preco_venda = 0.0
                    data_raw = row.get('data_venda', row.get('data', ''))
                    data_venda, raw_used = _parse_data_flex(data_raw)
                    if raw_used and raw_used.strip() and data_venda is None:
                        erros_detalhados.append(_msg_linha(linha_num, contexto, f"O formato da data '{raw_used}' é inválido. Use dd/mm/aaaa ou dd/mm/yy (ex: 01/01/2026 ou 01/01/26)", True))
                        erros += 1
                        continue
                    if data_venda is None:
                        data_venda = date.today()
                    nf_raw = _strip_quotes(row.get('nf', row.get('nota_fiscal', '')))
                    nf_val = (nf_raw or '').strip()
                    nf_sn_zero = (
                        nf_val.upper() in ('S/N', '0', '0.0') or nf_val == '' or
                        (nf_val.replace('.', '').replace(',', '').strip() == '0')
                    )
                    base_dup = Venda.query.filter(
                        Venda.cliente_id == cliente.id,
                        Venda.produto_id == produto.id,
                        Venda.data_venda == data_venda
                    )
                    if nf_sn_zero:
                        base_dup = base_dup.filter(
                            or_(
                                Venda.nf.is_(None),
                                Venda.nf == '',
                                Venda.nf == '0',
                                Venda.nf == '0.0',
                                func.lower(Venda.nf) == 's/n'
                            )
                        )
                        base_dup = base_dup.filter(
                            Venda.preco_venda == Decimal(str(preco_venda)),
                            Venda.quantidade_venda == quantidade_venda
                        )
                    else:
                        base_dup = base_dup.filter(
                            Venda.nf == nf_val,
                            Venda.preco_venda == Decimal(str(preco_venda)),
                            Venda.quantidade_venda == quantidade_venda
                        )
                    if base_dup.first():
                        vendas_ignoradas += 1
                        continue
                    # Ler empresa da coluna 'empresa' ou 'empresa_faturadora' (aceita NENHUM para vendas sem NF)
                    empresa_raw = row.get('empresa', row.get('empresa_faturadora', ''))
                    empresa_val = _strip_quotes(empresa_raw).upper().strip() if empresa_raw else ''
                    if empresa_val not in ('PATY', 'DESTAK', 'NENHUM'):
                        empresa_val = 'DESTAK'  # Fallback se valor inválido ou vazio
                    
                    # Captura a situação (aceita situacao, situação, status)
                    situacao_crua = str(row.get('situacao', row.get('situação', row.get('status', 'PENDENTE')))).strip().upper()
                    situacao_crua = _strip_quotes(situacao_crua) if situacao_crua else ''
                    # Filtro inteligente: reconhece PAGO ou PENDENTE mesmo com erros de digitação (ex: PENDETE)
                    if 'PAGO' in situacao_crua:
                        situacao_val = 'PAGO'
                    elif 'PEND' in situacao_crua:
                        situacao_val = 'PENDENTE'
                    else:
                        situacao_val = 'PENDENTE'  # Padrão de segurança
                    
                    venda = Venda(
                        cliente_id=cliente.id,
                        produto_id=produto.id,
                        nf=nf_val if nf_val else None,
                        preco_venda=Decimal(str(preco_venda)),
                        quantidade_venda=quantidade_venda,
                        data_venda=data_venda,
                        empresa_faturadora=empresa_val,
                        situacao=situacao_val
                    )
                    db.session.add(venda)
                    produto.estoque_atual -= quantidade_venda
                    db.session.commit()
                    vendas_novas += 1
                except Exception as e:
                    db.session.rollback()
                    erros_detalhados.append(_msg_linha(linha_num, contexto, f"Erro inesperado: {str(e)}", True))
                    erros += 1
            if filepath and os.path.exists(filepath):
                os.remove(filepath)
            if erros > 0:
                return render_template('vendas/importar.html', erros_detalhados=erros_detalhados, sucesso=vendas_novas, erros=erros, ignorados=vendas_ignoradas)
            if vendas_novas > 0 or vendas_ignoradas > 0:
                mensagem = f'🎉 Tudo pronto! Salvamos {vendas_novas} vendas novas no sistema.'
                if vendas_ignoradas > 0:
                    mensagem += f' Ah, e encontramos {vendas_ignoradas} vendas que já estavam cadastradas e pulamos elas para não duplicar nada! 😉'
                flash(mensagem, 'success')
            else:
                flash('A planilha estava vazia ou não encontramos dados válidos.', 'warning')
            return redirect(url_for('listar_vendas'))
        except Exception as e:
            db.session.rollback()
            # #region agent log
            _debug_log("app.py:importar_vendas", "outer except", {"route": "importar_vendas", "exc_type": type(e).__name__, "exc_msg": str(e), "tb": traceback.format_exc()}, "H1")
            # #endregion
            if filepath and os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            return render_template('vendas/importar.html', erros_detalhados=[f'Erro ao processar o arquivo: {str(e)}'], sucesso=0, erros=1)
    return render_template('vendas/importar.html')


# API para obter informações do produto (usado no frontend)
@app.route('/api/produto/<int:id>')
@login_required
def api_produto(id):
    produto = Produto.query.get_or_404(id)
    return jsonify({
        'nome': produto.nome_produto,
        'estoque': produto.estoque_atual,
        'preco_custo': float(produto.preco_custo)
    })


@app.route('/admin/raio_x', methods=['GET'])
@login_required
def raio_x():
    """Diagnóstico: últimos 5 documentos cadastrados e ID do usuário atual."""
    if not current_user.is_admin():
        return '<html><body><p>Acesso negado.</p></body></html>', 403
    docs = Documento.query.order_by(Documento.id.desc()).limit(5).all()
    html = '''<!DOCTYPE html>
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
        html += f'<tr><td>{d.id}</td><td>{nome}</td><td>{usuario_id_str}</td><td>{status}</td><td>{data_str}</td></tr>'
    html += '''</table>
</body></html>'''
    return html


@app.route('/admin/resgatar_orfaos', methods=['GET', 'POST'])
@login_required
def resgatar_orfaos():
    """Atribui ao usuário atual todos os documentos com usuario_id NULL (órfãos)."""
    if not current_user.is_admin():
        flash('Acesso negado. Apenas administradores podem executar esta ação.', 'error')
        return redirect(url_for('dashboard'))
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


@app.route('/admin/forcar_leitura_pasta', methods=['GET', 'POST'])
@login_required
def forcar_leitura_pasta():
    """Rota de emergência: lê PDFs em boletos e notas_fiscais, cria registros Documento para os que não existem no banco."""
    if not current_user.is_admin():
        return jsonify({'erro': 'Acesso negado. Apenas administradores.'}), 403
    db.session.rollback()
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'documentos_entrada')
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
                caminho_relativo = os.path.join('documentos_entrada', 'boletos' if tipo == 'BOLETO' else 'notas_fiscais', nome).replace(os.sep, '/')
                doc_existente = Documento.query.filter_by(caminho_arquivo=caminho_relativo).first()
                if not doc_existente:
                    caminho_full = os.path.join(base_dir, 'boletos' if tipo == 'BOLETO' else 'notas_fiscais', nome)
                    url_arquivo = None
                    public_id = None
                    if os.environ.get('CLOUDINARY_URL') or app.config.get('CLOUDINARY_URL'):
                        try:
                            resultado_nuvem = cloudinary.uploader.upload(caminho_full, resource_type='raw')
                            url_arquivo = resultado_nuvem.get('secure_url')
                            public_id = resultado_nuvem.get('public_id')
                        except Exception as ex:
                            print(f"Erro Cloudinary (forcar_leitura): {ex}")
                    doc = Documento(
                        caminho_arquivo=caminho_relativo,
                        url_arquivo=url_arquivo,
                        public_id=public_id,
                        tipo=tipo,
                        usuario_id=current_user.id,
                        data_processamento=date.today()
                    )
                    db.session.add(doc)
                    ressuscitados += 1
        db.session.commit()
        # Remove PDFs locais após upload para Cloudinary (mantém apenas na nuvem)
        for tipo, pasta in pastas.items():
            if not os.path.exists(pasta):
                continue
            for nome in os.listdir(pasta):
                if not nome.lower().endswith('.pdf'):
                    continue
                caminho_full = os.path.join(pasta, nome)
                doc = Documento.query.filter_by(caminho_arquivo=os.path.join('documentos_entrada', 'boletos' if tipo == 'BOLETO' else 'notas_fiscais', nome).replace(os.sep, '/')).first()
                if doc and doc.url_arquivo and os.path.exists(caminho_full):
                    try:
                        os.remove(caminho_full)
                    except Exception as rm_err:
                        print(f"Aviso: não foi possível remover {caminho_full}: {rm_err}")
    except Exception as e:
        db.session.rollback()
        return jsonify({'erro': str(e), 'ressuscitados': 0}), 500
    return jsonify({'ressuscitados': ressuscitados, 'mensagem': f'{ressuscitados} arquivo(s) ressuscitado(s) e inserido(s) no banco.'})


@app.route('/admin/limpar_fantasmas', methods=['GET', 'POST'])
@login_required
def limpar_fantasmas():
    """Remove da tabela Documento os registros cujo arquivo físico não existe mais."""
    if not current_user.is_admin():
        return jsonify({'erro': 'Acesso negado. Apenas administradores.'}), 403
    db.session.rollback()
    base_path = os.path.dirname(os.path.abspath(__file__))
    removidos = 0
    try:
        docs = Documento.query.all()
        for doc in docs:
            if doc.url_arquivo:
                continue  # Documentos no Cloudinary não têm arquivo local
            caminho_full = os.path.join(base_path, doc.caminho_arquivo or '')
            if doc.caminho_arquivo and not os.path.exists(caminho_full):
                db.session.delete(doc)
                removidos += 1
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        return jsonify({'erro': str(e), 'removidos': 0}), 500
    return jsonify({'removidos': removidos, 'mensagem': f'{removidos} fantasma(s) removido(s) do banco.'})


@app.route('/admin/limpar_vinculos_quebrados', methods=['POST'])
@login_required
def limpar_vinculos_quebrados():
    """Limpa todos os vínculos quebrados:
    1. caminho_boleto/caminho_nf que apontam para documentos inexistentes
    2. Documentos com venda_id apontando para vendas inexistentes"""
    if not current_user.is_admin():
        flash('Acesso negado. Apenas administradores podem executar esta ação.', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        limpos_boleto = 0
        limpos_nf = 0
        limpos_docs = 0
        
        # 1. Limpar caminho_boleto que aponta para documentos inexistentes
        vendas_com_boleto = Venda.query.filter(Venda.caminho_boleto.isnot(None)).all()
        for v in vendas_com_boleto:
            caminho = (v.caminho_boleto or '').strip()
            if caminho:
                doc = Documento.query.filter(or_(Documento.caminho_arquivo == caminho, Documento.url_arquivo == caminho)).first()
                if not doc:
                    v.caminho_boleto = None
                    limpos_boleto += 1
        
        # 2. Limpar caminho_nf que aponta para documentos inexistentes
        vendas_com_nf = Venda.query.filter(Venda.caminho_nf.isnot(None)).all()
        for v in vendas_com_nf:
            caminho = (v.caminho_nf or '').strip()
            if caminho:
                doc = Documento.query.filter(or_(Documento.caminho_arquivo == caminho, Documento.url_arquivo == caminho)).first()
                if not doc:
                    v.caminho_nf = None
                    limpos_nf += 1
        
        # 3. Limpar documentos com venda_id apontando para vendas inexistentes
        documentos_com_venda = Documento.query.filter(Documento.venda_id.isnot(None)).all()
        for doc in documentos_com_venda:
            venda = Venda.query.get(doc.venda_id)
            if not venda:
                doc.venda_id = None
                limpos_docs += 1
        
        db.session.commit()
        total = limpos_boleto + limpos_nf + limpos_docs
        flash(f'✅ Limpeza concluída: {limpos_boleto} vínculo(s) de boleto, {limpos_nf} vínculo(s) de NF e {limpos_docs} documento(s) órfão(s) removidos ({total} total).', 'success')
        print(f"DEBUG LIMPEZA: {limpos_boleto} boletos, {limpos_nf} NFs e {limpos_docs} documentos órfãos limpos")
    except Exception as e:
        db.session.rollback()
        flash(f'Erro ao limpar vínculos: {str(e)}', 'error')
        print(f"DEBUG LIMPEZA ERRO: {str(e)}")
    
    return redirect(url_for('dashboard'))


@app.route('/debug/testar_log')
@login_required
def debug_testar_log():
    """Endpoint de debug para testar criação de arquivo de log"""
    import traceback
    try:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vinculo_detalhado.log')
        with open(log_path, 'w', encoding='utf-8') as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - TESTE DE CRIAÇÃO DE ARQUIVO\n")
        return jsonify({
            'sucesso': True,
            'arquivo_criado': os.path.exists(log_path),
            'caminho': log_path,
            'tamanho': os.path.getsize(log_path) if os.path.exists(log_path) else 0
        })
    except Exception as e:
        return jsonify({'sucesso': False, 'erro': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/debug-ping')
def debug_ping():
    """Sem login. Escreve em debug.log, lê o arquivo e retorna OK + preview; confirma que logging em request funciona."""
    try:
        _debug_log("app.py:debug-ping", "debug-ping hit", {"path": DEBUG_LOG_PATH}, "ALL", run_id="ping")
        lines = []
        if os.path.exists(DEBUG_LOG_PATH):
            with open(DEBUG_LOG_PATH, "r", encoding="utf-8") as f:
                lines = f.readlines()
        return jsonify({"ok": True, "path": DEBUG_LOG_PATH, "log_lines": len(lines), "log_preview": lines[-50:] if len(lines) > 50 else lines})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route('/sw.js')
def service_worker():
    """Serve o Service Worker com o tipo MIME correto."""
    return send_file('static/sw.js', mimetype='application/javascript')


@app.route('/cliente/<int:id>/receber_lote', methods=['POST'])
@login_required
def receber_lote_cliente(id):
    """Abatimento Inteligente: recebe valor em lote e abate nas vendas pendentes mais antigas."""
    valor_str = (request.form.get('valor_recebido') or '0').replace('.', '').replace(',', '.')
    valor_recebido = float(valor_str) if valor_str else 0.0
    forma_pgto = request.form.get('forma_pagamento', 'Dinheiro')

    cliente = Cliente.query.get_or_404(id)

    # Busca vendas PENDENTES ou PARCIAIS, da mais velha para a mais nova
    vendas_abertas = Venda.query.filter(
        Venda.cliente_id == id,
        Venda.situacao.in_(['PENDENTE', 'PARCIAL'])
    ).order_by(Venda.data_venda.asc()).all()

    valor_restante = valor_recebido

    for venda in vendas_abertas:
        if valor_restante <= 0:
            break

        venda.valor_pago = venda.valor_pago or 0.0
        valor_total_venda = float(venda.calcular_total())
        valor_falta = valor_total_venda - venda.valor_pago

        if valor_restante >= valor_falta:
            valor_abatido = valor_falta
            venda.valor_pago = valor_total_venda
            venda.situacao = 'PAGO'
            valor_restante -= valor_falta
        else:
            valor_abatido = valor_restante
            venda.valor_pago = (venda.valor_pago or 0) + valor_restante
            venda.situacao = 'PARCIAL'
            valor_restante = 0

        forma_pgto_upper = str(forma_pgto or '').upper()
        data_venc = getattr(venda, 'data_vencimento', None)
        if 'BOLETO' in forma_pgto_upper and data_venc:
            data_lancamento_caixa = data_venc
        else:
            data_lancamento_caixa = date.today()
        novo_lanc = LancamentoCaixa(
            data=data_lancamento_caixa,
            descricao=f"Venda #{venda.id} - {cliente.nome_cliente} (Abatimento)",
            tipo='ENTRADA',
            categoria='Entrada Cliente',
            forma_pagamento=forma_pgto,
            valor=valor_abatido,
            usuario_id=current_user.id
        )
        db.session.add(novo_lanc)

        if 'boleto' in forma_pgto.lower():
            repasse_lanc = LancamentoCaixa(
                data=data_lancamento_caixa,
                descricao=f"Venda #{venda.id} - {cliente.nome_cliente} (Repasse Abatimento)",
                tipo='SAIDA',
                categoria='Saída Fornecedor',
                forma_pagamento=forma_pgto,
                valor=valor_abatido,
                usuario_id=current_user.id
            )
            db.session.add(repasse_lanc)

    db.session.commit()
    limpar_cache_dashboard()
    flash(f'Abatimento de R$ {valor_recebido:,.2f} processado com sucesso para {cliente.nome_cliente}!', 'success')
    return redirect(url_for('listar_clientes'))


@app.route('/api/backup/excel')
@login_required
def backup_excel():
    """Cofre de Dados: exporta Vendas, Clientes e Produtos para CSV."""
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')

    # Seção 1: Vendas
    writer.writerow(["=== VENDAS ==="])
    writer.writerow(["ID", "Data", "Cliente", "Produto", "Qtd", "Preço Unit.", "Valor Total", "Custo Total", "Lucro", "Situação", "Status Entrega"])
    vendas = Venda.query.options(joinedload(Venda.cliente), joinedload(Venda.produto)).all()
    for v in vendas:
        cliente = v.cliente
        nome_cliente = cliente.nome_cliente if cliente else "Desconhecido"
        data_venda = v.data_venda.strftime('%d/%m/%Y') if v.data_venda else ""
        qtd = v.quantidade_venda or 0
        preco_unit = float(v.preco_venda or 0)
        valor_total = float(v.calcular_total())
        lucro = float(v.calcular_lucro())
        custo_total = valor_total - lucro
        produto_nome = v.produto.nome_produto if v.produto else "-"
        writer.writerow([v.id, data_venda, nome_cliente, produto_nome, qtd, preco_unit, valor_total, custo_total, lucro, v.situacao or "", v.status_entrega or ""])

    # Seção 2: Clientes
    writer.writerow([])
    writer.writerow(["=== CLIENTES ==="])
    writer.writerow(["ID", "Nome", "Razão Social", "CNPJ", "Cidade", "Endereço"])
    clientes = Cliente.query.all()
    for c in clientes:
        writer.writerow([c.id, c.nome_cliente or "", c.razao_social or "", c.cnpj or "", c.cidade or "", c.endereco or ""])

    # Seção 3: Estoque/Produtos
    writer.writerow([])
    writer.writerow(["=== ESTOQUE ==="])
    writer.writerow(["ID", "Nome", "Tipo", "Fornecedor", "Nacionalidade", "Tamanho", "Marca", "Preço Custo", "Qtd Entrada", "Estoque Atual", "Data Chegada"])
    produtos = Produto.query.all()
    for p in produtos:
        data_chegada = p.data_chegada.strftime('%d/%m/%Y') if p.data_chegada else ""
        writer.writerow([p.id, p.nome_produto or "", p.tipo or "", p.fornecedor or "", p.nacionalidade or "", p.tamanho or "", p.marca or "", float(p.preco_custo or 0), p.quantidade_entrada or 0, p.estoque_atual or 0, data_chegada])

    csv_data = output.getvalue()
    data_atual = datetime.now().strftime('%Y-%m-%d_%Hh%M')
    nome_arquivo = f"backup_menino_do_alho_{data_atual}.csv"
    return send_file(
        io.BytesIO(csv_data.encode('utf-8-sig')),
        download_name=nome_arquivo,
        as_attachment=True,
        mimetype='text/csv'
    )


@app.route('/debug-vincular')
@login_required
def debug_vincular():
    """Endpoint de debug para diagnóstico de vínculos - retorna todos os logs em JSON"""
    import traceback
    try:
        # Executar processamento com captura de logs em memória
        resultado = _processar_documentos_pendentes(capturar_logs_memoria=True)
        
        # Preparar resposta detalhada
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
        import traceback
        return jsonify({
            'sucesso': False,
            'erro': str(e),
            'traceback': traceback.format_exc(),
            'timestamp': datetime.now().isoformat()
        }), 500


if __name__ == '__main__':
    def _local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '127.0.0.1'

    try:
        ip = _local_ip()
        print('\n' + '=' * 50)
        print('Menino do Alho — Acesso na rede local (mobile):')
        print('  http://{}:5001'.format(ip))
        print('=' * 50 + '\n')
    except Exception:
        pass

    # Porta 5001 para fugir do bloqueio do Mac (AirPlay Receiver em 5000)
    # Debug deve ser False em produção para evitar vazamento de informações sensíveis
    app.run(host='0.0.0.0', port=5001, debug=False)
