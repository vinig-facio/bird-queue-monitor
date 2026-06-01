import os
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Carrega credenciais do .env
load_dotenv()


class BirdMonitor:
    def __init__(self):
        self.email = os.getenv('BIRD_EMAIL')
        self.password = os.getenv('BIRD_PASSWORD')
        self.token = None
        self.token_file = Path('token.json')

    def login_and_get_token(self):
        """Faz login uma vez e salva o token por 24h.
        Retorna o token se sucesso, None se falhar."""

        # Se já tem token salvo e ainda é válido, reutiliza
        if self.token_file.exists():
            with open(self.token_file, 'r') as f:
                data = json.load(f)
                if datetime.fromisoformat(data['expiry']) > datetime.now():
                    self.token = data['token']
                    print("✅ Token reutilizado do cache")
                    return self.token

        print("🔐 Fazendo login no Bird...")

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()

                try:
                    # ETAPA 1: Página de login
                    page.goto('https://app.bird.com/auth/login')
                    page.wait_for_timeout(3000)

                    # Digita o email
                    page.fill('input[name="email"]', self.email)

                    # Clica no botão "Continuar"
                    page.click('button[type="submit"]')
                    page.wait_for_timeout(2000)

                    # ETAPA 2: Escolhe "Continuar com senha"
                    page.click('button:has-text("Continuar com senha")')
                    page.wait_for_timeout(2000)

                    # ETAPA 3: Digita senha e loga
                    page.fill('input[name="password"]', self.password)
                    page.click('button[type="submit"]')

                    # Espera o login processar
                    page.wait_for_timeout(5000)

                    # Verifica se logou
                    try:
                        page.wait_for_selector('text=workspaces', timeout=10000)
                        print("✅ Login realizado!")
                    except PlaywrightTimeout:
                        print("⚠️ Verificando login...")
                        page.wait_for_timeout(3000)

                    # Navega DIRETO para a página da fila
                    print("📂 Indo para a fila...")
                    page.goto(
                        'https://app.bird.com/workspaces/dbd7eacd-6312-441f-86dd-d933200b3e3f/inbox/cs-inbox/feed/queue%3A01984c00-922a-7bb5-aa7a-624fb399892c')
                    page.wait_for_timeout(5000)

                    # Captura o token DURANTE a navegação
                    def capture_token(request):
                        auth = request.headers.get('authorization', '')
                        if auth.startswith('Bearer ') and not self.token:
                            self.token = auth.replace('Bearer ', '')
                            print("🎯 Token capturado!")

                    page.on('request', capture_token)

                    # Recarrega pra garantir
                    page.reload()
                    page.wait_for_timeout(5000)

                    if self.token:
                        expiry = datetime.now() + timedelta(hours=23)
                        with open(self.token_file, 'w') as f:
                            json.dump({
                                'token': self.token,
                                'expiry': expiry.isoformat()
                            }, f)
                        print(f"✅ Token salvo (válido até {expiry.strftime('%H:%M')})")
                        return self.token
                    else:
                        print("❌ Token não encontrado após login")
                        return None

                finally:
                    browser.close()

        except PlaywrightTimeout as e:
            print(f"❌ Timeout no Playwright: {e}")
            return None
        except Exception as e:
            print(f"❌ Erro inesperado no login: {e}")
            return None

    def check_queue(self):
        """Verifica quantas pessoas estão na fila.
        Retorna dict com 'total' e 'timestamp', ou None se falhar."""

        # Garante que temos token
        if not self.token:
            token = self.login_and_get_token()
            if not token:
                print("❌ Sem token disponível")
                return None

        url = ("https://api.bird.com/workspaces/dbd7eacd-6312-441f-86dd-d933200b3e3f/"
               "feeds/queue:01984c00-922a-7bb5-aa7a-624fb399892c/items"
               "?sortBy=lastActivity"
               "&dateRange=%7B%7D"
               "&participants=%7B%22agents%22:%5B%5D,%22contacts%22:%5B%5D%7D"
               "&searchTerm="
               "&limit=1")

        headers = {
            "accept": "application/json",
            "authorization": f"Bearer {self.token}",
            "origin": "https://app.bird.com"
        }

        try:
            resp = requests.get(url, headers=headers, timeout=30)

            # Se token expirou, deleta cache e tenta de novo
            if resp.status_code == 401:
                print("🔐 Token expirado! Renovando...")
                self.token_file.unlink(missing_ok=True)
                self.token = None
                return self.check_queue()  # Tenta novamente

            # Se outro erro HTTP
            if resp.status_code != 200:
                print(f"❌ Erro HTTP {resp.status_code}: {resp.text}")
                return None

            data = resp.json()
            return {
                'total': data['total'],
                'timestamp': datetime.now().isoformat()
            }

        except requests.ConnectionError:
            print("❌ Erro de conexão com a API")
            return None
        except requests.Timeout:
            print("❌ Timeout na requisição da API")
            return None
        except requests.RequestException as e:
            print(f"❌ Erro na requisição: {e}")
            return None
        except (KeyError, ValueError) as e:
            print(f"❌ Erro ao processar resposta JSON: {e}")
            return None


# ========== TESTE LOCAL ==========
if __name__ == '__main__':
    import time

    print("🚀 Bird Queue Monitor - Teste Local")
    print("-" * 40)

    monitor = BirdMonitor()

    for i in range(3):
        dados = monitor.check_queue()
        if dados:
            print(f"📊 {datetime.now().strftime('%H:%M:%S')} | Fila: {dados['total']} pessoas")
        else:
            print("❌ Falha ao obter dados")

        if i < 2:
            time.sleep(30)

    print("-" * 40)
    print("✅ Teste concluído!")