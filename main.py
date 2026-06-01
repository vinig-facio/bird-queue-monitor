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

    def _load_token(self) -> Optional[str]:
        if not self.token_file.exists():
            return None

        try:
            with open(self.token_file, 'r') as f:
                data = json.load(f)

            expiry = datetime.fromisoformat(data['expiry'])
            if expiry > datetime.now():
                self.token = data['token']
                logger.info("Token reutilizado do cache")
                return self.token
            else:
                logger.debug("Token expirado")
                self.token_file.unlink(missing_ok=True)
                return None
        except (json.JSONDecodeError, KeyError, ValueError):
            self.token_file.unlink(missing_ok=True)
            return None

    def _save_token(self, token: str):
        expiry = datetime.now() + timedelta(hours=TOKEN_EXPIRY_HOURS)
        with open(self.token_file, 'w') as f:
            json.dump({
                'token': token,
                'expiry': expiry.isoformat()
            }, f)
        logger.debug(f"Token salvo (expira às {expiry.strftime('%H:%M')})")

    def login_and_get_token(self) -> Optional[str]:
        cached = self._load_token()
        if cached:
            return cached

        logger.info("Fazendo login no Bird...")
        start_time = datetime.now()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-dev-shm-usage']
                )
                context = browser.new_context()
                page = context.new_page()

                page.goto('https://app.bird.com/auth/login', timeout=20000)
                page.fill('input[name="email"]', self.email)
                page.click('button[type="submit"]')
                page.wait_for_timeout(1500)

                page.click('button:has-text("Continuar com senha")')
                page.wait_for_timeout(1500)

                page.fill('input[name="password"]', self.password)
                page.click('button[type="submit"]')
                page.wait_for_timeout(3000)

                page.goto(
                    'https://app.bird.com/workspaces/dbd7eacd-6312-441f-86dd-d933200b3e3f/'
                    'inbox/cs-inbox/feed/queue%3A01984c00-922a-7bb5-aa7a-624fb399892c',
                    timeout=20000
                )
                page.wait_for_timeout(3000)

                captured_token = {'value': None}

                def capture_token(request):
                    if 'api.bird.com' in request.url and '/auth' not in request.url:
                        auth = request.headers.get('authorization', '')
                        if auth.startswith('Bearer ') and not captured_token['value']:
                            captured_token['value'] = auth.replace('Bearer ', '')

                page.on('request', capture_token)
                page.reload()
                page.wait_for_timeout(3000)

                self.token = captured_token['value']
                browser.close()

                if self.token:
                    self._save_token(self.token)
                    login_time = (datetime.now() - start_time).total_seconds()
                    logger.info(f"Login realizado com sucesso ({login_time:.1f}s)")
                    return self.token
                else:
                    logger.error("Token não encontrado após login")
                    return None

        except Exception as e:
            logger.error(f"Erro no login: {type(e).__name__}")
            return None

    def _fetch_all_items(self) -> Tuple[Optional[List], Optional[int]]:
        """Busca TODOS os itens da fila usando paginação com pageToken"""
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

                logger.debug(f"Buscando página {page}...")
                resp = self._session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

                if resp.status_code == 401:
                    logger.info("Token expirado. Renovando...")
                    self.token_file.unlink(missing_ok=True)
                    self.token = None
                    return None, None

                if resp.status_code != 200:
                    logger.error(f"Erro HTTP {resp.status_code} na página {page}")
                    return None, None

                data = resp.json()

                # Seta total_oficial na primeira página
                if page == 1:
                    total_oficial = data.get('total', 0)
                    logger.info(f"Total oficial da fila: {total_oficial} clientes")

                results = data.get('results', [])
                if not results:
                    logger.debug(f"Página {page} sem resultados, encerrando")
                    break

                # Adiciona itens novos
                novos = 0
                for item in results:
                    item_id = item.get('id')
                    if item_id and item_id not in seen_ids:
                        seen_ids.add(item_id)
                        all_items.append(item)
                        novos += 1

                logger.debug(f"Página {page}: {len(results)} recebidos, {novos} novos (total: {len(all_items)})")

                # Verifica se tem próxima página
                next_token = data.get('nextPageToken')
                if not next_token:
                    logger.debug(f"Última página atingida")
                    break

                page += 1

                # Pausa a cada 10 páginas para não sobrecarregar a API
                if page % 10 == 0:
                    import time
                    time.sleep(0.5)

            logger.info(f"✅ Paginação finalizada: {len(all_items)} itens | Total oficial: {total_oficial}")

            if len(all_items) != total_oficial:
                logger.warning(f"⚠️ Discrepância: {len(all_items)} coletados vs {total_oficial} oficial")

            return all_items, total_oficial

        except requests.RequestException as e:
            logger.error(f"Erro na paginação: {type(e).__name__}")
            return None, None
        except Exception as e:
            logger.error(f"Erro inesperado: {e}")
            return None, None

    def check_queue(self) -> Optional[Dict[str, Any]]:
        if not self.token:
            if not self.login_and_get_token():
                return None

        items, total = self._fetch_all_items()
        if items is None:
            return None

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

            # Verifica SLA baseado no slaPolicy.timers
            sla_policy = item.get('slaPolicy', {})
            for timer in sla_policy.get('timers', []):
                if (timer.get('metric') == 'firstReplyTime' and
                        timer.get('status') == 'expired'):
                    sla_estourado += 1
                    break

            # Calcula tempo de espera
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
            'data': [aguardando, com_bot, em_atendimento, sla_estourado, total or 0, tempo_medio],
            'timestamp': now.isoformat()
        }

    def send_to_sheets(self, dados: Dict[str, Any]) -> bool:
        if not self.sheets_url:
            logger.warning("URL do Google Sheets não configurada")
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
            else:
                logger.error(f"❌ Erro HTTP {resp.status_code}")
                return False

        except Exception as e:
            logger.error(f"❌ Erro ao enviar: {type(e).__name__}")
            return False

    def run_once(self) -> Optional[Dict[str, Any]]:
        agora = datetime.now()
        hora = agora.hour
        minuto = agora.minute

        # Hard stop às 22h
        if hora >= HARDCAP_HORA:
            logger.info(f"⏹️ Hardcap {HARDCAP_HORA}h atingido às {hora:02d}:{minuto:02d}. Encerrando.")
            return None

        # Antes do expediente
        if hora < HORA_INICIO:
            logger.debug(f"Fora do horário ({hora:02d}:{minuto:02d})")
            return None

        # Overtime (após 18h)
        if hora >= HORA_FIM:
            dados = self.check_queue()
            if dados and dados['data'][4] > 0:
                logger.info(f"🕒 Overtime! {dados['data'][4]} clientes na fila às {hora:02d}:{minuto:02d}")
                self.send_to_sheets(dados)
                d = dados['data']
                logger.info(
                    f"Total: {d[4]} | Aguardando: {d[0]} | Bot: {d[1]} | Atend: {d[2]} | SLA: {d[3]} | TmpMéd: {d[5]}min")
                return dados
            elif dados:
                logger.info(f"✅ Fila zerada às {hora:02d}:{minuto:02d}. Bom descansar!")
                return None
            return None

        # Horário normal
        dados = self.check_queue()
        if dados:
            self.send_to_sheets(dados)
            d = dados['data']
            logger.info(
                f"Total: {d[4]} | Aguardando: {d[0]} | Bot: {d[1]} | Atend: {d[2]} | SLA: {d[3]} | TmpMéd: {d[5]}min")
            return dados

        return None


# ========== FUNÇÕES AUXILIARES ==========
def main():
    """Execução principal para produção (GitHub Actions)"""
    monitor = BirdMonitor()
    resultado = monitor.run_once()
    return 0 if resultado else 1


def test_local():
    """Teste com múltiplas iterações (apenas para desenvolvimento local)"""
    import time

    print("=" * 50)
    print("🚀 Bird Queue Monitor - TESTE LOCAL")
    print("=" * 50)

    monitor = BirdMonitor()

    for i in range(2):
        print(f"\n--- Iteração {i + 1} ---")
        resultado = monitor.run_once()

        if resultado:
            print(f"✅ Iteração {i + 1} concluída!")
        else:
            print(f"❌ Iteração {i + 1} falhou!")

        if i < 1:
            print("Aguardando 30 segundos...")
            time.sleep(30)

    print("=" * 50)
    print("✅ Teste concluído!")


def debug():
    """Modo debug para verificar estrutura dos dados"""
    print("=" * 50)
    print("🔍 Bird Queue Monitor - MODO DEBUG")
    print("=" * 50)

    monitor = BirdMonitor()
    monitor.login_and_get_token()
    items, total = monitor._fetch_all_items()

    if items:
        print(f"\n📊 Total oficial: {total}")
        print(f"📊 Itens recuperados: {len(items)}")

        primeiro = items[0]
        print(f"\n📋 Estrutura do primeiro item:")
        print(f"  ID: {primeiro.get('id')}")

        queue_info = primeiro.get('queueInfo', {})
        print(f"  queueInfo keys: {list(queue_info.keys())}")
        print(f"  queuedAt: {queue_info.get('queuedAt')}")
    else:
        print("❌ Nenhum item encontrado")

    print("=" * 50)


if __name__ == '__main__':
    import sys

    if len(sys.argv) > 1:
        if sys.argv[1] == '--test':
            test_local()
        elif sys.argv[1] == '--debug':
            debug()
        else:
            print(f"Argumento desconhecido: {sys.argv[1]}")
            print("Uso: python main.py [--test | --debug]")
            sys.exit(1)
    else:
        sys.exit(main())