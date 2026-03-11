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
from fastapi.staticfiles import StaticFiles

# === 시스템 절대 경로 고정 ===
BASE_DIR = '/home/young/farm'
LOG_DIR = os.path.join(BASE_DIR, 'logs')
STATIC_DIR = os.path.join(BASE_DIR, 'static')

os.makedirs(LOG_DIR, exist_ok=True) 
os.makedirs(STATIC_DIR, exist_ok=True)

PORT = '/dev/ttyUSB0'
LOG_5MIN = os.path.join(LOG_DIR, 'data_5min.csv')
LOG_1HR = os.path.join(LOG_DIR, 'data_1hr.csv')
LOG_ALARM = os.path.join(LOG_DIR, 'data_alarm.csv')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

app = FastAPI()
modbus_lock = threading.Lock()

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

BASE_UNITS = {
    "mlss": "mg/L", "uv254": "mg/L", "do": "mg/L", "orp": "mV", 
    "oil": "ug/L", "ph": "pH", "ec": "uS/cm", "turbidity": "NTU"
}

# --- 동적(Dynamic) 설정 ---
DEFAULT_CONFIG = {
    "theme": "dark",
    "lang": "en",
    "admin_pwd": "1234",
    "relay_id": 8,
    "ao_id": 9,
    "sensors": {
        "s_1": {"id": 15, "type": "mlss", "enabled": True, "label": "MLSS", "color": "#94a3b8", "unit": "mg/L", "min": 0, "max": 10000, "a": 1.0, "b": 0.0, "c_mode": "off", "c_int": 30, "c_dur": 10, "c_rel": 0},
        "s_2": {"id": 16, "type": "uv254", "enabled": True, "label": "UV254 (COD)", "color": "#3b82f6", "unit": "mg/L", "min": 0, "max": 100, "a": 1.0, "b": 0.0, "c_mode": "off", "c_int": 30, "c_dur": 10, "c_rel": 0}
    }
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f: 
                cfg = json.load(f)
                if "theme" not in cfg: cfg["theme"] = "dark"
                if "lang" not in cfg: cfg["lang"] = "en"
                if "admin_pwd" not in cfg: cfg["admin_pwd"] = "1234"
                if "ao_id" not in cfg: cfg["ao_id"] = 9
                if "sensors" not in cfg: cfg["sensors"] = {} 
                
                for k, v in cfg.get("sensors", {}).items():
                    s_type = v.get("type", "mlss")
                    if "unit" not in v:
                        default_units = {
                            "mlss":"mg/L", "uv254":"mg/L", "do":"mg/L", "orp":"mV", "oil":"ug/L",
                            "ph":"pH", "ec":"uS/cm", "turbidity":"NTU"
                        }
                        v["unit"] = default_units.get(s_type, "")
                    if "min" not in v: v["min"] = 0
                    if "max" not in v: v["max"] = 100
                    if "a" not in v: v["a"] = 1.0
                    if "b" not in v: v["b"] = 0.0
                    if "c_mode" not in v: v["c_mode"] = "off"
                    if "c_int" not in v: v["c_int"] = 30
                    if "c_dur" not in v: v["c_dur"] = 10
                    if "c_rel" not in v: v["c_rel"] = 0
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

clean_last_run = {}
clean_relay_off = {}

def init_data_structures():
    global sensor_data, history_data, hourly_buffer, alarm_states, clean_last_run, clean_relay_off
    sensor_data = {"status": "Online"}
    history_data = {}
    hourly_buffer = {}
    alarm_states = {}
    clean_last_run = {}
    clean_relay_off = {}
    
    for key, s in config.get("sensors", {}).items():
        if s["type"] == "uv254":
            sensor_data[key] = {"val": "--", "log_val": "--", "temp": "--", "turb": "--", "ao": "--", "status": "WAIT"}
        else:
            sensor_data[key] = {"val": "--", "log_val": "--", "ao": "--", "status": "WAIT"}
        history_data[key] = [None]*30
        hourly_buffer[key] = []
        alarm_states[key] = False

init_data_structures()

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

_shared_instr = None

def create_instrument(sensor_id):
    global _shared_instr
    if _shared_instr is None:
        _shared_instr = minimalmodbus.Instrument(PORT, int(sensor_id))
        _shared_instr.serial.baudrate = 9600
        _shared_instr.serial.timeout = 0.7  
        _shared_instr.clear_buffers_before_each_transaction = True
    else:
        _shared_instr.address = int(sensor_id)
    return _shared_instr

def read_with_retry(func, *args, retries=2):
    for attempt in range(retries):
        try: return func(*args)
        except:
            time.sleep(0.2) 
            if attempt == retries - 1: raise

def write_csv_log(filepath, headers, row):
    is_new = not os.path.exists(filepath)
    try:
        with open(filepath, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if is_new: writer.writerow(headers)
            writer.writerow(row)
    except: pass

def get_safe_int(val, default_val=0):
    try:
        if val is None or str(val).strip() == "" or str(val) == "NaN":
            return default_val
        return int(val)
    except:
        return default_val

def trigger_cleaning(key, force_mode=None, force_dur=None, force_rel=None):
    global clean_relay_off
    s = config.get("sensors", {}).get(key)
    if not s: return {"status": "error", "message": "Sensor not found"}
    
    c_mode = force_mode if force_mode else s.get("c_mode", "off")
    if c_mode == "off": return {"status": "ok"}
    
    try:
        if c_mode == "internal":
            with modbus_lock:
                instr = create_instrument(s["id"])
                try:
                    read_with_retry(instr.write_register, 12544, 1, 0, 6, retries=2)
                except:
                    read_with_retry(instr.write_registers, 12544, [1], retries=2)
        
        elif c_mode == "external":
            c_rel = force_rel if force_rel is not None else get_safe_int(s.get("c_rel", 0))
            c_dur = force_dur if force_dur is not None else get_safe_int(s.get("c_dur", 10))
            r_id = get_safe_int(config.get("relay_id", 8), 8)
            with modbus_lock:
                instr = create_instrument(r_id)
                read_with_retry(instr.write_bit, c_rel, 1, 5, retries=2)
                
            clean_relay_off[c_rel] = time.time() + c_dur
            relay_states[c_rel] = 1 
            
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def cleaning_worker():
    global clean_last_run, clean_relay_off
    while True:
        now = time.time()
        
        for key, s in config.get("sensors", {}).items():
            if not s.get("enabled", False): continue
            c_mode = s.get("c_mode", "off")
            if c_mode == "off": continue
            
            c_int_sec = get_safe_int(s.get("c_int", 30)) * 60
            if c_int_sec <= 0: continue
            
            if key not in clean_last_run:
                clean_last_run[key] = now 
                
            if now - clean_last_run[key] >= c_int_sec:
                trigger_cleaning(key)
                clean_last_run[key] = now
                
        for ch, off_time in list(clean_relay_off.items()):
            if now >= off_time:
                try:
                    r_id = get_safe_int(config.get("relay_id", 8), 8)
                    with modbus_lock:
                        instr = create_instrument(r_id)
                        read_with_retry(instr.write_bit, int(ch), 0, 5, retries=2)
                    relay_states[int(ch)] = 0
                    del clean_relay_off[ch]
                except: pass
                
        time.sleep(1)

def modbus_worker():
    global sensor_data, history_data, hourly_buffer, alarm_states
    
    last_5min_minute = -1
    last_1hr_hour = -1
    
    while True:
        now = datetime.now()
        all_keys = list(config.get("sensors", {}).keys())
        active_keys = [k for k in all_keys if config["sensors"][k].get("enabled")]
        
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
                s_id = get_safe_int(s.get("id"), 1)
                s_unit = s.get("unit", "")
                ao_val = 4.0
                
                try:
                    with modbus_lock:
                        instr = create_instrument(s_id)
                        raw_val = None
                        
                        if s_type == "mlss":
                            raw_val = read_with_retry(instr.read_float, 6, 3, 2)
                        elif s_type == "uv254":
                            read_with_retry(instr.read_register, 12288, 0, 3)
                            t_r = read_with_retry(instr.read_registers, 9728, 2, 3)
                            c_r = read_with_retry(instr.read_registers, 9730, 2, 3)
                            tr_r = read_with_retry(instr.read_registers, 4608, 2, 3)
                            raw_val = decode_dcba(c_r)
                            sensor_data[key]["temp"] = f"{decode_dcba(t_r):.1f}"
                            sensor_data[key]["turb"] = f"{decode_dcba(tr_r):.2f}"
                        # 추가된 범용 센서들 통신 로직 병합
                        elif s_type in ["orp", "oil", "do", "ph", "ec", "turbidity"]:
                            read_with_retry(instr.read_register, 12288, 0, 3)
                            raw_val = decode_dcba(read_with_retry(instr.read_registers, 9730, 2, 3))
                        
                        val_num = None
                        if raw_val is not None:
                            a_val = float(s.get("a", 1.0))
                            b_val = float(s.get("b", 0.0))
                            base_val = (raw_val * a_val) + b_val
                            
                            val_num = base_val
                            # 단위 변환 로직
                            if s_type == "oil" and s_unit in ["mg/L", "ppm"]:
                                val_num = base_val / 1000.0
                            elif s_type == "mlss" and s_unit == "g/L":
                                val_num = base_val / 1000.0
                            elif s_type == "mlss" and s_unit == "%":
                                val_num = base_val / 10000.0
                            elif s_type == "ec" and s_unit == "mS/cm":
                                val_num = base_val / 1000.0
                            
                            fmt = "{:.1f}" if s_type == "orp" else "{:.2f}"
                            sensor_data[key]["val"] = fmt.format(val_num)
                            sensor_data[key]["log_val"] = fmt.format(base_val)
                        
                        min_v = float(s.get("min", 0))
                        max_v = float(s.get("max", 100))
                        
                        if val_num is not None:
                            if key not in hourly_buffer: hourly_buffer[key] = []
                            hourly_buffer[key].append(base_val)
                            
                            is_alarm = val_num >= (max_v * 0.9)
                            if is_alarm and not alarm_states.get(key, False):
                                alarm_states[key] = True
                                write_csv_log(LOG_ALARM, ["Time", "Sensor", "Event", "Value"], [now.strftime("%Y-%m-%d %H:%M:%S"), s["label"], "HIGH ALARM TRIGGERED", f"{val_num:.2f}"])
                            elif not is_alarm and alarm_states.get(key, False):
                                alarm_states[key] = False
                                write_csv_log(LOG_ALARM, ["Time", "Sensor", "Event", "Value"], [now.strftime("%Y-%m-%d %H:%M:%S"), s["label"], "ALARM CLEARED", f"{val_num:.2f}"])
                        
                        if max_v <= min_v: ao_val = 4.0
                        else:
                            c_val = max(min_v, min(val_num if val_num is not None else min_v, max_v))
                            ao_val = 4.0 + ((c_val - min_v) / (max_v - min_v)) * 16.0
                        
                        sensor_data[key]["ao"] = f"{ao_val:.2f}"
                        sensor_data[key]["status"] = "OK"
                        
                    if key in active_keys:
                        ch_index = active_keys.index(key) 
                        if ch_index < 4: 
                            try:
                                ao_id = get_safe_int(config.get("ao_id", 9), 9)
                                ao_int = int(ao_val * 1000) 
                                with modbus_lock:
                                    instr_ao = create_instrument(ao_id)
                                    read_with_retry(instr_ao.write_register, ch_index, ao_int, 0, 6, retries=1)
                            except Exception: 
                                pass 
                            
                    if len(history_data[key]) > 0: history_data[key].pop(0)
                    history_data[key].append(val_num)
                    
                except Exception as e:
                    if s["type"] == "uv254": 
                        sensor_data[key]["val"] = "Err"
                        sensor_data[key]["temp"] = "Err"
                        sensor_data[key]["turb"] = "Err"
                    else: 
                        sensor_data[key]["val"] = "Err"
                    sensor_data[key]["log_val"] = "Err"
                    sensor_data[key]["ao"] = "0.00"
                    sensor_data[key]["status"] = "ERR"
                    if len(history_data[key]) > 0: history_data[key].pop(0)
                    history_data[key].append(None)

            if all_keys:
                if now.minute % 5 == 0 and now.minute != last_5min_minute:
                    headers = ["Time"] + [f"{config['sensors'][k]['label']} ({BASE_UNITS.get(config['sensors'][k].get('type'), '')})" for k in all_keys]
                    row = [now.strftime("%Y-%m-%d %H:%M:00")] + [sensor_data.get(k, {}).get("log_val", "--") for k in all_keys]
                    write_csv_log(LOG_5MIN, headers, row)
                    last_5min_minute = now.minute

                if now.minute == 0 and now.hour != last_1hr_hour:
                    headers = ["Time"] + [f"{config['sensors'][k]['label']} ({BASE_UNITS.get(config['sensors'][k].get('type'), '')} AVG)" for k in all_keys]
                    row = [now.strftime("%Y-%m-%d %H:00:00")]
                    for k in all_keys:
                        vals = [v for v in hourly_buffer.get(k, []) if v is not None]
                        if vals: row.append(f"{(sum(vals)/len(vals)):.2f}")
                        else: row.append("--")
                        hourly_buffer[k] = [] 
                    
                    write_csv_log(LOG_1HR, headers, row)
                    last_1hr_hour = now.hour

        except Exception: pass
        time.sleep(0.5)

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
    new_config = await request.json()
    
    old_keys = set(config.get("sensors", {}).keys())
    new_keys = set(new_config.get("sensors", {}).keys())
    
    config = new_config
    save_config(config)
    
    if old_keys != new_keys:
        init_data_structures()
        try:
            if os.path.exists(LOG_5MIN): os.remove(LOG_5MIN)
            if os.path.exists(LOG_1HR): os.remove(LOG_1HR)
        except: pass
        
    return {"status": "ok"}

@app.get("/api/relay")
def toggle_relay(ch: int, state: int):
    relay_states[ch] = state
    try:
        r_id = get_safe_int(config.get("relay_id", 8), 8)
        with modbus_lock:
            instr = create_instrument(r_id)
            read_with_retry(instr.write_bit, ch, state, 5, retries=2)
    except: pass
    return {"status": "ok"}

@app.get("/api/trigger_clean")
def trigger_clean_api(key: str, mode: str = None, dur: int = 10, rel: int = 0):
    return trigger_cleaning(key, force_mode=mode, force_dur=dur, force_rel=rel)

@app.get("/api/export_options")
def get_export_options(type: str):
    file_map = {"5min": LOG_5MIN, "1hr": LOG_1HR, "alarm": LOG_ALARM}
    target_file = file_map.get(type)
    
    if not target_file or not os.path.exists(target_file):
        return {"status": "error", "message": "No data available"}
    
    try:
        with open(target_file, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            headers = next(reader, [])
            dates = set()
            for row in reader:
                if row and len(row) > 0:
                    date_part = row[0].split(' ')[0]
                    dates.add(date_part)
        
        return {
            "status": "ok", 
            "columns": headers, 
            "dates": sorted(list(dates), reverse=True)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/export_execute")
async def export_execute(request: Request):
    payload = await request.json()
    log_type = payload.get("type", "5min")
    sel_cols = payload.get("columns", [])
    sel_dates = payload.get("dates", [])
    
    file_map = {"5min": LOG_5MIN, "1hr": LOG_1HR, "alarm": LOG_ALARM}
    target_file = file_map.get(log_type)
    
    if not target_file or not os.path.exists(target_file):
        return {"status": "error"}
        
    try:
        desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
        if not os.path.exists(desktop_path): 
            desktop_path = os.path.expanduser("~") 
            
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_name = f"Export_{log_type}_{timestamp}.csv"
        destination = os.path.join(desktop_path, export_name)
        
        with open(target_file, 'r', encoding='utf-8') as fin, open(destination, 'w', newline='', encoding='utf-8') as fout:
            reader = csv.reader(fin)
            writer = csv.writer(fout)
            
            headers = next(reader, [])
            
            col_indices = [i for i, h in enumerate(headers) if h in sel_cols]
            if 0 not in col_indices and "Time" in headers:
                col_indices.insert(0, headers.index("Time"))
                
            if not col_indices:
                return {"status": "error", "message": "No columns selected"}
            
            writer.writerow([headers[i] for i in col_indices])
            
            for row in reader:
                if not row: continue
                date_part = row[0].split(' ')[0]
                if date_part in sel_dates:
                    writer.writerow([row[i] for i in col_indices if i < len(row)])
                    
        return {"status": "ok", "path": destination}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/get_cal")
def get_cal(sensor_id: int, s_type: str):
    try:
        with modbus_lock:
            instr = create_instrument(sensor_id)
            
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

@app.get("/", response_class=HTMLResponse)
def get_gui():
    return """
    <!DOCTYPE html>
    <html lang="en" class="dark">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
        <title>Smart Farm Pro</title>
        <script src="/static/tailwind.js"></script>
        <script>
            tailwind.config = { darkMode: 'class' }
        </script>
        <script src="/static/chart.js"></script>
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

            input[type="number"]::-webkit-outer-spin-button,
            input[type="number"]::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
            input[type="number"] { -moz-appearance: textfield; }
            .no-select { user-select: none; -webkit-user-select: none; }

            input[type="color"] { -webkit-appearance: none; border: none; padding: 0; background: transparent; }
            input[type="color"]::-webkit-color-swatch-wrapper { padding: 0; }
            input[type="color"]::-webkit-color-swatch { border: 2px solid #cbd5e1; border-radius: 6px; transition: border-color 0.2s; }
            .dark input[type="color"]::-webkit-color-swatch { border-color: #475569; }
            input[type="color"]:hover::-webkit-color-swatch { border-color: #94a3b8; }
            .dark input[type="color"]:hover::-webkit-color-swatch { border-color: #94a3b8; }
        </style>
    </head>
    <body class="h-screen flex flex-col relative bg-slate-100 dark:bg-[#020617] text-slate-800 dark:text-[#f8fafc]">

        <nav class="flex gap-8 px-8 py-4 bg-white dark:bg-slate-900/50 border-b border-slate-300 dark:border-slate-800 items-center shrink-0 z-10 transition-colors">
            <button onclick="showTab('dash')" id="btn-dash" class="nav-btn active text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400" data-i18n="nav_dash">Dashboard</button>
            <button onclick="showTab('trends')" id="btn-trends" class="nav-btn text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400" data-i18n="nav_trends">Trends</button>
            <button onclick="showTab('logs')" id="btn-logs" class="nav-btn text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400" data-i18n="nav_logs">Logs</button>
            <button onclick="showTab('ctrl')" id="btn-ctrl" class="nav-btn text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400" data-i18n="nav_ctrl">Control</button>
            <button onclick="requestAdminTab('eng')" id="btn-eng" class="nav-btn text-sm font-black uppercase tracking-wider text-slate-500 hover:text-cyan-600 dark:hover:text-cyan-400" data-i18n="nav_setup">Setup</button>
            
            <div class="flex-grow"></div>
            
            <div class="flex items-center gap-3 mr-6">
                <button onclick="toggleLang()" class="text-slate-600 dark:text-sky-400 text-sm font-bold bg-slate-200 dark:bg-slate-800/80 px-4 py-1.5 rounded hover:bg-slate-300 dark:hover:bg-slate-700 transition-colors flex items-center border border-slate-300 dark:border-slate-700 shadow-sm">
                    <span id="text-lang" class="text-xs tracking-wider uppercase">ENG</span>
                </button>
                <button onclick="toggleTheme()" id="btn-theme" class="text-slate-600 dark:text-amber-400 text-sm font-bold bg-slate-200 dark:bg-slate-800/80 px-4 py-1.5 rounded hover:bg-slate-300 dark:hover:bg-slate-700 transition-colors flex items-center border border-slate-300 dark:border-slate-700 shadow-sm">
                    <span class="text-xs tracking-wider uppercase" data-i18n="theme">Theme</span>
                </button>
            </div>
            
            <div id="sys-clock" class="text-cyan-700 dark:text-cyan-600/80 font-mono text-sm font-bold mr-6 tracking-widest">----/--/-- --:--:--</div>
            <button onclick="fetch('/api/exit')" class="text-rose-600 dark:text-rose-500 text-sm font-bold bg-rose-100 dark:bg-rose-950/30 px-4 py-1.5 rounded hover:bg-rose-200 dark:hover:bg-rose-900/50 transition-colors border border-rose-200 dark:border-rose-900/50 shadow-sm" data-i18n="exit">EXIT</button>
        </nav>

        <main id="tab-dash" class="p-6 flex gap-4 flex-grow overflow-hidden min-h-0">
            <div class="flex flex-col gap-4 flex-grow min-w-0 min-h-0">
                <div id="dashboard-grid" class="grid gap-4 flex-grow min-h-0"></div>
                <div id="dash-chart-wrapper" class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-3 h-[25vh] min-h-[150px] hidden flex-col shrink-0">
                    <div class="flex justify-between items-center mb-1">
                        <span class="text-[10px] font-bold text-slate-500 uppercase tracking-widest ml-10" data-i18n="mini_trend">MINI TREND</span>
                    </div>
                    <div id="dash-canvas-container" class="relative flex-grow min-h-0 w-full"></div>
                </div>
            </div>
            
            <div class="w-[280px] 2xl:w-[320px] flex flex-col gap-3 shrink-0 min-h-0">
                <h2 class="text-xs font-black text-slate-500 uppercase tracking-widest shrink-0 px-2" data-i18n="sys_status">System Status</h2>
                <div id="status-sidebar" class="overflow-y-auto space-y-3 pr-2 pb-4 flex-grow min-h-0"></div>
            </div>
        </main>
        
        <main id="tab-trends" class="p-6 hidden flex-grow overflow-hidden flex flex-col min-h-0">
            <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg flex-grow p-6 flex flex-col min-h-0">
                <div class="flex justify-between items-center mb-4 shrink-0">
                    <span id="chart-title" class="text-sm font-bold text-slate-500 dark:text-slate-400 uppercase tracking-widest" data-i18n="multi_trend">MULTI-TREND ANALYSIS</span>
                    <button onclick="setChartMode('all')" class="bg-slate-100 dark:bg-slate-800 text-cyan-600 dark:text-cyan-400 text-xs px-4 py-2 rounded font-bold hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors border border-slate-300 dark:border-slate-700" data-i18n="show_all">SHOW ALL LINES</button>
                </div>
                <div id="trend-canvas-container" class="relative flex-grow min-h-0 w-full"></div>
            </div>
        </main>

        <main id="tab-logs" class="p-6 hidden flex-grow overflow-hidden flex flex-col min-h-0 gap-4">
            <div class="flex justify-between items-center shrink-0">
                <div class="flex gap-2">
                    <button onclick="setLogView('5min')" id="btn-v-5min" class="log-tab-btn active px-4 py-2 rounded font-bold text-xs uppercase transition-colors border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400" data-i18n="log_5min">5-Min Data</button>
                    <button onclick="setLogView('1hr')" id="btn-v-1hr" class="log-tab-btn px-4 py-2 rounded font-bold text-xs uppercase transition-colors border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400" data-i18n="log_1hr">1-Hour AVG</button>
                    <button onclick="setLogView('alarm')" id="btn-v-alarm" class="log-tab-btn px-4 py-2 rounded font-bold text-xs uppercase transition-colors border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400" data-i18n="log_alarm">Alarm History</button>
                </div>
                <button id="btn-export" onclick="openExportModal()" class="bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs px-6 py-2 rounded uppercase tracking-wider shadow-lg flex items-center gap-2 transition-colors border border-emerald-700">
                    <span data-i18n="export">Export to Desktop</span>
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
                <h2 class="text-cyan-600 dark:text-cyan-400 font-black text-xl mb-6 uppercase border-b border-slate-300 dark:border-slate-700 pb-4" data-i18n="manual_or">Manual Relay Override</h2>
                <div class="grid grid-cols-2 gap-8">
                    <script>
                        ['Pump 1 (Inlet)', 'Pump 2 (Outlet)', 'Aerator', 'Drain Valve'].forEach((name, i) => {
                            document.write(`
                            <div class="flex items-center justify-between bg-slate-100 dark:bg-slate-800/40 p-5 rounded-xl border border-slate-300 dark:border-slate-700/50">
                                <span class="text-sm font-bold text-slate-700 dark:text-slate-300 uppercase" data-i18n="relay_${i}">${name}</span>
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
                    <div class="flex items-center gap-4">
                        <h2 class="text-cyan-600 dark:text-cyan-400 font-black text-base uppercase flex items-center gap-3">
                            <span data-i18n="dev_net">Device Network Manager</span>
                            <button onclick="lockAdmin()" class="flex items-center bg-rose-100 text-rose-600 hover:bg-rose-200 dark:bg-rose-900/30 dark:text-rose-400 px-3 py-1.5 rounded text-xs font-black tracking-wider transition-colors shadow-sm border border-rose-200 dark:border-rose-800 no-select">
                                <span data-i18n="lock">LOCK</span>
                            </button>
                            <button onclick="openChangePwdModal()" class="flex items-center bg-amber-100 text-amber-700 hover:bg-amber-200 dark:bg-amber-900/30 dark:text-amber-400 px-3 py-1.5 rounded text-xs font-black tracking-wider transition-colors shadow-sm border border-amber-200 dark:border-amber-800 no-select">
                                <span data-i18n="change_pwd">CHANGE PWD</span>
                            </button>
                        </h2>
                    </div>
                    <div class="flex gap-2">
                        <button onclick="openIoModal()" class="flex items-center gap-1.5 bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-300 px-4 py-2 rounded text-sm font-bold hover:bg-slate-300 dark:hover:bg-slate-600 border border-slate-300 dark:border-slate-600 transition-colors shadow-sm">
                            <span data-i18n="io_setup">I/O SETUP</span>
                        </button>
                        <button onclick="openModal()" class="bg-emerald-600 text-white px-4 py-2 rounded text-sm font-bold hover:bg-emerald-500 border border-emerald-700 transition-colors shadow-sm" data-i18n="add_sensor">+ ADD SENSOR</button>
                    </div>
                </div>
                <div id="eng-sensors" class="grid grid-cols-2 gap-4 mt-2 overflow-y-auto pr-2 flex-grow min-h-0 content-start"></div>
            </div>
            
            <div class="flex flex-col gap-6 w-1/3 min-h-0">
                <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-6 flex flex-col gap-4 flex-grow min-h-0">
                    <div class="border-b border-slate-300 dark:border-slate-700 pb-3 flex justify-between items-end shrink-0">
                        <h2 class="text-emerald-600 dark:text-emerald-500 font-black text-base uppercase" data-i18n="ao_scaling">4-20mA Scaling</h2>
                    </div>
                    <div id="eng-ao-scaling" class="overflow-y-auto pr-2 flex-grow min-h-0 content-start flex flex-col gap-3"></div>
                </div>
            </div>
        </main>

        <div id="export-modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-xl w-[600px] p-6 shadow-2xl flex flex-col max-h-[90vh]">
                <h2 class="text-emerald-600 dark:text-emerald-500 font-black text-xl mb-4 shrink-0 uppercase border-b border-slate-200 dark:border-slate-800 pb-3" data-i18n="export_title">Advanced Data Export</h2>
                <div class="flex gap-6 flex-grow min-h-0 overflow-hidden">
                    <div class="w-1/2 flex flex-col min-h-0">
                        <h3 class="text-xs font-black text-slate-500 uppercase tracking-widest mb-2 shrink-0" data-i18n="export_dates">1. Select Dates</h3>
                        <div id="export-dates-container" class="flex-grow overflow-y-auto border border-slate-200 dark:border-slate-700 rounded bg-slate-50 dark:bg-slate-800/50 p-2 space-y-1"></div>
                    </div>
                    <div class="w-1/2 flex flex-col min-h-0">
                        <h3 class="text-xs font-black text-slate-500 uppercase tracking-widest mb-2 shrink-0" data-i18n="export_sensors">2. Select Sensors</h3>
                        <div id="export-cols-container" class="flex-grow overflow-y-auto border border-slate-200 dark:border-slate-700 rounded bg-slate-50 dark:bg-slate-800/50 p-2 space-y-1"></div>
                    </div>
                </div>
                <div class="flex justify-end gap-3 mt-6 shrink-0">
                    <button onclick="closeExportModal()" class="px-5 py-2.5 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 border border-slate-300 dark:border-slate-600" data-i18n="cancel">CANCEL</button>
                    <button id="btn-execute-export" onclick="executeAdvancedExport()" class="px-5 py-2.5 bg-emerald-600 rounded text-sm font-bold text-white hover:bg-emerald-500 border border-emerald-700 shadow-lg" data-i18n="download_csv">DOWNLOAD CSV</button>
                </div>
            </div>
        </div>

        <div id="io-modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-xl w-[400px] p-6 shadow-2xl flex flex-col">
                <h2 class="text-slate-700 dark:text-slate-300 font-black text-xl mb-4 shrink-0 uppercase border-b border-slate-200 dark:border-slate-800 pb-3" data-i18n="io_setup">I/O Modules Setup</h2>
                <div class="flex flex-col gap-4">
                    <div class="bg-slate-50 dark:bg-slate-800/50 p-4 rounded border border-slate-200 dark:border-slate-700">
                        <label class="block text-xs font-black text-slate-500 dark:text-slate-400 mb-2 uppercase tracking-widest" data-i18n="relay_module_id">Relay Module (KM6073) ID</label>
                        <div class="flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                            <button onclick="stepVal('eng-relay-id', -1)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                            <input onchange="triggerSave()" type="number" id="eng-relay-id" class="w-full bg-transparent text-center py-2 text-slate-900 dark:text-white text-lg font-bold outline-none">
                            <button onclick="stepVal('eng-relay-id', 1)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                        </div>
                    </div>
                    <div class="bg-slate-50 dark:bg-slate-800/50 p-4 rounded border border-slate-200 dark:border-slate-700">
                        <label class="block text-xs font-black text-slate-500 dark:text-slate-400 mb-2 uppercase tracking-widest" data-i18n="ao_module_id">Analog Output (KM6023) ID</label>
                        <div class="flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                            <button onclick="stepVal('eng-ao-id', -1)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                            <input onchange="triggerSave()" type="number" id="eng-ao-id" class="w-full bg-transparent text-center py-2 text-slate-900 dark:text-white text-lg font-bold outline-none">
                            <button onclick="stepVal('eng-ao-id', 1)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                        </div>
                    </div>
                </div>
                <div class="flex justify-end gap-3 mt-6">
                    <button onclick="closeIoModal()" class="px-6 py-2.5 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 border border-slate-300 dark:border-slate-600 shadow-sm" data-i18n="close">CLOSE</button>
                </div>
            </div>
        </div>

        <div id="admin-modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-xl w-[350px] p-6 shadow-2xl flex flex-col">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-amber-600 dark:text-amber-500 font-black text-xl uppercase tracking-wider flex items-center gap-2">
                        <span data-i18n="admin_login">Admin Login</span>
                    </h2>
                </div>
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5" data-i18n="password">PASSWORD</label>
                        <input type="password" id="admin-pwd-input" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-white text-base focus:outline-none focus:border-amber-500" placeholder="****" onkeypress="if(event.key === 'Enter') verifyAdmin()">
                        <p id="admin-error" class="text-xs text-rose-500 font-bold mt-2 hidden">Incorrect password!</p>
                    </div>
                </div>
                <div class="flex justify-end gap-3 mt-6">
                    <button onclick="closeAdminModal()" class="px-5 py-2 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 border border-slate-300 dark:border-slate-600 shadow-sm" data-i18n="cancel">CANCEL</button>
                    <button onclick="verifyAdmin()" class="px-5 py-2 bg-amber-600 rounded text-sm font-bold text-white hover:bg-amber-500 border border-amber-700 shadow-sm" data-i18n="unlock">UNLOCK</button>
                </div>
            </div>
        </div>
        
        <div id="pwd-modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-xl w-[350px] p-6 shadow-2xl flex flex-col">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-amber-600 dark:text-amber-500 font-black text-xl uppercase tracking-wider flex items-center gap-2">
                        <span data-i18n="change_pwd">CHANGE PWD</span>
                    </h2>
                </div>
                <div class="space-y-4">
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5" data-i18n="new_pwd">NEW PASSWORD</label>
                        <input type="password" id="new-pwd-input" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-white text-base focus:outline-none focus:border-amber-500" placeholder="***">
                    </div>
                </div>
                <div class="flex justify-end gap-3 mt-6">
                    <button onclick="closeChangePwdModal()" class="px-5 py-2 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 border border-slate-300 dark:border-slate-600 shadow-sm" data-i18n="cancel">CANCEL</button>
                    <button onclick="saveNewPwd()" class="px-5 py-2 bg-amber-600 rounded text-sm font-bold text-white hover:bg-amber-500 border border-amber-700 shadow-sm" data-i18n="save">SAVE</button>
                </div>
            </div>
        </div>

        <div id="modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-xl w-[450px] p-6 shadow-2xl max-h-[90vh] overflow-y-auto flex flex-col">
                <h2 class="text-cyan-600 dark:text-cyan-400 font-black text-xl mb-4 shrink-0 uppercase" data-i18n="add_sensor">Add New Sensor</h2>
                <div class="space-y-4 flex-grow min-h-0">
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5" data-i18n="sensor_type">SENSOR TYPE</label>
                        <select id="new-type" onchange="updateNewSensorUnits()" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-black text-base focus:outline-none focus:border-cyan-500">
                            <option value="mlss">MLSS (Suspended Solids)</option>
                            <option value="uv254">UV254 (COD/BOD)</option>
                            <option value="do">DO (Dissolved Oxygen)</option>
                            <option value="orp">ORP</option>
                            <option value="oil">OIL IN WATER</option>
                            <option value="ph">pH</option>
                            <option value="ec">CONDUCTIVITY (EC)</option>
                            <option value="turbidity">TURBIDITY</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5" data-i18n="unit_label">UNIT</label>
                        <select id="new-unit" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-black text-base focus:outline-none focus:border-cyan-500">
                        </select>
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5" data-i18n="modbus_id">MODBUS ID (1-247)</label>
                        <div class="flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                            <button onclick="stepVal('new-id', -1)" class="w-12 py-2.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                            <input type="number" id="new-id" class="w-full bg-transparent text-center p-2.5 text-slate-900 dark:text-white font-black text-lg outline-none">
                            <button onclick="stepVal('new-id', 1)" class="w-12 py-2.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                        </div>
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5" data-i18n="display_label">DISPLAY LABEL</label>
                        <input type="text" id="new-label" placeholder="e.g. DO 3 (Tank 2)" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-white text-base focus:outline-none focus:border-cyan-500">
                    </div>
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5" data-i18n="chart_color">CHART COLOR</label>
                        <input type="color" id="new-color" class="w-full h-10 bg-transparent border border-slate-300 dark:border-slate-600 rounded cursor-pointer">
                    </div>
                </div>
                <div class="flex justify-end gap-3 mt-6 shrink-0">
                    <button onclick="closeModal()" class="px-5 py-2 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 border border-slate-300 dark:border-slate-600 shadow-sm" data-i18n="cancel">CANCEL</button>
                    <button onclick="confirmAddSensor()" class="px-5 py-2 bg-emerald-600 rounded text-sm font-bold text-white hover:bg-emerald-500 border border-emerald-700 shadow-sm" data-i18n="add_sensor">ADD DEVICE</button>
                </div>
            </div>
        </div>

        <div id="cal-modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border-t-4 border-t-indigo-500 border border-slate-300 dark:border-slate-700 rounded-xl w-[450px] p-6 shadow-2xl flex flex-col max-h-[90vh] overflow-y-auto">
                <div class="flex justify-between items-center mb-4">
                    <h2 id="cal-modal-title" class="text-indigo-600 dark:text-indigo-400 font-black text-xl uppercase tracking-wider">CALIBRATION</h2>
                </div>
                
                <input type="hidden" id="cal-sensor-key">
                <input type="hidden" id="cal-sensor-id">
                <input type="hidden" id="cal-sensor-type">
                
                <div class="mb-6">
                    <h3 class="text-sm font-bold text-slate-800 dark:text-slate-200 uppercase mb-3 border-b border-slate-200 dark:border-slate-700 pb-1" data-i18n="sw_cal">1. HMI Software (y = A*x + B)</h3>
                    <div class="space-y-3">
                        <div class="flex items-center gap-3">
                            <span class="w-20 text-xs font-black text-slate-500 dark:text-slate-400 uppercase tracking-widest text-right">A (Slope)</span>
                            <div class="flex-1 flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                                <button onclick="stepVal('cal-soft-a', -0.01)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                                <input type="number" step="0.0001" id="cal-soft-a" onchange="triggerSave()" class="w-full bg-transparent text-center py-1.5 text-slate-900 dark:text-white text-base font-mono font-bold outline-none">
                                <button onclick="stepVal('cal-soft-a', 0.01)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                            </div>
                        </div>
                        <div class="flex items-center gap-3">
                            <span class="w-20 text-xs font-black text-slate-500 dark:text-slate-400 uppercase tracking-widest text-right">B (Offset)</span>
                            <div class="flex-1 flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                                <button onclick="stepVal('cal-soft-b', -0.1)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                                <input type="number" step="0.0001" id="cal-soft-b" onchange="triggerSave()" class="w-full bg-transparent text-center py-1.5 text-slate-900 dark:text-white text-base font-mono font-bold outline-none">
                                <button onclick="stepVal('cal-soft-b', 0.1)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                            </div>
                        </div>
                    </div>
                </div>

                <div>
                    <h3 class="text-sm font-bold text-rose-600 dark:text-rose-400 uppercase mb-3 border-b border-rose-200 dark:border-rose-900/50 pb-1" data-i18n="hw_cal">2. Sensor Hardware (Modbus)</h3>
                    <div class="space-y-3">
                        <div class="flex items-center gap-3">
                            <span class="w-20 text-xs font-black text-slate-500 dark:text-slate-400 uppercase tracking-widest text-right">K (Span)</span>
                            <div class="flex-1 flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                                <button onclick="stepVal('cal-hw-k', -0.01)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                                <input type="number" step="0.0001" id="cal-hw-k" class="w-full bg-transparent text-center py-1.5 text-slate-900 dark:text-white text-base font-mono font-bold outline-none" placeholder="Reading...">
                                <button onclick="stepVal('cal-hw-k', 0.01)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                            </div>
                        </div>
                        <div class="flex items-center gap-3">
                            <span class="w-20 text-xs font-black text-slate-500 dark:text-slate-400 uppercase tracking-widest text-right">B (Zero)</span>
                            <div class="flex-1 flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                                <button onclick="stepVal('cal-hw-b', -0.1)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                                <input type="number" step="0.0001" id="cal-hw-b" class="w-full bg-transparent text-center py-1.5 text-slate-900 dark:text-white text-base font-mono font-bold outline-none" placeholder="Reading...">
                                <button onclick="stepVal('cal-hw-b', 0.1)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                            </div>
                        </div>
                    </div>
                </div>
                
                <div class="flex justify-end gap-3 mt-8">
                    <button onclick="closeCalModal()" class="px-6 py-2.5 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 border border-slate-300 dark:border-slate-600 shadow-sm" data-i18n="close">CLOSE</button>
                    <button id="cal-save-btn" onclick="saveCalibration()" class="px-5 py-2.5 bg-indigo-600 rounded text-sm font-bold text-white hover:bg-indigo-500 border border-indigo-700 shadow-lg" data-i18n="save_hw_cal">SAVE HW CAL</button>
                </div>
            </div>
        </div>

        <div id="clean-modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border-t-4 border-t-sky-500 border border-slate-300 dark:border-slate-700 rounded-xl w-[400px] p-6 shadow-2xl flex flex-col">
                <div class="flex justify-between items-center mb-4">
                    <h2 id="clean-modal-title" class="text-sky-600 dark:text-sky-400 font-black text-xl uppercase tracking-wider" data-i18n="clean_title">CLEANING SETUP</h2>
                </div>
                
                <input type="hidden" id="clean-sensor-key">
                
                <div class="space-y-5">
                    <div>
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5 uppercase tracking-widest" data-i18n="ctrl_mode">Control Mode</label>
                        <select id="clean-mode" onchange="updateCleanUI(); triggerSave();" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-black font-bold text-sm focus:outline-none focus:border-sky-500">
                            <option value="off" data-i18n="opt_off">OFF (No Cleaning)</option>
                            <option value="internal" data-i18n="opt_int">INTERNAL (Sensor Wiper)</option>
                            <option value="external" data-i18n="opt_ext">EXTERNAL (KM6073 Relay)</option>
                        </select>
                    </div>
                    
                    <div id="clean-int-block" class="hidden">
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5 uppercase tracking-widest" data-i18n="interval_min">Interval (Minutes)</label>
                        <div class="flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                            <button onclick="stepVal('clean-int', -5)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                            <input onchange="triggerSave()" type="number" id="clean-int" class="w-full bg-transparent text-center py-2 text-slate-900 dark:text-white font-black text-base outline-none">
                            <button onclick="stepVal('clean-int', 5)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                        </div>
                    </div>

                    <div id="clean-dur-block" class="hidden">
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5 uppercase tracking-widest" data-i18n="duration_sec">Duration (Seconds)</label>
                        <div class="flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                            <button onclick="stepVal('clean-dur', -1)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                            <input onchange="triggerSave()" type="number" id="clean-dur" class="w-full bg-transparent text-center py-2 text-slate-900 dark:text-white font-black text-base outline-none">
                            <button onclick="stepVal('clean-dur', 1)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                        </div>
                    </div>

                    <div id="clean-rel-block" class="hidden">
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5 uppercase tracking-widest" data-i18n="relay_channel">KM6073 Relay Channel</label>
                        <select id="clean-rel" onchange="triggerSave()" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-black font-bold text-sm focus:outline-none focus:border-sky-500">
                            <option value="0">CH 0 (Pump 1)</option>
                            <option value="1">CH 1 (Pump 2)</option>
                            <option value="2">CH 2 (Aerator)</option>
                            <option value="3">CH 3 (Drain Valve)</option>
                        </select>
                    </div>
                </div>
                
                <div class="flex justify-between items-center mt-8 pt-4 border-t border-slate-200 dark:border-slate-700">
                    <button id="btn-test-clean" onclick="testCleaning()" class="flex items-center px-4 py-2 bg-slate-100 dark:bg-slate-800 text-slate-500 font-bold rounded text-xs border border-slate-300 dark:border-slate-600 hover:bg-slate-200 transition-colors shadow-sm">
                        <span data-i18n="test_now">TEST NOW</span>
                    </button>
                    <button onclick="closeCleanModal()" class="px-6 py-2.5 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 border border-slate-300 dark:border-slate-600 shadow-sm" data-i18n="close">CLOSE</button>
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
            
            const allowedUnits = {
                "mlss": ["mg/L", "g/L", "ppm", "%", "NTU"],
                "uv254": ["mg/L", "ppm"],
                "do": ["mg/L", "ppm", "%"],
                "orp": ["mV"],
                "oil": ["ug/L", "ppb", "mg/L", "ppm"],
                "ph": ["pH"],
                "ec": ["uS/cm", "mS/cm"],
                "turbidity": ["NTU", "FTU", "mg/L"]
            };

            const translations = {
                en: {
                    nav_dash: "Dashboard", nav_trends: "Trends", nav_logs: "Logs", nav_ctrl: "Control", nav_setup: "Setup", theme: "Theme", exit: "EXIT",
                    sys_status: "System Status", mini_trend: "MINI TREND", multi_trend: "MULTI-TREND ANALYSIS", show_all: "SHOW ALL LINES",
                    log_5min: "5-Min Data", log_1hr: "1-Hour AVG", log_alarm: "Alarm History", export: "Export to Desktop",
                    manual_or: "Manual Relay Override", relay_0: "Pump 1 (Inlet)", relay_1: "Pump 2 (Outlet)", relay_2: "Aerator", relay_3: "Drain Valve",
                    dev_net: "Device Network Manager", io_setup: "I/O SETUP", add_sensor: "ADD SENSOR", change_pwd: "CHANGE PWD", ao_scaling: "4-20mA Scaling",
                    export_title: "Advanced Data Export", export_dates: "1. Select Dates", export_sensors: "2. Select Sensors", cancel: "CANCEL", download_csv: "DOWNLOAD CSV",
                    admin_login: "Admin Login", password: "PASSWORD", unlock: "UNLOCK", new_pwd: "NEW PASSWORD", save: "SAVE", close: "CLOSE", lock: "LOCK",
                    sw_cal: "1. HMI Software (y = A*x + B)", hw_cal: "2. Sensor Hardware (Modbus)", save_hw_cal: "SAVE HW CAL",
                    clean_title: "CLEANING SETUP", ctrl_mode: "Control Mode", test_now: "TEST NOW",
                    btn_cal: "CALIBRATION", btn_clean: "CLEANING", cal_title: "CALIBRATION",
                    sensor_type: "SENSOR TYPE", unit_label: "UNIT", modbus_id: "MODBUS ID (1-247)", display_label: "DISPLAY LABEL", chart_color: "CHART COLOR",
                    relay_module_id: "Relay Module (KM6073) ID", ao_module_id: "Analog Output (KM6023) ID",
                    interval_min: "Interval (Minutes)", duration_sec: "Duration (Seconds)", relay_channel: "KM6073 Relay Channel",
                    opt_off: "OFF (No Cleaning)", opt_int: "INTERNAL (Sensor Wiper)", opt_ext: "EXTERNAL (KM6073 Relay)"
                },
                ko: {
                    nav_dash: "대시보드", nav_trends: "트렌드", nav_logs: "로그", nav_ctrl: "제어", nav_setup: "설정", theme: "테마", exit: "종료",
                    sys_status: "시스템 상태", mini_trend: "미니 트렌드", multi_trend: "다중 트렌드 분석", show_all: "모든 라인 보기",
                    log_5min: "5분 데이터", log_1hr: "1시간 평균", log_alarm: "알람 이력", export: "바탕화면 저장",
                    manual_or: "수동 릴레이 제어", relay_0: "펌프 1 (흡입)", relay_1: "펌프 2 (배출)", relay_2: "폭기장치", relay_3: "배수 밸브",
                    dev_net: "장치 네트워크 관리", io_setup: "I/O 설정", add_sensor: "센서 추가", change_pwd: "비밀번호 변경", ao_scaling: "4-20mA 스케일링",
                    export_title: "데이터 추출", export_dates: "1. 날짜 선택", export_sensors: "2. 센서 선택", cancel: "취소", download_csv: "CSV 다운로드",
                    admin_login: "관리자 로그인", password: "비밀번호", unlock: "잠금해제", new_pwd: "새 비밀번호", save: "저장", close: "닫기", lock: "잠금",
                    sw_cal: "1. HMI 소프트웨어 (y = A*x + B)", hw_cal: "2. 센서 하드웨어 (모드버스)", save_hw_cal: "하드웨어 저장",
                    clean_title: "세정(Cleaning) 설정", ctrl_mode: "제어 모드", test_now: "지금 테스트",
                    btn_cal: "교정 (CAL)", btn_clean: "세정 (CLEAN)", cal_title: "센서 교정",
                    sensor_type: "센서 종류", unit_label: "단위", modbus_id: "모드버스 ID (1-247)", display_label: "표시 이름", chart_color: "차트 색상",
                    relay_module_id: "릴레이 모듈 (KM6073) ID", ao_module_id: "아날로그 출력 (KM6023) ID",
                    interval_min: "작동 주기 (분)", duration_sec: "작동 시간 (초)", relay_channel: "KM6073 릴레이 채널",
                    opt_off: "사용 안함 (OFF)", opt_int: "내부 와이퍼 (INTERNAL)", opt_ext: "외부 릴레이 (EXTERNAL)"
                }
            };

            function applyLang() {
                if(!configData || !configData.lang) return;
                const lang = translations[configData.lang] ? configData.lang : 'en';
                document.getElementById('text-lang').innerText = lang === 'en' ? 'ENG' : '한국어';
                
                document.querySelectorAll('[data-i18n]').forEach(el => {
                    const key = el.getAttribute('data-i18n');
                    if(translations[lang][key]) {
                        el.innerText = translations[lang][key];
                    }
                });
            }

            function toggleLang() {
                if(!configData) return;
                configData.lang = configData.lang === 'en' ? 'ko' : 'en';
                applyLang();
                triggerSave();
            }

            function updateNewSensorUnits() {
                const type = document.getElementById('new-type').value;
                const unitSelect = document.getElementById('new-unit');
                unitSelect.innerHTML = allowedUnits[type].map(u => `<option value="${u}">${u}</option>`).join('');
            }

            async function triggerSave() {
                if(!configData) return;
                if(!configData.sensors) configData.sensors = {};
                
                const relayEl = document.getElementById('eng-relay-id');
                if(relayEl) configData.relay_id = parseInt(relayEl.value) || 0;
                const aoEl = document.getElementById('eng-ao-id');
                if(aoEl) configData.ao_id = parseInt(aoEl.value) || 0;
                
                let needsRedraw = false;

                for(const key of Object.keys(configData.sensors)) {
                    const idEl = document.getElementById('id-' + key);
                    if(idEl) configData.sensors[key].id = parseInt(idEl.value) || 1;
                    
                    const enEl = document.getElementById('en-' + key);
                    if(enEl) configData.sensors[key].enabled = enEl.checked;
                    
                    const lblEl = document.getElementById('label-' + key);
                    if(lblEl && configData.sensors[key].label !== lblEl.value) {
                        configData.sensors[key].label = lblEl.value;
                        needsRedraw = true;
                    }
                    
                    const colorEl = document.getElementById('color-' + key);
                    if(colorEl && configData.sensors[key].color !== colorEl.value) {
                        configData.sensors[key].color = colorEl.value;
                        needsRedraw = true;
                    }
                    
                    const unitEl = document.getElementById('unit-' + key);
                    if(unitEl && configData.sensors[key].unit !== unitEl.value) {
                        configData.sensors[key].unit = unitEl.value;
                        needsRedraw = true;
                    }

                    const minIn = document.getElementById('min-' + key);
                    const maxIn = document.getElementById('max-' + key);
                    if(minIn) configData.sensors[key].min = parseFloat(minIn.value);
                    if(maxIn) configData.sensors[key].max = parseFloat(maxIn.value);

                    if(document.getElementById('clean-sensor-key') && document.getElementById('clean-sensor-key').value === key && !document.getElementById('clean-modal-overlay').classList.contains('hidden')) {
                        configData.sensors[key].c_mode = document.getElementById('clean-mode').value;
                        configData.sensors[key].c_int = parseInt(document.getElementById('clean-int').value);
                        configData.sensors[key].c_dur = parseInt(document.getElementById('clean-dur').value);
                        configData.sensors[key].c_rel = parseInt(document.getElementById('clean-rel').value);
                    }

                    if(document.getElementById('cal-sensor-key') && document.getElementById('cal-sensor-key').value === key && !document.getElementById('cal-modal-overlay').classList.contains('hidden')) {
                        configData.sensors[key].a = parseFloat(document.getElementById('cal-soft-a').value);
                        configData.sensors[key].b = parseFloat(document.getElementById('cal-soft-b').value);
                    }
                }
                
                await fetch('/api/save_config', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(configData)
                });

                if (needsRedraw) {
                    initDynamicUI(); 
                }
            }

            function toggleSensorEnabled(key, isChecked) {
                configData.sensors[key].enabled = isChecked;
                triggerSave().then(() => { initDynamicUI(); });
            }

            function stepVal(id, step) {
                const el = document.getElementById(id);
                if(!el) return;
                let val = parseFloat(el.value);
                if(isNaN(val)) val = 0;
                val += step;
                val = Math.round(val * 10000) / 10000;
                el.value = val;
                
                if(id.startsWith('id-') || id.startsWith('min-') || id.startsWith('max-') || id === 'eng-relay-id' || id === 'eng-ao-id' || id.startsWith('clean-') || id.startsWith('cal-soft-')) {
                    triggerSave();
                }
            }

            let isAdmin = false;
            let pendingTab = null;
            let pwdFails = 0;

            function requestAdminTab(tab) {
                if (isAdmin) {
                    showTab(tab);
                } else {
                    pendingTab = tab;
                    document.getElementById('admin-error').classList.add('hidden');
                    document.getElementById('admin-pwd-input').value = '';
                    document.getElementById('admin-modal-overlay').classList.remove('hidden');
                    setTimeout(() => document.getElementById('admin-pwd-input').focus(), 100);
                }
            }

            function verifyAdmin() {
                const pwdInput = document.getElementById('admin-pwd-input');
                const pwd = pwdInput.value;
                const correctPwd = configData.admin_pwd || "1234";
                
                if(pwd === correctPwd) {
                    isAdmin = true;
                    pwdFails = 0;
                    pwdInput.value = "";
                    closeAdminModal();
                    document.getElementById('btn-eng').classList.add('text-amber-500'); 
                    if (pendingTab) {
                        showTab(pendingTab);
                        pendingTab = null;
                    }
                } else {
                    pwdFails++;
                    pwdInput.value = ""; 
                    
                    if(pwdFails >= 5) {
                        configData.admin_pwd = "1234";
                        triggerSave();
                        const errEl = document.getElementById('admin-error');
                        errEl.innerText = configData.lang === 'ko' ? "비밀번호가 1234로 초기화되었습니다!" : "Password reset to default (1234)!";
                        errEl.classList.remove('hidden');
                        pwdFails = 0;
                    } else {
                        const errEl = document.getElementById('admin-error');
                        errEl.innerText = configData.lang === 'ko' ? `비밀번호 오류! (${pwdFails}/5)` : `Incorrect password! (${pwdFails}/5)`;
                        errEl.classList.remove('hidden');
                    }
                }
            }

            function lockAdmin() {
                isAdmin = false;
                document.getElementById('btn-eng').classList.remove('text-amber-500');
                showTab('dash');
            }

            function closeAdminModal() { document.getElementById('admin-modal-overlay').classList.add('hidden'); }

            function openChangePwdModal() {
                document.getElementById('new-pwd-input').value = '';
                document.getElementById('pwd-modal-overlay').classList.remove('hidden');
                setTimeout(() => document.getElementById('new-pwd-input').focus(), 100);
            }
            
            function closeChangePwdModal() { document.getElementById('pwd-modal-overlay').classList.add('hidden'); }
            
            function saveNewPwd() {
                const newPwd = document.getElementById('new-pwd-input').value;
                if(newPwd.trim() === '') {
                    alert(configData.lang === 'ko' ? "비밀번호를 입력하세요." : "Please enter a password.");
                    return;
                }
                configData.admin_pwd = newPwd;
                triggerSave();
                closeChangePwdModal();
            }

            function openIoModal() {
                document.getElementById('eng-relay-id').value = configData.relay_id;
                document.getElementById('eng-ao-id').value = configData.ao_id;
                document.getElementById('io-modal-overlay').classList.remove('hidden');
            }
            function closeIoModal() { document.getElementById('io-modal-overlay').classList.add('hidden'); }

            let isDark = true; 
            let themeInitialized = false;
            let langInitialized = false;
            
            function toggleTheme() {
                const html = document.documentElement;
                isDark = !html.classList.contains('dark');
                if (isDark) {
                    html.classList.add('dark');
                } else {
                    html.classList.remove('dark');
                }
                updateChartColors();
                if(configData) {
                    configData.theme = isDark ? 'dark' : 'light';
                    triggerSave();
                }
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

            async function openExportModal() {
                const btn = document.getElementById('btn-export');
                const origHTML = btn.innerHTML;
                btn.innerHTML = "⏳ LOADING...";
                
                try {
                    const res = await fetch('/api/export_options?type=' + currentLogView);
                    const data = await res.json();
                    
                    if (data.status !== 'ok' || data.dates.length === 0) {
                        btn.innerHTML = "❌ NO DATA YET";
                        btn.classList.add('bg-rose-600', 'border-rose-700');
                        btn.classList.remove('bg-emerald-600', 'border-emerald-700', 'hover:bg-emerald-500');
                        setTimeout(() => { 
                            btn.innerHTML = origHTML; 
                            btn.classList.remove('bg-rose-600', 'border-rose-700');
                            btn.classList.add('bg-emerald-600', 'border-emerald-700', 'hover:bg-emerald-500');
                        }, 2000);
                        return;
                    }
                    
                    let datesHtml = '';
                    data.dates.forEach(d => {
                        datesHtml += `
                        <label class="flex items-center gap-3 p-2 hover:bg-slate-200 dark:hover:bg-slate-700 rounded cursor-pointer transition-colors">
                            <input type="checkbox" class="export-date-cb w-4 h-4 text-emerald-600 rounded border-gray-300 focus:ring-emerald-500" value="${d}" checked>
                            <span class="text-sm font-bold text-slate-700 dark:text-slate-300">${d}</span>
                        </label>`;
                    });
                    document.getElementById('export-dates-container').innerHTML = datesHtml;
                    
                    let colsHtml = '';
                    data.columns.forEach(c => {
                        if(c === 'Time') return; 
                        colsHtml += `
                        <label class="flex items-center gap-3 p-2 hover:bg-slate-200 dark:hover:bg-slate-700 rounded cursor-pointer transition-colors">
                            <input type="checkbox" class="export-col-cb w-4 h-4 text-emerald-600 rounded border-gray-300 focus:ring-emerald-500" value="${c}" checked>
                            <span class="text-sm font-bold text-slate-700 dark:text-slate-300">${c}</span>
                        </label>`;
                    });
                    document.getElementById('export-cols-container').innerHTML = colsHtml;
                    
                    document.getElementById('export-modal-overlay').classList.remove('hidden');
                } catch (e) {
                    btn.innerHTML = "❌ ERROR";
                    btn.classList.add('bg-rose-600', 'border-rose-700');
                    btn.classList.remove('bg-emerald-600', 'border-emerald-700', 'hover:bg-emerald-500');
                    setTimeout(() => { 
                        btn.innerHTML = origHTML; 
                        btn.classList.remove('bg-rose-600', 'border-rose-700');
                        btn.classList.add('bg-emerald-600', 'border-emerald-700', 'hover:bg-emerald-500');
                    }, 2000);
                }
                
                btn.innerHTML = origHTML;
            }

            function closeExportModal() { document.getElementById('export-modal-overlay').classList.add('hidden'); }

            async function executeAdvancedExport() {
                const dateCbs = document.querySelectorAll('.export-date-cb:checked');
                const colCbs = document.querySelectorAll('.export-col-cb:checked');
                
                const selectedDates = Array.from(dateCbs).map(cb => cb.value);
                const selectedCols = Array.from(colCbs).map(cb => cb.value);
                
                const btn = document.getElementById('btn-execute-export');
                const origText = btn.innerText;

                if (selectedDates.length === 0 || selectedCols.length === 0) {
                    btn.innerText = "❌ SELECT OPTIONS";
                    btn.classList.replace('bg-emerald-600', 'bg-rose-600');
                    setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-emerald-600'); }, 2000);
                    return;
                }
                
                btn.innerText = "⏳ SAVING...";
                
                try {
                    const res = await fetch('/api/export_execute', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ type: currentLogView, dates: selectedDates, columns: selectedCols })
                    });
                    const data = await res.json();
                    
                    if (data.status === 'ok') {
                        btn.innerText = "✅ SAVED TO DESKTOP";
                        btn.classList.replace('bg-emerald-600', 'bg-cyan-600');
                        setTimeout(() => { closeExportModal(); btn.innerText = origText; btn.classList.replace('bg-cyan-600', 'bg-emerald-600'); }, 1500);
                    } else {
                        btn.innerText = "❌ FAILED";
                        btn.classList.replace('bg-emerald-600', 'bg-rose-600');
                        setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-emerald-600'); }, 2000);
                    }
                } catch (e) {
                    btn.innerText = "❌ ERROR";
                    btn.classList.replace('bg-emerald-600', 'bg-rose-600');
                    setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-emerald-600'); }, 2000);
                }
            }

            function openModal() { 
                document.getElementById('new-type').value = 'mlss';
                updateNewSensorUnits(); 
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
                const unit = document.getElementById('new-unit').value;
                const newKey = 's_' + Date.now();
                configData.sensors[newKey] = { id: id, type: type, enabled: true, label: label, color: color, unit: unit, min: 0, max: 100, a: 1.0, b: 0.0, c_mode: "off", c_int: 30, c_dur: 10, c_rel: 0 };
                triggerSave().then(() => { initDynamicUI(); });
                closeModal();
            }

            function deleteSensor(key) {
                if(confirm(configData.lang === 'ko' ? '이 센서를 삭제하시겠습니까?' : 'Are you sure you want to delete this sensor?')) {
                    delete configData.sensors[key];
                    triggerSave().then(() => { initDynamicUI(); });
                }
            }

            async function openCalModal(key, id, type, label) {
                document.getElementById('cal-sensor-key').value = key;
                document.getElementById('cal-sensor-id').value = id;
                document.getElementById('cal-sensor-type').value = type;
                
                const safeLabel = label ? label.replace(/'/g, "\\'") : "SENSOR";
                document.getElementById('cal-modal-title').innerHTML = safeLabel + ` <span data-i18n="cal_title"></span>`;
                applyLang(); 
                
                const s = configData.sensors[key];
                document.getElementById('cal-soft-a').value = s.a !== undefined ? s.a : 1.0;
                document.getElementById('cal-soft-b').value = s.b !== undefined ? s.b : 0.0;

                document.getElementById('cal-hw-k').value = '';
                document.getElementById('cal-hw-b').value = '';
                document.getElementById('cal-hw-k').placeholder = 'Reading...';
                document.getElementById('cal-hw-b').placeholder = 'Reading...';
                
                document.getElementById('cal-modal-overlay').classList.remove('hidden');

                try {
                    const res = await fetch(`/api/get_cal?sensor_id=${id}&s_type=${type}`);
                    const data = await res.json();
                    if (data.status === 'ok') {
                        document.getElementById('cal-hw-k').value = data.k;
                        document.getElementById('cal-hw-b').value = data.b;
                    } else {
                        document.getElementById('cal-hw-k').placeholder = 'Error';
                        document.getElementById('cal-hw-b').placeholder = 'Error';
                    }
                } catch (e) {
                    document.getElementById('cal-hw-k').placeholder = 'Timeout';
                    document.getElementById('cal-hw-b').placeholder = 'Timeout';
                }
            }

            function closeCalModal() { document.getElementById('cal-modal-overlay').classList.add('hidden'); }

            async function saveCalibration() {
                const id = document.getElementById('cal-sensor-id').value;
                const type = document.getElementById('cal-sensor-type').value;
                const hwK = parseFloat(document.getElementById('cal-hw-k').value);
                const hwB = parseFloat(document.getElementById('cal-hw-b').value);
                
                const btn = document.getElementById('cal-save-btn');

                if (isNaN(hwK) || isNaN(hwB)) {
                    alert(configData.lang === 'ko' ? "올바른 숫자를 입력하세요." : "Please enter valid numeric values.");
                    return;
                }

                await triggerSave(); 

                const origText = btn.innerText;
                btn.innerText = "WRITING...";

                try {
                    const res = await fetch(`/api/set_cal?sensor_id=${id}&s_type=${type}&k=${hwK}&b=${hwB}`);
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
                        btn.innerText = "❌ HW FAILED";
                        btn.classList.replace('bg-indigo-600', 'bg-rose-600');
                        setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-indigo-600'); }, 2000);
                    }
                } catch(e) {
                    btn.innerText = "❌ HW FAILED";
                    btn.classList.replace('bg-indigo-600', 'bg-rose-600');
                    setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-indigo-600'); }, 2000);
                }
            }

            function openCleanModal(key, label) {
                document.getElementById('clean-sensor-key').value = key;
                
                const safeLabel = label ? label.replace(/'/g, "\\'") : "SENSOR";
                document.getElementById('clean-modal-title').innerHTML = safeLabel + ` <span data-i18n="clean_title"></span>`;
                applyLang();
                
                const s = configData.sensors[key];
                document.getElementById('clean-mode').value = s.c_mode || 'off';
                document.getElementById('clean-int').value = s.c_int !== undefined ? s.c_int : 30;
                document.getElementById('clean-dur').value = s.c_dur !== undefined ? s.c_dur : 10;
                document.getElementById('clean-rel').value = s.c_rel !== undefined ? s.c_rel : 0;
                
                updateCleanUI();
                document.getElementById('clean-modal-overlay').classList.remove('hidden');
            }

            function updateCleanUI() {
                const mode = document.getElementById('clean-mode').value;
                const intBlock = document.getElementById('clean-int-block');
                const durBlock = document.getElementById('clean-dur-block');
                const relBlock = document.getElementById('clean-rel-block');
                
                if (mode === 'off') {
                    intBlock.classList.add('hidden');
                    durBlock.classList.add('hidden');
                    relBlock.classList.add('hidden');
                } else if (mode === 'internal') {
                    intBlock.classList.remove('hidden');
                    durBlock.classList.add('hidden');
                    relBlock.classList.add('hidden');
                } else if (mode === 'external') {
                    intBlock.classList.remove('hidden');
                    durBlock.classList.remove('hidden');
                    relBlock.classList.remove('hidden');
                }
            }

            function closeCleanModal() { document.getElementById('clean-modal-overlay').classList.add('hidden'); }

            async function testCleaning() {
                const key = document.getElementById('clean-sensor-key').value;
                const mode = document.getElementById('clean-mode').value;
                const dur = parseInt(document.getElementById('clean-dur').value) || 10;
                const rel = parseInt(document.getElementById('clean-rel').value) || 0;
                
                const btn = document.getElementById('btn-test-clean');
                const origHTML = btn.innerHTML;
                const origClasses = btn.className;
                
                btn.innerHTML = "⏳ RUNNING...";
                btn.className = "flex items-center px-4 py-2 bg-sky-500 text-white font-bold rounded text-xs shadow transition-colors";

                await triggerSave();

                try {
                    const res = await fetch(`/api/trigger_clean?key=${key}&mode=${mode}&dur=${dur}&rel=${rel}`);
                    const data = await res.json();
                    
                    if (data.status === 'ok') {
                        btn.innerHTML = "✅ TRIGGERED";
                        btn.classList.replace('bg-sky-500', 'bg-emerald-500');
                    } else {
                        btn.innerHTML = "❌ FAILED";
                        btn.classList.replace('bg-sky-500', 'bg-rose-500');
                    }
                } catch(e) {
                    btn.innerHTML = "❌ ERROR";
                    btn.classList.replace('bg-sky-500', 'bg-rose-500');
                }
                
                setTimeout(() => { 
                    btn.innerHTML = origHTML; 
                    btn.className = origClasses; 
                }, 2000);
            }

            function focusChart(mode) { setChartMode(mode); showTab('trends'); }

            function setChartMode(mode) {
                currentChartMode = mode;
                document.querySelectorAll('.sensor-card').forEach(c => c.classList.remove('active-chart'));
                
                if (mode === 'all') {
                    if(charts[0]) charts[0].options.plugins.legend.display = false; 
                    if(charts[1]) charts[1].options.plugins.legend.display = true;
                    charts.forEach(c => {
                        c.data.datasets.forEach(ds => { ds.hidden = !(configData.sensors[ds.id] && configData.sensors[ds.id].enabled); });
                        c.update('none'); 
                    });
                } else {
                    const el = document.getElementById('card-' + mode);
                    if(el) el.classList.add('active-chart');
                    if(charts[0]) charts[0].options.plugins.legend.display = false;
                    if(charts[1]) charts[1].options.plugins.legend.display = false;
                    charts.forEach(c => {
                        c.data.datasets.forEach(ds => { ds.hidden = (ds.id !== mode); });
                        c.update('none');
                    });
                }
            }

            function initDynamicUI() {
                if (!configData.sensors) configData.sensors = {};
                
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

                if (count === 0) {
                    const textNoSensors = configData.lang === 'ko' ? "활성화된 센서가 없습니다. 설정(SETUP)으로 이동하세요." : "NO SENSORS ENABLED. GO TO SETUP.";
                    grid.innerHTML = `<div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-10 flex items-center justify-center text-slate-500 font-bold col-span-full">${textNoSensors}</div>`;
                }

                let valSize, unitSize, lblSize;
                if (count === 1) { valSize = '22vh'; unitSize = '5vh'; lblSize = '3vh'; }
                else if (count === 2) { valSize = '14vh'; unitSize = '4vh'; lblSize = '2.5vh'; }
                else if (count <= 4) { valSize = '10vh'; unitSize = '3vh'; lblSize = '2vh'; }
                else { valSize = '7vh'; unitSize = '2vh'; lblSize = '1.5vh'; }

                activeSensors.forEach(([key, s]) => {
                    const unit = s.unit || ""; 
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

                let engHTML = '';
                let aoHTML = '';

                for(const [key, s] of Object.entries(configData.sensors)) {
                    const lang = configData.lang === 'ko' ? 'ko' : 'en';
                    const unitOptions = allowedUnits[s.type] ? allowedUnits[s.type].map(u => `<option value="${u}" ${s.unit === u ? 'selected' : ''}>${u}</option>`).join('') : `<option value="">--</option>`;
                    const safeLabel = s.label ? s.label.replace(/'/g, "\\'") : "SENSOR";

                    engHTML += `
                    <div class="bg-slate-100 dark:bg-slate-800/40 p-4 rounded-lg border border-slate-300 dark:border-slate-700/50 h-auto flex flex-col justify-between">
                        <div class="flex justify-between items-center border-b border-slate-300 dark:border-slate-700/50 pb-3 mb-3">
                            <div class="flex items-center gap-3 w-full pr-2">
                                <label class="relative inline-flex items-center cursor-pointer shrink-0">
                                    <input type="checkbox" id="en-${key}" onchange="toggleSensorEnabled('${key}', this.checked)" ${s.enabled ? 'checked' : ''} class="sr-only peer">
                                    <div class="w-12 h-6 bg-slate-300 dark:bg-slate-900 rounded-full border border-slate-400 dark:border-slate-600 peer-checked:bg-emerald-500 transition-colors after:absolute after:top-[1px] after:left-[2px] after:bg-white dark:after:bg-slate-400 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-[24px] peer-checked:after:bg-white"></div>
                                </label>
                                <input type="color" id="color-${key}" onchange="triggerSave()" value="${s.color}" class="bg-transparent w-6 h-6 rounded cursor-pointer shrink-0" title="Change Sensor Color">
                                <input id="label-${key}" onchange="triggerSave()" type="text" value="${s.label}" class="editable-label text-sm font-black uppercase truncate flex-1 min-w-[50px] px-1 text-slate-800 dark:text-white" style="color: ${s.color}">
                            </div>
                            <button onclick="deleteSensor('${key}')" class="text-rose-500 hover:text-rose-600 dark:hover:text-rose-400 shrink-0"><svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-5 h-5"><path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" /></svg></button>
                        </div>
                        
                        <div class="flex justify-between items-center mt-2">
                            <span class="text-[11px] text-slate-500 font-bold tracking-widest uppercase">${s.type}</span>
                            <div class="flex items-center">
                                <div class="flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                                    <span class="px-2 py-1 bg-transparent text-xs font-black text-slate-500 dark:text-slate-400 uppercase tracking-wider border-r border-slate-300 dark:border-slate-600">ID</span>
                                    <button onclick="stepVal('id-${key}', -1)" class="w-7 py-1 bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                                    <input id="id-${key}" onchange="triggerSave()" type="number" value="${s.id}" class="w-8 text-center bg-transparent text-slate-800 dark:text-white font-black text-xs outline-none no-spin">
                                    <button onclick="stepVal('id-${key}', 1)" class="w-7 py-1 bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                                </div>
                                
                                <div class="ml-3 pl-3 border-l border-slate-300 dark:border-slate-700">
                                    <select id="unit-${key}" onchange="triggerSave()" class="bg-white dark:bg-slate-200 border border-slate-300 dark:border-slate-600 rounded py-1 px-2 text-slate-900 dark:text-black font-bold text-xs shadow-sm focus:outline-none focus:ring-1 focus:ring-cyan-500 cursor-pointer">
                                        ${unitOptions}
                                    </select>
                                </div>
                            </div>
                        </div>
                        
                        <div class="mt-4 pt-4 border-t border-slate-300 dark:border-slate-700/50 flex gap-2">
                            <button onclick="openCalModal('${key}', ${s.id}, '${s.type}', '${safeLabel}')" class="flex-1 bg-indigo-100 text-indigo-700 hover:bg-indigo-200 dark:bg-indigo-900/40 dark:text-indigo-400 dark:hover:bg-indigo-800/60 py-2 rounded text-[11px] font-black uppercase tracking-wider border border-indigo-200 dark:border-indigo-800 transition-colors shadow-sm flex items-center justify-center gap-1.5">
                                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" d="M10.5 6h9.75M10.5 6a1.5 1.5 0 11-3 0m3 0a1.5 1.5 0 10-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-9.75 0h9.75" /></svg>
                                <span>${translations[lang].btn_cal}</span>
                            </button>
                            <button onclick="openCleanModal('${key}', '${safeLabel}')" class="flex-1 bg-sky-100 text-sky-700 hover:bg-sky-200 dark:bg-sky-900/40 dark:text-sky-400 dark:hover:bg-sky-800/60 py-2 rounded text-[11px] font-black uppercase tracking-wider border border-sky-200 dark:border-sky-800 transition-colors shadow-sm flex items-center justify-center gap-1.5">
                                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 001.423 1.423l1.183.394-1.183.394a2.25 2.25 0 00-1.423 1.423z" /></svg>
                                <span>${translations[lang].btn_clean}</span>
                            </button>
                        </div>
                    </div>`;
                    
                    if(s.enabled) {
                        aoHTML += `
                        <div class="bg-slate-100 dark:bg-slate-800/30 p-4 rounded-lg border border-slate-300 dark:border-slate-700/50 shrink-0">
                            <div class="text-sm font-bold truncate mb-3" style="color: ${s.color}">${s.label}</div>
                            <div class="flex gap-4">
                                <div class="flex-1 flex flex-col gap-2">
                                    <span class="text-xs text-slate-500 font-bold tracking-widest uppercase">4mA Limit</span>
                                    <div class="flex items-center bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm">
                                        <button onclick="stepVal('min-${key}', -1)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                                        <input id="min-${key}" onchange="triggerSave()" type="number" value="${s.min}" class="w-full text-center bg-transparent text-slate-800 dark:text-white font-black text-base outline-none">
                                        <button onclick="stepVal('min-${key}', 1)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                                    </div>
                                </div>
                                <div class="flex-1 flex flex-col gap-2">
                                    <span class="text-xs text-slate-500 font-bold tracking-widest uppercase">20mA Limit</span>
                                    <div class="flex items-center bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm">
                                        <button onclick="stepVal('max-${key}', -1)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                                        <input id="max-${key}" onchange="triggerSave()" type="number" value="${s.max}" class="w-full text-center bg-transparent text-slate-800 dark:text-white font-black text-base outline-none">
                                        <button onclick="stepVal('max-${key}', 1)" class="w-10 py-1.5 bg-slate-100 dark:bg-slate-700 text-slate-600 dark:text-slate-300 font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                                    </div>
                                </div>
                            </div>
                        </div>`;
                    }
                }
                document.getElementById('eng-sensors').innerHTML = engHTML;
                document.getElementById('eng-ao-scaling').innerHTML = aoHTML;
                
                isInitialized = true;
                applyLang();
                setChartMode('all');
            }

            async function update() {
                try {
                    const res = await fetch('/api/all');
                    const d = await res.json();
                    
                    if(!isInitialized) { 
                        configData = d.config; 
                        if (!configData.sensors) configData.sensors = {}; 
                        
                        if (configData.theme && !themeInitialized) {
                            themeInitialized = true;
                            if (configData.theme === 'light') {
                                document.documentElement.classList.remove('dark');
                                document.getElementById('theme-icon').innerHTML = iconMoon;
                                isDark = false;
                            } else {
                                document.documentElement.classList.add('dark');
                                document.getElementById('theme-icon').innerHTML = iconSun;
                                isDark = true;
                            }
                            updateChartColors();
                        }
                        if (configData.lang && !langInitialized) {
                            langInitialized = true;
                            applyLang();
                        }
                        
                        initDynamicUI(); 
                    }

                    if(currentTab === 'dash' || currentTab === 'trends') {
                        let sidebarHTML = '';
                        const activeSensors = Object.entries(configData.sensors || {}).filter(([k, v]) => v.enabled);
                        
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

            setInterval(update, 1000);
        </script>
    </body>
    </html>
    """

def run_api(): uvicorn.run(app, host="127.0.0.1", port=5000, log_level="critical")

if __name__ == "__main__":
    threading.Thread(target=modbus_worker, daemon=True).start()
    threading.Thread(target=cleaning_worker, daemon=True).start()
    threading.Thread(target=run_api, daemon=True).start()
    time.sleep(1)
    webview.create_window("WATER ANALYZER PRO", "http://127.0.0.1:5000", fullscreen=True)
    webview.start()
