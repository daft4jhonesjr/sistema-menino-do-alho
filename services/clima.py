"""Clima atual via Open-Meteo para o Dashboard.

Fluxo:
  1. Lê ``Configuracao.endereco_deposito`` (cidade/endereço do depósito).
  2. Geocodifica com a API pública Open-Meteo (sem chave).
  3. Consulta temperatura e weather_code atuais.
  4. Cacheia o resultado por tenant (~45 min) via Flask-Caching.

Fallback de localização: ``Petrolina, PE`` (mesmo padrão da logística).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from extensions import cache

logger = logging.getLogger(__name__)

_CACHE_TTL_SEGUNDOS = 45 * 60  # 45 minutos
_HTTP_TIMEOUT = 4
_GEOCODING_URL = 'https://geocoding-api.open-meteo.com/v1/search'
_FORECAST_URL = 'https://api.open-meteo.com/v1/forecast'
_FALLBACK_CIDADE = 'Petrolina'
_FALLBACK_UF = 'PE'
_FALLBACK_LAT = -9.3891
_FALLBACK_LON = -40.5030
_USER_AGENT = 'SistemaMeninoDoAlho/1.0 (dashboard-clima)'

# WMO Weather interpretation codes → (rótulo PT, ícone Lucide)
_WMO_MAP = {
    0: ('Céu limpo', 'sun'),
    1: ('Principalmente limpo', 'sun'),
    2: ('Parcialmente nublado', 'cloud-sun'),
    3: ('Nublado', 'cloud'),
    45: ('Neblina', 'cloud-fog'),
    48: ('Neblina com geada', 'cloud-fog'),
    51: ('Garoa fraca', 'cloud-drizzle'),
    53: ('Garoa', 'cloud-drizzle'),
    55: ('Garoa forte', 'cloud-drizzle'),
    56: ('Garoa congelante', 'cloud-drizzle'),
    57: ('Garoa congelante forte', 'cloud-drizzle'),
    61: ('Chuva fraca', 'cloud-rain'),
    63: ('Chuva', 'cloud-rain'),
    65: ('Chuva forte', 'cloud-rain'),
    66: ('Chuva congelante', 'cloud-rain'),
    67: ('Chuva congelante forte', 'cloud-rain'),
    71: ('Neve fraca', 'cloud-snow'),
    73: ('Neve', 'cloud-snow'),
    75: ('Neve forte', 'cloud-snow'),
    77: ('Grãos de neve', 'cloud-snow'),
    80: ('Pancadas fracas', 'cloud-rain'),
    81: ('Pancadas de chuva', 'cloud-rain'),
    82: ('Pancadas fortes', 'cloud-rain'),
    85: ('Pancadas de neve', 'cloud-snow'),
    86: ('Pancadas de neve fortes', 'cloud-snow'),
    95: ('Tempestade', 'cloud-lightning'),
    96: ('Tempestade com granizo', 'cloud-lightning'),
    99: ('Tempestade com granizo forte', 'cloud-lightning'),
}


def _http_get_json(url: str) -> Optional[dict]:
    req = urllib.request.Request(
        url,
        headers={'User-Agent': _USER_AGENT, 'Accept': 'application/json'},
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            raw = resp.read().decode('utf-8')
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
        OSError,
    ) as exc:
        logger.warning('Falha HTTP clima (%s): %s', url.split('?', 1)[0], exc)
        return None


def _endereco_deposito() -> str:
    """Endereço/cidade do depósito: Configuracao → env → fallback Petrolina, PE."""
    try:
        from services.config_helpers import get_config
        config = get_config()
        end = (getattr(config, 'endereco_deposito', None) or '').strip()
        if end:
            return end
    except Exception:
        logger.debug('Não foi possível ler endereco_deposito', exc_info=True)
    return (os.environ.get('GALPAO_ENDERECO') or f'{_FALLBACK_CIDADE}, {_FALLBACK_UF}').strip()


def _extrair_cidade_busca(endereco: str) -> list[str]:
    """Gera candidatos de busca a partir do endereço cadastrado.

    Ordem: string completa → "Cidade, UF" detectado → último token útil.
    """
    endereco = (endereco or '').strip()
    if not endereco:
        return [f'{_FALLBACK_CIDADE}, {_FALLBACK_UF}']

    candidatos: list[str] = [endereco]
    partes = [p.strip() for p in endereco.split(',') if p.strip()]

    # Padrão brasileiro: ..., Cidade, UF[, CEP]
    for i, parte in enumerate(partes):
        uf = re.sub(r'[^A-Za-z]', '', parte)
        if len(uf) == 2 and uf.isalpha() and i > 0:
            cidade_uf = f'{partes[i - 1]}, {uf.upper()}'
            if cidade_uf not in candidatos:
                candidatos.append(cidade_uf)
            if partes[i - 1] not in candidatos:
                candidatos.append(partes[i - 1])
            break
    else:
        # Sem UF explícita: tenta o penúltimo / último segmento (ignora CEP)
        for parte in reversed(partes):
            if re.match(r'^\d{5}-?\d{0,3}$', parte):
                continue
            if parte not in candidatos:
                candidatos.append(parte)
            break

    return candidatos


def _geocodificar(nome: str) -> Optional[dict[str, Any]]:
    """Consulta Open-Meteo Geocoding. Retorna dict com lat/lon/cidade/uf ou None."""
    nome = (nome or '').strip()
    if not nome:
        return None
    params = urllib.parse.urlencode({
        'name': nome,
        'count': 1,
        'language': 'pt',
        'format': 'json',
        'countryCode': 'BR',
    })
    data = _http_get_json(f'{_GEOCODING_URL}?{params}')
    if not data:
        return None
    results = data.get('results') or []
    if not results:
        return None
    item = results[0]
    try:
        lat = float(item['latitude'])
        lon = float(item['longitude'])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        'latitude': lat,
        'longitude': lon,
        'cidade': (item.get('name') or nome).strip(),
        'uf': (item.get('admin1') or '').strip(),
        'pais': (item.get('country_code') or '').strip().upper(),
    }


def _resolver_localizacao(endereco: str) -> dict[str, Any]:
    """Resolve lat/lon/cidade a partir do endereço, com fallback Petrolina."""
    for candidato in _extrair_cidade_busca(endereco):
        geo = _geocodificar(candidato)
        if geo:
            return geo
    return {
        'latitude': _FALLBACK_LAT,
        'longitude': _FALLBACK_LON,
        'cidade': _FALLBACK_CIDADE,
        'uf': _FALLBACK_UF,
        'pais': 'BR',
    }


def _interpretar_codigo(codigo: Any) -> tuple[str, str]:
    try:
        code = int(codigo)
    except (TypeError, ValueError):
        return ('Condição desconhecida', 'cloud')
    return _WMO_MAP.get(code, ('Condição desconhecida', 'cloud'))


def _consultar_forecast(lat: float, lon: float) -> Optional[dict[str, Any]]:
    params = urllib.parse.urlencode({
        'latitude': f'{lat:.4f}',
        'longitude': f'{lon:.4f}',
        'current': 'temperature_2m,weather_code',
        'timezone': 'auto',
    })
    data = _http_get_json(f'{_FORECAST_URL}?{params}')
    if not data:
        return None
    current = data.get('current') or {}
    temp = current.get('temperature_2m')
    code = current.get('weather_code')
    if temp is None:
        return None
    try:
        temperatura = round(float(temp))
    except (TypeError, ValueError):
        return None
    descricao, icone = _interpretar_codigo(code)
    return {
        'temperatura': temperatura,
        'weather_code': code,
        'descricao': descricao,
        'icone': icone,
    }


def _cache_key(empresa_id: Optional[int], endereco: str) -> str:
    end_norm = re.sub(r'\s+', ' ', (endereco or '').strip().lower())
    escopo = f'emp:{empresa_id}' if empresa_id is not None else 'emp:global'
    return f'clima:v1:{escopo}:{end_norm}'


def obter_clima_atual(empresa_id: Optional[int] = None) -> dict[str, Any]:
    """Retorna o clima atual do depósito do tenant (com cache).

    Formato de sucesso::

        {
          'ok': True,
          'cidade': 'Petrolina',
          'uf': 'Pernambuco',
          'temperatura': 32,
          'unidade': '°C',
          'descricao': 'Céu limpo',
          'icone': 'sun',
          'weather_code': 0,
        }

    Em falha total (API offline), retorna ``ok: False`` sem quebrar o caller.
    """
    endereco = _endereco_deposito()
    key = _cache_key(empresa_id, endereco)

    try:
        cached = cache.get(key)
        if isinstance(cached, dict) and cached.get('ok'):
            cached = dict(cached)
            cached['cached'] = True
            return cached
    except Exception:
        logger.debug('Cache get clima falhou', exc_info=True)

    try:
        local = _resolver_localizacao(endereco)
        forecast = _consultar_forecast(local['latitude'], local['longitude'])
        if not forecast:
            return {
                'ok': False,
                'mensagem': 'Não foi possível obter o clima agora.',
                'cidade': local.get('cidade') or _FALLBACK_CIDADE,
            }

        cidade = local.get('cidade') or _FALLBACK_CIDADE
        uf = local.get('uf') or ''
        payload = {
            'ok': True,
            'cidade': cidade,
            'uf': uf,
            'temperatura': forecast['temperatura'],
            'unidade': '°C',
            'descricao': forecast['descricao'],
            'icone': forecast['icone'],
            'weather_code': forecast['weather_code'],
            'cached': False,
        }
        try:
            cache.set(key, {k: v for k, v in payload.items() if k != 'cached'}, timeout=_CACHE_TTL_SEGUNDOS)
        except Exception:
            logger.debug('Cache set clima falhou', exc_info=True)
        return payload
    except Exception:
        logger.exception('Erro inesperado ao obter clima')
        return {
            'ok': False,
            'mensagem': 'Não foi possível obter o clima agora.',
        }
