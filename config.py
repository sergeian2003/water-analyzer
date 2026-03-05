import minimalmodbus
import time
import sys

PORT = '/dev/ttyUSB0'

def change_slave_id():
    print("="*50)
    print(" 🛠️ RS485 SENSOR CONFIGURATOR TOOL")
    print("="*50)
    print("WARNING: Only ONE sensor must be connected to the bus!")
    print("Please close the main dashboard before proceeding.\n")
    
    confirm = input("Is the bus isolated? (y/n): ")
    if confirm.lower() != 'y':
        print("Operation cancelled.")
        return

    try:
        current_id = int(input("\nEnter CURRENT ID (e.g., 1): "))
        new_id = int(input("Enter NEW ID (1 to 247): "))
        
        # ID register is usually 0x100 (256) for these water sensors
        id_register = 256 
        
        print(f"\nConnecting to ID {current_id} at 9600 bps...")
        instr = minimalmodbus.Instrument(PORT, current_id)
        instr.serial.baudrate = 9600
        instr.serial.timeout = 1.0
        
        print("Checking connection...")
        instr.read_register(id_register, 0)
        print("✅ Sensor connected successfully!")
        
        print(f"Writing new ID ({new_id})...")
        instr.write_register(id_register, new_id, 0, functioncode=6)
        
        print(f"✅ SUCCESS! The sensor address is now: {new_id}")
        print("Please reboot the sensor (power off/on) to apply changes.")

    except ValueError:
        print("❌ Error: Please enter a valid number.")
    except minimalmodbus.NoResponseError:
        print("❌ Error: Sensor timeout. Check wiring or Current ID.")
    except Exception as e:
        print(f"❌ System Error: {e}")

if __name__ == "__main__":
    change_slave_id()
