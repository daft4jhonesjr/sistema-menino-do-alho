#!/usr/bin/env bash
# set -u

# ============================================================
# Roteiro de validação de segurança (semi-automatizado)
# Projeto: Menino do Alho
# ============================================================
#
# Uso:
#   chmod +x roteiro_validacao_seguranca.sh
#   BASE_URL="http://127.0.0.1:5000" \
#   USERNAME="usuario" \
#   PASSWORD="senha" \
#   API_UPLOAD_TOKEN="token_do_upload" \
#   VENDA_ID="123" \
#   CLIENTE_ID="1" \
#   PRODUTO_ID="2" \
#   ./roteiro_validacao_seguranca.sh
#
# Variáveis úteis:
#   BASE_URL                    (default: http://127.0.0.1:5000)
#   USERNAME / PASSWORD         (login para testes autenticados)
#   API_UPLOAD_TOKEN            (token da rota /upload - usa API_TOKEN no backend)
#   API_RECEBER_TOKEN_OK        (token correto para /api/receber_automatico, opcional)
#   VENDA_ID                    (para testes CSRF/path)
#   CLIENTE_ID / PRODUTO_ID     (para teste concorrência de estoque)
#

BASE_URL="${BASE_URL:-https://sistema-menino-do-alho.onrender.com}"
COOKIE_JAR="${COOKIE_JAR:-/tmp/menino_alho.cookies}"
TMP_DIR="${TMP_DIR:-/tmp/menino_alho_validacao}"

USERNAME="${USERNAME:-}"
PASSWORD="${PASSWORD:-}"
API_UPLOAD_TOKEN="${API_UPLOAD_TOKEN:-}"
API_RECEBER_TOKEN_OK="${API_RECEBER_TOKEN_OK:-}"
VENDA_ID="${VENDA_ID:-1}"
CLIENTE_ID="${CLIENTE_ID:-1}"
PRODUTO_ID="${PRODUTO_ID:-1}"

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

mkdir -p "$TMP_DIR"
rm -f "$COOKIE_JAR"

log() { echo -e "$*"; }
pass() { PASS_COUNT=$((PASS_COUNT + 1)); log "✅ PASS: $*"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); log "❌ FAIL: $*"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); log "⚠️  WARN: $*"; }

http_code() {
  # Extrai status code de arquivo de headers salvo pelo curl.
  awk 'toupper($1) ~ /^HTTP\// { code=$2 } END { print code }' "$1"
}

extract_csrf_from_html() {
  local html_file="$1"
  # tenta meta csrf primeiro
  local token
  token="$(sed -n 's/.*meta name="csrf-token" content="\([^"]*\)".*/\1/p' "$html_file" | head -n1)"
  if [ -n "$token" ]; then
    echo "$token"
    return 0
  fi
  # fallback: input hidden csrf_token
  token="$(sed -n 's/.*name="csrf_token" value="\([^"]*\)".*/\1/p' "$html_file" | head -n1)"
  echo "$token"
}

run_curl() {
  # run_curl "descricao" [curl args...]
  local desc="$1"
  shift
  local hdr="$TMP_DIR/headers_$(date +%s%N).txt"
  local body="$TMP_DIR/body_$(date +%s%N).txt"
  curl -sS -D "$hdr" -o "$body" "$@"
  local code
  code="$(http_code "$hdr")"
  log "→ $desc | HTTP $code"
  echo "$hdr|$body|$code"
}

divider() {
  log ""
  log "------------------------------------------------------------"
  log "$1"
  log "------------------------------------------------------------"
}

divider "1) SANITY CHECK"
if ! command -v curl >/dev/null 2>&1; then
  log "curl não encontrado."
  exit 1
fi

resp="$(run_curl "Health GET /login" "$BASE_URL/login")"
code="${resp##*|}"
if [ "$code" = "200" ] || [ "$code" = "302" ]; then
  pass "Aplicação responde em /login"
else
  fail "Aplicação não respondeu como esperado em /login (HTTP $code)"
fi

divider "2) /upload sem autenticação (deve bloquear)"
echo "teste" > "$TMP_DIR/teste.txt"
resp="$(run_curl "POST /upload sem login" -X POST "$BASE_URL/upload" -F "file=@$TMP_DIR/teste.txt;type=text/plain" -F "tipo=boleto")"
code="${resp##*|}"
case "$code" in
  302|401|403)
    pass "/upload bloqueado sem autenticação (HTTP $code)"
    ;;
  *)
    fail "/upload deveria bloquear sem autenticação (HTTP $code)"
    ;;
esac

divider "3) Login + CSRF"
if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
  warn "USERNAME/PASSWORD não informados. Pulando testes autenticados."
else
  LOGIN_HTML="$TMP_DIR/login_page.html"
  LOGIN_HDR="$TMP_DIR/login_page_headers.txt"
  curl -sS -c "$COOKIE_JAR" -D "$LOGIN_HDR" -o "$LOGIN_HTML" "$BASE_URL/login"
  CSRF_LOGIN="$(extract_csrf_from_html "$LOGIN_HTML")"

  if [ -z "$CSRF_LOGIN" ]; then
    fail "Não consegui extrair csrf_token da página de login."
  else
    resp="$(run_curl "POST /login" -b "$COOKIE_JAR" -c "$COOKIE_JAR" -X POST "$BASE_URL/login" \
      -d "csrf_token=$CSRF_LOGIN" -d "username=$USERNAME" -d "password=$PASSWORD" -d "remember=on")"
    code="${resp##*|}"
    if [ "$code" = "302" ] || [ "$code" = "200" ]; then
      pass "Login realizado (HTTP $code)"
    else
      fail "Falha no login (HTTP $code)"
    fi
  fi
fi

divider "4) /upload logado com arquivo inválido (deve rejeitar extensão/MIME)"
if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ]; then
  warn "Sem login, teste de upload inválido autenticado foi pulado."
else
  AUTH_HEADER=()
  if [ -n "$API_UPLOAD_TOKEN" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer $API_UPLOAD_TOKEN")
  fi
  resp="$(run_curl "POST /upload com .txt (logado)" -b "$COOKIE_JAR" -X POST "$BASE_URL/upload" \
    "${AUTH_HEADER[@]:-}" \
    -F "file=@$TMP_DIR/teste.txt;type=text/plain" -F "tipo=boleto")"
  code="${resp##*|}"
  # Possibilidades:
  # 400 -> validação de extensão/mime funcionando
  # 403 -> token do upload ausente/inválido
  # 503 -> API_TOKEN não configurado no backend
  case "$code" in
    400) pass "Upload inválido rejeitado por extensão/MIME (HTTP 400)" ;;
    403) pass "Upload bloqueado por token de integração (HTTP 403)" ;;
    503) pass "Upload bloqueado por token de integração não configurado (HTTP 503)" ;;
    *) fail "Resultado inesperado no upload inválido autenticado (HTTP $code)" ;;
  esac
fi

divider "5) /api/receber_automatico (token)"
resp="$(run_curl "POST /api/receber_automatico sem token" -X POST "$BASE_URL/api/receber_automatico")"
code="${resp##*|}"
if [ "$code" = "503" ] || [ "$code" = "403" ]; then
  pass "API automática bloqueou sem token/sem config (HTTP $code)"
else
  fail "API automática deveria bloquear sem token (HTTP $code)"
fi

resp="$(run_curl "POST /api/receber_automatico token errado" -X POST "$BASE_URL/api/receber_automatico" \
  -H "Authorization: Bearer token_errado")"
code="${resp##*|}"
if [ "$code" = "403" ] || [ "$code" = "503" ]; then
  pass "API automática bloqueou token inválido (HTTP $code)"
else
  fail "API automática deveria bloquear token inválido (HTTP $code)"
fi

if [ -n "$API_RECEBER_TOKEN_OK" ]; then
  resp="$(run_curl "POST /api/receber_automatico token correto (sem arquivo)" -X POST "$BASE_URL/api/receber_automatico" \
    -H "Authorization: Bearer $API_RECEBER_TOKEN_OK")"
  code="${resp##*|}"
  if [ "$code" = "400" ]; then
    pass "Token correto aceito e validação de arquivo acionada (HTTP 400 esperado)"
  else
    warn "Token correto não produziu 400 esperado (HTTP $code). Verifique configuração."
  fi
fi

divider "6) CSRF em endpoint AJAX"
if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ] || [ -z "$VENDA_ID" ]; then
  warn "Sem USER/PASS ou VENDA_ID, teste de CSRF AJAX foi pulado."
else
  DASH_HTML="$TMP_DIR/dashboard.html"
  curl -sS -b "$COOKIE_JAR" -o "$DASH_HTML" "$BASE_URL/dashboard"
  CSRF_META="$(extract_csrf_from_html "$DASH_HTML")"

  resp="$(run_curl "POST atualizar_situacao_rapida sem X-CSRFToken" -b "$COOKIE_JAR" \
    -X POST "$BASE_URL/vendas/$VENDA_ID/atualizar_situacao_rapida" \
    -H "Content-Type: application/json" \
    -d '{"situacao":"PAGO"}')"
  code="${resp##*|}"
  if [ "$code" = "400" ] || [ "$code" = "403" ]; then
    pass "Endpoint AJAX bloqueou sem CSRF (HTTP $code)"
  else
    warn "Sem CSRF não bloqueou como esperado (HTTP $code). Revisar política."
  fi

  if [ -n "$CSRF_META" ]; then
    resp="$(run_curl "POST atualizar_situacao_rapida com X-CSRFToken" -b "$COOKIE_JAR" \
      -X POST "$BASE_URL/vendas/$VENDA_ID/atualizar_situacao_rapida" \
      -H "Content-Type: application/json" \
      -H "X-CSRFToken: $CSRF_META" \
      -d '{"situacao":"PENDENTE"}')"
    code="${resp##*|}"
    if [ "$code" = "200" ] || [ "$code" = "403" ]; then
      pass "Endpoint respondeu com CSRF (HTTP $code; 403 pode ser regra de permissão)"
    else
      fail "Resposta inesperada com CSRF no endpoint AJAX (HTTP $code)"
    fi
  else
    warn "Não foi possível extrair CSRF meta do dashboard."
  fi
fi

divider "7) Concorrência de estoque (simples)"
if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ] || [ -z "$CLIENTE_ID" ] || [ -z "$PRODUTO_ID" ]; then
  warn "Sem CLIENTE_ID/PRODUTO_ID ou credenciais, teste de concorrência foi pulado."
else
  DASH_HTML="$TMP_DIR/dashboard2.html"
  curl -sS -b "$COOKIE_JAR" -o "$DASH_HTML" "$BASE_URL/dashboard"
  CSRF_META="$(extract_csrf_from_html "$DASH_HTML")"
  if [ -z "$CSRF_META" ]; then
    warn "Sem CSRF para teste de concorrência. Pulando."
  else
    OUT1="$TMP_DIR/concurrency_1.txt"
    OUT2="$TMP_DIR/concurrency_2.txt"

    curl -sS -b "$COOKIE_JAR" -X POST "$BASE_URL/add_venda" \
      -H "X-CSRFToken: $CSRF_META" \
      -d "cliente_id=$CLIENTE_ID" \
      -d "produto_id=$PRODUTO_ID" \
      -d "quantidade_venda=1" \
      -d "preco_venda=100,00" \
      -d "empresa_faturadora=PATY" \
      -d "situacao=PENDENTE" > "$OUT1" &
    PID1=$!

    curl -sS -b "$COOKIE_JAR" -X POST "$BASE_URL/add_venda" \
      -H "X-CSRFToken: $CSRF_META" \
      -d "cliente_id=$CLIENTE_ID" \
      -d "produto_id=$PRODUTO_ID" \
      -d "quantidade_venda=1" \
      -d "preco_venda=100,00" \
      -d "empresa_faturadora=PATY" \
      -d "situacao=PENDENTE" > "$OUT2" &
    PID2=$!

    wait "$PID1" "$PID2"
    pass "Disparo concorrente executado (verifique se apenas uma operação foi aceita)."
    log "Arquivos de saída: $OUT1 | $OUT2"
  fi
fi

divider "8) Resultado final"
log "PASS: $PASS_COUNT"
log "FAIL: $FAIL_COUNT"
log "WARN: $WARN_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  log ""
  log "Resultado: FALHOU (há falhas críticas para revisar)."
  exit 2
fi

log ""
log "Resultado: OK (sem falhas críticas no roteiro automatizado)."
exit 0
