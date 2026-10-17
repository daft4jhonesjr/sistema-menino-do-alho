# Schema do banco

Fonte: modelos SQLAlchemy em `models.py`. O isolamento é por `empresa_id` (tenant). Não existe tabela `pedidos`, `lotes` nem `despesas`: esses conceitos estão nas tabelas abaixo.

| Conceito | Onde vive |
| --- | --- |
| Cliente | `clientes` |
| Lote | `produtos` — cada linha é um lote de entrada (tipo, marca, tamanho, custo, saldo) |
| Venda / item de pedido | `vendas` — uma linha por produto vendido |
| Pedido | Agrupamento lógico de linhas de `vendas` com o mesmo `empresa_id`, `cliente_id`, `data_venda` e, quando o cliente tem CNPJ, a mesma `nf`. Não há cabeçalho |
| Despesa operacional | `lancamentos_caixa` com `tipo = 'SAIDA'` (ex.: `Saída Pessoal`, `Saída Fornecedor`, `Fundo de Caixa (Saída)`) |
| Despesa pessoal / fixa | `itens_orcamento` + status mensal em `pagamentos_orcamento` |
| Recebimento de venda | `lancamentos_caixa` com `tipo = 'ENTRADA'` e `venda_id` preenchido |

`produtos.fornecedor` e `produtos.tipo` são texto, não chave estrangeira. `vendas.empresa_faturadora` também é texto (quem emitiu a NF), distinto de `empresas`.

## Relacionamentos

```
empresas
  ├── usuarios
  ├── clientes ─────────────── vendas ─── documentos
  ├── produtos (lotes) ───────┘     │
  │     └── produto_fotos           └── lancamentos_caixa (venda_id, ON DELETE SET NULL)
  ├── fornecedores ── fornecedor_tipo_assoc ── tipos_produto
  ├── configuracoes
  ├── contagens_gaveta
  ├── itens_orcamento ── pagamentos_orcamento
  ├── lembretes (parent_id → lembretes)
  └── votos_frase

usuarios
  ├── historico_login
  ├── push_subscriptions
  ├── log_atividades
  └── (opcional) lancamentos_caixa, contagens_gaveta, itens_orcamento, documentos
```

Regras de exclusão relevantes:

- Apagar `clientes` ou `produtos` falha se existir `vendas` apontando para eles (`ON DELETE` padrão = restrição).
- Apagar `vendas` apaga `documentos` vinculados (`ON DELETE CASCADE`) e zera `lancamentos_caixa.venda_id` (`ON DELETE SET NULL`). O lançamento de caixa permanece.
- Apagar `empresas` em cascata remove os dados do tenant nas tabelas que declaram `ON DELETE CASCADE`.
- Apagar `usuarios` apaga histórico de login e push (`CASCADE`) e deixa `log_atividades.usuario_id` nulo (`SET NULL`).

## Tabelas

Tipos refletem o modelo. `empresa_id` é anulável em várias tabelas por causa da migração; a regra de negócio exige valor, exceto no usuário `MASTER`.

### `empresas`

Tenant. PK `id`.

| Coluna | Tipo | Notas |
| --- | --- | --- |
| id | Integer | PK |
| nome_fantasia | String(150) | obrigatório, indexado |
| cnpj | String(18) | único, opcional |
| data_cadastro | DateTime | obrigatório |
| ativo | Boolean | `false` suspende o login dos usuários do tenant |

### `usuarios`

Conta de acesso. PK `id`. FK `empresa_id` → `empresas.id` (`ON DELETE SET NULL`).

| Coluna | Tipo | Notas |
| --- | --- | --- |
| username | String(80) | único, obrigatório |
| password_hash | String(256) | obrigatório |
| role | String(20) | legado: `admin` ou `user` |
| perfil | String(20) | `MASTER`, `DONO` ou `FUNCIONARIO` |
| empresa_id | Integer | nulo só para `MASTER` |
| nome | String(100) | |
| email | String(150) | |
| profile_image_url | String(500) | Cloudinary |
| permissoes | Text | JSON de módulos |
| session_token | String(100) | invalidação de sessão |
| onesignal_player_id | String(255) | último device |
| ultimo_acesso | DateTime | |
| notifica_boletos / notifica_radar / notifica_logistica / notifica_frase | Boolean | padrão `true` |
| horario_boletos / horario_radar / horario_logistica / horario_frase | String(5) | `HH:MM` |

### `historico_login`

Um registro por login bem-sucedido. PK `id`. FK `usuario_id` → `usuarios.id` (`CASCADE`), obrigatória.

Colunas: `data_hora`, `ip_address` (45), `dispositivo` (200), `localizacao` (150).

### `clientes`

PK `id`. FK `empresa_id` → `empresas.id` (`CASCADE`). Único (`empresa_id`, `cnpj`). Índice (`empresa_id`, `ativo`).

| Coluna | Tipo | Notas |
| --- | --- | --- |
| nome_cliente | String(200) | obrigatório |
| razao_social | String(200) | |
| cnpj | String(18) | único dentro da empresa |
| inscricao_estadual | String(50) | |
| telefone / telefone_secundario | String(20) | |
| nome_contato / nome_contato_secundario | String(100) | |
| cidade, bairro | String(100) | |
| estado | String(2) | UF |
| cep | String(20) | |
| rua | String(150) | |
| numero | String(20) | |
| endereco | String(255) | legado; a UI usa os campos estruturados |
| latitude / longitude | Float | cache Nominatim para rota |
| ativo | Boolean | soft delete |

Relacionamento: 1 cliente → N `vendas`.

### `produtos` (lotes)

PK `id`. FK `empresa_id` → `empresas.id` (`CASCADE`). Check `estoque_atual >= 0`. Índices (`empresa_id`, `estoque_atual`) e (`empresa_id`, `tipo`).

| Coluna | Tipo | Notas |
| --- | --- | --- |
| tipo | String(50) | nome do tipo (texto), ou `OUTROS` |
| nacionalidade | String(20) | obrigatório |
| marca | String(100) | obrigatório |
| tamanho | String(10) | número ou `P`/`M`/`G`/`S/N` |
| fornecedor | String(20) | nome do fornecedor, sem FK |
| caminhoneiro | String(100) | obrigatório |
| preco_custo | Numeric(10,2) | obrigatório |
| preco_venda_alvo | Numeric(10,2) | opcional |
| quantidade_entrada | Integer | quantidade original do lote |
| estoque_atual | Integer | saldo |
| quantidade_devolvida | Integer | acumulado devolvido ao fornecedor |
| data_chegada | Date | |
| nome_produto | String(200) | nome gerado do lote |

Relacionamentos: 1 lote → N `vendas` e N `produto_fotos` (fotos em cascata, até 5 na aplicação).

### `fornecedores`

PK `id`. FK `empresa_id` → `empresas.id` (`CASCADE`). Único (`empresa_id`, `nome`).

Colunas: `nome` (100, obrigatório), `razao_social` (150), `cnpj` (20), `endereco` (255).

Não há FK a partir de `produtos`. O vínculo com tipos é N:N.

### `tipos_produto`

PK `id`. FK `empresa_id` → `empresas.id` (`CASCADE`). Único (`empresa_id`, `nome`).

Colunas: `nome` (100), `config_atributos` (Text, JSON: quais atributos o tipo usa e as opções de nacionalidade, tamanho e marca).

### `fornecedor_tipo_assoc`

Tabela de associação. PK composta.

| Coluna | FK | On delete |
| --- | --- | --- |
| fornecedor_id | `fornecedores.id` | CASCADE |
| tipo_id | `tipos_produto.id` | CASCADE |

### `produto_fotos`

PK `id`. FK `produto_id` → `produtos.id` (`CASCADE`), obrigatória.

Colunas: `arquivo` (500, URL Cloudinary ou nome local legado), `public_id` (200).

### `vendas`

Item de pedido. PK `id`. FKs:

| Coluna | Referência | On delete |
| --- | --- | --- |
| empresa_id | `empresas.id` | CASCADE |
| cliente_id | `clientes.id` | restrição (obrigatória) |
| produto_id | `produtos.id` | restrição (obrigatória) |

Índices compostos: (`empresa_id`, `data_venda`), (`empresa_id`, `situacao`), (`empresa_id`, `cliente_id`, `data_venda`), (`empresa_id`, `tipo_operacao`).

| Coluna | Tipo | Notas |
| --- | --- | --- |
| nf | String(50) | número da nota; entra na chave lógica do pedido |
| preco_venda | Numeric(10,2) | unitário |
| quantidade_venda | Integer | baixa o `estoque_atual` do lote |
| data_venda | Date | |
| empresa_faturadora | String(20) | texto, não é `empresas.id` |
| situacao | String(20) | `PENDENTE`, `PARCIAL`, `PAGO`, `PERDA` |
| valor_pago | Numeric(10,2) | abatido; o detalhe fica em `lancamentos_caixa` |
| status_entrega | String(50) | padrão `PENDENTE` |
| forma_pagamento | String(50) | |
| tipo_operacao | String(20) | `VENDA` ou `PERDA` |
| lucro_percentual | Numeric(6,2) | opcional; senão o lucro usa custo do lote |
| cliente_avulso | String(100) | nome livre quando não há cadastro útil |
| caminho_boleto / caminho_nf | String(500) | atalho do arquivo já vinculado |
| data_vencimento | Date | do boleto |
| prazo_dias | Integer | `data_vencimento - data_venda` |

Total da linha = `preco_venda * quantidade_venda` (zero se `tipo_operacao = PERDA`). Lucro de perda é o custo do lote com sinal negativo.

### `documentos`

PDF de boleto ou NF. PK `id`. FKs:

| Coluna | Referência | On delete |
| --- | --- | --- |
| empresa_id | `empresas.id` | CASCADE |
| venda_id | `vendas.id` | CASCADE, opcional até o vínculo |
| usuario_id | `usuarios.id` | sem `ON DELETE` declarado |

| Coluna | Tipo | Notas |
| --- | --- | --- |
| tipo | String(20) | `BOLETO` ou `NOTA_FISCAL` |
| url_arquivo | String(500) | Cloudinary |
| public_id | String(200) | único, exclusão na nuvem |
| caminho_arquivo | String(500) | caminho local legado |
| cnpj | String(18) | extraído |
| numero_nf / nf_extraida | String(50) | `nf_extraida` é cache de OCR |
| razao_social | String(200) | |
| data_vencimento | Date | |
| valor | Numeric(12,2) | |
| data_processamento | Date | |

O bootstrap também cria `conteudo_binario` (`BYTEA` no Postgres, `BLOB` no SQLite). A coluna não está mapeada no modelo.

### `lancamentos_caixa`

Livro caixa: entradas e saídas. Despesa operacional = linha com `tipo = 'SAIDA'`. PK `id`.

| Coluna | Referência / tipo | Notas |
| --- | --- | --- |
| empresa_id | FK `empresas.id` CASCADE | |
| venda_id | FK `vendas.id` SET NULL | pagamento ou estorno de uma venda; nulo em despesa, gaveta e ajuste |
| usuario_id | FK `usuarios.id` | quem lançou |
| data | Date | obrigatório |
| descricao | String(200) | legado ainda grava `Venda #N - cliente` |
| tipo | String(20) | `ENTRADA` ou `SAIDA` |
| categoria | String(50) | ex.: `Entrada Cliente`, `Saída Pessoal`, `Saída Fornecedor` |
| forma_pagamento | String(50) | |
| setor | String(50) | padrão `GERAL` |
| status_envio | String(20) | ciclo do cheque (`Não Enviado` / `Enviado`) |
| valor | Numeric(10,2) | |

### `contagens_gaveta`

Snapshot da contagem de dinheiro e cheques. PK `id`. FKs `empresa_id` → `empresas.id` (`CASCADE`) e `usuario_id` → `usuarios.id`.

Colunas: `data`, `estado_json` (Text, `{"dinheiro":[...], "cheques":[...]}`), `atualizado_em`.

### `itens_orcamento`

Despesa fixa ou variável do orçamento pessoal. PK `id`. FKs `empresa_id` → `empresas.id` (`CASCADE`) e `usuario_id` → `usuarios.id`.

| Coluna | Tipo | Notas |
| --- | --- | --- |
| descricao | String(150) | obrigatório |
| valor | Numeric(10,2) | obrigatório |
| categoria | String(50) | `Custo básico`, `Saúde`, `Lazer`, `Investimento` |
| forma_pagamento | String(50) | padrão `Pix` |
| data_vencimento | Date | dia de vencimento |

1 item → N `pagamentos_orcamento` (apaga os pagamentos junto).

### `pagamentos_orcamento`

Marca o item como pago numa competência. PK `id`. FK `item_id` → `itens_orcamento.id` (`CASCADE`). Único (`item_id`, `mes_ano`).

`mes_ano` é `YYYY-MM`. Sem linha naquele mês, o item está pendente. Não guarda valor: o valor continua em `itens_orcamento`.

### `configuracoes`

Uma linha de parâmetros por tenant. PK `id`. FK `empresa_id` → `empresas.id` (`CASCADE`).

Colunas: `codigo_cadastro` (100, código do `/cadastro`, padrão `alho123`), `endereco_deposito` (255, origem das rotas).

### `lembretes`

Compromisso manual do calendário. PK `id`. FK `empresa_id` → `empresas.id` (`CASCADE`), obrigatória. FK `parent_id` → `lembretes.id` (`CASCADE`): nulo no lembrete principal, preenchido nos sub-lembretes.

| Coluna | Tipo | Notas |
| --- | --- | --- |
| data | Date | |
| descricao | String(500) | |
| concluido | Boolean | |
| ordem | Integer | drag-and-drop |
| horario | String(5) | `HH:MM`, opcional |
| recorrencia | String(20) | `nunca`, `diaria`, `semanal`, `mensal`, `anual` |
| origem_id | Integer | id do lembrete que gerou a ocorrência; sem FK |
| criado_em | DateTime | |

### `votos_frase`

Like/dislike da frase do dia, um por tenant e texto. PK `id`. FK `empresa_id` → `empresas.id` (`CASCADE`). Único (`empresa_id`, `frase_texto`).

Colunas: `frase_texto` (500), `autor` (200), `gostou` (Boolean), `criado_em`.

### `push_subscriptions`

Inscrição Web Push. PK `id`. FK `user_id` → `usuarios.id` (`CASCADE`). `endpoint` é único.

Colunas: `endpoint` (Text), `p256dh` (Text), `auth` (64), `criado_em`. Um usuário pode ter várias linhas (um device cada).

### `log_atividades`

Auditoria de ações. PK `id`. FK `usuario_id` → `usuarios.id` (`SET NULL`). Não tem `empresa_id`: o tenant é inferido pelo usuário.

| Coluna | Tipo | Notas |
| --- | --- | --- |
| acao | String(20) | `CRIAR`, `EDITAR`, `EXCLUIR`, `INATIVAR`, `ATIVAR`, `PAGAR`, `BACKUP` |
| modulo | String(30) | `VENDAS`, `CLIENTES`, `PRODUTOS`, `USUARIOS`, `BACKUP` |
| descricao | Text | |
| data_hora | DateTime | UTC |
| ip_address | String(45) | |
| arquivo_anexo | String(500) | caminho ou URL para redownload |

## Fluxo venda → lote → caixa

1. A entrada de mercadoria cria ou soma um lote em `produtos` (`quantidade_entrada` e `estoque_atual`).
2. Cada item vendido insere uma linha em `vendas` (`cliente_id` + `produto_id`) e reduz `produtos.estoque_atual`.
3. Várias linhas com o mesmo cliente, data e NF formam um pedido.
4. Recebimento grava `lancamentos_caixa` (`ENTRADA`, `venda_id`) e atualiza `vendas.valor_pago` / `situacao`.
5. Boleto ou NF entra em `documentos`. O vínculo preenche `documentos.venda_id` e os atalhos `vendas.caminho_boleto` / `vendas.caminho_nf`.
6. Despesa que não quita venda fica em `lancamentos_caixa` com `venda_id` nulo. Despesa recorrente do orçamento pessoal fica em `itens_orcamento`, não no livro caixa, até alguém lançá-la à parte.
