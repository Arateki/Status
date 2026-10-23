#!/usr/bin/env bash
# ==============================================================================
# implantar.sh — Script de implantação idempotente do Status da Arateki
# ==============================================================================
# Executa a implantação completa via AWS CloudFormation e publica o código e a página.
# Credenciais e parâmetros sensíveis são lidos exclusivamente de variáveis de ambiente.
#
# Variáveis de ambiente esperadas:
#   ALERT_EMAILS           (opcional, ex: "email1@exemplo.com,email2@exemplo.com")
#   BUCKET_NAME            (opcional, padrão: "arateki-status")
#   R2_ENDPOINT_URL        (obrigatório, ex: "https://<account_id>.r2.cloudflarestorage.com")
#   R2_ACCESS_KEY_ID       (obrigatório, chave de acesso do Cloudflare R2)
#   R2_SECRET_ACCESS_KEY   (obrigatório, segredo de acesso do Cloudflare R2)
#
# Uso:
#   ./implantar.sh [--regiao us-east-2]
# ==============================================================================

set -euo pipefail

# 1. Determina o binário da AWS CLI
if [[ -x "/srv/dev/ferramentas/bin/aws" ]]; then
  AWS_CLI="/srv/dev/ferramentas/bin/aws"
elif command -v aws >/dev/null 2>&1; then
  AWS_CLI="aws"
else
  echo "ERRO: AWS CLI não encontrada em /srv/dev/ferramentas/bin/aws nem no PATH." >&2
  exit 1
fi

STACK_NAME="arateki-status"
REGIAO="us-east-2"
ALERT_EMAILS="${ALERT_EMAILS:-}"
R2_ENDPOINT_URL="${R2_ENDPOINT_URL:-}"
R2_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-}"
R2_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-}"
BUCKET_NAME="${BUCKET_NAME:-arateki-status}"

# Processamento de argumentos (somente opções não sensíveis)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --regiao)
      REGIAO="$2"
      shift 2
      ;;
    --ajuda|-h)
      echo "Uso: $0 [--regiao us-east-2]"
      echo ""
      echo "Configure as variáveis de ambiente antes de executar:"
      echo "  ALERT_EMAILS, BUCKET_NAME, R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY"
      exit 0
      ;;
    *)
      echo "Argumento desconhecido: $1" >&2
      exit 1
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Status da Arateki — Implantação ==="
echo "Região: $REGIAO"
echo "Stack:  $STACK_NAME"
echo "Bucket: $BUCKET_NAME"

# 2. Divide a lista de e-mails de alerta (suporta até 2 na CloudFormation)
EMAIL1=""
EMAIL2=""
if [[ -n "$ALERT_EMAILS" ]]; then
  IFS=',' read -r -a EMAILS_ARRAY <<< "$ALERT_EMAILS"
  EMAIL1="${EMAILS_ARRAY[0]:-}"
  EMAIL2="${EMAILS_ARRAY[1]:-}"
fi

# 3. Validação do template CloudFormation
echo "--> Validando template CloudFormation..."
"$AWS_CLI" cloudformation validate-template \
  --region "$REGIAO" \
  --template-body file://infra/template.yaml >/dev/null
echo "    Template válido!"

# 4. Cria arquivo JSON de parâmetros com umask restrita (077) e remoção segura via trap
OLD_UMASK=$(umask)
umask 077
PARAMS_FILE=$(mktemp /tmp/cf-params.XXXXXX.json)
TMP_ZIP=$(mktemp /tmp/arateki-checador.XXXXXX.zip)
umask "$OLD_UMASK"

trap 'rm -f "$PARAMS_FILE" "$TMP_ZIP"' EXIT INT TERM

# Os valores vão pelo ambiente do processo (não pela linha de comando, que aparece no `ps`).
BUCKET_NAME="$BUCKET_NAME" EMAIL1="$EMAIL1" EMAIL2="$EMAIL2" PARAMS_FILE="$PARAMS_FILE" \
R2_ENDPOINT_URL="$R2_ENDPOINT_URL" R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
python3 - <<'PY'
import json, os
e = os.environ
pares = (('BucketName', 'BUCKET_NAME'), ('R2EndpointUrl', 'R2_ENDPOINT_URL'), ('R2AccessKeyId', 'R2_ACCESS_KEY_ID'),
         ('R2SecretAccessKey', 'R2_SECRET_ACCESS_KEY'), ('AlertEmail1', 'EMAIL1'), ('AlertEmail2', 'EMAIL2'))
params = [{'ParameterKey': chave, 'ParameterValue': e.get(var, '')} for chave, var in pares]
with open(e['PARAMS_FILE'], 'w', encoding='utf-8') as f:
    json.dump(params, f)
PY

# 5. Executa deploy da Stack CloudFormation usando arquivo de parâmetros
echo "--> Executando CloudFormation deploy (idempotente)..."
"$AWS_CLI" cloudformation deploy \
  --template-file infra/template.yaml \
  --stack-name "$STACK_NAME" \
  --region "$REGIAO" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides "file://$PARAMS_FILE" \
  --no-fail-on-empty-changeset
echo "    Stack CloudFormation atualizada com sucesso!"

# 6. Empacota o código da Lambda checadora
echo "--> Empacotando código Python da Lambda..."
(
  cd "$SCRIPT_DIR"
  python3 -c "
import zipfile, os

with zipfile.ZipFile('$TMP_ZIP', 'w', zipfile.ZIP_DEFLATED) as z:
    for raiz, _, arquivos in os.walk('checador'):
        for arq in arquivos:
            if arq.endswith(('.py', '.json')) and not arq.startswith('.'):
                caminho = os.path.join(raiz, arq)
                z.write(caminho)
"
)
echo "    Pacote gerado ($(stat -c%s "$TMP_ZIP") bytes)."

# 7. Atualiza o código da Lambda
echo "--> Atualizando código da função Lambda arateki-status-checador..."
"$AWS_CLI" lambda update-function-code \
  --function-name "arateki-status-checador" \
  --region "$REGIAO" \
  --zip-file "fileb://$TMP_ZIP" >/dev/null
echo "    Código da Lambda atualizado!"

# 8. Publica a página web (index.html) no Cloudflare R2
if [[ -n "$R2_ENDPOINT_URL" && -n "$R2_ACCESS_KEY_ID" && -n "$R2_SECRET_ACCESS_KEY" ]]; then
  echo "--> Publicando pagina/index.html no Cloudflare R2 ($BUCKET_NAME)..."
  env -u AWS_PROFILE -u AWS_SESSION_TOKEN AWS_REQUEST_CHECKSUM_CALCULATION=when_required \
    AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    "$AWS_CLI" s3 cp pagina/index.html "s3://$BUCKET_NAME/index.html" \
    --endpoint-url "$R2_ENDPOINT_URL" \
    --content-type "text/html; charset=utf-8" \
    --cache-control "public, max-age=300"
  echo "    Página publicada com sucesso no R2!"
else
  echo "AVISO: Credenciais de R2 não configuradas; upload de index.html pulado."
fi

# 9. Executa a Lambda uma primeira vez para aquecimento e geração do status.json inicial
echo "--> Executando primeira checagem de aquecimento na Lambda..."
INVOKE_OUTPUT=$(mktemp /tmp/arateki-invoke.XXXXXX.json)
"$AWS_CLI" lambda invoke \
  --function-name "arateki-status-checador" \
  --region "$REGIAO" \
  "$INVOKE_OUTPUT" >/dev/null || true
if [[ -f "$INVOKE_OUTPUT" ]]; then
  cat "$INVOKE_OUTPUT"
  echo ""
  rm -f "$INVOKE_OUTPUT"
fi

echo "=== Implantação concluída com sucesso! ==="
echo "Painel configurado para rodar automaticamente a cada 1 minuto."
