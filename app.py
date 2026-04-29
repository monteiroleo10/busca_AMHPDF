"""
Exportar Extrato AMHPTISS — Interface Streamlit
"""
import streamlit as st
import threading
import queue
import os
import sys
import glob
import platform
import time
import pandas as pd
from playwright.sync_api import sync_playwright

# Pasta de destino: temporaria no servidor, Downloads no Windows local
if platform.system() == "Windows":
    PASTA_DESTINO = os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")
else:
    PASTA_DESTINO = "/tmp/extratos_amhp"

def cookies_file(usuario):
    digitos = ''.join(c for c in usuario if c.isdigit())[:6]
    return f"/tmp/amhp_cookies_{digitos}.json"

HEADLESS = platform.system() != "Windows"


# ─── LOGICA DE EXPORTACAO ─────────────────────────────────────────

def encontrar_chromium():
    """Localiza o Chromium no Windows. No Linux o Playwright resolve sozinho."""
    possiveis = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
        os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "ms-playwright"),
    ]
    for base in possiveis:
        for pasta_chrome in ["chrome-win64", "chrome-win"]:
            matches = glob.glob(os.path.join(base, "chromium*", pasta_chrome, "chrome.exe"))
            if matches:
                return sorted(matches)[-1]
    return None


def login_http(usuario, senha):
    """Tenta login via HTTP direto sem navegador. Retorna cookies se funcionar."""
    import requests
    from bs4 import BeautifulSoup

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://portal.amhp.com.br/",
    })

    # Obtém a página de login para capturar tokens
    resp = session.get("https://portal.amhp.com.br/", timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    # Monta payload com campos ocultos do formulário
    payload = {"username": usuario, "password": senha}
    for inp in soup.find_all("input", {"type": "hidden"}):
        if inp.get("name"):
            payload[inp["name"]] = inp.get("value", "")

    # Tenta POST na raiz e em endpoints comuns
    for endpoint in [
        "https://portal.amhp.com.br/",
        "https://portal.amhp.com.br/login",
        "https://portal.amhp.com.br/api/login",
    ]:
        r = session.post(endpoint, data=payload, timeout=30, allow_redirects=True)
        if "perfil" in r.url or "perfil" in r.text.lower():
            # Login funcionou — retorna cookies no formato Playwright
            cookies = []
            for c in session.cookies:
                cookies.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain or "portal.amhp.com.br",
                    "path": c.path or "/",
                    "sameSite": "Lax",
                })
            return cookies
    return None


def validar_sessao(page):
    """Verifica se a sessão está autenticada acessando uma página protegida."""
    page.goto("https://portal.amhp.com.br/pages/PJ/perfil.html")
    page.wait_for_load_state("networkidle")
    # Se não estiver logado, redireciona para a página de login
    if "perfil" not in page.url:
        return False
    return True


def login_stealth(page, usuario, senha):
    """Tenta login com Playwright disfarçado de navegador humano."""
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except Exception:
        pass

    page.goto("https://portal.amhp.com.br/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)
    page.locator("input[type='text']").last.fill(usuario)
    page.locator("input[type='password']").fill(senha)
    page.locator("button[type='button']").filter(has_text="ENTRAR").click()
    try:
        page.wait_for_url("**/perfil.html", timeout=15000)
    except Exception:
        page.wait_for_load_state("networkidle")
    return "perfil" in page.url


ANTICAPTCHA_KEY_FILE = "/tmp/anticaptcha_key.txt"

def salvar_anticaptcha_key(api_key):
    with open(ANTICAPTCHA_KEY_FILE, "w") as f:
        f.write(api_key.strip())

def carregar_anticaptcha_key():
    if os.path.exists(ANTICAPTCHA_KEY_FILE):
        with open(ANTICAPTCHA_KEY_FILE, "r") as f:
            return f.read().strip()
    return os.environ.get("ANTICAPTCHA_KEY", "")


def salvar_cookies(usuario, cookies_json):
    with open(cookies_file(usuario), "w") as f:
        f.write(cookies_json)


def carregar_cookies_salvos(usuario):
    path = cookies_file(usuario)
    if os.path.exists(path):
        with open(path, "r") as f:
            return f.read()
    return None


def resolver_recaptcha(sitekey, pageurl, api_key, log_queue):
    """Envia o reCAPTCHA para o 2captcha e retorna o token resolvido."""
    import requests as req

    log_queue.put("  Enviando reCAPTCHA para o 2captcha...")
    r = req.post("http://2captcha.com/in.php", data={
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": sitekey,
        "pageurl": pageurl,
        "json": 1,
    }, timeout=30)
    resultado = r.json()
    if resultado.get("status") != 1:
        raise Exception(f"2captcha erro ao enviar: {resultado.get('request')}")

    captcha_id = resultado["request"]
    log_queue.put(f"  reCAPTCHA enviado (id={captcha_id}). Aguardando resolucao...")

    for tentativa in range(24):  # ate 2 minutos
        time.sleep(5)
        r = req.get("http://2captcha.com/res.php", params={
            "key": api_key,
            "action": "get",
            "id": captcha_id,
            "json": 1,
        }, timeout=30)
        resultado = r.json()
        if resultado.get("status") == 1:
            log_queue.put("  reCAPTCHA resolvido!")
            return resultado["request"]
        if resultado.get("request") != "CAPCHA_NOT_READY":
            raise Exception(f"2captcha erro na resolucao: {resultado.get('request')}")
        log_queue.put(f"  Aguardando... ({(tentativa+1)*5}s)")

    raise Exception("2captcha timeout: reCAPTCHA nao resolvido em 2 minutos")


def detectar_sitekey(page, log_queue):
    """Detecta o sitekey do reCAPTCHA usando multiplas estrategias."""
    # Aguarda scripts de captcha carregarem
    page.wait_for_timeout(3000)

    sitekey = page.evaluate("""() => {
        // 1. Atributo data-sitekey em qualquer elemento
        const el = document.querySelector('[data-sitekey]');
        if (el) return el.getAttribute('data-sitekey');

        // 2. Sitekey no src do iframe do reCAPTCHA
        const iframes = document.querySelectorAll('iframe[src*="recaptcha"], iframe[src*="google.com/recaptcha"]');
        for (const f of iframes) {
            const m = f.src.match(/[?&]k=([^&]+)/);
            if (m) return m[1];
        }

        // 3. Sitekey em scripts inline
        for (const s of document.scripts) {
            const text = s.text || '';
            const patterns = [
                /['"](6L[0-9A-Za-z_-]{30,})['"]/,
                /sitekey['":\s]+['"]([\w-]{30,})['"]/i,
                /grecaptcha\.render\([^)]*['"]([\w-]{30,})['"]/,
            ];
            for (const re of patterns) {
                const m = text.match(re);
                if (m) return m[1];
            }
        }

        // 4. Sitekey em src de scripts externos
        for (const s of document.scripts) {
            if (s.src) {
                const m = s.src.match(/[?&]render=([^&]+)/);
                if (m && m[1] !== 'explicit') return m[1];
            }
        }

        return null;
    }""")

    if sitekey:
        log_queue.put(f"  Sitekey encontrado: {sitekey[:20]}...")
    else:
        # Loga HTML resumido para diagnostico
        html_resumo = page.evaluate("() => document.documentElement.outerHTML.slice(0, 1000)")
        log_queue.put(f"  Sitekey nao encontrado. HTML inicial: {html_resumo}")

    return sitekey


def injetar_token_recaptcha(page, token):
    """Injeta o token resolvido e dispara os callbacks do grecaptcha."""
    page.evaluate("""(token) => {
        // Seta todos os campos g-recaptcha-response
        document.querySelectorAll('[name="g-recaptcha-response"], #g-recaptcha-response').forEach(el => {
            el.removeAttribute('disabled');
            el.value = token;
            el.innerHTML = token;
        });

        // Dispara callbacks registrados no grecaptcha
        try {
            const cfg = window.___grecaptcha_cfg;
            if (cfg && cfg.clients) {
                for (const client of Object.values(cfg.clients)) {
                    // Percorre o objeto procurando funcoes de callback
                    const visitar = (obj, profundidade) => {
                        if (profundidade > 6 || !obj || typeof obj !== 'object') return;
                        for (const [k, v] of Object.entries(obj)) {
                            if (typeof v === 'function' && (k === 'callback' || k === 'l')) {
                                try { v(token); } catch(e) {}
                            } else {
                                visitar(v, profundidade + 1);
                            }
                        }
                    };
                    visitar(client, 0);
                }
            }
        } catch(e) {}
    }""", token)


def login_com_anticaptcha(page, usuario, senha, api_key, log_queue):
    """Faz login resolvendo o reCAPTCHA via 2captcha."""
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except Exception:
        pass

    page.goto("https://portal.amhp.com.br/")
    page.wait_for_load_state("networkidle")

    sitekey = detectar_sitekey(page, log_queue)

    if sitekey:
        log_queue.put("  Resolvendo reCAPTCHA via 2captcha...")
        token = resolver_recaptcha(sitekey, page.url, api_key, log_queue)
        injetar_token_recaptcha(page, token)
        page.wait_for_timeout(1000)
    else:
        log_queue.put("  Nenhum reCAPTCHA detectado — tentando login direto.")

    page.locator("input[type='text']").last.fill(usuario)
    page.locator("input[type='password']").fill(senha)
    page.locator("button[type='button']").filter(has_text="ENTRAR").click()

    try:
        page.wait_for_url("**/perfil.html", timeout=20000)
    except Exception:
        page.wait_for_load_state("networkidle")

    return "perfil" in page.url


def login_com_cookies(context, cookies_json):
    import json
    cookies = json.loads(cookies_json)
    mapa_samesite = {
        "strict": "Strict",
        "lax": "Lax",
        "none": "None",
        "no_restriction": "None",
        "unspecified": "Lax",
    }
    for c in cookies:
        c.setdefault("path", "/")
        raw = str(c.get("sameSite", "lax")).lower()
        c["sameSite"] = mapa_samesite.get(raw, "Lax")
        for campo in ["hostOnly", "session", "storeId", "id", "expirationDate"]:
            c.pop(campo, None)
    context.add_cookies(cookies)


def navegar_para_extrato(page, log_queue):
    page.goto("https://portal.amhp.com.br/pages/PJ/perfil.html")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(5000)

    # Loga iframes e texto visível da página para diagnóstico
    iframes = page.evaluate("() => Array.from(document.querySelectorAll('iframe')).map(f => f.src || f.name || 'sem-src')")
    log_queue.put(f"  Iframes: {iframes}")
    texto = page.evaluate("() => document.body.innerText.slice(0, 500)")
    log_queue.put(f"  Texto da pagina: {texto}")

    # Tenta clicar pelo texto
    try:
        page.get_by_text("AMHPTISS", exact=False).first.click(timeout=10000)
        page.wait_for_load_state("networkidle")
        log_queue.put(f"  URL apos clique: {page.url}")
    except Exception as e:
        log_queue.put(f"  Clique falhou: {e}")

    page.goto("https://amhptiss.amhp.com.br/Extrato.aspx")
    page.wait_for_load_state("networkidle")
    log_queue.put(f"  Extrato URL: {page.url}")
    return page


def obter_referencias_disponiveis(page):
    # Abre o dropdown e lê os itens diretamente do DOM
    page.locator("#ctl00_MainContent_rcbReferencia_Input").click()
    page.wait_for_selector("li.rcbItem", timeout=15000)
    refs = page.evaluate("""() => {
        return Array.from(document.querySelectorAll('li.rcbItem'))
            .map(el => el.textContent.trim())
            .filter(t => t.length > 0);
    }""")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    return refs


def selecionar_referencia(page, texto_referencia):
    page.locator("#ctl00_MainContent_rcbReferencia_Input").click()
    page.wait_for_timeout(1500)
    page.locator(f"li.rcbItem:has-text('{texto_referencia}')").first.click(timeout=10000)
    page.wait_for_timeout(1000)


def exportar_csv(page, texto_referencia, usuario):
    selecionar_referencia(page, texto_referencia)
    page.locator("#ctl00_MainContent_rbtExportarCsv_input").click()
    page.wait_for_timeout(2000)

    caminho = None
    try:
        page.wait_for_selector("iframe[src*='ExtratoExportacao'][tabindex='0']", timeout=15000)
        frame = page.frame(url="*ExtratoExportacao*")
        if frame:
            frame.wait_for_load_state("load")
        page.wait_for_timeout(1000)

        popup = page.frame_locator("iframe[src*='ExtratoExportacao'][tabindex='0']")

        try:
            popup.locator("a.rlbTransferAllFrom").first.click(timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(1000)

        with page.expect_download(timeout=120000) as download_info:
            popup.locator("#rbtExportarCsv_input").click(timeout=10000)

        download = download_info.value
        os.makedirs(PASTA_DESTINO, exist_ok=True)
        prefixo = ''.join(c for c in usuario if c.isdigit())[:6]
        nome_arquivo = (
            prefixo + "_Extrato_"
            + texto_referencia
              .replace("ª", "a").replace("ç", "c").replace("ã", "a")
              .replace("é", "e").replace("ê", "e").replace("á", "a")
              .replace("â", "a").replace("ó", "o").replace("ô", "o")
              .replace("ú", "u").replace("/", "_").replace(" ", "_")
            + ".csv"
        )
        caminho = os.path.join(PASTA_DESTINO, nome_arquivo)
        download.save_as(caminho)
    finally:
        page.evaluate("""
            document.querySelectorAll('[id^="RadWindowWrapper_"], .TelerikModalOverlay').forEach(el => el.remove());
        """)
        page.wait_for_timeout(500)

    return caminho


def consolidar_excel(arquivos, usuario):
    prefixo = ''.join(c for c in usuario if c.isdigit())[:6]
    nome_excel = os.path.join(PASTA_DESTINO, f"{prefixo}_Extratos_Consolidado.xlsx")
    frames = []
    for arq in arquivos:
        try:
            df = pd.read_csv(arq, sep=";", encoding="latin1", dtype=str)
            df.insert(0, "Referencia", os.path.basename(arq).replace(".csv", ""))
            frames.append(df)
        except Exception:
            pass
    if frames:
        pd.concat(frames, ignore_index=True).to_excel(nome_excel, index=False)
    return nome_excel


def rodar_exportacao(usuario, senha, quantidade, cookies_json, api_key, log_queue):
    arquivos_exportados = []
    try:
        # No Windows (exe), localiza o Chromium manualmente
        chromium_exe = encontrar_chromium() if platform.system() == "Windows" else None

        log_queue.put("Iniciando navegador...")
        with sync_playwright() as p:
            if platform.system() == "Windows":
                browser = p.chromium.launch(headless=HEADLESS, executable_path=chromium_exe)
            else:
                browser = p.firefox.launch(headless=True)

            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            autenticado = False

            # Tentativa 1: login via HTTP com as credenciais do usuario
            log_queue.put("Tentando login com CNPJ/senha informados (HTTP)...")
            cookies_http = login_http(usuario, senha)
            if cookies_http:
                context.add_cookies(cookies_http)
                if validar_sessao(page):
                    log_queue.put("Login via HTTP funcionou!")
                    autenticado = True

            # Tentativa 2: login com anti-captcha (2captcha) se API key disponivel
            if not autenticado and api_key:
                log_queue.put("Tentando login com resolucao de reCAPTCHA via 2captcha...")
                try:
                    ok = login_com_anticaptcha(page, usuario, senha, api_key, log_queue)
                    if ok and validar_sessao(page):
                        log_queue.put("Login com anti-captcha funcionou!")
                        autenticado = True
                    else:
                        log_queue.put("Login com anti-captcha falhou na validacao.")
                except Exception as e:
                    log_queue.put(f"  Erro no anti-captcha: {e}")

            # Tentativa 3: login com stealth usando as credenciais do usuario
            if not autenticado:
                log_queue.put("Tentando login com stealth (simulando navegador humano)...")
                ok = login_stealth(page, usuario, senha)
                if ok and validar_sessao(page):
                    log_queue.put("Login com stealth funcionou!")
                    autenticado = True
                else:
                    log_queue.put("Login automatico bloqueado (provavel reCAPTCHA).")

            # Tentativa 3 (fallback): cookies colados no formulario para este CNPJ
            if not autenticado:
                cookies_limpo = (cookies_json or "").strip()
                if not (cookies_limpo and cookies_limpo.startswith("[")):
                    # Tenta cookies salvos especificos deste CNPJ
                    cookies_limpo = carregar_cookies_salvos(usuario) or ""

                if cookies_limpo and cookies_limpo.startswith("["):
                    log_queue.put("Login automatico falhou — usando cookies para este CNPJ...")
                    login_com_cookies(context, cookies_limpo)
                    if validar_sessao(page):
                        salvar_cookies(usuario, cookies_limpo)
                        log_queue.put("Sessao validada com cookies!")
                        autenticado = True
                    else:
                        cf = cookies_file(usuario)
                        if os.path.exists(cf):
                            os.remove(cf)
                        raise Exception(
                            "Cookies invalidos ou expirados para este CNPJ.\n\n"
                            "Para renovar:\n"
                            "1. Faca login em portal.amhp.com.br\n"
                            "2. Cookie-Editor > Export > Export as JSON\n"
                            "3. Cole o JSON no campo 'Cookies' do app"
                        )
                else:
                    raise Exception(
                        "Login automatico bloqueado pelo reCAPTCHA do portal.\n\n"
                        "Para continuar:\n"
                        "1. Instale a extensao Cookie-Editor no navegador\n"
                        "2. Faca login em portal.amhp.com.br com seu CNPJ\n"
                        "3. Cookie-Editor > Export > Export as JSON\n"
                        "4. Cole o JSON no campo 'Cookies' do app"
                    )

            log_queue.put("Navegando para o Extrato...")
            page = navegar_para_extrato(page, log_queue)
            log_queue.put("Extrato carregado!")

            todas = obter_referencias_disponiveis(page)
            referencias = todas[:quantidade]

            log_queue.put(f"Referencias selecionadas ({quantidade} mais recentes):")
            for i, ref in enumerate(referencias):
                log_queue.put(f"  [{i+1}] {ref}")

            for i, ref in enumerate(referencias):
                log_queue.put(f"Exportando [{i+1}/{len(referencias)}]: {ref}")
                try:
                    caminho = exportar_csv(page, ref, usuario)
                    if caminho:
                        arquivos_exportados.append(caminho)
                        log_queue.put(f"  Salvo: {os.path.basename(caminho)}")
                    time.sleep(2)
                except Exception as e:
                    log_queue.put(f"  ERRO: {e}")

            browser.close()

        if arquivos_exportados:
            log_queue.put("Consolidando arquivos em Excel...")
            excel = consolidar_excel(arquivos_exportados, usuario)
            arquivos_exportados.append(excel)
            log_queue.put(f"Excel gerado: {os.path.basename(excel)}")

    except Exception as e:
        import traceback
        log_queue.put(f"ERRO GERAL: {e}")
        log_queue.put(traceback.format_exc())

    log_queue.put(("CONCLUIDO", arquivos_exportados))


# ─── INTERFACE STREAMLIT ──────────────────────────────────────────

st.set_page_config(page_title="Exportar Extrato AMHP", page_icon="📊", layout="centered")
st.title("Exportar Extrato AMHP")

with st.expander("Como obter os cookies? (necessario quando o login automatico e bloqueado)", expanded=False):
    st.markdown("""
1. Instale a extensao **Cookie-Editor** no seu navegador:
   - [Chrome](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
   - [Firefox](https://addons.mozilla.org/firefox/addon/cookie-editor/)
2. Acesse **portal.amhp.com.br** e faca login com seu CNPJ e senha
3. Clique no icone da extensao Cookie-Editor
4. Clique em **Export > Export as JSON**
5. Copie o conteudo e cole no campo 'Cookies' abaixo
""")

api_key_salva = carregar_anticaptcha_key()
api_key_via_env = bool(os.environ.get("ANTICAPTCHA_KEY", ""))

with st.form("login_form"):
    usuario  = st.text_input("CPF/CNPJ")
    senha    = st.text_input("Senha", type="password")
    qtd      = st.number_input("Quantos extratos recentes?", min_value=1, max_value=50, value=1, step=1)
    if api_key_via_env:
        st.info("Chave 2captcha carregada via variavel de ambiente.")
        api_key = api_key_salva
    else:
        api_key  = st.text_input(
            "Chave API 2captcha (opcional)",
            value=api_key_salva,
            type="password",
            help="Crie uma conta em 2captcha.com, deposite credito e cole sua API key aqui. Sera salva automaticamente."
        )
    cookies_input = st.text_area(
        "Cookies (JSON) — necessario quando o login automatico e bloqueado",
        height=120,
        placeholder='Cole aqui o JSON exportado pelo Cookie-Editor. Ex: [{"name":"SESSION","value":"abc...",...}]',
        help="Exporte os cookies apos fazer login manual em portal.amhp.com.br usando a extensao Cookie-Editor."
    )
    iniciar  = st.form_submit_button("Exportar", use_container_width=True)

if iniciar:
    if not usuario or not senha:
        st.error("Preencha CPF/CNPJ e Senha.")
    else:
        api_key_final = api_key.strip()
        if api_key_final:
            salvar_anticaptcha_key(api_key_final)

        log_queue = queue.Queue()
        thread = threading.Thread(
            target=rodar_exportacao,
            args=(usuario, senha, int(qtd), cookies_input.strip(), api_key_final, log_queue),
            daemon=True
        )
        thread.start()

        st.subheader("Progresso")
        log_placeholder = st.empty()
        logs = []
        arquivos_finais = []

        while thread.is_alive() or not log_queue.empty():
            while not log_queue.empty():
                item = log_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "CONCLUIDO":
                    arquivos_finais = item[1]
                else:
                    logs.append(item)
                    log_placeholder.code("\n".join(logs))
            time.sleep(0.2)

        csvs  = [f for f in arquivos_finais if f.endswith(".csv")]
        excel = next((f for f in arquivos_finais if f.endswith(".xlsx")), None)

        if arquivos_finais:
            st.success(f"Concluido! {len(csvs)} CSV(s) exportado(s).")
            st.subheader("Baixar arquivos")
            for arq in csvs:
                with open(arq, "rb") as f:
                    st.download_button(
                        label=f"Baixar {os.path.basename(arq)}",
                        data=f.read(),
                        file_name=os.path.basename(arq),
                        mime="text/csv"
                    )
            if excel and os.path.exists(excel):
                with open(excel, "rb") as f:
                    st.download_button(
                        label=f"Baixar {os.path.basename(excel)} (consolidado)",
                        data=f.read(),
                        file_name=os.path.basename(excel),
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                    )
        else:
            st.error("Nenhum arquivo foi exportado. Verifique o log acima.")
