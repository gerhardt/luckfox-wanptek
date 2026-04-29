"""
WANPTEK Power Supply Web Application with SCPI Server
====================================================

This application provides:
1. Web interface on port 80 (Flask)
2. SCPI command server on port 5050 (Socket server)
3. Real-time monitoring and control

Usage:
    python wanptek_webapp.py

Web Interface: http://localhost
SCPI Interface: telnet localhost 5050
"""

import json
import socket
import threading
import time
from datetime import datetime
from flask import Flask, render_template, request, jsonify, Response, current_app
from wanptek_controller import WanptekPowerSupply, WanptekMonitor
import re

# Global power supply instance
psu = None
psu_lock = threading.Lock()

# Flask app
app = Flask(__name__)

def get_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0)
    try:
        # doesn't even have to be reachable
        s.connect(('10.254.254.254', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = '127.0.0.1'
    finally:
        s.close()
    return IP

IP_ADDRESS=get_ip()
MAC_ADDRESS=open('/sys/class/net/eth0/address').readline()

class SCPICommandProcessor:
    """SCPI command processor compatible with Rigol DP800 commands"""
    
    def __init__(self, power_supply):
        self.psu = power_supply
        self.output_state = False
        
        # SCPI command mapping
        self.commands = {
            # System commands
            '*IDN?': self.get_identification,
            '*RST': self.reset_device,
            '*TST?': self.self_test,
            'SYSTem:ERRor?': self.get_error,
            'SYSTem:VERSion?': self.get_version,
            
            # Source commands (voltage)
            'SOURce:VOLTage': self.set_voltage,
            'SOURce:VOLTage?': self.get_voltage_setting,
            'SOURce:VOLTage:LEVel:IMMediate:AMPLitude': self.set_voltage,
            'VOLTage': self.set_voltage,
            'VOLTage?': self.get_voltage_setting,
            
            # Source commands (current)
            'SOURce:CURRent': self.set_current,
            'SOURce:CURRent?': self.get_current_setting,
            'SOURce:CURRent:LEVel:IMMediate:AMPLitude': self.set_current,
            'CURRent': self.set_current,
            'CURRent?': self.get_current_setting,
            
            # Measure commands
            'MEASure:VOLTage?': self.measure_voltage,
            'MEASure:CURRent?': self.measure_current,
            'MEASure:POWer?': self.measure_power,
            'MEASure:ALL?': self.measure_all,
            
            # Output commands
            'OUTPut': self.set_output_state,
            'OUTPut?': self.get_output_state,
            'OUTPut:STATe': self.set_output_state,
            'OUTPut:STATe?': self.get_output_state,
            
            # Protection commands
            'SOURce:CURRent:PROTection:STATe': self.set_ocp_state,
            'SOURce:CURRent:PROTection:STATe?': self.get_ocp_state,
            
            # Status commands
            'SOURce:CURRent:PROTection:TRIPped?': self.get_current_protection_tripped,
            'STATus:QUEStionable:CONDition?': self.get_questionable_condition,
            'STATus:OPERation:CONDition?': self.get_operation_condition,
        }
    
    def process_command(self, command_line):
        """Process a single SCPI command line"""
        command_line = command_line.strip()
        
        if not command_line:
            return ""
        
        # Handle multiple commands separated by semicolon
        commands = command_line.split(';')
        responses = []
        
        for cmd in commands:
            cmd = cmd.strip()
            if not cmd:
                continue
                
            response = self._process_single_command(cmd)
            if response is not None:
                responses.append(str(response))
        
        return '\n'.join(responses) if responses else ""
    
    def _process_single_command(self, command):
        """Process a single SCPI command"""
        try:
            # Normalize command (case insensitive, handle abbreviated forms)
            normalized_cmd = self._normalize_command(command)
            
            # Extract command and parameter
            if ' ' in normalized_cmd:
                cmd_name, param = normalized_cmd.split(' ', 1)
            else:
                cmd_name, param = normalized_cmd, None
            
            # Find matching command
            handler = None
            for cmd_pattern, cmd_handler in self.commands.items():
                if self._match_command(cmd_name, cmd_pattern):
                    handler = cmd_handler
                    break
            
            print("handler:")
            print(str(handler))
            if handler:
                if param is not None:
                    return handler(param)
                else:
                    return handler()
            else:
                return "ERROR: Unknown command"
                
        except Exception as e:
            return f"ERROR: {str(e)}"
    
    def _normalize_command(self, command):
        """Normalize SCPI command format"""
        # Convert to uppercase and expand abbreviated forms
        command = command.upper()
        
        # Handle common abbreviations
        abbreviations = {
            'SOUR': 'SOURce',
            'VOLT': 'VOLTage',
            'CURR': 'CURRent',
            'MEAS': 'MEASure',
            'OUTP': 'OUTPut',
            'STAT': 'STATe',
            'PROT': 'PROTection',
            'SYST': 'SYSTem',
            'QUES': 'QUEStionable',
            'OPER': 'OPERation',
            'COND': 'CONDition',
            'IMME': 'IMMediate',
            'AMPL': 'AMPLitude',
            'LEVE': 'LEVel',
            'TRIP': 'TRIPped'
        }
        
        for abbrev, full in abbreviations.items():
            command = command.replace(abbrev, full)
        
        return command
    
    def _match_command(self, input_cmd, pattern_cmd):
        """Match input command against command pattern"""
        # Simple exact match for now
        return input_cmd == pattern_cmd.upper()
    
    # System Commands
    def get_identification(self):
        """*IDN? - Get device identification"""
        info = self.psu.get_device_info()
        return f"WANPTEK,{info['model']},SN123456,V1.0"
    
    def reset_device(self):
        """*RST - Reset device to default state"""
        try:
            self.psu.set_output(voltage=0, current=0, power_on=False, ocp_enable=True)
            return "OK"
        except:
            return "ERROR"
    
    def self_test(self):
        """*TST? - Self test"""
        return "0"  # 0 = passed
    
    def get_error(self):
        """SYST:ERR? - Get system error"""
        return "0,\"No error\""
    
    def get_version(self):
        """SYST:VERS? - Get SCPI version"""
        return "1999.0"
    
    # Source Commands
    def set_voltage(self, value=None):
        """Set output voltage"""
        if value is None:
            return "ERROR: Missing parameter"
        try:
            voltage = float(value)
            self.psu.set_voltage(voltage)
            return "OK"
        except Exception as e:
            return f"ERROR: {str(e)}"
    
    def get_voltage_setting(self):
        """Get voltage setting"""
        try:
            status = self.psu.read_status()
            return f"{status['set_voltage']:.3f}"
        except:
            return "ERROR"
    
    def set_current(self, value=None):
        """Set output current"""
        if value is None:
            return "ERROR: Missing parameter"
        try:
            current = float(value)
            self.psu.set_current(current)
            return "OK"
        except Exception as e:
            return f"ERROR: {str(e)}"
    
    def get_current_setting(self):
        """Get current setting"""
        try:
            status = self.psu.read_status()
            return f"{status['set_current']:.3f}"
        except:
            return "ERROR"
    
    # Measure Commands
    def measure_voltage(self):
        """MEAS:VOLT? - Measure output voltage"""
        try:
            voltage = self.psu.read_voltage()
            return f"{voltage:.3f}"
        except:
            return "ERROR"
    
    def measure_current(self):
        """MEAS:CURR? - Measure output current"""
        try:
            current = self.psu.read_current()
            return f"{current:.3f}"
        except:
            return "ERROR"
    
    def measure_power(self):
        """MEAS:POW? - Measure output power"""
        try:
            power = self.psu.read_power()
            return f"{power:.3f}"
        except:
            return "ERROR"
    
    def measure_all(self):
        """MEAS:ALL? - Measure all parameters"""
        try:
            status = self.psu.read_status()
            return f"{status['real_voltage']:.3f},{status['real_current']:.3f},{status['real_power']:.3f}"
        except:
            return "ERROR"
    
    # Output Commands
    def set_output_state(self, state=None):
        """Set output on/off state"""
        if state is None:
            return "ERROR: Missing parameter"
        try:
            if state.upper() in ['ON', '1']:
                self.psu.power_on()
                self.output_state = True
            elif state.upper() in ['OFF', '0']:
                self.psu.power_off()
                self.output_state = False
            else:
                return "ERROR: Invalid parameter"
            return "OK"
        except Exception as e:
            return f"ERROR: {str(e)}"
    
    def get_output_state(self):
        """Get output state"""
        try:
            is_on = self.psu.is_power_on()
            return "1" if is_on else "0"
        except:
            return "ERROR"
    
    # Protection Commands
    def set_ocp_state(self, state=None):
        """Set over-current protection state"""
        if state is None:
            return "ERROR: Missing parameter"
        try:
            if state.upper() in ['ON', '1']:
                self.psu.enable_ocp()
            elif state.upper() in ['OFF', '0']:
                self.psu.disable_ocp()
            else:
                return "ERROR: Invalid parameter"
            return "OK"
        except Exception as e:
            return f"ERROR: {str(e)}"
    
    def get_ocp_state(self):
        """Get over-current protection state"""
        try:
            status = self.psu.read_status()
            return "1" if status['ocp_enabled'] else "0"
        except:
            return "ERROR"
    
    def get_current_protection_tripped(self):
        """Get current protection trip status"""
        try:
            status = self.psu.read_status()
            return "1" if status['constant_current_mode'] else "0"
        except:
            return "ERROR"
    
    def get_questionable_condition(self):
        """Get questionable condition register"""
        return "0"  # No questionable conditions
    
    def get_operation_condition(self):
        """Get operation condition register"""
        try:
            status = self.psu.read_status()
            condition = 0
            if status['constant_current_mode']:
                condition |= 0x02  # CC mode bit
            return str(condition)
        except:
            return "0"


class SCPIServer:
    """SCPI server that listens on port 5050"""
    
    def __init__(self, power_supply, port=5050):
        self.psu = power_supply
        self.port = port
        self.server_socket = None
        self.running = False
        self.processor = SCPICommandProcessor(power_supply)
    
    def start(self):
        """Start the SCPI server"""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        try:
            self.server_socket.bind(('0.0.0.0', self.port))
            self.server_socket.listen(5)
            self.running = True
            
            print(f"🔌 SCPI server started on port {self.port}")
            
            while self.running:
                try:
                    client_socket, address = self.server_socket.accept()
                    print(f"📡 SCPI client connected from {address}")
                    
                    # Handle client in separate thread
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client_socket, address)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                    
                except Exception as e:
                    if self.running:
                        print(f"❌ SCPI server error: {e}")
                        
        except Exception as e:
            print(f"❌ Failed to start SCPI server: {e}")
        finally:
            if self.server_socket:
                self.server_socket.close()
    
    def _handle_client(self, client_socket, address):
        """Handle individual SCPI client connection"""
        try:
            client_socket.send(b"WANPTEK SCPI Server Ready\n")
            
            buffer = ""
            while self.running:
                try:
                    data = client_socket.recv(1024).decode('utf-8')
                    if not data:
                        break
                    
                    buffer += data
                    
                    # Process complete lines
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        
                        if line.upper() == 'QUIT' or line.upper() == 'EXIT':
                            client_socket.send(b"Goodbye\n")
                            break
                        
                        if line:
                            print(f"📥 SCPI command from {address}: {line}")
                            
                            with psu_lock:
                                response = self.processor.process_command(line)
                            
                            if response:
                                client_socket.send(f"{response}\n".encode('utf-8'))
                                print(f"📤 SCPI response to {address}: {response}")
                
                except socket.timeout:
                    continue
                except Exception as e:
                    print(f"❌ Error handling SCPI client {address}: {e}")
                    break
        
        except Exception as e:
            print(f"❌ SCPI client error {address}: {e}")
        finally:
            try:
                client_socket.close()
                print(f"📡 SCPI client {address} disconnected")
            except:
                pass
    
    def stop(self):
        """Stop the SCPI server"""
        self.running = False
        if self.server_socket:
            self.server_socket.close()


# Flask Web Interface
@app.route('/')
def index():
    """Main web interface"""
    return render_template('index.html', ipa=IP_ADDRESS, mac=MAC_ADDRESS)

@app.route('/css')
def css():
    """CSS interface"""
    return current_app.send_static_file("style.css")

@app.route('/help')
def help():
    """HELP interface"""
    return render_template('help.html', ipa=IP_ADDRESS, mac=MAC_ADDRESS)

@app.route('/api/status')
def get_status():
    """Get power supply status via API"""
    try:
        with psu_lock:
            if psu and psu.connected:
                status = psu.read_status()
                device_info = psu.get_device_info()
                return jsonify({
                    'success': True,
                    'status': status,
                    'device_info': device_info,
                    'timestamp': datetime.now().isoformat()
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Power supply not connected'
                })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/api/set_output', methods=['POST'])
def set_output():
    """Set power supply output parameters"""
    try:
        data = request.get_json()
        
        with psu_lock:
            if not psu or not psu.connected:
                return jsonify({
                    'success': False,
                    'error': 'Power supply not connected'
                })
            
            # Extract parameters
            voltage = data.get('voltage')
            current = data.get('current')
            power_on = data.get('power_on')
            ocp_enable = data.get('ocp_enable')
            
            # Convert string values to appropriate types
            if voltage is not None:
                voltage = float(voltage)
            if current is not None:
                current = float(current)
            if power_on is not None:
                power_on = bool(power_on)
            if ocp_enable is not None:
                ocp_enable = bool(ocp_enable)
            
            # Set output
            success = psu.set_output(
                voltage=voltage,
                current=current,
                power_on=power_on,
                ocp_enable=ocp_enable
            )
            
            if success:
                # Return updated status
                status = psu.read_status()
                return jsonify({
                    'success': True,
                    'status': status
                })
            else:
                return jsonify({
                    'success': False,
                    'error': 'Failed to set output'
                })
                
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/api/power/<action>')
def power_control(action):
    """Power on/off control"""
    try:
        with psu_lock:
            if not psu or not psu.connected:
                return jsonify({
                    'success': False,
                    'error': 'Power supply not connected'
                })
            
            if action == 'on':
                success = psu.power_on()
            elif action == 'off':
                success = psu.power_off()
            else:
                return jsonify({
                    'success': False,
                    'error': 'Invalid action'
                })
            
            return jsonify({
                'success': success,
                'status': psu.read_status() if success else None
            })
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        })

@app.route('/api/stream')
def stream_data():
    """Server-sent events for real-time data streaming"""
    def generate():
        while True:
            try:
                with psu_lock:
                    if psu and psu.connected:
                        status = psu.read_status()
                        data = {
                            'voltage': status['real_voltage'],
                            'current': status['real_current'],
                            'power': status['real_power'],
                            'power_on': status['power_on'],
                            'constant_current': status['constant_current_mode'],
                            'timestamp': datetime.now().isoformat()
                        }
                        yield f"data: {json.dumps(data)}\n\n"
                    else:
                        yield f"data: {json.dumps({'error': 'Not connected'})}\n\n"
                        
                time.sleep(0.5)  # Update every 500ms
                
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                time.sleep(1)
    
    return Response(generate(), mimetype='text/plain')


# Create templates directory and save the HTML template
import os

def initialize_power_supply():
    """Initialize the power supply connection"""
    global psu
    try:
        print("🔍 Initializing WANPTEK power supply...")
        psu = WanptekPowerSupply(port="/dev/ttyS1",debug=False)
        print("✅ Power supply initialized successfully")
        return True
    except Exception as e:
        print(f"❌ Failed to initialize power supply: {e}")
        print("💡 Make sure the device is connected and try again")
        return False

def main():
    """Main application entry point"""
    print("🚀 Starting WANPTEK Web Application with SCPI Server")
    print("=" * 60)
    
    # Initialize power supply
    if not initialize_power_supply():
        print("⚠️  Continuing without power supply connection")
        print("   You can try to reconnect through the web interface")
    
    # Start SCPI server in background thread
    if psu:
        scpi_server = SCPIServer(psu, port=5050)
        scpi_thread = threading.Thread(target=scpi_server.start)
        scpi_thread.daemon = True
        scpi_thread.start()
    else:
        print("⚠️  SCPI server not started (no power supply connection)")
    
    # Start Flask web server
    print("🌐 Starting web server on port 80...")
    print("📡 SCPI server available on port 5050")
    print("\n🔗 Access points:")
    print("   Web Interface: http://localhost")
    print("   SCPI Interface: telnet localhost 5050")
    print("\n💡 SCPI Commands (Rigol DP800 compatible):")
    print("   *IDN?                    - Get device identification")
    print("   VOLT 5.0                 - Set voltage to 5V")
    print("   VOLT?                    - Read voltage setting")
    print("   CURR 1.0                 - Set current to 1A") 
    print("   CURR?                    - Read current setting")
    print("   MEAS:VOLT?               - Measure actual voltage")
    print("   MEAS:CURR?               - Measure actual current")
    print("   MEAS:POW?                - Measure actual power")
    print("   OUTP ON                  - Turn output on")
    print("   OUTP OFF                 - Turn output off")
    print("   OUTP?                    - Check output state")
    print("\n🔧 Press Ctrl+C to stop the server")
    
    try:
        # Run Flask app
        app.run(host='0.0.0.0', port=80, debug=False, threaded=True)
    except KeyboardInterrupt:
        print("\n⏹️  Shutting down...")
        if psu:
            psu.close()
        print("✅ Shutdown complete")
    except Exception as e:
        print(f"❌ Server error: {e}")
        if psu:
            psu.close()

if __name__ == "__main__":
    main()
