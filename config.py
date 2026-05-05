import os


def _is_production() -> bool:
    """Heurística para detectar ambiente produtivo.

    A Render expõe ``RENDER=true`` por default. Em outros provedores,
    setar ``FLASK_ENV=production`` ou ``ENV=production`` faz o mesmo
    efeito. Em dev/CI seguimos com fallback.
    """
    if os.environ.get('RENDER') == 'true':
        return True
    if (os.environ.get('FLASK_ENV') or '').lower() == 'production':
        return True
    if (os.environ.get('ENV') or '').lower() == 'production':
        return True
    return False


_secret_env = os.environ.get('SECRET_KEY')
if not _secret_env and _is_production():
    raise RuntimeError(
        "SECRET_KEY não definida em ambiente de produção. "
        "Configure a variável de ambiente SECRET_KEY antes de iniciar o serviço."
    )


class Config:
    # Em produção, ``SECRET_KEY`` é obrigatória (validado acima).
    # Em dev, fallback com ``os.urandom`` rotaciona a cada restart — sessões
    # caem, mas o desenvolvimento segue sem precisar configurar nada.
    SECRET_KEY = _secret_env or os.urandom(24).hex()
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///menino_do_alho.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Cloudinary (uploads em nuvem)
    CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', '')
    CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', '')
    CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', '')
