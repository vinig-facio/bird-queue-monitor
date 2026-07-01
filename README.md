# 🐦 Bird Queue Monitor

Monitor em tempo real da fila de atendimento do Bird, com alertas no Slack e dashboard no Google Sheets.

## 🎯 Funcionalidades

- ✅ Login automático no Bird via Playwright
- ✅ Captura do total de clientes na fila via API
- ✅ Cache de token (reduz logins desnecessários)
- ✅ Integração com Google Sheets (dashboard)
- ✅ Alertas no Slack quando fila atinge thresholds
- ✅ Execução automática via GitHub Actions (a cada 2 minutos)

## 🔒 Segurança

- Credenciais armazenadas em variáveis de ambiente (`.env`)
- Token JWT cacheado localmente com expiração de 24h
- `.gitignore` configurado para NUNCA expor senhas
- Secrets do GitHub para CI/CD

## 📊 Stack

- **Python 3.11+**
- **Playwright** (automação do login)
- **Requests** (consumo da API)
- **Google Sheets API** (dashboard)
- **Slack Webhook** (alertas)

## 🛠️ Instalação Local

```bash
# Clone o repositório
git clone https://github.com/vinig-facio/bird-queue-monitor.git
cd bird-queue-monitor

# Instale as dependências
pip install -r requirements.txt
playwright install chromium

# Crie o arquivo .env com suas credenciais
# (NUNCA commite este arquivo!)
echo "BIRD_EMAIL=seu.email@empresa.com" > .env
echo "BIRD_PASSWORD=sua_senha" >> .env

# Execute
python main.py --test
```
