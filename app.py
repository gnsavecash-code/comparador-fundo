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

dados_cvm_cache = pd.DataFrame()
cad_fundos_cache = pd.DataFrame()

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def obter_ultimos_meses(qtd_meses=12):
    """Gera lista no formato YYYYMM para os últimos N meses a partir de hoje"""
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

def inicializar_dados():
    global dados_cvm_cache, cad_fundos_cache
    
    arq_cad_csv = os.path.join(CACHE_DIR, "cad_fi.csv")
    url_cad = "https://dados.cvm.gov.br/dados/FI/CAD/DADOS/cad_fi.csv"
    
    print("🚀 Iniciando carregamento das bases de dados...")

    # 1. Baixa o Cadastro de Fundos da CVM caso não exista localmente
    if not os.path.exists(arq_cad_csv):
        try:
            print("🔄 Baixando cadastro atualizado de fundos da CVM...")
            resp_cad = requests.get(url_cad, headers=HEADERS, timeout=60)
            if resp_cad.status_code == 200:
                with open(arq_cad_csv, "wb") as f:
                    f.write(resp_cad.content)
                print("✅ Cadastro de fundos baixado e salvo localmente!")
        except Exception as e:
            print("❌ Erro ao baixar cadastro da CVM:", e)

    # 2. Carrega o Cadastro Local e filtra fundos em funcionamento normal
    if os.path.exists(arq_cad_csv):
        try:
            print("📦 Lendo 'cad_fi.csv' da pasta cache_cvm...")
            df_cad = pd.read_csv(arq_cad_csv, sep=";", encoding="ISO-8859-1", low_memory=False)
            df_cad.columns = df_cad.columns.str.replace('"', '').str.strip().str.upper()
            
            c_cnpj = next((c for c in df_cad.columns if 'CNPJ' in c), None)
            c_nome = next((c for c in df_cad.columns if 'DENOM' in c or 'NOME' in c), None)
            c_sit = next((c for c in df_cad.columns if 'SIT' in c), None)
            
            if c_cnpj:
                df_cad['CNPJ_FUNDO'] = df_cad[c_cnpj].astype(str).str.replace(r'\D', '', regex=True).str.zfill(14)
                df_cad['NOME_FUNDO'] = df_cad[c_nome].astype(str).str.strip() if c_nome else "Fundo CNPJ: " + df_cad['CNPJ_FUNDO']
                
                if c_sit:
                    df_cad = df_cad[df_cad[c_sit].astype(str).str.strip().str.upper() == 'EM FUNCIONAMENTO NORMAL']

                cad_fundos_cache = df_cad
                print(f"✅ Cadastro carregado! Total de fundos ativos: {len(df_cad)}")
        except Exception as e:
            print("❌ Erro ao ler cad_fi.csv:", e)

    # 3. Baixa e consolida os últimos 12 meses de cotações
    meses_para_carregar = obter_ultimos_meses(12)
    dfs_cotas = []

    for ano_mes in meses_para_carregar:
        arq_inf = os.path.join(CACHE_DIR, f"cvm_{ano_mes}.parquet")
        df_mes = pd.DataFrame()

        if os.path.exists(arq_inf):
            try:
                print(f"📦 Carregando cotas ({ano_mes}) do cache local...")
                df_mes = pd.read_parquet(arq_inf)
            except Exception as e:
                print(f"❌ Erro ao ler cache {arq_inf}:", e)

        if df_mes.empty:
            try:
                print(f"🔄 Baixando informe diário de cotas da CVM ({ano_mes})...")
                url_inf = f"https://dados.cvm.gov.br/dados/FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{ano_mes}.zip"
                resp = requests.get(url_inf, headers=HEADERS, timeout=60)
                
                if resp.status_code == 200:
                    z = zipfile.ZipFile(io.BytesIO(resp.content))
                    csv_name = z.namelist()[0]
                    with z.open(csv_name) as f:
                        df_mes = pd.read_csv(f, sep=";", encoding="ISO-8859-1", low_memory=False)
                    
                    df_mes.columns = df_mes.columns.str.replace('"', '').str.strip().str.upper()
                    c_cnpj = next((c for c in df_mes.columns if 'CNPJ' in c), None)
                    c_quota = next((c for c in df_mes.columns if 'QUOTA' in c or 'COTA' in c), None)
                    c_data = next((c for c in df_mes.columns if 'DATA' in c or 'COMPTC' in c), None)
                    
                    df_mes = df_mes.rename(columns={c_cnpj: 'CNPJ_FUNDO', c_quota: 'VL_QUOTA', c_data: 'DT_COMPTC'})
                    df_mes['CNPJ_FUNDO'] = df_mes['CNPJ_FUNDO'].astype(str).str.replace(r'\D', '', regex=True).str.zfill(14)
                    df_mes['DT_COMPTC'] = pd.to_datetime(df_mes['DT_COMPTC'], errors='coerce')
                    df_mes['VL_QUOTA'] = df_mes['VL_QUOTA'].astype(str).str.replace(',', '.').astype(float)
                    
                    df_mes.to_parquet(arq_inf, index=False)
                    print(f"✅ Cotas ({ano_mes}) baixadas e salvas em cache!")
            except Exception as e:
                print(f"⚠️ Informe ({ano_mes}) ainda não disponível na CVM.")

        if not df_mes.empty:
            dfs_cotas.append(df_mes)

    if dfs_cotas:
        dados_cvm_cache = pd.concat(dfs_cotas, ignore_index=True)
        dados_cvm_cache['CNPJ_FUNDO'] = dados_cvm_cache['CNPJ_FUNDO'].astype(str).str.zfill(14)
        dados_cvm_cache = dados_cvm_cache.sort_values('DT_COMPTC').drop_duplicates(subset=['CNPJ_FUNDO', 'DT_COMPTC'])
        print(f"✅ Base total de cotas consolidada! Total de registros: {len(dados_cvm_cache)}")

    # 4. Cruzamento final com o cadastro
    if not cad_fundos_cache.empty and not dados_cvm_cache.empty:
        cnpjs_com_cotas = dados_cvm_cache['CNPJ_FUNDO'].unique()
        cad_fundos_cache = cad_fundos_cache[cad_fundos_cache['CNPJ_FUNDO'].isin(cnpjs_com_cotas)]
        print(f"✅ Cruzamento concluído! Fundos ativos negociados no período: {len(cad_fundos_cache)}")

inicializar_dados()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/pesquisar', methods=['GET'])
def pesquisar_fundos():
    termo = request.args.get('termo', '').strip().lower()
    if not termo or cad_fundos_cache.empty:
        return jsonify([])

    c_nome = 'NOME_FUNDO' if 'NOME_FUNDO' in cad_fundos_cache.columns else next((c for c in cad_fundos_cache.columns if 'DENOM' in c or 'NOME' in c), None)
    if not c_nome:
        return jsonify([])

    df_busca = cad_fundos_cache.copy()
    df_busca['NOME_LOWER'] = df_busca[c_nome].astype(str).str.lower()
    df_busca['CNPJ_STR'] = df_busca['CNPJ_FUNDO'].astype(str)
    
    termo_limpo = ''.join(filter(str.isdigit, termo))

    if termo_limpo and len(termo_limpo) >= 3:
        resultado = df_busca[
            df_busca['NOME_LOWER'].str.contains(termo, na=False) | 
            df_busca['CNPJ_STR'].str.contains(termo_limpo, na=False)
        ].head(30)
    else:
        resultado = df_busca[
            df_busca['NOME_LOWER'].str.contains(termo, na=False)
        ].head(30)

    lista = []
    for _, row in resultado.iterrows():
        cnpj = row['CNPJ_FUNDO']
        nome = row[c_nome]
        classe = row.get('CLASSE', row.get('CLASSE_ANBIMA', 'Não informada'))
        admin = row.get('ADMIN', row.get('ADMINISTRADOR', 'Não informado'))
        
        lista.append({
            'cnpj': str(cnpj).zfill(14),
            'nome': str(nome).strip(),
            'classe': str(classe).strip(),
            'admin': str(admin).strip()
        })

    return jsonify(lista)

def buscar_serie_bc(serie_id, data_inicio, data_fim):
    try:
        dt_i_str = data_inicio.strftime('%d/%m/%Y')
        dt_f_str = data_fim.strftime('%d/%m/%Y')
        url = f"https://api.bcb.gov.br/dados/serie/bcdata.sgs.{serie_id}/dados?formato=json&dataInicial={dt_i_str}&dataFinal={dt_f_str}"
        
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            dados = resp.json()
            if not dados:
                return pd.DataFrame()
            
            df = pd.DataFrame(dados)
            df['data'] = pd.to_datetime(df['data'], format='%d/%m/%Y')
            df['valor'] = pd.to_numeric(df['valor'], errors='coerce')
            df = df.dropna(subset=['valor']).sort_values('data')
            return df
    except Exception as e:
        print("❌ Exceção ao consultar API do BCB:", e)
    return pd.DataFrame()

@app.route('/api/comparar', methods=['POST'])
def comparar():
    global dados_cvm_cache, cad_fundos_cache
    if dados_cvm_cache.empty:
        return jsonify({'sucesso': False, 'msg': 'Dados de cotas da CVM não disponíveis.'})

    req_data = request.json or {}
    cnpjs = [str(c).strip().replace('.', '').replace('/', '').replace('-', '').zfill(14) for c in req_data.get('cnpjs', [])]
    incluir_cdi = str(req_data.get('cdi', False)).lower() in ['true', '1', 't', 'yes']
    periodo = req_data.get('periodo', '12m')

    df_filtrado = dados_cvm_cache[dados_cvm_cache['CNPJ_FUNDO'].isin(cnpjs)].sort_values('DT_COMPTC')
    if df_filtrado.empty:
        return jsonify({'sucesso': False, 'msg': 'Nenhuma cotação encontrada para os fundos selecionados.'})

    data_maxima = df_filtrado['DT_COMPTC'].max()

    # Filtra período de análise
    if periodo == '12m':
        data_corte = data_maxima - timedelta(days=365)
        df_filtrado = df_filtrado[df_filtrado['DT_COMPTC'] >= data_corte]

    data_inicio = df_filtrado['DT_COMPTC'].min()
    data_fim = df_filtrado['DT_COMPTC'].max()

    resultado = {}
    c_nome = 'NOME_FUNDO' if 'NOME_FUNDO' in cad_fundos_cache.columns else next((c for c in cad_fundos_cache.columns if 'DENOM' in c or 'NOME' in c), None) if not cad_fundos_cache.empty else None

    # Processamento do CDI
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
                'nome': 'CDI',
                'serie': serie_cdi,
                'rentabilidadeTotal': rent_cdi,
                'pctCdi': 100.0,
                'volatilidade': vol_cdi
            }

    # Processamento dos Fundos
    for cnpj in cnpjs:
        temp = df_filtrado[df_filtrado['CNPJ_FUNDO'] == cnpj].sort_values('DT_COMPTC')
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
        if not cad_fundos_cache.empty and c_nome:
            match = cad_fundos_cache[cad_fundos_cache['CNPJ_FUNDO'] == cnpj]
            if not match.empty:
                nome_fundo = match.iloc[0].get(c_nome, nome_fundo)

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