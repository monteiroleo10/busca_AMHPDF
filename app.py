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
import io
import json
import platform
import tempfile
import time
import re
import urllib.parse
import base64
import pandas as pd
import openpyxl
from datetime import date, datetime
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


def exportar_csv(page, texto_referencia, usuario, pasta_destino=None):
    selecionar_referencia(page, texto_referencia)
    page.locator("#ctl00_MainContent_rbtExportarCsv_input").click()

    destino = pasta_destino or PASTA_DESTINO
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
        os.makedirs(destino, exist_ok=True)
        prefixo = ''.join(c for c in usuario if c.isdigit())[:6]
        nome = (prefixo + "_Extrato_"
                + texto_referencia
                  .replace("ª", "a").replace("ç", "c").replace("ã", "a")
                  .replace("é", "e").replace("ê", "e").replace("á", "a")
                  .replace("â", "a").replace("ó", "o").replace("ô", "o")
                  .replace("ú", "u").replace("/", "_").replace(" ", "_")
                + ".csv")
        caminho = os.path.join(destino, nome)
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
    # Pausa defensiva para a tabela popular após a busca. A AMHP renderiza
    # o cabecalho da tabela antes dos dados chegarem, entao um wait_for_selector
    # por <tr> retornaria cedo demais e a leitura viria vazia.
    page.wait_for_timeout(2000)

    log_queue.put("Extraindo dados da tabela...")
    # Extracao em UMA chamada ao navegador (em vez de uma por celula).
    # Acelera dramaticamente quando a tabela tem muitas linhas.
    dados = page.evaluate("""() => {
        const rows = Array.from(document.querySelectorAll(
            '#ctl00_MainContent_rdgAcompanhamentoDigital table.rgMasterTable tr'
        ));
        return rows.map(row =>
            Array.from(row.querySelectorAll('th, td'))
                 .map(c => (c.innerText || '').trim())
        ).filter(row => row.some(cell => cell.length > 0));
    }""")
    return dados or []


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


# ─── ANÁLISE: ENVIOS vs QUITAÇÕES ─────────────────────────────────

def _encontrar_coluna(df, candidatos):
    """Procura coluna por substring (case-insensitive), respeitando ordem de
    prioridade dos candidatos: tenta o 1º candidato em todas as colunas, depois
    o 2º, etc. Retorna nome da coluna ou None."""
    cols_norm = [(c, str(c).strip().lower()) for c in df.columns]
    for cand in candidatos:
        cand_norm = cand.lower()
        for col, col_norm in cols_norm:
            if cand_norm in col_norm:
                return col
    return None


def _para_numero(valor):
    """Converte string monetária brasileira em float; retorna 0.0 se inválido."""
    if pd.isna(valor) or valor in (None, ""):
        return 0.0
    s = str(valor).strip().replace("R$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def envios_para_df(envios_raw):
    """Converte a lista de listas vinda da página de Acompanhamento em DataFrame."""
    if not envios_raw or len(envios_raw) < 2:
        return pd.DataFrame()
    header = envios_raw[0]
    return pd.DataFrame(envios_raw[1:], columns=header)


def quitacoes_para_df(csv_paths):
    """Lê e concatena os CSVs de quitação em um único DataFrame."""
    frames = []
    for arq in csv_paths:
        try:
            df = pd.read_csv(arq, sep=";", encoding="latin1", dtype=str)
            df["__Referencia"] = os.path.basename(arq).replace(".csv", "")
            frames.append(df)
        except Exception:
            pass
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def cruzar_envios_quitacoes(envios_df, quitacoes_df):
    """
    Cruza por número da guia. Retorna (analise_df, diag) onde:
      analise_df tem Numero_Guia, Status, Valor_Guia_Enviado, Total_Repasse,
                 Diferenca, Total_Glosa, Qtd_Procedimentos, Detalhe_Glosas
      diag       tem nomes das colunas detectadas e contadores de duplicatas
    """
    col_guia_env  = _encontrar_coluna(envios_df,    ["nº da guia", "nº guia", "n guia", "num guia", "numero da guia", "guia"])
    col_valor_env = _encontrar_coluna(envios_df,    ["valor da guia", "vlr guia", "vl guia", "valor total", "vlr total"])
    col_guia_qit  = _encontrar_coluna(quitacoes_df, ["nº da guia", "nº guia", "n guia", "num guia", "numero da guia", "guia"])
    col_repasse   = _encontrar_coluna(quitacoes_df, ["valor do repasse", "vlr repasse", "vl repasse", "repasse ao", "repasse"])
    col_glosa     = _encontrar_coluna(quitacoes_df, ["valor da glosa", "vlr glosa", "vl glosa", "valor glosa", "glosa"])
    col_codigo    = _encontrar_coluna(quitacoes_df, ["código do serviço", "codigo do servico", "código", "codigo", "procedimento"])
    col_desc      = _encontrar_coluna(quitacoes_df, ["descrição do serviço", "descricao do servico", "descrição", "descricao"])

    if not col_guia_env:
        raise ValueError("Não consegui identificar a coluna de número da guia nos envios.")
    if not col_guia_qit:
        raise ValueError("Não consegui identificar a coluna de número da guia nas quitações.")

    envios_df    = envios_df.copy()
    quitacoes_df = quitacoes_df.copy()
    envios_df["__guia"]    = envios_df[col_guia_env].astype(str).str.strip()
    quitacoes_df["__guia"] = quitacoes_df[col_guia_qit].astype(str).str.strip()

    envios_df["__valor_guia"] = envios_df[col_valor_env].apply(_para_numero) if col_valor_env else 0.0
    quitacoes_df["__repasse"] = quitacoes_df[col_repasse].apply(_para_numero) if col_repasse else 0.0
    quitacoes_df["__glosa"]   = quitacoes_df[col_glosa].apply(_para_numero)   if col_glosa   else 0.0

    qtd_envios_total = len(envios_df)
    qtd_guias_unicas = envios_df["__guia"].nunique()
    duplicadas       = qtd_envios_total - qtd_guias_unicas

    agg_quit = quitacoes_df.groupby("__guia").agg(
        Total_Repasse=("__repasse", "sum"),
        Total_Glosa=("__glosa", "sum"),
        Qtd_Procedimentos=("__guia", "size"),
    ).reset_index()

    # Detalhe das glosas: só procedimentos com glosa > 0
    detalhes_glosas = {}
    com_glosa = quitacoes_df[quitacoes_df["__glosa"] > 0.001]
    for guia, grupo in com_glosa.groupby("__guia"):
        partes = []
        for _, r in grupo.iterrows():
            codigo   = str(r[col_codigo]).strip() if col_codigo else ""
            desc     = str(r[col_desc]).strip()   if col_desc   else ""
            etiqueta = " ".join(p for p in [codigo, desc] if p)
            valor    = r["__glosa"]
            valor_fmt = f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
            partes.append(f"{etiqueta}: {valor_fmt}" if etiqueta else valor_fmt)
        detalhes_glosas[guia] = " | ".join(partes)

    # Agrupa envios por guia somando o valor (em vez de drop_duplicates).
    # Assim, se a mesma guia aparece em mais de um envio, soma os valores;
    # a soma da aba Envios bate com a soma da Análise.
    envios_agg = envios_df.groupby("__guia").agg(
        Valor_Guia_Enviado=("__valor_guia", "sum"),
    ).reset_index().rename(columns={"__guia": "Numero_Guia"})

    resultado = envios_agg.merge(
        agg_quit.rename(columns={"__guia": "Numero_Guia"}),
        on="Numero_Guia", how="left",
    )

    resultado["Total_Repasse"]     = resultado["Total_Repasse"].fillna(0.0)
    resultado["Total_Glosa"]       = resultado["Total_Glosa"].fillna(0.0)
    resultado["Qtd_Procedimentos"] = resultado["Qtd_Procedimentos"].fillna(0).astype(int)
    resultado["Diferenca"]         = resultado["Valor_Guia_Enviado"] - resultado["Total_Repasse"]
    resultado["Detalhe_Glosas"]    = resultado["Numero_Guia"].map(detalhes_glosas).fillna("")

    def _classificar(row):
        if row["Qtd_Procedimentos"] == 0:
            return "Pendente"
        if abs(row["Diferenca"]) <= 0.01:
            return "Quitada integralmente"
        return "Quitada parcial (glosa)"

    resultado["Status"] = resultado.apply(_classificar, axis=1)

    resultado = resultado[[
        "Numero_Guia", "Status", "Valor_Guia_Enviado", "Total_Repasse",
        "Diferenca", "Total_Glosa", "Qtd_Procedimentos", "Detalhe_Glosas",
    ]]

    diag = {
        "col_guia_envio":     col_guia_env,
        "col_valor_envio":    col_valor_env,
        "col_guia_quitacao":  col_guia_qit,
        "col_repasse":        col_repasse,
        "col_glosa":          col_glosa,
        "col_codigo":         col_codigo,
        "col_descricao":      col_desc,
        "qtd_envios_total":   qtd_envios_total,
        "qtd_guias_unicas":   qtd_guias_unicas,
        "envios_duplicados":  duplicadas,
    }
    return resultado, diag


def tabela_glosas(quitacoes_df, diag):
    """Retorna DataFrame com 1 linha por procedimento glosado (Valor Glosa > 0)."""
    col_guia   = diag.get("col_guia_quitacao")
    col_glosa  = diag.get("col_glosa")
    col_codigo = diag.get("col_codigo")
    col_desc   = diag.get("col_descricao")

    if not col_glosa:
        return pd.DataFrame(columns=["Numero_Guia", "Codigo", "Descricao", "Valor_Glosa"])

    df = quitacoes_df.copy()
    df["__glosa_num"] = df[col_glosa].apply(_para_numero)
    df = df[df["__glosa_num"] > 0.001]
    if df.empty:
        return pd.DataFrame(columns=["Numero_Guia", "Codigo", "Descricao", "Valor_Glosa"])

    out = pd.DataFrame({
        "Numero_Guia": df[col_guia].astype(str).str.strip() if col_guia else "",
        "Codigo":      df[col_codigo].astype(str).str.strip() if col_codigo else "",
        "Descricao":   df[col_desc].astype(str).str.strip()   if col_desc   else "",
        "Valor_Glosa": df["__glosa_num"],
    }).reset_index(drop=True)
    return out


def gerar_xlsx_analise(envios_df, quitacoes_df, analise_df, meta, diag=None, glosas_df=None):
    """Gera o XLSX da análise em memória (bytes) com até 5 abas: Resumo,
    Análise, Glosas, Envios, Quitações. Converte as colunas numéricas-chave
    pra float nas abas Envios/Quitações pra permitir SOMA() no Excel."""
    diag = diag or {}
    envios_export    = envios_df.copy()
    quitacoes_export = quitacoes_df.copy()

    # Converte colunas numéricas pra float (assim somam direito no Excel)
    cv = diag.get("col_valor_envio")
    if cv and cv in envios_export.columns:
        envios_export[cv] = envios_export[cv].apply(_para_numero)
    for c in [diag.get("col_repasse"), diag.get("col_glosa")]:
        if c and c in quitacoes_export.columns:
            quitacoes_export[c] = quitacoes_export[c].apply(_para_numero)

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        resumo = pd.DataFrame([
            ["Credenciado",              meta.get("credenciado", "")],
            ["Período de envio",         f"{meta.get('data_ini', '')} a {meta.get('data_fim', '')}"],
            ["Referências de quitação",  ", ".join(meta.get("refs_quitacao", []))],
            ["Gerado em",                meta.get("gerado_em", "")],
            ["", ""],
            ["Total de guias enviadas",      len(analise_df)],
            ["Quitadas integralmente",       int((analise_df["Status"] == "Quitada integralmente").sum())],
            ["Quitadas parcial (glosa)",     int((analise_df["Status"] == "Quitada parcial (glosa)").sum())],
            ["Pendentes",                    int((analise_df["Status"] == "Pendente").sum())],
            ["Valor total enviado (R$)",     round(float(analise_df["Valor_Guia_Enviado"].sum()), 2)],
            ["Valor total recebido (R$)",    round(float(analise_df["Total_Repasse"].sum()), 2)],
            ["Total glosado (R$)",           round(float(analise_df["Total_Glosa"].sum()), 2)],
            ["Diferença total (R$)",         round(float(analise_df["Diferenca"].sum()), 2)],
            ["", ""],
            ["— Diagnóstico —", ""],
            ["Coluna de Valor da Guia (envios)",     diag.get("col_valor_envio") or "(não detectada)"],
            ["Coluna de Valor do Repasse (quit.)",   diag.get("col_repasse")     or "(não detectada)"],
            ["Coluna de Valor da Glosa (quit.)",     diag.get("col_glosa")       or "(não detectada)"],
            ["Linhas no relatório de envios",        diag.get("qtd_envios_total", 0)],
            ["Guias únicas após agregação",          diag.get("qtd_guias_unicas", 0)],
            ["Envios duplicados (mesma guia repete)", diag.get("envios_duplicados", 0)],
        ], columns=["Campo", "Valor"])
        resumo.to_excel(writer,        sheet_name="Resumo",    index=False)
        analise_df.to_excel(writer,    sheet_name="Análise",   index=False)
        if glosas_df is not None and not glosas_df.empty:
            glosas_df.to_excel(writer, sheet_name="Glosas",    index=False)
        envios_export.to_excel(writer,    sheet_name="Envios",    index=False)
        quitacoes_export.to_excel(writer, sheet_name="Quitações", index=False)
    return buf.getvalue()


def gerar_json_analise(envios_df, quitacoes_df, analise_df, meta):
    """Gera JSON cru (bytes) com meta + dados crus + análise — útil pra migração futura."""
    obj = {
        "meta":      meta,
        "envios":    envios_df.to_dict(orient="records"),
        "quitacoes": quitacoes_df.to_dict(orient="records"),
        "analise":   analise_df.to_dict(orient="records"),
    }
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def limpar_pasta_tmp(pasta):
    """Remove arquivos e a pasta temporária da análise. Tolera erros."""
    if not pasta or not os.path.isdir(pasta):
        return
    try:
        for arq in glob.glob(os.path.join(pasta, "*")):
            try: os.remove(arq)
            except Exception: pass
        os.rmdir(pasta)
    except Exception:
        pass


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
                            log_queue.put(("ACOMPANHAMENTO_OK", nome_arq, len(dados) - 1))

                    elif acao == "RODAR_ANALISE":
                        _, credenciado, data_ini, data_fim, refs_quitacao = cmd

                        log_queue.put("Etapa 1/2: buscando envios digitais...")
                        envios_raw = buscar_acompanhamento(page, data_ini, data_fim, credenciado, log_queue)
                        if not envios_raw or len(envios_raw) <= 1:
                            log_queue.put(("ERRO_OPERACAO",
                                "Nenhum envio encontrado para esse credenciado e período. "
                                "Verifique as datas e tente novamente."))
                        else:
                            log_queue.put(f"Envios encontrados: {len(envios_raw) - 1} linha(s).")
                            log_queue.put("Etapa 2/2: baixando quitações...")
                            navegar_para_extrato(page, credenciado, log_queue)

                            pasta_tmp = tempfile.mkdtemp(prefix="amhp_analise_")
                            csv_paths = []
                            total = len(refs_quitacao)
                            for i, ref in enumerate(refs_quitacao):
                                log_queue.put(f"  [{i+1}/{total}] Baixando {ref}...")
                                try:
                                    caminho = exportar_csv(page, ref, usuario, pasta_destino=pasta_tmp)
                                    if caminho:
                                        csv_paths.append(caminho)
                                    time.sleep(0.5)
                                except Exception as e:
                                    log_queue.put(f"  Erro em {ref}: {e}")

                            log_queue.put(("ANALISE_OK", envios_raw, csv_paths, pasta_tmp))

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
              "credenciados", "credenciado_atual", "referencias", "refs_multi",
              "selecionadas", "arquivos_quitacao",
              "acomp_arquivo", "acomp_total",
              "logs_acumulados", "fluxo_atual",
              "quitacao_celebrou", "acomp_celebrou"]:
        st.session_state.pop(k, None)
    st.session_state.step = "input"


# ─── INTERFACE STREAMLIT ──────────────────────────────────────────

st.set_page_config(page_title="Exportar Extrato AMHP", page_icon="📊", layout="centered")

# Tentativa de forçar locale PT-BR no calendário do date_input.
# O Streamlit não oferece API nativa pra isso; injetamos JS no documento pai
# pra que widgets que respeitam o atributo `lang` mostrem meses em portugues.
components.html(
    """
    <script>
    try {
        const doc = window.parent && window.parent.document;
        if (doc && doc.documentElement) {
            doc.documentElement.lang = 'pt-BR';
            doc.documentElement.setAttribute('lang', 'pt-BR');
        }
    } catch (e) { /* silencioso */ }
    </script>
    """,
    height=0,
)

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
    "selecionadas": [],
    "arquivos_quitacao": [],
    "acomp_arquivo": None,
    "acomp_total": 0,
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

    if st.button("🔍 Análise: Envios vs Quitações", use_container_width=True, key="btn_analise", type="primary"):
        st.session_state.fluxo_atual = "analise"
        st.session_state.step = "analise_listando"
        st.session_state.logs_acumulados = []
        esvaziar_log_queue()
        st.session_state.cmd_queue.put(("LISTAR_REFS_QUITACAO", st.session_state.credenciado_atual))
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

    # Inicializa a seleção uma única vez (primeira referência)
    if "refs_multi" not in st.session_state:
        st.session_state["refs_multi"] = (
            st.session_state.referencias[:1] if st.session_state.referencias else []
        )

    # Filtro + botões marcar/desmarcar
    col_filtro, col_m, col_d = st.columns([3, 1, 1])
    with col_filtro:
        filtro = st.text_input(
            "Filtrar",
            placeholder="Ex: 2025, Out, ...",
            label_visibility="collapsed",
            key="filtro_refs",
        )
    filtro_lc = (filtro or "").lower()
    refs_filtradas = [r for r in st.session_state.referencias if filtro_lc in r.lower()]

    with col_m:
        if st.button("✓ Todas", use_container_width=True, key="marcar_todas", type="secondary"):
            fora_filtro = [r for r in st.session_state["refs_multi"] if r not in refs_filtradas]
            st.session_state["refs_multi"] = fora_filtro + refs_filtradas
            st.rerun()
    with col_d:
        if st.button("✗ Nenhuma", use_container_width=True, key="desmarcar_todas", type="secondary"):
            st.session_state["refs_multi"] = [
                r for r in st.session_state["refs_multi"] if r not in refs_filtradas
            ]
            st.rerun()

    if filtro_lc:
        st.caption(f"Mostrando {len(refs_filtradas)} de {len(st.session_state.referencias)}")

    selecionadas_visiveis = st.multiselect(
        "Selecione as referências para exportar:",
        options=refs_filtradas,
        default=[r for r in st.session_state["refs_multi"] if r in refs_filtradas],
        key="multi_widget",
    )
    invisiveis = [r for r in st.session_state["refs_multi"] if r not in refs_filtradas]
    st.session_state["refs_multi"] = invisiveis + selecionadas_visiveis

    total_marcadas = len(st.session_state["refs_multi"])
    if total_marcadas:
        st.caption(f"📌 {total_marcadas} referência(s) marcada(s) no total")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("← Voltar ao menu", use_container_width=True,
                     key="voltar_quit_sel", type="secondary"):
            st.session_state.pop("refs_multi", None)
            st.session_state.step = "menu"
            st.rerun()
    with col2:
        if st.button("Exportar selecionados →", use_container_width=True,
                     key="exp_quit", type="primary"):
            if not st.session_state["refs_multi"]:
                st.error("Selecione ao menos uma referência.")
            else:
                st.session_state.selecionadas = list(st.session_state["refs_multi"])
                st.session_state.logs_acumulados = []
                esvaziar_log_queue()
                st.session_state.cmd_queue.put(
                    ("RODAR_QUITACAO", st.session_state.credenciado_atual,
                     st.session_state.selecionadas)
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
        st.session_state.pop("quitacao_celebrou", None)
        st.session_state.pop("refs_multi", None)
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
        _, nome_arq, total = resultado
        st.session_state.acomp_arquivo = nome_arq
        st.session_state.acomp_total   = total
        st.session_state.step          = "acomp_done"
    st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Acompanhamento — download
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "acomp_done":
    banner_sessao_ativa()
    nome_arq = st.session_state.acomp_arquivo
    total    = st.session_state.acomp_total

    if not st.session_state.get("acomp_celebrou"):
        st.balloons()
        st.session_state["acomp_celebrou"] = True

    st.success(f"✅ Concluído! {total} linha(s) encontrada(s).")
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
        st.session_state.pop("acomp_celebrou", None)
        st.session_state.step          = "menu"
        st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Análise — buscando referências
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "analise_listando":
    banner_sessao_ativa()
    log_queue = st.session_state.log_queue
    thread    = st.session_state.browser_thread
    ESTIMATIVA = 20

    st.markdown("**Análise: Envios vs Quitações**")
    st.caption("Buscando referências de quitação disponíveis...")
    progress_bar = st.progress(0.0)
    col_t, col_m = st.columns([1, 3])
    timer_ph     = col_t.empty()
    msg_ph       = col_m.empty()
    status_ph    = st.empty()
    botao_cancelar_operacao(key="cancel_analise_list")

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
            elif ev[0] in ("ERRO_OPERACAO", "ERRO_LOGIN"):
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
    else:
        st.session_state.analise_refs = refs
        st.session_state.step = "analise_filtros"
    st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Análise — filtros
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "analise_filtros":
    banner_sessao_ativa()
    st.markdown("**Análise: Envios vs Quitações**")
    st.caption(f"Credenciado: {st.session_state.credenciado_atual}")
    st.info(
        "Escolha o período dos envios digitais que você quer analisar e quais "
        "referências de quitação cruzar. O resultado mostra, guia por guia, "
        "o que já foi quitado, o que veio com glosa e o que ainda está pendente."
    )

    refs_disponiveis = st.session_state.get("analise_refs", [])
    default_refs = refs_disponiveis[:2] if len(refs_disponiveis) >= 2 else refs_disponiveis

    with st.form("analise_filtros_form"):
        col_ini, col_fim = st.columns(2)
        with col_ini:
            data_ini_dt = st.date_input("Data Início (envio)", format="DD/MM/YYYY", value=date.today())
        with col_fim:
            data_fim_dt = st.date_input("Data Fim (envio)",    format="DD/MM/YYYY", value=date.today())

        refs_escolhidas = st.multiselect(
            "Referências de quitação a cruzar",
            options=refs_disponiveis,
            default=default_refs,
            help="Inclua as referências que cobrem o período que você espera ter recebido. "
                 "Recomendado: o mês do envio e os 1–2 seguintes.",
        )

        rodar = st.form_submit_button("Rodar análise", use_container_width=True, type="primary")

    if st.button("← Voltar ao menu", use_container_width=True, key="voltar_analise_filtros", type="secondary"):
        st.session_state.step = "menu"
        st.rerun()

    if rodar:
        if data_ini_dt > data_fim_dt:
            st.error("A Data Início não pode ser depois da Data Fim.")
        elif not refs_escolhidas:
            st.error("Selecione pelo menos uma referência de quitação.")
        else:
            data_ini = data_ini_dt.strftime("%d/%m/%Y")
            data_fim = data_fim_dt.strftime("%d/%m/%Y")
            st.session_state.analise_meta = {
                "credenciado":    st.session_state.credenciado_atual,
                "data_ini":       data_ini,
                "data_fim":       data_fim,
                "refs_quitacao":  refs_escolhidas,
                "gerado_em":      datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
            }
            st.session_state.logs_acumulados = []
            esvaziar_log_queue()
            st.session_state.cmd_queue.put((
                "RODAR_ANALISE",
                st.session_state.credenciado_atual,
                data_ini, data_fim,
                refs_escolhidas,
            ))
            st.session_state.step = "analise_processando"
            st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Análise — processando
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "analise_processando":
    banner_sessao_ativa()
    log_queue = st.session_state.log_queue
    thread    = st.session_state.browser_thread
    meta      = st.session_state.get("analise_meta", {})
    qtd_refs  = len(meta.get("refs_quitacao", []))
    ESTIMATIVA = 30 + 60 * qtd_refs  # envio + ~60s por referência

    st.markdown("**Rodando análise...**")
    st.caption(f"Período: {meta.get('data_ini')} a {meta.get('data_fim')} · "
               f"{qtd_refs} referência(s) de quitação")
    progress_bar = st.progress(0.0)
    col_t, col_m = st.columns([1, 3])
    timer_ph     = col_t.empty()
    msg_ph       = col_m.empty()
    log_ph       = st.empty()
    botao_cancelar_operacao(key="cancel_analise_proc")

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
            if ev[0] == "ANALISE_OK":
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
        st.rerun()
    else:
        _, envios_raw, csv_paths, pasta_tmp = resultado
        try:
            envios_df          = envios_para_df(envios_raw)
            quitacoes_df       = quitacoes_para_df(csv_paths)
            analise_df, diag   = cruzar_envios_quitacoes(envios_df, quitacoes_df)
            glosas_df          = tabela_glosas(quitacoes_df, diag)

            st.session_state.analise_envios_df    = envios_df
            st.session_state.analise_quitacoes_df = quitacoes_df
            st.session_state.analise_resultado_df = analise_df
            st.session_state.analise_diag         = diag
            st.session_state.analise_glosas_df    = glosas_df
            st.session_state.analise_pasta_tmp    = pasta_tmp
            st.session_state.step = "analise_done"
        except Exception as e:
            limpar_pasta_tmp(pasta_tmp)
            st.session_state.erro = f"Erro ao cruzar os dados: {e}"
            st.session_state.step = "menu"
        st.rerun()


# ──────────────────────────────────────────────────────────────────
# TELA: Análise — resultado + downloads
# ──────────────────────────────────────────────────────────────────
elif st.session_state.step == "analise_done":
    banner_sessao_ativa()
    analise_df   = st.session_state.analise_resultado_df
    envios_df    = st.session_state.analise_envios_df
    quitacoes_df = st.session_state.analise_quitacoes_df
    glosas_df    = st.session_state.get("analise_glosas_df")
    diag         = st.session_state.get("analise_diag", {})
    meta         = st.session_state.analise_meta

    if not st.session_state.get("analise_celebrou"):
        st.balloons()
        st.session_state["analise_celebrou"] = True

    total      = len(analise_df)
    quitadas   = int((analise_df["Status"] == "Quitada integralmente").sum())
    parciais   = int((analise_df["Status"] == "Quitada parcial (glosa)").sum())
    pendentes  = int((analise_df["Status"] == "Pendente").sum())
    soma_env   = float(analise_df["Valor_Guia_Enviado"].sum())
    soma_rec   = float(analise_df["Total_Repasse"].sum())
    soma_dif   = float(analise_df["Diferenca"].sum())

    st.success(f"✅ Análise concluída! {total} guia(s) processada(s).")

    if diag.get("envios_duplicados"):
        st.warning(
            f"⚠️ {diag['envios_duplicados']} envio(s) tinham número de guia "
            "repetido — os valores dessas guias foram somados na análise."
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total",      total)
    c2.metric("Quitadas",   quitadas)
    c3.metric("Parciais",   parciais)
    c4.metric("Pendentes",  pendentes)

    c5, c6, c7 = st.columns(3)
    c5.metric("Enviado (R$)",   f"{soma_env:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    c6.metric("Recebido (R$)",  f"{soma_rec:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))
    c7.metric("Diferença (R$)", f"{soma_dif:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."))

    with st.expander("🔎 Diagnóstico (colunas detectadas)"):
        st.write({
            "Valor da Guia (envios)":       diag.get("col_valor_envio")    or "❌ não detectada",
            "Nº da Guia (envios)":          diag.get("col_guia_envio")     or "❌ não detectada",
            "Nº da Guia (quitação)":        diag.get("col_guia_quitacao")  or "❌ não detectada",
            "Valor do Repasse (quitação)":  diag.get("col_repasse")        or "❌ não detectada",
            "Valor da Glosa (quitação)":    diag.get("col_glosa")          or "❌ não detectada",
            "Código do Serviço (quitação)": diag.get("col_codigo")         or "❌ não detectada",
            "Descrição do Serviço (quit.)": diag.get("col_descricao")      or "❌ não detectada",
        })

    st.markdown("---")
    st.markdown("**Pré-visualização do resultado:**")
    st.dataframe(analise_df, use_container_width=True, height=320)

    if glosas_df is not None and not glosas_df.empty:
        st.markdown(f"**Glosas detalhadas ({len(glosas_df)} procedimento(s) glosado(s)):**")
        st.dataframe(glosas_df, use_container_width=True, height=240)

    st.markdown("---")
    st.markdown("**Baixar resultados:**")
    xlsx_bytes = gerar_xlsx_analise(envios_df, quitacoes_df, analise_df, meta, diag, glosas_df)
    json_bytes = gerar_json_analise(envios_df, quitacoes_df, analise_df, meta)
    cred_slug  = (meta.get("credenciado", "")[:6] or "cred").strip().replace(" ", "_")
    stamp      = datetime.now().strftime("%Y%m%d_%H%M")
    nome_base  = f"Analise_{cred_slug}_{stamp}"

    col_x, col_j = st.columns(2)
    with col_x:
        st.download_button(
            label="⬇️ Baixar XLSX",
            data=xlsx_bytes,
            file_name=f"{nome_base}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
            key="dl_analise_xlsx",
        )
    with col_j:
        st.download_button(
            label="⬇️ Baixar JSON (dados crus)",
            data=json_bytes,
            file_name=f"{nome_base}.json",
            mime="application/json",
            use_container_width=True,
            key="dl_analise_json",
        )

    st.markdown("<br>", unsafe_allow_html=True)
    if st.button("← Voltar ao menu", use_container_width=True, key="voltar_analise_done", type="primary"):
        limpar_pasta_tmp(st.session_state.get("analise_pasta_tmp"))
        for chave in [
            "analise_envios_df", "analise_quitacoes_df", "analise_resultado_df",
            "analise_glosas_df", "analise_diag",
            "analise_meta", "analise_pasta_tmp", "analise_refs", "analise_celebrou",
        ]:
            st.session_state.pop(chave, None)
        st.session_state.step = "menu"
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
