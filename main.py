import webview
import threading
import uvicorn
import minimalmodbus
import struct
import time
import os
import csv
from datetime import datetime
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

PORT = '/dev/ttyUSB0'
LOG_FILE = 'logs.csv'
app = FastAPI()

# RS485 Bus Mutex Lock
modbus_lock = threading.Lock()

# Initialize log file with headers
if not os.path.exists(LOG_FILE):
    with open(LOG_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["Timestamp", "MLSS", "UV_COD", "ORP", "OIL", "DO"])

# Global data storage
sensor_data = {
    "mlss_val": "--", "mlss_temp": "--",
    "uv_cod": "--", "uv_temp": "--", "uv_turb": "--",
    "orp_val": "--",
    "oil_val": "--", "oil_temp": "--",
    "do_val": "--", "do_temp": "--",
    "status": "Initializing...",
    "chart_data": [0] * 30
}

# Relay states (for UI sync)
relay_states = [0, 0, 0, 0]

def decode_dcba(registers):
    packed = struct.pack('>HH', registers[0], registers[1])
    return struct.unpack('<f', packed)[0]

def create_instrument(sensor_id):
    instr = minimalmodbus.Instrument(PORT, sensor_id)
    instr.serial.baudrate = 9600
    instr.serial.timeout = 0.5
    instr.clear_buffers_before_each_transaction = True 
    return instr

def read_with_retry(func, *args, retries=2):
    for attempt in range(retries):
        try:
            return func(*args)
        except Exception:
            time.sleep(0.1)
            if attempt == retries - 1:
                raise

def modbus_worker():
    global sensor_data
    last_log_time = time.time()
    
    while True:
        sensor_data["status"] = "Polling sensors..."
        
        # 1. MLSS
        try:
            with modbus_lock:
                instr = create_instrument(15)
                temp = read_with_retry(instr.read_float, 4, 3, 2)
                val = read_with_retry(instr.read_float, 6, 3, 2)
            sensor_data["mlss_temp"] = f"{temp:.1f}"
            sensor_data["mlss_val"] = f"{val:.2f}"
        except:
            sensor_data["mlss_val"] = "Error"
        time.sleep(0.1)

        # 2. UV254
        try:
            with modbus_lock:
                instr = create_instrument(16)
                read_with_retry(instr.read_register, 12288, 0, 3)
                t_regs = read_with_retry(instr.read_registers, 9728, 2, 3)
                c_regs = read_with_retry(instr.read_registers, 9730, 2, 3)
                turb_regs = read_with_retry(instr.read_registers, 4608, 2, 3)
            sensor_data["uv_temp"] = f"{decode_dcba(t_regs):.1f}"
            sensor_data["uv_cod"] = f"{decode_dcba(c_regs):.2f}"
            sensor_data["uv_turb"] = f"{decode_dcba(turb_regs):.2f}"
        except:
            sensor_data["uv_cod"] = "Error"
        time.sleep(0.1)

        # 3. ORP
        try:
            with modbus_lock:
                instr = create_instrument(17)
                read_with_retry(instr.read_register, 12288, 0, 3)
                regs = read_with_retry(instr.read_registers, 9730, 2, 3)
            sensor_data["orp_val"] = f"{decode_dcba(regs):.1f}"
        except:
            sensor_data["orp_val"] = "Error"
        time.sleep(0.1)

        # 4. OIL
        try:
            with modbus_lock:
                instr = create_instrument(18)
                read_with_retry(instr.read_register, 12288, 0, 3)
                t_regs = read_with_retry(instr.read_registers, 9728, 2, 3)
                v_regs = read_with_retry(instr.read_registers, 9730, 2, 3)
            sensor_data["oil_temp"] = f"{decode_dcba(t_regs):.1f}"
            sensor_data["oil_val"] = f"{decode_dcba(v_regs):.2f}"
        except:
            sensor_data["oil_val"] = "Error"
        time.sleep(0.1)

        # 5. DO
        try:
            with modbus_lock:
                instr = create_instrument(19)
                read_with_retry(instr.read_register, 12288, 0, 3)
                t_regs = read_with_retry(instr.read_registers, 9728, 2, 3)
                v_regs = read_with_retry(instr.read_registers, 9730, 2, 3)
            
            do_value = decode_dcba(v_regs)
            sensor_data["do_temp"] = f"{decode_dcba(t_regs):.1f}"
            sensor_data["do_val"] = f"{do_value:.2f}"
            sensor_data["chart_data"].pop(0)
            sensor_data["chart_data"].append(float(f"{do_value:.2f}"))
        except:
            sensor_data["do_val"] = "Error"
            
        sensor_data["status"] = "System Stable"
        
        # DATALOGGING (Every 60 sec)
        current_time = time.time()
        if current_time - last_log_time >= 60:
            try:
                with open(LOG_FILE, 'a', newline='') as f:
                    csv.writer(f).writerow([
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        sensor_data["mlss_val"], sensor_data["uv_cod"],
                        sensor_data["orp_val"], sensor_data["oil_val"], sensor_data["do_val"]
                    ])
                last_log_time = current_time
            except: pass

        time.sleep(0.5)

# --- API ENDPOINTS ---
@app.get("/api/data")
def get_data():
    return {"sensors": sensor_data, "relays": relay_states}

@app.get("/api/relay")
def toggle_relay(ch: int, state: int):
    global relay_states
    try:
        with modbus_lock:
            instr = create_instrument(8) # KM6073 ID
            instr.write_bit(ch, state, functioncode=5)
        relay_states[ch] = state
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/exit")
def exit_app():
    os._exit(0)

@app.get("/logs", response_class=HTMLResponse)
def get_logs():
    rows = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f: rows = list(csv.reader(f))
    data = rows[1:]
    data.reverse()
    data = data[:50]
    table_html = "".join([f"<tr class='border-b border-slate-700/50 hover:bg-slate-800/50'><td class='py-3 px-4 font-mono text-cyan-400'>{'</td><td class=py-3 px-4>'.join(row)}</td></tr>" for row in data])

    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Logs - Water Control Pro</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <style>body {{ background: #0f172a; color: #f8fafc; font-family: sans-serif; height: 100vh; overflow: hidden; }} ::-webkit-scrollbar {{ display: none; }}</style>
    </head>
    <body class="p-6 flex flex-col h-full">
        <header class="flex justify-between items-center mb-6">
            <h1 class="text-2xl font-bold text-cyan-400">DATA LOGS</h1>
            <button onclick="window.location.href='/'" class="px-5 py-2.5 bg-slate-800 rounded-lg font-bold">⬅ BACK</button>
        </header>
        <div class="flex-grow overflow-auto bg-slate-900/50 rounded-xl p-1">
            <table class="w-full text-left text-sm"><thead class="text-gray-400 bg-slate-800"><tr><th class="py-4 px-4">Time</th><th class="py-4 px-4">MLSS</th><th class="py-4 px-4">UV (COD)</th><th class="py-4 px-4">ORP</th><th class="py-4 px-4">OIL</th><th class="py-4 px-4">DO</th></tr></thead><tbody>{table_html}</tbody></table>
        </div>
    </body>
    </html>
    """

@app.get("/", response_class=HTMLResponse)
def get_dashboard():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Water Control Pro</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            ::-webkit-scrollbar { display: none; }
            body { background: #0f172a; color: #f8fafc; height: 100vh; overflow: hidden; font-family: 'Inter', sans-serif; }
            .glass-card { background: rgba(30, 41, 59, 0.7); backdrop-filter: blur(10px); border: 1px solid rgba(255, 255, 255, 0.05); border-radius: 1rem; padding: 1.5rem; }
            .value-text { font-size: 3rem; font-weight: 800; line-height: 1; margin-top: 0.5rem; margin-bottom: 0.5rem; }
            .toggle-checkbox:checked { right: 0; border-color: #06b6d4; }
            .toggle-checkbox:checked + .toggle-label { background-color: #06b6d4; }
        </style>
    </head>
    <body class="p-6 flex flex-col gap-4">
        
        <header class="flex justify-between items-center mb-2">
            <div>
                <h1 class="text-2xl font-bold text-transparent bg-clip-text bg-gradient-to-r from-cyan-400 to-blue-500">SMART FARM WATER MONITORING</h1>
                <p id="status-text" class="text-xs text-gray-400 mt-1">Starting system...</p>
            </div>
            <div class="flex items-center gap-4">
                <button onclick="window.location.href='/logs'" class="px-4 py-2 bg-slate-800 rounded-lg text-sm font-bold text-blue-300">LOGS</button>
                <button onclick="fetch('/api/exit')" class="px-4 py-2 bg-rose-900/50 rounded-lg text-sm font-bold text-rose-300">✕ EXIT</button>
            </div>
        </header>

        <div class="grid grid-cols-3 gap-4">
            <div class="glass-card flex flex-col justify-between">
                <div class="text-gray-400 text-xs font-semibold">MLSS (Suspended Solids)</div>
                <div class="flex items-baseline gap-2"><span id="mlss-val" class="value-text text-white">--</span><span class="text-gray-500 font-bold">mg/L</span></div>
            </div>
            <div class="glass-card flex flex-col justify-between">
                <div class="text-blue-400 text-xs font-semibold">UV254 (COD)</div>
                <div class="flex items-baseline gap-2"><span id="uv-cod" class="value-text text-white">--</span><span class="text-gray-500 font-bold">mg/L</span></div>
            </div>
            <div class="glass-card flex flex-col justify-between">
                <div class="text-purple-400 text-xs font-semibold">ORP (Oxidation)</div>
                <div class="flex items-baseline gap-2"><span id="orp-val" class="value-text text-white">--</span><span class="text-gray-500 font-bold">mV</span></div>
            </div>
        </div>

        <div class="grid grid-cols-3 gap-4">
            <div class="glass-card flex flex-col justify-between">
                <div class="text-amber-400 text-xs font-semibold">OIL IN WATER</div>
                <div class="flex items-baseline gap-2"><span id="oil-val" class="value-text text-white">--</span><span class="text-gray-500 font-bold">ug/L</span></div>
            </div>
            <div class="glass-card flex flex-col justify-between border-cyan-900/50 bg-cyan-950/20">
                <div class="text-cyan-400 text-xs font-semibold">DO (Dissolved Oxygen)</div>
                <div class="flex items-baseline gap-2"><span id="do-val" class="value-text text-white">--</span><span class="text-gray-500 font-bold">mg/L</span></div>
            </div>
            
            <div class="glass-card flex flex-col border-rose-900/30">
                <div class="text-rose-400 text-xs font-semibold mb-4">RELAY CONTROL (KM6073)</div>
                <div class="grid grid-cols-2 gap-4 flex-grow">
                    <script>
                        const relayNames = ['Pump 1', 'Pump 2', 'Aerator', 'Valve'];
                        for(let i=0; i<4; i++) {
                            document.write(`
                            <div class="flex items-center justify-between bg-slate-800/50 p-3 rounded-lg border border-slate-700">
                                <span class="text-sm font-medium text-gray-300">${relayNames[i]}</span>
                                <div class="relative inline-block w-12 mr-2 align-middle select-none transition duration-200 ease-in">
                                    <input type="checkbox" name="toggle" id="relay-${i}" onchange="switchRelay(${i}, this.checked)" class="toggle-checkbox absolute block w-6 h-6 rounded-full bg-white border-4 border-slate-600 appearance-none cursor-pointer z-10"/>
                                    <label for="relay-${i}" class="toggle-label block overflow-hidden h-6 rounded-full bg-slate-600 cursor-pointer"></label>
                                </div>
                            </div>
                            `);
                        }
                    </script>
                </div>
            </div>
        </div>

        <div class="glass-card flex-grow relative flex flex-col">
            <div class="flex-grow relative w-full h-full"><canvas id="liveChart"></canvas></div>
        </div>

        <script>
            const ctx = document.getElementById('liveChart').getContext('2d');
            const gradient = ctx.createLinearGradient(0, 0, 0, 150);
            gradient.addColorStop(0, 'rgba(34, 211, 238, 0.4)'); 
            gradient.addColorStop(1, 'rgba(34, 211, 238, 0.0)');

            const chart = new Chart(ctx, {
                type: 'line',
                data: { labels: Array(30).fill(''), datasets: [{ data: Array(30).fill(0), borderColor: '#22d3ee', backgroundColor: gradient, borderWidth: 2, pointRadius: 0, fill: true, tension: 0.4 }] },
                options: { responsive: true, maintainAspectRatio: false, animation: { duration: 0 }, scales: { y: { display: false }, x: { display: false } }, plugins: { legend: { display: false } } }
            });

            async function switchRelay(channel, isChecked) {
                const state = isChecked ? 1 : 0;
                try { await fetch(`/api/relay?ch=${channel}&state=${state}`); } 
                catch (e) { console.error("Relay Error"); }
            }

            async function update() {
                try {
                    const res = await fetch('/api/data');
                    const data = await res.json();
                    
                    document.getElementById('mlss-val').innerText = data.sensors.mlss_val;
                    document.getElementById('uv-cod').innerText = data.sensors.uv_cod;
                    document.getElementById('orp-val').innerText = data.sensors.orp_val;
                    document.getElementById('oil-val').innerText = data.sensors.oil_val;
                    document.getElementById('do-val').innerText = data.sensors.do_val;
                    document.getElementById('status-text').innerText = data.sensors.status;

                    for(let i=0; i<4; i++) {
                        const toggle = document.getElementById(`relay-${i}`);
                        if (toggle && document.activeElement !== toggle) toggle.checked = data.relays[i] === 1;
                    }
                    chart.data.datasets[0].data = data.sensors.chart_data;
                    chart.update();
                } catch (e) { }
            }
            setInterval(update, 500);
        </script>
    </body>
    </html>
    """

def run_fastapi():
    uvicorn.run(app, host="127.0.0.1", port=5000, log_level="critical")

if __name__ == "__main__":
    threading.Thread(target=modbus_worker, daemon=True).start()
    threading.Thread(target=run_fastapi, daemon=True).start()
    time.sleep(1)
    
    webview.create_window(
        title="Smart Farm HMI",
        url="http://127.0.0.1:5000",
        width=1024,
        height=600,
        fullscreen=True,
        frameless=True
    )
    webview.start()
