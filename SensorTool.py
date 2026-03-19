import customtkinter as ctk
import minimalmodbus
import serial.tools.list_ports
import threading
import time

# Настройка темы
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class SensorTool(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Smart Farm - ID Configurator")
        self.geometry("450x600")
        self.resizable(False, False)

        # Заголовок
        self.title_label = ctk.CTkLabel(self, text="Modbus Device Configurator", font=ctk.CTkFont(size=20, weight="bold"))
        self.title_label.pack(pady=(20, 15))

        # Фрейм настроек
        self.frame = ctk.CTkFrame(self)
        self.frame.pack(padx=20, pady=10, fill="both", expand=True)

        # 1. COM Port
        self.port_label = ctk.CTkLabel(self.frame, text="1. Select COM Port:", font=ctk.CTkFont(weight="bold"))
        self.port_label.pack(anchor="w", padx=20, pady=(15, 0))
        
        self.port_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.port_frame.pack(fill="x", padx=20, pady=5)
        
        self.port_combo = ctk.CTkComboBox(self.port_frame, values=self.get_ports(), width=200)
        self.port_combo.pack(side="left")
        
        self.refresh_btn = ctk.CTkButton(self.port_frame, text="Refresh", width=80, command=self.refresh_ports)
        self.refresh_btn.pack(side="right", padx=(10, 0))

        # 2. Тип устройства
        self.type_label = ctk.CTkLabel(self.frame, text="2. Device Type:", font=ctk.CTkFont(weight="bold"))
        self.type_label.pack(anchor="w", padx=20, pady=(15, 0))
        
        self.type_combo = ctk.CTkComboBox(self.frame, values=[
            "1. Standard Sensors (Oil, pH, UV254, DO, etc.)", 
            "2. MLSS Sensor Only", 
            "3. I/O Module (Relay/AO)"
        ])
        self.type_combo.pack(fill="x", padx=20, pady=5)

        # 3. ID (Current & New)
        self.id_frame = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.id_frame.pack(fill="x", padx=20, pady=(15, 5))

        self.curr_id_label = ctk.CTkLabel(self.id_frame, text="Current ID:", font=ctk.CTkFont(size=11))
        self.curr_id_label.pack(side="left")
        self.curr_id_entry = ctk.CTkEntry(self.id_frame, width=50, justify="center")
        self.curr_id_entry.insert(0, "255")
        self.curr_id_entry.pack(side="left", padx=(10, 5))

        self.detect_btn = ctk.CTkButton(self.id_frame, text="DETECT", width=60, fg_color="#10b981", hover_color="#059669", font=ctk.CTkFont(size=11, weight="bold"), command=self.start_detect_id)
        self.detect_btn.pack(side="left", padx=(0, 10))

        self.new_id_label = ctk.CTkLabel(self.id_frame, text="NEW ID:", font=ctk.CTkFont(size=11, weight="bold"), text_color="#22d3ee")
        self.new_id_label.pack(side="left", padx=(10, 0))
        self.new_id_entry = ctk.CTkEntry(self.id_frame, width=60, justify="center", font=ctk.CTkFont(weight="bold"))
        self.new_id_entry.pack(side="left", padx=10)

        # 4. Большая кнопка
        self.change_btn = ctk.CTkButton(self.frame, text="CHANGE ID", font=ctk.CTkFont(weight="bold", size=14), height=40, command=self.start_change_id)
        self.change_btn.pack(fill="x", padx=20, pady=25)

        # 5. Консоль
        self.log_box = ctk.CTkTextbox(self, height=120, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.pack(padx=20, pady=(0, 20), fill="both", expand=True)
        self.log(f"[*] System ready.\n[*] Insert USB-RS485 and select port.")

    def get_ports(self):
        ports = [port.device for port in serial.tools.list_ports.comports()]
        return ports if ports else ["No Ports Found"]

    def refresh_ports(self):
        self.port_combo.configure(values=self.get_ports())
        self.port_combo.set(self.get_ports()[0])
        self.log("[*] Ports refreshed.")

    def log(self, message):
        self.log_box.insert("end", message + "\n")
        self.log_box.see("end")

    def start_change_id(self):
        # Запускаем в отдельном потоке, чтобы не заморозить UI
        self.change_btn.configure(state="disabled", text="PROCESSING...")
        threading.Thread(target=self.change_id_logic, daemon=True).start()

    def change_id_logic(self):
        port = self.port_combo.get()
        s_type = self.type_combo.get()
        
        try:
            curr_id = int(self.curr_id_entry.get())
            new_id = int(self.new_id_entry.get())
        except ValueError:
            self.log("[!] ERROR: Please enter valid numbers for IDs.")
            self.change_btn.configure(state="normal", text="CHANGE ID")
            return

        if port == "No Ports Found" or port == "":
            self.log("[!] ERROR: Invalid COM port.")
            self.change_btn.configure(state="normal", text="CHANGE ID")
            return

        self.log(f"\n[-] Connecting to {port} (Target ID: {curr_id})...")
        
        try:
            instr = minimalmodbus.Instrument(port, curr_id)
            instr.serial.baudrate = 9600
            instr.serial.bytesize = 8
            instr.serial.parity = minimalmodbus.serial.PARITY_NONE
            instr.serial.stopbits = 2
            instr.serial.timeout = 1.0  # Даем чуть больше времени
            instr.clear_buffers_before_each_transaction = True

            # Логика I/O Модуля
            if "I/O Module" in s_type:
                try:
                    current_reg = instr.read_register(0, 0)
                    baud_rate_code = current_reg & 0x00FF  
                    new_val = (new_id << 8) | baud_rate_code 
                    instr.write_register(0, new_val, 0, functioncode=6)
                except Exception:
                    fallback_val = (new_id << 8) | 3
                    instr.write_register(0, fallback_val, 0, functioncode=6)
                
            else:
                id_register = 25 if "MLSS" in s_type else 12288
                
                try:
                    current_reg = instr.read_register(id_register, 0)
                except Exception:
                    if curr_id != 255:
                        self.log("[!] Timeout. Trying broadcast ID 255...")
                        instr.address = 255
                        current_reg = instr.read_register(id_register, 0)
                    else:
                        raise Exception("Sensor is not responding.")
                
                if "MLSS" in s_type:
                    write_val = new_id
                else:
                    if current_reg >= 256: 
                        baud_rate = current_reg & 0x00FF
                        write_val = (new_id << 8) | baud_rate
                    else:
                        write_val = new_id

                instr.write_register(id_register, write_val, 0, functioncode=6)

            self.log(f"[+] SUCCESS! ID changed to {new_id}.")
            self.log("[!] PLEASE REBOOT SENSOR (Power OFF/ON).")

        except Exception as e:
            self.log(f"[!] FAILED: {str(e)}")
            self.log("[!] Check wiring, power, and try Broadcast ID 255.")

        finally:
            self.change_btn.configure(state="normal", text="CHANGE ID")

    def start_detect_id(self):
        # Запускаем в отдельном потоке
        self.detect_btn.configure(state="disabled", text="...")
        threading.Thread(target=self.detect_id_logic, daemon=True).start()

    def detect_id_logic(self):
        port = self.port_combo.get()
        s_type = self.type_combo.get()

        if port == "No Ports Found" or port == "":
            self.log("[!] ERROR: Invalid COM port.")
            self.detect_btn.configure(state="normal", text="DETECT")
            return

        self.log(f"\n[*] Scanning bus on {port} (IDs 1-247)...")
        
        try:
            # Настраиваем сканер
            instr = minimalmodbus.Instrument(port, 1)
            instr.serial.baudrate = 9600
            instr.serial.timeout = 0.05  # МИКРО-ТАЙМАУТ для быстрого перебора!
            instr.clear_buffers_before_each_transaction = True

            # Выбираем, какой регистр "дергать" для проверки связи
            reg_to_read = 12288
            if "MLSS" in s_type: reg_to_read = 25
            elif "I/O" in s_type: reg_to_read = 0

            found_id = None
            for target_id in range(1, 248):
                if target_id % 50 == 0:
                    self.log(f"    Scanning {target_id}/247...") # Показываем прогресс
                
                instr.address = target_id
                try:
                    instr.read_register(reg_to_read, 0)
                    found_id = target_id
                    break # Успех! Получили ответ
                except minimalmodbus.NoResponseError:
                    continue # Нет ответа, идем дальше
                except Exception:
                    # Если датчик ответил ошибкой Modbus (например, регистр закрыт), 
                    # это всё равно значит, что датчик ТАМ ЕСТЬ и он живой!
                    found_id = target_id
                    break

            if found_id:
                self.log(f"[+] FOUND SENSOR AT ID: {found_id}!")
                # Автоматически вписываем найденный ID в поле
                self.curr_id_entry.delete(0, "end")
                self.curr_id_entry.insert(0, str(found_id))
            else:
                self.log("[-] No sensor found. Check wiring or power.")

        except Exception as e:
            self.log(f"[!] Scan error: {str(e)}")
        finally:
            self.detect_btn.configure(state="normal", text="DETECT")

if __name__ == "__main__":
    app = SensorTool()
    app.mainloop()
