import os
import secrets

class Config:
    # Secret key forte gerada com secrets.token_urlsafe
    # Em produção, defina SECRET_KEY como variável de ambiente
    SECRET_KEY = os.environ.get('SECRET_KEY') or '-vCohb0GSb3IEtEiotaxZ0_45Dtfu5Uq49llGXFQdOEy8AxcvvQj7Ft2uDeuCbeAGyKzUbN1P9QeQkDIfo-tVA'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///menino_do_alho.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
