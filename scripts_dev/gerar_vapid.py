#!/usr/bin/env python3
"""Gera par de chaves VAPID para Web Push (PWA).

Uso:
    python scripts_dev/gerar_vapid.py

Configure na Render (ou .env local):
    VAPID_PUBLIC_KEY=<publicKey base64url>
    VAPID_PRIVATE_KEY=<privateKey base64url>
    VAPID_CLAIM_EMAIL=mailto:seu-email@dominio.com.br

Alternativa via Node:
    npx web-push generate-vapid-keys
"""

from __future__ import annotations

import base64
import sys


def main() -> int:
    try:
        from py_vapid import Vapid
    except ImportError:
        print(
            'Instale pywebpush (inclui py_vapid): pip install pywebpush',
            file=sys.stderr,
        )
        return 1

    vapid = Vapid()
    vapid.generate_keys()

    private_pem = vapid.private_pem().decode('utf-8')
    public_bytes = vapid.public_key.public_bytes(
        __import__('cryptography.hazmat.primitives.serialization', fromlist=['Encoding', 'PublicFormat']).Encoding.X962,
        __import__('cryptography.hazmat.primitives.serialization', fromlist=['PublicFormat']).PublicFormat.UncompressedPoint,
    )
    public_b64 = base64.urlsafe_b64encode(public_bytes).decode('utf-8').rstrip('=')

    # pywebpush aceita PEM ou base64url; web-push CLI usa base64url para ambas.
    private_raw = vapid.private_key.private_bytes(
        __import__('cryptography.hazmat.primitives.serialization', fromlist=['Encoding', 'PrivateFormat', 'NoEncryption']).Encoding.DER,
        __import__('cryptography.hazmat.primitives.serialization', fromlist=['PrivateFormat']).PrivateFormat.PKCS8,
        __import__('cryptography.hazmat.primitives.serialization', fromlist=['NoEncryption']).NoEncryption(),
    )
    private_b64 = base64.urlsafe_b64encode(private_raw).decode('utf-8').rstrip('=')

    print('=' * 72)
    print('Chaves VAPID — copie para variáveis de ambiente na Render')
    print('=' * 72)
    print()
    print('VAPID_PUBLIC_KEY=' + public_b64)
    print('VAPID_PRIVATE_KEY=' + private_b64)
    print('VAPID_CLAIM_EMAIL=mailto:admin@meninoalho.com.br')
    print()
    print('(Formato alternativo — chave privada PEM, também aceito pelo pywebpush:)')
    print(private_pem)
    print('=' * 72)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
