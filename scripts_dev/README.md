# scripts_dev

Scripts CLI utilitários para setup e manutenção de ambiente. **Não** são
executados pela aplicação em runtime — só por operação manual.

Todos exigem variáveis de ambiente para evitar credenciais chumbadas.

## init_db.py

Cria as tabelas e (opcionalmente) o usuário admin Jhones.

```bash
ADMIN_INITIAL_PASS="senha_forte" python scripts_dev/init_db.py
```

Sem `ADMIN_INITIAL_PASS`, apenas garante que as tabelas existam.

## criar_master.py

Cria o usuário Super Admin (MASTER) usado para acessar `/master-admin`.

```bash
MASTER_USERNAME="master" MASTER_PASSWORD="senha_provisoria_forte" \
    python scripts_dev/criar_master.py
```

Após o primeiro login, ALTERE A SENHA pela tela de gerenciamento de
usuários.

## resetar_senha.py

Reseta a senha de um usuário existente (uso emergencial).

```bash
RESET_USERNAME="Jhones" RESET_PASSWORD="senha_nova_forte" \
    python scripts_dev/resetar_senha.py
```

## migrar_dados.py

Migração one-shot SQLite → Postgres (histórico). Hoje é referência;
exige `POSTGRES_URL` no ambiente.

```bash
SQLITE_DB="instance/menino_do_alho.db" \
POSTGRES_URL="postgresql://user:pass@host/db?sslmode=require" \
    python scripts_dev/migrar_dados.py
```

## Pasta irmã: `scripts_seed/`

Operações destrutivas no banco (`drop_all + create_all`) ficam em
`scripts_seed/` e exigem `CONFIRMO_DROP_PROD=YES_I_KNOW` para rodar
fora de `localhost`.
