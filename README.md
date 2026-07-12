# Smart Water Analyzer

IoT kiosk application built for Raspberry Pi to monitor water quality and control farm environments. The system communicates with multiple Modbus RTU (RS485) sensors, logs data, and provides a touchscreen-friendly Kiosk UI.

## Features

*   **Real-time Modbus Communication**: Supports concurrent reading from multiple RS485 sensors (MLSS, UV254, DO, ORP, Oil, pH, EC, Turbidity) using `minimalmodbus`[cite: 1, 3].
*   **Kiosk Web Interface**: A responsive, dark/light mode UI built with FastAPI, TailwindCSS, and Chart.js, rendered as a native desktop app via `pywebview`[cite: 1, 3].
*   **Automated Data Logging**: Generates 5-minute, 1-hour average, and alarm history CSV logs locally[cite: 1].
*   **Hardware Control**: Manual and automated control of 4-20mA Analog Outputs and Relays for pumps and cleaning mechanisms[cite: 1].
*   **Sensor Configuration Tools**: Includes additional CLI and GUI (`customtkinter`) tools to scan and reassign Modbus Slave IDs on the RS485 bus[cite: 2, 4].

## Hardware Requirements

*   **Host**: Raspberry Pi (Tested on standard Raspbian OS)[cite: 1].
*   **Communication**: USB-to-RS485 converter or SPI-to-UART HAT (`/dev/ttyUSB0`, `/dev/ttySC0`, etc.)[cite: 1].
*   **Sensors**: Any standard industrial Modbus RTU sensors and KM60xx I/O modules[cite: 1, 2].

## Installation

1. Clone the repository:
   \`\`\`bash
   git clone https://github.com/sergeian2003/smart-farm-analyzer.git
   cd smart-farm-analyzer
   \`\`\`

2. Create a virtual environment and install dependencies:
   \`\`\`bash
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   \`\`\`

3. Run the main Kiosk application:
   \`\`\`bash
   python src/main.py
   \`\`\`

## Configuration Tools

If you need to configure new sensors before attaching them to the main bus, use the provided tools (ensure only **ONE** sensor is connected to the bus during this process):

*   **GUI Tool**: Run `python tools/sensor_tool.py` for a visual configurator[cite: 2].
*   **CLI Tool**: Run `python tools/change_id.py` for headless environments[cite: 4].

## License
MIT License
