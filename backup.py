"""
Sistema de Backup Automático para Menino do Alho
Cria backups automáticos do banco de dados SQLite com rotação de 7 dias
"""
import os
import shutil
from datetime import datetime
from pathlib import Path


def safe_get_mtime(arquivo):
    """
    Função auxiliar para obter data de modificação de forma segura.
    
    Em ambientes com múltiplos workers (Gunicorn), pode ocorrer race condition
    onde um worker deleta um arquivo enquanto outro tenta ler sua data.
    
    Args:
        arquivo: Caminho do arquivo (Path ou str)
    
    Returns:
        float: Timestamp de modificação ou 0 se arquivo não existir
    """
    try:
        return os.path.getmtime(arquivo)
    except FileNotFoundError:
        # Arquivo foi deletado por outro worker durante a operação
        return 0


def realizar_backup():
    """
    Realiza backup automático do banco de dados SQLite.
    
    Funcionalidades:
    - Cria pasta de backup em ~/Documents/Backups_MeninoDoAlho
    - Copia o arquivo do banco de dados com timestamp
    - Mantém apenas os últimos 7 backups (rotação automática)
    
    Returns:
        tuple: (sucesso: bool, mensagem: str, caminho_backup: str ou None)
    """
    try:
        # Determinar caminho do banco de dados
        # SQLite URI pode ser: sqlite:///menino_do_alho.db (relativo) ou sqlite:////path/to/db.db (absoluto)
        db_uri = os.environ.get('DATABASE_URL') or 'sqlite:///menino_do_alho.db'
        
        base_dir = os.path.dirname(os.path.abspath(__file__))
        caminho_db = None
        
        # Extrair nome do arquivo do banco
        if db_uri.startswith('sqlite:///'):
            db_path = db_uri.replace('sqlite:///', '')
            # Se começar com 3 barras, é caminho absoluto
            if db_path.startswith('/'):
                caminho_db = db_path
            else:
                # Caminho relativo - tentar primeiro na pasta instance (padrão Flask)
                caminho_instance = os.path.join(base_dir, 'instance', db_path)
                if os.path.exists(caminho_instance):
                    caminho_db = caminho_instance
                else:
                    # Se não encontrar em instance, tentar na raiz
                    caminho_raiz = os.path.join(base_dir, db_path)
                    if os.path.exists(caminho_raiz):
                        caminho_db = caminho_raiz
        else:
            # Fallback: procurar em múltiplos locais
            # 1. Tentar primeiro em instance/
            caminho_instance = os.path.join(base_dir, 'instance', 'menino_do_alho.db')
            if os.path.exists(caminho_instance):
                caminho_db = caminho_instance
            else:
                # 2. Tentar na raiz
                caminho_raiz = os.path.join(base_dir, 'menino_do_alho.db')
                if os.path.exists(caminho_raiz):
                    caminho_db = caminho_raiz
        
        # Verificar se o arquivo foi encontrado
        if caminho_db is None or not os.path.exists(caminho_db):
            # Listar locais tentados para debug
            locais_tentados = [
                os.path.join(base_dir, 'instance', 'menino_do_alho.db'),
                os.path.join(base_dir, 'menino_do_alho.db')
            ]
            mensagem_aviso = f"Arquivo do banco de dados não encontrado. Locais verificados: {', '.join(locais_tentados)}"
            print(f"[BACKUP] AVISO: {mensagem_aviso}")
            return False, mensagem_aviso, None
        
        # Criar pasta de backup
        home_dir = Path.home()
        pasta_backup = home_dir / 'Documents' / 'Backups_MeninoDoAlho'
        pasta_backup.mkdir(parents=True, exist_ok=True)
        
        # Gerar nome do arquivo de backup com timestamp
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        nome_backup = f'backup_{timestamp}.db'
        caminho_backup = pasta_backup / nome_backup
        
        # Copiar arquivo do banco de dados
        shutil.copy2(caminho_db, caminho_backup)
        
        # Rotação: manter apenas os últimos 7 backups
        # Usar safe_get_mtime para evitar race condition com múltiplos workers
        backups = sorted(pasta_backup.glob('backup_*.db'), key=safe_get_mtime, reverse=True)
        
        if len(backups) > 7:
            # Remover backups antigos (manter apenas os 7 mais recentes)
            for backup_antigo in backups[7:]:
                try:
                    os.remove(backup_antigo)
                    print(f"[BACKUP] Backup antigo removido: {backup_antigo.name}")
                except FileNotFoundError:
                    # Arquivo já foi deletado por outro worker - trabalho já está feito
                    pass
                except Exception as e:
                    print(f"[BACKUP] Erro ao remover backup antigo {backup_antigo.name}: {e}")
        
        mensagem = f"Backup criado com sucesso: {nome_backup} ({len(backups)} backups mantidos)"
        print(f"[BACKUP] {mensagem}")
        
        return True, mensagem, str(caminho_backup)
        
    except Exception as e:
        mensagem_erro = f"Erro ao realizar backup: {str(e)}"
        print(f"[BACKUP] ERRO: {mensagem_erro}")
        import traceback
        traceback.print_exc()
        return False, mensagem_erro, None


if __name__ == '__main__':
    # Teste manual
    sucesso, mensagem, caminho = realizar_backup()
    if sucesso:
        print(f"✓ {mensagem}")
        print(f"  Localização: {caminho}")
    else:
        print(f"✗ {mensagem}")
