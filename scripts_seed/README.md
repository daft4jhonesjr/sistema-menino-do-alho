# scripts_seed

Scripts de bootstrap/manutenção do banco. Estão isolados nesta pasta para
não serem confundidos com código de aplicação.

## reset_db.py / migrate_recreate_db.py

Operações destrutivas (`db.drop_all()`). Por padrão só rodam em ambiente
local. Para forçar em ambiente remoto, exporte
`CONFIRMO_DROP_PROD=YES_I_KNOW` (sabendo que vai perder os dados).

```bash
python scripts_seed/reset_db.py
python scripts_seed/migrate_recreate_db.py
```

## Outros scripts utilitários (na raiz)

- `criar_master.py` — cria o usuário MASTER inicial. Exige
  `MASTER_PASSWORD` no ambiente.
- `init_db.py` — cria as tabelas e (opcionalmente) o admin Jhones.
  Exige `ADMIN_INITIAL_PASS` para criar o admin.
- `resetar_senha.py` — reseta senha de um usuário. Exige
  `RESET_USERNAME` e `RESET_PASSWORD`.
- `migrar_dados.py` — migração one-shot SQLite → Postgres. Exige
  `POSTGRES_URL` no ambiente (não traz mais credenciais chumbadas).
