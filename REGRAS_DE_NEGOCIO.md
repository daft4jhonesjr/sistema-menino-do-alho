# Regras de negócio

Regras extraídas das funções que calculam estoque, preço, caixa e mensagens. Um lote é uma linha de `produtos`. Uma venda é uma linha de `vendas`. O pedido é o conjunto de linhas com o mesmo cliente, a mesma data e, quando o CNPJ não é de consumidor final, a mesma NF.

O WhatsApp não é enviado por API. O sistema monta o texto e abre `https://wa.me/{telefone}?text=...`, ou copia o romaneio para a área de transferência. O operador dispara a mensagem.

## 1. Lotes de alho

Fonte: `routes/produtos.py` (`novo_produto`, `gerar_nome_produto`, `devolver_produto`, importação), `services/estoque_fifo.py`, `routes/vendas.py` (`nova_venda`, `processar_carrinho`, `excluir_item_venda`, `trocar_item_venda`).

### Entrada

O cadastro manual cria sempre um lote novo. Não soma num lote já existente.

Obrigatórios: fornecedor, preço de custo, caminhoneiro, tipo e quantidade maior que zero. `quantidade_entrada` e `estoque_atual` nascem iguais. `preco_venda_alvo` fica nulo. Até 5 fotos vão para o Cloudinary.

Nome do lote (exceto bacalhau):

```
TIPO NACIONALIDADE MARCA DD/MM/AA TAMANHO
```

Partes `S/N`, `N/A`, `NA` e `NENHUMA` saem do nome. Data ausente usa o dia corrente.

Regras por tipo:

| Tipo | Regra |
| --- | --- |
| ALHO | Nacionalidade, marca e tamanho vêm do formulário (opções do `TipoProduto`). Sem lista fixa no backend |
| SACOLA | Nacionalidade vira `N/A` (some do nome), marca fixa `SOPACK`, tamanho só `P`, `M`, `G` ou `S/N` |
| BACALHAU | Marca padrão `NORGY`, fornecedor padrão `ARMAZEM LACERDA`. Tamanho só `7/9`, `10/12`, `13/15`, `16/20` ou `DESFIADO`. Nome: `BACALHAU MARCA TAMANHO`, sem data |
| Demais | Nacionalidade ou tamanho vazios viram `N/A`. Tipo desconhecido no cadastro da empresa vira `OUTROS` |

Na importação, linha idêntica (mesmo nome, mesma `data_chegada`, mesma quantidade e mesmo fornecedor) é ignorada. Se já existe o mesmo `nome_produto`, a quantidade é **somada** a `estoque_atual` e custo, fornecedor e caminhoneiro são **substituídos**. `quantidade_entrada` desse lote antigo não muda.

### Controle (FIFO)

SKU do lote = `tipo + nacionalidade + marca + tamanho`, em maiúsculas. Fornecedor e data não entram na chave.

A baixa de uma venda (`alocar_baixa_fifo`) consome os lotes desse SKU com saldo, do mais antigo para o mais novo (`data_chegada`, depois `id`). Cada lote é travado com `FOR UPDATE`. Se um lote não cobre a quantidade, a venda se parte em várias linhas, uma por lote, com o mesmo preço, cliente, NF e data. Estoque agregado insuficiente rejeita a operação e não debita nada.

Idade no galpão (`dias_no_galpao`), só para o radar:

| Dias | Status |
| --- | --- |
| 0 a 20 | Fresco |
| 21 a 40 | Atenção - Escoar |
| 41 ou mais | CRÍTICO - Promocionar |

### Abatimento e devolução de estoque

| Operação | Efeito no lote |
| --- | --- |
| Venda ou perda | Debita `estoque_atual` via FIFO. Perda também debita, com quantidade mínima 1 |
| Exclusão da linha | Devolve `quantidade_venda` ao lote daquela linha (`produto_id`), não redistribui em FIFO |
| Troca de item | Devolve a quantidade ao lote antigo e debita o lote escolhido (sem FIFO). Troca parcial cria outra linha no mesmo pedido. A linha nova fica com entrega `PENDENTE`. Perda não pode ser trocada |
| Devolução ao fornecedor | Debita `estoque_atual` e soma em `quantidade_devolvida`. Quantidade entre 1 e o saldo. Não gera venda |

Lote com venda vinculada não pode ser excluído (a chave estrangeira impede).

## 2. Custo, margem e preço

Fonte: `models.py` (`Venda.calcular_total`, `Venda.calcular_lucro`, `Produto.preco_venda_alvo_ou_default`) e `routes/dashboard.py` (`calcular_rentabilidade_por_praca`, `calcular_rentabilidade_por_bairro`).

O preço de venda **não é calculado** a partir do custo. O operador informa `preco_venda` unitário. `preco_custo` é o custo unitário do lote. Se `preco_venda_alvo` do lote estiver vazio, `preco_venda_alvo_ou_default()` devolve 160; esse valor não entra no lucro.

Empresas faturadoras aceitas no carrinho: `DESTAK`, `PATY`, `NENHUM`, `ARMAZEM LACERDA`, `SERVE BEM`. O padrão do formulário é `PATY`.

### Total e lucro da linha

`tipo_operacao` só pode ser `VENDA` ou `PERDA`.

| Caso | Total | Lucro |
| --- | --- | --- |
| PERDA | 0. Preço, forma de pagamento, prazo e `lucro_percentual` são zerados. `situacao` vira `PERDA` | `-(preco_custo do lote × quantidade)` |
| VENDA com `lucro_percentual` > 0 | `preco_venda × quantidade` | `total × (lucro_percentual / 100)`. Percentual negativo informado vira 0 e cai na fórmula de custo |
| VENDA sem percentual | `preco_venda × quantidade` | `(preco_venda − preco_custo) × quantidade` |

### Margem dos relatórios de praça e bairro

Esses relatórios **não usam** `lucro_percentual`. Excluem `tipo_operacao = PERDA`.

```
faturamento = Σ (preco_venda × quantidade)
lucro       = Σ ((preco_venda − preco_custo) × quantidade)
preço médio = faturamento / volume
margem %    = (lucro / faturamento) × 100
```

Volume zero ou faturamento zero geram média e margem 0. Cidade vazia vira `NÃO INFORMADA`; bairro vazio vira `NÃO INFORMADO`. Pedido distinto = `cliente_id + NF + data`.

Cor da margem: verde se `≥ 12%`, amarelo se `≥ 8%` e `< 12%`, vermelho abaixo de 8%.

Período: mês corrente, últimos 90 dias, ano ativo da sessão (padrão) ou histórico inteiro.

## 3. Caixa, boletos e romaneio

### Situação da venda

Fonte: `app.py` (`_resincronizar_pagamento_venda`).

A situação de pagamento sai da soma das entradas de caixa cuja descrição começa com `Venda #{id} -`, no mesmo tenant. Saídas não entram na soma. Tolerância de R$ 0,01.

| Soma das entradas | Situação |
| --- | --- |
| ≤ 0,01 | `PENDENTE` e `valor_pago = 0` |
| maior que 0,01 e menor que o total − 0,01 | `PARCIAL` |
| ≥ total − 0,01 | `PAGO` |

`PERDA` não é ressincronizada. A variante `_resincronizar_pagamento_venda_seguro` (rotas administrativas) só promove `PENDENTE → PARCIAL → PAGO` e nunca rebaixa um `PAGO` legado que não tem lançamento.

### Abatimento de recebimento

Fonte: `routes/clientes.py` (`receber_lote_cliente`).

O valor (formato `1.000,00`) percorre as vendas `PENDENTE` e `PARCIAL` do cliente, da mais antiga para a mais nova (`data_venda`, depois `id`). Pula `PERDA` e linha já quitada. Cada fatia vira uma entrada:

```
Venda #{id} - {cliente} (Lote: R$ {valor total do pagamento})
categoria: Entrada Cliente
venda_id: id da venda
```

O prefixo `Venda #{id} -` é o que o resync reconhece. O marcador `(Lote: R$ …)` é só auditoria e se repete em todas as fatias do mesmo pagamento. Sobra sem saldo devedor não gera lançamento.

Se a forma contém `boleto` e não há triangulação, cada fatia ganha uma saída do mesmo valor, categoria `Saída Fornecedor`, descrição `Venda #{id} - {cliente} (Repasse Lote: R$ …)`.

Triangulação (`repassar_fornecedor`): o cliente pagou na conta do fornecedor. As entradas por venda continuam (para quitar). Uma única saída do valor efetivamente aplicado substitui os repasses por venda:

```
Repasse Direto (Origem: Lote {cliente}) - Fornecedor: {nome}
```

Entrada e saída se anulam no saldo do caixa.

Na criação da venda, o caixa automático só roda se a situação já nasce `PAGO` ou `CONCLUÍDO` e ainda não existe lançamento `Venda #{id} -`. Gera a entrada. Se a forma contém `boleto`, gera também a saída `(Repasse Fornecedor)` do mesmo valor. Com boleto e `data_vencimento` preenchida, a data do lançamento é o vencimento; senão, é hoje. Se o FIFO partiu a venda em várias linhas, esse lançamento automático usa só a última linha criada.

Cheque nasce com `status_envio = Não Enviado`. Outras formas ficam com status nulo.

### Fechamento mensal

Fonte: `routes/caixa.py` (`realizar_fechamento_mensal`, `_calcular_saldos_por_forma`).

Não fecha o mês corrente nem mês futuro. Setor só `GERAL` ou `BACALHAU` (outro valor vira `GERAL`).

Saldo da forma = entradas − saídas do mês, no setor e no tenant. Formas reconhecidas: `Dinheiro`, `Pix` (também o texto `transfer`), `Cheque`, `Boleto`. Forma fora disso não entra no transporte.

Só saldo **positivo** é transportado. Para cada forma:

- saída no último dia do mês, categoria `Fundo de Caixa (Saída)`, descrição `Fechamento mensal — transporte {forma} ({Mês/ano})`;
- entrada no dia 1º do mês seguinte, categoria `Fundo de Caixa (Entrada)`, descrição `Abertura de mês — fundo transportado de {Mês/ano} ({forma})`.

Se já existe uma saída `Fundo de Caixa (Saída)` naquele mês e setor, o fechamento não altera nada. Ao abrir o caixa, o mês anterior é fechado sozinho quando ainda tem saldo positivo e não foi fechado.

Categorias do livro: `Entrada Cliente`, `Saída Pessoal`, `Saída Fornecedor`, `Fundo de Caixa (Entrada)`, `Fundo de Caixa (Saída)`.

### Boletos e NF

Fonte: vínculo automático em `app.py` e `Venda.aplicar_vencimento_e_prazo`.

O PDF casa com a venda pela NF normalizada. Havendo a mesma NF em datas diferentes, vale a `data_venda` mais recente. NF idêntica substitui vínculo anterior do mesmo tipo (boleto ou NF). Vínculo cujo arquivo sumiu é limpo e refeito. Boleto e NF do mesmo pedido coexistem.

No vínculo do boleto, `prazo_dias = (data_vencimento − data_venda)` em dias, só se o resultado for ≥ 0.

Alerta de boleto (push, não WhatsApp): vendas `PENDENTE` ou `PARCIAL` com `data_vencimento` anterior a hoje (vencido) ou igual a hoje.

### Romaneio

Há dois artefatos, ambos a partir dos pedidos marcados na logística.

**Texto para o entregador** (`copiarRomaneioWhatsApp` em `templates/logistica.html`). Exige ao menos um pedido. Cada produto do pedido vira uma linha; vários produtos ficam separados por vírgula e quebra de linha.

```
🚚 *ROMANEIO DE ENTREGAS* 🚚
🗓 {dd/mm/aaaa} — {N} pedido(s)

📦 *Cliente:* {nome}
📍 *Endereço:* {endereço ou "Não informado"}
🛍 *Produto:* {item},
{item}
💰 *Total:* {total}
---------------------------
```

**Impressão de remanejo** (`logistica_remanejo` + `templates/vendas/remanejo.html`). Título `Romaneio de Remanejo`. Uma linha por pedido (cliente + data; a NF entra na chave quando o CNPJ não é `0`, `00000000000000` ou vazio). Colunas: cliente e NF, endereço, produtos, quantidade somada, célula de assinatura. Sem segunda via e sem canhoto. O rodapé mostra a quantidade de pedidos, a soma dos totais em reais no rótulo "Total de itens" e o campo "Responsável pela carga".

Entrega: sem o atalho "Já entregue?", `status_entrega` nasce `PENDENTE` e o pedido entra na fila. `ENTREGUE` sai da fila de pendentes.

## 4. Mensagens de WhatsApp

Telefone: só dígitos. Se tiver até 11 dígitos (extrato e boleto do backend) ou não começar com `55` (telas de vendas e radar), o sistema prefixa `55`. Sem telefone, a ação é recusada. O extrato usa `telefone` e, na falta, `telefone_secundario`.

### Extrato de cobrança

`routes/clientes.py` (`extrato_whatsapp`). Entram vendas `PENDENTE` ou `PARCIAL`, e também as marcadas `PAGO` cujo `valor_pago` seja menor que o total − 0,01. Saem perda e total zero. Devido da linha = `calcular_total() − valor_pago`.

O HTML do extrato é salvo em disco e, se o Cloudinary estiver configurado, o link da mensagem é a URL pública. Senão, o link é o anexo do histórico ou a página do extrato.

```
Olá, tudo bem? 🧄

Segue o extrato de cobrança de *{cliente}* com total devido de R$ {1.234,56}.

📄 Acesse ou baixe o extrato aqui:
{url}

Qualquer dúvida, estamos à disposição!
```

O valor sai no formato brasileiro. A ação grava log `WHATSAPP` / `CLIENTES`.

### Boleto (rota direta)

`routes/documentos.py` (`enviar_whatsapp_boleto`). Exige boleto já vinculado em `caminho_boleto`. O valor é a soma de `calcular_total()` das linhas do mesmo cliente, mesma data e mesma NF. O link aponta para `/venda/{id}/ver_boleto`.

```
Olá, tudo bem? 🧄

Segue o link do seu boleto referente à NF {nf ou S/N} no valor de R$ {1,234.56}.

📄 Acesse ou baixe seu boleto aqui:
{url}

Qualquer dúvida, estamos à disposição!
```

Nesta rota o valor usa vírgula de milhar e ponto decimal (`:,.2f`), diferente do extrato.

### Pedido na listagem de vendas

`templates/vendas/listar.html`. Três textos, com o nome do cliente em negrito.

Resumo:

```
Olá *{cliente}*, aqui é do *Menino do Alho*! 🧄

Segue o resumo do seu pedido:
• {qtd}x {produto}

*Valor Total:* {total}
```

Nota fiscal:

```
Olá *{cliente}*, aqui é do *Menino do Alho*! 🧄

Segue a *Nota Fiscal* referente ao seu pedido (NF *{nf ou S/N}*).

📄 Acesse ou baixe a NF aqui:
{url}

Qualquer dúvida, estamos à disposição!
```

Sem URL, a linha do link vira `Número da NF: *{nf}*`.

Boleto:

```
Olá *{cliente}*, tudo bem? 🧄

Segue o boleto referente à NF *{nf ou S/N}* no valor de *{total}*.

📄 Acesse ou baixe seu boleto aqui:
{url}

Qualquer dúvida, estamos à disposição!
```

Sem boleto vinculado, o link é trocado por: `_(O arquivo do boleto ainda não está vinculado — em breve enviaremos o link.)_`.

### Radar de recompra

O calendário monta esta cobrança quando o cliente está em "É Hoje!" ou "Atrasado". O radar exige pelo menos duas compras da mesma categoria em 365 dias, intervalo mínimo de 14 dias entre a primeira e a última, e ignora bacalhau. Previsão = data da última compra + (quantidade da última compra / consumo diário das compras anteriores). Cliente cuja última compra passou de 60 dias não entra na agenda.

```
Olá {cliente}, notamos que seu estoque de {categoria} pode estar acabando. Deseja já deixar o próximo pedido no jeito?
```

### O que não vai para o WhatsApp

Boletos a vencer, radar e entregas pendentes disparam push e notificação local, não WhatsApp. Textos: `Há {N vencendo hoje} e {M vencido(s)} para acompanhar.` (título `Vencimento de Boletos`), `{N} cliente(s) no ponto de recompra (É Hoje! / Atrasado).` e `{N} entrega(s) pendente(s) na fila de logística.` Horários padrão: logística 07:30, boletos 08:00, radar 09:00, frase 06:00 (a frase sai por e-mail e push).
