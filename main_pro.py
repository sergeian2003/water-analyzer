import webview
import threading
import uvicorn
import minimalmodbus
import struct
import time
import os
import csv
import json
import signal
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

PORT = '/dev/ttyUSB0'
LOG_FILE = 'water_logs.csv'
CONFIG_FILE = 'config.json'
app = FastAPI()
modbus_lock = threading.Lock()

# --- Config & Rules Storage ---
DEFAULT_CONFIG = {
    "ids": {"relay": 8, "mlss": 15, "uv254": 16, "orp": 17, "oil": 18, "do1": 19, "do2": 20},
    "rules": [{"sensor": "do1", "condition": "<", "threshold": 4.0, "action": "Pump 1", "enabled": False}]
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    return DEFAULT_CONFIG

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f: json.dump(cfg, f)

config = load_config()

# --- Data Engine ---
sensor_data = {
    "mlss": {"val": "--"}, 
    "uv254": {"cod": "--", "temp": "--", "turb": "--"},
    "orp": {"val": "--"}, "oil": {"val": "--"}, 
    "do1": {"val": "--"}, "do2": {"val": "--"}, 
    "status": "Online",
    "history": { "mlss": [0]*30, "uv254": [0]*30, "orp": [0]*30, "oil": [0]*30, "do1": [0]*30, "do2": [0]*30 }
}
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
        try: return func(*args)
        except:
            time.sleep(0.1)
            if attempt == retries - 1: raise

def modbus_worker():
    global sensor_data
    last_log_time = time.time()
    
    while True:
        ids = config["ids"]
        
        # 1. MLSS
        try:
            with modbus_lock:
                instr = create_instrument(ids["mlss"])
                v = read_with_retry(instr.read_float, 6, 3, 2)
            sensor_data["mlss"]["val"] = f"{v:.2f}"
            sensor_data["history"]["mlss"].pop(0); sensor_data["history"]["mlss"].append(v)
        except: sensor_data["mlss"]["val"] = "Err"

        # 2. UV254
        try:
            with modbus_lock:
                instr = create_instrument(ids["uv254"])
                read_with_retry(instr.read_register, 12288, 0, 3)
                t_r = read_with_retry(instr.read_registers, 9728, 2, 3)
                c_r = read_with_retry(instr.read_registers, 9730, 2, 3)
                tr_r = read_with_retry(instr.read_registers, 4608, 2, 3)
            sensor_data["uv254"]["temp"] = f"{decode_dcba(t_r):.1f}"
            sensor_data["uv254"]["cod"] = f"{decode_dcba(c_r):.2f}"
            sensor_data["uv254"]["turb"] = f"{decode_dcba(tr_r):.2f}"
            sensor_data["history"]["uv254"].pop(0); sensor_data["history"]["uv254"].append(decode_dcba(c_r))
        except: 
            sensor_data["uv254"]["cod"] = "Err"
            sensor_data["uv254"]["temp"] = "Err"
            sensor_data["uv254"]["turb"] = "Err"

        # 3. ORP
        try:
            with modbus_lock:
                instr = create_instrument(ids["orp"])
                read_with_retry(instr.read_register, 12288, 0, 3)
                val = decode_dcba(read_with_retry(instr.read_registers, 9730, 2, 3))
            sensor_data["orp"]["val"] = f"{val:.1f}"
            sensor_data["history"]["orp"].pop(0); sensor_data["history"]["orp"].append(val)
        except: sensor_data["orp"]["val"] = "Err"

        # 4. OIL
        try:
            with modbus_lock:
                instr = create_instrument(ids["oil"])
                read_with_retry(instr.read_register, 12288, 0, 3)
                val = decode_dcba(read_with_retry(instr.read_registers, 9730, 2, 3))
            sensor_data["oil"]["val"] = f"{val:.2f}"
            sensor_data["history"]["oil"].pop(0); sensor_data["history"]["oil"].append(val)
        except: sensor_data["oil"]["val"] = "Err"

        # 5. DO 1
        try:
            with modbus_lock:
                instr = create_instrument(ids["do1"])
                read_with_retry(instr.read_register, 12288, 0, 3)
                val = decode_dcba(read_with_retry(instr.read_registers, 9730, 2, 3))
            sensor_data["do1"]["val"] = f"{val:.2f}"
            sensor_data["history"]["do1"].pop(0); sensor_data["history"]["do1"].append(val)
        except: sensor_data["do1"]["val"] = "Err"
        
        # 6. DO 2
        try:
            with modbus_lock:
                instr = create_instrument(ids["do2"])
                read_with_retry(instr.read_register, 12288, 0, 3)
                val = decode_dcba(read_with_retry(instr.read_registers, 9730, 2, 3))
            sensor_data["do2"]["val"] = f"{val:.2f}"
            sensor_data["history"]["do2"].pop(0); sensor_data["history"]["do2"].append(val)
        except: sensor_data["do2"]["val"] = "Err"

        # Logging
        if time.time() - last_log_time >= 60:
            try:
                with open(LOG_FILE, 'a', newline='') as f:
                    csv.writer(f).writerow([datetime.now().strftime("%H:%M:%S"), 
                        sensor_data["mlss"]["val"], sensor_data["uv254"]["cod"], 
                        sensor_data["orp"]["val"], sensor_data["oil"]["val"], 
                        sensor_data["do1"]["val"], sensor_data["do2"]["val"]])
                last_log_time = time.time()
            except: pass
            
        time.sleep(0.5)

# --- API ---
@app.get("/api/all")
def get_all(): 
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f: 
            all_rows = [row for row in csv.reader(f) if row and row[0] != "Timestamp"]
            logs = all_rows[-20:]
            logs.reverse()
    return {"data": sensor_data, "relays": relay_states, "config": config, "logs": logs}

@app.post("/api/save_config")
async def update_cfg(request: Request):
    global config
    config = await request.json()
    save_config(config)
    return {"status": "ok"}

@app.get("/api/relay")
def toggle_relay(ch: int, state: int):
    relay_states[ch] = state
    # Здесь мы физически отправляем команду на реле KM6073
    try:
        with modbus_lock:
            instr = create_instrument(config["ids"]["relay"])
            instr.write_bit(ch, state, 5)
    except: pass
    return {"status": "ok"}

@app.get("/api/exit")
def exit_app():
    os.kill(os.getpid(), signal.SIGINT)
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def get_gui():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>Smart Farm Pro</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { 
                background: #020617; color: #f8fafc; font-family: 'Inter', sans-serif; overflow: hidden; 
                -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale; text-rendering: optimizeLegibility;
            }
            .nav-btn.active { border-bottom: 2px solid #22d3ee; color: #22d3ee; }
            .card { background: #0f172a; border: 1px solid #1e293b; border-radius: 8px; transition: all 0.2s; }
            .sensor-card { cursor: pointer; }
            .sensor-card:hover { border-color: #475569; }
            .sensor-card.active-chart { border-color: #22d3ee; background: rgba(34, 211, 238, 0.05); }
            
            /* Custom Toggle Switch Styles */
            .toggle-bg { transition: background-color 0.3s ease; }
            .toggle-dot { transition: transform 0.3s ease; }
            input:checked ~ .toggle-bg { background-color: #06b6d4; border-color: #06b6d4; }
            input:checked ~ .toggle-bg .toggle-dot { transform: translateX(100%); background-color: white; }
        </style>
    </head>
    <body class="h-screen flex flex-col">
        <nav class="flex gap-8 px-8 py-4 bg-slate-900/50 border-b border-white/5">
            <button onclick="showTab('dash')" id="btn-dash" class="nav-btn active text-sm font-black uppercase">Dashboard</button>
            <button onclick="showTab('logs')" id="btn-logs" class="nav-btn text-sm font-black uppercase">History Logs</button>
            <button onclick="showTab('ctrl')" id="btn-ctrl" class="nav-btn text-sm font-black uppercase">Manual Control</button>
            <button onclick="showTab('eng')" id="btn-eng" class="nav-btn text-sm font-black uppercase">Engineering</button>
            <div class="flex-grow"></div>
            <button onclick="fetch('/api/exit')" class="text-rose-500 text-sm font-bold bg-rose-950/30 px-4 py-1 rounded">EXIT SYSTEM</button>
        </nav>

        <main id="tab-dash" class="p-6 flex flex-col gap-4 flex-grow">
            <div class="grid grid-cols-3 gap-4">
                <div id="card-mlss" onclick="setChartMode('mlss')" class="card sensor-card p-4 flex flex-col justify-between">
                    <p class="text-xs font-bold text-slate-500 uppercase">MLSS</p>
                    <div class="flex items-baseline gap-1 mt-2">
                        <p id="v-mlss" class="text-3xl font-black">--</p>
                        <span class="text-xs text-slate-500 font-bold">mg/L</span>
                    </div>
                </div>
                
                <div id="card-uv254" onclick="setChartMode('uv254')" class="card sensor-card p-4 flex flex-col justify-between">
                    <div class="flex justify-between items-start">
                        <p class="text-xs font-bold text-blue-500 uppercase">UV254 (COD)</p>
                        <div class="text-[11px] text-slate-400 text-right leading-snug">
                            T: <span id="v-uv-t" class="text-slate-100 font-bold">--</span> °C<br>
                            Tb: <span id="v-uv-tr" class="text-slate-100 font-bold">--</span> NTU
                        </div>
                    </div>
                    <div class="flex items-baseline gap-1 mt-1">
                        <p id="v-uv254" class="text-3xl font-black">--</p>
                        <span class="text-xs text-slate-500 font-bold">mg/L</span>
                    </div>
                </div>

                <div id="card-orp" onclick="setChartMode('orp')" class="card sensor-card p-4 flex flex-col justify-between">
                    <p class="text-xs font-bold text-purple-500 uppercase">ORP</p>
                    <div class="flex items-baseline gap-1 mt-2">
                        <p id="v-orp" class="text-3xl font-black">--</p>
                        <span class="text-xs text-slate-500 font-bold">mV</span>
                    </div>
                </div>
                
                <div id="card-oil" onclick="setChartMode('oil')" class="card sensor-card p-4 flex flex-col justify-between">
                    <p class="text-xs font-bold text-amber-500 uppercase">OIL IN WATER</p>
                    <div class="flex items-baseline gap-1 mt-2">
                        <p id="v-oil" class="text-3xl font-black">--</p>
                        <span class="text-xs text-slate-500 font-bold">ug/L</span>
                    </div>
                </div>
                
                <div id="card-do1" onclick="setChartMode('do1')" class="card sensor-card p-4 border-cyan-900/50 bg-cyan-950/20 flex flex-col justify-between">
                    <p class="text-xs font-bold text-cyan-400 uppercase">DO 1 (Inlet)</p>
                    <div class="flex items-baseline gap-1 mt-2">
                        <p id="v-do1" class="text-3xl font-black text-white">--</p>
                        <span class="text-xs text-cyan-700 font-bold">mg/L</span>
                    </div>
                </div>
                
                <div id="card-do2" onclick="setChartMode('do2')" class="card sensor-card p-4 border-teal-900/50 bg-teal-950/20 flex flex-col justify-between">
                    <p class="text-xs font-bold text-teal-400 uppercase">DO 2 (Outlet)</p>
                    <div class="flex items-baseline gap-1 mt-2">
                        <p id="v-do2" class="text-3xl font-black text-white">--</p>
                        <span class="text-xs text-teal-700 font-bold">mg/L</span>
                    </div>
                </div>
            </div>
            
            <div class="card flex-grow p-4 flex flex-col min-h-[200px]">
                <div class="flex justify-between items-center mb-1">
                    <span id="chart-title" class="text-xs font-bold text-slate-500 uppercase tracking-widest">MULTI-TREND ANALYSIS</span>
                    <button onclick="setChartMode('all')" class="bg-slate-800 text-cyan-400 text-[10px] px-3 py-1 rounded font-bold hover:bg-slate-700 transition-colors shadow-lg">SHOW ALL LINES</button>
                </div>
                <div class="relative flex-grow">
                    <canvas id="proChart"></canvas>
                </div>
            </div>
        </main>

        <main id="tab-logs" class="p-6 hidden flex-grow overflow-auto">
            <div class="card overflow-hidden">
                <table class="w-full text-left text-xs">
                    <thead class="bg-slate-800 text-slate-400"><tr><th class="p-4">Time</th><th>MLSS</th><th>COD</th><th>ORP</th><th>OIL</th><th>DO 1</th><th>DO 2</th></tr></thead>
                    <tbody id="log-table"></tbody>
                </table>
            </div>
        </main>

        <main id="tab-ctrl" class="p-6 hidden flex-grow flex items-center justify-center">
            <div class="card p-8 w-full max-w-3xl border-cyan-900/30">
                <h2 class="text-cyan-400 font-black text-xl mb-6 uppercase border-b border-slate-700 pb-4">Manual Relay Override</h2>
                <div class="grid grid-cols-2 gap-8">
                    <script>
                        const relayNames = ['Pump 1 (Inlet)', 'Pump 2 (Outlet)', 'Aerator', 'Drain Valve'];
                        relayNames.forEach((name, i) => {
                            document.write(`
                            <div class="flex items-center justify-between bg-slate-800/40 p-5 rounded-xl border border-slate-700/50">
                                <span class="text-sm font-bold text-slate-300 uppercase">${name}</span>
                                <label class="relative inline-flex items-center cursor-pointer">
                                    <input type="checkbox" id="relay-${i}" onchange="fetch('/api/relay?ch=${i}&state='+(this.checked?1:0))" class="sr-only peer">
                                    <div class="w-14 h-7 bg-slate-900 rounded-full border border-slate-600 peer-checked:bg-cyan-500 peer-checked:border-cyan-500 transition-colors after:content-[''] after:absolute after:top-[3px] after:left-[3px] after:bg-slate-400 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-[28px] peer-checked:after:bg-white"></div>
                                </label>
                            </div>
                            `);
                        });
                    </script>
                </div>
            </div>
        </main>

        <main id="tab-eng" class="p-6 hidden flex-grow">
             <div class="grid grid-cols-2 gap-6 h-full">
                <div class="card p-6 flex flex-col gap-4">
                    <div class="flex justify-between items-end border-b border-slate-700 pb-2">
                        <h2 class="text-cyan-400 font-black text-sm uppercase">Hardware ID Config</h2>
                        <span class="text-[10px] text-slate-500 font-bold uppercase">Bus Network</span>
                    </div>
                    <div id="id-inputs" class="grid grid-cols-2 gap-4 mt-2"></div>
                    <button onclick="saveEngineering()" class="mt-auto bg-cyan-600 p-3 rounded text-sm font-bold shadow-lg hover:bg-cyan-500">SAVE & APPLY</button>
                </div>
                <div class="card p-6 flex flex-col gap-4">
                    <div class="border-b border-slate-700 pb-2">
                        <h2 class="text-amber-500 font-black text-sm uppercase">Automation Logic (BETA)</h2>
                    </div>
                    <div class="bg-black/20 p-5 rounded border border-white/5 space-y-4">
                        <div class="flex items-center justify-between text-sm">
                            <span>Condition: IF <b>DO 1 Level</b> &lt; <b>4.0 mg/L</b></span>
                            <span class="text-slate-500 italic">Target: Pump 1 (ON)</span>
                        </div>
                        <div class="flex items-center gap-2 mt-4">
                            <input type="checkbox" disabled class="h-5 w-5 rounded border-slate-600 bg-slate-800">
                            <span class="text-xs text-slate-500 font-bold">Enable Auto-Control (Requires testing)</span>
                        </div>
                    </div>
                    <p class="text-xs text-slate-600 mt-auto font-medium">* Automatic logic is disabled until additional terminal blocks are installed.</p>
                </div>
            </div>
        </main>

        <script>
            let currentTab = 'dash';
            let currentChartMode = 'all';
            
            const ctx = document.getElementById('proChart').getContext('2d');
            const chart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: Array(30).fill(''),
                    datasets: [
                        { id: 'mlss', label: 'MLSS', data: [], borderColor: '#94a3b8', borderWidth: 2, pointRadius: 0 },
                        { id: 'uv254', label: 'COD', data: [], borderColor: '#3b82f6', borderWidth: 2, pointRadius: 0 },
                        { id: 'orp', label: 'ORP', data: [], borderColor: '#a855f7', borderWidth: 2, pointRadius: 0 },
                        { id: 'oil', label: 'OIL', data: [], borderColor: '#f59e0b', borderWidth: 2, pointRadius: 0 },
                        { id: 'do1', label: 'DO 1', data: [], borderColor: '#06b6d4', borderWidth: 2, pointRadius: 0 },
                        { id: 'do2', label: 'DO 2', data: [], borderColor: '#2dd4bf', borderWidth: 2, pointRadius: 0 }
                    ]
                },
                options: { 
                    responsive: true, 
                    maintainAspectRatio: false, 
                    scales: { 
                        x: { display: false },
                        y: { grid: { color: 'rgba(255,255,255,0.05)' } }
                    },
                    plugins: { 
                        legend: { 
                            display: true, 
                            position: 'top',
                            align: 'end',
                            labels: { color: '#cbd5e1', font: { size: 11, family: 'Inter' }, boxWidth: 12 } 
                        } 
                    }
                }
            });

            function showTab(tab) {
                document.querySelectorAll('main').forEach(m => m.classList.add('hidden'));
                document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
                document.getElementById('tab-' + tab).classList.remove('hidden');
                document.getElementById('btn-' + tab).classList.add('active');
                currentTab = tab;
            }

            function setChartMode(mode) {
                currentChartMode = mode;
                
                document.querySelectorAll('.sensor-card').forEach(c => c.classList.remove('active-chart'));
                
                if (mode === 'all') {
                    document.getElementById('chart-title').innerText = 'MULTI-TREND ANALYSIS';
                    chart.options.plugins.legend.display = true;
                    chart.data.datasets.forEach(ds => ds.hidden = false);
                } else {
                    document.getElementById('chart-title').innerText = mode.toUpperCase() + ' ISOLATED TREND';
                    document.getElementById('card-' + mode).classList.add('active-chart');
                    chart.options.plugins.legend.display = false;
                    chart.data.datasets.forEach(ds => {
                        ds.hidden = (ds.id !== mode);
                    });
                }
                chart.update('none');
            }

            async function update() {
                try {
                    const res = await fetch('/api/all');
                    const d = await res.json();
                    
                    if(currentTab === 'dash') {
                        document.getElementById('v-mlss').innerText = d.data.mlss.val;
                        document.getElementById('v-uv254').innerText = d.data.uv254.cod;
                        document.getElementById('v-uv-t').innerText = d.data.uv254.temp;
                        document.getElementById('v-uv-tr').innerText = d.data.uv254.turb;
                        document.getElementById('v-orp').innerText = d.data.orp.val;
                        document.getElementById('v-oil').innerText = d.data.oil.val;
                        document.getElementById('v-do1').innerText = d.data.do1.val;
                        document.getElementById('v-do2').innerText = d.data.do2.val;
                        
                        chart.data.datasets[0].data = d.data.history.mlss;
                        chart.data.datasets[1].data = d.data.history.uv254;
                        chart.data.datasets[2].data = d.data.history.orp;
                        chart.data.datasets[3].data = d.data.history.oil;
                        chart.data.datasets[4].data = d.data.history.do1;
                        chart.data.datasets[5].data = d.data.history.do2;
                        chart.update('none');
                    }

                    if(currentTab === 'logs') {
                        const tbody = document.getElementById('log-table');
                        tbody.innerHTML = d.logs.map(row => {
                            while(row.length < 7) row.push('--');
                            return `<tr class="border-b border-white/5 hover:bg-white/5"><td class="p-3">${row.join('</td><td class="p-3">')}</td></tr>`;
                        }).join('');
                    }

                    // Обновляем состояния тумблеров в Manual Control
                    if(currentTab === 'ctrl') {
                        for(let i=0; i<4; i++) {
                            const toggle = document.getElementById('relay-' + i);
                            if(toggle && document.activeElement !== toggle) {
                                toggle.checked = (d.relays[i] === 1);
                            }
                        }
                    }

                    if(currentTab === 'eng' && !document.getElementById('id-inputs').innerHTML) {
                        let html = '';
                        for(const [name, id] of Object.entries(d.config.ids)) {
                            // Компактный дизайн для сетки
                            html += `<div class="flex justify-between items-center text-xs bg-slate-800/40 p-2.5 rounded border border-slate-700/50">
                                <span class="uppercase font-bold text-slate-400">${name}</span>
                                <input id="id-${name}" type="number" value="${id}" class="bg-black border border-slate-600 w-14 text-center rounded p-1 text-white font-bold">
                            </div>`;
                        }
                        document.getElementById('id-inputs').innerHTML = html;
                    }
                } catch (e) {}
            }

            setInterval(update, 1000);
            setChartMode('all');
        </script>
    </body>
    </html>
    """

def run_api(): uvicorn.run(app, host="127.0.0.1", port=5000, log_level="critical")

if __name__ == "__main__":
    threading.Thread(target=modbus_worker, daemon=True).start()
    threading.Thread(target=run_api, daemon=True).start()
    time.sleep(1)
    webview.create_window("WATER ANALYZER PRO", "http://127.0.0.1:5000", fullscreen=True)
    webview.start()
