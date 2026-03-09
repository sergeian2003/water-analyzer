import webview
import threading
import uvicorn
import minimalmodbus
import struct
import time
import os
import sys
import csv
import json
import signal
import shutil
import math
from datetime import datetime
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse

# === 시스템 절대 경로 고정 (바탕화면 실행 및 Kiosk 모드 방어) ===
BASE_DIR = '/home/young/farm'
LOG_DIR = os.path.join(BASE_DIR, 'logs')

# logs 폴더가 없으면 자동 생성
os.makedirs(LOG_DIR, exist_ok=True) 

PORT = '/dev/ttyUSB0'
LOG_5MIN = os.path.join(LOG_DIR, 'data_5min.csv')
LOG_1HR = os.path.join(LOG_DIR, 'data_1hr.csv')
LOG_ALARM = os.path.join(LOG_DIR, 'data_alarm.csv')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

app = FastAPI()
modbus_lock = threading.Lock()

# --- 동적(Dynamic) 설정 ---
DEFAULT_CONFIG = {
    "relay_id": 8,
    "ao_id": 9,
    "sensors": {
        "s_1": {"id": 15, "type": "mlss", "enabled": True, "label": "MLSS", "color": "#94a3b8", "min": 0, "max": 10000},
        "s_2": {"id": 16, "type": "uv254", "enabled": True, "label": "UV254 (COD)", "color": "#3b82f6", "min": 0, "max": 100}
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f: 
                cfg = json.load(f)
                if "ao_id" not in cfg: cfg["ao_id"] = 9
                for k, v in cfg.get("sensors", {}).items():
                    if "min" not in v: v["min"] = 0
                    if "max" not in v: v["max"] = 100
                return cfg
        except: return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f: json.dump(cfg, f)

config = load_config()

# --- 동적 데이터 엔진 ---
sensor_data = {}
history_data = {}
relay_states = [0, 0, 0, 0]

hourly_buffer = {}
alarm_states = {}

def init_data_structures():
    global sensor_data, history_data, hourly_buffer, alarm_states
    sensor_data = {"status": "Online"}
    history_data = {}
    hourly_buffer = {}
    alarm_states = {}
    
    for key, s in config.get("sensors", {}).items():
        if s["type"] == "uv254":
            sensor_data[key] = {"val": "--", "temp": "--", "turb": "--", "ao": "--", "status": "WAIT"}
        else:
            sensor_data[key] = {"val": "--", "ao": "--", "status": "WAIT"}
        history_data[key] = [None]*30
        hourly_buffer[key] = []
        alarm_states[key] = False

init_data_structures()

# --- Modbus Float Helper ---
def decode_dcba(registers):
    packed = struct.pack('>HH', registers[0], registers[1])
    return struct.unpack('<f', packed)[0]

def encode_dcba(val):
    packed = struct.pack('<f', float(val))
    return struct.unpack('>HH', packed)

def decode_abcd(registers):
    packed = struct.pack('>HH', registers[0], registers[1])
    return struct.unpack('>f', packed)[0]

def encode_abcd(val):
    packed = struct.pack('>f', float(val))
    return struct.unpack('>HH', packed)

def create_instrument(sensor_id):
    instr = minimalmodbus.Instrument(PORT, int(sensor_id))
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

def write_csv_log(filepath, headers, row):
    is_new = not os.path.exists(filepath)
    try:
        with open(filepath, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if is_new: writer.writerow(headers)
            writer.writerow(row)
    except: pass

# --- 메인 Modbus 루프 ---
def modbus_worker():
    global sensor_data, history_data, hourly_buffer, alarm_states
    
    last_5min_minute = -1
    last_1hr_hour = -1
    
    while True:
        now = datetime.now()
        active_keys = [k for k, v in config.get("sensors", {}).items() if v.get("enabled")]
        
        try:
            sensors_cfg = list(config.get("sensors", {}).items())
            
            for key, s in sensors_cfg:
                if key not in sensor_data or key not in history_data: continue
                    
                if not s.get("enabled", False):
                    if s["type"] == "uv254": sensor_data[key] = {"val": "Off", "temp": "--", "turb": "--", "ao": "0.00", "status": "OFF"}
                    else: sensor_data[key] = {"val": "Off", "ao": "0.00", "status": "OFF"}
                    if len(history_data[key]) > 0: history_data[key].pop(0)
                    history_data[key].append(None)
                    continue
                    
                s_type = s.get("type")
                s_id = int(s.get("id"))
                ao_val = 4.0
                
                try:
                    with modbus_lock:
                        instr = create_instrument(s_id)
                        val_num = None
                        
                        if s_type == "mlss":
                            v = read_with_retry(instr.read_float, 6, 3, 2)
                            sensor_data[key]["val"] = f"{v:.2f}"
                            val_num = v
                            
                        elif s_type == "uv254":
                            read_with_retry(instr.read_register, 12288, 0, 3)
                            t_r = read_with_retry(instr.read_registers, 9728, 2, 3)
                            c_r = read_with_retry(instr.read_registers, 9730, 2, 3)
                            tr_r = read_with_retry(instr.read_registers, 4608, 2, 3)
                            c_val = decode_dcba(c_r)
                            sensor_data[key]["val"] = f"{c_val:.2f}"
                            sensor_data[key]["temp"] = f"{decode_dcba(t_r):.1f}"
                            sensor_data[key]["turb"] = f"{decode_dcba(tr_r):.2f}"
                            val_num = c_val
                            
                        elif s_type in ["orp", "oil", "do"]:
                            read_with_retry(instr.read_register, 12288, 0, 3)
                            val = decode_dcba(read_with_retry(instr.read_registers, 9730, 2, 3))
                            fmt = "{:.1f}" if s_type == "orp" else "{:.2f}"
                            sensor_data[key]["val"] = fmt.format(val)
                            val_num = val
                        
                        min_v = float(s.get("min", 0))
                        max_v = float(s.get("max", 100))
                        
                        if val_num is not None:
                            if key not in hourly_buffer: hourly_buffer[key] = []
                            hourly_buffer[key].append(val_num)
                            
                            is_alarm = val_num >= (max_v * 0.9)
                            if is_alarm and not alarm_states.get(key, False):
                                alarm_states[key] = True
                                write_csv_log(LOG_ALARM, ["Time", "Sensor", "Event", "Value"], [now.strftime("%Y-%m-%d %H:%M:%S"), s["label"], "HIGH ALARM TRIGGERED", f"{val_num:.2f}"])
                            elif not is_alarm and alarm_states.get(key, False):
                                alarm_states[key] = False
                                write_csv_log(LOG_ALARM, ["Time", "Sensor", "Event", "Value"], [now.strftime("%Y-%m-%d %H:%M:%S"), s["label"], "ALARM CLEARED", f"{val_num:.2f}"])
                        
                        if max_v <= min_v: ao_val = 4.0
                        else:
                            c_val = max(min_v, min(val_num if val_num else min_v, max_v))
                            ao_val = 4.0 + ((c_val - min_v) / (max_v - min_v)) * 16.0
                        
                        sensor_data[key]["ao"] = f"{ao_val:.2f}"
                        sensor_data[key]["status"] = "OK"
                        
                    if key in active_keys:
                        ch_index = active_keys.index(key) 
                        if ch_index < 4: 
                            ao_id = int(config.get("ao_id", 9))
                            ao_int = int(ao_val * 1000) 
                            try:
                                with modbus_lock:
                                    instr_ao = create_instrument(ao_id)
                                    read_with_retry(instr_ao.write_register, ch_index, ao_int, 0, 6, retries=1)
                            except Exception: pass
                            
                    if len(history_data[key]) > 0: history_data[key].pop(0)
                    history_data[key].append(val_num)
                    
                except Exception as e:
                    if s["type"] == "uv254": 
                        sensor_data[key]["val"] = "Err"
                        sensor_data[key]["temp"] = "Err"
                        sensor_data[key]["turb"] = "Err"
                    else: 
                        sensor_data[key]["val"] = "Err"
                    sensor_data[key]["ao"] = "0.00"
                    sensor_data[key]["status"] = "ERR"
                    if len(history_data[key]) > 0: history_data[key].pop(0)
                    history_data[key].append(None)

            if active_keys:
                if now.minute % 5 == 0 and now.minute != last_5min_minute:
                    headers = ["Time"] + [config["sensors"][k]["label"] for k in active_keys]
                    row = [now.strftime("%Y-%m-%d %H:%M:00")] + [sensor_data.get(k, {}).get("val", "--") for k in active_keys]
                    write_csv_log(LOG_5MIN, headers, row)
                    last_5min_minute = now.minute

                if now.minute == 0 and now.hour != last_1hr_hour:
                    headers = ["Time"] + [config["sensors"][k]["label"] + " (AVG)" for k in active_keys]
                    row = [now.strftime("%Y-%m-%d %H:00:00")]
                    for k in active_keys:
                        vals = [v for v in hourly_buffer.get(k, []) if v is not None]
                        if vals: row.append(f"{(sum(vals)/len(vals)):.2f}")
                        else: row.append("--")
                        hourly_buffer[k] = [] 
                    
                    write_csv_log(LOG_1HR, headers, row)
                    last_1hr_hour = now.hour

        except Exception: pass
        time.sleep(0.5)

# --- API ---
def read_tail(filepath, lines=30):
    if not os.path.exists(filepath): return {"headers": [], "rows": []}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            all_rows = list(csv.reader(f))
            if len(all_rows) > 0:
                return {"headers": all_rows[0], "rows": list(reversed(all_rows[1:][-lines:]))}
    except: pass
    return {"headers": [], "rows": []}

@app.get("/api/all")
def get_all(): 
    logs = {
        "5min": read_tail(LOG_5MIN),
        "1hr": read_tail(LOG_1HR),
        "alarm": read_tail(LOG_ALARM, 50)
    }
    return {"data": sensor_data, "history": history_data, "relays": relay_states, "config": config, "logs": logs}

@app.post("/api/save_config")
async def update_cfg(request: Request):
    global config
    config = await request.json()
    save_config(config)
    init_data_structures()
    return {"status": "ok"}

@app.get("/api/relay")
def toggle_relay(ch: int, state: int):
    relay_states[ch] = state
    try:
        with modbus_lock:
            instr = create_instrument(config.get("relay_id", 8))
            instr.write_bit(ch, state, 5)
    except: pass
    return {"status": "ok"}

@app.get("/api/export_to_desktop")
def export_to_desktop(type: str):
    file_map = {"5min": LOG_5MIN, "1hr": LOG_1HR, "alarm": LOG_ALARM}
    target_file = file_map.get(type)
    
    if not target_file or not os.path.exists(target_file):
        return {"status": "error", "message": "No data"}
        
    try:
        desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.exists(desktop_path):
            desktop_path = os.path.expanduser("~") 
            
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_name = f"Export_{type}_{timestamp}.csv"
        destination = os.path.join(desktop_path, export_name)
        
        shutil.copy2(target_file, destination)
        return {"status": "ok", "path": destination}
    except Exception as e:
        return {"status": "error", "message": str(e)}

# === 하드웨어 교정(Calibration) API ===
@app.get("/api/get_cal")
def get_cal(sensor_id: int, s_type: str):
    try:
        with modbus_lock:
            instr = create_instrument(sensor_id)
            instr.serial.timeout = 1.0
            
            if s_type == "mlss":
                k_val = 1.0
                b_val = read_with_retry(instr.read_float, 18, 3, 2, retries=2)
            else:
                regs = read_with_retry(instr.read_registers, 4352, 4, 3, retries=2)
                k_val = decode_dcba(regs[0:2])
                b_val = decode_dcba(regs[2:4])
                
            if math.isnan(k_val): k_val = 1.0
            if math.isnan(b_val): b_val = 0.0
                
            return {"status": "ok", "k": round(k_val, 4), "b": round(b_val, 4)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/set_cal")
def set_cal(sensor_id: int, s_type: str, k: float, b: float):
    try:
        with modbus_lock:
            instr = create_instrument(sensor_id)
            instr.serial.timeout = 1.0
            
            if s_type == "mlss":
                read_with_retry(instr.write_float, 18, float(b), 2, 0, retries=2)
            else:
                k_regs = encode_dcba(k)
                b_regs = encode_dcba(b)
                payload = [k_regs[0], k_regs[1], b_regs[0], b_regs[1]]
                read_with_retry(instr.write_registers, 4352, payload, retries=2)
            
            return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/exit")
def exit_app():
    os.kill(os.getpid(), signal.SIGINT)
    return {"status": "ok"}

# --- HTML/JS Frontend ---
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
        <script>
            tailwind.config = { darkMode: 'class' }
        </script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body { font-family: 'Inter', sans-serif; overflow: hidden; -webkit-font-smoothing: antialiased; transition: background-color 0.3s, color 0.3s; }
            .nav-btn.active { border-bottom: 2px solid #0891b2; color: #0891b2; }
            .dark .nav-btn.active { border-bottom: 2px solid #22d3ee; color: #22d3ee; }
            
            .card { transition: background-color 0.3s ease, border-color 0.3s ease; box-shadow: none !important; }
            .sensor-card { cursor: pointer; }
            .sensor-card:hover { border-color: #94a3b8; }
            .dark .sensor-card:hover { border-color: #475569; }
            .sensor-card.active-chart { border-color: #0891b2; background: rgba(8, 145, 178, 0.05); }
            .dark .sensor-card.active-chart { border-color: #22d3ee; background: rgba(34, 211, 238, 0.05); }
            
            .toggle-bg { transition: background-color 0.3s ease; }
            input:checked ~ .toggle-bg { background-color: #0891b2; border-color: #0891b2; }
            .dark input:checked ~ .toggle-bg { background-color: #06b6d4; border-color: #06b6d4; }
            input:checked ~ .toggle-bg .toggle-dot { transform: translateX(100%); background-color: white; }
            
            .modal-overlay { backdrop-filter: blur(5px); }
            
            select option { background-color: #ffffff; color: #0f172a; }
            .dark select option { background-color: #1e293b; color: #f8fafc; }

            .editable-label { background: transparent; border-bottom: 1px solid transparent; transition: border-color 0.2s; }
            .editable-label:hover { border-bottom: 1px solid #94a3b8; }
            .dark .editable-label:hover { border-bottom: 1px solid #475569; }
            .editable-label:focus { border-bottom: 1px solid #0891b2; outline: none; }
            .dark .editable-label:focus { border-bottom: 1px solid #06b6d4; outline: none; }
            
            ::-webkit-scrollbar { width: 6px; }
            ::-webkit-scrollbar-track { background: transparent; }
            ::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 4px; }
            .dark ::-webkit-scrollbar-thumb { background: #334155; }
            ::-webkit-scrollbar-thumb:hover { background: #94a3b8; }
            .dark ::-webkit-scrollbar-thumb:hover { background: #475569; }
            
            .log-tab-btn.active { background-color: #0891b2; color: white; border-color: #0891b2; }
            .dark .log-tab-btn.active { background-color: #06b6d4; color: white; border-color: #06b6d4; }
        </style>
        <script>
            window.isAppDark = true;
            try {
                if (window.localStorage && window.localStorage.getItem('theme') === 'light') {
                    window.isAppDark = false;
                }
            } catch(e) {}
            if (window.isAppDark) document.documentElement.classList.add('dark');
        </script>
    </head>
    <body class="h-screen flex flex-col relative bg-slate-100 dark:bg-[#020617] text-slate-800 dark:text-[#f8fafc]">

        <nav class="flex gap-8 px-8 py-4 bg-white dark:bg-slate-900/50 border-b border-slate-300 dark:border-slate-800 items-center shrink-0 z-10 transition-colors">
            <button onclick="showTab('dash')" id="btn-dash" class="nav-btn active text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400">Dashboard</button>
            <button onclick="showTab('trends')" id="btn-trends" class="nav-btn text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400">Trends</button>
            <button onclick="showTab('logs')" id="btn-logs" class="nav-btn text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400">Logs</button>
            <button onclick="showTab('ctrl')" id="btn-ctrl" class="nav-btn text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400">Control</button>
            <button onclick="showTab('eng')" id="btn-eng" class="nav-btn text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400">Setup</button>
            <div class="flex-grow"></div>
            
            <button onclick="toggleTheme()" id="btn-theme" class="text-slate-600 dark:text-amber-400 text-sm font-bold bg-slate-200 dark:bg-slate-800/80 px-4 py-1.5 rounded hover:bg-slate-300 dark:hover:bg-slate-700 transition-colors flex items-center gap-2 mr-4 border border-slate-300 dark:border-slate-700">
                <span id="theme-icon" class="w-4 h-4 flex items-center justify-center"></span> <span class="text-xs tracking-wider uppercase">Theme</span>
            </button>
            
            <div id="sys-clock" class="text-cyan-700 dark:text-cyan-600/80 font-mono text-sm font-bold mr-6 tracking-widest">----/--/-- --:--:--</div>
            <button onclick="fetch('/api/exit')" class="text-rose-600 dark:text-rose-500 text-sm font-bold bg-rose-100 dark:bg-rose-950/30 px-4 py-1.5 rounded hover:bg-rose-200 dark:hover:bg-rose-900/50 transition-colors border border-rose-200 dark:border-rose-900/50">EXIT</button>
        </nav>

        <main id="tab-dash" class="p-6 flex gap-4 flex-grow overflow-hidden min-h-0">
            <div class="flex flex-col gap-4 flex-grow min-w-0 min-h-0">
                <div id="dashboard-grid" class="grid gap-4 flex-grow min-h-0"></div>
                <div id="dash-chart-wrapper" class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-3 h-[25vh] min-h-[150px] hidden flex-col shrink-0">
                    <div class="flex justify-between items-center mb-1">
                        <span class="text-[10px] font-bold text-slate-500 uppercase tracking-widest ml-10">MINI TREND</span>
                    </div>
                    <div id="dash-canvas-container" class="relative flex-grow min-h-0 w-full"></div>
                </div>
            </div>
            
            <div class="w-[280px] 2xl:w-[320px] flex flex-col gap-3 shrink-0 min-h-0">
                <h2 class="text-xs font-black text-slate-500 uppercase tracking-widest shrink-0 px-2">System Status</h2>
                <div id="status-sidebar" class="overflow-y-auto space-y-3 pr-2 pb-4 flex-grow min-h-0"></div>
            </div>
        </main>
        
        <main id="tab-trends" class="p-6 hidden flex-grow overflow-hidden flex flex-col min-h-0">
            <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg flex-grow p-6 flex flex-col min-h-0">
                <div class="flex justify-between items-center mb-4 shrink-0">
                    <span id="chart-title" class="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-widest">MULTI-TREND ANALYSIS</span>
                    <button onclick="setChartMode('all')" class="bg-slate-100 dark:bg-slate-800 text-cyan-600 dark:text-cyan-400 text-xs px-4 py-2 rounded font-bold hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors border border-slate-300 dark:border-slate-700">SHOW ALL LINES</button>
                </div>
                <div id="trend-canvas-container" class="relative flex-grow min-h-0 w-full"></div>
            </div>
        </main>

        <main id="tab-logs" class="p-6 hidden flex-grow overflow-hidden flex flex-col min-h-0 gap-4">
            <div class="flex justify-between items-center shrink-0">
                <div class="flex gap-2">
                    <button onclick="setLogView('5min')" id="btn-v-5min" class="log-tab-btn active px-4 py-2 rounded font-bold text-xs uppercase transition-colors border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400">5-Min Data</button>
                    <button onclick="setLogView('1hr')" id="btn-v-1hr" class="log-tab-btn px-4 py-2 rounded font-bold text-xs uppercase transition-colors border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400">1-Hour AVG</button>
                    <button onclick="setLogView('alarm')" id="btn-v-alarm" class="log-tab-btn px-4 py-2 rounded font-bold text-xs uppercase transition-colors border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400">Alarm History</button>
                </div>
                <button id="btn-export" onclick="exportToDesktop()" class="bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs px-6 py-2 rounded uppercase tracking-wider shadow-lg flex items-center gap-2 transition-colors border border-emerald-700">
                    <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.5" stroke="currentColor" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" /></svg>
                    Export to Desktop
                </button>
            </div>
            <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg overflow-auto flex-grow min-h-0">
                <table class="w-full text-left text-sm whitespace-nowrap">
                    <thead id="log-head" class="sticky top-0 z-10 shadow-sm"></thead>
                    <tbody id="log-body"></tbody>
                </table>
            </div>
        </main>

        <main id="tab-ctrl" class="p-6 hidden flex-grow flex items-center justify-center min-h-0">
            <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-8 w-full max-w-3xl border-t-4 border-t-cyan-500 dark:border-t-cyan-900/30">
                <h2 class="text-cyan-600 dark:text-cyan-400 font-black text-xl mb-6 uppercase border-b border-slate-300 dark:border-slate-700 pb-4">Manual Relay Override</h2>
                <div class="grid grid-cols-2 gap-8">
                    <script>
                        ['Pump 1 (Inlet)', 'Pump 2 (Outlet)', 'Aerator', 'Drain Valve'].forEach((name, i) => {
                            document.write(`
                            <div class="flex items-center justify-between bg-slate-100 dark:bg-slate-800/40 p-5 rounded-xl border border-slate-300 dark:border-slate-700/50">
                                <span class="text-sm font-bold text-slate-700 dark:text-slate-300 uppercase">${name}</span>
                                <label class="relative inline-flex items-center cursor-pointer">
                                    <input type="checkbox" id="relay-${i}" onchange="fetch('/api/relay?ch=${i}&state='+(this.checked?1:0))" class="sr-only peer">
                                    <div class="w-14 h-7 bg-slate-300 dark:bg-slate-900 rounded-full border border-slate-400 dark:border-slate-600 peer-checked:bg-cyan-500 transition-colors after:content-[''] after:absolute after:top-[3px] after:left-[3px] after:bg-white dark:after:bg-slate-400 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-[28px] peer-checked:after:bg-white"></div>
                                </label>
                            </div>`);
                        });
                    </script>
                </div>
            </div>
        </main>

        <main id="tab-eng" class="p-6 hidden flex-grow flex gap-6 overflow-hidden min-h-0">
            
            <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-6 flex flex-col gap-4 w-2/3 min-h-0">
                <div class="flex justify-between items-end border-b border-slate-300 dark:border-slate-700 pb-3 shrink-0">
                    <h2 class="text-cyan-600 dark:text-cyan-400 font-black text-base uppercase">Device Network Manager</h2>
                    <button onclick="openModal()" class="bg-emerald-600 text-white px-4 py-2 rounded text-sm font-bold hover:bg-emerald-500">+ ADD SENSOR</button>
                </div>
                <div id="eng-sensors" class="grid grid-cols-2 gap-4 mt-2 overflow-y-auto pr-2 flex-grow min-h-0 content-start"></div>
                <button id="save-btn" onclick="saveEngineering()" class="mt-2 bg-cyan-600 p-4 rounded text-base text-white font-black hover:bg-cyan-500 transition-all shrink-0 border border-cyan-700">SAVE & APPLY ALL SETTINGS</button>
            </div>
            
            <div class="flex flex-col gap-6 w-1/3 min-h-0">
                <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-6 flex flex-col gap-4 shrink-0">
                    <div class="border-b border-slate-300 dark:border-slate-700 pb-3 shrink-0"><h2 class="text-amber-600 dark:text-amber-500 font-black text-base uppercase">Hardware IDs</h2></div>
                    <div class="flex flex-col gap-3">
                        <div class="flex justify-between items-center bg-slate-100 dark:bg-slate-800/40 p-3 rounded-lg border border-slate-300 dark:border-slate-700/50">
                            <span class="text-sm font-bold text-slate-700 dark:text-slate-300 uppercase">Relay (KM6073)</span>
                            <input id="eng-relay-id" type="number" class="bg-white dark:bg-black border border-slate-300 dark:border-slate-600 w-20 text-center rounded p-1.5 text-slate-900 dark:text-white font-bold text-base outline-none focus:border-cyan-500 dark:focus:border-cyan-400">
                        </div>
                        <div class="flex justify-between items-center bg-slate-100 dark:bg-slate-800/40 p-3 rounded-lg border border-slate-300 dark:border-slate-700/50">
                            <span class="text-sm font-bold text-slate-700 dark:text-slate-300 uppercase">Analog (KM6023)</span>
                            <input id="eng-ao-id" type="number" class="bg-white dark:bg-black border border-slate-300 dark:border-slate-600 w-20 text-center rounded p-1.5 text-slate-900 dark:text-white font-bold text-base outline-none focus:border-cyan-500 dark:focus:border-cyan-400">
                        </div>
                    </div>
                </div>
                
                <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-6 flex flex-col gap-4 flex-grow min-h-0">
                    <div class="border-b border-slate-300 dark:border-slate-700 pb-3 flex justify-between items-end shrink-0">
                        <h2 class="text-emerald-600 dark:text-emerald-500 font-black text-base uppercase">4-20mA Scaling</h2>
                        <span class="text-xs text-slate-500 font-bold">Min / Max</span>
                    </div>
                    <div id="eng-ao-scaling" class="overflow-y-auto pr-2 flex-grow min-h-0 content-start flex flex-col gap-3"></div>
                </div>
            </div>
        </main>

        <div id="modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-xl w-[450px] p-6 shadow-2xl max-h-[90vh] overflow-y-auto flex flex-col">
                <h2 class="text-cyan-600 dark:text-cyan-400 font-black text-xl mb-4 shrink-0 uppercase">Add New Sensor</h2>
                <div class="space-y-4 flex-grow min-h-0">
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5">SENSOR TYPE</label>
                        <select id="new-type" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-black dark:text-black text-base focus:outline-none focus:border-cyan-500 dark:focus:border-cyan-400">
                            <option value="mlss">MLSS (Suspended Solids)</option>
                            <option value="uv254">UV254 (COD/BOD)</option>
                            <option value="do">DO (Dissolved Oxygen)</option>
                            <option value="orp">ORP</option>
                            <option value="oil">OIL IN WATER</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5">MODBUS ID (1-247)</label>
                        <input type="number" id="new-id" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-white text-base focus:outline-none focus:border-cyan-500 dark:focus:border-cyan-400">
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5">DISPLAY LABEL</label>
                        <input type="text" id="new-label" placeholder="e.g. DO 3 (Tank 2)" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-white text-base focus:outline-none focus:border-cyan-500 dark:focus:border-cyan-400">
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5">CHART COLOR</label>
                        <input type="color" id="new-color" class="w-full h-10 bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded cursor-pointer">
                    </div>
                </div>
                <div class="flex justify-end gap-3 mt-6 shrink-0">
                    <button onclick="closeModal()" class="px-5 py-2 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 dark:hover:bg-slate-600 border border-slate-300 dark:border-slate-600">CANCEL</button>
                    <button onclick="confirmAddSensor()" class="px-5 py-2 bg-emerald-600 rounded text-sm font-bold text-white hover:bg-emerald-500 border border-emerald-700">ADD DEVICE</button>
                </div>
            </div>
        </div>

        <div id="cal-modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border-t-4 border-t-indigo-500 border border-slate-300 dark:border-slate-700 rounded-xl w-[400px] p-6 shadow-2xl flex flex-col">
                <div class="flex justify-between items-center mb-2">
                    <h2 id="cal-modal-title" class="text-indigo-600 dark:text-indigo-400 font-black text-xl uppercase tracking-wider">CALIBRATION</h2>
                </div>
                <p class="text-xs font-bold text-rose-500 mb-5 uppercase tracking-wide">⚠️ Writes directly to sensor memory</p>
                
                <input type="hidden" id="cal-sensor-id">
                <input type="hidden" id="cal-sensor-type">
                
                <div class="space-y-4">
                    <div class="bg-slate-50 dark:bg-slate-800/50 p-4 rounded border border-slate-200 dark:border-slate-700">
                        <label class="block text-[11px] font-black text-slate-500 dark:text-slate-400 mb-1 uppercase tracking-widest">K Value (Span / 2-Point)</label>
                        <input type="number" step="0.0001" id="cal-k" class="w-full bg-transparent border-b border-slate-300 dark:border-slate-600 p-1 text-slate-900 dark:text-white text-lg font-mono font-bold focus:outline-none focus:border-indigo-500 text-right" placeholder="Reading...">
                    </div>
                    <div class="bg-slate-50 dark:bg-slate-800/50 p-4 rounded border border-slate-200 dark:border-slate-700">
                        <label class="block text-[11px] font-black text-slate-500 dark:text-slate-400 mb-1 uppercase tracking-widest">B Value (Zero / 1-Point)</label>
                        <input type="number" step="0.0001" id="cal-b" class="w-full bg-transparent border-b border-slate-300 dark:border-slate-600 p-1 text-slate-900 dark:text-white text-lg font-mono font-bold focus:outline-none focus:border-indigo-500 text-right" placeholder="Reading...">
                    </div>
                </div>
                
                <div class="flex justify-end gap-3 mt-6">
                    <button onclick="closeCalModal()" class="px-5 py-2 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 dark:hover:bg-slate-600 border border-slate-300 dark:border-slate-600">CANCEL</button>
                    <button id="cal-save-btn" onclick="saveCalibration()" class="px-5 py-2 bg-indigo-600 rounded text-sm font-bold text-white hover:bg-indigo-500 border border-indigo-700 transition-colors shadow-lg">SAVE TO SENSOR</button>
                </div>
            </div>
        </div>

        <script>
            let currentTab = 'dash';
            let currentChartMode = 'all';
            let currentLogView = '5min';
            let configData = null;
            let isInitialized = false;
            let charts = [];
            const unitMap = { "mlss": "mg/L", "uv254": "mg/L", "do": "mg/L", "orp": "mV", "oil": "ug/L" };

            const iconSun = `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" d="M12 3v2.25m6.364.386l-1.591 1.591M21 12h-2.25m-.386 6.364l-1.591-1.591M12 18.75V21m-4.773-4.227l-1.591 1.591M5.25 12H3m4.227-4.773L5.636 5.636M15.75 12a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z" /></svg>`;
            const iconMoon = `<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" d="M21.752 15.002A9.718 9.718 0 0118 15.75c-5.385 0-9.75-4.365-9.75-9.75 0-1.33.266-2.597.748-3.752A9.753 9.753 0 003 11.25C3 16.635 7.365 21 12.75 21a9.753 9.753 0 009.002-5.998z" /></svg>`;

            let isDark = window.isAppDark;
            document.getElementById('theme-icon').innerHTML = isDark ? iconSun : iconMoon;
            
            function toggleTheme() {
                const html = document.documentElement;
                isDark = !html.classList.contains('dark');
                if (isDark) {
                    html.classList.add('dark');
                    document.getElementById('theme-icon').innerHTML = iconSun;
                } else {
                    html.classList.remove('dark');
                    document.getElementById('theme-icon').innerHTML = iconMoon;
                }
                try { if (window.localStorage) window.localStorage.setItem('theme', isDark ? 'dark' : 'light'); } catch(e) {}
                updateChartColors();
            }

            function updateChartColors() {
                const gridColor = isDark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.05)';
                const tickColor = isDark ? '#64748b' : '#94a3b8';
                const legendColor = isDark ? '#cbd5e1' : '#475569';
                if(charts[0]) {
                    charts[0].options.scales.y.grid.color = gridColor;
                    charts[0].options.scales.y.ticks.color = tickColor;
                    charts[0].update('none');
                }
                if(charts[1]) {
                    charts[1].options.scales.y.grid.color = gridColor;
                    charts[1].options.plugins.legend.labels.color = legendColor;
                    charts[1].update('none');
                }
            }

            function updateClock() {
                const now = new Date();
                const pad = (n) => n.toString().padStart(2, '0');
                const clockEl = document.getElementById('sys-clock');
                if (clockEl) clockEl.innerText = `${now.getFullYear()}-${pad(now.getMonth()+1)}-${pad(now.getDate())} ${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
            }
            setInterval(updateClock, 1000);
            updateClock();

            function showTab(tab) {
                document.querySelectorAll('main').forEach(m => m.classList.add('hidden'));
                document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
                document.getElementById('tab-' + tab).classList.remove('hidden');
                document.getElementById('btn-' + tab).classList.add('active');
                currentTab = tab;
            }

            function setLogView(view) {
                currentLogView = view;
                document.querySelectorAll('.log-tab-btn').forEach(b => b.classList.remove('active'));
                document.getElementById('btn-v-' + view).classList.add('active');
                update();
            }

            async function exportToDesktop() {
                const btn = document.getElementById('btn-export');
                const origHTML = btn.innerHTML;
                const origClasses = btn.className;

                try {
                    const res = await fetch('/api/export_to_desktop?type=' + currentLogView);
                    const data = await res.json();

                    if (data.status === 'ok') {
                        btn.innerHTML = '✅ SAVED TO DESKTOP';
                        btn.className = 'bg-emerald-500 text-white font-black text-xs px-6 py-2 rounded uppercase tracking-widest flex items-center gap-2 transition-colors border border-emerald-600';
                    } else {
                        btn.innerHTML = '❌ NO DATA YET';
                        btn.className = 'bg-rose-500 text-white font-black text-xs px-6 py-2 rounded uppercase tracking-widest flex items-center gap-2 transition-colors border border-rose-600';
                    }
                } catch (e) {
                    btn.innerHTML = '❌ ERROR';
                    btn.className = 'bg-rose-500 text-white font-black text-xs px-6 py-2 rounded uppercase tracking-widest flex items-center gap-2 transition-colors border border-rose-600';
                }

                setTimeout(() => {
                    btn.innerHTML = origHTML;
                    btn.className = origClasses;
                }, 3000);
            }

            function openModal() { 
                document.getElementById('new-type').value = 'mlss';
                document.getElementById('new-id').value = '21';
                document.getElementById('new-label').value = '';
                document.getElementById('new-color').value = '#22d3ee';
                document.getElementById('modal-overlay').classList.remove('hidden'); 
            }
            function closeModal() { document.getElementById('modal-overlay').classList.add('hidden'); }
            
            function confirmAddSensor() {
                const type = document.getElementById('new-type').value;
                const id = parseInt(document.getElementById('new-id').value);
                const label = document.getElementById('new-label').value || type.toUpperCase();
                const color = document.getElementById('new-color').value;
                const newKey = 's_' + Date.now();
                configData.sensors[newKey] = { id: id, type: type, enabled: true, label: label, color: color, min: 0, max: 100 };
                saveEngineering(true);
            }

            function deleteSensor(key) {
                if(confirm('Are you sure you want to delete this sensor?')) {
                    delete configData.sensors[key];
                    saveEngineering(true);
                }
            }

            async function openCalModal(id, type, label) {
                document.getElementById('cal-sensor-id').value = id;
                document.getElementById('cal-sensor-type').value = type;
                document.getElementById('cal-modal-title').innerText = label + " CALIBRATION";
                document.getElementById('cal-k').value = '';
                document.getElementById('cal-b').value = '';
                document.getElementById('cal-k').placeholder = 'Reading...';
                document.getElementById('cal-b').placeholder = 'Reading...';
                document.getElementById('cal-modal-overlay').classList.remove('hidden');

                try {
                    const res = await fetch(`/api/get_cal?sensor_id=${id}&s_type=${type}`);
                    const data = await res.json();
                    if (data.status === 'ok') {
                        document.getElementById('cal-k').value = data.k;
                        document.getElementById('cal-b').value = data.b;
                    } else {
                        document.getElementById('cal-k').placeholder = 'Comm Error';
                        document.getElementById('cal-b').placeholder = 'Comm Error';
                    }
                } catch (e) {
                    document.getElementById('cal-k').placeholder = 'Timeout';
                    document.getElementById('cal-b').placeholder = 'Timeout';
                }
            }

            function closeCalModal() { document.getElementById('cal-modal-overlay').classList.add('hidden'); }

            async function saveCalibration() {
                const id = document.getElementById('cal-sensor-id').value;
                const type = document.getElementById('cal-sensor-type').value;
                const k = parseFloat(document.getElementById('cal-k').value);
                const b = parseFloat(document.getElementById('cal-b').value);
                const btn = document.getElementById('cal-save-btn');

                if (isNaN(k) || isNaN(b)) {
                    alert("Please enter valid numeric values for K and B.");
                    return;
                }

                const origText = btn.innerText;
                btn.innerText = "WRITING...";

                try {
                    const res = await fetch(`/api/set_cal?sensor_id=${id}&s_type=${type}&k=${k}&b=${b}`);
                    const data = await res.json();
                    if (data.status === 'ok') {
                        btn.innerText = "✅ SAVED";
                        btn.classList.replace('bg-indigo-600', 'bg-emerald-600');
                        btn.classList.replace('border-indigo-700', 'border-emerald-700');
                        setTimeout(() => { 
                            closeCalModal(); 
                            btn.innerText = origText; 
                            btn.classList.replace('bg-emerald-600', 'bg-indigo-600');
                            btn.classList.replace('border-emerald-700', 'border-indigo-700');
                        }, 1200);
                    } else {
                        btn.innerText = "❌ FAILED";
                        btn.classList.replace('bg-indigo-600', 'bg-rose-600');
                        setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-indigo-600'); }, 2000);
                    }
                } catch(e) {
                    btn.innerText = "❌ FAILED";
                    btn.classList.replace('bg-indigo-600', 'bg-rose-600');
                    setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-indigo-600'); }, 2000);
                }
            }

            function focusChart(mode) { setChartMode(mode); showTab('trends'); }

            function setChartMode(mode) {
                currentChartMode = mode;
                document.querySelectorAll('.sensor-card').forEach(c => c.classList.remove('active-chart'));
                
                if (mode === 'all') {
                    document.getElementById('chart-title').innerText = 'MULTI-TREND ANALYSIS';
                    if(charts[0]) charts[0].options.plugins.legend.display = false; 
                    if(charts[1]) charts[1].options.plugins.legend.display = true;
                    charts.forEach(c => {
                        c.data.datasets.forEach(ds => { ds.hidden = !(configData.sensors[ds.id] && configData.sensors[ds.id].enabled); });
                        c.update('none'); 
                    });
                } else {
                    const el = document.getElementById('card-' + mode);
                    if(el) el.classList.add('active-chart');
                    document.getElementById('chart-title').innerText = configData.sensors[mode].label + ' ISOLATED TREND';
                    if(charts[0]) charts[0].options.plugins.legend.display = false;
                    if(charts[1]) charts[1].options.plugins.legend.display = false;
                    charts.forEach(c => {
                        c.data.datasets.forEach(ds => { ds.hidden = (ds.id !== mode); });
                        c.update('none');
                    });
                }
            }

            function initDynamicUI() {
                const activeSensors = Object.entries(configData.sensors).filter(([k, v]) => v.enabled);
                const count = activeSensors.length;
                
                const grid = document.getElementById('dashboard-grid');
                grid.innerHTML = '';
                const dashChartWrap = document.getElementById('dash-chart-wrapper');
                
                if (count === 1) {
                    grid.className = "grid gap-4 grid-cols-1 grid-rows-1 flex-grow min-h-0";
                    dashChartWrap.classList.remove('hidden');
                    dashChartWrap.style.display = 'flex';
                } else if (count === 2) {
                    grid.className = "grid gap-4 grid-cols-1 grid-rows-2 flex-grow min-h-0";
                    dashChartWrap.classList.remove('hidden');
                    dashChartWrap.style.display = 'flex';
                } else if (count <= 4) {
                    grid.className = "grid gap-4 grid-cols-2 grid-rows-2 flex-grow min-h-0";
                    dashChartWrap.classList.add('hidden');
                    dashChartWrap.style.display = 'none';
                } else {
                    grid.className = "grid gap-4 grid-cols-2 grid-rows-3 flex-grow min-h-0";
                    dashChartWrap.classList.add('hidden');
                    dashChartWrap.style.display = 'none';
                }

                if (count === 0) grid.innerHTML = `<div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-10 flex items-center justify-center text-slate-500 font-bold col-span-full">NO SENSORS ENABLED. GO TO SETUP.</div>`;

                let valSize, unitSize, lblSize;
                if (count === 1) { valSize = '22vh'; unitSize = '5vh'; lblSize = '3vh'; }
                else if (count === 2) { valSize = '14vh'; unitSize = '4vh'; lblSize = '2.5vh'; }
                else if (count <= 4) { valSize = '10vh'; unitSize = '3vh'; lblSize = '2vh'; }
                else { valSize = '7vh'; unitSize = '2vh'; lblSize = '1.5vh'; }

                activeSensors.forEach(([key, s]) => {
                    const unit = unitMap[s.type] || "";
                    let extraHtml = '';
                    if (s.type === 'uv254') {
                        extraHtml = `
                        <div class="absolute top-3 right-4 text-right font-bold text-slate-500 dark:text-slate-400" style="font-size: ${unitSize};">
                            T: <span id="v-${key}-t" class="text-slate-800 dark:text-white">--</span> °C<br>
                            Tb: <span id="v-${key}-tr" class="text-slate-800 dark:text-white">--</span> NTU
                        </div>`;
                    }
                    grid.innerHTML += `
                    <div id="card-${key}" onclick="focusChart('${key}')" class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg sensor-card relative overflow-hidden group flex items-center justify-center">
                        <div class="absolute top-3 left-4 font-black uppercase tracking-wider" style="color: ${s.color}; font-size: ${lblSize};">${s.label}</div>
                        ${extraHtml}
                        <div class="flex items-baseline justify-center">
                            <p id="v-${key}" class="font-black text-slate-800 dark:text-white leading-none tracking-tighter" style="font-size: ${valSize};">--</p>
                            <span class="text-slate-500 font-bold ml-3" style="font-size: ${unitSize};">${unit}</span>
                        </div>
                    </div>`;
                });

                charts.forEach(c => c.destroy());
                charts = [];
                
                document.getElementById('dash-canvas-container').innerHTML = '<canvas id="dashChart" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;"></canvas>';
                document.getElementById('trend-canvas-container').innerHTML = '<canvas id="trendChart" style="position: absolute; top: 0; left: 0; width: 100%; height: 100%;"></canvas>';

                setTimeout(() => {
                    const datasets = activeSensors.map(([key, s]) => ({
                        id: key, label: s.label, data: [], borderColor: s.color, borderWidth: 2, pointRadius: 0, tension: 0.1, spanGaps: false
                    }));

                    const gridColor = isDark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.05)';
                    const tickColor = isDark ? '#64748b' : '#94a3b8';
                    const legendColor = isDark ? '#cbd5e1' : '#475569';

                    let newCharts = [];

                    const dashCtx = document.getElementById('dashChart');
                    if (dashCtx) {
                        newCharts.push(new Chart(dashCtx.getContext('2d'), {
                            type: 'line', data: { labels: Array(30).fill(''), datasets: JSON.parse(JSON.stringify(datasets)) },
                            options: { 
                                animation: false, responsive: true, maintainAspectRatio: false, layout: { padding: { top: 5, bottom: 5 } }, 
                                scales: { x: { display: false }, y: { display: true, grace: '10%', border: { display: false }, grid: { color: gridColor }, ticks: { color: tickColor, font: { size: 10 }, maxTicksLimit: 4 } } }, 
                                plugins: { legend: { display: false } } 
                            }
                        }));
                    }

                    const trendCtx = document.getElementById('trendChart');
                    if (trendCtx) {
                        newCharts.push(new Chart(trendCtx.getContext('2d'), {
                            type: 'line', data: { labels: Array(30).fill(''), datasets: JSON.parse(JSON.stringify(datasets)) },
                            options: { animation: false, responsive: true, maintainAspectRatio: false, scales: { x: { display: false }, y: { grid: { color: gridColor } } }, plugins: { legend: { display: true, position: 'top', align: 'end', labels: { color: legendColor, boxWidth: 12 } } } }
                        }));
                    }

                    charts = newCharts;
                }, 100);

                document.getElementById('eng-relay-id').value = configData.relay_id;
                document.getElementById('eng-ao-id').value = configData.ao_id;
                
                let engHTML = '';
                let aoHTML = '';
                for(const [key, s] of Object.entries(configData.sensors)) {
                    engHTML += `
                    <div class="flex flex-col bg-slate-100 dark:bg-slate-800/40 p-4 rounded-lg border border-slate-300 dark:border-slate-700/50 h-[100px] shrink-0">
                        <div class="flex justify-between items-center border-b border-slate-300 dark:border-slate-700/50 pb-3 mb-3">
                            <div class="flex items-center gap-4">
                                <label class="relative inline-flex items-center cursor-pointer">
                                    <input type="checkbox" id="en-${key}" ${s.enabled ? 'checked' : ''} class="sr-only peer">
                                    <div class="w-12 h-6 bg-slate-300 dark:bg-slate-900 rounded-full border border-slate-400 dark:border-slate-600 peer-checked:bg-emerald-500 transition-colors after:absolute after:top-[1px] after:left-[2px] after:bg-white dark:after:bg-slate-400 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-[24px] peer-checked:after:bg-white"></div>
                                </label>
                                <input id="label-${key}" type="text" value="${s.label}" class="editable-label text-sm font-black uppercase truncate w-28 px-1 text-slate-800 dark:text-white" style="color: ${s.color}">
                            </div>
                            <button onclick="deleteSensor('${key}')" class="text-rose-500 hover:text-rose-600 dark:hover:text-rose-400"><svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" /></svg></button>
                        </div>
                        <div class="flex justify-between items-center">
                            <span class="text-xs text-slate-500 font-bold">TYPE: ${s.type.toUpperCase()}</span>
                            <div class="flex items-center">
                                <span class="text-xs text-slate-500 mr-2">ID:</span>
                                <input id="id-${key}" type="number" value="${s.id}" class="bg-white dark:bg-black border border-slate-300 dark:border-slate-600 w-12 text-center rounded p-1 text-slate-800 dark:text-white font-bold text-sm outline-none focus:border-cyan-500 dark:focus:border-cyan-400">
                                <button onclick="openCalModal(${s.id}, '${s.type}', '${s.label}')" class="bg-indigo-100 text-indigo-600 hover:bg-indigo-200 dark:bg-indigo-900/40 dark:text-indigo-400 dark:hover:bg-indigo-800/60 px-2 py-1 rounded text-[10px] font-black uppercase tracking-wider ml-2 border border-indigo-200 dark:border-indigo-800 transition-colors">CAL</button>
                            </div>
                        </div>
                    </div>`;
                    
                    if(s.enabled) {
                        aoHTML += `
                        <div class="bg-slate-100 dark:bg-slate-800/30 p-4 rounded-lg border border-slate-300 dark:border-slate-700/50 shrink-0">
                            <div class="text-sm font-bold truncate mb-2.5" style="color: ${s.color}">${s.label}</div>
                            <div class="flex gap-3">
                                <div class="flex-1 flex items-center bg-white dark:bg-black border border-slate-300 dark:border-slate-600 rounded px-2 py-1.5">
                                    <span class="text-xs text-slate-500 w-9">4mA:</span>
                                    <input id="min-${key}" type="number" value="${s.min}" class="bg-transparent w-full text-right text-slate-800 dark:text-white font-bold text-sm outline-none">
                                </div>
                                <div class="flex-1 flex items-center bg-white dark:bg-black border border-slate-300 dark:border-slate-600 rounded px-2 py-1.5">
                                    <span class="text-xs text-slate-500 w-10">20mA:</span>
                                    <input id="max-${key}" type="number" value="${s.max}" class="bg-transparent w-full text-right text-slate-800 dark:text-white font-bold text-sm outline-none">
                                </div>
                            </div>
                        </div>`;
                    }
                }
                document.getElementById('eng-sensors').innerHTML = engHTML;
                document.getElementById('eng-ao-scaling').innerHTML = aoHTML;
                
                isInitialized = true;
                setChartMode('all');
            }

            async function update() {
                try {
                    const res = await fetch('/api/all');
                    const d = await res.json();
                    if(!isInitialized) { configData = d.config; initDynamicUI(); }

                    if(currentTab === 'dash' || currentTab === 'trends') {
                        let sidebarHTML = '';
                        const activeSensors = Object.entries(configData.sensors).filter(([k, v]) => v.enabled);
                        
                        activeSensors.forEach(([key, s]) => {
                            const d_s = d.data[key];
                            if(!d_s) return;
                            
                            const el = document.getElementById('v-' + key);
                            if(el) el.innerText = d_s.val;
                            if(s.type === 'uv254') {
                                const elT = document.getElementById(`v-${key}-t`);
                                const elTr = document.getElementById(`v-${key}-tr`);
                                if(elT) elT.innerText = d_s.temp;
                                if(elTr) elTr.innerText = d_s.turb;
                            }
                            
                            const isErr = d_s.status === 'ERR' || d_s.val === 'Err';
                            const statColor = isErr ? 'text-rose-500' : 'text-emerald-600 dark:text-emerald-400';
                            const dotColor = isErr ? 'bg-rose-500' : 'bg-emerald-500 animate-pulse';
                            
                            let alarmText = 'NORMAL';
                            let alarmColor = 'text-slate-500';
                            if(!isErr && d_s.val !== '--' && d_s.val !== 'Off') {
                                const v = parseFloat(d_s.val);
                                const m = parseFloat(s.max || 100);
                                if(v >= m * 0.9) { alarmText = 'HIGH ALARM'; alarmColor = 'text-amber-600 dark:text-amber-500 animate-pulse font-black'; }
                            }
                            if(isErr) { alarmText = 'COMM FAULT'; alarmColor = 'text-rose-600 dark:text-rose-500 font-black'; }

                            sidebarHTML += `
                            <div class="card p-3.5 rounded-lg flex flex-col gap-1 border bg-white dark:bg-transparent ${isErr ? 'border-rose-400 bg-rose-50 dark:border-rose-900/50 dark:bg-rose-950/10' : 'border-slate-300 dark:border-slate-800'} shrink-0">
                                <div class="flex justify-between items-center mb-1 pb-2 border-b border-slate-300 dark:border-slate-700/50">
                                    <span class="text-sm font-black truncate pr-2 uppercase" style="color: ${s.color}">${s.label}</span>
                                    <span class="flex items-center gap-1.5 text-[11px] font-bold ${statColor} shrink-0 uppercase tracking-widest">
                                        <div class="w-2.5 h-2.5 rounded-full ${dotColor}"></div> ${d_s.status || 'WAIT'}
                                    </span>
                                </div>
                                <div class="flex justify-between items-end mt-1">
                                    <span class="text-xs text-slate-500 font-bold mb-1">OUT (AO):</span>
                                    <span class="font-mono text-cyan-600 dark:text-cyan-400 font-black text-2xl tracking-wider">${d_s.ao || '--'} <span class="text-xs text-cyan-700">mA</span></span>
                                </div>
                                <div class="flex justify-between items-end mt-1">
                                    <span class="text-xs text-slate-500 font-bold">ALARM:</span>
                                    <span class="text-sm tracking-wide ${alarmColor}">${alarmText}</span>
                                </div>
                            </div>`;
                        });
                        
                        const sb = document.getElementById('status-sidebar');
                        if(sb) sb.innerHTML = sidebarHTML;

                        charts.forEach(c => {
                            c.data.datasets.forEach(ds => { ds.data = d.history[ds.id]; });
                            c.update('none');
                        });
                    }

                    if(currentTab === 'logs') {
                        const targetLog = d.logs[currentLogView];
                        const thead = document.getElementById('log-head');
                        const tbody = document.getElementById('log-body');
                        
                        if(targetLog && targetLog.headers.length > 0) {
                            thead.innerHTML = `<tr class="bg-slate-200 dark:bg-slate-800"><th class="px-6 py-4 font-black tracking-wider uppercase text-slate-700 dark:text-slate-300">${targetLog.headers.join('</th><th class="px-6 py-4 font-black tracking-wider uppercase text-slate-700 dark:text-slate-300">')}</th></tr>`;
                            
                            tbody.innerHTML = targetLog.rows.map(row => {
                                while(row.length < targetLog.headers.length) row.push('--');
                                
                                let rowClass = "border-b border-slate-300 dark:border-slate-700 hover:bg-slate-100 dark:hover:bg-slate-800/80 transition-colors text-sm font-bold";
                                
                                if(currentLogView === 'alarm' && row[2] === 'HIGH ALARM TRIGGERED') {
                                    rowClass += " bg-rose-100 dark:bg-rose-950/30 text-rose-700 dark:text-rose-400 font-black";
                                }
                                
                                return `<tr class="${rowClass}"><td class="px-6 py-4 text-slate-800 dark:text-slate-200 tracking-wide">${row.join('</td><td class="px-6 py-4 text-slate-800 dark:text-slate-200 tracking-wide">')}</td></tr>`;
                            }).join('');
                        } else {
                            thead.innerHTML = '';
                            tbody.innerHTML = `<tr><td class="p-12 text-center text-slate-500 font-black uppercase tracking-widest text-lg">No Data Available Yet</td></tr>`;
                        }
                    }

                    if(currentTab === 'ctrl') {
                        for(let i=0; i<4; i++) {
                            const toggle = document.getElementById('relay-' + i);
                            if(toggle && document.activeElement !== toggle) toggle.checked = (d.relays[i] === 1);
                        }
                    }
                } catch (e) {}
            }

            async function saveEngineering(isFromModal = false) {
                if(!isFromModal) {
                    configData.relay_id = parseInt(document.getElementById('eng-relay-id').value);
                    configData.ao_id = parseInt(document.getElementById('eng-ao-id').value);
                    
                    for(const key of Object.keys(configData.sensors)) {
                        configData.sensors[key].id = parseInt(document.getElementById('id-' + key).value);
                        configData.sensors[key].enabled = document.getElementById('en-' + key).checked;
                        configData.sensors[key].label = document.getElementById('label-' + key).value;
                        
                        const minIn = document.getElementById('min-' + key);
                        const maxIn = document.getElementById('max-' + key);
                        if(minIn) configData.sensors[key].min = parseFloat(minIn.value);
                        if(maxIn) configData.sensors[key].max = parseFloat(maxIn.value);
                    }
                }
                
                await fetch('/api/save_config', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(configData)
                });
                
                closeModal();
                isInitialized = false; 
                
                const btn = document.getElementById('save-btn');
                if(btn && !isFromModal) {
                    const orig = btn.innerText;
                    btn.innerText = "SAVED SUCCESSFULLY!";
                    btn.classList.add('bg-emerald-600');
                    setTimeout(() => { btn.innerText = orig; btn.classList.remove('bg-emerald-600'); }, 1500);
                }
            }

            setInterval(update, 1000);
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
