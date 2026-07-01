import os
import json
import logging
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

load_dotenv()

# ========== CONFIGURAÇÕES ==========
MONITOR_INTERVAL = 2  # minutos
HORA_INICIO = 9  # 09:00 - início
HORA_FIM = 18  # 18:00 - fim do expediente
HARDCAP_HORA = 22  # 22:00 - hard stop (segurança)
REQUEST_TIMEOUT = 15
TOKEN_EXPIRY_HOURS = 23
REQUEST_RETRIES = 2
MAX_PAGINATION_PAGES = 50
# ====================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-5s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


class BirdMonitor:
    def __init__(self):
        self.email = os.getenv('BIRD_EMAIL')
        self.password = os.getenv('BIRD_PASSWORD')
        self.sheets_url = os.getenv('GOOGLE_APPS_SCRIPT_URL')
        self.token = None
        self.token_file = Path('token.json')
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': 'Bird-Monitor/1.0',
            'Accept': 'application/json'
        })
        self._validate_config()

    def _validate_config(self):
        if not self.email or '@' not in self.email:
            raise ValueError("❌ BIRD_EMAIL inválido ou não configurado no .env")
        if not self.password:
            raise ValueError("❌ BIRD_PASSWORD não configurada no .env")
        if not self.sheets_url or 'script.google.com' not in self.sheets_url:
            raise ValueError("❌ GOOGLE_APPS_SCRIPT_URL inválida ou não configurada")
        logger.debug("✅ Configurações validadas")

    # ========== LOGIN (SEU ORIGINAL QUE FUNCIONAVA) ==========
    def login_and_get_token(self):
        """Faz login uma vez e salva o token por 24h"""

        if self.token_file.exists():
            with open(self.token_file, 'r') as f:
                data = json.load(f)
                if datetime.fromisoformat(data['expiry']) > datetime.now():
                    self.token = data['token']
                    logger.info("Token reutilizado do cache")
                    return self.token

        logger.info("Fazendo login no Bird...")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu'
                    ]
                )
                context = browser.new_context()
                page = context.new_page()

                try:
                    # ETAPA 1: Página de login
                    page.goto('https://app.bird.com/auth/login')
                    page.wait_for_timeout(3000)

                    page.fill('input[name="email"]', self.email)
                    page.click('button[type="submit"]')
                    page.wait_for_timeout(2000)

                    # ETAPA 2: Continuar com senha
                    page.click('button:has-text("Continuar com senha")')
                    page.wait_for_timeout(2000)

                    # ETAPA 3: Senha e login
                    page.fill('input[name="password"]', self.password)
                    page.click('button[type="submit"]')
                    page.wait_for_timeout(5000)

                    # Verifica login
                    try:
                        page.wait_for_selector('text=workspaces', timeout=10000)
                        logger.info("Login realizado com sucesso")
                    except PlaywrightTimeout:
                        logger.warning("Verificação de login alternativa...")
                        page.wait_for_timeout(3000)

                    # Vai para a fila
                    logger.debug("Navegando para a fila...")
                    page.goto(
                        'https://app.bird.com/workspaces/dbd7eacd-6312-441f-86dd-d933200b3e3f/'
                        'inbox/cs-inbox/feed/queue%3A01984c00-922a-7bb5-aa7a-624fb399892c'
                    )
                    page.wait_for_timeout(5000)

                    # Captura o token (apenas de api.bird.com)
                    captured_token = {'value': None}

                    def capture_token(request):
                        if 'api.bird.com' in request.url:
                            auth = request.headers.get('authorization', '')
                            if auth.startswith('Bearer ') and not captured_token['value']:
                                captured_token['value'] = auth.replace('Bearer ', '')
                                logger.debug("Token capturado com sucesso")

                    page.on('request', capture_token)
                    page.reload()
                    page.wait_for_timeout(5000)

                    self.token = captured_token['value']

                    if self.token:
                        expiry = datetime.now() + timedelta(hours=23)
                        with open(self.token_file, 'w') as f:
                            json.dump({
                                'token': self.token,
                                'expiry': expiry.isoformat()
                            }, f)
                        logger.info(f"Token salvo (válido até {expiry.strftime('%H:%M')})")
                        return self.token
                    else:
                        logger.error("Token não encontrado após login")
                        return None

                finally:
                    browser.close()

        except PlaywrightTimeout as e:
            logger.error(f"Timeout no Playwright: {e}")
            return None
        except Exception as e:
            logger.error(f"Erro inesperado no login: {type(e).__name__}")
            logger.debug(f"Detalhes: {e}")
            return None

    # ========== BUSCA TODOS OS ITENS COM PAGE TOKEN ==========
    def _fetch_all_items(self) -> Tuple[Optional[List], Optional[int]]:
        """Busca TODOS os itens da fila usando paginação com pageToken"""
        if not self.token:
            # Tenta obter token
            self.token = self.login_and_get_token()
            if not self.token:
                return None, None

        headers = {"Authorization": f"Bearer {self.token}"}

        all_items = []
        seen_ids = set()
        total_oficial = 0
        next_token = None
        page = 1

        try:
            while page <= MAX_PAGINATION_PAGES:
                # Constrói URL base
                url = (
                    f"https://api.bird.com/workspaces/dbd7eacd-6312-441f-86dd-d933200b3e3f/"
                    f"feeds/queue:01984c00-922a-7bb5-aa7a-624fb399892c/items"
                    f"?sortBy=lastActivity"
                    f"&dateRange=%7B%7D"
                    f"&participants=%7B%22agents%22:%5B%5D,%22contacts%22:%5B%5D%7D"
                    f"&searchTerm="
                    f"&limit=20"
                )

                # Adiciona pageToken se existir
                if next_token:
                    url += f"&pageToken={next_token}"

                resp = self._session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

                if resp.status_code == 401:
                    logger.info("Token expirado. Renovando...")
                    self.token_file.unlink(missing_ok=True)
                    self.token = None
                    self.token = self.login_and_get_token()
                    if self.token:
                        headers = {"Authorization": f"Bearer {self.token}"}
                        continue
                    else:
                        return None, None

                if resp.status_code != 200:
                    logger.error(f"Erro HTTP {resp.status_code}")
                    return None, None

                data = resp.json()

                if page == 1:
                    total_oficial = data.get('total', 0)
                    if total_oficial == 0:
                        logger.info("📭 Fila vazia")
                        return [], 0
                    logger.info(f"Total oficial da fila: {total_oficial} clientes")

                results = data.get('results', [])
                if not results:
                    break

                for item in results:
                    item_id = item.get('id')
                    if item_id and item_id not in seen_ids:
                        seen_ids.add(item_id)
                        all_items.append(item)

                next_token = data.get('nextPageToken')
                if not next_token:
                    break

                page += 1

            if total_oficial > 0:
                logger.info(f"✅ Paginação finalizada: {len(all_items)} itens")
            return all_items, total_oficial

        except Exception as e:
            logger.error(f"Erro na paginação: {e}")
            return None, None

    def check_queue(self) -> Optional[Dict[str, Any]]:
        if not self.token:
            self.token = self.login_and_get_token()
            if not self.token:
                logger.error("❌ Sem token, não é possível verificar fila")
                return None

        items, total = self._fetch_all_items()
        if items is None:
            return None

        if total == 0 or len(items) == 0:
            return {
                'data': [0, 0, 0, 0, 0, 0],
                'timestamp': datetime.now().isoformat()
            }

        aguardando = 0
        com_bot = 0
        em_atendimento = 0
        sla_estourado = 0
        tempos_espera = []
        now = datetime.now()

        for item in items:
            agent = item.get('agent')
            if not agent:
                aguardando += 1
            elif agent.get('name') == 'Facinho OOB':
                com_bot += 1
            else:
                em_atendimento += 1

            # SLA baseado no slaPolicy.timers
            sla_policy = item.get('slaPolicy', {})
            for timer in sla_policy.get('timers', []):
                if (timer.get('metric') == 'firstReplyTime' and
                        timer.get('status') == 'expired'):
                    sla_estourado += 1
                    break

            queue_info = item.get('queueInfo', {})
            queued_at = queue_info.get('queuedAt')

            if queued_at:
                try:
                    if queued_at.endswith('Z'):
                        queued_at = queued_at.replace('Z', '+00:00')
                    queued = datetime.fromisoformat(queued_at)
                    if queued.tzinfo:
                        queued_utc = queued.astimezone(timezone.utc)
                        espera_min = (datetime.now(timezone.utc) - queued_utc).total_seconds() / 60
                    else:
                        espera_min = (now - queued).total_seconds() / 60
                    if espera_min > 0:
                        tempos_espera.append(espera_min)
                except Exception:
                    pass

        tempo_medio = round(sum(tempos_espera) / len(tempos_espera), 1) if tempos_espera else 0

        return {
            'data': [aguardando, com_bot, em_atendimento, sla_estourado, total, tempo_medio],
            'timestamp': now.isoformat()
        }

    def send_to_sheets(self, dados: Dict[str, Any]) -> bool:
        if not self.sheets_url:
            return False

        try:
            resp = self._session.post(
                self.sheets_url,
                json={'data': dados['data'][:6]},
                headers={'Content-Type': 'application/json'},
                timeout=10
            )
            if resp.status_code == 200:
                logger.info("✅ Dados enviados para o Sheets")
                return True
            return False
        except Exception:
            return False

    def run_once(self) -> Optional[Dict[str, Any]]:
        agora = datetime.now()
        hora = agora.hour
        minuto = agora.minute

        if hora >= HARDCAP_HORA:
            logger.info(f"⏹️ Hardcap {HARDCAP_HORA}h. Encerrando.")
            return None

        if hora < HORA_INICIO:
            return None

        dados = self.check_queue()
        if dados:
            self.send_to_sheets(dados)
            d = dados['data']
            if d[4] == 0:
                logger.info(f"📭 Fila vazia às {hora:02d}:{minuto:02d}")
            else:
                logger.info(
                    f"Total: {d[4]} | Aguardando: {d[0]} | Bot: {d[1]} | Atend: {d[2]} | SLA: {d[3]} | TmpMéd: {d[5]}min")

            if hora >= HORA_FIM and d[4] > 0:
                logger.info(f"🕒 Overtime! {d[4]} clientes na fila")

            return dados

        return None


# ========== FUNÇÕES AUXILIARES ==========
def main():
    """Execução principal para produção"""
    monitor = BirdMonitor()
    resultado = monitor.run_once()
    return 0 if resultado else 1


def test_local():
    """Teste local com 2 iterações"""
    import time

    print("=" * 50)
    print("🚀 Bird Queue Monitor - TESTE LOCAL")
    print("=" * 50)

    monitor = BirdMonitor()

    for i in range(2):
        print(f"\n--- Iteração {i + 1} ---")
        resultado = monitor.run_once()

        if resultado is not None:
            print(f"✅ Iteração {i + 1} concluída!")
        else:
            # Verifica se é problema de token
            if not monitor.token and not monitor.token_file.exists():
                print(f"⚠️ Sem token - execute login manual ou verifique credenciais")
            else:
                print(f"⚠️ Sem dados (fora do horário ou fila vazia)")

        if i < 1:
            print("Aguardando 30 segundos...")
            time.sleep(30)

    print("=" * 50)
    print("✅ Teste concluído!")


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        test_local()
    else:
        sys.exit(main())