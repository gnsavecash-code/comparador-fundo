from flask import Flask, render_template, jsonify, request
import pandas as pd
import requests
import zipfile
import io
import os
from datetime import datetime, timedelta

app = Flask(__name__)

CACHE_DIR = "cache_cvm"
os.makedirs(CACHE_DIR, exist_ok=True)

cad_fundos_cache = pd.DataFrame()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def obter_ultimos_meses(qtd_meses=12):
    meses = []
    hoje = datetime.now()
    for i in range(qtd_meses):
        ano = hoje.year
        mes = hoje.month - i
        while mes <= 0:
            mes += 12
            ano -= 1
        meses.append(f"{ano}{mes:02d}")
    return meses

def carregar_cadastro():
    global cad_fundos_cache
    if not cad_fundos_cache.empty:
        return cad_fundos_cache

    arq_cad_csv = os.path.join(CACHE_DIR, "cad_fi.csv")
    url_cad = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/cad_fi.csv"

    if not os.path.exists(arq_cad_csv):
        try:
            resp_cad = requests.get(url_cad, headers=HEADERS, timeout=30)
            if resp_cad.status_code == 200:
                with open(arq_cad_csv, "wb") as f:
                    f.write(resp_cad.content)
        except Exception as e:
            print("Erro ao baixar cadastro CVM:", e)

    if os.path.exists(arq_cad_csv):
        try:
            df = pd.read_csv(arq_cad_csv, sep=";", encoding="ISO-8859-1", low_memory=False)
            df.columns = df.columns.str.replace('"', '').str.strip().str.upper()

            c_cnpj = next((c for c in df.columns if 'CNPJ' in c), None)
            c_nome = next((c for c in df.columns if 'DENOM' in c or 'NOME' in c), None)
            c_sit = next((c for c in df.columns if 'SIT' in c), None)

            if c_cnpj:
                df['CNPJ_FUNDO'] = df[c_cnpj].astype(str).str.replace(r'\D', '', regex=True).str.zfill(14)
                df['NOME_FUNDO'] = df[c_nome].astype(str).str.strip() if c_nome else df['CNPJ_FUNDO']

                if c_sit:
                    df = df[df[c_sit].astype(str).str.strip().str.upper() == 'EM FUNCIONAMENTO NORMAL']

                cols_desejadas = ['CNPJ_FUNDO', 'NOME_FUNDO']
                for col in ['CLASSE', 'ADMINISTRADOR', 'ADMIN']:
                    if col in df.columns:
                        cols_desejadas.append(col)

                cad_fundos_cache = df[cols_desejadas].copy()
        except Exception as e:
            print("Erro ao carregar cad_fi.csv:", e)

    return cad_fundos_cache

def buscar_cotas_cnpjs(cnpjs, qtd_meses=12):
    """Baixa e processa em chunks apenas os CNPJs solicitados para economizar RAM"""
    meses = obter_ultimos_meses(qtd_meses)
    dfs = []
    cnpjs_set = set(cnpjs)

    for ano_mes in meses:
        url_inf = f"https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{ano_mes}.zip"
        arq_zip = os.path.join(CACHE_DIR, f"inf_{ano_mes}.zip")

        try:
            if not os.path.exists(arq_zip):
                resp = requests.get(url_inf, headers=HEADERS, timeout=30)
                if resp.status_code == 200:
                    with open(arq_zip, "wb") as f:
                        f.write(resp.content)

            if os.path.exists(arq_zip):
                with zipfile.ZipFile(arq_zip) as z:
                    csv_name = z.namelist()[0]
                    with z.open(csv_name) as f:
                        for chunk in pd.read_csv(f, sep=";", encoding="ISO-8859-1", chunksize=100000, low_memory=False):
                            chunk.columns = chunk.columns.str.replace('"', '').str.strip().str.upper()
                            c_cnpj = next((c for c in chunk.columns if 'CNPJ' in c), None)
                            c_quota = next((c for c in chunk.columns if 'QUOTA' in c or 'COTA' in c), None)
                            c_data = next((c for c in chunk.columns if 'DATA' in c or 'COMPTC' in c), None)

                            if c_cnpj and c_quota and c_data:
                                chunk['CNPJ_FUNDO'] = chunk[c_cnpj].astype(str).str.replace(r'\D', '', regex=True).str.zfill(14)
                                filtrado = chunk[chunk['CNPJ_FUNDO'].isin(cnpjs_set)].copy()

                                if not filtrado.empty:
                                    filtrado = filtrado.rename(columns={c_quota: 'VL_QUOTA', c_data: 'DT_COMPTC'})
                                    filtrado['DT_COMPTC'] = pd.to_datetime(filtrado['DT_COMPTC'], errors='coerce')
                                    filtrado['VL_QUOTA'] = filtrado['VL_QUOTA'].astype(str).str.replace(',', '.').astype(float)
                                    dfs.append(filtrado[['CNPJ_FUNDO', 'DT_COMPTC', 'VL_QUOTA']])
        except Exception as e:
            print(f"Aviso: Erro ao ler mês {ano_mes}: {e}")

    if dfs:
        df_final = pd.concat(dfs, ignore_index=True)
        return df_final.sort_values('DT_COMPTC').drop_duplicates(subset=['CNPJ_FUNDO', 'DT_COMPTC'])

    return pd.DataFrame()

def buscar_serie_bc(serie_id, data_inicio, data_fim):
    try:
        dt_i_str = data_inicio.strftime('%d/%m/%Y')
        dt_f_str = data_fim.strftime('%d/%m/%Y')
        url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie_id}/dados?formato=json&dataInicial={dt_i_str}&dataFinal={dt_f_str}"

        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200 and resp.json():
            df = pd.DataFrame(resp.json())
            df['data'] = pd.to_datetime(df['data'], format='%d/%m/%Y')
            df['valor'] = pd.to_numeric(df['valor'], errors='coerce')
            return df.dropna(subset=['valor']).sort_values('data')
    except Exception as e:
        print("Erro ao consultar API do BCB:", e)
    return pd.DataFrame()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/pesquisar', methods=['GET'])
def pesquisar_fundos():
    termo = request.args.get('termo', '').strip().lower()
    df_cad = carregar_cadastro()

    if not termo or df_cad.empty:
        return jsonify([])

    df_busca = df_cad.copy()
    df_busca['NOME_LOWER'] = df_busca['NOME_FUNDO'].astype(str).str.lower()
    df_busca['CNPJ_STR'] = df_busca['CNPJ_FUNDO'].astype(str)

    termo_limpo = ''.join(filter(str.isdigit, termo))

    if termo_limpo and len(termo_limpo) >= 3:
        resultado = df_busca[
            df_busca['NOME_LOWER'].str.contains(termo, na=False) |
            df_busca['CNPJ_STR'].str.contains(termo_limpo, na=False)
        ].head(30)
    else:
        resultado = df_busca[df_busca['NOME_LOWER'].str.contains(termo, na=False)].head(30)

    lista = []
    for _, row in resultado.iterrows():
        classe = row.get('CLASSE', 'Não informada')
        admin = row.get('ADMINISTRADOR', row.get('ADMIN', 'Não informado'))
        lista.append({
            'cnpj': str(row['CNPJ_FUNDO']).zfill(14),
            'nome': str(row['NOME_FUNDO']).strip(),
            'classe': str(classe).strip(),
            'admin': str(admin).strip()
        })

    return jsonify(lista)

@app.route('/api/comparar', methods=['POST'])
def comparar():
    req_data = request.json or {}
    cnpjs = [str(c).strip().replace('.', '').replace('/', '').replace('-', '').zfill(14) for c in req_data.get('cnpjs', [])]
    incluir_cdi = str(req_data.get('cdi', False)).lower() in ['true', '1', 't', 'yes']
    periodo = req_data.get('periodo', '12m')

    qtd_meses = 12 if periodo == '12m' else 24
    df_cotas = buscar_cotas_cnpjs(cnpjs, qtd_meses=qtd_meses)

    if df_cotas.empty:
        return jsonify({'sucesso': False, 'msg': 'Nenhuma cotação encontrada para os fundos selecionados.'})

    data_maxima = df_cotas['DT_COMPTC'].max()

    if periodo == '12m':
        data_corte = data_maxima - timedelta(days=365)
        df_cotas = df_cotas[df_cotas['DT_COMPTC'] >= data_corte]

    data_inicio = df_cotas['DT_COMPTC'].min()
    data_fim = df_cotas['DT_COMPTC'].max()

    resultado = {}
    df_cad = carregar_cadastro()

    rent_cdi = None
    if incluir_cdi and data_inicio and data_fim:
        df_cdi = buscar_serie_bc(12, data_inicio, data_fim)
        if not df_cdi.empty:
            df_cdi['fator'] = 1.0 + (df_cdi['valor'] / 100.0)
            df_cdi['acumulado'] = df_cdi['fator'].cumprod()
            primeiro = df_cdi['acumulado'].iloc[0]
            df_cdi['norm'] = (df_cdi['acumulado'] / primeiro) * 100.0

            serie_cdi = [{'data': r['data'].strftime('%Y-%m-%d'), 'valor': float(r['norm'])} for _, r in df_cdi.iterrows()]
            rent_cdi = float((df_cdi['fator'].prod() - 1.0) * 100.0)
            vol_cdi = float(df_cdi['valor'].std() * (252 ** 0.5))

            resultado['CDI'] = {
                'nome': 'CDI (Banco Central)',
                'serie': serie_cdi,
                'rentabilidadeTotal': rent_cdi,
                'pctCdi': 100.0,
                'volatilidade': vol_cdi
            }

    for cnpj in cnpjs:
        temp = df_cotas[df_cotas['CNPJ_FUNDO'] == cnpj].sort_values('DT_COMPTC')
        if temp.empty:
            continue

        cota_inicial = temp['VL_QUOTA'].iloc[0]
        serie = []
        retornos = temp['VL_QUOTA'].pct_change().dropna()
        vol_anual = float(retornos.std() * (252 ** 0.5) * 100) if len(retornos) > 1 else 0.0

        for _, row in temp.iterrows():
            norm = float((row['VL_QUOTA'] / cota_inicial) * 100)
            serie.append({
                'data': row['DT_COMPTC'].strftime('%Y-%m-%d'),
                'valor': norm
            })

        cota_fim = temp['VL_QUOTA'].iloc[-1]
        rentabilidade = float(((cota_fim / cota_inicial) - 1) * 100)
        pct_cdi = float((rentabilidade / rent_cdi) * 100) if (rent_cdi and rent_cdi != 0) else None

        nome_fundo = f"CNPJ: {cnpj}"
        if not df_cad.empty:
            match = df_cad[df_cad['CNPJ_FUNDO'] == cnpj]
            if not match.empty:
                nome_fundo = match.iloc[0].get('NOME_FUNDO', nome_fundo)

        resultado[cnpj] = {
            'nome': str(nome_fundo).strip(),
            'serie': serie,
            'rentabilidadeTotal': rentabilidade,
            'pctCdi': pct_cdi,
            'volatilidade': vol_anual
        }

    return jsonify({'sucesso': True, 'dados': resultado, 'periodo': periodo})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)