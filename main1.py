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
import glob
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

def get_active_port():
    # 1. Сначала ищем стандартные USB-свистки
    ports = glob.glob('/dev/ttyUSB*')
    if ports:
        ports.sort()
        return ports[0]
        
    # 2. Если USB нет, проверяем системный UART (для таких HAT-модулей, как на фото)
    if os.path.exists('/dev/serial0'):
        return '/dev/serial0'
    elif os.path.exists('/dev/ttyAMA0'):
        return '/dev/ttyAMA0'
        
    # 3. Резервный вариант
    return '/dev/ttyUSB0'

PORT = get_active_port()

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
    "relay_name": "KM6063 Relay Module",
    "ao_name": "KM6023 Analog Output",
    "sys_temp": {"sensor": "", "ch": 4, "min": 0, "max": 50},
    "relay_actions": {
        "r_0": {"label": "Pump 1 (Inlet)", "modbus_id": 8, "ch": 0, "enabled": True},
        "r_1": {"label": "Pump 2 (Outlet)", "modbus_id": 8, "ch": 1, "enabled": True},
        "r_2": {"label": "Aerator", "modbus_id": 8, "ch": 2, "enabled": True},
        "r_3": {"label": "Drain Valve", "modbus_id": 8, "ch": 3, "enabled": True}
    },
    "sensors": {
        "s_1": {"id": 15, "type": "mlss", "enabled": True, "label": "MLSS", "color": "#94a3b8", "unit": "mg/L", "min": 0, "max": 10000, "a": 1.0, "b": 0.0, "c_mode": "off", "c_int": 30, "c_dur": 10, "c_rel_key": ""},
        "s_2": {"id": 16, "type": "uv254", "enabled": True, "label": "UV254 (COD)", "color": "#3b82f6", "unit": "mg/L", "min": 0, "max": 100, "a": 1.0, "b": 0.0, "c_mode": "off", "c_int": 30, "c_dur": 10, "c_rel_key": ""}
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
                if "relay_id" not in cfg: cfg["relay_id"] = 8
                if "relay_name" not in cfg: cfg["relay_name"] = "KM6063 Relay Module"
                if "ao_name" not in cfg: cfg["ao_name"] = "KM6023 Analog Output"
                if "sys_temp" not in cfg: cfg["sys_temp"] = {"sensor": "", "ch": 4, "min": 0, "max": 50}
                if "ao_map" not in cfg: cfg["ao_map"] = {str(i): "off" for i in range(8)}
                if "export_path" not in cfg: cfg["export_path"] = ""
                
                if "relay_actions" not in cfg:
                    old_relays = cfg.pop("relays", {})
                    cfg["relay_actions"] = {}
                    for k, v in old_relays.items():
                        if int(k) < 4: 
                            cfg["relay_actions"][f"r_{k}"] = {
                                "label": v.get("label", f"Relay {k}"),
                                "modbus_id": cfg.get("relay_id", 8),
                                "ch": int(k),
                                "enabled": v.get("enabled", True)
                            }
                    if not cfg["relay_actions"]:
                        cfg["relay_actions"] = DEFAULT_CONFIG["relay_actions"]
                
                if "sensors" not in cfg: cfg["sensors"] = {} 
                
                for k, v in cfg.get("sensors", {}).items():
                    s_type = v.get("type", "mlss")
                    if "unit" not in v:
                        default_units = {"mlss":"mg/L", "uv254":"mg/L", "do":"mg/L", "orp":"mV", "oil":"ug/L", "ph":"pH", "ec":"uS/cm", "turbidity":"NTU"}
                        v["unit"] = default_units.get(s_type, "")
                    if "min" not in v: v["min"] = 0
                    if "max" not in v: v["max"] = 100
                    if "a" not in v: v["a"] = 1.0
                    if "b" not in v: v["b"] = 0.0
                    if "c_mode" not in v: v["c_mode"] = "off"
                    if "c_int" not in v: v["c_int"] = 30
                    if "c_dur" not in v: v["c_dur"] = 10
                    if "c_rel_key" not in v: v["c_rel_key"] = ""
                    if "contam" not in v: v["contam"] = {"enabled": False, "months": 3, "start_ts": time.time()}
                return cfg
        except: return DEFAULT_CONFIG
    return DEFAULT_CONFIG

def save_config(cfg):
    with open(CONFIG_FILE, 'w') as f: json.dump(cfg, f)

config = load_config()

sensor_data = {"sys_temp": "--", "sys_temp_ao": "0.00"}
history_data = {}
relay_states = {} 
ao_manual = {i: {"active": False, "val": 4.0} for i in range(8)}

hourly_buffer = {}
alarm_states = {}

clean_last_run = {}
clean_relay_off = {}

def init_data_structures():
    global sensor_data, history_data, hourly_buffer, alarm_states, clean_last_run, clean_relay_off, relay_states
    sensor_data = {"status": "Online", "sys_temp": "--", "sys_temp_ao": "0.00"}
    history_data = {}
    hourly_buffer = {}
    alarm_states = {}
    clean_last_run = {}
    clean_relay_off = {}
    
    relay_states = {k: 0 for k in config.get("relay_actions", {}).keys()}
    
    for key, s in config.get("sensors", {}).items():
        if s["type"] == "uv254":
            sensor_data[key] = {"val": "--", "cod": "--", "log_val": "--", "temp": "--", "turb": "--", "ao": "--", "status": "WAIT", "contam_pct": 0}
        else:
            sensor_data[key] = {"val": "--", "log_val": "--", "temp": "--", "ao": "--", "status": "WAIT", "contam_pct": 0}
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

_shared_instr = None

def create_instrument(sensor_id):
    global _shared_instr
    if _shared_instr is None:
        _shared_instr = minimalmodbus.Instrument(PORT, int(sensor_id))
        _shared_instr.serial.baudrate = 9600
        _shared_instr.serial.bytesize = 8
        _shared_instr.serial.parity = minimalmodbus.serial.PARITY_NONE
        _shared_instr.serial.stopbits = 2  
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
            
def trim_csv_log(filepath, max_rows=50000):
    if not os.path.exists(filepath): return
    try:
        if os.path.getsize(filepath) < 3 * 1024 * 1024: return 
        with open(filepath, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        if len(lines) > max_rows:
            headers = lines[0]
            keep = lines[-(max_rows-1):]
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(headers)
                f.writelines(keep)
    except: pass

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

def trigger_cleaning(key, force_mode=None, force_dur=None, force_rel_key=None):
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
            c_rel_key = force_rel_key if force_rel_key is not None else s.get("c_rel_key", "")
            c_dur = force_dur if force_dur is not None else get_safe_int(s.get("c_dur", 10))
            
            act = config.get("relay_actions", {}).get(c_rel_key)
            if act:
                with modbus_lock:
                    instr = create_instrument(act["modbus_id"])
                    read_with_retry(instr.write_bit, act["ch"], 1, 5, retries=2)
                clean_relay_off[c_rel_key] = time.time() + c_dur
                relay_states[c_rel_key] = 1 
            
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
                
        for act_key, off_time in list(clean_relay_off.items()):
            if now >= off_time:
                act = config.get("relay_actions", {}).get(act_key)
                if act:
                    try:
                        with modbus_lock:
                            instr = create_instrument(act["modbus_id"])
                            read_with_retry(instr.write_bit, act["ch"], 0, 5, retries=2)
                        relay_states[act_key] = 0
                    except: pass
                del clean_relay_off[act_key]
                
        time.sleep(1)

def modbus_worker():
    global sensor_data, history_data, hourly_buffer, alarm_states
    last_5min_minute = -1
    last_1hr_hour = -1
    
    while True:
        now = datetime.now()
        all_keys = list(config.get("sensors", {}).keys())
        active_keys = [k for k in all_keys if config["sensors"][k].get("enabled")]
        auto_ao_out = {0: 4.0, 1: 4.0, 2: 4.0, 3: 4.0}
        
        try:
            sensors_cfg = list(config.get("sensors", {}).items())
            for key, s in sensors_cfg:
                if key not in sensor_data or key not in history_data: continue
                if not s.get("enabled", False):
                    if s["type"] == "uv254": sensor_data[key] = {"val": "Off", "temp": "--", "turb": "--", "ao": "0.00", "status": "OFF"}
                    else: sensor_data[key] = {"val": "Off", "temp": "--", "ao": "0.00", "status": "OFF"}
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
                            try:
                                regs = read_with_retry(instr.read_registers, 9728, 4, 3)
                                t_val = decode_dcba(regs[0:2])
                                raw_val = decode_dcba(regs[2:4])
                                sensor_data[key]["temp"] = f"{t_val:.1f}"
                            except:
                                c_r = read_with_retry(instr.read_registers, 9730, 2, 3)
                                raw_val = decode_dcba(c_r)
                            tr_r = read_with_retry(instr.read_registers, 4608, 2, 3)
                            sensor_data[key]["turb"] = f"{decode_dcba(tr_r):.2f}"
                        elif s_type in ["orp", "oil", "do", "ph", "ec", "turbidity"]:
                            read_with_retry(instr.read_register, 12288, 0, 3)
                            try:
                                regs = read_with_retry(instr.read_registers, 9728, 4, 3)
                                t_val = decode_dcba(regs[0:2])
                                raw_val = decode_dcba(regs[2:4])
                                sensor_data[key]["temp"] = f"{t_val:.1f}"
                            except:
                                raw_val = decode_dcba(read_with_retry(instr.read_registers, 9730, 2, 3))
                        
                        val_num = None
                        base_val = None
                        if raw_val is not None:
                            a_val = float(s.get("a", 1.0))
                            b_val = float(s.get("b", 0.0))
                            base_val = (raw_val * a_val) + b_val
                            
                            val_num = base_val
                            if s_type == "oil" and s_unit in ["mg/L", "ppm"]: val_num = base_val / 1000.0
                            elif s_type == "mlss" and s_unit == "g/L": val_num = base_val / 1000.0
                            elif s_type == "mlss" and s_unit == "%": val_num = base_val / 10000.0
                            elif s_type == "ec" and s_unit == "mS/cm": val_num = base_val / 1000.0
                        
                        # --- ЛОГИКА ЗАГРЯЗНЕНИЯ (ИЗНОСА) ---
                        contam_cfg = s.get("contam", {})
                        contam_pct = 0
                        is_contam_err = False
                        
                        if contam_cfg.get("enabled"):
                            elapsed = time.time() - contam_cfg.get("start_ts", time.time())
                            total_sec = int(contam_cfg.get("months", 3)) * 30 * 86400
                            if total_sec > 0:
                                contam_pct = min(99, int((elapsed / total_sec) * 100))
                            if contam_pct >= 99:
                                is_contam_err = True
                        
                        sensor_data[key]["contam_pct"] = contam_pct

                        if is_contam_err:
                            val_num = None
                            sensor_data[key]["val"] = "Err"
                            if s_type == "uv254": 
                                sensor_data[key]["cod"] = "Err"
                            sensor_data[key]["log_val"] = "Err"
                            sensor_data[key]["status"] = "CONTAM"
                        else:
                            fmt = "{:.1f}" if s_type == "orp" else "{:.2f}"
                            sensor_data[key]["val"] = fmt.format(val_num)
                            if s_type == "uv254": sensor_data[key]["cod"] = fmt.format(val_num)
                            
                            # CSV 정합성: 항상 RAW Base Value 저장
                            sensor_data[key]["log_val"] = fmt.format(base_val)
                        
                        min_v = float(s.get("min", 0))
                        max_v = float(s.get("max", 100))
                        
                        if val_num is not None:
                            # 1시간 평균도 RAW 데이터 기준으로 쌓음
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
                            
                    if len(history_data[key]) > 0: history_data[key].pop(0)
                    history_data[key].append(val_num)
                    
                except Exception as e:
                    if s["type"] == "uv254": 
                        sensor_data[key]["val"] = "Err"
                        sensor_data[key]["cod"] = "Err"
                        sensor_data[key]["temp"] = "Err"
                        sensor_data[key]["turb"] = "Err"
                    else: 
                        sensor_data[key]["val"] = "Err"
                        sensor_data[key]["temp"] = "Err"
                    sensor_data[key]["log_val"] = "Err"
                    sensor_data[key]["ao"] = "0.00"
                    sensor_data[key]["status"] = "ERR"
                    if len(history_data[key]) > 0: history_data[key].pop(0)
                    history_data[key].append(None)

            sys_t_cfg = config.get("sys_temp", {})
            m_sensor = sys_t_cfg.get("sensor")
            
            if m_sensor and m_sensor in sensor_data and "temp" in sensor_data[m_sensor] and sensor_data[m_sensor]["temp"] not in ["--", "Err"]:
                try:
                    sys_t_val = float(sensor_data[m_sensor]["temp"])
                    t_min = float(sys_t_cfg.get("min", 0))
                    t_max = float(sys_t_cfg.get("max", 50))
                    if t_max <= t_min: t_ao = 4.0
                    else:
                        c_t = max(t_min, min(sys_t_val, t_max))
                        t_ao = 4.0 + ((c_t - t_min) / (t_max - t_min)) * 16.0
                    sensor_data["sys_temp"] = f"{sys_t_val:.1f}"
                    sensor_data["sys_temp_ao"] = f"{t_ao:.2f}"
                except Exception:
                    sensor_data["sys_temp"] = "--"
                    sensor_data["sys_temp_ao"] = "0.00"
            else:
                sensor_data["sys_temp"] = "--"
                sensor_data["sys_temp_ao"] = "0.00"
                
            # === MAP AO CHANNELS (8 PORTS) ===
            ao_map = config.get("ao_map", {})
            for c in range(8):
                src = ao_map.get(str(c), "off")
                if src == "sys_temp":
                    auto_ao_out[c] = float(sensor_data.get("sys_temp_ao", 4.0))
                elif src in config.get("sensors", {}):
                    s_data = sensor_data.get(src)
                    if s_data and s_data.get("ao") not in ["--", "Err", "0.00", "Off"]:
                        auto_ao_out[c] = float(s_data["ao"])
                    else:
                        auto_ao_out[c] = 4.0
                else:
                    auto_ao_out[c] = 4.0

            # === CENTRALIZED AO WRITE BLOCK ===
            try:
                ao_id = get_safe_int(config.get("ao_id", 9), 9)
                with modbus_lock:
                    instr_ao = create_instrument(ao_id)
                    for c in range(8):
                        is_manual = ao_manual.get(c, {}).get("active")
                        target_ao = ao_manual[c]["val"] if is_manual else auto_ao_out.get(c, 4.0)
                        
                        out_val = int(target_ao * 1000)
                        out_val = max(0, min(20000, out_val))
                        
                        try:
                            reg_address = c + 2 
                            read_with_retry(instr_ao.write_register, reg_address, out_val, 0, 6, retries=1)
                            time.sleep(0.05) 
                        except Exception: pass
            except Exception: pass

            # === LOGS WRITING (NOW SAVES KEYS AS HEADERS) ===
            if all_keys:
                if now.minute % 5 == 0 and now.minute != last_5min_minute:
                    headers = ["Time"] + all_keys + ["sys_temp"]
                    row = [now.strftime("%Y-%m-%d %H:%M:00")] + [sensor_data.get(k, {}).get("log_val", "--") for k in all_keys]
                    row.append(sensor_data.get("sys_temp", "--"))
                    write_csv_log(LOG_5MIN, headers, row)
                    last_5min_minute = now.minute

                if now.minute == 0 and now.hour != last_1hr_hour:
                    headers = ["Time"] + all_keys + ["sys_temp"]
                    row = [now.strftime("%Y-%m-%d %H:00:00")]
                    for k in all_keys:
                        vals = [v for v in hourly_buffer.get(k, []) if v is not None]
                        if vals: row.append(f"{(sum(vals)/len(vals)):.2f}")
                        else: row.append("--")
                        hourly_buffer[k] = [] 
                    row.append(sensor_data.get("sys_temp", "--"))
                    write_csv_log(LOG_1HR, headers, row)
                    last_1hr_hour = now.hour
                    trim_csv_log(LOG_5MIN)
                    trim_csv_log(LOG_1HR)
                    trim_csv_log(LOG_ALARM)

        except Exception: pass
        time.sleep(0.5)

def read_tail(filepath, lines=30, start_date=None, end_date=None):
    if not os.path.exists(filepath): return {"headers": [], "rows": []}
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            all_rows = list(csv.reader(f))
            if len(all_rows) > 0:
                headers = all_rows[0]
                data_rows = all_rows[1:]
                
                if start_date or end_date:
                    filtered = []
                    for r in data_rows:
                        if not r or len(r) == 0: continue
                        row_date = r[0].split(' ')[0] 
                        if start_date and row_date < start_date: continue
                        if end_date and row_date > end_date: continue
                        filtered.append(r)
                    return {"headers": headers, "rows": list(reversed(filtered[-500:]))}
                
                return {"headers": headers, "rows": list(reversed(data_rows[-lines:]))}
    except: pass
    return {"headers": [], "rows": []}

@app.get("/api/all")
def get_all(log_start: str = None, log_end: str = None): 
    if log_start == "": log_start = None
    if log_end == "": log_end = None
    
    logs = {
        "5min": read_tail(LOG_5MIN, 30, log_start, log_end),
        "1hr": read_tail(LOG_1HR, 30, log_start, log_end),
        "alarm": read_tail(LOG_ALARM, 50, log_start, log_end)
    }
    return {"data": sensor_data, "history": history_data, "relays": relay_states, "ao_manual": ao_manual, "config": config, "logs": logs}

@app.post("/api/save_config")
async def update_cfg(request: Request):
    global config
    new_config = await request.json()
    
    old_keys = list(config.get("sensors", {}).keys())
    new_keys = list(new_config.get("sensors", {}).keys())
    
    config = new_config
    save_config(config)
    
    if set(old_keys) != set(new_keys):
        init_data_structures()
        
        # --- SMART CSV REBUILD (Защита истории) ---
        def rebuild_csv(filepath, target_keys):
            if not os.path.exists(filepath): return
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    rows = list(csv.reader(f))
                if len(rows) < 2: return
                
                old_headers = rows[0]
                new_headers = ["Time"] + target_keys + ["sys_temp"]
                
                # Ищем индексы старых колонок
                idx_map = []
                for nh in new_headers:
                    if nh in old_headers:
                        idx_map.append(old_headers.index(nh))
                    else:
                        idx_map.append(-1) # Для новых датчиков
                        
                # Переписываем файл с новыми колонками
                with open(filepath, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(new_headers)
                    for r in rows[1:]:
                        if not r: continue
                        new_r = []
                        for idx in idx_map:
                            if idx != -1 and idx < len(r):
                                new_r.append(r[idx])
                            else:
                                new_r.append("--")
                        writer.writerow(new_r)
            except: pass

        rebuild_csv(LOG_5MIN, new_keys)
        rebuild_csv(LOG_1HR, new_keys)
        
    return {"status": "ok"}

@app.get("/api/relay")
def toggle_relay(key: str, state: int):
    act = config.get("relay_actions", {}).get(key)
    if act:
        relay_states[key] = state
        try:
            with modbus_lock:
                instr = create_instrument(act["modbus_id"])
                read_with_retry(instr.write_bit, act["ch"], state, 5, retries=2)
        except: pass
    return {"status": "ok"}

@app.get("/api/ao_manual")
def set_ao_manual(ch: int, active: int, val: float):
    if ch in ao_manual:
        ao_manual[ch]["active"] = bool(active)
        ao_manual[ch]["val"] = val
    return {"status": "ok"}

@app.get("/api/trigger_clean")
def trigger_clean_api(key: str, mode: str = None, dur: int = 10, rel_key: str = None):
    return trigger_cleaning(key, force_mode=mode, force_dur=dur, force_rel_key=rel_key)

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
            
            cols_info = []
            for h in headers:
                if h == "Time": cols_info.append({"key": h, "label": "Time"})
                elif h == "sys_temp": cols_info.append({"key": h, "label": "SYS TEMP (°C)"})
                elif h in config.get("sensors", {}):
                    s = config["sensors"][h]
                    cols_info.append({"key": h, "label": f"{s['label']} ({s.get('unit', '')})"})
                else:
                    cols_info.append({"key": h, "label": h})
            
            for row in reader:
                if row and len(row) > 0:
                    date_part = row[0].split(' ')[0]
                    dates.add(date_part)
        
        return {
            "status": "ok", 
            "columns": cols_info, 
            "dates": sorted(list(dates), reverse=True)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/api/export_execute")
async def export_execute(request: Request):
    payload = await request.json()
    log_type = payload.get("type", "5min")
    sel_cols = payload.get("columns", []) # 이제 key값 배열이 들어옴 ("Time", "s_1" 등)
    sel_dates = payload.get("dates", [])
    destination = payload.get("path", "") 
    
    file_map = {"5min": LOG_5MIN, "1hr": LOG_1HR, "alarm": LOG_ALARM}
    target_file = file_map.get(log_type)
    
    if not target_file or not os.path.exists(target_file) or not destination:
        return {"status": "error"}
        
    try:
        with open(target_file, 'r', encoding='utf-8') as fin, open(destination, 'w', newline='', encoding='utf-8') as fout:
            reader = csv.reader(fin)
            writer = csv.writer(fout)
            
            headers = next(reader, [])
            
            col_indices = [i for i, h in enumerate(headers) if h in sel_cols]
            if 0 not in col_indices and "Time" in headers:
                col_indices.insert(0, headers.index("Time"))
                
            if not col_indices:
                return {"status": "error", "message": "No columns selected"}
            
            # 1. 파일용 헤더(Label + Unit) 생성
            out_headers = []
            for i in col_indices:
                h = headers[i]
                if h == "Time": out_headers.append("Time")
                elif h == "sys_temp": out_headers.append("SYS TEMP (°C)")
                elif h in config.get("sensors", {}):
                    s = config["sensors"][h]
                    out_headers.append(f"{s['label']} ({s.get('unit', '')})")
                else: out_headers.append(h)
                
            writer.writerow(out_headers)
            
            # 2. 데이터 변환 및 쓰기
            for row in reader:
                if not row: continue
                date_part = row[0].split(' ')[0]
                if date_part in sel_dates:
                    new_row = []
                    for i in col_indices:
                        h = headers[i]
                        val_str = row[i] if i < len(row) else ""
                        
                        if h in config.get("sensors", {}) and val_str not in ["", "--", "Err", "Off", "NaN"]:
                            try:
                                raw_val = float(val_str)
                                s = config["sensors"][h]
                                s_type = s.get("type")
                                s_unit = s.get("unit", "")
                                val_num = raw_val
                                
                                if s_type == "oil" and s_unit in ["mg/L", "ppm"]: val_num = raw_val / 1000.0
                                elif s_type == "mlss" and s_unit == "g/L": val_num = raw_val / 1000.0
                                elif s_type == "mlss" and s_unit == "%": val_num = raw_val / 10000.0
                                elif s_type == "ec" and s_unit == "mS/cm": val_num = raw_val / 1000.0
                                
                                fmt = "{:.1f}" if s_type == "orp" else "{:.2f}"
                                new_row.append(fmt.format(val_num))
                            except:
                                new_row.append(val_str)
                        else:
                            new_row.append(val_str)
                    writer.writerow(new_row)
                    
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
        
@app.get("/api/change_sensor_id")
def change_sensor_id(curr_id: int, new_id: int, s_type: str):
    try:
        with modbus_lock:
            instr = create_instrument(curr_id)
            
            if s_type == 'io_module':
                try:
                    current_reg = instr.read_register(0, 0)
                    baud_rate_code = current_reg & 0x00FF  
                    new_val = (new_id << 8) | baud_rate_code 
                    instr.write_register(0, new_val, 0, functioncode=6)
                except Exception:
                    fallback_val = (new_id << 8) | 3
                    try:
                        instr.write_register(0, fallback_val, 0, functioncode=6)
                    except Exception as e2:
                        return {"status": "error", "message": str(e2)}
                return {"status": "ok"}

            id_register = 25 if s_type == 'mlss' else 12288
            try:
                current_reg = instr.read_register(id_register, 0)
            except Exception:
                try:
                    instr.address = 255
                    current_reg = instr.read_register(id_register, 0)
                except Exception:
                    return {"status": "error", "message": "Timeout. Cannot read sensor."}
            
            if s_type == 'mlss':
                write_val = new_id
            else:
                if current_reg >= 256: 
                    baud_rate = current_reg & 0x00FF
                    write_val = (new_id << 8) | baud_rate
                else:
                    write_val = new_id

            try:
                instr.write_register(id_register, write_val, 0, functioncode=6)
            except Exception:
                pass 
                
            return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/scan_io")
def scan_io_module(module_type: str):
    try:
        known_sensors = [get_safe_int(s.get("id")) for s in config.get("sensors", {}).values()]
        
        if module_type == 'relay':
            ignore_id = get_safe_int(config.get("ao_id", 9), 9)
        else:
            ignore_id = get_safe_int(config.get("relay_id", 8), 8)
            
        with modbus_lock:
            for i in range(1, 100):
                if i in known_sensors or i == ignore_id: 
                    continue
                
                instr = create_instrument(i)
                instr.serial.timeout = 0.05  
                
                try:
                    if module_type == 'ao':
                        instr.read_register(560, 0, 3)
                    else:
                        instr.read_bit(0, 1)
                    return {"status": "ok", "id": i}
                except minimalmodbus.IllegalRequestError:
                    return {"status": "ok", "id": i}
                except Exception: 
                    pass
                    
        return {"status": "error", "message": "Module not found on bus"}
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
            html { font-size: 16px; }
            @media (max-width: 1280px) { html { font-size: 14px; } }
            @media (max-width: 1024px) { html { font-size: 12px; } }
            @media (max-width: 800px) { html { font-size: 10px; } }
            button, .nav-btn, .text-xs, .text-sm, span { 
                white-space: nowrap; 
            }
            
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

        <nav class="flex gap-3 lg:gap-8 px-3 lg:px-8 py-3 lg:py-4 bg-white dark:bg-slate-900/50 border-b border-slate-300 dark:border-slate-800 items-center shrink-0 z-10 transition-colors overflow-x-auto no-scrollbar">
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
            
            <div class="w-64 xl:w-72 flex flex-col gap-3 shrink-0 min-h-0">
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
            <div class="flex flex-col xl:flex-row justify-between items-start xl:items-center shrink-0 gap-4 w-full">
                
                <div class="flex flex-wrap items-center gap-2 shrink-0">
                    <button onclick="setLogView('5min')" id="btn-v-5min" class="log-tab-btn active px-4 py-2 rounded font-bold text-xs uppercase transition-colors border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400" data-i18n="log_5min">5-Min Data</button>
                    <button onclick="setLogView('1hr')" id="btn-v-1hr" class="log-tab-btn px-4 py-2 rounded font-bold text-xs uppercase transition-colors border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400" data-i18n="log_1hr">1-Hour AVG</button>
                    <button onclick="setLogView('alarm')" id="btn-v-alarm" class="log-tab-btn px-4 py-2 rounded font-bold text-xs uppercase transition-colors border border-slate-300 dark:border-slate-700 bg-white dark:bg-slate-800 text-slate-600 dark:text-slate-400" data-i18n="log_alarm">Alarm History</button>
                    
                    <div class="w-px h-6 bg-slate-300 dark:bg-slate-700 mx-1 hidden sm:block"></div>
                    
                    <button id="btn-show-graph" onclick="openLogChart()" class="bg-indigo-600 hover:bg-indigo-500 text-white font-bold text-xs px-5 py-2 rounded uppercase tracking-wider shadow-sm transition-colors border border-indigo-700 whitespace-nowrap" data-i18n="show_graph">SHOW GRAPH</button>
                    <button id="btn-export" onclick="openExportModal()" class="bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs px-5 py-2 rounded uppercase tracking-wider shadow-sm transition-colors border border-emerald-700 whitespace-nowrap" data-i18n="export">EXPORT TO FILE</button>
                </div>
                
                <div class="flex items-center gap-2 bg-white dark:bg-slate-800 p-1.5 rounded-lg border border-slate-300 dark:border-slate-700 shadow-sm shrink-0">
                    <div class="flex items-center gap-1.5 pl-1">
                        <span class="text-[10px] font-bold text-slate-500 uppercase tracking-widest whitespace-nowrap" data-i18n="start_date">Start Date</span>
                        <input type="date" id="log-start-date" class="bg-slate-50 dark:bg-slate-900 border border-slate-300 dark:border-slate-600 rounded text-xs px-2 py-1.5 dark:text-white outline-none focus:border-cyan-500 font-bold text-slate-700">
                    </div>
                    <span class="text-slate-400 font-bold px-1">-</span>
                    <div class="flex items-center gap-1.5">
                        <span class="text-[10px] font-bold text-slate-500 uppercase tracking-widest whitespace-nowrap" data-i18n="end_date">End Date</span>
                        <input type="date" id="log-end-date" class="bg-slate-50 dark:bg-slate-900 border border-slate-300 dark:border-slate-600 rounded text-xs px-2 py-1.5 dark:text-white outline-none focus:border-cyan-500 font-bold text-slate-700">
                    </div>
                    <button onclick="applyLogFilter()" class="bg-cyan-600 hover:bg-cyan-500 text-white font-bold text-[10px] px-3 py-1.5 rounded uppercase tracking-wider transition-colors shadow-sm ml-1 whitespace-nowrap" data-i18n="search">Search</button>
                    <button onclick="clearLogFilter()" class="bg-slate-200 dark:bg-slate-700 hover:bg-slate-300 dark:hover:bg-slate-600 text-slate-700 dark:text-white font-bold text-[10px] px-3 py-1.5 rounded uppercase tracking-wider transition-colors shadow-sm whitespace-nowrap" data-i18n="clear">Clear</button>
                </div>
            </div>
            
            <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg overflow-auto flex-grow min-h-0">
                <table class="w-full text-left text-sm whitespace-nowrap">
                    <thead id="log-head" class="sticky top-0 z-10 shadow-sm"></thead>
                    <tbody id="log-body"></tbody>
                </table>
            </div>
        </main>

        <main id="tab-ctrl" class="p-6 hidden flex-grow flex flex-col gap-6 items-center min-h-0 overflow-y-auto">
            <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-8 w-full max-w-4xl border-t-4 border-t-cyan-500 dark:border-t-cyan-900/30 shrink-0">
                <h2 class="text-cyan-600 dark:text-cyan-400 font-black text-xl mb-6 uppercase border-b border-slate-300 dark:border-slate-700 pb-4" data-i18n="manual_or">Manual Relay Override</h2>
                <div id="ctrl-relay-grid" class="grid grid-cols-2 gap-8">
                    </div>
            </div>

            <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-8 w-full max-w-4xl border-t-4 border-t-fuchsia-500 dark:border-t-fuchsia-900/30 shrink-0 mb-8">
                <h2 class="text-fuchsia-600 dark:text-fuchsia-400 font-black text-xl mb-6 uppercase border-b border-slate-300 dark:border-slate-700 pb-4" data-i18n="manual_ao">Manual Analog Output</h2>
                <div class="grid grid-cols-2 gap-8">
                    <script>
                        for(let i=0; i<8; i++) {
                            document.write(`
                            <div class="flex flex-col bg-slate-100 dark:bg-slate-800/40 p-5 rounded-xl border border-slate-300 dark:border-slate-700/50 shadow-sm gap-4">
                                <div class="flex items-center justify-between">
                                    <span class="text-sm font-bold text-slate-700 dark:text-slate-300 uppercase">AO CH ${i}</span>
                                    <div class="flex items-center gap-2">
                                        <span class="text-[10px] font-black text-slate-400 uppercase tracking-widest">AUTO</span>
                                        <label class="relative inline-flex items-center cursor-pointer">
                                            <input type="checkbox" id="ao-toggle-${i}" onchange="updateAoManual(${i})" class="sr-only peer">
                                            <div class="w-10 h-5 bg-slate-300 dark:bg-slate-700 rounded-full peer-checked:bg-fuchsia-500 transition-colors after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-[20px]"></div>
                                        </label>
                                        <span class="text-[10px] font-black text-fuchsia-500 uppercase tracking-widest">MANUAL</span>
                                    </div>
                                </div>
                                <div class="flex flex-col gap-3 opacity-50 pointer-events-none transition-opacity" id="ao-ctrl-box-${i}">
                                    <div class="flex items-center gap-3">
                                        <button onclick="stepAoUI(${i}, -0.1)" class="w-8 h-8 flex items-center justify-center bg-white dark:bg-slate-700 rounded-full text-slate-600 dark:text-slate-300 font-black hover:bg-fuchsia-100 dark:hover:bg-fuchsia-900/50 hover:text-fuchsia-600 dark:hover:text-fuchsia-400 transition-colors shadow-sm border border-slate-300 dark:border-slate-600">-</button>
                                        <input type="range" id="ao-slider-${i}" min="4" max="20" step="0.1" value="4.0" oninput="syncAoUI(${i})" class="flex-1 accent-fuchsia-500">
                                        <button onclick="stepAoUI(${i}, 0.1)" class="w-8 h-8 flex items-center justify-center bg-white dark:bg-slate-700 rounded-full text-slate-600 dark:text-slate-300 font-black hover:bg-fuchsia-100 dark:hover:bg-fuchsia-900/50 hover:text-fuchsia-600 dark:hover:text-fuchsia-400 transition-colors shadow-sm border border-slate-300 dark:border-slate-600">+</button>
                                        <div class="w-16 text-right font-mono font-black text-fuchsia-600 dark:text-fuchsia-400"><span id="ao-val-${i}">4.0</span> mA</div>
                                    </div>
                                    <div class="flex gap-2 justify-between mt-1">
                                        <button onclick="setAoPreset(${i}, 4.0)" class="flex-1 py-1.5 bg-slate-200 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded text-xs font-bold text-slate-600 dark:text-slate-400 hover:border-fuchsia-500 hover:text-fuchsia-600 dark:hover:text-fuchsia-400 transition-colors shadow-sm">4</button>
                                        <button onclick="setAoPreset(${i}, 8.0)" class="flex-1 py-1.5 bg-slate-200 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded text-xs font-bold text-slate-600 dark:text-slate-400 hover:border-fuchsia-500 hover:text-fuchsia-600 dark:hover:text-fuchsia-400 transition-colors shadow-sm">8</button>
                                        <button onclick="setAoPreset(${i}, 12.0)" class="flex-1 py-1.5 bg-slate-200 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded text-xs font-bold text-slate-600 dark:text-slate-400 hover:border-fuchsia-500 hover:text-fuchsia-600 dark:hover:text-fuchsia-400 transition-colors shadow-sm">12</button>
                                        <button onclick="setAoPreset(${i}, 16.0)" class="flex-1 py-1.5 bg-slate-200 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded text-xs font-bold text-slate-600 dark:text-slate-400 hover:border-fuchsia-500 hover:text-fuchsia-600 dark:hover:text-fuchsia-400 transition-colors shadow-sm">16</button>
                                        <button onclick="setAoPreset(${i}, 20.0)" class="flex-1 py-1.5 bg-slate-200 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded text-xs font-bold text-slate-600 dark:text-slate-400 hover:border-fuchsia-500 hover:text-fuchsia-600 dark:hover:text-fuchsia-400 transition-colors shadow-sm">20</button>
                                    </div>
                                </div>
                            </div>
                            `);
                        }
                    </script>
                </div>
            </div>
        </main>

        <main id="tab-eng" class="p-4 lg:p-6 hidden flex-grow flex flex-col lg:flex-row gap-4 lg:gap-6 overflow-y-auto lg:overflow-hidden min-h-0">
            <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-6 flex flex-col gap-4 w-full lg:w-2/3 min-h-[50vh] lg:min-h-0 min-h-0">
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
                        <button onclick="openIdTool()" class="flex items-center gap-1.5 bg-blue-100 text-blue-700 hover:bg-blue-200 dark:bg-blue-900/40 dark:text-blue-400 px-4 py-2 rounded text-sm font-bold border border-blue-200 dark:border-blue-800 transition-colors shadow-sm">
                            <span data-i18n="id_tool">ID TOOL</span>
                        </button>
                        <button onclick="openIoModal()" class="flex items-center gap-1.5 bg-slate-200 dark:bg-slate-700 text-slate-700 dark:text-slate-300 px-4 py-2 rounded text-sm font-bold hover:bg-slate-300 dark:hover:bg-slate-600 border border-slate-300 dark:border-slate-600 transition-colors shadow-sm">
                            <span data-i18n="io_setup">I/O SETUP</span>
                        </button>
                        <button onclick="openModal()" class="bg-emerald-600 text-white px-4 py-2 rounded text-sm font-bold hover:bg-emerald-500 border border-emerald-700 transition-colors shadow-sm" data-i18n="add_sensor">+ ADD SENSOR</button>
                    </div>
                </div>
                <div id="eng-sensors" class="grid grid-cols-2 gap-4 mt-2 overflow-y-auto pr-2 flex-grow min-h-0 content-start"></div>
            </div>
            
            <div class="flex flex-col gap-4 lg:gap-6 w-full lg:w-1/3 min-h-0 shrink-0 overflow-y-auto pr-1">
                <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-6 flex flex-col gap-4 shrink-0">
                    <div class="border-b border-slate-300 dark:border-slate-700 pb-3 flex justify-between items-end shrink-0">
                        <h2 class="text-emerald-600 dark:text-emerald-500 font-black text-base uppercase" data-i18n="ao_scaling">4-20mA Scaling</h2>
                    </div>
                    <div id="eng-ao-scaling" class="flex flex-col gap-3"></div>
                </div>
                
                <div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-6 flex flex-col gap-4 shrink-0">
                    <div class="border-b border-slate-300 dark:border-slate-700 pb-3 flex justify-between items-end shrink-0">
                        <h2 class="text-purple-600 dark:text-purple-500 font-black text-base uppercase" data-i18n="relay_setup">Relay Configuration</h2>
                    </div>
                    <div id="eng-relays-list" class="flex flex-col gap-2"></div>
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
                    <button onclick="closeExportModal()" class="px-5 py-2.5 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 border border-slate-300 dark:border-slate-600 shadow-sm" data-i18n="cancel">CANCEL</button>
                    <button id="btn-execute-export" onclick="executeAdvancedExport()" class="px-5 py-2.5 bg-emerald-600 rounded text-sm font-bold text-white hover:bg-emerald-500 border border-emerald-700 shadow-lg" data-i18n="download_csv">DOWNLOAD CSV</button>
                </div>
            </div>
        </div>

        <div id="io-modal-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-xl w-[400px] p-6 shadow-2xl flex flex-col">
                <h2 class="text-slate-700 dark:text-slate-300 font-black text-xl mb-4 shrink-0 uppercase border-b border-slate-200 dark:border-slate-800 pb-3" data-i18n="io_setup">I/O Modules Setup</h2>
                <div class="flex flex-col gap-4">
                    
                    <div class="bg-slate-50 dark:bg-slate-800/50 p-4 rounded border border-slate-200 dark:border-slate-700">
                        <div class="flex justify-between items-end mb-3">
                            <div class="flex flex-col">
                                <span class="text-[10px] text-slate-400 font-bold uppercase tracking-widest mb-0.5" data-i18n="relay_module_title">DEFAULT RELAY ID</span>
                                <input type="text" id="eng-relay-name" onchange="triggerSave()" class="text-xs font-black text-slate-700 dark:text-slate-300 uppercase tracking-widest bg-transparent border-b border-slate-300 dark:border-slate-600 hover:border-indigo-400 focus:border-indigo-500 outline-none w-48 pb-1">
                            </div>
                            <button id="btn-scan-relay" onclick="autoDetectIo('relay', 'eng-relay-id', 'btn-scan-relay')" class="px-3 py-1.5 bg-indigo-100 text-indigo-600 hover:bg-indigo-200 dark:bg-indigo-900/30 dark:text-indigo-400 rounded text-[10px] font-black uppercase tracking-wider transition-colors border border-indigo-200 dark:border-indigo-800">
                                AUTO DETECT
                            </button>
                        </div>
                        <div class="flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden shadow-sm bg-white dark:bg-slate-800">
                            <button onclick="stepVal('eng-relay-id', -1)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">-</button>
                            <input onchange="triggerSave()" type="number" id="eng-relay-id" class="w-full bg-transparent text-center py-2 text-slate-900 dark:text-white text-lg font-bold outline-none">
                            <button onclick="stepVal('eng-relay-id', 1)" class="w-12 py-2 bg-slate-100 dark:bg-slate-700 text-lg font-black hover:bg-slate-200 dark:hover:bg-slate-600 no-select transition-colors">+</button>
                        </div>
                    </div>
                    
                    <div class="bg-slate-50 dark:bg-slate-800/50 p-4 rounded border border-slate-200 dark:border-slate-700">
                        <div class="flex justify-between items-end mb-3">
                            <div class="flex flex-col">
                                <span class="text-[10px] text-slate-400 font-bold uppercase tracking-widest mb-0.5" data-i18n="ao_module_title">ANALOG OUTPUT ID</span>
                                <input type="text" id="eng-ao-name" onchange="triggerSave()" class="text-xs font-black text-slate-700 dark:text-slate-300 uppercase tracking-widest bg-transparent border-b border-slate-300 dark:border-slate-600 hover:border-indigo-400 focus:border-indigo-500 outline-none w-48 pb-1">
                            </div>
                            <button id="btn-scan-ao" onclick="autoDetectIo('ao', 'eng-ao-id', 'btn-scan-ao')" class="px-3 py-1.5 bg-indigo-100 text-indigo-600 hover:bg-indigo-200 dark:bg-indigo-900/30 dark:text-indigo-400 rounded text-[10px] font-black uppercase tracking-wider transition-colors border border-indigo-200 dark:border-indigo-800">
                                AUTO DETECT
                            </button>
                        </div>
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

        <div id="id-tool-modal" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-50 flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border-t-4 border-t-blue-500 border border-slate-300 dark:border-slate-700 rounded-xl w-[400px] p-6 shadow-2xl flex flex-col">
                <div class="flex justify-between items-center mb-2">
                    <h2 class="text-blue-600 dark:text-blue-400 font-black text-xl uppercase tracking-wider" data-i18n="id_tool_title">CHANGE SENSOR ID</h2>
                </div>
                <p class="text-[11px] font-bold text-rose-600 dark:text-rose-400 mb-5 bg-rose-50 dark:bg-rose-950/30 p-2 rounded border border-rose-200 dark:border-rose-900/50" data-i18n="id_tool_warn">
                    [WARNING] Only ONE device must be connected to the bus!
                </p>
                
                <div class="space-y-4">
                    <div>
                        <label class="block text-xs font-bold text-slate-500 dark:text-slate-400 mb-1.5" data-i18n="sensor_type">SENSOR TYPE</label>
                        <select id="tool-s-type" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2 text-slate-900 dark:text-black font-bold text-sm focus:outline-none focus:border-blue-500">
                            <option value="std" data-i18n="std_sensors">Standard (pH, DO, ORP, etc.)</option>
                            <option value="mlss" data-i18n="mlss_sensor">MLSS Sensor</option>
                            <option value="io_module" data-i18n="io_module_type">I/O Module (KM60xx Relay/AO)</option>
                        </select>
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-slate-500 dark:text-slate-400 mb-1.5" data-i18n="current_id">CURRENT ID (255=Broadcast)</label>
                        <input type="number" id="tool-curr-id" value="255" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2 text-slate-900 dark:text-white font-bold text-base focus:outline-none focus:border-blue-500 text-center">
                    </div>
                    <div>
                        <label class="block text-xs font-bold text-blue-600 dark:text-blue-400 mb-1.5" data-i18n="new_id">NEW ID (1-247)</label>
                        <input type="number" id="tool-new-id" placeholder="e.g. 15" class="w-full bg-blue-50 dark:bg-blue-900/20 border border-blue-300 dark:border-blue-700 rounded p-2 text-blue-700 dark:text-blue-400 font-black text-xl focus:outline-none focus:border-blue-500 text-center shadow-inner">
                    </div>
                </div>
                
                <div class="flex justify-end gap-3 mt-6">
                    <button onclick="closeIdTool()" class="px-5 py-2 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 border border-slate-300 dark:border-slate-600 shadow-sm" data-i18n="close">CLOSE</button>
                    <button id="btn-exec-id" onclick="executeChangeId()" class="px-5 py-2 bg-blue-600 rounded text-sm font-bold text-white hover:bg-blue-500 border border-blue-700 shadow-lg" data-i18n="change_id">CHANGE ID</button>
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
                            <option value="external" data-i18n="opt_ext">EXTERNAL (Relay)</option>
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
                        <label class="block text-sm font-bold text-slate-500 dark:text-slate-400 mb-1.5 uppercase tracking-widest" data-i18n="relay_channel">Relay Channel</label>
                        <select id="clean-rel" onchange="triggerSave()" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded p-2.5 text-slate-900 dark:text-black font-bold text-sm focus:outline-none focus:border-sky-500">
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

        <div id="custom-alert-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-[100] flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-xl w-[350px] p-6 shadow-2xl flex flex-col transform scale-95 transition-transform duration-200" id="custom-alert-box">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-sky-600 dark:text-sky-400 font-black text-lg uppercase tracking-wider flex items-center gap-2">
                        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-6 h-6"><path stroke-linecap="round" stroke-linejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" /></svg>
                        <span id="custom-alert-title">Notification</span>
                    </h2>
                </div>
                <p id="custom-alert-msg" class="text-sm font-bold text-slate-600 dark:text-slate-300 mb-6 leading-relaxed"></p>
                <div class="flex justify-end">
                    <button onclick="closeCustomAlert()" class="px-6 py-2 bg-sky-600 rounded text-sm font-bold text-white hover:bg-sky-500 shadow-md transition-colors w-full">OK</button>
                </div>
            </div>
        </div>

        <div id="custom-confirm-overlay" class="modal-overlay fixed inset-0 bg-slate-900/60 dark:bg-black/80 hidden z-[100] flex justify-center items-center p-4">
            <div class="bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-700 rounded-xl w-[350px] p-6 shadow-2xl flex flex-col transform scale-95 transition-transform duration-200" id="custom-confirm-box">
                <div class="flex justify-between items-center mb-4">
                    <h2 class="text-rose-600 dark:text-rose-500 font-black text-lg uppercase tracking-wider flex items-center gap-2">
                        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-6 h-6"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
                        <span id="custom-confirm-title">Warning</span>
                    </h2>
                </div>
                <p id="custom-confirm-msg" class="text-sm font-bold text-slate-600 dark:text-slate-300 mb-6 leading-relaxed"></p>
                <div class="flex justify-end gap-3">
                    <button onclick="closeCustomConfirm()" class="px-5 py-2 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300 shadow-sm transition-colors w-1/2">CANCEL</button>
                    <button onclick="executeCustomConfirm()" id="custom-confirm-btn" class="px-5 py-2 bg-rose-600 rounded text-sm font-bold text-white hover:bg-rose-500 shadow-md transition-colors w-1/2">CONFIRM</button>
                </div>
            </div>
        </div>
        
        <!-- ДОБАВЛЕНО: ОКНА ГРАФИКА И ЗАГРЯЗНЕНИЯ -->
        <div id="log-chart-modal" class="modal-overlay fixed inset-0 bg-slate-900/80 hidden z-50 flex justify-center items-center p-8">
            <div class="bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-slate-700 rounded-xl w-full h-full p-6 shadow-2xl flex flex-col">
                <div class="flex justify-between items-center mb-4 shrink-0">
                    <h2 class="text-indigo-600 dark:text-indigo-400 font-black text-xl uppercase tracking-wider" data-i18n="data_graph">DATA GRAPH</h2>
                    <button onclick="document.getElementById('log-chart-modal').classList.add('hidden')" class="px-6 py-2 bg-slate-200 dark:bg-slate-700 rounded text-sm font-bold text-slate-800 dark:text-white hover:bg-slate-300" data-i18n="close">CLOSE</button>
                </div>
                <div class="flex-grow relative min-h-0"><canvas id="logFullCanvas"></canvas></div>
            </div>
        </div>

        <div id="mfg-pwd-modal" class="modal-overlay fixed inset-0 bg-slate-900/60 hidden z-[60] flex justify-center items-center">
            <div class="bg-white dark:bg-slate-900 border-t-4 border-t-rose-500 rounded-xl w-[350px] p-6 shadow-2xl">
                <h2 class="text-rose-600 font-black text-sm mb-4 leading-relaxed break-keep" data-i18n="mfg_login">MANUFACTURER LOGIN</h2>
                <input type="password" id="mfg-pwd-input" class="w-full bg-slate-50 dark:bg-slate-800 border border-slate-300 dark:border-slate-700 rounded p-2.5 mb-4 text-slate-900 dark:text-white focus:outline-none focus:border-rose-500" placeholder="****">
                <div class="flex justify-end gap-2">
                    <button onclick="document.getElementById('mfg-pwd-modal').classList.add('hidden')" class="px-5 py-2 bg-slate-200 dark:bg-slate-700 hover:bg-slate-300 dark:hover:bg-slate-600 transition-colors rounded text-sm font-bold text-slate-800 dark:text-white" data-i18n="cancel">CANCEL</button>
                    <button onclick="verifyMfgPwd()" class="px-5 py-2 bg-rose-600 hover:bg-rose-500 transition-colors text-white rounded text-sm font-bold shadow-sm" data-i18n="enter">ENTER</button>
                </div>
            </div>
        </div>

        <div id="contam-setup-modal" class="modal-overlay fixed inset-0 bg-slate-900/60 hidden z-[60] flex justify-center items-center">
            <div class="bg-white dark:bg-slate-900 border-t-4 border-t-emerald-500 rounded-xl w-[400px] p-6 shadow-2xl">
                <h2 class="text-emerald-600 font-black text-xl mb-4" data-i18n="contam_setup">CONTAMINATION SETUP</h2>
                <input type="hidden" id="contam-s-key">
                <div class="flex justify-between items-center mb-6 bg-slate-100 dark:bg-slate-800 p-3 rounded">
                    <span class="font-bold dark:text-white text-sm" data-i18n="contam_enable">Enable Contamination Alert</span>
                    <input type="checkbox" id="contam-enabled" class="w-5 h-5 accent-emerald-500">
                </div>
                <div class="mb-6">
                    <label class="block text-sm font-bold text-slate-500 mb-2" data-i18n="contam_cycle">Cycle (Months)</label>
                    <select id="contam-months" class="w-full bg-slate-50 dark:bg-slate-800 border rounded p-2 font-bold dark:text-black">
                    </select>
                </div>
                <button onclick="resetContamTimer()" class="w-full py-2 mb-6 bg-amber-100 text-amber-700 hover:bg-amber-200 rounded font-black border border-amber-300" data-i18n="reset_timer">RESET TIMER (0%)</button>
                <div class="flex justify-end gap-2 border-t pt-4">
                    <button onclick="document.getElementById('contam-setup-modal').classList.add('hidden')" class="px-5 py-2 bg-slate-200 dark:bg-slate-700 rounded font-bold text-slate-700 dark:text-white" data-i18n="close">CLOSE</button>
                    <button onclick="saveContamSetup()" class="px-5 py-2 bg-emerald-600 text-white rounded font-bold" data-i18n="save_setup">SAVE SETUP</button>
                </div>
            </div>
        </div>

        <script>
            // =========================================================
            // 🔥 ADVANCED SMART ON-SCREEN KEYBOARD (OSK) with Preview
            // =========================================================
            const KioskBoard = {
                activeInput: null,
                mode: 'num', 
                isShift: false,

                init() {
                    // --- CSS стили для клавиатуры и предпросмотра ---
                    const stylesHTML = `
                    <style>
                        #osk-wrapper .osk-btn {
                            flex: 1; height: 3.8rem; display: flex; align-items: center; justify-content: center;
                            font-weight: 700; border-radius: 0.375rem; border-width: 1px; transition: all 150ms ease-in-out;
                            cursor: pointer; user-select: none; font-size: 1.25rem;
                        }
                        
                        /* Светлая тема (По умолчанию) */
                        .osk-btn { background-color: #ffffff; color: #1e293b; border-color: #cbd5e1; }
                        .osk-btn:hover { background-color: #f1f5f9; }
                        .osk-btn:active { background-color: #06b6d4; color: #ffffff; }
                        
                        /* Темная тема (через класс .dark) */
                        .dark .osk-btn { background-color: #334155; color: #ffffff; border-color: #475569; }
                        .dark .osk-btn:hover { background-color: #475569; }
                        .dark .osk-btn:active { background-color: #0891b2; color: #ffffff; }

                        /* ENTER Кнопка */
                        #osk-wrapper .osk-btn-enter { background-color: #0891b2; color: #ffffff; border-color: #0e7490; font-weight: 900; font-size: 0.8rem; letter-spacing: 0.1em; }
                        #osk-wrapper .osk-btn-enter:hover { background-color: #06b6d4; }
                        #osk-wrapper .osk-btn-enter:active { background-color: #22d3ee; }

                        /* Shift Кнопка (активная) */
                        #osk-wrapper .osk-btn-shift-active { background-color: #cffafe; color: #0891b2; border-color: #22d3ee; }
                        .dark #osk-wrapper .osk-btn-shift-active { background-color: #0c4a6e; color: #67e8f9; border-color: #0e7490; }

                        /* Danger Кнопки (Back, CLR) */
                        #osk-wrapper .osk-btn-danger { background-color: #fee2e2; color: #dc2626; border-color: #fecaca; }
                        .dark #osk-wrapper .osk-btn-danger { background-color: #4c1d1d; color: #f87171; border-color: #7f1d1d; }

                        /* --- Поле предпросмотра (Preview) --- */
                        #osk-preview {
                            font-family: 'Montserrat', sans-serif; font-weight: 700; font-size: 1.5rem;
                            padding: 0.5rem 1.25rem; border-radius: 0.375rem; border-width: 1px;
                            flex: 1; margin: 0 1rem; box-shadow: inset 0 2px 4px rgba(0,0,0,0.1); outline: none;
                            background-color: #ffffff; color: #111827; border-color: #cbd5e1;
                        }
                        .dark #osk-preview { background-color: #030712; color: #06b6d4; border-color: #334155; text-shadow: 0 0 10px rgba(6,182,212,0.3); }

                        /* CLOSE Кнопка (на панели) */
                        #osk-wrapper .osk-btn-close {
                            text-transform: uppercase; font-weight: 900; font-size: 0.8rem; padding: 0.6rem 1.5rem;
                            background-color: #fee2e2; color: #dc2626; border-color: #fecaca; border-radius: 0.375rem; border-width: 1px; cursor: pointer;
                        }
                        .dark #osk-wrapper .osk-btn-close { background-color: #4c1d1d; color: #f87171; border-color: #7f1d1d; }
                    </style>`;
                    document.body.insertAdjacentHTML('beforeend', stylesHTML);

                    const html = `
                    <div id="osk-bg" class="fixed inset-0 bg-transparent hidden z-[199]" onclick="KioskBoard.hide()"></div>
                    <div id="osk-wrapper" class="fixed bottom-0 left-0 w-full bg-slate-200 dark:bg-slate-900 border-t border-slate-300 dark:border-slate-700 shadow-[0_-10px_40px_rgba(0,0,0,0.3)] z-[200] transform translate-y-full transition-transform duration-300 hidden select-none pb-4">
                        <div class="flex justify-between items-center bg-slate-300 dark:bg-slate-950 px-4 py-2.5 border-b border-slate-400 dark:border-slate-800">
                            <span class="text-slate-600 dark:text-slate-400 font-black text-sm tracking-widest shrink-0" id="osk-title">KEYBOARD</span>
                            
                            <input type="text" id="osk-preview" readonly placeholder="Enter value...">
                            <button onclick="KioskBoard.hide()" class="osk-btn-close">CLOSE (ENTER)</button>
                        </div>
                        <div id="osk-keys" class="p-2 gap-1.5 flex flex-col mt-2 max-w-5xl mx-auto w-full"></div>
                    </div>`;
                    document.body.insertAdjacentHTML('beforeend', html);

                    // Глобальный перехватчик фокуса
                    document.addEventListener('focusin', (e) => {
                        if(e.target.tagName === 'INPUT' && !['checkbox', 'radio', 'color', 'date'].includes(e.target.type)) {
                            // Игнорируем фокус на самом поле предпросмотра
                            if(e.target.id === 'osk-preview') return;

                            if(e.target.type === 'number' || e.target.id.includes('pwd') || e.target.classList.contains('no-spin')) {
                                KioskBoard.show(e.target, 'num');
                            } else {
                                KioskBoard.show(e.target, 'en');
                            }
                        }
                    });
                },

                show(input, mode) {
                    this.activeInput = input;
                    this.mode = mode;
                    this.isShift = false;
                    
                    // --- 1. Обновить поле предпросмотра при открытии ---
                    const preview = document.getElementById('osk-preview');
                    if(preview) {
                        preview.value = input.value;
                        preview.type = input.type; // Зеркалим тип (password -> ***)
                        // Placeholder
                        preview.placeholder = mode === 'num' ? 'Enter numbers...' : 'Enter text...';
                    }
                    // ------------------------------------------------

                    this.render();
                    
                    document.getElementById('osk-bg').classList.remove('hidden');
                    const wrapper = document.getElementById('osk-wrapper');
                    wrapper.classList.remove('hidden');
                    setTimeout(() => wrapper.classList.remove('translate-y-full'), 10);
                },

                hide() {
                    if (this.activeInput) {
                        this.activeInput.blur(); 
                        this.activeInput = null;
                    }
                    
                    // --- 2. Очистить поле предпросмотра при закрытии ---
                    const preview = document.getElementById('osk-preview');
                    if(preview) preview.value = '';
                    // ------------------------------------------------

                    document.getElementById('osk-bg').classList.add('hidden');
                    const wrapper = document.getElementById('osk-wrapper');
                    wrapper.classList.add('translate-y-full');
                    setTimeout(() => wrapper.classList.add('hidden'), 300);
                },

                render() {
                    const container = document.getElementById('osk-keys');
                    container.innerHTML = '';
                    document.getElementById('osk-title').innerText = this.mode === 'num' ? 'NUM PAD' : 'ENGLISH OSK';
                    
                    // (Логика layoutData осталась старой, пропущу для краткости)
                    const rowsFull = [['1','2','3','4','5','6','7','8','9','0','-','+','Back:1.5'],['q','w','e','r','t','y','u','i','o','p','[',']'],['a','s','d','f','g','h','j','k','l',':',';','Enter:1.5'],['Shift:1.5','z','x','c','v','b','n','m',',','.','/','Shift:1.5'],['Space:5']];
                    const rowsShift = [['!','@','#','$','%','^','&','*','(',')','_','=','Back:1.5'],['Q','W','E','R','T','Y','U','I','O','P','{','}'],['A','S','D','F','G','H','J','K','L','"',"'",'Enter:1.5'],['Shift:1.5','Z','X','C','V','B','N','M','<','>','?','Shift:1.5'],['Space:5']];
                    const rowsNum = [['7','8','9','Back:1.5'],['4','5','6','CLR:1.5'],['1','2','3','Enter:1.5'],['0','-','.','Space:1.5']];

                    const layoutData = this.mode === 'num' ? rowsNum : (this.isShift ? rowsShift : rowsFull);

                    layoutData.forEach(row => {
                        const rowDiv = document.createElement('div');
                        rowDiv.className = 'flex justify-center gap-1.5 w-full';
                        row.forEach(keyDef => {
                            let [key, flex] = keyDef.split(':');
                            flex = flex || '1';
                            const btn = document.createElement('button');
                            btn.className = 'osk-btn'; // Используем новый CSS класс
                            btn.style.flex = flex;
                            
                            if (key === 'Space') { btn.innerHTML = '&#9251;'; } 
                            else if (key === 'Back' || key === 'CLR') { btn.innerHTML = key === 'Back' ? '&#9003;' : 'CLR'; btn.classList.add('osk-btn-danger'); } 
                            else if (key === 'Enter') { btn.innerHTML = 'ENTER'; btn.classList.add('osk-btn-enter'); } 
                            else if (key === 'Shift') { btn.innerHTML = '&#8679;'; if (this.isShift) btn.classList.add('osk-btn-shift-active'); } 
                            else { btn.innerText = key; }

                            btn.onmousedown = (e) => { e.preventDefault(); this.handleKey(key); };
                            rowDiv.appendChild(btn);
                        });
                        container.appendChild(rowDiv);
                    });
                },

                handleKey(key) {
                    if(!this.activeInput) return;
                    
                    if(key === 'Shift') { this.isShift = !this.isShift; this.render(); return; }
                    if(key === 'Enter') { this.hide(); if (this.activeInput.id === 'admin-pwd-input') verifyAdmin(); if (this.activeInput.id === 'mfg-pwd-input') verifyMfgPwd(); return; }

                    let val = this.activeInput.value;

                    if(key === 'CLR') { val = ''; } 
                    else if(key === 'Back') { val = val.slice(0, -1); } 
                    else if(key === 'Space') { val += ' '; } 
                    else { val += key; }
                    
                    this.activeInput.value = val;
                    
                    // --- 3. НЕВЕРОЯТНО ВАЖНО: Зеркалим ввод в предпросмотр ---
                    const preview = document.getElementById('osk-preview');
                    if(preview) preview.value = val;
                    // -------------------------------------------------------

                    this.activeInput.dispatchEvent(new Event('input', { bubbles: true }));
                }
            };
            KioskBoard.init();
            // =========================================================
            
            // --- CUSTOM DIALOG FUNCTIONS ---
            function customAlert(msg) {
                document.getElementById('custom-alert-msg').innerText = msg;
                document.getElementById('custom-alert-title').innerText = (configData && configData.lang === 'ko') ? "알림" : "Notification";
                const overlay = document.getElementById('custom-alert-overlay');
                const box = document.getElementById('custom-alert-box');
                overlay.classList.remove('hidden');
                setTimeout(() => box.classList.remove('scale-95'), 10);
            }
            function closeCustomAlert() {
                const overlay = document.getElementById('custom-alert-overlay');
                const box = document.getElementById('custom-alert-box');
                box.classList.add('scale-95');
                setTimeout(() => overlay.classList.add('hidden'), 200);
            }

            let currentConfirmCallback = null;
            function customConfirm(msg, callback) {
                document.getElementById('custom-confirm-msg').innerText = msg;
                document.getElementById('custom-confirm-title').innerText = (configData && configData.lang === 'ko') ? "경고" : "Warning";
                document.getElementById('custom-confirm-btn').innerText = (configData && configData.lang === 'ko') ? "확인" : "CONFIRM";
                
                const cancelBtn = document.querySelector('#custom-confirm-box button[onclick="closeCustomConfirm()"]');
                if(cancelBtn) cancelBtn.innerText = (configData && configData.lang === 'ko') ? "취소" : "CANCEL";
                
                currentConfirmCallback = callback;
                
                const overlay = document.getElementById('custom-confirm-overlay');
                const box = document.getElementById('custom-confirm-box');
                overlay.classList.remove('hidden');
                setTimeout(() => box.classList.remove('scale-95'), 10);
            }
            function closeCustomConfirm() {
                const overlay = document.getElementById('custom-confirm-overlay');
                const box = document.getElementById('custom-confirm-box');
                box.classList.add('scale-95');
                setTimeout(() => overlay.classList.add('hidden'), 200);
                currentConfirmCallback = null;
            }
            function executeCustomConfirm() {
                if(currentConfirmCallback) currentConfirmCallback();
                closeCustomConfirm();
            }
            // -------------------------------

            let currentTab = 'dash';
            let currentChartMode = 'all';
            let currentLogView = '5min';
            let configData = null;
            let isInitialized = false;
            let charts = [];
            
            let filterStartDate = '';
            let filterEndDate = '';
            
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
                    nav_dash: "Monitoring", nav_trends: "Trends", nav_logs: "Data", nav_ctrl: "Control", nav_setup: "Setup", theme: "Theme", exit: "EXIT",
                    sys_status: "System Status", mini_trend: "MINI TREND", multi_trend: "MULTI-TREND ANALYSIS", show_all: "SHOW ALL LINES",
                    log_5min: "5-Min Data", log_1hr: "1-Hour AVG", log_alarm: "Alarm History", export: "Export to File",
                    manual_or: "Manual Relay Override",
                    dev_net: "Device Network Manager", io_setup: "I/O SETUP", add_sensor: "ADD SENSOR", change_pwd: "CHANGE PWD", ao_scaling: "4-20mA Scaling", relay_setup: "Relay Configuration",
                    export_title: "Advanced Data Export", export_dates: "1. Select Dates", export_sensors: "2. Select Sensors", cancel: "CANCEL", download_csv: "DOWNLOAD CSV",
                    admin_login: "Admin Login", password: "PASSWORD", unlock: "UNLOCK", new_pwd: "NEW PASSWORD", save: "SAVE", close: "CLOSE", lock: "LOCK",
                    sw_cal: "1. HMI Software (y = A*x + B)", hw_cal: "2. Sensor Hardware (Modbus)", save_hw_cal: "SAVE HW CAL",
                    clean_title: "CLEANING SETUP", ctrl_mode: "Control Mode", test_now: "TEST NOW",
                    btn_cal: "CALIBRATION", btn_contam: "CONTAM", btn_clean: "CLEANING", cal_title: "CALIBRATION",
                    sensor_type: "SENSOR TYPE", unit_label: "UNIT", modbus_id: "MODBUS ID (1-247)", display_label: "DISPLAY LABEL", chart_color: "CHART COLOR",
                    relay_module_title: "DEFAULT RELAY ID", ao_module_title: "ANALOG OUTPUT ID",
                    interval_min: "Interval (Minutes)", duration_sec: "Duration (Seconds)", relay_channel: "Relay Channel",
                    opt_off: "OFF (No Cleaning)", opt_int: "INTERNAL (Sensor Wiper)", opt_ext: "EXTERNAL (Relay Action)",
                    id_tool: "ID TOOL", id_tool_title: "CHANGE SENSOR ID", id_tool_warn: "[WARNING] Only ONE device must be connected to the bus!",
                    current_id: "CURRENT ID (255=Broadcast)", new_id: "NEW ID", change_id: "CHANGE ID", std_sensors: "Standard Sensors", mlss_sensor: "MLSS Sensor",
                    sys_temp_setup: "Master Temperature Setup", sys_temp: "SYSTEM TEMP",
                    start_date: "Start Date", end_date: "End Date", search: "Search", clear: "Clear", no_relays: "No active relays",
                    io_module_type: "I/O Module (KM60xx Relay/AO)", manual_ao: "Manual Analog Output",
                    modbus_id_short: "ID", ch_short: "CH", add_action: "+ ADD NEW ACTION",
                    show_graph: "SHOW GRAPH", data_graph: "DATA GRAPH",
                    contam_label: "Contamination: ", contam_setup: "CONTAMINATION SETUP",
                    contam_enable: "Enable Contamination Alert", contam_cycle: "Cycle (Months)", reset_timer: "RESET TIMER (0%)", save_setup: "SAVE SETUP",
                    mfg_login: "WARNING: Arbitrary changes may cause system failure. Contact Manufacturer.", enter: "ENTER", off: "OFF",
                    ao_mapping: "AO Port Mapping"
                },
                ko: {
                    nav_dash: "감시화면", nav_trends: "트렌드", nav_logs: "자료조회", nav_ctrl: "제어", nav_setup: "설정", theme: "테마", exit: "종료",
                    sys_status: "시스템 상태", mini_trend: "미니 트렌드", multi_trend: "다중 트렌드 분석", show_all: "모든 라인 보기",
                    log_5min: "5분 데이터", log_1hr: "1시간 평균", log_alarm: "알람 이력", export: "자료보내기",
                    manual_or: "수동 릴레이 제어",
                    dev_net: "장치 네트워크 관리", io_setup: "I/O 설정", add_sensor: "센서 추가", change_pwd: "비밀번호 변경", ao_scaling: "4-20mA 스케일링", relay_setup: "릴레이 채널 설정",
                    export_title: "데이터 추출", export_dates: "1. 날짜 선택", export_sensors: "2. 센서 선택", cancel: "취소", download_csv: "CSV 다운로드",
                    admin_login: "관리자 로그인", password: "비밀번호", unlock: "잠금해제", new_pwd: "새 비밀번호", save: "저장", close: "닫기", lock: "잠금",
                    sw_cal: "1. HMI 소프트웨어 (y = A*x + B)", hw_cal: "2. 센서 하드웨어 (모드버스)", save_hw_cal: "하드웨어 저장",
                    clean_title: "세정(Cleaning) 설정", ctrl_mode: "제어 모드", test_now: "지금 테스트",
                    btn_cal: "교정 (CAL)", btn_contam: "오염도", btn_clean: "세정 (CLEAN)", cal_title: "센서 교정",
                    sensor_type: "센서 종류", unit_label: "단위", modbus_id: "모드버스 ID (1-247)", display_label: "표시 이름", chart_color: "차트 색상",
                    relay_module_title: "기본 릴레이 ID", ao_module_title: "아날로그 출력 ID",
                    interval_min: "작동 주기 (분)", duration_sec: "작동 시간 (초)", relay_channel: "릴레이 채널",
                    opt_off: "사용 안함 (OFF)", opt_int: "내부 와이퍼 (INTERNAL)", opt_ext: "외부 릴레이 (EXTERNAL)",
                    id_tool: "ID 변경 툴", id_tool_title: "센서 ID 변경", id_tool_warn: "[주의] 통신선에 변경할 장치 1개만 연결하세요!",
                    current_id: "현재 ID (모를경우 255)", new_id: "새로운 ID", change_id: "ID 변경", std_sensors: "일반 센서 (pH, DO, ORP 등)", mlss_sensor: "MLSS 센서",
                    sys_temp_setup: "시스템 온도 설정 (마스터)", sys_temp: "시스템 온도",
                    start_date: "시작일", end_date: "종료일", search: "조회", clear: "초기화", no_relays: "활성화된 릴레이가 없습니다",
                    io_module_type: "I/O 모듈 (KM60xx 릴레이/AO)", manual_ao: "수동 아날로그 출력 테스트",
                    modbus_id_short: "ID", ch_short: "CH", add_action: "+ 새 동작 추가",
                    show_graph: "그래프 보기", data_graph: "데이터 그래프",
                    contam_label: "센서 오염도: ", contam_setup: "오염도 알람 설정",
                    contam_enable: "오염도 알람 사용", contam_cycle: "교체 주기 (개월)",
                    reset_timer: "시간 초기화 (0%)", save_setup: "설정 저장", mfg_login: "임의적으로 조작시 문제가 발생할 수 있으니 제조사 연락바랍니다.", enter: "확인", off: "꺼짐",
                    ao_mapping: "아날로그 출력 (AO) 포트 매핑"
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
            
            function applyLogFilter() {
                filterStartDate = document.getElementById('log-start-date').value;
                filterEndDate = document.getElementById('log-end-date').value;
                update(); 
            }

            function clearLogFilter() {
                document.getElementById('log-start-date').value = '';
                document.getElementById('log-end-date').value = '';
                filterStartDate = '';
                filterEndDate = '';
                update();
            }
            
            function updateNewSensorUnits() {
                const type = document.getElementById('new-type').value;
                const unitSelect = document.getElementById('new-unit');
                unitSelect.innerHTML = allowedUnits[type].map(u => `<option value="${u}">${u}</option>`).join('');
            }

            async function triggerSave() {
                if(!configData) return;
                if(!configData.sensors) configData.sensors = {};
                if(!configData.relay_actions) configData.relay_actions = {};
                if(!configData.ao_map) configData.ao_map = {};
                
                const relayEl = document.getElementById('eng-relay-id');
                if(relayEl) configData.relay_id = parseInt(relayEl.value) || 0;
                const aoEl = document.getElementById('eng-ao-id');
                if(aoEl) configData.ao_id = parseInt(aoEl.value) || 0;
                
                if(document.getElementById('sys-t-sensor')) {
                    configData.sys_temp.sensor = document.getElementById('sys-t-sensor').value;
                    configData.sys_temp.min = parseFloat(document.getElementById('sys-t-min').value) || 0;
                    configData.sys_temp.max = parseFloat(document.getElementById('sys-t-max').value) || 50;
                }
                
                for(let i=0; i<8; i++) {
                    const el = document.getElementById('ao-map-' + i);
                    if(el) configData.ao_map[i] = el.value;
                }
                
                let needsRedraw = false;

                // Save Dynamic Relay Actions
                for(const rKey of Object.keys(configData.relay_actions)) {
                    const enEl = document.getElementById('rel-en-' + rKey);
                    const midEl = document.getElementById('rel-mid-' + rKey);
                    const chEl = document.getElementById('rel-ch-' + rKey);
                    const lblEl = document.getElementById('rel-lbl-' + rKey);
                    
                    if(enEl && midEl && chEl && lblEl) {
                        const newEn = enEl.checked;
                        const newMid = parseInt(midEl.value) || 1;
                        const newCh = parseInt(chEl.value) || 0;
                        const newLbl = lblEl.value;

                        if (configData.relay_actions[rKey].enabled !== newEn || 
                            configData.relay_actions[rKey].label !== newLbl ||
                            configData.relay_actions[rKey].modbus_id !== newMid ||
                            configData.relay_actions[rKey].ch !== newCh) {
                            
                            configData.relay_actions[rKey].enabled = newEn;
                            configData.relay_actions[rKey].modbus_id = newMid;
                            configData.relay_actions[rKey].ch = newCh;
                            configData.relay_actions[rKey].label = newLbl;
                            needsRedraw = true;
                        }
                    }
                }

                // Save Sensors Config
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
                        configData.sensors[key].c_rel_key = document.getElementById('clean-rel').value;
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
                
                if(id.startsWith('id-') || id.startsWith('min-') || id.startsWith('max-') || id.startsWith('sys-') || id === 'eng-relay-id' || id === 'eng-ao-id' || id.startsWith('clean-') || id.startsWith('cal-soft-')) {
                    triggerSave();
                }
            }
            
            function addRelayAction() {
                const newKey = 'r_' + Date.now();
                configData.relay_actions[newKey] = { label: "New Action", modbus_id: configData.relay_id || 8, ch: 0, enabled: true };
                triggerSave().then(() => initDynamicUI());
            }

            function deleteRelayAction(key) {
                customConfirm(configData.lang === 'ko' ? '이 동작을 삭제하시겠습니까?' : 'Delete this action?', () => {
                    delete configData.relay_actions[key];
                    triggerSave().then(() => initDynamicUI());
                });
            }

            function updateAoManual(ch) {
                const isActive = document.getElementById('ao-toggle-' + ch).checked;
                const box = document.getElementById('ao-ctrl-box-' + ch);
                const val = parseFloat(document.getElementById('ao-slider-' + ch).value);
                
                if (isActive) {
                    box.classList.remove('opacity-50', 'pointer-events-none');
                } else {
                    box.classList.add('opacity-50', 'pointer-events-none');
                }
                
                fetch(`/api/ao_manual?ch=${ch}&active=${isActive ? 1 : 0}&val=${val}`);
            }
            
            // --- СИНХРОНИЗАЦИЯ НОВОГО UI ---
            function syncAoUI(ch) {
                const slider = document.getElementById('ao-slider-' + ch);
                const valTxt = document.getElementById('ao-val-' + ch);
                let val = parseFloat(slider.value);
                valTxt.innerText = val.toFixed(1);
                updateAoManual(ch);
            }

            function stepAoUI(ch, step) {
                const slider = document.getElementById('ao-slider-' + ch);
                let val = parseFloat(slider.value);
                val += step;
                if (val < 4) val = 4;
                if (val > 20) val = 20;
                slider.value = val;
                syncAoUI(ch);
            }

            function setAoPreset(ch, val) {
                const slider = document.getElementById('ao-slider-' + ch);
                slider.value = val;
                syncAoUI(ch);
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
                    customAlert(configData.lang === 'ko' ? "비밀번호를 입력하세요." : "Please enter a password.");
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
                btn.innerHTML = "LOADING...";
                
                try {
                    const res = await fetch('/api/export_options?type=' + currentLogView);
                    const data = await res.json();
                    
                    if (data.status !== 'ok' || data.dates.length === 0) {
                        btn.innerHTML = "NO DATA YET";
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
                        if(c.key === 'Time') return; 
                        colsHtml += `
                        <label class="flex items-center gap-3 p-2 hover:bg-slate-200 dark:hover:bg-slate-700 rounded cursor-pointer transition-colors">
                            <input type="checkbox" class="export-col-cb w-4 h-4 text-emerald-600 rounded border-gray-300 focus:ring-emerald-500" value="${c.key}" checked>
                            <span class="text-sm font-bold text-slate-700 dark:text-slate-300">${c.label}</span>
                        </label>`;
                    });
                    document.getElementById('export-cols-container').innerHTML = colsHtml;
                    
                    document.getElementById('export-modal-overlay').classList.remove('hidden');
                } catch (e) {
                    btn.innerHTML = "ERROR";
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
                    btn.innerText = "SELECT OPTIONS";
                    btn.classList.replace('bg-emerald-600', 'bg-rose-600');
                    setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-emerald-600'); }, 2000);
                    return;
                }
                
                if (!window.pywebview || !window.pywebview.api) {
                    customAlert("System API not ready.");
                    return;
                }

                const now = new Date();
                const pad = (n) => n.toString().padStart(2, '0');
                const timestamp = `${now.getFullYear()}${pad(now.getMonth()+1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}`;
                const defaultFilename = `Export_${currentLogView}_${timestamp}.csv`;

                btn.innerText = "SELECT FOLDER...";
                const destPath = await window.pywebview.api.save_file_dialog(defaultFilename);

                if (!destPath) {
                    btn.innerText = origText; 
                    return;
                }
                
                btn.innerText = "SAVING...";
                
                try {
                    const res = await fetch('/api/export_execute', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ type: currentLogView, dates: selectedDates, columns: selectedCols, path: destPath })
                    });
                    const data = await res.json();
                    
                    if (data.status === 'ok') {
                        btn.innerText = "FILE SAVED!";
                        btn.classList.replace('bg-emerald-600', 'bg-cyan-600');
                        setTimeout(() => { closeExportModal(); btn.innerText = origText; btn.classList.replace('bg-cyan-600', 'bg-emerald-600'); }, 1500);
                    } else {
                        btn.innerText = "FAILED";
                        btn.classList.replace('bg-emerald-600', 'bg-rose-600');
                        setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-emerald-600'); }, 2000);
                    }
                } catch (e) {
                    btn.innerText = "ERROR";
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
            
            function openIdTool() {
                document.getElementById('tool-curr-id').value = '255';
                document.getElementById('tool-new-id').value = '';
                document.getElementById('id-tool-modal').classList.remove('hidden');
            }
            
            function closeIdTool() {
                document.getElementById('id-tool-modal').classList.add('hidden');
            }
            
            async function executeChangeId() {
                const sType = document.getElementById('tool-s-type').value;
                const currId = parseInt(document.getElementById('tool-curr-id').value);
                const newId = parseInt(document.getElementById('tool-new-id').value);
                
                if (isNaN(currId) || isNaN(newId)) {
                    customAlert(configData.lang === 'ko' ? '올바른 숫자를 입력하세요.' : 'Please enter valid numbers.');
                    return;
                }

                const btn = document.getElementById('btn-exec-id');
                const origText = btn.innerText;
                btn.innerText = "PROCESSING...";
                
                try {
                    const res = await fetch(`/api/change_sensor_id?curr_id=${currId}&new_id=${newId}&s_type=${sType}`);
                    const data = await res.json();
                    
                    if (data.status === 'ok') {
                        btn.innerText = "SUCCESS";
                        btn.classList.replace('bg-blue-600', 'bg-emerald-600');
                        setTimeout(() => { 
                            closeIdTool(); 
                            btn.innerText = origText; 
                            btn.classList.replace('bg-emerald-600', 'bg-blue-600');
                            customAlert(configData.lang === 'ko' ? "ID가 변경되었습니다! 센서의 전원을 껐다 켜주세요." : "ID changed! Please reboot the sensor (power off/on).");
                        }, 1000);
                    } else {
                        btn.innerText = "FAILED";
                        btn.classList.replace('bg-blue-600', 'bg-rose-600');
                        setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-blue-600'); }, 2000);
                        customAlert("Error: " + data.message);
                    }
                } catch(e) {
                    btn.innerText = "ERROR";
                    btn.classList.replace('bg-blue-600', 'bg-rose-600');
                    setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-blue-600'); }, 2000);
                }
            }
            function confirmAddSensor() {
                const type = document.getElementById('new-type').value;
                const id = parseInt(document.getElementById('new-id').value);
                const label = document.getElementById('new-label').value || type.toUpperCase();
                const color = document.getElementById('new-color').value;
                const unit = document.getElementById('new-unit').value;
                const newKey = 's_' + Date.now();
                configData.sensors[newKey] = { id: id, type: type, enabled: true, label: label, color: color, unit: unit, min: 0, max: 100, a: 1.0, b: 0.0, c_mode: "off", c_int: 30, c_dur: 10, c_rel_key: "" };
                triggerSave().then(() => { initDynamicUI(); });
                closeModal();
            }

            function deleteSensor(key) {
                const sName = configData.sensors[key].label || "SENSOR";
                const msg = configData.lang === 'ko' 
                    ? `[위험] '${sName}' 센서를 정말 삭제하시겠습니까?\n\n이 센서의 과거 데이터 열(Column)이 로그 파일에서 영구적으로 삭제됩니다. (나머지 센서 기록은 유지됨).\n\n 단순히 숨기고 싶다면 삭제하지 말고 좌측 상단의 [스위치를 OFF]로 끄는 것을 권장합니다.` 
                    : `[WARNING] Delete '${sName}' sensor?\n\nIts data column will be permanently removed from the log history (other sensors will be kept).\n\n To just hide it, we recommend turning the [Switch OFF] instead of deleting.`;
                
                customConfirm(msg, () => {
                    delete configData.sensors[key];
                    triggerSave().then(() => { initDynamicUI(); });
                });
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
                    customAlert(configData.lang === 'ko' ? "올바른 숫자를 입력하세요." : "Please enter valid numeric values.");
                    return;
                }

                await triggerSave(); 

                const origText = btn.innerText;
                btn.innerText = "WRITING...";

                try {
                    const res = await fetch(`/api/set_cal?sensor_id=${id}&s_type=${type}&k=${hwK}&b=${hwB}`);
                    const data = await res.json();
                    if (data.status === 'ok') {
                        btn.innerText = "SAVED";
                        btn.classList.replace('bg-indigo-600', 'bg-emerald-600');
                        btn.classList.replace('border-indigo-700', 'border-emerald-700');
                        setTimeout(() => { 
                            closeCalModal(); 
                            btn.innerText = origText; 
                            btn.classList.replace('bg-emerald-600', 'bg-indigo-600');
                            btn.classList.replace('border-emerald-700', 'border-indigo-700');
                        }, 1200);
                    } else {
                        btn.innerText = "HW FAILED";
                        btn.classList.replace('bg-indigo-600', 'bg-rose-600');
                        setTimeout(() => { btn.innerText = origText; btn.classList.replace('bg-rose-600', 'bg-indigo-600'); }, 2000);
                    }
                } catch(e) {
                    btn.innerText = "HW FAILED";
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
                
                const relSelect = document.getElementById('clean-rel');
                relSelect.innerHTML = '';
                for(const [rKey, r] of Object.entries(configData.relay_actions || {})) {
                    if(r.enabled) {
                        relSelect.innerHTML += `<option value="${rKey}">ID ${r.modbus_id} : CH ${r.ch} - ${r.label}</option>`;
                    }
                }
                if(relSelect.innerHTML === '') relSelect.innerHTML = '<option value="">No Relays Enabled</option>';
                
                document.getElementById('clean-rel').value = s.c_rel_key || "";
                
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
                const relKey = document.getElementById('clean-rel').value;
                
                const btn = document.getElementById('btn-test-clean');
                const origHTML = btn.innerHTML;
                const origClasses = btn.className;
                
                btn.innerHTML = "RUNNING...";
                btn.className = "flex items-center px-4 py-2 bg-sky-500 text-white font-bold rounded text-xs shadow transition-colors";

                await triggerSave();

                try {
                    const res = await fetch(`/api/trigger_clean?key=${key}&mode=${mode}&dur=${dur}&rel_key=${relKey}`);
                    const data = await res.json();
                    
                    if (data.status === 'ok') {
                        btn.innerHTML = "TRIGGERED";
                        btn.classList.replace('bg-sky-500', 'bg-emerald-500');
                    } else {
                        btn.innerHTML = "FAILED";
                        btn.classList.replace('bg-sky-500', 'bg-rose-500');
                    }
                } catch(e) {
                    btn.innerHTML = "ERROR";
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
                if (!configData.relay_actions) configData.relay_actions = {};
                
                const activeSensors = Object.entries(configData.sensors || {}).filter(([k, v]) => v.enabled);
                
                // === МАГИЯ РАСЩЕПЛЕНИЯ UV254 ===
                let displayCards = [];
                activeSensors.forEach(([key, s]) => {
                    if (s.type === 'uv254') {
                        // Создаем 4 виртуальные карточки
                        displayCards.push({ id: key, origKey: key, label: 'TOC', unit: s.unit || 'mg/L', color: s.color });
                        displayCards.push({ id: key + '_cod', origKey: key, label: 'CODcr', unit: s.unit || 'mg/L', color: s.color });
                        displayCards.push({ id: key + '_tb', origKey: key, label: 'TURBIDITY', unit: 'NTU', color: s.color });
                        displayCards.push({ id: key + '_temp', origKey: key, label: 'TEMP', unit: '°C', color: s.color });
                    } else {
                        // Обычный датчик
                        displayCards.push({ id: key, origKey: key, label: s.label, unit: s.unit, color: s.color });
                    }
                });

                const count = displayCards.length;
                
                const grid = document.getElementById('dashboard-grid');
                grid.innerHTML = '';
                const dashChartWrap = document.getElementById('dash-chart-wrapper');
                
                if (count === 1) {
                    grid.className = "grid gap-4 grid-cols-1 grid-rows-1 flex-grow min-h-0";
                    dashChartWrap.classList.remove('hidden');
                    dashChartWrap.style.display = 'flex';
                } else if (count === 2) {
                    grid.className = "grid gap-4 grid-cols-2 grid-rows-1 flex-grow min-h-0"; 
                    dashChartWrap.classList.remove('hidden');
                    dashChartWrap.style.display = 'flex';
                } else if (count === 3 || count === 4) {
                    grid.className = "grid gap-4 grid-cols-2 grid-rows-2 flex-grow min-h-0";
                    dashChartWrap.classList.add('hidden');
                    dashChartWrap.style.display = 'none';
                } else if (count === 5 || count === 6) {
                    grid.className = "grid gap-4 grid-cols-2 grid-rows-3 flex-grow min-h-0"; 
                    dashChartWrap.classList.add('hidden');
                    dashChartWrap.style.display = 'none';
                } else {
                    grid.className = "grid gap-4 grid-cols-2 grid-rows-4 flex-grow min-h-0"; 
                    dashChartWrap.classList.add('hidden');
                    dashChartWrap.style.display = 'none';
                }

                if (count === 0) {
                    const textNoSensors = configData.lang === 'ko' ? "활성화된 센서가 없습니다. 설정(SETUP)으로 이동하세요." : "NO SENSORS ENABLED. GO TO SETUP.";
                    grid.innerHTML = `<div class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg p-10 flex items-center justify-center text-slate-500 font-bold col-span-full">${textNoSensors}</div>`;
                }

                let valSize, unitSize, lblSize;
                if (count === 1) { valSize = '28vh'; unitSize = '6vh'; lblSize = '4vh'; }
                else if (count === 2) { valSize = '16vh'; unitSize = '4.5vh'; lblSize = '3vh'; }
                else if (count <= 4) { valSize = '12vh'; unitSize = '3.5vh'; lblSize = '2.5vh'; }
                else if (count <= 6) { valSize = '10vh'; unitSize = '3vh'; lblSize = '2vh'; }
                else { valSize = '6vh'; unitSize = '2vh'; lblSize = '1.5vh'; }

                // Отрисовка сгенерированных карточек
                displayCards.forEach((card, index) => {
                    let spanClass = "";
                    if ((count === 3 && index === 0) || (count === 5 && index === 0)) {
                        spanClass = "col-span-2"; 
                    }
                    
                    // Проверяем, включено ли загрязнение, и если нет - добавляем класс hidden
                    const s_cfg = configData.sensors[card.origKey];
                    const isContamOn = s_cfg && s_cfg.contam && s_cfg.contam.enabled;
                    const hideContam = isContamOn ? '' : 'hidden';

                    grid.innerHTML += `
                    <div id="card-${card.id}" onclick="focusChart('${card.origKey}')" class="card bg-white dark:bg-[#0f172a] border border-slate-300 dark:border-[#1e293b] rounded-lg sensor-card relative overflow-hidden group flex items-center justify-center ${spanClass}">
                        <div class="absolute top-4 left-5 font-black uppercase tracking-wider" style="color: ${card.color}; font-size: ${lblSize};">${card.label}</div>
                        
                        <div class="flex items-baseline justify-center">
                            <p id="v-${card.id}" class="font-black text-slate-800 dark:text-white leading-none tracking-tighter transition-colors" style="font-size: ${valSize};">--</p>
                            <span class="text-slate-500 font-bold ml-3" style="font-size: ${unitSize};">${card.unit}</span>
                        </div>

                        <div id="contam-box-${card.id}" class="absolute bottom-2.5 right-2.5 w-auto text-right px-3 py-2 cursor-pointer bg-slate-100 dark:bg-slate-800/60 hover:bg-slate-200 dark:hover:bg-slate-700 transition-colors rounded-lg border border-slate-200 dark:border-slate-700/50 shadow-inner no-select ${hideContam}" onclick="event.stopPropagation(); promptMfgPwd('${card.origKey}')">
                            <span class="text-xs font-bold text-slate-500 uppercase tracking-widest pl-0.5" data-i18n="contam_label">Contamination: </span> 
                            <span id="contam-val-${card.id}" class="text-sm font-black text-slate-400 pl-0.5 pr-0.5">OFF</span>
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
                let tempOpts = `<option value="">-- Select --</option>`;

                for(const [key, s] of Object.entries(configData.sensors)) {
                    const lang = configData.lang === 'ko' ? 'ko' : 'en';
                    const unitOptions = allowedUnits[s.type] ? allowedUnits[s.type].map(u => `<option value="${u}" ${s.unit === u ? 'selected' : ''}>${u}</option>`).join('') : `<option value="">--</option>`;
                    const safeLabel = s.label ? s.label.replace(/'/g, "\\'") : "SENSOR";
                    
                    if(s.enabled && s.type !== 'mlss') {
                        tempOpts += `<option value="${key}" ${configData.sys_temp.sensor === key ? 'selected' : ''}>${s.label}</option>`;
                    }

                    const unitDropdownHTML = `
                    <div class="ml-3 pl-3 border-l border-slate-300 dark:border-slate-700">
                        <select id="unit-${key}" onchange="triggerSave()" class="bg-white dark:bg-slate-200 border border-slate-300 dark:border-slate-600 rounded py-1 px-2 text-slate-900 dark:text-black font-bold text-xs shadow-sm focus:outline-none focus:ring-1 focus:ring-cyan-500 cursor-pointer">
                            ${unitOptions}
                        </select>
                    </div>`;

                    const uv254DetailsHTML = `
                    <div class="mt-3 bg-white dark:bg-slate-900/50 border border-slate-200 dark:border-slate-700 rounded p-2 grid grid-cols-2 gap-x-3 gap-y-2 shadow-inner">
                        <div class="flex justify-between items-center text-[10px] font-bold px-1">
                            <span class="text-slate-600 dark:text-slate-400">1. TOC</span>
                            <select id="unit-${key}" onchange="triggerSave()" class="bg-slate-100 dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded py-0.5 text-slate-900 dark:text-black font-bold text-[10px] shadow-sm focus:outline-none cursor-pointer w-14 text-center">
                                ${unitOptions}
                            </select>
                        </div>
                        <div class="flex justify-between items-center text-[10px] font-bold px-2 border-l border-slate-200 dark:border-slate-700">
                            <span class="text-slate-600 dark:text-slate-400 truncate pr-1">3. Turbidity</span>
                            <span class="text-slate-500 font-black">NTU</span>
                        </div>
                        <div class="flex justify-between items-center text-[10px] font-bold px-1">
                            <span class="text-slate-600 dark:text-slate-400">2. CODcr</span>
                            <span class="text-slate-400 dark:text-slate-500 text-[9px] pr-1">(=TOC)</span>
                        </div>
                        <div class="flex justify-between items-center text-[10px] font-bold px-2 border-l border-slate-200 dark:border-slate-700">
                            <span class="text-slate-600 dark:text-slate-400">4. Temp</span>
                            <span class="text-slate-500 font-black">°C</span>
                        </div>
                    </div>`;

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
                                ${s.type !== 'uv254' ? unitDropdownHTML : ''}
                            </div>
                        </div>
                        
                        ${s.type === 'uv254' ? uv254DetailsHTML : ''}
                                                
                        <div class="mt-4 pt-4 border-t border-slate-300 dark:border-slate-700/50 flex gap-2">
                            <button onclick="openCalModal('${key}', ${s.id}, '${s.type}', '${safeLabel}')" class="flex-1 bg-indigo-100 text-indigo-700 hover:bg-indigo-200 dark:bg-indigo-900/40 dark:text-indigo-400 dark:hover:bg-indigo-800/60 py-2 rounded text-[11px] font-black uppercase tracking-wider border border-indigo-200 dark:border-indigo-800 transition-colors shadow-sm flex items-center justify-center gap-1">
                                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-3.5 h-3.5"><path stroke-linecap="round" stroke-linejoin="round" d="M10.5 6h9.75M10.5 6a1.5 1.5 0 11-3 0m3 0a1.5 1.5 0 10-3 0M3.75 6H7.5m3 12h9.75m-9.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-3.75 0H7.5m9-6h3.75m-3.75 0a1.5 1.5 0 01-3 0m3 0a1.5 1.5 0 00-3 0m-9.75 0h9.75" /></svg>
                                <span data-i18n="btn_cal">CAL</span>
                            </button>
                            <button onclick="openCleanModal('${key}', '${safeLabel}')" class="flex-1 bg-sky-100 text-sky-700 hover:bg-sky-200 dark:bg-sky-900/40 dark:text-sky-400 dark:hover:bg-sky-800/60 py-2 rounded text-[11px] font-black uppercase tracking-wider border border-sky-200 dark:border-sky-800 transition-colors shadow-sm flex items-center justify-center gap-1">
                                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-3.5 h-3.5"><path stroke-linecap="round" stroke-linejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 001.423 1.423l1.183.394-1.183.394a2.25 2.25 0 00-1.423 1.423z" /></svg>
                                <span data-i18n="btn_clean">CLEAN</span>
                            </button>
                            <button onclick="promptMfgPwd('${key}')" class="flex-1 bg-emerald-100 text-emerald-700 hover:bg-emerald-200 dark:bg-emerald-900/40 dark:text-emerald-400 dark:hover:bg-emerald-800/60 py-2 rounded text-[11px] font-black uppercase tracking-wider border border-emerald-200 dark:border-emerald-800 transition-colors shadow-sm flex items-center justify-center gap-1">
                                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-3.5 h-3.5"><path stroke-linecap="round" stroke-linejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" /></svg>
                                <span data-i18n="btn_contam">CONTAM</span>
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
                
                // === НОВАЯ КАРТОЧКА AO MAPPING ===
                let aoMapHTML = `
                <div class="bg-orange-50 dark:bg-orange-900/20 p-4 rounded-lg border border-orange-200 dark:border-orange-800 mb-4 shrink-0">
                    <h3 class="text-sm font-black text-orange-700 dark:text-orange-400 uppercase tracking-widest mb-3" data-i18n="ao_mapping">AO Port Mapping</h3>
                    <div class="grid grid-cols-1 xl:grid-cols-2 gap-2">
                `;

                let srcOpts = `<option value="off" data-i18n="opt_off">OFF (Not Used)</option><option value="sys_temp">SYS TEMP</option>`;
                for(const [key, s] of Object.entries(configData.sensors)) {
                    if(s.enabled) srcOpts += `<option value="${key}">${s.label}</option>`;
                }

                for(let i=0; i<8; i++) {
                    const currentSrc = configData.ao_map && configData.ao_map[i] ? configData.ao_map[i] : 'off';
                    const mappedOpts = srcOpts.replace(`value="${currentSrc}"`, `value="${currentSrc}" selected`);
                    aoMapHTML += `
                    <div class="flex items-center justify-between bg-white dark:bg-slate-800 p-2 rounded border border-slate-300 dark:border-slate-600 shadow-sm">
                        <span class="text-xs font-black text-slate-500 dark:text-slate-400 w-8">CH ${i}</span>
                        <select id="ao-map-${i}" onchange="triggerSave()" class="w-full bg-transparent text-xs font-bold outline-none text-slate-800 dark:text-white cursor-pointer truncate">
                            ${mappedOpts}
                        </select>
                    </div>`;
                }
                aoMapHTML += `</div></div>`;

                // === ОБНОВЛЕННАЯ ТЕМПЕРАТУРА (БЕЗ ВЫБОРА CH) ===
                let sysTempHTML = `
                <div class="bg-blue-50 dark:bg-blue-900/20 p-4 rounded-lg border border-blue-200 dark:border-blue-800 mb-4 shrink-0">
                    <h3 class="text-sm font-black text-blue-700 dark:text-blue-400 uppercase tracking-widest mb-3 flex items-center gap-2">
                        <span data-i18n="sys_temp_setup">Master Temperature Setup</span>
                    </h3>
                    <div class="flex flex-col gap-3">
                        <div class="flex items-center justify-between">
                            <span class="text-xs font-bold text-slate-600 dark:text-slate-300 uppercase">Master Sensor</span>
                            <select id="sys-t-sensor" onchange="triggerSave()" class="w-full max-w-[150px] bg-white dark:bg-slate-800 border border-slate-300 dark:border-slate-600 rounded py-1.5 px-2 text-xs font-bold focus:outline-none dark:text-black">
                                ${tempOpts}
                            </select>
                        </div>
                        <div class="flex gap-2 mt-1">
                            <div class="flex-1 flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden bg-white dark:bg-slate-800">
                                <span class="px-2 py-1.5 text-[10px] font-black text-slate-500 border-r border-slate-300 dark:border-slate-600">4mA (°C)</span>
                                <input type="number" id="sys-t-min" onchange="triggerSave()" value="${configData.sys_temp.min}" class="w-full text-center bg-transparent text-xs font-bold outline-none dark:text-white">
                            </div>
                            <div class="flex-1 flex items-center border border-slate-300 dark:border-slate-600 rounded overflow-hidden bg-white dark:bg-slate-800">
                                <span class="px-2 py-1.5 text-[10px] font-black text-slate-500 border-r border-slate-300 dark:border-slate-600">20mA (°C)</span>
                                <input type="number" id="sys-t-max" onchange="change" triggerSave()" value="${configData.sys_temp.max}" class="w-full text-center bg-transparent text-xs font-bold outline-none dark:text-white">
                            </div>
                        </div>
                    </div>
                </div>
                `;

                let relaysEngHTML = '';
                for(const [rKey, r] of Object.entries(configData.relay_actions || {})) {
                    relaysEngHTML += `
                    <div class="flex items-center gap-3 bg-slate-50 dark:bg-slate-800/50 p-2.5 rounded border border-slate-200 dark:border-slate-700">
                        <label class="relative inline-flex items-center cursor-pointer shrink-0">
                            <input type="checkbox" id="rel-en-${rKey}" onchange="triggerSave()" ${r.enabled ? 'checked' : ''} class="sr-only peer">
                            <div class="w-9 h-5 bg-slate-300 dark:bg-slate-600 rounded-full peer-checked:bg-purple-500 after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:after:translate-x-[16px]"></div>
                        </label>
                        <div class="flex flex-col gap-1 w-16 shrink-0">
                            <span class="text-[9px] text-slate-400 font-bold uppercase tracking-widest leading-none" data-i18n="modbus_id_short">ID</span>
                            <input type="number" id="rel-mid-${rKey}" onchange="triggerSave()" value="${r.modbus_id}" class="w-full bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-600 rounded px-1 py-0.5 text-xs font-black text-center outline-none text-slate-800 dark:text-white">
                        </div>
                        <div class="flex flex-col gap-1 w-12 shrink-0">
                            <span class="text-[9px] text-slate-400 font-bold uppercase tracking-widest leading-none" data-i18n="ch_short">CH</span>
                            <input type="number" id="rel-ch-${rKey}" onchange="triggerSave()" value="${r.ch}" class="w-full bg-white dark:bg-slate-900 border border-slate-300 dark:border-slate-600 rounded px-1 py-0.5 text-xs font-black text-center outline-none text-slate-800 dark:text-white">
                        </div>
                        <div class="flex flex-col gap-1 flex-1">
                            <span class="text-[9px] text-slate-400 font-bold uppercase tracking-widest leading-none">LABEL</span>
                            <input type="text" id="rel-lbl-${rKey}" onchange="triggerSave()" value="${r.label}" class="w-full bg-transparent text-sm font-bold outline-none text-slate-800 dark:text-white border-b border-transparent focus:border-purple-500 transition-colors px-1" placeholder="Action Name">
                        </div>
                        <button onclick="deleteRelayAction('${rKey}')" class="text-rose-500 hover:text-rose-600 shrink-0 p-1"><svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" class="w-4 h-4"><path stroke-linecap="round" stroke-linejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" /></svg></button>
                    </div>`;
                }
                relaysEngHTML += `<button onclick="addRelayAction()" class="mt-2 w-full py-2 bg-purple-100 text-purple-700 hover:bg-purple-200 dark:bg-purple-900/30 dark:text-purple-400 dark:hover:bg-purple-800/50 rounded text-xs font-black uppercase tracking-wider transition-colors border border-purple-200 dark:border-purple-800 border-dashed" data-i18n="add_action">+ ADD NEW ACTION</button>`;

                let ctrlHTML = '';
                for(const [rKey, r] of Object.entries(configData.relay_actions || {})) {
                    if(r.enabled) {
                        ctrlHTML += `
                        <div class="flex items-center justify-between bg-slate-100 dark:bg-slate-800/40 p-5 rounded-xl border border-slate-300 dark:border-slate-700/50 shadow-sm">
                            <div class="flex flex-col">
                                <span class="text-sm font-bold text-slate-700 dark:text-slate-300 uppercase pr-4">${r.label}</span>
                                <span class="text-[10px] font-black text-slate-400 uppercase tracking-widest">ID: ${r.modbus_id} | CH: ${r.ch}</span>
                            </div>
                            <label class="relative inline-flex items-center cursor-pointer shrink-0">
                                <input type="checkbox" id="relay-toggle-${rKey}" onchange="fetch('/api/relay?key=${rKey}&state='+(this.checked?1:0))" class="sr-only peer">
                                <div class="w-14 h-7 bg-slate-300 dark:bg-slate-900 rounded-full border border-slate-400 dark:border-slate-600 peer-checked:bg-cyan-500 transition-colors after:content-[''] after:absolute after:top-[3px] after:left-[3px] after:bg-white dark:after:bg-slate-400 after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:after:translate-x-[28px] peer-checked:after:bg-white"></div>
                            </label>
                        </div>`;
                    }
                }
                
                const ctrlGrid = document.getElementById('ctrl-relay-grid');
                if(ctrlGrid) ctrlGrid.innerHTML = ctrlHTML || '<div class="col-span-2 text-center text-slate-500 font-bold py-10" data-i18n="no_relays">No active relays</div>';

                document.getElementById('eng-sensors').innerHTML = engHTML;
                document.getElementById('eng-ao-scaling').innerHTML = aoMapHTML + sysTempHTML + aoHTML;
                document.getElementById('eng-relays-list').innerHTML = relaysEngHTML;
                
                isInitialized = true;
                applyLang();
                setChartMode('all');
            }

            async function update() {
                try {
                    const res = await fetch(`/api/all?log_start=${filterStartDate}&log_end=${filterEndDate}`);
                    const d = await res.json();
                    
                    if(!isInitialized) { 
                        configData = d.config; 
                        if (!configData.sensors) configData.sensors = {}; 
                        if (!configData.relay_actions) configData.relay_actions = {};
                        
                        if (configData.theme && !themeInitialized) {
                            themeInitialized = true;
                            if (configData.theme === 'light') {
                                document.documentElement.classList.remove('dark');
                                isDark = false;
                            } else {
                                document.documentElement.classList.add('dark');
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
                        
                        if (configData.sys_temp && configData.sys_temp.sensor) {
                            const masterName = configData.sensors[configData.sys_temp.sensor] ? configData.sensors[configData.sys_temp.sensor].label : '';
                            sidebarHTML += `
                            <div class="card p-4 rounded-lg flex flex-col gap-1 border border-blue-300 dark:border-blue-800 bg-blue-50 dark:bg-blue-900/20 shrink-0 mb-2 shadow-sm">
                                <div class="flex justify-between items-center mb-1 pb-2 border-b border-blue-200 dark:border-blue-800/50">
                                    <span class="text-xs font-black text-blue-700 dark:text-blue-400 uppercase flex items-center gap-1.5"><span data-i18n="sys_temp">SYS TEMP</span></span>
                                    <span class="text-[10px] font-bold text-blue-500 dark:text-blue-400 truncate max-w-[100px] uppercase">${masterName}</span>
                                </div>
                                <div class="flex justify-between items-end mt-1">
                                    <div class="flex items-baseline gap-1">
                                        <span class="font-black text-slate-800 dark:text-white text-3xl tracking-tighter">${d.data.sys_temp || '--'}</span>
                                        <span class="text-sm font-bold text-slate-500">°C</span>
                                    </div>
                                    <div class="text-right">
                                        <span class="text-[10px] text-slate-500 font-bold block mb-0.5 uppercase tracking-widest">OUT (AO):</span>
                                        <span class="font-mono text-blue-600 dark:text-blue-400 font-black text-base tracking-wider">${d.data.sys_temp_ao || '--'} <span class="text-[10px]">mA</span></span>
                                    </div>
                                </div>
                            </div>
                            `;
                        }

                        const activeSensors = Object.entries(configData.sensors || {}).filter(([k, v]) => v.enabled);
                        
                        activeSensors.forEach(([key, s]) => {
                            const d_s = d.data[key];
                            if(!d_s) return;
                            
                            const isErr = d_s.status === 'ERR' || d_s.val === 'Err';
                            const statColor = isErr ? 'text-rose-500' : 'text-emerald-600 dark:text-emerald-400';
                            const dotColor = isErr ? 'bg-rose-500' : 'bg-emerald-500 animate-pulse';
                            
                            // --- ОБНОВЛЕНИЕ ЗАГРЯЗНЕНИЯ ---
                            const subIds = s.type === 'uv254' ? [key, key+'_cod', key+'_tb', key+'_temp'] : [key];
                            subIds.forEach(id => {
                                const cVal = document.getElementById('contam-val-' + id);
                                if(cVal) {
                                    if(s.contam && s.contam.enabled) {
                                        cVal.innerText = (d_s.contam_pct || 0) + '%';
                                        cVal.className = "text-sm font-black text-rose-500 pl-0.5 pr-0.5";
                                    } else {
                                        cVal.innerText = translations[configData.lang === 'ko' ? 'ko' : 'en'].off;
                                        cVal.className = "text-sm font-black text-slate-400 pl-0.5 pr-0.5";
                                    }
                                }
                            });
                            
                            // === ОБНОВЛЕНИЕ РАСЩЕПЛЕННОГО UV254 ===
                            if (s.type === 'uv254') {
                                const els = [
                                    { id: 'v-' + key, val: d_s.val },
                                    { id: 'v-' + key + '_cod', val: d_s.cod },
                                    { id: 'v-' + key + '_tb', val: d_s.turb },
                                    { id: 'v-' + key + '_temp', val: d_s.temp }
                                ];
                                
                                els.forEach(el => {
                                    const domEl = document.getElementById(el.id);
                                    if (domEl) {
                                        domEl.innerText = el.val || '--';
                                        if (isErr || el.val === 'Err') domEl.classList.add('text-rose-500');
                                        else domEl.classList.remove('text-rose-500');
                                    }
                                });
                            } else {
                                // Обычный датчик
                                const el = document.getElementById('v-' + key);
                                if(el) {
                                    el.innerText = d_s.val;
                                    if (isErr) el.classList.add('text-rose-500');
                                    else el.classList.remove('text-rose-500');
                                }
                            }
                            
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

                    // === ДИНАМИЧЕСКИЙ ПЕРЕСЧЕТ ЛОГОВ (PRESENTATION LAYER) ===
                    if(currentTab === 'logs') {
                        const targetLog = d.logs[currentLogView];
                        const thead = document.getElementById('log-head');
                        const tbody = document.getElementById('log-body');
                        
                        if(targetLog && targetLog.headers.length > 0) {
                            
                            // 1. Динамически создаем красивые заголовки на основе актуальных настроек
                            const uiHeaders = targetLog.headers.map(h => {
                                if (h === 'Time') return 'TIME';
                                if (h === 'sys_temp') return 'SYS TEMP (°C)';
                                const s = configData.sensors[h];
                                if (s) return `${s.label} (${s.unit})`;
                                return h;
                            });

                            // 2. Динамически пересчитываем сырые данные из CSV в нужные единицы измерения
                            const uiRows = targetLog.rows.map(row => {
                                return row.map((valStr, i) => {
                                    const h = targetLog.headers[i];
                                    if (h === 'Time' || h === 'sys_temp') return valStr;
                                    
                                    const s = configData.sensors[h];
                                    if (s && valStr !== '--' && valStr !== 'Err' && valStr !== 'Off' && !isNaN(valStr)) {
                                        let raw = parseFloat(valStr);
                                        // Применяем математику "на лету"
                                        if (s.type === 'oil' && (s.unit === 'mg/L' || s.unit === 'ppm')) raw /= 1000.0;
                                        else if (s.type === 'mlss' && s.unit === 'g/L') raw /= 1000.0;
                                        else if (s.type === 'mlss' && s.unit === '%') raw /= 10000.0;
                                        else if (s.type === 'ec' && s.unit === 'mS/cm') raw /= 1000.0;
                                        
                                        return s.type === 'orp' ? raw.toFixed(1) : raw.toFixed(2);
                                    }
                                    return valStr;
                                });
                            });

                            targetLog.uiHeaders = uiHeaders;
                            targetLog.uiRows = uiRows;

                            thead.innerHTML = `<tr class="bg-slate-200 dark:bg-slate-800"><th class="px-6 py-4 font-black tracking-wider uppercase text-slate-700 dark:text-slate-300">${uiHeaders.join('</th><th class="px-6 py-4 font-black tracking-wider uppercase text-slate-700 dark:text-slate-300">')}</th></tr>`;
                            
                            tbody.innerHTML = uiRows.map(row => {
                                while(row.length < uiHeaders.length) row.push('--');
                                
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
                        for(const rKey of Object.keys(configData.relay_actions || {})) {
                            const toggle = document.getElementById('relay-toggle-' + rKey);
                            if(toggle && document.activeElement !== toggle) toggle.checked = (d.relays[rKey] === 1);
                        }
                        
                        for(let i=0; i<8; i++) {
                            const ao = d.ao_manual[i];
                            if(ao) {
                                const toggle = document.getElementById('ao-toggle-' + i);
                                const slider = document.getElementById('ao-slider-' + i);
                                const box = document.getElementById('ao-ctrl-box-' + i);
                                const valTxt = document.getElementById('ao-val-' + i);
                                
                                if(toggle && document.activeElement !== toggle) {
                                    toggle.checked = ao.active;
                                    if(ao.active) box.classList.remove('opacity-50', 'pointer-events-none');
                                    else box.classList.add('opacity-50', 'pointer-events-none');
                                }
                                if(slider && document.activeElement !== slider) {
                                    slider.value = ao.val;
                                    valTxt.innerText = parseFloat(ao.val).toFixed(1);
                                }
                            }
                        }
                    }
                } catch (e) {}
            }

            setInterval(update, 1000);
            
            async function autoDetectIo(type, inputId, btnId) {
                const btn = document.getElementById(btnId);
                const origText = btn.innerHTML;
                const origClasses = btn.className;
                
                btn.innerHTML = "SCANNING...";
                btn.className = "px-3 py-1.5 bg-amber-500 text-white rounded text-[10px] font-black uppercase tracking-wider transition-colors";
                
                try {
                    const res = await fetch(`/api/scan_io?module_type=${type}`);
                    const data = await res.json();
                    
                    if(data.status === 'ok') {
                        document.getElementById(inputId).value = data.id;
                        await triggerSave(); 
                        
                        btn.innerHTML = `FOUND: ID ${data.id}`;
                        btn.className = "px-3 py-1.5 bg-emerald-500 text-white rounded text-[10px] font-black uppercase tracking-wider transition-colors";
                    } else {
                        btn.innerHTML = "NOT FOUND";
                        btn.className = "px-3 py-1.5 bg-rose-500 text-white rounded text-[10px] font-black uppercase tracking-wider transition-colors";
                    }
                } catch(e) {
                    btn.innerHTML = "ERROR";
                    btn.className = "px-3 py-1.5 bg-rose-500 text-white rounded text-[10px] font-black uppercase tracking-wider transition-colors";
                }
                
                setTimeout(() => {
                    btn.innerHTML = origText;
                    btn.className = origClasses;
                }, 3000);
            }
            
            // --- ГРАФИКИ ЛОГОВ ---
            let logFullChartInstance = null;
            async function openLogChart() {
                const btn = document.getElementById('btn-show-graph');
                const lang = configData.lang === 'ko' ? 'ko' : 'en';
                btn.innerText = lang === 'ko' ? "로딩중..." : "LOADING...";
                
                try {
                    const res = await fetch(`/api/all?log_start=${filterStartDate}&log_end=${filterEndDate}`);
                    const d = await res.json();
                    const targetLog = d.logs[currentLogView];
                    
                    if(!targetLog || targetLog.rows.length === 0) {
                        btn.innerText = lang === 'ko' ? "데이터 없음" : "NO DATA";
                        setTimeout(() => btn.innerText = translations[lang].show_graph, 2000);
                        return;
                    }

                    // --- УМНОЕ ФОРМАТИРОВАНИЕ ДАННЫХ ДЛЯ ГРАФИКА ---
                    const uiHeaders = targetLog.headers.map(h => {
                        if (h === 'Time') return 'TIME';
                        if (h === 'sys_temp') return 'SYS TEMP (°C)';
                        const s = configData.sensors[h];
                        if (s) return `${s.label} (${s.unit})`;
                        return h;
                    });

                    const uiRows = targetLog.rows.map(row => {
                        return row.map((valStr, i) => {
                            const h = targetLog.headers[i];
                            if (h === 'Time' || h === 'sys_temp') return valStr;
                            
                            const s = configData.sensors[h];
                            if (s && valStr !== '--' && valStr !== 'Err' && valStr !== 'Off' && !isNaN(valStr)) {
                                let raw = parseFloat(valStr);
                                if (s.type === 'oil' && (s.unit === 'mg/L' || s.unit === 'ppm')) raw /= 1000.0;
                                else if (s.type === 'mlss' && s.unit === 'g/L') raw /= 1000.0;
                                else if (s.type === 'mlss' && s.unit === '%') raw /= 10000.0;
                                else if (s.type === 'ec' && s.unit === 'mS/cm') raw /= 1000.0;
                                return s.type === 'orp' ? raw.toFixed(1) : raw.toFixed(2);
                            }
                            return valStr;
                        });
                    });
                    // -----------------------------------------------

                    document.getElementById('log-chart-modal').classList.remove('hidden');
                    const ctx = document.getElementById('logFullCanvas').getContext('2d');
                    if(logFullChartInstance) logFullChartInstance.destroy();

                    const labels = uiRows.map(r => r[0].split(' ')[1] || r[0]);
                    const datasets = [];
                    const colors = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4'];

                    for(let i=1; i<targetLog.headers.length; i++) {
                        if(targetLog.headers[i].includes('SYS TEMP')) continue;
                        datasets.push({
                            label: uiHeaders[i], // Используем красивые названия
                            data: uiRows.map(r => parseFloat(r[i]) || null), // Используем пересчитанные значения
                            borderColor: colors[i % colors.length],
                            borderWidth: 2, tension: 0.1, pointRadius: 1
                        });
                    }

                    logFullChartInstance = new Chart(ctx, {
                        type: 'line',
                        data: { labels: labels, datasets: datasets },
                        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { position: 'top', labels:{color: isDark?'#cbd5e1':'#475569'} } }, scales: { x:{ticks:{color: isDark?'#64748b':'#94a3b8'}}, y:{grid:{color: isDark?'rgba(255,255,255,0.05)':'rgba(0,0,0,0.05)'}, ticks:{color: isDark?'#64748b':'#94a3b8'}} } }
                    });
                } catch(e) {}
                btn.innerText = translations[lang].show_graph;
            }

            // --- ЗАГРЯЗНЕНИЕ ---
            let currentContamKey = null;
            const MFG_PWD = "9999"; 

            function promptMfgPwd(key) {
                currentContamKey = key;
                document.getElementById('mfg-pwd-input').value = '';
                document.getElementById('mfg-pwd-modal').classList.remove('hidden');
                setTimeout(()=>document.getElementById('mfg-pwd-input').focus(), 100);
            }

            function verifyMfgPwd() {
                const pwd = document.getElementById('mfg-pwd-input').value;
                if(pwd === MFG_PWD) {
                    document.getElementById('mfg-pwd-modal').classList.add('hidden');
                    openContamSetup(currentContamKey);
                } else {
                    document.getElementById('mfg-pwd-input').value = '';
                    customAlert(configData.lang === 'ko' ? "인가되지 않은 접근입니다! (비밀번호 오류)" : "Unauthorized Access! (Wrong Password)");
                }
            }

            function openContamSetup(key) {
                const s = configData.sensors[key];
                document.getElementById('contam-s-key').value = key;
                document.getElementById('contam-enabled').checked = s.contam ? s.contam.enabled : false;
                const monthTxt = configData.lang === 'ko' ? '개월' : 'Months';
                document.getElementById('contam-months').innerHTML = [1,2,3,4,5,6,7,8,9,10,11,12].map(m=>`<option value="${m}">${m} ${monthTxt}</option>`).join('');
                document.getElementById('contam-months').value = s.contam ? s.contam.months : 3;
                
                document.getElementById('contam-setup-modal').classList.remove('hidden');
            }

            function resetContamTimer() {
                customConfirm(configData.lang==='ko'?'오염도 시간을 0%로 초기화 하시겠습니까?':'Reset contamination timer to 0%?', async () => {
                    const key = document.getElementById('contam-s-key').value;
                    if(!configData.sensors[key].contam) configData.sensors[key].contam = {};
                    configData.sensors[key].contam.start_ts = Date.now() / 1000;
                    await triggerSave();
                    customAlert("Timer Reset Successful!");
                });
            }

            async function saveContamSetup() {
                const key = document.getElementById('contam-s-key').value;
                if(!configData.sensors[key].contam) configData.sensors[key].contam = {};
                
                configData.sensors[key].contam.enabled = document.getElementById('contam-enabled').checked;
                configData.sensors[key].contam.months = parseInt(document.getElementById('contam-months').value);
                
                if(configData.sensors[key].contam.enabled && !configData.sensors[key].contam.start_ts) {
                    configData.sensors[key].contam.start_ts = Date.now() / 1000;
                }
                
                await triggerSave();
                document.getElementById('contam-setup-modal').classList.add('hidden');
                initDynamicUI();
            }            
        </script>
    </body>
    </html>
    """

def run_api(): uvicorn.run(app, host="127.0.0.1", port=5000, log_level="critical")

class JsApi:
    def save_file_dialog(self, default_filename):
        import webview
        try:
            result = webview.windows[0].create_file_dialog(
                webview.FileDialog.SAVE,
                save_filename=default_filename,
                file_types=('CSV files (*.csv)', 'All files (*.*)')
            )
            if result and len(result) > 0:
                return result[0]
        except Exception as e:
            pass
        return ""

js_api = JsApi()

if __name__ == "__main__":
    threading.Thread(target=modbus_worker, daemon=True).start()
    threading.Thread(target=cleaning_worker, daemon=True).start()
    threading.Thread(target=run_api, daemon=True).start()
    time.sleep(1)
    webview.create_window("WATER ANALYZER PRO", "http://127.0.0.1:5000", fullscreen=True, js_api=js_api)
    webview.start()
