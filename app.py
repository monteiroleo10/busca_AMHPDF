"""
Exportar Extrato AMHPTISS — Interface Streamlit

Fluxo:
  1. Login (uma única vez por sessão)
  2. Seleção de credenciado (pulada quando há apenas 1)
  3. Menu de relatórios
  4. Relatório escolhido → volta ao menu
"""
import streamlit as st
import streamlit.components.v1 as components
import threading
import queue
import os
import glob
import json
import platform
import time
import re
import urllib.parse
import base64
import pandas as pd
import openpyxl
from datetime import date
from playwright.sync_api import sync_playwright


# ─── CONFIGURACAO ─────────────────────────────────────────────────

if platform.system() == "Windows":
    PASTA_DESTINO = os.path.join(os.environ.get("USERPROFILE", ""), "Downloads")
else:
    PASTA_DESTINO = "/tmp/extratos_amhp"

HEADLESS = platform.system() != "Windows"

URL_PORTAL          = "https://portal.amhp.com.br/"
URL_PERFIL          = "https://portal.amhp.com.br/pages/PJ/perfil.html"
URL_EXTRATO         = "https://amhptiss.amhp.com.br/Extrato.aspx"
URL_ACOMPANHAMENTO  = "https://amhptiss.amhp.com.br/AcompanhamentoAtendimentoDigital.aspx"

CONFIG_FILE = os.path.join(
    os.path.expanduser("~"), ".amhp_app_config.json"
)


def carregar_api_key():
    return os.environ.get("ANTICAPTCHA_KEY", "")


def carregar_config():
    """Carrega preferências locais (último CPF/CNPJ, etc.). Nunca salva senha."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def salvar_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
    except Exception:
        pass


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

        const el = document.querySelector('[data-sitekey]');
        if (el) sitekey = el.getAttribute('data-sitekey');

        if (!sitekey) {
            for (const f of document.querySelectorAll('iframe')) {
                const m = f.src.match(/[?&]k=([^&]+)/);
                if (m) { sitekey = m[1]; break; }
            }
        }

        if (!sitekey) {
            for (const s of document.scripts) {
                if (s.src) {
                    const m = s.src.match(/[?&]render=([^&]+)/);
                    if (m && m[1] !== 'explicit') { sitekey = m[1]; versao = 'v3'; break; }
                }
            }
        }

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

    return resultado.get("sitekey"), resultado.get("versao", "v2"), resultado.get("action")


# ─── 2CAPTCHA ─────────────────────────────────────────────────────

def resolver_captcha(sitekey, page_url, api_key, versao="v2", action=None):
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

    page.goto(URL_PORTAL)
    # DOM pronto basta — o detectar_sitekey já espera o script do reCAPTCHA carregar.
    page.wait_for_load_state("domcontentloaded")

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
        raise Exception("captcha_sitekey_nao_encontrado")

    token = resolver_captcha(sitekey, page.url, api_key, versao=versao, action=action)

    def trocar_token(route, request):
        if request.method == "POST" and "portal.amhp.com.br" in request.url:
            pd_post = request.post_data or ""
            if "g-recaptcha-response" in pd_post:
                try:
                    params = dict(urllib.parse.parse_qsl(pd_post, keep_blank_values=True))
                    params["g-recaptcha-response"] = token
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


def classificar_erro_login(exc):
    """Converte erros técnicos do login em mensagens claras pro usuário."""
    msg = str(exc).lower()
    if "captcha_sitekey_nao_encontrado" in msg:
        return ("O site da AMHP não está mostrando o campo de captcha como deveria. "
                "Isso geralmente é uma instabilidade temporária do site. "
                "Aguarde alguns minutos e tente novamente.")
    if "saldo" in msg or "balance" in msg or "no_money" in msg or "zero_balance" in msg:
        return ("O serviço de captcha está sem créditos. "
                "Avise o Lucas para renovar a chave do 2captcha.")
    if "wrong" in msg or "incorrect" in msg or "invalid" in msg or "error_wrong" in msg:
        return ("O captcha foi resolvido, mas o site recusou. "
                "Pode ser CPF/CNPJ ou senha incorretos. "
                "Verifique seus dados e tente novamente.")
    if "timeout" in msg and ("portal.amhp" in msg or "amhp.com" in msg):
        return ("O site da AMHP está muito lento ou fora do ar. "
                "Tente novamente em alguns minutos.")
    if "net::" in msg or "connection" in msg:
        return ("Não foi possível conectar ao site da AMHP. "
                "Verifique sua internet ou tente novamente mais tarde.")
    return f"Não conseguimos completar o login. Detalhes técnicos: {exc}"


# ─── CREDENCIADOS ─────────────────────────────────────────────────

def obter_credenciados_disponiveis(page, log_queue):
    """
    Coleta a lista de credenciados disponíveis para esse login.
    Usa a página de Acompanhamento como ponto de coleta (sabemos que o
    dropdown está lá; o mesmo conjunto vale para todos os relatórios).
    """
    try:
        page.wait_for_selector("text=AMHPTISS", timeout=8000)
        page.get_by_text("AMHPTISS", exact=False).first.click()
        page.wait_for_load_state("load")
    except Exception:
        pass

    page.goto(URL_ACOMPANHAMENTO)
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_selector("#ctl00_MainContent_rcbCredenciado_Input", timeout=30000)
    page.locator("#ctl00_MainContent_rcbCredenciado_Input").click()
    page.wait_for_selector(
        "#ctl00_MainContent_rcbCredenciado_DropDown .rcbList li",
        state="visible", timeout=10000,
    )
    itens = page.locator("#ctl00_MainContent_rcbCredenciado_DropDown .rcbList li")
    credenciados = []
    for i in range(itens.count()):
        txt = itens.nth(i).text_content().strip()
        if txt:
            credenciados.append(txt)
    page.keyboard.press("Escape")
    return credenciados


def selecionar_credenciado_no_dropdown(page, credenciado, prefixo_id):
    """
    Seleciona um credenciado em um dropdown do tipo RadComboBox (Telerik).
    `prefixo_id` é o prefixo do componente, ex: 'ctl00_MainContent_rcbCredenciado'.
    """
    codigo = credenciado.split(" - ")[0].strip()
    input_sel    = f"#{prefixo_id}_Input"
    dropdown_sel = f"#{prefixo_id}_DropDown .rcbList li"

    page.wait_for_selector(input_sel, timeout=15000)
    page.locator(input_sel).click()
    page.wait_for_selector(dropdown_sel, state="visible", timeout=10000)
    page.locator(f"{dropdown_sel}:has-text('{codigo}')").first.click()
    # Espera o dropdown fechar para confirmar que a seleção foi aceita.
    try:
        page.wait_for_selector(dropdown_sel, state="hidden", timeout=3000)
    except Exception:
        pass


# ─── QUITAÇÃO ─────────────────────────────────────────────────────

def navegar_para_extrato(page, credenciado, log_queue):
    """Vai para a página de Extrato e seleciona o credenciado."""
    page.goto(URL_EXTRATO)
    # DOM pronto basta — temos waits específicos abaixo.
    page.wait_for_load_state("domcontentloaded")

    try:
        page.wait_for_selector("#ctl00_MainContent_rcbCredenciado_Input", timeout=5000)
        log_queue.put("Selecionando credenciado na página de Extrato...")
        selecionar_credenciado_no_dropdown(page, credenciado, "ctl00_MainContent_rcbCredenciado")
        # Após a seleção, esperar a página estabilizar (a AMHP refaz a lista de referências).
        page.wait_for_load_state("networkidle")
    except Exception:
        # Página de Extrato não tem seletor de credenciado (caso de login com só 1).
        pass

    # Aguarda o dropdown de referências aparecer.
    page.wait_for_selector("#ctl00_MainContent_rcbReferencia_Input", timeout=20000)


def obter_referencias_disponiveis(page):
    """Lista as referências (meses) disponíveis para exportar."""
    page.locator("#ctl00_MainContent_rcbReferencia_Input").click()
    page.wait_for_selector(
        "#ctl00_MainContent_rcbReferencia_DropDown .rcbList li",
        state="visible", timeout=15000,
    )
    refs = page.evaluate("""() =>
        Array.from(document.querySelectorAll('#ctl00_MainContent_rcbReferencia_DropDown .rcbList li'))
            .map(el => el.textContent.trim())
            .filter(t => t.length > 0)
    """)
    page.keyboard.press("Escape")
    return refs


def selecionar_referencia(page, texto_referencia):
    page.locator("#ctl00_MainContent_rcbReferencia_Input").click()
    page.wait_for_selector(
        "#ctl00_MainContent_rcbReferencia_DropDown .rcbList li",
        state="visible", timeout=10000,
    )
    page.locator(
        f"#ctl00_MainContent_rcbReferencia_DropDown .rcbList li:has-text('{texto_referencia}')"
    ).first.click(timeout=10000)
    # Espera o dropdown fechar para confirmar a seleção.
    try:
        page.wait_for_selector(
            "#ctl00_MainContent_rcbReferencia_DropDown .rcbList li",
            state="hidden", timeout=3000,
        )
    except Exception:
        pass


def ler_tabela_extrato_visivel(page):
    """
    Lê a tabela de lançamentos que fica visível na página de Extrato após
    selecionar uma referência. Retorna (cabecalho, linhas) ou ([], []) se
    não conseguir encontrar a tabela.
    """
    try:
        page.wait_for_selector(
            "#ctl00_MainContent_pnlExtrato table.rgMasterTable, "
            "table.rgMasterTable",
            state="visible", timeout=15000,
        )
    except Exception:
        return [], []

    dados = page.evaluate("""() => {
        const tables = Array.from(document.querySelectorAll('table.rgMasterTable'));
        if (!tables.length) return null;
        // Escolhe a tabela com mais linhas (a do extrato é a principal).
        let melhor = tables[0];
        for (const t of tables) {
            if (t.rows.length > melhor.rows.length) melhor = t;
        }
        return Array.from(melhor.rows).map(row =>
            Array.from(row.cells).map(c => (c.innerText || '').trim())
        );
    }""")

    if not dados or len(dados) < 1:
        return [], []
    return dados[0], dados[1:]


def exportar_csv(page, texto_referencia, usuario):
    selecionar_referencia(page, texto_referencia)
    page.locator("#ctl00_MainContent_rbtExportarCsv_input").click()

    caminho = None
    try:
        # Espera o iframe do popup aparecer — substitui a antiga pausa fixa de 2s.
        page.wait_for_selector("iframe[src*='ExtratoExportacao'][tabindex='0']", timeout=15000)
        frame = page.frame(url="*ExtratoExportacao*")
        if frame:
            frame.wait_for_load_state("load")

        popup = page.frame_locator("iframe[src*='ExtratoExportacao'][tabindex='0']")
        # Espera o conteúdo do popup estar interativo.
        popup.locator("#rbtExportarCsv_input").wait_for(state="visible", timeout=15000)

        try:
            popup.locator("a.rlbTransferAllFrom").first.click(timeout=5000)
        except Exception:
            pass

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


# ─── ACOMPANHAMENTO ───────────────────────────────────────────────

def buscar_acompanhamento(page, data_ini, data_fim, credenciado, log_queue):
    log_queue.put("Navegando para a página de envios digitais...")
    page.goto(URL_ACOMPANHAMENTO)
    page.wait_for_selector("#ctl00_MainContent_rdpDataInicio_dateInput", timeout=20000)

    log_queue.put("Preenchendo filtros...")
    campo_ini = page.locator("#ctl00_MainContent_rdpDataInicio_dateInput")
    campo_ini.click()
    campo_ini.press("Control+a")
    campo_ini.type(data_ini)
    campo_ini.press("Tab")

    campo_fim = page.locator("#ctl00_MainContent_rdpDataFim_dateInput")
    campo_fim.click()
    campo_fim.press("Control+a")
    campo_fim.type(data_fim)
    campo_fim.press("Tab")

    selecionar_credenciado_no_dropdown(page, credenciado, "ctl00_MainContent_rcbCredenciado")

    log_queue.put("Executando busca...")
    page.locator("#ctl00_MainContent_btnBuscarAtendimentos_input").click()
    try:
        page.wait_for_selector(".raDiv", state="hidden", timeout=30000)
    except Exception:
        pass
    # Espera a tabela renderizar (substitui pausa fixa de 2s).
    try:
        page.wait_for_selector(
            "#ctl00_MainContent_rdgAcompanhamentoDigital table.rgMasterTable tr",
            timeout=10000,
        )
    except Exception:
        pass

    log_queue.put("Extraindo dados da tabela...")
    linhas = page.locator(
        "#ctl00_MainContent_rdgAcompanhamentoDigital table.rgMasterTable tr"
    ).all()
    dados = []
    for linha in linhas:
        celulas = linha.locator("th, td").all()
        row = [c.text_content().strip() for c in celulas]
        if any(row):
            dados.append(row)
    return dados


def salvar_acompanhamento_xlsx(dados, credenciado, data_ini, data_fim):
    """Cria a planilha Excel a partir dos dados extraídos da tabela."""
    os.makedirs(PASTA_DESTINO, exist_ok=True)
    credenciado_slug = credenciado[:6].strip().replace(" ", "_")
    nome_arq = os.path.join(
        PASTA_DESTINO,
        f"Acompanhamento_{credenciado_slug}_{data_ini.replace('/', '')}_{data_fim.replace('/', '')}.xlsx",
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Envios Digitais"
    for row in dados:
        ws.append(row)
    # Coluna J (índice 10) — converte para número quando possível
    for row in ws.iter_rows(min_row=2, min_col=10, max_col=10):
        for cell in row:
            if cell.value:
                try:
                    cell.value = float(str(cell.value).replace(",", ".").replace(" ", ""))
                except (ValueError, TypeError):
                    pass
    for col in ws.columns:
        max_len = max((len(str(cell.value)) for cell in col if cell.value), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 50)
    wb.save(nome_arq)
    return nome_arq


# ─── BROWSER ──────────────────────────────────────────────────────

def _criar_browser(p):
    if platform.system() == "Windows":
        return p.chromium.launch(headless=HEADLESS, executable_path=encontrar_chromium())
    return p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])


def _criar_context(browser):
    return browser.new_context(
        accept_downloads=True,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    )


# ─── THREAD DE SESSAO PERSISTENTE ─────────────────────────────────
#
# Mantém o navegador aberto entre operações. Faz login uma vez,
# coleta credenciados, e fica em loop esperando comandos da UI.
#
# Comandos (cmd_queue):
#   ("RODAR_QUITACAO", credenciado, refs_selecionadas)
#   ("LISTAR_REFS_QUITACAO", credenciado)
#   ("RODAR_ACOMPANHAMENTO", credenciado, data_ini, data_fim)
#   ("SAIR",)
#
# Eventos (log_queue):
#   str  → mensagem de status / log
#   ("CREDENCIADOS", lista)
#   ("REFERENCIAS", lista)
#   ("QUITACAO_OK", arquivos)
#   ("ACOMPANHAMENTO_OK", nome_arq, total)
#   ("ERRO_LOGIN", msg_amigavel)
#   ("ERRO_OPERACAO", msg_amigavel)
#   ("ENCERRADA",)

def sessao_persistente_thread(usuario, senha, api_key, log_queue, cmd_queue):
    try:
        log_queue.put("Iniciando sessão...")
        with sync_playwright() as p:
            browser = _criar_browser(p)
            page    = _criar_context(browser).new_page()

            # ── Login ───────────────────────────────────────────
            log_queue.put("Autenticando no portal AMHP...")
            try:
                ok = login_com_2captcha(page, usuario, senha, api_key, log_queue)
            except Exception as e:
                log_queue.put(("ERRO_LOGIN", classificar_erro_login(e)))
                browser.close()
                return

            if not ok:
                log_queue.put(("ERRO_LOGIN",
                    "O login no portal AMHP não foi confirmado. "
                    "Pode ser CPF/CNPJ ou senha incorretos, ou o site pode estar instável. "
                    "Verifique seus dados e tente novamente."))
                browser.close()
                return

            # ── Coleta de credenciados ──────────────────────────
            log_queue.put("Login realizado! Buscando credenciados disponíveis...")
            try:
                credenciados = obter_credenciados_disponiveis(page, log_queue)
            except Exception as e:
                log_queue.put(("ERRO_LOGIN",
                    f"Login OK, mas não conseguimos carregar a lista de credenciados. "
                    f"Detalhes: {e}"))
                browser.close()
                return

            log_queue.put(f"{len(credenciados)} credenciado(s) encontrado(s).")
            log_queue.put(("CREDENCIADOS", credenciados))

            # ── Loop de comandos ────────────────────────────────
            while True:
                cmd = cmd_queue.get()
                if cmd is None or (isinstance(cmd, tuple) and cmd and cmd[0] == "SAIR"):
                    break

                acao = cmd[0]

                try:
                    if acao == "LISTAR_REFS_QUITACAO":
                        _, credenciado = cmd
                        log_queue.put(f"Abrindo Extrato para {credenciado}...")
                        navegar_para_extrato(page, credenciado, log_queue)
                        log_queue.put("Buscando referências disponíveis...")
                        refs = obter_referencias_disponiveis(page)
                        log_queue.put(f"{len(refs)} referência(s) encontrada(s).")
                        log_queue.put(("REFERENCIAS", refs))

                    elif acao == "PREVIEW_QUITACAO":
                        _, referencia = cmd
                        log_queue.put(f"Carregando dados de {referencia}...")
                        selecionar_referencia(page, referencia)
                        cabecalho, linhas = ler_tabela_extrato_visivel(page)
                        log_queue.put(f"Tabela carregada: {len(linhas)} linha(s).")
                        log_queue.put(("PREVIEW_QUITACAO_OK", referencia, cabecalho, linhas))

                    elif acao == "RODAR_QUITACAO":
                        _, credenciado, selecionadas = cmd
                        arquivos = []
                        total = len(selecionadas)
                        log_queue.put(f"Exportando {total} referência(s)...")
                        for i, ref in enumerate(selecionadas):
                            log_queue.put(f"  [{i+1}/{total}] {ref}")
                            try:
                                caminho = exportar_csv(page, ref, usuario)
                                if caminho:
                                    arquivos.append(caminho)
                                    log_queue.put(f"  Salvo: {os.path.basename(caminho)}")
                                # Pausa curta entre exportações para o site se estabilizar.
                                time.sleep(0.5)
                            except Exception as e:
                                log_queue.put(f"  Erro em {ref}: {e}")

                        # Só gera o consolidado quando há mais de 1 arquivo —
                        # com um CSV só, o consolidado seria redundante.
                        if len(arquivos) > 1:
                            log_queue.put("Consolidando em Excel...")
                            excel = consolidar_excel(arquivos, usuario)
                            arquivos.append(excel)
                        log_queue.put(("QUITACAO_OK", arquivos))

                    elif acao == "RODAR_ACOMPANHAMENTO":
                        _, credenciado, data_ini, data_fim = cmd
                        dados = buscar_acompanhamento(page, data_ini, data_fim, credenciado, log_queue)
                        if not dados or len(dados) <= 1:
                            log_queue.put(("ERRO_OPERACAO",
                                "Nenhum dado encontrado para esse credenciado e período. "
                                "Verifique as datas e tente novamente."))
                        else:
                            nome_arq = salvar_acompanhamento_xlsx(dados, credenciado, data_ini, data_fim)
                            log_queue.put(("ACOMPANHAMENTO_OK", nome_arq, len(dados) - 1, dados))

                    else:
                        log_queue.put(f"Comando desconhecido: {acao}")

                except Exception as e:
                    import traceback
                    log_queue.put(traceback.format_exc())
                    log_queue.put(("ERRO_OPERACAO",
                        f"Houve um erro ao executar essa operação. "
                        f"A sessão continua ativa — você pode tentar de novo. "
                        f"Detalhes: {e}"))

            browser.close()
        log_queue.put(("ENCERRADA",))

    except Exception as e:
        import traceback
        log_queue.put(traceback.format_exc())
        log_queue.put(("ERRO_LOGIN", f"Erro fatal na sessão: {e}"))


# ─── HELPERS DE UI ────────────────────────────────────────────────

def drenar_log_queue(log_queue, eventos_estruturados, logs_texto):
    """Lê tudo o que está na fila de log. Separa eventos estruturados (tuplas)
    de mensagens de texto. Retorna True se algum evento estruturado chegou."""
    apareceu_evento = False
    while not log_queue.empty():
        try:
            item = log_queue.get_nowait()
        except queue.Empty:
            break
        if isinstance(item, tuple):
            eventos_estruturados.append(item)
            apareceu_evento = True
        else:
            logs_texto.append(str(item))
    return apareceu_evento


def encerrar_sessao_silenciosa():
    """Manda o comando de encerrar para a thread, se existir."""
    cmd_queue = st.session_state.get("cmd_queue")
    if cmd_queue is not None:
        try:
            cmd_queue.put(("SAIR",))
        except Exception:
            pass


def esvaziar_log_queue():
    """Descarta mensagens antigas pendentes na fila de log, antes de uma nova operação."""
    log_queue = st.session_state.get("log_queue")
    if log_queue is None:
        return
    while not log_queue.empty():
        try:
            log_queue.get_nowait()
        except queue.Empty:
            break


def resetar_sessao():
    """Limpa todo o estado da sessão (após sair ou erro fatal)."""
    encerrar_sessao_silenciosa()
    for k in ["step", "usuario", "senha", "log_queue", "cmd_queue", "browser_thread",
              "credenciados", "credenciado_atual", "referencias", "referencia_atual",
              "preview_cabecalho", "preview_linhas", "selecionadas",
              "arquivos_quitacao", "acomp_arquivo", "acomp_total", "acomp_dados",
              "logs_acumulados", "fluxo_atual",
              "quitacao_celebrou", "acomp_celebrou"]:
        st.session_state.pop(k, None)
    st.session_state.step = "input"


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
        max-width: 560px;
    }

    [data-testid="stForm"] {
        background: #FFFFFF;
        border-radius: 12px;
        padding: 1.5rem 2rem 1rem 2rem;
        box-shadow: 0 2px 16px rgba(14, 31, 59, 0.10);
        border: 1px solid #c5d8ea;
    }

    .stButton > button,
    .stFormSubmitButton > button {
        border-radius: 8px !important;
        font-weight: 600 !important;
        font-size: 1rem !important;
        transition: background 0.2s, border-color 0.2s !important;
        min-height: 2.5rem !important;
    }

    /* Botão primário (azul escuro) — usado em ações principais */
    .stButton > button[kind="primary"],
    .stFormSubmitButton > button {
        background-color: #0E1F3B !important;
        color: #FFFFFF !important;
        border: 1.5px solid #0E1F3B !important;
    }
    .stButton > button[kind="primary"]:hover,
    .stFormSubmitButton > button:hover {
        background-color: #4A90C4 !important;
        border-color: #4A90C4 !important;
    }

    /* Botão secundário (branco com borda) — usado em ações de saída/voltar */
    .stButton > button[kind="secondary"] {
        background-color: #FFFFFF !important;
        color: #0E1F3B !important;
        border: 1.5px solid #c5d8ea !important;
    }
    .stButton > button[kind="secondary"]:hover {
        background-color: #f0f4fa !important;
        border-color: #4A90C4 !important;
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

    .sessao-ativa {
        background-color: #e8f4fc;
        border-left: 3px solid #4A90C4;
        padding: 0.5rem 0.75rem;
        border-radius: 4px;
        font-size: 0.82rem;
        color: #0E1F3B;
        margin-bottom: 1rem;
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
else:
    st.title("Exportar Extrato AMHP")

st.markdown(
    "<p style='text-align:center; color:#4A7FA5; font-size:0.95rem; margin-top:0.25rem; margin-bottom:1.25rem;'>"
    "Acompanhe os envios e baixas de faturamento da AMHPDF em um só lugar."
    "</p>",
    unsafe_allow_html=True,
)

api_key = carregar_api_key()

# Inicializa session_state
DEFAULTS = {
    "step": "input",
    "usuario": "",
    "senha": "",
    "credenciados": [],
    "credenciado_atual": None,
    "referencias": [],
    "referencia_atual": None,
    "preview_cabecalho": [],
    "preview_linhas": [],
    "selecionadas": [],
    "arquivos_quitacao": [],
    "acomp_arquivo": None,
    "acomp_total": 0,
    "acomp_dados": [],
    "logs_acumulados": [],
    "fluxo_atual": None,  # 'quitacao' ou 'acompanhamento'
    "erro": None,
}
for k, v in DEFAULTS.items():
    if k not in st.session_state:
        st.session_state[k] = v


def proteger_contra_fechar_aba():
    """
    Injeta JS que faz o navegador perguntar 'Tem certeza que quer sair?' quando
    o usuário tenta fechar a aba ou recarregar. Importante porque hoje fechar
    a aba no meio de uma operação deixa a sessão (e o browser do robô) órfã.
    Os navegadores modernos mostram um diálogo genérico — a mensagem custom
    não é exibida por segurança, mas o aviso aparece.
    """
    components.html(
        """
        <script>
        const win = window.parent || window;
        if (!win.__amhp_unload_attached) {
            win.addEventListener('beforeunload', function(e) {
                e.preventDefault();
                e.returnValue = '';
                return '';
            });
            win.__amhp_unload_attached = true;
        }
        </script>
        """,
        height=0,
    )


def banner_sessao_ativa():
    """Mostra um banner indicando que há uma sessão ativa no navegador."""
    proteger_contra_fechar_aba()
    cred = st.session_state.get("credenciado_atual")
    if cred:
        st.markdown(
            f"<div class='sessao-ativa'>🟢 <b>Sessão ativa</b> — {cred}<br>"
            "<small>Não feche esta aba até concluir. Use 'Sair' para encerrar a sessão.</small></div>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<div class='sessao-ativa'>🟢 <b>Sessão ativa</b><br>"
            "<small>Não feche esta aba até concluir.</small></div>",
            unsafe_allow_html=True,
        )


def botao_cancelar_operacao(key):
    """Renderiza um botão de cancelar que encerra a sessão inteira."""
    if st.button(
        "✖ Cancelar operação (encerra sessão)",
        use_container_width=True,
        key=key,
        type="secondary",
        help="Cancela a operação em andamento e fecha a sessão no navegador. Você precisará logar de novo.",
    ):
        resetar_sessao()
        st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Login (formulário)
# ──────────────────────────────────────────────────────────────────
if st.session_state.step == "input":
    if st.session_state.get("erro"):
        st.error(st.session_state.pop("erro"))

    if not api_key:
        st.error("Chave 2captcha não configurada. Defina a variável de ambiente ANTICAPTCHA_KEY.")
    else:
        config = carregar_config()
        usuario_default = config.get("ultimo_usuario", "")

        st.info(
            "🔐 Use o **mesmo CPF/CNPJ e senha** que você usa para entrar no site da AMHP "
            "([portal.amhp.com.br](https://portal.amhp.com.br/)). "
            "Nada é armazenado em nenhum servidor — o app apenas automatiza o acesso ao portal por você."
        )

        with st.form("login_form"):
            usuario = st.text_input("CPF/CNPJ", value=usuario_default)
            senha   = st.text_input("Senha", type="password")
            entrar  = st.form_submit_button("Entrar", use_container_width=True)

        if entrar:
            if not usuario or not senha:
                st.error("Preencha CPF/CNPJ e Senha.")
            else:
                salvar_config({"ultimo_usuario": usuario})
                st.session_state.usuario   = usuario
                st.session_state.senha     = senha
                st.session_state.log_queue = queue.Queue()
                st.session_state.cmd_queue = queue.Queue()
                t = threading.Thread(
                    target=sessao_persistente_thread,
                    args=(usuario, senha, api_key,
                          st.session_state.log_queue, st.session_state.cmd_queue),
                    daemon=True,
                )
                t.start()
                st.session_state.browser_thread = t
                st.session_state.step = "logando"
                st.session_state.logs_acumulados = []
                st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Autenticando + buscando credenciados
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "logando":
    proteger_contra_fechar_aba()
    log_queue  = st.session_state.log_queue
    thread     = st.session_state.browser_thread
    ESTIMATIVA = 55  # segundos

    st.markdown("**Conectando à AMHP...**")

    progress_bar = st.progress(0.0)
    col_t, col_m = st.columns([1, 3])
    timer_ph     = col_t.empty()
    msg_ph       = col_m.empty()
    status_ph    = st.empty()

    # Botão cancelar
    col_a, col_b = st.columns([3, 1])
    with col_b:
        if st.button("Cancelar", use_container_width=True, key="cancelar_login", type="secondary"):
            resetar_sessao()
            st.rerun()

    start_time   = time.time()
    eventos, logs = [], st.session_state.logs_acumulados

    credenciados, erro = None, None

    while credenciados is None and erro is None:
        elapsed = time.time() - start_time
        progress_bar.progress(min(elapsed / ESTIMATIVA, 0.95))
        timer_ph.metric("⏱", f"{int(elapsed)}s")

        if elapsed < ESTIMATIVA:
            msg_ph.caption(f"Tempo estimado: ~{ESTIMATIVA}s")
        else:
            msg_ph.caption("⚠️ Tá demorando mais que o normal — o site da AMHP pode estar lento.")

        drenar_log_queue(log_queue, eventos, logs)

        for ev in eventos:
            if ev[0] == "CREDENCIADOS":
                credenciados = ev[1]
            elif ev[0] == "ERRO_LOGIN":
                erro = ev[1]
        eventos.clear()

        if logs:
            status_ph.info(logs[-1])

        if credenciados is None and erro is None:
            if not thread.is_alive():
                erro = "A sessão encerrou inesperadamente. Tente novamente."
                break
            time.sleep(0.2)

    progress_bar.progress(1.0)

    if erro:
        resetar_sessao()
        st.session_state.erro = erro
        st.rerun()
    else:
        st.session_state.credenciados = credenciados
        # Se só tem 1 credenciado, pula a tela de seleção
        if len(credenciados) == 1:
            st.session_state.credenciado_atual = credenciados[0]
            st.session_state.step = "menu"
        else:
            st.session_state.step = "escolher_credenciado"
        st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Escolher credenciado
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "escolher_credenciado":
    banner_sessao_ativa()
    st.markdown(f"**{len(st.session_state.credenciados)} credenciado(s) disponível(is)**")
    st.caption("Escolha o credenciado para o qual você quer gerar relatórios.")

    escolha = st.radio(
        "Credenciado:",
        options=st.session_state.credenciados,
        index=0,
        label_visibility="collapsed",
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Sair", use_container_width=True, key="sair_cred", type="secondary"):
            resetar_sessao()
            st.rerun()
    with col2:
        if st.button("Continuar →", use_container_width=True, key="cont_cred", type="primary"):
            st.session_state.credenciado_atual = escolha
            st.session_state.step = "menu"
            st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Menu principal
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "menu":
    banner_sessao_ativa()

    if st.session_state.get("erro"):
        st.error(st.session_state.pop("erro"))

    st.markdown("**O que você quer fazer?**")

    if st.button("📄 Relatório de Quitações", use_container_width=True, key="btn_quit", type="primary"):
        st.session_state.fluxo_atual = "quitacao"
        st.session_state.step = "quitacao_listando"
        st.session_state.logs_acumulados = []
        esvaziar_log_queue()
        st.session_state.cmd_queue.put(("LISTAR_REFS_QUITACAO", st.session_state.credenciado_atual))
        st.rerun()

    if st.button("📋 Acompanhamento de Envios Digitais", use_container_width=True, key="btn_acomp", type="primary"):
        st.session_state.fluxo_atual = "acompanhamento"
        st.session_state.step = "acomp_filtros"
        st.rerun()

    st.markdown("---")

    col1, col2 = st.columns(2)
    with col1:
        if len(st.session_state.credenciados) > 1:
            if st.button("↺ Trocar credenciado", use_container_width=True, key="btn_trocar", type="secondary"):
                st.session_state.step = "escolher_credenciado"
                st.rerun()
    with col2:
        if st.button("⎋ Encerrar sessão", use_container_width=True, key="btn_sair_menu", type="secondary"):
            resetar_sessao()
            st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Quitação — buscando referências
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "quitacao_listando":
    banner_sessao_ativa()
    log_queue = st.session_state.log_queue
    thread    = st.session_state.browser_thread
    ESTIMATIVA = 20

    st.markdown("**Buscando referências disponíveis...**")
    progress_bar = st.progress(0.0)
    col_t, col_m = st.columns([1, 3])
    timer_ph     = col_t.empty()
    msg_ph       = col_m.empty()
    status_ph    = st.empty()
    botao_cancelar_operacao(key="cancel_quit_list")

    start_time = time.time()
    eventos    = []
    logs       = st.session_state.logs_acumulados

    refs, erro = None, None
    while refs is None and erro is None:
        elapsed = time.time() - start_time
        progress_bar.progress(min(elapsed / ESTIMATIVA, 0.95))
        timer_ph.metric("⏱", f"{int(elapsed)}s")
        if elapsed < ESTIMATIVA:
            msg_ph.caption(f"Tempo estimado: ~{ESTIMATIVA}s")
        else:
            msg_ph.caption("⚠️ Tá demorando mais que o normal...")

        drenar_log_queue(log_queue, eventos, logs)
        for ev in eventos:
            if ev[0] == "REFERENCIAS":
                refs = ev[1]
            elif ev[0] == "ERRO_OPERACAO":
                erro = ev[1]
            elif ev[0] == "ERRO_LOGIN":
                erro = ev[1]
        eventos.clear()

        if logs:
            status_ph.info(logs[-1])

        if refs is None and erro is None:
            if not thread.is_alive():
                erro = "A sessão encerrou inesperadamente."
                break
            time.sleep(0.2)

    progress_bar.progress(1.0)

    if erro:
        st.session_state.erro = erro
        st.session_state.step = "menu"
        st.rerun()
    else:
        st.session_state.referencias = refs
        st.session_state.step = "quitacao_selecionar"
        st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Quitação — selecionar referências
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "quitacao_selecionar":
    banner_sessao_ativa()
    st.markdown(f"**{len(st.session_state.referencias)} referência(s) disponível(is)**")
    st.caption("Escolha uma referência para visualizar antes de exportar.")

    referencia = st.selectbox(
        "Referência:",
        options=st.session_state.referencias,
        index=0,
        key="ref_unica",
        label_visibility="collapsed",
    )

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Voltar ao menu", use_container_width=True,
                     key="voltar_quit_sel", type="secondary"):
            st.session_state.step = "menu"
            st.rerun()
    with col2:
        if st.button("👁 Visualizar dados →", use_container_width=True,
                     key="ver_quit", type="primary"):
            st.session_state.referencia_atual = referencia
            st.session_state.logs_acumulados = []
            esvaziar_log_queue()
            st.session_state.cmd_queue.put(("PREVIEW_QUITACAO", referencia))
            st.session_state.step = "quitacao_carregando_preview"
            st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Quitação — carregando preview da referência escolhida
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "quitacao_carregando_preview":
    banner_sessao_ativa()
    log_queue = st.session_state.log_queue
    thread    = st.session_state.browser_thread
    ESTIMATIVA = 10

    st.markdown(f"**Carregando dados de {st.session_state.referencia_atual}...**")
    progress_bar = st.progress(0.0)
    col_t, col_m = st.columns([1, 3])
    timer_ph     = col_t.empty()
    msg_ph       = col_m.empty()
    status_ph    = st.empty()
    botao_cancelar_operacao(key="cancel_quit_preview")

    start_time = time.time()
    eventos    = []
    logs       = st.session_state.logs_acumulados
    preview, erro = None, None

    while preview is None and erro is None:
        elapsed = time.time() - start_time
        progress_bar.progress(min(elapsed / ESTIMATIVA, 0.95))
        timer_ph.metric("⏱", f"{int(elapsed)}s")
        if elapsed < ESTIMATIVA:
            msg_ph.caption(f"Tempo estimado: ~{ESTIMATIVA}s")
        else:
            msg_ph.caption("⚠️ Tá demorando mais que o normal...")

        drenar_log_queue(log_queue, eventos, logs)
        for ev in eventos:
            if ev[0] == "PREVIEW_QUITACAO_OK":
                preview = ev  # ("PREVIEW_QUITACAO_OK", ref, cabecalho, linhas)
            elif ev[0] in ("ERRO_OPERACAO", "ERRO_LOGIN"):
                erro = ev[1]
        eventos.clear()

        if logs:
            status_ph.info(logs[-1])

        if preview is None and erro is None:
            if not thread.is_alive():
                erro = "A sessão encerrou inesperadamente."
                break
            time.sleep(0.2)

    progress_bar.progress(1.0)

    if erro:
        st.session_state.erro = erro
        st.session_state.step = "menu"
    else:
        _, ref, cabecalho, linhas = preview
        st.session_state.preview_cabecalho = cabecalho
        st.session_state.preview_linhas    = linhas
        st.session_state.step              = "quitacao_preview"
    st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Quitação — preview da tabela
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "quitacao_preview":
    banner_sessao_ativa()
    ref = st.session_state.referencia_atual
    cabecalho = st.session_state.preview_cabecalho
    linhas    = st.session_state.preview_linhas

    st.markdown(f"**Referência:** {ref}")
    st.caption(f"📊 {len(linhas)} linha(s) encontrada(s)")

    if linhas:
        try:
            df = pd.DataFrame(linhas, columns=cabecalho or None)
        except Exception:
            df = pd.DataFrame(linhas)
        st.dataframe(df, use_container_width=True, hide_index=True, height=400)
    else:
        st.warning("Não foi possível carregar a tabela na tela. Você ainda pode tentar exportar diretamente.")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Escolher outra", use_container_width=True,
                     key="outra_ref", type="secondary"):
            st.session_state.pop("preview_cabecalho", None)
            st.session_state.pop("preview_linhas", None)
            st.session_state.step = "quitacao_selecionar"
            st.rerun()
    with col2:
        if st.button("⬇️ Exportar este (CSV) →", use_container_width=True,
                     key="exp_quit_unica", type="primary"):
            st.session_state.selecionadas = [ref]
            st.session_state.logs_acumulados = []
            esvaziar_log_queue()
            st.session_state.cmd_queue.put(
                ("RODAR_QUITACAO", st.session_state.credenciado_atual, [ref])
            )
            st.session_state.step = "quitacao_exportando"
            st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Quitação — exportando
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "quitacao_exportando":
    banner_sessao_ativa()
    log_queue = st.session_state.log_queue
    thread    = st.session_state.browser_thread
    total_refs = len(st.session_state.selecionadas)

    st.markdown(f"**Exportando {total_refs} referência(s)...**")
    progress_bar = st.progress(0.0)
    timer_ph     = st.empty()
    log_ph       = st.empty()
    botao_cancelar_operacao(key="cancel_quit_exp")

    start_time   = time.time()
    eventos      = []
    logs         = st.session_state.logs_acumulados
    arquivos     = None
    erro         = None
    current_ref  = 0

    while arquivos is None and erro is None:
        elapsed = time.time() - start_time
        timer_ph.caption(f"⏱ {int(elapsed)}s")

        drenar_log_queue(log_queue, eventos, logs)
        for ev in eventos:
            if ev[0] == "QUITACAO_OK":
                arquivos = ev[1]
            elif ev[0] in ("ERRO_OPERACAO", "ERRO_LOGIN"):
                erro = ev[1]
        eventos.clear()

        for msg in logs[-10:]:
            m = re.search(r'\[(\d+)/\d+\]', msg)
            if m:
                current_ref = int(m.group(1))
                progress_bar.progress((current_ref - 0.5) / total_refs)
        if logs:
            log_ph.code("\n".join(logs[-15:]))

        if arquivos is None and erro is None:
            if not thread.is_alive():
                erro = "A sessão encerrou inesperadamente."
                break
            time.sleep(0.3)

    progress_bar.progress(1.0)

    if erro:
        st.session_state.erro = erro
        st.session_state.step = "menu"
    else:
        st.session_state.arquivos_quitacao = arquivos
        st.session_state.step = "quitacao_done"
    st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Quitação — download
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "quitacao_done":
    banner_sessao_ativa()
    arquivos = st.session_state.arquivos_quitacao
    csvs  = [f for f in arquivos if f.endswith(".csv")]
    excel = next((f for f in arquivos if f.endswith(".xlsx")), None)

    if arquivos:
        if not st.session_state.get("quitacao_celebrou"):
            st.balloons()
            st.session_state["quitacao_celebrou"] = True

        st.success(f"✅ Concluído! {len(csvs)} arquivo(s) exportado(s).")
        st.caption(f"📁 Salvos também em: `{PASTA_DESTINO}`")
        st.subheader("Baixar arquivos")
        for arq in csvs:
            with open(arq, "rb") as f:
                st.download_button(
                    label=f"⬇️ {os.path.basename(arq)}",
                    data=f.read(),
                    file_name=os.path.basename(arq),
                    mime="text/csv",
                    key=f"dl_{os.path.basename(arq)}",
                )
        if excel and os.path.exists(excel):
            with open(excel, "rb") as f:
                st.download_button(
                    label=f"⬇️ {os.path.basename(excel)} (consolidado)",
                    data=f.read(),
                    file_name=os.path.basename(excel),
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key=f"dl_excel",
                )
    else:
        st.warning("Nenhum arquivo exportado.")

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("← Voltar ao menu", use_container_width=True, key="voltar_quit_done", type="primary"):
        st.session_state.arquivos_quitacao = []
        st.session_state.preview_cabecalho = []
        st.session_state.preview_linhas    = []
        st.session_state.referencia_atual  = None
        st.session_state.pop("quitacao_celebrou", None)
        st.session_state.step = "menu"
        st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Acompanhamento — filtros (datas)
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "acomp_filtros":
    banner_sessao_ativa()
    st.markdown("**Acompanhamento de Envios Digitais**")
    st.caption(f"Credenciado: {st.session_state.credenciado_atual}")

    with st.form("acomp_filtros_form"):
        col_ini, col_fim = st.columns(2)
        with col_ini:
            data_ini_dt = st.date_input("Data Início", format="DD/MM/YYYY", value=date.today())
        with col_fim:
            data_fim_dt = st.date_input("Data Fim", format="DD/MM/YYYY", value=date.today())
        buscar = st.form_submit_button("Buscar Atendimentos", use_container_width=True)

    if st.button("← Voltar ao menu", use_container_width=True, key="voltar_acomp_filtros", type="secondary"):
        st.session_state.step = "menu"
        st.rerun()

    if buscar:
        if data_ini_dt > data_fim_dt:
            st.error("A Data Início não pode ser depois da Data Fim. Verifique e tente de novo.")
        else:
            data_ini = data_ini_dt.strftime("%d/%m/%Y")
            data_fim = data_fim_dt.strftime("%d/%m/%Y")
            st.session_state.logs_acumulados = []
            esvaziar_log_queue()
            st.session_state.cmd_queue.put(
                ("RODAR_ACOMPANHAMENTO", st.session_state.credenciado_atual, data_ini, data_fim)
            )
            st.session_state.step = "acomp_buscando"
            st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Acompanhamento — buscando
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "acomp_buscando":
    banner_sessao_ativa()
    log_queue  = st.session_state.log_queue
    thread     = st.session_state.browser_thread
    ESTIMATIVA = 30

    st.markdown("**Buscando atendimentos...**")
    progress_bar = st.progress(0.0)
    col_t, col_m = st.columns([1, 3])
    timer_ph     = col_t.empty()
    msg_ph       = col_m.empty()
    log_ph       = st.empty()
    botao_cancelar_operacao(key="cancel_acomp")

    start_time = time.time()
    eventos    = []
    logs       = st.session_state.logs_acumulados
    resultado, erro = None, None

    while resultado is None and erro is None:
        elapsed = time.time() - start_time
        progress_bar.progress(min(elapsed / ESTIMATIVA, 0.95))
        timer_ph.metric("⏱", f"{int(elapsed)}s")
        if elapsed < ESTIMATIVA:
            msg_ph.caption(f"Tempo estimado: ~{ESTIMATIVA}s")
        else:
            msg_ph.caption("⚠️ Tá demorando mais que o normal...")

        drenar_log_queue(log_queue, eventos, logs)
        for ev in eventos:
            if ev[0] == "ACOMPANHAMENTO_OK":
                resultado = ev
            elif ev[0] in ("ERRO_OPERACAO", "ERRO_LOGIN"):
                erro = ev[1]
        eventos.clear()

        if logs:
            log_ph.info(logs[-1])

        if resultado is None and erro is None:
            if not thread.is_alive():
                erro = "A sessão encerrou inesperadamente."
                break
            time.sleep(0.3)

    progress_bar.progress(1.0)

    if erro:
        st.session_state.erro = erro
        st.session_state.step = "menu"
    else:
        _, nome_arq, total, dados = resultado
        st.session_state.acomp_arquivo = nome_arq
        st.session_state.acomp_total   = total
        st.session_state.acomp_dados   = dados
        st.session_state.step          = "acomp_done"
    st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Acompanhamento — download
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "acomp_done":
    banner_sessao_ativa()
    nome_arq = st.session_state.acomp_arquivo
    total    = st.session_state.acomp_total
    dados    = st.session_state.acomp_dados or []

    if not st.session_state.get("acomp_celebrou"):
        st.balloons()
        st.session_state["acomp_celebrou"] = True

    st.success(f"✅ Concluído! {total} linha(s) encontrada(s).")

    # Tabela de preview na tela
    if dados and len(dados) > 1:
        cabecalho = dados[0]
        linhas    = dados[1:]
        try:
            df = pd.DataFrame(linhas, columns=cabecalho)
        except Exception:
            df = pd.DataFrame(linhas)
        st.dataframe(df, use_container_width=True, hide_index=True, height=400)

    st.caption(f"📁 Salvo também em: `{PASTA_DESTINO}`")
    if nome_arq and os.path.exists(nome_arq):
        with open(nome_arq, "rb") as f:
            st.download_button(
                label=f"⬇️ {os.path.basename(nome_arq)}",
                data=f.read(),
                file_name=os.path.basename(nome_arq),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="dl_acomp",
            )

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("← Voltar ao menu", use_container_width=True, key="voltar_acomp_done", type="primary"):
        st.session_state.acomp_arquivo = None
        st.session_state.acomp_total   = 0
        st.session_state.acomp_dados   = []
        st.session_state.pop("acomp_celebrou", None)
        st.session_state.step          = "menu"
        st.rerun()


# ──────────────────────────────────────────────────────────────────
# Rodapé
# ──────────────────────────────────────────────────────────────────
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
