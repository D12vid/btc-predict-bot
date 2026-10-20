import ccxt
import pandas as pd
import time
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string, make_response

app = Flask(__name__)

class ScalpingWebEngine:
    def __init__(self):
        self.exchange = ccxt.binance({'enableRateLimit': True})
        self.symbol = 'BTC/USDT'
        self.timeframe = '5m'
        
        self.monto_apuesta = 1.0
        self.pago_neto = 0.88 
        
        self.precio = 0.0
        self.rsi = 50.0
        self.tendencia = "⏳ Sincronizando..."
        
        self.prediccion_5m = {
            "accion": "ESPERANDO...",
            "color": "gray",
            "razon": "Conectando con Binance en la nube...",
            "confianza": "50%",
            "precio_apertura": 0.0,
            "dif_actual": 0.0,
            "estado_actual": "⏳ Analizando",
            "id_vela": 0
        }

        self.s_estricta = {
            "titulo": "Análisis Estricto: Esperando Datos",
            "desc": "Estableciendo conexión con el servidor...",
            "confianza": "Filtro Activo",
            "color": "gray"
        }
        self.s_dinamica = {
            "titulo": "⚡ Filtro de Volatilidad y PnL",
            "desc": "Esperando primer ciclo de velas...",
            "riesgo": "PnL: $0.00",
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

        if (rsi >= 68 or cierre >= upper) and (cierre < apertura or mecha_sup > cuerpo * 0.8 or rsi < rsi_ant):
            return "DOWN", f"Rechazo bajista confirmado en techo (RSI {rsi:.1f}).", "90%", True
        
        if (rsi <= 32 or cierre <= lower) and (cierre > apertura or mecha_inf > cuerpo * 0.8 or rsi > rsi_ant):
            return "UP", f"Rebote alcista confirmado en piso (RSI {rsi:.1f}).", "90%", True

        rango_bandas = upper - lower
        if (44 <= rsi <= 56 and cuerpo < 15) or rango_bandas < 60:
            return "SKIP", f"Mercado lateral (RSI {rsi:.1f}). Protegiendo capital.", "Neutral", False

        if ema9 > ema21 and cierre > apertura and 52 < rsi < 68:
            return "UP", f"Impulso alcista limpio (RSI {rsi:.1f}).", "78%", False
        
        if ema9 < ema21 and cierre < apertura and 32 < rsi < 48:
            return "DOWN", f"Impulso bajista limpio (RSI {rsi:.1f}).", "78%", False

        return "SKIP", f"Condición mixta (RSI {rsi:.1f}).", "Neutral", False

    def cargar_backtest_inicial(self, df):
        for i in range(22, len(df) - 1):
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
        try:
            print("Descargando velas de Binance...")
            velas = self.exchange.fetch_ohlcv(self.symbol, self.timeframe, limit=75)
            df = pd.DataFrame(velas, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
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
                accion_txt = "⚪ NO APOSTAR (ESPERAR)"
                color_hero = "gray"
                estado_txt = "🛡️ PROTEGIENDO CAPITAL"
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
                "titulo": f"🎯 PnL Estricto ($1/trade): {'+' if pnl_estricto>=0 else ''}${pnl_estricto:.2f} USDT",
                "desc": "Extremos con confirmación de mecha.",
                "confianza": f"W:{self.w_est} / L:{self.l_est}",
                "color": "green" if pnl_estricto > 0 else ("red" if pnl_estricto < 0 else "gray")
            }

            self.s_dinamica = {
                "titulo": f"⚡ PnL Filtrado ($1/trade): {'+' if pnl_dinamico>=0 else ''}${pnl_dinamico:.2f} USDT",
                "desc": "Predicciones de 5m con filtros inteligentes.",
                "riesgo": f"W:{self.w_din} / L:{self.l_din}",
                "color": "green" if pnl_dinamico > 0 else ("red" if pnl_dinamico < 0 else "gray")
            }
            print("Datos actualizados correctamente.")
        except Exception as e:
            print("ERROR EN LOOP DE MERCADO:", str(e))

bot = ScalpingWebEngine()

def loop_mercado():
    while True:
        bot.actualizar_datos()
        time.sleep(3)

HTML_UI = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <title>BTC 5M - Binance Predict Pro V3</title>
    <style>
        * { box-sizing: border-box; font-family: 'Segoe UI', Roboto, Helvetica, sans-serif; }
        body { background-color: #090b10; color: #f0f6fc; margin: 0; padding: 20px; display: flex; justify-content: center; }
        .main-panel { width: 100%; max-width: 1120px; background-color: #0f1218; border: 1px solid #21262d; border-radius: 14px; padding: 22px; box-shadow: 0 12px 35px rgba(0,0,0,0.8); }
        .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1f242c; padding-bottom: 14px; margin-bottom: 18px; }
        .title { color: #f59e0b; font-size: 20px; font-weight: 800; display: flex; align-items: center; gap: 8px; }
        .version-tag { background: #10b981; color: #000; font-size: 11px; padding: 2px 7px; border-radius: 5px; font-weight: 800; }
        .badge-online { background-color: rgba(16, 185, 129, 0.1); border: 1px solid #10b981; color: #10b981; padding: 6px 14px; border-radius: 8px; font-size: 13px; font-weight: 600; display: flex; align-items: center; gap: 7px; }
        .dot { width: 9px; height: 9px; background-color: #10b981; border-radius: 50%; display: inline-block; box-shadow: 0 0 8px #10b981; }

        .predict-hero { border-radius: 12px; padding: 18px 22px; margin-bottom: 18px; display: flex; justify-content: space-between; align-items: center; border: 2px solid #30363d; background: #141821; }
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

        table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
        th { background-color: #1b202b; color: #8b949e; font-weight: 600; text-align: left; padding: 8px 9px; border-radius: 4px; }
        td { padding: 8px 9px; border-bottom: 1px solid #1f242c; color: #e5e7eb; }
        .pending-row { background-color: rgba(245, 158, 11, 0.12); color: #fbbf24; font-weight: 700; }
    </style>
</head>
<body>
    <div class="main-panel">
        <div class="header">
            <div class="title">⚡ BTC 5M - Binance Predict Pro <span class="version-tag">V3.1 DEBUG</span></div>
            <div class="badge-online"><span class="dot"></span> En línea</div>
        </div>

        <div class="predict-hero" id="hero-predict">
            <div>
                <div class="predict-label">🎯 SEÑAL FILTRADA BINANCE PREDICT</div>
                <div class="predict-action" id="pred-accion">CARGANDO DATOS...</div>
                <div class="predict-reason" id="pred-razon">Obteniendo velas desde Binance...</div>
            </div>
            <div class="predict-stats">
                <div class="stat-line">Apertura: <b id="pred-apertura">$0.00</b></div>
                <div class="stat-line">Diferencia: <b id="pred-dif">$0.00</b></div>
                <div class="live-status" id="pred-estado">⏳ Conectando...</div>
            </div>
        </div>

        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-label">Precio BTC</div>
                <div class="metric-val" id="precio">$0.00</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">RSI (14)</div>
                <div class="metric-val" id="rsi">0.0</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Tendencia</div>
                <div class="metric-trend" id="tendencia">Cargando...</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Vela 5M (Restante)</div>
                <div class="metric-timer" id="reloj">00:00</div>
            </div>
        </div>

        <div class="signal-box gray" id="box-estricto">
            <div>
                <div class="signal-title" id="est-titulo">Cargando...</div>
                <div class="signal-desc" id="est-desc">Esperando ciclo de red...</div>
            </div>
            <div class="pill" id="est-conf">W:0 / L:0</div>
        </div>

        <div class="signal-box gray" id="box-dinamico">
            <div>
                <div class="signal-title" id="din-titulo">Cargando...</div>
                <div class="signal-desc" id="din-desc">Esperando ciclo de red...</div>
            </div>
            <div class="pill" id="din-riesgo">W:0 / L:0</div>
        </div>
    </div>

    <script>
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
            } catch (e) { console.error(e); }
        }
        setInterval(obtenerEstado, 2000);
        obtenerEstado();
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    resp = make_response(render_template_string(HTML_UI))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp

@app.route('/api/status')
def status():
    resp = make_response(jsonify({
        "precio": bot.precio,
        "rsi": bot.rsi,
        "tendencia": bot.tendencia,
        "prediccion_5m": bot.prediccion_5m,
        "s_estricta": bot.s_estricta,
        "s_dinamica": bot.s_dinamica,
        "vela_en_curso": bot.prediccion_vela_actual
    }))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    return resp

if __name__ == '__main__':
    hilo = threading.Thread(target=loop_mercado, daemon=True)
    hilo.start()
    app.run(host='127.0.0.1', port=5000, debug=False)
