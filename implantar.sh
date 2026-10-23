#!/usr/bin/env bash
# ==============================================================================
# implantar.sh — Script de implantação idempotente do Status da Arateki
# ==============================================================================
# Executa a implantação completa via AWS CloudFormation e publica o código e a página.
# Não armazena nem commita segredos ou e-mails.
#
# Uso:
#   ./implantar.sh [--emails "admin@arateki.com,dev@arateki.com"] [--regiao us-east-2]
#                  [--r2-endpoint "https://<id>.r2.cloudflarestorage.com"]
#                  [--r2-key "KEY"] [--r2-secret "SECRET"] [--bucket "nome-do-bucket"]
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
REGIAO="${AWS_REGION:-us-east-2}"
ALERT_EMAILS="${ALERT_EMAILS:-}"
R2_ENDPOINT_URL="${R2_ENDPOINT_URL:-}"
R2_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-}"
R2_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-}"
BUCKET_NAME="${BUCKET_NAME:-}"

# Processamento de argumentos
while [[ $# -gt 0 ]]; do
  case "$1" in
    --emails)
      ALERT_EMAILS="$2"
      shift 2
      ;;
    --regiao)
      REGIAO="$2"
      shift 2
      ;;
    --r2-endpoint)
      R2_ENDPOINT_URL="$2"
      shift 2
      ;;
    --r2-key)
      R2_ACCESS_KEY_ID="$2"
      shift 2
      ;;
    --r2-secret)
      R2_SECRET_ACCESS_KEY="$2"
      shift 2
      ;;
    --bucket)
      BUCKET_NAME="$2"
      shift 2
      ;;
    --ajuda|-h)
      echo "Uso: $0 [--emails 'email1,email2'] [--regiao us-east-2] [--r2-endpoint URL] [--r2-key KEY] [--r2-secret SECRET] [--bucket BUCKET]"
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

# 4. Monta parâmetros para o deploy
PARAMS=()
[[ -n "$EMAIL1" ]] && PARAMS+=("AlertEmail1=$EMAIL1")
[[ -n "$EMAIL2" ]] && PARAMS+=("AlertEmail2=$EMAIL2")
[[ -n "$BUCKET_NAME" ]] && PARAMS+=("BucketName=$BUCKET_NAME")
[[ -n "$R2_ENDPOINT_URL" ]] && PARAMS+=("R2EndpointUrl=$R2_ENDPOINT_URL")
[[ -n "$R2_ACCESS_KEY_ID" ]] && PARAMS+=("R2AccessKeyId=$R2_ACCESS_KEY_ID")
[[ -n "$R2_SECRET_ACCESS_KEY" ]] && PARAMS+=("R2SecretAccessKey=$R2_SECRET_ACCESS_KEY")

# 5. Executa deploy da Stack CloudFormation
echo "--> Executando CloudFormation deploy (idempotente)..."
DEPLOY_CMD=(
  "$AWS_CLI" cloudformation deploy
  --template-file infra/template.yaml
  --stack-name "$STACK_NAME"
  --region "$REGIAO"
  --capabilities CAPABILITY_NAMED_IAM
  --no-fail-on-empty-changeset
)

if [[ ${#PARAMS[@]} -gt 0 ]]; then
  DEPLOY_CMD+=(--parameter-overrides "${PARAMS[@]}")
fi

"${DEPLOY_CMD[@]}"
echo "    Stack CloudFormation atualizada com sucesso!"

# 6. Empacota o código da Lambda checadora
echo "--> Empacotando código Python da Lambda..."
TMP_ZIP="/tmp/arateki-status-checador.zip"
rm -f "$TMP_ZIP"

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
echo "    Pacote gerado em $TMP_ZIP ($(stat -c%s "$TMP_ZIP") bytes)."

# 7. Atualiza o código da Lambda
echo "--> Atualizando código da função Lambda arateki-status-checador..."
"$AWS_CLI" lambda update-function-code \
  --function-name "arateki-status-checador" \
  --region "$REGIAO" \
  --zip-file "fileb://$TMP_ZIP" >/dev/null
echo "    Código da Lambda atualizado!"

# 8. Publica a página web (index.html) no bucket
echo "--> Obtendo bucket de armazenamento da stack..."
BUCKET_RESOLVIDO="$BUCKET_NAME"
if [[ -z "$BUCKET_RESOLVIDO" ]]; then
  BUCKET_RESOLVIDO=$("$AWS_CLI" cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$REGIAO" \
    --query "Stacks[0].Outputs[?OutputKey=='StatusBucketName'].OutputValue" \
    --output text)
fi

if [[ -n "$BUCKET_RESOLVIDO" && "$BUCKET_RESOLVIDO" != "None" ]]; then
  echo "--> Publicando pagina/index.html no bucket $BUCKET_RESOLVIDO..."
  "$AWS_CLI" s3 cp pagina/index.html "s3://$BUCKET_RESOLVIDO/index.html" \
    --region "$REGIAO" \
    --content-type "text/html; charset=utf-8" \
    --cache-control "public, max-age=3600"
  echo "    Página publicada com sucesso!"
fi

# 9. Executa a Lambda uma primeira vez para gerar o status.json inicial
echo "--> Executando primeira checagem de aquecimento na Lambda..."
INVOKE_OUTPUT="/tmp/arateki-status-invoke.json"
"$AWS_CLI" lambda invoke \
  --function-name "arateki-status-checador" \
  --region "$REGIAO" \
  "$INVOKE_OUTPUT" >/dev/null || true
if [[ -f "$INVOKE_OUTPUT" ]]; then
  cat "$INVOKE_OUTPUT"
  echo ""
  rm -f "$INVOKE_OUTPUT"
fi

rm -f "$TMP_ZIP"

echo "=== Implantação concluída com sucesso! ==="
echo "Painel configurado para rodar automaticamente a cada 1 minuto."
