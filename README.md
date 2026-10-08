Конечно, вот готовый профессиональный README.md на английском языке для твоего репозитория на GitHub. Как ты и просил, я полностью вырезал раздел про создание ярлыка (desktop shortcut) и обновил нумерацию.

Скопируй этот текст в свой файл README.md:

Smart Water Analyzer — Technical Guide & User Manual
A comprehensive system for water quality monitoring and industrial automation based on Raspberry Pi / Linux, Modbus RTU (RS-485), FastAPI, pywebview, and TailwindCSS.

This system is designed for 24/7 real-time data collection from industrial water sensors, auto-calibration, data logging, external actuator control (relays, 4-20mA analog outputs), and automatic sensor cleaning. The latest update introduces 8-channel Analog Inputs (AI 4-20mA) and maximizes RS-485 communication stability on Linux.

Table of Contents
Key System Updates (Latest Release)

Project Structure

Hardware Requirements

Raspberry Pi Deployment & Installation

Password & Security Settings

Running System Components

Kiosk UI Interface Guide

Equipment Connection (RS485 / Modbus)

Sensor ID Setup Utility

Troubleshooting

1. Key System Updates (Latest Release)
This version introduces 5 core technical improvements designed for industrial stability:

Linux RS-485 Driver Optimization (DI Fix): Removed dynamic timeout switching in the io_worker and modbus_worker threads. The system now strictly adheres to a single initialized timeout (0.7s) to prevent buffer flushing issues inherent to Linux usb-serial drivers. The fallback bit-by-bit DI scanning logic that previously bottlenecked the bus has been removed, maximizing communication speed.

AI (Analog Inputs 4-20mA) Integration: Added 8-channel ai_states to the backend. Introduced sequential polling using Modbus Function Code 04 (FC04) in io_worker. Added a dedicated AI monitoring card in the UI's 'Control' tab. Features dynamic rendering: blue text for valid values (mA) and red "Err" text for disconnections or comm faults. Fully supports English/Korean multi-language UI (ai_title).

AUTO DETECT (Module Scanning) Overhaul: Replaced the flawed ignore_id scanning method with strict hardware feature identification. During scanning, the system first tests FC04 (Analog Read); if it responds, the device is classified as an AO module and skipped. It is confirmed as a Relay module only if it responds to FC01 (Read Coil Status). This logic is wrapped in a try...finally block to guarantee timeout restoration for background workers.

UI/UX Toggle Switch Responsiveness: Fixed a visual bug where white toggle dots (checkboxes) overflowed their backgrounds under different screen sizes or font scaling. Replaced fixed pixel margins (e.g., translate-x-[20px]) with responsive TailwindCSS relative classes (e.g., translate-x-full) for perfect scaling on any display.

I/O Module ID Zeroing Bug Fix: Resolved a critical flaw where closing the setup modal would read empty fields and reset IDs to 0 in config.json. Saving relay (relay_id) and analog module (ao_id) addresses is now completely decoupled from the general triggerSave() function. IDs are ONLY saved when explicitly clicking the "APPLY ID" button in the I/O SETUP window.

2. Project Structure
Plaintext
water-analyzer/
├── src/
│   ├── main.py             # Main app server, Modbus logic, API, and Kiosk UI [LATEST]
│   ├── config.json         # System dynamic configuration file (auto-generated)
│   ├── logs/               # Local CSV log files directory (auto-generated)
│   └── static/             # Frontend static files (Tailwind, Chart.js, etc.)
├── tools/
│   ├── sensor_tool.py      # GUI (CustomTkinter) Modbus ID Scanner and Setup Tool
│   └── config.py           # CLI utility for changing Slave IDs on the RS-485 bus
├── requirements.txt        # Python dependencies list
└── README.md               # User manual & technical guide
3. Hardware Requirements
Controller (Host): Raspberry Pi 3B+ / 4B / Compute Module 4, or an Industrial PC running Linux OS (Debian/Ubuntu/Raspberry Pi OS Desktop).

Screen: Touchscreen display with a minimum resolution of 1024x600 px.

Communication Interface: USB to RS485 converter (FTDI / CH340 / CP2102) or SPI to UART / RS485 HAT expansion board.

Supported Sensors & Modules:

MLSS/SS, UV254 (COD/BOD), DO, ORP, Oil-in-Water, pH, EC, Turbidity.

I/O Boards: Relay & 4-20mA Analog In/Out Expansion Modules (e.g., KM6063 / KM6023 / 8AI-4AO).

4. Raspberry Pi Deployment & Installation
Step 1. Install Linux System Dependencies
To run pywebview with hardware acceleration on Linux, WebKit2GTK and Python system components are required. Run the following in the terminal:

Bash
sudo apt update
sudo apt install -y python3-pip python3-venv python3-dev \
                    libgtk-3-dev libwebkit2gtk-4.0-dev \
                    gobject-introspection libgirepository1.0-dev
Step 2. Clone Project & Create Virtual Environment

Bash
cd ~
git clone https://github.com/sergeian2003/water-analyzer.git
cd water-analyzer
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
Step 3. Configure Serial Port Permissions
Add your current user to the dialout group to allow port access without root privileges:

Bash
sudo usermod -a -G dialout $USER
CRITICAL: You must reboot the system (sudo reboot) after running this command for the permissions to take effect.

5. Password & Security Settings
Administrator: Used for engineering setups, HMI calibration, and Modbus ID parameter changes. Default Password: 1234. (Accessed via the 'Setup' tab).

Manufacturer: Used exclusively for resetting the Sensor Contamination counter. Default Password: mfg123.

(The Admin password can be changed via the "CHANGE PWD" button in the Setup tab and is safely saved in config.json.)

6. Running System Components
With the virtual environment activated, you can run the components manually via the terminal:

Bash
cd ~/water-analyzer
source venv/bin/activate

# Run the main Kiosk Interface
python src/main.py

# Run the graphical utility for Sensor ID setup
python tools/sensor_tool.py
7. Kiosk UI Interface Guide
1. Monitoring (Dashboard)
The main screen. Displays real-time sensor values, measurement units, contamination percentages (%), and connection statuses.

2. Trends
Visualizes real-time measurement changes on a graph. View all sensors simultaneously or focus on a specific sensor.

3. Logs / Data
View and filter measurement data (5-minute logs, 1-hour averages) and alarm histories. Use SHOW GRAPH for visual analysis or EXPORT TO FILE to extract CSV data to a USB drive.

4. Control
Monitor and manage external I/O modules:

DI / MANUAL RELAY OVERRIDE: Monitor Digital Input status and manually control physical relay switches.

AI (ANALOG INPUTS): Displays real-time 4-20mA current readings for 8 input channels. Valid readings are shown in blue (mA); comm faults show as a red "Err" for instant troubleshooting.

MANUAL ANALOG OUTPUT: A manual 4-20mA output generator for PLC testing.

5. Setup
Engineering tab for system administrators:

CAL (Calibration): Configure HMI software offsets (A, B) and Modbus hardware zero/span calibration (EEPROM).

I/O SETUP (APPLY ID): Assign communication IDs for Relay and Analog boards. You must click the "APPLY ID" button to save these settings permanently.

AUTO DETECT: Automatically scans for I/O module addresses. Accurately distinguishes between Analog and Relay boards using strict function code validation (FC04/FC01).

8. Equipment Connection (RS485 / Modbus RTU)
The RS485 bus must be wired in a Daisy Chain topology using twisted pair cables. The sensor power supply and communication modules must share a Common Ground (GND) for the Digital Inputs (NPN) and data lines to function correctly without noise.

Plaintext
[Raspberry Pi / USB-RS485]
       |
       +=== A (+) ===================== A (+) Sensor 1 ==== A (+) Sensor 2 ...
       +=== B (-) ===================== B (-) Sensor 1 ==== B (-) Sensor 2 ...
       +=== GND (Shield) ============== GND Sensor 1 ====== GND Sensor 2 ...
9. Sensor ID Setup Utility
WARNING: When changing a Modbus ID on the RS485 bus, ONLY ONE device must be physically connected to the network to prevent address collisions!

Close the main Kiosk application (main.py).

Connect only the single sensor you want to configure, then run python tools/sensor_tool.py.

Click DETECT to scan, or manually enter 255 (Broadcast) in the Current ID field.

Enter the new address in the NEW ID field, click CHANGE ID, and then reboot the sensor (power off/on) to apply changes.

10. Troubleshooting
Q. My device ID resets to 0 upon restarting the software.

This was a bug related to modal closure and is now fully resolved. Ensure you use the I/O SETUP menu in the Setup tab and explicitly click the APPLY ID button to save new addresses.

Q. All sensors show "ERROR" and the system is unresponsive.

If sensors are physically disconnected but left turned ON in the SETUP menu, the system will continuously wait for their response, causing a timeout bottleneck (lag) across the entire bus. Always toggle physically disconnected sensors to OFF in the interface.

Q. The AI block in the Control tab shows red "Err" across all channels.

This indicates a communication failure with the Analog Input module. Go to Setup > I/O SETUP and ensure the ANALOG OUTPUT ID matches the physical dip switch address of your module, then click APPLY ID.

Q. I get a "Permission Denied" error for /dev/ttyUSB0.

Your Linux user lacks port permissions. Open the terminal, run sudo usermod -a -G dialout $USER, and reboot the system.
