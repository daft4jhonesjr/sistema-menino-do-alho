import os

class Config:
    # Em produção, defina SECRET_KEY como variável de ambiente obrigatória.
    # O fallback com os.urandom garante que cada reinício gere uma chave nova
    # (sessões são invalidadas), mas é infinitamente mais seguro que chave fixa.
    SECRET_KEY = os.environ.get('SECRET_KEY') or os.urandom(24).hex()
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///menino_do_alho.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Cloudinary (uploads em nuvem)
    CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', '')
    CLOUDINARY_API_KEY = os.environ.get('CLOUDINARY_API_KEY', '')
    CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', '')
