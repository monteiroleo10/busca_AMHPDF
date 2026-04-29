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
import urllib.parse
import base64
import pandas as pd
from playwright.sync_api import sync_playwright


# ─── CONFIGURACAO ─────────────────────────────────────────────────

if platform.system() == "Windows":
    PASTA_DESTINO = os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")
else:
    PASTA_DESTINO = "/tmp/extratos_amhp"

HEADLESS = platform.system() != "Windows"


def carregar_api_key():
    return os.environ.get("ANTICAPTCHA_KEY", "")


def encontrar_chromium():
    """Localiza o Chromium instalado pelo Playwright no Windows."""
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


# ─── DIAGNOSTICO ──────────────────────────────────────────────────

def diagnosticar_pagina(page, log_queue, prefixo=""):
    try:
        log_queue.put(f"{prefixo}URL: {page.url}")
        log_queue.put(f"{prefixo}Titulo: {page.title()}")
        texto = page.evaluate("() => document.body ? document.body.innerText.slice(0, 600) : ''")
        log_queue.put(f"{prefixo}Texto: {texto}")
        inputs = page.evaluate("""() => Array.from(document.querySelectorAll('input')).map(i => ({
            type: i.type, name: i.name, id: i.id,
            value: (i.name === 'g-recaptcha-response' ? i.value.slice(0, 50) : i.value.slice(0, 10)) || '(vazio)'
        }))""")
        log_queue.put(f"{prefixo}Inputs: {inputs}")
        page.screenshot(path="/tmp/amhp_debug.png", full_page=False)
    except Exception as e:
        log_queue.put(f"{prefixo}Erro no diagnostico: {e}")


# ─── DETECCAO DE SITEKEY ──────────────────────────────────────────

def detectar_sitekey(page, log_queue):
    page.wait_for_timeout(3000)

    resultado = page.evaluate("""() => {
        let sitekey = null, action = null, versao = 'v2';

        const actionEl = document.querySelector('input[name="action"]');
        if (actionEl) action = actionEl.value;

        // data-sitekey (v2 widget)
        const el = document.querySelector('[data-sitekey]');
        if (el) sitekey = el.getAttribute('data-sitekey');

        // iframe do reCAPTCHA (v2)
        if (!sitekey) {
            for (const f of document.querySelectorAll('iframe')) {
                const m = f.src.match(/[?&]k=([^&]+)/);
                if (m) { sitekey = m[1]; break; }
            }
        }

        // script externo com render= (v3)
        if (!sitekey) {
            for (const s of document.scripts) {
                if (s.src) {
                    const m = s.src.match(/[?&]render=([^&]+)/);
                    if (m && m[1] !== 'explicit') { sitekey = m[1]; versao = 'v3'; break; }
                }
            }
        }

        // scripts inline
        if (!sitekey) {
            for (const s of document.scripts) {
                const t = s.text || '';
                const m = t.match(/['"](6L[0-9A-Za-z_-]{30,})['"]/)
                       || t.match(/sitekey["' :]+([0-9A-Za-z_-]{30,})/);
                if (m) { sitekey = m[1]; break; }
            }
        }

        if (action && sitekey) versao = 'v3';
        return { sitekey, action, versao };
    }""")

    sitekey = resultado.get("sitekey")
    action  = resultado.get("action")
    versao  = resultado.get("versao", "v2")

    if sitekey:
        log_queue.put(f"  Sitekey ({versao}): {sitekey[:20]}... action={action}")
    else:
        log_queue.put("  Sitekey nao encontrado.")

    return sitekey, versao, action


# ─── 2CAPTCHA ─────────────────────────────────────────────────────

def resolver_captcha(sitekey, page_url, api_key, log_queue, versao="v2", action=None):
    from twocaptcha import TwoCaptcha
    solver = TwoCaptcha(api_key)
    log_queue.put(f"  Enviando para 2captcha ({versao}, action={action})...")
    if versao == "v3":
        result = solver.recaptcha(
            sitekey=sitekey, url=page_url,
            version="v3", action=action or "submit", score=0.9,
        )
    else:
        result = solver.recaptcha(sitekey=sitekey, url=page_url)
    log_queue.put("  Captcha resolvido!")
    return result["code"]


# ─── LOGIN ────────────────────────────────────────────────────────

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

    # Preenche credenciais antes de resolver o captcha
    for selector in ["input[name='username']", "input[name='user']", "input[type='text']"]:
        try:
            loc = page.locator(selector).last
            if loc.count() > 0:
                loc.fill(usuario)
                log_queue.put(f"  Usuario preenchido via: {selector}")
                break
        except Exception:
            continue

    page.locator("input[type='password']").fill(senha)

    sitekey, versao, action = detectar_sitekey(page, log_queue)
    if not sitekey:
        raise Exception("Sitekey do reCAPTCHA nao encontrado na pagina de login.")

    token = resolver_captcha(sitekey, page.url, api_key, log_queue, versao=versao, action=action)

    # Intercepta o POST e injeta o token resolvido antes de chegar no servidor
    interceptado = [False]

    def trocar_token(route, request):
        if request.method == "POST" and "portal.amhp.com.br" in request.url:
            pd = request.post_data or ""
            if "g-recaptcha-response" in pd:
                try:
                    params = dict(urllib.parse.parse_qsl(pd, keep_blank_values=True))
                    old = params.get("g-recaptcha-response", "")[:20]
                    params["g-recaptcha-response"] = token
                    log_queue.put(f"  POST interceptado! {old}... -> token 2captcha")
                    interceptado[0] = True
                    route.continue_(post_data=urllib.parse.urlencode(params))
                    return
                except Exception as e:
                    log_queue.put(f"  Erro na interceptacao: {e}")
        route.continue_()

    page.route("**/*", trocar_token)
    try:
        page.locator("button[type='button']").filter(has_text="ENTRAR").click()
        try:
            page.wait_for_url("**/perfil.html", timeout=20000)
        except Exception:
            page.wait_for_load_state("networkidle")
    finally:
        page.unroute("**/*", trocar_token)

    if not interceptado[0]:
        log_queue.put("  AVISO: POST nao interceptado — pode usar fetch/XHR.")

    diagnosticar_pagina(page, log_queue, "  [pos-login] ")
    return "perfil" in page.url


def validar_sessao(page):
    page.goto("https://portal.amhp.com.br/pages/PJ/perfil.html")
    page.wait_for_load_state("networkidle")
    return "perfil" in page.url


# ─── NAVEGACAO E EXPORTACAO ───────────────────────────────────────

def navegar_para_extrato(page, log_queue):
    page.goto("https://portal.amhp.com.br/pages/PJ/perfil.html")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(5000)

    iframes = page.evaluate("() => Array.from(document.querySelectorAll('iframe')).map(f => f.src || f.name || 'sem-src')")
    log_queue.put(f"  Iframes: {iframes}")

    try:
        page.get_by_text("AMHPTISS", exact=False).first.click(timeout=10000)
        page.wait_for_load_state("networkidle")
        log_queue.put(f"  URL apos clique: {page.url}")
    except Exception as e:
        log_queue.put(f"  Clique AMHPTISS falhou: {e}")

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

def rodar_exportacao(usuario, senha, quantidade, api_key, log_queue):
    arquivos_exportados = []
    try:
        log_queue.put("Iniciando navegador...")
        with sync_playwright() as p:
            if platform.system() == "Windows":
                browser = p.chromium.launch(
                    headless=HEADLESS,
                    executable_path=encontrar_chromium(),
                )
            else:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage"],
                )

            context = browser.new_context(
                accept_downloads=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            )
            page = context.new_page()

            log_queue.put("Autenticando via 2captcha...")
            ok = login_com_2captcha(page, usuario, senha, api_key, log_queue)

            if not ok or not validar_sessao(page):
                raise Exception("Login falhou. Verifique CNPJ, senha e saldo no 2captcha.")

            log_queue.put("Login realizado! Navegando para o Extrato...")
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

st.markdown("""
<style>
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}

    .block-container {
        padding-top: 1.5rem;
        padding-bottom: 1rem;
        max-width: 520px;
    }

    [data-testid="stForm"] {
        background: #FFFFFF;
        border-radius: 12px;
        padding: 1.5rem 2rem 1rem 2rem;
        box-shadow: 0 2px 16px rgba(14, 31, 59, 0.10);
        border: 1px solid #c5d8ea;
    }

    .stButton > button {
        background-color: #0E1F3B !important;
        color: #FFFFFF !important;
        border: none !important;
        border-radius: 8px !important;
        font-weight: 600 !important;
        font-size: 1rem !important;
        transition: background 0.2s !important;
    }

    .stButton > button:hover {
        background-color: #4A90C4 !important;
    }

    .stTextInput > div > div > input,
    .stNumberInput input {
        border-radius: 6px !important;
        border: 1.5px solid #c5d8ea !important;
    }

    [data-testid="stCodeBlock"] pre {
        background-color: #0E1F3B !important;
        color: #D6E4F0 !important;
        border-radius: 8px !important;
        font-size: 0.82rem !important;
    }

    .dev-footer {
        text-align: center;
        margin-top: 2.5rem;
        padding-top: 1rem;
        border-top: 1px solid #c5d8ea;
        color: #666;
        font-size: 0.78rem;
    }
</style>
""", unsafe_allow_html=True)

# Logo AMHP
if os.path.exists("amhplogo.png"):
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.image("amhplogo.png", use_container_width=True)
    st.markdown("<br>", unsafe_allow_html=True)
else:
    st.title("Exportar Extrato AMHP")

api_key = carregar_api_key()

if not api_key:
    st.error("Chave 2captcha nao configurada. Defina a variavel de ambiente ANTICAPTCHA_KEY.")
    st.stop()

with st.form("login_form"):
    usuario = st.text_input("CPF/CNPJ")
    senha   = st.text_input("Senha", type="password")
    qtd     = st.number_input("Quantos extratos recentes?", min_value=1, max_value=50, value=1, step=1)
    iniciar = st.form_submit_button("Exportar", use_container_width=True)

if iniciar:
    if not usuario or not senha:
        st.error("Preencha CPF/CNPJ e Senha.")
    else:
        log_queue = queue.Queue()
        thread = threading.Thread(
            target=rodar_exportacao,
            args=(usuario, senha, int(qtd), api_key, log_queue),
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

# Rodapé com logo do desenvolvedor
if os.path.exists("logo_b4strategy.png"):
    with open("logo_b4strategy.png", "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode()
    st.markdown(f"""
<div class="dev-footer">
    Desenvolvido por<br>
    <img src="data:image/png;base64,{logo_b64}" height="32" style="margin-top:6px; opacity:0.85;">
</div>
""", unsafe_allow_html=True)
