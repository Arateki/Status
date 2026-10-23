# Status da Arateki

Painel público e leve de monitoramento de disponibilidade (`https://status.arateki.com`) alimentado por AWS Lambda e servido via Cloudflare.

O sistema monitora os serviços da Arateki a cada 1 minuto, registra o histórico no DynamoDB, gera um `status.json` estático e envia alertas por e-mail via SNS apenas na confirmação de queda (2 falhas consecutivas) e no retorno (1 sucesso).

**Sem computação por visita:** a página é estática e lê o `status.json` pré-calculado em cache de borda pelo Cloudflare, protegendo a infraestrutura contra custos e picos de acessos.

---

## Tabela de Custo Mensal Estimado

| Componente | Uso Mensal | Franquia Gratuita Permanente | Custo Estimado |
|---|---|---|---|
| **AWS Lambda** (arm64, 128 MB) | 43.200 invocações (~5.400 GB-s) | 1.000.000 invocações e 3.200.000 GB-s | **US$ 0,00** |
| **EventBridge Scheduler** | 43.200 execuções (1/min) | 14.000.000 invocações | **US$ 0,00** |
| **Amazon DynamoDB** (provisionado) | 1 RCU / 1 WCU, ~20 MB (TTL 90d) | 25 RCU / 25 WCU e 25 GB | **US$ 0,00** |
| **Amazon SNS** (alertas por e-mail) | < 50 notificações/mês | 100.000 publicações e 1.000 e-mails | **US$ 0,00** |
| **CloudWatch Logs** (retenção 7d) | ~40 MB/mês | 5 GB de ingestão e armazenamento | **US$ 0,00** |
| **Cloudflare** (DNS, CDN, WAF) | Ilimitado | Tráfego e mitigação DDoS não dosados | **US$ 0,00** |
| **Armazenamento (R2)** *(Recomendado)* | 43.200 escritas, 43.200 leituras | 1M Class A (escrita), 10M Class B (leitura) | **US$ 0,00** |
| **Armazenamento (S3)** *(Alternativa)* | 43.200 PUTs, ~43.200 GETs | Sem franquia permanente de PUTs | ~US$ 0,22 |

**Custo Total Normal:**
- Com Cloudflare R2: **US$ 0,00 por mês** (~R$ 0,00).
- Com AWS S3: **~US$ 0,22 por mês** (~R$ 1,20).

### Custo no Pior Caso (Ataque de 100 milhões de requisições)
Graças às regras de cache do Cloudflare (Edge TTL de 60 s para `status.json` e 1 dia para `index.html` ignorando query strings), 100 milhões de acessos são absorvidos 100% na borda do Cloudflare a **US$ 0,00**.
- Se ocorresse bypass total da borda direto no **Cloudflare R2**: 10M leituras grátis + 90M × US$ 0,36/1M = **US$ 32,40** (com egress US$ 0,00).
- Se ocorresse bypass no **AWS S3**: 100M GETs (US$ 40,00) + ~500 GB de egress a US$ 0,09/GB (US$ 36,00) = **~US$ 76,00 a US$ 121,00**.

---

## Estrutura do Repositório

```
.
├── checador/
│   ├── config.py             # Configurações gerais e User-Agent
│   ├── sistemas.json         # Lista de sistemas monitorados
│   ├── checker.py            # Checagem HTTP em paralelo com timeout de 10s
│   ├── evaluator.py          # Regra das 2 falhas, volta com 1 ok e cálculos
│   ├── storage.py            # Persistência no DynamoDB e upload do status.json
│   ├── notifier.py           # Envio de alertas via Amazon SNS
│   └── lambda_handler.py     # Ponto de entrada da Lambda
├── pagina/
│   └── index.html            # Interface web estática única (HTML + CSS + JS)
├── infra/
│   └── template.yaml         # CloudFormation (DynamoDB, Lambda, Scheduler, SNS, S3)
├── testes/                   # Suíte de testes unitários em Python
├── implantar.sh              # Script idempotente de deploy via AWS CLI
└── LEIA-ME.md
```

---

## Regra de Bloqueio do Cloudflare e User-Agent

O Cloudflare costuma bloquear requisições de bibliotecas padrão (como `urllib` ou `curl`) com **HTTP 403**. Por isso:

1. O checador envia obrigatoriamente o cabeçalho:
   `User-Agent: AratekiStatus/1.0 (+https://status.arateki.com)`
2. Se receber 403 com indicação do Cloudflare (`Server: cloudflare`, `cf-ray`), o checador classifica internamente como **erro do checador**, e **não** como queda do sistema vigiado (não dispara alertas indevidos).
3. **Como liberar permanentemente no Cloudflare do sistema vigiado:**
   - Acesse o painel do Cloudflare do domínio vigiado (ex: `arateki.com`).
   - Vá em **Security** ➔ **WAF** ➔ **Custom Rules**.
   - Crie uma regra:
     - **Field:** `User Agent`
     - **Operator:** `equals`
     - **Value:** `AratekiStatus/1.0 (+https://status.arateki.com)`
     - **Action:** `Skip` ➔ marque todas as checagens (WAF, Bot Management, Rate Limiting).

---

## Como Implantar

### Pré-requisitos
- AWS CLI configurada (`/srv/dev/ferramentas/bin/aws` ou no `PATH`).
- Permissões para CloudFormation, IAM, Lambda, DynamoDB, SNS e S3 na região `us-east-2`.

### Opção 1 — Com Cloudflare R2 (Recomendado, Custo Zero)
1. Crie um bucket no Cloudflare R2 (ex: `arateki-status`) e vincule o domínio personalizado `status.arateki.com`.
2. No Cloudflare, configure uma **Cache Rule** para `status.arateki.com`:
   - Edge Cache TTL: 60 s para `status.json`, 1 dia para `index.html`.
   - Cache Key: Ignore query strings.
3. Gere uma API Token do R2 com permissão de leitura/escrita no bucket.
4. Execute o script de implantação:
   ```bash
   ./implantar.sh \
     --emails "admin@arateki.com,dev@arateki.com" \
     --regiao us-east-2 \
     --bucket "arateki-status" \
     --r2-endpoint "https://<account_id>.r2.cloudflarestorage.com" \
     --r2-key "SUA_ACCESS_KEY_R2" \
     --r2-secret "SUA_SECRET_KEY_R2"
   ```

### Opção 2 — Com AWS S3 (100% nativo na AWS)
```bash
./implantar.sh \
  --emails "admin@arateki.com,dev@arateki.com" \
  --regiao us-east-2
```
O CloudFormation criará automaticamente o bucket `arateki-status-<account-id>` e publicará `pagina/index.html` e `status.json`.

---

## Como Adicionar um Novo Sistema

Edite o arquivo `checador/sistemas.json` e adicione a entrada:

```json
{
  "id": "novo-servico",
  "nome": "Meu Novo Serviço",
  "url": "https://meuservico.arateki.com/health",
  "espera_codigo": 200,
  "espera_corpo": "ok"
}
```

Em seguida, execute `./implantar.sh` para atualizar o código da Lambda. As checagens passam a incluir o novo sistema imediatamente no próximo minuto.

---

## Testes Unitários

Para rodar toda a suíte de testes unitários:

```bash
python3 -m unittest discover -s testes -p "test_*.py" -v
```

A suíte cobre:
- Regra das 2 falhas para queda e volta imediata com 1 ok.
- Não disparo de alerta na 1ª falha ou após a 2ª falha persistente.
- Detecção e não-derrubada de sistemas em caso de bloqueio 403 do Cloudflare.
- Cálculo de disponibilidade percentual para 24h, 7d, 30d e 90d.
- Execução concorrente em paralelo de todas as checagens.
- Timeout estrito por sistema.
- Sanitização de dados em `status.json` e serialização do histórico.

---

## Como Apagar Tudo (Desimplantação)

Para remover todos os recursos da AWS criados pelo projeto:

1. Esvazie os objetos do bucket S3 (se aplicável):
   ```bash
   aws s3 rm s3://arateki-status-<account-id> --recursive
   ```
2. Exclua a stack do CloudFormation:
   ```bash
   aws cloudformation delete-stack --stack-name arateki-status --region us-east-2
   ```
3. Aguarde a finalização da exclusão:
   ```bash
   aws cloudformation wait stack-delete-complete --stack-name arateki-status --region us-east-2
   ```
Todos os recursos (tabela DynamoDB, Lambda, Scheduler, Tópico SNS e IAM Roles) serão desprovisionados sem deixar resíduos.
