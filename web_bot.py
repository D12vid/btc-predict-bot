import time
import requests
import pandas as pd
from datetime import datetime
from flask import Flask, jsonify, render_template_string, make_response

app = Flask(__name__)

class CloudScalpingEngine:
    def __init__(self):
        self.monto_apuesta = 1.0
        self.pago_neto = 0.88
        
        self.precio = 0.0
        self.rsi = 50.0
        self.tendencia = "⏳ Conectando..."
        self.fuente_datos = "Iniciando..."
        
        self.prediccion_5m = {
            "accion": "SINCRONIZANDO...",
            "color": "gray",
            "razon": "Estableciendo enlace con el mercado...",
            "confianza": "50%",
            "precio_apertura": 0.0,
            "dif_actual": 0.0,
            "estado_actual": "⏳ Analizando",
            "id_vela": 0
        }

        self.s_estricta = {
            "titulo": "🎯 PnL Señal Estricta ($1/trade): $0.00 USDT",
            "desc": "Esperando datos...",
            "confianza": "W:0 / L:0",
            "color": "gray"
        }
        self.s_dinamica = {
            "titulo": "⚡ PnL Predict Filtrado ($1/trade): $0.00 USDT",
            "desc": "Esperando datos...",
            "riesgo": "W:0 / L:0",
            "color": "gray"
        }
        
        self.hist_estricto = []
        self.hist_dinamico = []
        self.w_est = 0
        self.l_est = 0
        self.w_din = 0
        self.l_din = 0
        
        self.vela_actual_ts = None
        self.prediccion_vela_actual = None
        self.backtest_completado = False
        self.ultimo_update = 0

    def obtener_velas_multifuente(self):
        """Intenta 4 servidores públicos en cascada para evitar el bloqueo de IP en Render (EE.UU.)."""
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        
        # 1. Endpoints compatibles con formato Binance (Vision no bloquea IPs de EE.UU.)
        urls_binance = [
            ("Binance Vision", "https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=80"),
            ("Binance US", "https://api.binance.us/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=80"),
            ("Binance Global", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=80")
        ]
        
        for nombre, url in urls_binance:
            try:
                r = requests.get(url, headers=headers, timeout=4)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 30:
                        df = pd.DataFrame(data, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'ct', 'qav', 'nt', 'tb', 'tq', 'ig'])
                        for col in ['open', 'high', 'low', 'close', 'vol']:
                            df[col] = df[col].astype(float)
                        df['ts'] = df['ts'].astype(int)
                        self.fuente_datos = f"🟢 {nombre}"
                        return df
            except Exception:
                pass

        # 2. Respaldo Institucional: Bybit V5 Public API
        try:
            url_bybit = "https://api.bybit.com/v5/market/kline?category=spot&symbol=BTCUSDT&interval=5&limit=80"
            r = requests.get(url_bybit, headers=headers, timeout=4)
            if r.status_code == 200:
                lista = r.json().get('result', {}).get('list', [])
                if len(lista) > 30:
                    lista.reverse() # Bybit entrega de más reciente a más antigua
                    df = pd.DataFrame(lista, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'turnover'])
                    for col in ['open', 'high', 'low', 'close', 'vol']:
                        df[col] = df[col].astype(float)
                    df['ts'] = df['ts'].astype(int)
                    self.fuente_datos = "🟢 Bybit Spot"
                    return df
        except Exception:
            pass

        # 3. Respaldo EE.UU. Garantizado: Coinbase Exchange API
        try:
            url_cb = "https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=300"
            r = requests.get(url_cb, headers=headers, timeout=4)
            if r.status_code == 200:
                lista = r.json()
                if isinstance(lista, list) and len(lista) > 30:
                    lista = lista[:80]
                    lista.reverse()
                    df = pd.DataFrame(lista, columns=['ts', 'low', 'high', 'open', 'close', 'vol'])
                    for col in ['open', 'high', 'low', 'close', 'vol']:
                        df[col] = df[col].astype(float)
                    df['ts'] = df['ts'].astype(int) * 1000
                    self.fuente_datos = "🟢 Coinbase"
                    return df
        except Exception as e:
            self.fuente_datos = f"🔴 Error Red: {str(e)[:25]}"

        return None

    def calcular_indicadores(self, df):
        delta = df['close'].diff()
        gain = delta.clip(lower=0)
        loss = -1 * delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        rs = avg_gain / avg_loss
        df['RSI'] = 100 - (100 / (1 + rs))
        
        df['EMA_9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['EMA_21'] = df['close'].ewm(span=21, adjust=False).mean()
        df['sma'] = df['close'].rolling(20).mean()
        df['std'] = df['close'].rolling(20).std()
        df['upper'] = df['sma'] + (df['std'] * 2)
        df['lower'] = df['sma'] - (df['std'] * 2)
        
        df['body'] = (df['close'] - df['open']).abs()
        df['upper_wick'] = df['high'] - df[['open', 'close']].max(axis=1)
        df['lower_wick'] = df[['open', 'close']].min(axis=1) - df['low']
        return df

    def evaluar_vela_predict(self, previa, antepenultima):
        rsi = float(previa['RSI'])
        rsi_ant = float(antepenultima['RSI'])
        ema9 = float(previa['EMA_9'])
        ema21 = float(previa['EMA_21'])
        cierre = float(previa['close'])
        apertura = float(previa['open'])
        upper = float(previa['upper'])
        lower = float(previa['lower'])
        mecha_sup = float(previa['upper_wick'])
        mecha_inf = float(previa['lower_wick'])
        cuerpo = float(previa['body'])

        if (rsi >= 66 or cierre >= upper) and (cierre < apertura or mecha_sup > cuerpo * 0.7 or rsi < rsi_ant):
            return "DOWN", f"Rechazo bajista confirmado en techo (RSI {rsi:.1f}).", "90%", True
        
        if (rsi <= 34 or cierre <= lower) and (cierre > apertura or mecha_inf > cuerpo * 0.7 or rsi > rsi_ant):
            return "UP", f"Rebote alcista confirmado en piso (RSI {rsi:.1f}).", "90%", True

        rango_bandas = upper - lower
        if (45 <= rsi <= 55 and cuerpo < 12) or rango_bandas < 50:
            return "SKIP", f"Mercado lateral sin volumen (RSI {rsi:.1f}). Protegiendo $1.00 USD.", "Neutral", False

        if ema9 > ema21 and cierre >= apertura and 51 <= rsi < 66:
            return "UP", f"Impulso alcista limpio (EMA9 > EMA21, RSI {rsi:.1f}).", "78%", False
        
        if ema9 < ema21 and cierre < apertura and 34 < rsi <= 49:
            return "DOWN", f"Impulso bajista limpio (EMA9 < EMA21, RSI {rsi:.1f}).", "78%", False

        return "SKIP", f"Vela de transición sin ventaja clara (RSI {rsi:.1f}). Esperando mejor entrada.", "Neutral", False

    def cargar_backtest_inicial(self, df):
        for i in range(25, len(df) - 1):
            vela_eval = df.iloc[i]
            vela_prev = df.iloc[i - 1]
            vela_ante = df.iloc[i - 2]
            
            dir_pred, razon, conf, es_estricto = self.evaluar_vela_predict(vela_prev, vela_ante)
            if dir_pred == "SKIP":
                continue

            apertura = float(vela_eval['open'])
            cierre = float(vela_eval['close'])
            rsi_prev = float(vela_prev['RSI'])
            hora_vela = datetime.fromtimestamp(int(vela_eval['ts']) / 1000).strftime('%H:%M')

            gano = (dir_pred == "UP" and cierre > apertura) or (dir_pred == "DOWN" and cierre < apertura)
            etiqueta_tipo = "🟢 COMPRAR UP" if dir_pred == "UP" else "🔴 COMPRAR DOWN"
            res_texto = "✅ WIN" if gano else "❌ LOSS"

            if gano: self.w_din += 1
            else: self.l_din += 1
            self.hist_dinamico.insert(0, {
                "hora": hora_vela, "tipo": etiqueta_tipo,
                "entrada": f"${apertura:,.2f}", "extremo": f"Cierre: ${cierre:,.2f}", "res": res_texto
            })

            if es_estricto:
                if gano: self.w_est += 1
                else: self.l_est += 1
                self.hist_estricto.insert(0, {
                    "hora": hora_vela, "tipo": etiqueta_tipo,
                    "entrada": f"${apertura:,.2f}", "extremo": f"RSI {rsi_prev:.1f}", "res": res_texto
                })

        self.hist_dinamico = self.hist_dinamico[:10]
        self.hist_estricto = self.hist_estricto[:10]
        self.backtest_completado = True

    def actualizar_datos(self):
        ahora = time.time()
        if ahora - self.ultimo_update < 2.5 and self.backtest_completado:
            return

        df = self.obtener_velas_multifuente()
        if df is None or df.empty:
            self.prediccion_5m["razon"] = f"Fallo de conexión: {self.fuente_datos}"
            return

        df = self.calcular_indicadores(df)
        if not self.backtest_completado:
            self.cargar_backtest_inicial(df)

        actual = df.iloc[-1]
        previa = df.iloc[-2]
        antepenultima = df.iloc[-3]

        self.precio = float(actual['close'])
        apertura_actual = float(actual['open'])
        self.rsi = round(float(actual['RSI']), 1) if not pd.isna(actual['RSI']) else 50.0

        ts_actual = int(actual['ts'])
        if self.vela_actual_ts is not None and ts_actual != self.vela_actual_ts:
            ap_cerrada = float(previa['open'])
            ci_cerrada = float(previa['close'])
            hora_cerrada = datetime.fromtimestamp(int(previa['ts']) / 1000).strftime('%H:%M')
            
            if self.prediccion_vela_actual and self.prediccion_vela_actual["dir"] in ["UP", "DOWN"]:
                dir_ant = self.prediccion_vela_actual["dir"]
                gano = (dir_ant == "UP" and ci_cerrada > ap_cerrada) or (dir_ant == "DOWN" and ci_cerrada < ap_cerrada)
                etiqueta = "🟢 COMPRAR UP" if dir_ant == "UP" else "🔴 COMPRAR DOWN"
                res_str = "✅ WIN" if gano else "❌ LOSS"

                if gano: self.w_din += 1
                else: self.l_din += 1
                self.hist_dinamico.insert(0, {
                    "hora": hora_cerrada, "tipo": etiqueta,
                    "entrada": f"${ap_cerrada:,.2f}", "extremo": f"Cierre: ${ci_cerrada:,.2f}", "res": res_str
                })
                self.hist_dinamico = self.hist_dinamico[:10]

                if self.prediccion_vela_actual.get("estricto"):
                    if gano: self.w_est += 1
                    else: self.l_est += 1
                    self.hist_estricto.insert(0, {
                        "hora": hora_cerrada, "tipo": etiqueta,
                        "entrada": f"${ap_cerrada:,.2f}", "extremo": f"RSI {self.prediccion_vela_actual['rsi']:.1f}", "res": res_str
                    })
                    self.hist_estricto = self.hist_estricto[:10]

        self.vela_actual_ts = ts_actual

        dir_actual, razon_actual, conf_actual, es_estricto_actual = self.evaluar_vela_predict(previa, antepenultima)
        self.prediccion_vela_actual = {
            "hora": datetime.fromtimestamp(ts_actual / 1000).strftime('%H:%M'),
            "dir": dir_actual,
            "tipo": "🟢 COMPRAR UP" if dir_actual == "UP" else ("🔴 COMPRAR DOWN" if dir_actual == "DOWN" else "⚪ NO OPERAR"),
            "entrada": apertura_actual,
            "rsi": float(previa['RSI']),
            "estricto": es_estricto_actual
        }

        dif = self.precio - apertura_actual
        if dir_actual == "SKIP":
            accion_txt = "⚪ NO APOSTAR EN ESTA VELA (ESPERAR)"
            color_hero = "gray"
            estado_txt = "🛡️ PROTEGIENDO TU $1.00 USD"
        else:
            accion_txt = "🟢 COMPRAR UP (SUBE)" if dir_actual == "UP" else "🔴 COMPRAR DOWN (BAJA)"
            color_hero = "up" if dir_actual == "UP" else "down"
            ganando_ahora = (dir_actual == "UP" and dif > 0) or (dir_actual == "DOWN" and dif < 0)
            estado_txt = "🔥 GANANDO EN VIVO" if ganando_ahora else "⏳ EN DESARROLLO"

        self.prediccion_5m = {
            "accion": accion_txt,
            "color": color_hero,
            "razon": razon_actual,
            "confianza": conf_actual,
            "precio_apertura": apertura_actual,
            "dif_actual": dif,
            "estado_actual": estado_txt,
            "id_vela": ts_actual if dir_actual != "SKIP" else 0
        }

        self.tendencia = "🚀 Subida Activa" if dif >= 0 else "📉 Bajada Activa"

        pnl_estricto = (self.w_est * self.pago_neto) - (self.l_est * self.monto_apuesta)
        pnl_dinamico = (self.w_din * self.pago_neto) - (self.l_din * self.monto_apuesta)

        self.s_estricta = {
            "titulo": f"🎯 Rentabilidad Señal Estricta ($1/trade): {'+' if pnl_estricto>=0 else ''}${pnl_estricto:.2f} USDT",
            "desc": f"Fuente activa: {self.fuente_datos} | Solo entra en extremos con confirmación de rechazo.",
            "confianza": f"W:{self.w_est} / L:{self.l_est}",
            "color": "green" if pnl_estricto > 0 else ("red" if pnl_estricto < 0 else "gray")
        }

        self.s_dinamica = {
            "titulo": f"⚡ Rentabilidad Predict Filtrado ($1/trade): {'+' if pnl_dinamico>=0 else ''}${pnl_dinamico:.2f} USDT",
            "desc": "Calculado descontando comisión de plataforma (Pago estimado 1.88x).",
            "riesgo": f"W:{self.w_din} / L:{self.l_din}",
            "color": "green" if pnl_dinamico > 0 else ("red" if pnl_dinamico < 0 else "gray")
        }
        self.ultimo_update = ahora

bot = CloudScalpingEngine()

HTML_UI = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>BTC 5M - Binance Predict Pro V4.0</title>
    <style>
        * { box-sizing: border-box; font-family: 'Segoe UI', Roboto, Helvetica, sans-serif; }
        body { background-color: #090b10; color: #f0f6fc; margin: 0; padding: 20px; display: flex; justify-content: center; }
        .main-panel { width: 100%; max-width: 1120px; background-color: #0f1218; border: 1px solid #21262d; border-radius: 14px; padding: 22px; box-shadow: 0 12px 35px rgba(0,0,0,0.8); }
        
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1f242c; padding-bottom: 14px; margin-bottom: 18px; }
        .title { color: #f59e0b; font-size: 20px; font-weight: 800; display: flex; align-items: center; gap: 8px; }
        .version-tag { background: #3b82f6; color: #fff; font-size: 11px; padding: 3px 8px; border-radius: 5px; font-weight: 800; }
        .header-controls { display: flex; align-items: center; gap: 10px; }
        .btn-sound { background-color: #161a23; border: 1px solid #30363d; color: #e5e7eb; padding: 6px 12px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; }
        .btn-sound.active { border-color: #f59e0b; color: #f59e0b; background-color: rgba(245, 158, 11, 0.12); }
        .badge-online { background-color: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #10b981; padding: 6px 14px; border-radius: 8px; font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 7px; }
        .dot { width: 9px; height: 9px; background-color: #10b981; border-radius: 50%; display: inline-block; box-shadow: 0 0 8px #10b981; }

        .predict-hero { border-radius: 12px; padding: 18px 22px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; border: 2px solid #30363d; background: #141821; transition: 0.3s; }
        .predict-hero.up { border-color: #10b981; background: linear-gradient(90deg, rgba(16,185,129,0.16) 0%, rgba(20,24,33,0.95) 100%); }
        .predict-hero.down { border-color: #ef4444; background: linear-gradient(90deg, rgba(239,68,68,0.16) 0%, rgba(20,24,33,0.95) 100%); }
        .predict-hero.gray { border-color: #4b5563; background: #161a23; }
        .predict-label { font-size: 12px; text-transform: uppercase; letter-spacing: 1px; color: #9ca3af; font-weight: 700; margin-bottom: 4px; }
        .predict-action { font-size: 27px; font-weight: 900; margin-bottom: 5px; color: #e5e7eb; }
        .predict-hero.up .predict-action { color: #10b981; }
        .predict-hero.down .predict-action { color: #ef4444; }
        .predict-reason { font-size: 14px; color: #d1d5db; }
        .predict-stats { text-align: right; background: rgba(0,0,0,0.35); padding: 10px 16px; border-radius: 10px; border: 1px solid #262c36; min-width: 240px; }
        .stat-line { font-size: 13px; color: #9ca3af; margin-bottom: 4px; }
        .stat-line b { color: #fff; }
        .live-status { font-size: 13px; font-weight: 800; padding-top: 4px; display: inline-block; }

        .metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 16px; }
        .metric-card { background-color: #161a23; border: 1px solid #222733; border-radius: 10px; padding: 14px 16px; }
        .metric-label { color: #8b949e; font-size: 13px; margin-bottom: 6px; }
        .metric-val { font-size: 25px; font-weight: 800; color: #ffffff; }
        .metric-trend { font-size: 17px; font-weight: 700; margin-top: 5px; }
        .metric-timer { font-size: 25px; font-weight: 800; color: #f59e0b; }

        .signal-box { background-color: #161a23; border-radius: 10px; padding: 14px 18px; margin-bottom: 14px; display: flex; justify-content: space-between; align-items: center; border-left: 5px solid #6e7681; }
        .signal-box.red { border-left-color: #ef4444; background-color: rgba(239, 68, 68, 0.06); }
        .signal-box.green { border-left-color: #10b981; background-color: rgba(16, 185, 129, 0.06); }
        .signal-title { font-size: 15px; font-weight: 700; margin-bottom: 4px; }
        .signal-desc { color: #9ca3af; font-size: 13px; }
        .pill { background-color: #212631; color: #d1d5db; padding: 6px 12px; border-radius: 7px; font-size: 12px; font-weight: 600; border: 1px solid #30363d; }

        .history-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 18px; }
        .history-card { background-color: #13171f; border-radius: 10px; padding: 15px; border-top: 3px solid #10b981; border: 1px solid #21262d; border-top-width: 3px; }
        .history-card.dynamic { border-top-color: #f59e0b; }
        .history-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
        .h-title-strict { color: #10b981; font-weight: 700; font-size: 15px; }
        .h-title-dyn { color: #f59e0b; font-weight: 700; font-size: 15px; }
        .wl-counter { font-size: 13px; font-weight: 700; color: #8b949e; }
        .wl-w { color: #10b981; }
        .wl-l { color: #ef4444; }
        .wr-badge { color: #f0f6fc; background: #212631; padding: 2px 7px; border-radius: 4px; margin-left: 5px; font-size: 12px; border: 1px solid #30363d; }
        .btn-trash { background: #1f242d; border: 1px solid #30363d; color: #9ca3af; border-radius: 6px; padding: 4px 8px; cursor: pointer; margin-left: 8px; }

        table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
        th { background-color: #1b202b; color: #8b949e; font-weight: 600; text-align: left; padding: 8px 9px; border-radius: 4px; }
        td { padding: 8px 9px; border-bottom: 1px solid #1f242c; color: #e5e7eb; }
        .pending-row { background-color: rgba(245, 158, 11, 0.12); color: #fbbf24; font-weight: 700; }
    </style>
</head>
<body>
    <div class="main-panel">
        <div class="header">
            <div class="title">
                ⚡ BTC 5M - Binance Predict Pro
                <span class="version-tag">V4.0 MULTI-API CLOUD</span>
            </div>
            <div class="header-controls">
                <button class="btn-sound" id="btn-audio" onclick="toggleAudio()">🔇 Activar Sonido</button>
                <div class="badge-online"><span class="dot"></span> <span id="fuente-tag">En línea</span></div>
            </div>
        </div>

        <div class="predict-hero" id="hero-predict">
            <div>
                <div class="predict-label">🎯 SEÑAL FILTRADA BINANCE PREDICT (5 MINUTOS)</div>
                <div class="predict-action" id="pred-accion">CONECTANDO...</div>
                <div class="predict-reason" id="pred-razon">Enlazando con pasarela de datos...</div>
            </div>
            <div class="predict-stats">
                <div class="stat-line">Apertura Vela 5M: <b id="pred-apertura">$0.00</b></div>
                <div class="stat-line">Diferencia Actual: <b id="pred-dif">$0.00</b></div>
                <div class="live-status" id="pred-estado">⏳ Calculando...</div>
            </div>
        </div>

        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-label">Precio BTC (En Vivo)</div>
                <div class="metric-val" id="precio">$0.00</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">RSI (14) [Scalp]</div>
                <div class="metric-val" id="rsi">0.0</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Tendencia / Impulso</div>
                <div class="metric-trend" id="tendencia">Cargando...</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Vela 5M (Restante)</div>
                <div class="metric-timer" id="reloj">00:00</div>
            </div>
        </div>

        <div class="signal-box gray" id="box-estricto">
            <div>
                <div class="signal-title" id="est-titulo">Cargando PnL Estricto...</div>
                <div class="signal-desc" id="est-desc">Simulación con entradas de $1.00 USD...</div>
            </div>
            <div class="pill" id="est-conf">W:0 / L:0</div>
        </div>

        <div class="signal-box gray" id="box-dinamico">
            <div>
                <div class="signal-title" id="din-titulo">Cargando PnL Filtrado...</div>
                <div class="signal-desc" id="din-desc">Simulación descontando comisión...</div>
            </div>
            <div class="pill" id="din-riesgo">W:0 / L:0</div>
        </div>

        <div class="history-grid">
            <div class="history-card">
                <div class="history-header">
                    <span class="h-title-strict">🎯 Historial Estricto (Rechazo Confirmado)</span>
                    <div>
                        <span class="wl-counter">
                            W: <span class="wl-w" id="w-est">0</span> | L: <span class="wl-l" id="l-est">0</span>
                            <span class="wr-badge" id="wr-est">0%</span>
                        </span>
                        <button class="btn-trash" onclick="limpiar('estricto')">🗑️</button>
                    </div>
                </div>
                <table>
                    <thead><tr><th>Hora</th><th>Orden</th><th>Apertura</th><th>Filtro</th><th>Res.</th></tr></thead>
                    <tbody id="tabla-estricta"></tbody>
                </table>
            </div>

            <div class="history-card dynamic">
                <div class="history-header">
                    <span class="h-title-dyn">⚡ Historial Predict Filtrado (Sin Ruido)</span>
                    <div>
                        <span class="wl-counter">
                            W: <span class="wl-w" id="w-din">0</span> | L: <span class="wl-l" id="l-din">0</span>
                            <span class="wr-badge" id="wr-din">0%</span>
                        </span>
                        <button class="btn-trash" onclick="limpiar('dinamico')">🗑️</button>
                    </div>
                </div>
                <table>
                    <thead><tr><th>Hora</th><th>Orden 5M</th><th>Apertura</th><th>Cierre</th><th>Res.</th></tr></thead>
                    <tbody id="tabla-dinamica"></tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        let audioHabilitado = false;
        let audioCtx = null;
        let ultimaVelaSonada = 0;

        function toggleAudio() {
            if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
            audioHabilitado = !audioHabilitado;
            const btn = document.getElementById('btn-audio');
            if (audioHabilitado) {
                btn.innerText = "🔊 Sonido Activo";
                btn.classList.add('active');
                sonarBeep(820, 0.15);
            } else {
                btn.innerText = "🔇 Activar Sonido";
                btn.classList.remove('active');
            }
        }

        function sonarBeep(freq, dur) {
            if (!audioHabilitado || !audioCtx) return;
            const osc = audioCtx.createOscillator();
            const gain = audioCtx.createGain();
            osc.frequency.value = freq;
            gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + dur);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start();
            osc.stop(audioCtx.currentTime + dur);
        }

        function actualizarReloj() {
            const ahora = new Date();
            const segRestantes = 300 - ((ahora.getMinutes() % 5) * 60 + ahora.getSeconds());
            const m = String(Math.floor(segRestantes / 60)).padStart(2, '0');
            const s = String(segRestantes % 60).padStart(2, '0');
            document.getElementById('reloj').innerText = `${m}:${s}`;
        }
        setInterval(actualizarReloj, 1000);
        actualizarReloj();

        async function obtenerEstado() {
            try {
                const res = await fetch('/api/status?t=' + Date.now());
                const data = await res.json();
                
                document.getElementById('fuente-tag').innerText = data.fuente || 'En línea';
                document.getElementById('precio').innerText = '$' + data.precio.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
                document.getElementById('rsi').innerText = data.rsi.toFixed(1);
                document.getElementById('tendencia').innerText = data.tendencia;

                const hero = document.getElementById('hero-predict');
                hero.className = 'predict-hero ' + data.prediccion_5m.color;
                document.getElementById('pred-accion').innerText = data.prediccion_5m.accion;
                document.getElementById('pred-razon').innerText = data.prediccion_5m.razon;
                document.getElementById('pred-apertura').innerText = '$' + data.prediccion_5m.precio_apertura.toLocaleString('en-US', {minimumFractionDigits: 2});
                
                const dif = data.prediccion_5m.dif_actual;
                const signo = dif >= 0 ? '+' : '';
                document.getElementById('pred-dif').innerText = `${signo}$${dif.toFixed(2)}`;
                document.getElementById('pred-estado').innerText = data.prediccion_5m.estado_actual;

                if (data.prediccion_5m.id_vela !== 0 && data.prediccion_5m.id_vela !== ultimaVelaSonada) {
                    sonarBeep(920, 0.2);
                    setTimeout(() => sonarBeep(1200, 0.25), 220);
                    ultimaVelaSonada = data.prediccion_5m.id_vela;
                }

                document.getElementById('box-estricto').className = 'signal-box ' + data.s_estricta.color;
                document.getElementById('est-titulo').innerText = data.s_estricta.titulo;
                document.getElementById('est-desc').innerText = data.s_estricta.desc;
                document.getElementById('est-conf').innerText = data.s_estricta.confianza;

                document.getElementById('box-dinamico').className = 'signal-box ' + data.s_dinamica.color;
                document.getElementById('din-titulo').innerText = data.s_dinamica.titulo;
                document.getElementById('din-desc').innerText = data.s_dinamica.desc;
                document.getElementById('din-riesgo').innerText = data.s_dinamica.riesgo;

                document.getElementById('w-est').innerText = data.w_est;
                document.getElementById('l-est').innerText = data.l_est;
                const totEst = data.w_est + data.l_est;
                document.getElementById('wr-est').innerText = totEst > 0 ? Math.round((data.w_est / totEst) * 100) + '% WinRate' : '0%';

                document.getElementById('w-din').innerText = data.w_din;
                document.getElementById('l-din').innerText = data.l_din;
                const totDin = data.w_din + data.l_din;
                document.getElementById('wr-din').innerText = totDin > 0 ? Math.round((data.w_din / totDin) * 100) + '% WinRate' : '0%';

                const mostrarCurso = data.vela_en_curso && data.vela_en_curso.dir !== 'SKIP';
                renderTabla('tabla-estricta', data.hist_estricto, (mostrarCurso && data.vela_en_curso.estricto) ? data.vela_en_curso : null);
                renderTabla('tabla-dinamica', data.hist_dinamico, mostrarCurso ? data.vela_en_curso : null);
            } catch (e) { console.error(e); }
        }

        function renderTabla(id, filas, enCurso) {
            const tbody = document.getElementById(id);
            let html = '';
            if (enCurso) {
                html += `
                <tr class="pending-row">
                    <td>${enCurso.hora}</td>
                    <td><b>${enCurso.tipo}</b></td>
                    <td>$${enCurso.entrada.toLocaleString('en-US', {minimumFractionDigits: 2})}</td>
                    <td>En vivo...</td>
                    <td>⏳ Activa</td>
                </tr>`;
            }
            if (filas) {
                html += filas.map(f => `
                    <tr>
                        <td>${f.hora}</td>
                        <td><b>${f.tipo}</b></td>
                        <td>${f.entrada}</td>
                        <td>${f.extremo}</td>
                        <td><b>${f.res}</b></td>
                    </tr>
                `).join('');
            }
            tbody.innerHTML = html;
        }

        async function limpiar(tipo) {
            await fetch('/api/clear/' + tipo, {method: 'POST'});
            obtenerEstado();
        }

        setInterval(obtenerEstado, 2500);
        obtenerEstado();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    bot.actualizar_datos()
    resp = make_response(render_template_string(HTML_UI))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp

@app.route('/api/status')
def status():
    bot.actualizar_datos()
    resp = make_response(jsonify({
        "precio": bot.precio,
        "rsi": bot.rsi,
        "tendencia": bot.tendencia,
        "fuente": bot.fuente_datos,
        "prediccion_5m": bot.prediccion_5m,
        "s_estricta": bot.s_estricta,
        "s_dinamica": bot.s_dinamica,
        "hist_estricto": bot.hist_estricto,
        "hist_dinamico": bot.hist_dinamico,
        "w_est": bot.w_est,
        "l_est": bot.l_est,
        "w_din": bot.w_din,
        "l_din": bot.l_din,
        "vela_en_curso": bot.prediccion_vela_actual
    }))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp

@app.route('/api/clear/<tipo>', methods=['POST'])
def clear_history(tipo):
    if tipo == 'estricto':
        bot.hist_estricto = []
        bot.w_est = 0
        bot.l_est = 0
    elif tipo == 'dinamico':
        bot.hist_dinamico = []
        bot.w_din = 0
        bot.l_din = 0
    return jsonify({"ok": True})
    
