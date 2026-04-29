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
import re
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
    try:
        page.wait_for_function(
            "() => Array.from(document.scripts).some(s => s.src && s.src.includes('recaptcha'))",
            timeout=3000,
        )
    except Exception:
        pass

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

    return sitekey, versao, action


# ─── 2CAPTCHA ─────────────────────────────────────────────────────

def resolver_captcha(sitekey, page_url, api_key, log_queue, versao="v2", action=None):
    from twocaptcha import TwoCaptcha
    solver = TwoCaptcha(api_key)
    if versao == "v3":
        result = solver.recaptcha(
            sitekey=sitekey, url=page_url,
            version="v3", action=action or "submit", score=0.9,
        )
    else:
        result = solver.recaptcha(sitekey=sitekey, url=page_url)
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

    for selector in ["input[name='username']", "input[name='user']", "input[type='text']"]:
        try:
            loc = page.locator(selector).last
            if loc.count() > 0:
                loc.fill(usuario)
                break
        except Exception:
            continue

    page.locator("input[type='password']").fill(senha)

    sitekey, versao, action = detectar_sitekey(page, log_queue)
    if not sitekey:
        raise Exception("Sitekey do reCAPTCHA nao encontrado na pagina de login.")

    token = resolver_captcha(sitekey, page.url, api_key, log_queue, versao=versao, action=action)

    interceptado = [False]

    def trocar_token(route, request):
        if request.method == "POST" and "portal.amhp.com.br" in request.url:
            pd = request.post_data or ""
            if "g-recaptcha-response" in pd:
                try:
                    params = dict(urllib.parse.parse_qsl(pd, keep_blank_values=True))
                    params["g-recaptcha-response"] = token
                    interceptado[0] = True
                    route.continue_(post_data=urllib.parse.urlencode(params))
                    return
                except Exception:
                    pass
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

    return "perfil" in page.url


def validar_sessao(page):
    page.goto("https://portal.amhp.com.br/pages/PJ/perfil.html")
    page.wait_for_load_state("networkidle")
    return "perfil" in page.url


# ─── NAVEGACAO E EXPORTACAO ───────────────────────────────────────

def navegar_para_extrato(page, log_queue):
    # Após login bem-sucedido já estamos em perfil.html — sem goto redundante
    try:
        page.wait_for_selector("text=AMHPTISS", timeout=8000)
        page.get_by_text("AMHPTISS", exact=False).first.click()
        page.wait_for_load_state("load")
    except Exception:
        pass

    page.goto("https://amhptiss.amhp.com.br/Extrato.aspx")
    page.wait_for_load_state("networkidle")
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

def _criar_browser(p):
    if platform.system() == "Windows":
        return p.chromium.launch(headless=HEADLESS, executable_path=encontrar_chromium())
    return p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])


def _criar_context(browser):
    return browser.new_context(
        accept_downloads=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )


def sessao_unica_thread(usuario, senha, api_key, log_queue, cmd_queue):
    try:
        log_queue.put("Iniciando sessão...")
        with sync_playwright() as p:
            browser = _criar_browser(p)
            page = _criar_context(browser).new_page()

            log_queue.put("Autenticando...")
            ok = login_com_2captcha(page, usuario, senha, api_key, log_queue)

            if not ok:
                raise Exception("Login falhou. Verifique CPF/CNPJ, senha e saldo no 2captcha.")

            log_queue.put("Login realizado! Buscando referências disponíveis...")
            page = navegar_para_extrato(page, log_queue)
            refs = obter_referencias_disponiveis(page)
            log_queue.put(f"{len(refs)} referência(s) encontrada(s).")
            log_queue.put(("REFERENCIAS", refs))

            # Pausa aqui aguardando a seleção do usuário
            selecionadas = cmd_queue.get()

            if selecionadas is None:
                browser.close()
                return

            arquivos = []
            log_queue.put(f"Exportando {len(selecionadas)} referência(s)...")
            for i, ref in enumerate(selecionadas):
                log_queue.put(f"  [{i+1}/{len(selecionadas)}] {ref}")
                try:
                    caminho = exportar_csv(page, ref, usuario)
                    if caminho:
                        arquivos.append(caminho)
                        log_queue.put(f"  Salvo: {os.path.basename(caminho)}")
                    time.sleep(2)
                except Exception as e:
                    log_queue.put(f"  Erro em {ref}: {e}")

            browser.close()

        if arquivos:
            log_queue.put("Consolidando em Excel...")
            excel = consolidar_excel(arquivos, usuario)
            arquivos.append(excel)

        log_queue.put(("CONCLUIDO", arquivos))

    except Exception as e:
        import traceback
        log_queue.put(f"Erro: {e}")
        log_queue.put(traceback.format_exc())
        log_queue.put(("ERRO", str(e)))


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

    .stTextInput > div > div > input {
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

# Inicializa session_state
for _k, _v in [("step", "input"), ("referencias", []), ("usuario", ""), ("senha", ""),
               ("selecionadas", []), ("arquivos_finais", [])]:
    if _k not in st.session_state:
        st.session_state[_k] = _v

# ── Etapa 1: credenciais ──────────────────────────────────────────
if st.session_state.step == "input":
    if st.session_state.get("erro"):
        st.error(st.session_state.pop("erro"))

    with st.form("credentials_form"):
        usuario = st.text_input("CPF/CNPJ")
        senha   = st.text_input("Senha", type="password")
        buscar  = st.form_submit_button("Buscar Referências", use_container_width=True)

    if buscar:
        if not usuario or not senha:
            st.error("Preencha CPF/CNPJ e Senha.")
        else:
            st.session_state.usuario   = usuario
            st.session_state.senha     = senha
            st.session_state.log_queue = queue.Queue()
            st.session_state.cmd_queue = queue.Queue()
            t = threading.Thread(
                target=sessao_unica_thread,
                args=(usuario, senha, api_key,
                      st.session_state.log_queue, st.session_state.cmd_queue),
                daemon=True,
            )
            t.start()
            st.session_state.browser_thread = t
            st.session_state.step = "buscando"
            st.rerun()

# ── Etapa 2: aguardando referências ──────────────────────────────
elif st.session_state.step == "buscando":
    log_queue  = st.session_state.log_queue
    thread     = st.session_state.browser_thread
    ESTIMATIVA = 55  # segundos estimados para login + busca

    st.markdown("**Buscando referências disponíveis...**")
    progress_bar      = st.progress(0.0)
    col_timer, col_est = st.columns([1, 3])
    timer_ph          = col_timer.empty()
    col_est.caption(f"Tempo estimado: ~{ESTIMATIVA}s")
    status_ph         = st.empty()
    logs, refs, erro  = [], None, None
    start_time        = time.time()

    while refs is None and erro is None:
        elapsed  = time.time() - start_time
        progress_bar.progress(min(elapsed / ESTIMATIVA, 0.95))
        timer_ph.metric("⏱", f"{int(elapsed)}s")

        while not log_queue.empty():
            item = log_queue.get_nowait()
            if isinstance(item, tuple) and item[0] == "REFERENCIAS":
                refs = item[1]
            elif isinstance(item, tuple) and item[0] == "ERRO":
                erro = item[1]
            else:
                logs.append(str(item))
                status_ph.info(logs[-1])

        if refs is None and erro is None:
            if not thread.is_alive():
                erro = "Sessão encerrada inesperadamente."
                break
            time.sleep(0.2)

    if refs is not None:
        progress_bar.progress(1.0)

    if erro:
        st.session_state.cmd_queue.put(None)
        st.session_state.erro = erro
        st.session_state.step = "input"
    else:
        st.session_state.referencias = refs
        st.session_state.step = "select"
    st.rerun()

# ── Etapa 3: seleção de referências ──────────────────────────────
elif st.session_state.step == "select":
    st.markdown(f"**{len(st.session_state.referencias)} referência(s) disponível(is)**")

    selecionadas = st.multiselect(
        "Selecione as referências para exportar:",
        options=st.session_state.referencias,
        default=st.session_state.referencias[:1] if st.session_state.referencias else [],
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Voltar", use_container_width=True):
            st.session_state.cmd_queue.put(None)
            st.session_state.step = "input"
            st.rerun()
    with col2:
        exportar = st.button("Exportar Selecionados →", use_container_width=True)

    if exportar:
        if not selecionadas:
            st.error("Selecione ao menos uma referência.")
        else:
            st.session_state.selecionadas = selecionadas
            st.session_state.cmd_queue.put(selecionadas)
            st.session_state.step = "exportando"
            st.rerun()

# ── Etapa 4: exportando ───────────────────────────────────────────
elif st.session_state.step == "exportando":
    log_queue  = st.session_state.log_queue
    thread     = st.session_state.browser_thread
    total_refs = len(st.session_state.selecionadas)

    st.markdown(f"**Exportando {total_refs} referência(s)...**")
    progress_bar  = st.progress(0.0)
    timer_ph      = st.empty()
    log_ph        = st.empty()
    logs, arquivos_finais, erro = [], [], None
    start_time    = time.time()
    current_ref   = 0

    while thread.is_alive() or not log_queue.empty():
        elapsed = time.time() - start_time
        timer_ph.caption(f"⏱ {int(elapsed)}s")

        while not log_queue.empty():
            item = log_queue.get_nowait()
            if isinstance(item, tuple) and item[0] == "CONCLUIDO":
                arquivos_finais = item[1]
            elif isinstance(item, tuple) and item[0] == "ERRO":
                erro = item[1]
            else:
                msg = str(item)
                m = re.search(r'\[(\d+)/\d+\]', msg)
                if m:
                    current_ref = int(m.group(1))
                    progress_bar.progress((current_ref - 0.5) / total_refs)
                logs.append(msg)
                log_ph.code("\n".join(logs))
        time.sleep(0.2)

    progress_bar.progress(1.0)
    st.session_state.arquivos_finais = arquivos_finais
    if erro:
        st.session_state.erro = erro
    st.session_state.step = "done"
    st.rerun()

# ── Etapa 5: download ─────────────────────────────────────────────
elif st.session_state.step == "done":
    if st.session_state.get("erro"):
        st.error(st.session_state.pop("erro"))

    arquivos_finais = st.session_state.arquivos_finais
    csvs  = [f for f in arquivos_finais if f.endswith(".csv")]
    excel = next((f for f in arquivos_finais if f.endswith(".xlsx")), None)

    if arquivos_finais:
        st.success(f"Concluído! {len(csvs)} arquivo(s) exportado(s).")
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
        st.warning("Nenhum arquivo exportado. Verifique suas credenciais e tente novamente.")
        if os.path.exists("/tmp/amhp_debug.png"):
            with open("/tmp/amhp_debug.png", "rb") as f:
                st.image(f.read(), caption="Estado da página ao falhar")

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("Nova Exportação", use_container_width=True):
        for _k in ["step", "referencias", "usuario", "senha", "selecionadas",
                   "arquivos_finais", "log_queue", "cmd_queue", "browser_thread"]:
            st.session_state.pop(_k, None)
        st.rerun()

# ── Rodapé ────────────────────────────────────────────────────────
if os.path.exists("logo_b4strategy.png"):
    with open("logo_b4strategy.png", "rb") as f:
        logo_b64 = base64.b64encode(f.read()).decode()
    st.markdown(f"""
<div class="dev-footer">
    Desenvolvido por<br>
    <img src="data:image/png;base64,{logo_b64}" height="32" style="margin-top:6px; opacity:0.85;"><br>
    <a href="mailto:contato@b4strategy.com.br" style="color:#4A90C4; text-decoration:none; font-size:0.75rem;">
        contato@b4strategy.com.br
    </a>
</div>
""", unsafe_allow_html=True)
