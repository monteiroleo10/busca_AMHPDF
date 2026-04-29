"""
Exportar Extrato AMHPTISS — Interface Streamlit
"""
import streamlit as st
import threading
import queue
import os
import glob
import platform
import time
import pandas as pd
from playwright.sync_api import sync_playwright

# Pasta de destino
if platform.system() == "Windows":
    PASTA_DESTINO = os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")
else:
    PASTA_DESTINO = "/tmp/extratos_amhp"

HEADLESS = platform.system() != "Windows"
ANTICAPTCHA_KEY_FILE = "/tmp/anticaptcha_key.txt"


def cookies_file(usuario):
    digitos = ''.join(c for c in usuario if c.isdigit())[:6]
    return f"/tmp/amhp_cookies_{digitos}.json"


# ─── UTILITARIOS ──────────────────────────────────────────────────

def encontrar_chromium():
    possiveis = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright"),
        os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "ms-playwright"),
    ]
    for base in possiveis:
        for pasta in ["chrome-win64", "chrome-win"]:
            matches = glob.glob(os.path.join(base, "chromium*", pasta, "chrome.exe"))
            if matches:
                return sorted(matches)[-1]
    return None


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


def diagnosticar_pagina(page, log_queue, prefixo=""):
    try:
        log_queue.put(f"{prefixo}URL: {page.url}")
        log_queue.put(f"{prefixo}Titulo: {page.title()}")
        texto = page.evaluate("() => document.body ? document.body.innerText.slice(0, 800) : ''")
        log_queue.put(f"{prefixo}Texto: {texto}")
        inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input')).map(i =>
            ({type: i.type, name: i.name, id: i.id,
              value: (i.name === 'g-recaptcha-response' ? i.value.slice(0,50) : i.value.slice(0,10)) || '(vazio)'}))""")
        log_queue.put(f"{prefixo}Inputs: {inputs}")
        page.screenshot(path="/tmp/amhp_debug.png", full_page=False)
    except Exception as e:
        log_queue.put(f"{prefixo}Erro no diagnostico: {e}")


# ─── DETECCAO DE SITEKEY ──────────────────────────────────────────

def detectar_sitekey(page, log_queue):
    page.wait_for_timeout(3000)

    resultado = page.evaluate("""() => {
        let sitekey = null;
        let action = null;
        let versao = 'v2';

        // action de campo oculto (indica v3)
        const actionEl = document.querySelector('input[name="action"]');
        if (actionEl) action = actionEl.value;

        // 1. data-sitekey (v2 widget)
        const el = document.querySelector('[data-sitekey]');
        if (el) sitekey = el.getAttribute('data-sitekey');

        // 2. iframe do reCAPTCHA (v2)
        if (!sitekey) {
            for (const f of document.querySelectorAll('iframe')) {
                const m = f.src.match(/[?&]k=([^&]+)/);
                if (m) { sitekey = m[1]; break; }
            }
        }

        // 3. script externo com render= (v3)
        if (!sitekey) {
            for (const s of document.scripts) {
                if (s.src) {
                    const m = s.src.match(/[?&]render=([^&]+)/);
                    if (m && m[1] !== 'explicit') {
                        sitekey = m[1];
                        versao = 'v3';
                        break;
                    }
                }
            }
        }

        // 4. scripts inline
        if (!sitekey) {
            for (const s of document.scripts) {
                const t = s.text || '';
                const m = t.match(/['"](6L[0-9A-Za-z_-]{30,})['"]/)
                       || t.match(/sitekey["' :]+([0-9A-Za-z_-]{30,})/);
                if (m) { sitekey = m[1]; break; }
            }
        }

        // Se tem campo action, e' v3
        if (action && sitekey) versao = 'v3';

        return {sitekey, action, versao};
    }""")

    sitekey = resultado.get("sitekey")
    action  = resultado.get("action")
    versao  = resultado.get("versao", "v2")

    if sitekey:
        log_queue.put(f"  Sitekey detectado ({versao}): {sitekey[:20]}... action={action}")
    else:
        html = page.evaluate("() => document.documentElement.outerHTML.slice(0, 800)")
        log_queue.put(f"  Sitekey nao encontrado. HTML: {html}")

    return sitekey, versao, action


# ─── RESOLVER CAPTCHA VIA 2CAPTCHA ────────────────────────────────

def resolver_captcha_2captcha(sitekey, page_url, api_key, log_queue, versao="v2", action=None):
    from twocaptcha import TwoCaptcha
    solver = TwoCaptcha(api_key)
    log_queue.put(f"  Enviando para 2captcha ({versao}, action={action})...")
    try:
        if versao == "v3":
            result = solver.recaptcha(
                sitekey=sitekey,
                url=page_url,
                version="v3",
                action=action or "submit",
                score=0.9,
            )
        else:
            result = solver.recaptcha(sitekey=sitekey, url=page_url)
        log_queue.put(f"  Captcha resolvido!")
        return result["code"]
    except Exception as e:
        raise Exception(f"2captcha erro: {e}")


def injetar_token(page, token, versao="v2"):
    if versao == "v3":
        # Para v3: intercepta grecaptcha.execute para retornar nosso token
        # quando o site chamar execute() no submit do formulario
        page.evaluate("""(token) => {
            // Seta o campo diretamente
            document.querySelectorAll('[name="g-recaptcha-response"], #g-recaptcha-response').forEach(el => {
                el.removeAttribute('disabled');
                el.value = token;
            });
            // Sobrescreve grecaptcha.execute para retornar nosso token
            const patchExecute = (obj) => {
                if (!obj) return;
                obj.execute = () => Promise.resolve(token);
                obj.execute_internal = () => Promise.resolve(token);
                if (obj.enterprise) obj.enterprise.execute = () => Promise.resolve(token);
            };
            if (window.grecaptcha) patchExecute(window.grecaptcha);
            // Garante que mesmo apos recarregar a lib o patch persiste
            Object.defineProperty(window, 'grecaptcha', {
                configurable: true,
                get: function() { return this._grecaptcha_patched; },
                set: function(v) {
                    patchExecute(v);
                    this._grecaptcha_patched = v;
                }
            });
        }""", token)
    else:
        # Para v2: injeta token e dispara callback
        page.evaluate("""(token) => {
            document.querySelectorAll('[name="g-recaptcha-response"], #g-recaptcha-response').forEach(el => {
                el.removeAttribute('disabled');
                el.value = token;
                el.innerHTML = token;
            });
            try {
                const cfg = window.___grecaptcha_cfg;
                if (cfg && cfg.clients) {
                    const buscar = (obj, n) => {
                        if (n > 5 || !obj || typeof obj !== 'object') return;
                        for (const [k, v] of Object.entries(obj)) {
                            if (typeof v === 'function' && (k === 'callback' || k === 'l')) {
                                try { v(token); } catch(e) {}
                            } else { buscar(v, n + 1); }
                        }
                    };
                    Object.values(cfg.clients).forEach(c => buscar(c, 0));
                }
            } catch(e) {}
        }""", token)


# ─── FLUXOS DE LOGIN ──────────────────────────────────────────────

def login_http(usuario, senha):
    import requests
    from bs4 import BeautifulSoup

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://portal.amhp.com.br/",
    })
    resp = session.get("https://portal.amhp.com.br/", timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    payload = {"username": usuario, "password": senha}
    for inp in soup.find_all("input", {"type": "hidden"}):
        if inp.get("name"):
            payload[inp["name"]] = inp.get("value", "")

    for endpoint in ["https://portal.amhp.com.br/", "https://portal.amhp.com.br/login"]:
        r = session.post(endpoint, data=payload, timeout=30, allow_redirects=True)
        if "perfil" in r.url or "perfil" in r.text.lower():
            return [{"name": c.name, "value": c.value,
                     "domain": c.domain or "portal.amhp.com.br",
                     "path": c.path or "/", "sameSite": "Lax"}
                    for c in session.cookies]
    return None


def validar_sessao(page):
    page.goto("https://portal.amhp.com.br/pages/PJ/perfil.html")
    page.wait_for_load_state("networkidle")
    return "perfil" in page.url


def login_com_2captcha(page, usuario, senha, api_key, log_queue):
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except Exception:
        pass

    page.goto("https://portal.amhp.com.br/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1000)

    diagnosticar_pagina(page, log_queue, "  [pre-login] ")

    # Preenche credenciais ANTES de resolver o captcha
    usuario_filled = False
    for selector in ["input[name='username']", "input[name='user']", "input[type='text']"]:
        try:
            loc = page.locator(selector).last
            if loc.count() > 0:
                loc.fill(usuario)
                usuario_filled = True
                log_queue.put(f"  Usuario preenchido via: {selector}")
                break
        except Exception:
            continue
    if not usuario_filled:
        log_queue.put("  AVISO: nao encontrou campo de usuario!")

    page.locator("input[type='password']").fill(senha)
    log_queue.put("  Senha preenchida.")

    sitekey, versao, action = detectar_sitekey(page, log_queue)
    if sitekey:
        token = resolver_captcha_2captcha(sitekey, page.url, api_key, log_queue, versao=versao, action=action)
        injetar_token(page, token, versao=versao)
        page.wait_for_timeout(500)
        # Aguarda navegacao automatica; se nao ocorrer, clica ENTRAR
        try:
            page.wait_for_url("**/perfil.html", timeout=5000)
            log_queue.put("  Form submetido automaticamente.")
        except Exception:
            page.locator("button[type='button']").filter(has_text="ENTRAR").click()
    else:
        log_queue.put("  Nenhum captcha detectado — submetendo direto.")
        page.locator("button[type='button']").filter(has_text="ENTRAR").click()

    try:
        page.wait_for_url("**/perfil.html", timeout=20000)
    except Exception:
        page.wait_for_load_state("networkidle")

    diagnosticar_pagina(page, log_queue, "  [pos-login] ")
    return "perfil" in page.url


def login_stealth(page, usuario, senha, log_queue):
    try:
        from playwright_stealth import stealth_sync
        stealth_sync(page)
    except Exception:
        pass

    page.goto("https://portal.amhp.com.br/")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(2000)

    diagnosticar_pagina(page, log_queue, "  [pre-login] ")

    try:
        page.locator("input[type='text']").last.fill(usuario)
        page.locator("input[type='password']").fill(senha)
        page.locator("button[type='button']").filter(has_text="ENTRAR").click()
    except Exception as e:
        log_queue.put(f"  Erro ao preencher formulario: {e}")
        return False

    try:
        page.wait_for_url("**/perfil.html", timeout=15000)
    except Exception:
        page.wait_for_load_state("networkidle")

    diagnosticar_pagina(page, log_queue, "  [pos-login] ")
    return "perfil" in page.url


def login_com_cookies(context, cookies_json):
    import json
    cookies = json.loads(cookies_json)
    mapa = {"strict": "Strict", "lax": "Lax", "none": "None",
            "no_restriction": "None", "unspecified": "Lax"}
    for c in cookies:
        c.setdefault("path", "/")
        c["sameSite"] = mapa.get(str(c.get("sameSite", "lax")).lower(), "Lax")
        for campo in ["hostOnly", "session", "storeId", "id", "expirationDate"]:
            c.pop(campo, None)
    context.add_cookies(cookies)


# ─── NAVEGACAO E EXPORTACAO ───────────────────────────────────────

def navegar_para_extrato(page, log_queue):
    page.goto("https://portal.amhp.com.br/pages/PJ/perfil.html")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(5000)

    iframes = page.evaluate("() => Array.from(document.querySelectorAll('iframe')).map(f => f.src || f.name || 'sem-src')")
    log_queue.put(f"  Iframes: {iframes}")
    texto = page.evaluate("() => document.body.innerText.slice(0, 500)")
    log_queue.put(f"  Texto da pagina: {texto}")

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
    page.locator("#ctl00_MainContent_rcbReferencia_Input").click()
    page.wait_for_selector("li.rcbItem", timeout=15000)
    refs = page.evaluate("""() =>
        Array.from(document.querySelectorAll('li.rcbItem'))
            .map(el => el.textContent.trim())
            .filter(t => t.length > 0)
    """)
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

        with page.expect_download(timeout=120000) as dl:
            popup.locator("#rbtExportarCsv_input").click(timeout=10000)

        download = dl.value
        os.makedirs(PASTA_DESTINO, exist_ok=True)
        prefixo = ''.join(c for c in usuario if c.isdigit())[:6]
        nome = (prefixo + "_Extrato_"
                + texto_referencia
                  .replace("ª", "a").replace("ç", "c").replace("ã", "a")
                  .replace("é", "e").replace("ê", "e").replace("á", "a")
                  .replace("â", "a").replace("ó", "o").replace("ô", "o")
                  .replace("ú", "u").replace("/", "_").replace(" ", "_")
                + ".csv")
        caminho = os.path.join(PASTA_DESTINO, nome)
        download.save_as(caminho)
    finally:
        page.evaluate("document.querySelectorAll('[id^=\"RadWindowWrapper_\"], .TelerikModalOverlay').forEach(el => el.remove())")
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


# ─── FLUXO PRINCIPAL ──────────────────────────────────────────────

def rodar_exportacao(usuario, senha, quantidade, cookies_json, api_key, log_queue):
    arquivos_exportados = []
    try:
        chromium_exe = encontrar_chromium() if platform.system() == "Windows" else None

        log_queue.put("Iniciando navegador...")
        with sync_playwright() as p:
            if platform.system() == "Windows":
                browser = p.chromium.launch(headless=HEADLESS, executable_path=chromium_exe)
            else:
                browser = p.firefox.launch(headless=True)

            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            autenticado = False

            # 1. Login via HTTP (sem navegador)
            log_queue.put("Tentando login via HTTP...")
            cookies_http = login_http(usuario, senha)
            if cookies_http:
                context.add_cookies(cookies_http)
                if validar_sessao(page):
                    log_queue.put("Login via HTTP funcionou!")
                    autenticado = True

            # 2. Login com 2captcha (resolve captcha via API)
            if not autenticado and api_key:
                log_queue.put("Tentando login com 2captcha...")
                try:
                    ok = login_com_2captcha(page, usuario, senha, api_key, log_queue)
                    if ok and validar_sessao(page):
                        log_queue.put("Login com 2captcha funcionou!")
                        autenticado = True
                    else:
                        log_queue.put("Login com 2captcha falhou na validacao.")
                except Exception as e:
                    log_queue.put(f"  Erro no 2captcha: {e}")

            # 3. Login stealth (simulando navegador humano)
            if not autenticado:
                log_queue.put("Tentando login stealth...")
                ok = login_stealth(page, usuario, senha, log_queue)
                if ok and validar_sessao(page):
                    log_queue.put("Login stealth funcionou!")
                    autenticado = True
                else:
                    log_queue.put("Login automatico falhou.")

            # 4. Fallback: cookies
            if not autenticado:
                cookies_limpo = (cookies_json or "").strip()
                if not (cookies_limpo and cookies_limpo.startswith("[")):
                    cookies_limpo = carregar_cookies_salvos(usuario) or ""

                if cookies_limpo and cookies_limpo.startswith("["):
                    log_queue.put("Usando cookies para autenticacao...")
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
                            "Cookies invalidos ou expirados.\n\n"
                            "Renove: faca login em portal.amhp.com.br, exporte via Cookie-Editor e cole no campo 'Cookies'."
                        )
                else:
                    raise Exception(
                        "Nao foi possivel autenticar.\n\n"
                        "Cole os cookies no campo abaixo:\n"
                        "1. Faca login em portal.amhp.com.br\n"
                        "2. Cookie-Editor > Export > Export as JSON\n"
                        "3. Cole o JSON no app"
                    )

            log_queue.put("Navegando para o Extrato...")
            page = navegar_para_extrato(page, log_queue)
            log_queue.put("Extrato carregado!")

            todas = obter_referencias_disponiveis(page)
            referencias = todas[:quantidade]
            log_queue.put(f"Referencias ({quantidade} mais recentes):")
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
            log_queue.put("Consolidando em Excel...")
            excel = consolidar_excel(arquivos_exportados, usuario)
            arquivos_exportados.append(excel)
            log_queue.put(f"Excel: {os.path.basename(excel)}")

    except Exception as e:
        import traceback
        log_queue.put(f"ERRO GERAL: {e}")
        log_queue.put(traceback.format_exc())

    log_queue.put(("CONCLUIDO", arquivos_exportados))


# ─── INTERFACE STREAMLIT ──────────────────────────────────────────

st.set_page_config(page_title="Exportar Extrato AMHP", page_icon="📊", layout="centered")
st.title("Exportar Extrato AMHP")

with st.expander("Como obter os cookies?", expanded=False):
    st.markdown("""
1. Instale a extensao **Cookie-Editor** no navegador:
   - [Chrome](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
   - [Firefox](https://addons.mozilla.org/firefox/addon/cookie-editor/)
2. Acesse **portal.amhp.com.br** e faca login com seu CNPJ e senha
3. Clique no icone Cookie-Editor > **Export > Export as JSON**
4. Copie e cole no campo 'Cookies' abaixo
""")

api_key_salva = carregar_anticaptcha_key()
api_key_via_env = bool(os.environ.get("ANTICAPTCHA_KEY", ""))

with st.form("login_form"):
    usuario = st.text_input("CPF/CNPJ")
    senha   = st.text_input("Senha", type="password")
    qtd     = st.number_input("Quantos extratos recentes?", min_value=1, max_value=50, value=1, step=1)
    if api_key_via_env:
        st.info("Chave 2captcha carregada via variavel de ambiente.")
        api_key = api_key_salva
    else:
        api_key = st.text_input(
            "Chave API 2captcha (opcional)",
            value=api_key_salva,
            type="password",
            help="Crie uma conta em 2captcha.com e cole sua chave aqui. Sera salva automaticamente.",
        )
    cookies_input = st.text_area(
        "Cookies (JSON) — necessario quando o login automatico e bloqueado",
        height=120,
        placeholder='[{"name":"SESSION","value":"abc...",...}]',
    )
    iniciar = st.form_submit_button("Exportar", use_container_width=True)

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
            daemon=True,
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
                        mime="text/csv",
                    )
            if excel and os.path.exists(excel):
                with open(excel, "rb") as f:
                    st.download_button(
                        label=f"Baixar {os.path.basename(excel)} (consolidado)",
                        data=f.read(),
                        file_name=os.path.basename(excel),
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
        else:
            st.error("Nenhum arquivo exportado. Verifique o log acima.")
            if os.path.exists("/tmp/amhp_debug.png"):
                st.subheader("Screenshot no momento da falha")
                with open("/tmp/amhp_debug.png", "rb") as f:
                    st.image(f.read(), caption="Estado da pagina ao falhar o login")
