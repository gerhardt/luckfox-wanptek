import serial
import struct
import time
import glob
import logging
import os
from datetime import datetime
from typing import Tuple, Optional, Dict, List
from enum import IntEnum

class VoltageRange(IntEnum):
    """Voltage range constants"""
    V15 = 0
    V30 = 1
    V60 = 2
    V100 = 3
    V120 = 4
    V150 = 5
    V160 = 6
    V200 = 7
    V300 = 8

class CurrentRange(IntEnum):
    """Current range constants"""
    A1 = 0
    A2 = 1
    A3 = 2
    A5 = 3
    A6 = 4
    A10 = 5
    A20 = 6
    A30 = 7
    A40 = 8
    A50 = 9
    A60 = 10
    A80 = 11
    A100 = 12
    A200 = 13

class DebugLogger:
    """Enhanced debug logger for serial communication"""
    
    def __init__(self, enabled: bool = False, log_file: Optional[str] = None):
        self.enabled = enabled
        self.logger = None
        self.log_file = log_file or f"/tmp/wanptek_debug_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        
        if self.enabled:
            self._setup_logger()
    
    def _setup_logger(self):
        """Setup the debug logger"""
        self.logger = logging.getLogger('WanptekDebug')
        self.logger.setLevel(logging.DEBUG)
        
        # Remove existing handlers
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)
        
        # File handler
        file_handler = logging.FileHandler(self.log_file, mode='w')
        file_handler.setLevel(logging.DEBUG)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # Formatter
        formatter = logging.Formatter(
            '%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s',
            datefmt='%H:%M:%S'
        )
        file_handler.setFormatter(formatter)
        console_handler.setFormatter(formatter)
        
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        
        self.logger.info(f"=== WANPTEK Debug Session Started ===")
        self.logger.info(f"Log file: {self.log_file}")
    
    def enable(self, log_file: Optional[str] = None):
        """Enable debug logging"""
        if log_file:
            self.log_file = log_file
        self.enabled = True
        if not self.logger:
            self._setup_logger()
        self.info("Debug logging enabled")
    
    def disable(self):
        """Disable debug logging"""
        if self.enabled:
            self.info("Debug logging disabled")
        self.enabled = False
    
    def _log(self, level: str, message: str):
        """Internal logging method"""
        if self.enabled and self.logger:
            getattr(self.logger, level.lower())(message)
    
    def debug(self, message: str):
        self._log('DEBUG', message)
    
    def info(self, message: str):
        self._log('INFO', message)
    
    def warning(self, message: str):
        self._log('WARNING', message)
    
    def error(self, message: str):
        self._log('ERROR', message)
    
    def hex_dump(self, data: bytes, prefix: str = ""):
        """Create detailed hex dump of data"""
        if not self.enabled:
            return
            
        if not data:
            self.debug(f"{prefix}[EMPTY]")
            return
        
        # Create hex string with spaces
        hex_str = ' '.join(f'{b:02X}' for b in data)
        
        # Create ASCII representation
        ascii_str = ''.join(chr(b) if 32 <= b <= 126 else '.' for b in data)
        
        # Log basic info
        self.debug(f"{prefix}Length: {len(data)} bytes")
        self.debug(f"{prefix}Hex:    {hex_str}")
        self.debug(f"{prefix}ASCII:  {ascii_str}")
        
        # Create detailed hex dump (16 bytes per line)
        hex_dump_lines = []
        for i in range(0, len(data), 16):
            chunk = data[i:i+16]
            hex_part = ' '.join(f'{b:02X}' for b in chunk)
            ascii_part = ''.join(chr(b) if 32 <= b <= 126 else '.' for b in chunk)
            hex_dump_lines.append(f"{prefix}{i:04X}: {hex_part:<47} |{ascii_part}|")
        
        for line in hex_dump_lines:
            self.debug(line)
    
    def log_command(self, description: str, command: bytes, response: bytes = None, 
                   error: str = None, duration: float = None):
        """Log a complete command transaction"""
        if not self.enabled:
            return
        
        self.info(f"--- {description} ---")
        
        if duration is not None:
            self.debug(f"Duration: {duration:.3f}s")
        
        self.debug("COMMAND SENT:")
        self.hex_dump(command, "  ")
        
        if response is not None:
            self.debug("RESPONSE RECEIVED:")
            self.hex_dump(response, "  ")
            
            # Parse response structure
            if len(response) >= 3:
                addr = response[0]
                func = response[1]
                self.debug(f"  Response - Address: 0x{addr:02X}, Function: 0x{func:02X}")
                
                if func == 0x03:  # Read response
                    if len(response) >= 3:
                        byte_count = response[2]
                        self.debug(f"  Data bytes: {byte_count}")
                elif func == 0x10:  # Write response
                    if len(response) >= 6:
                        start_addr = struct.unpack('>H', response[2:4])[0]
                        num_regs = struct.unpack('>H', response[4:6])[0]
                        self.debug(f"  Write confirmed - Start: 0x{start_addr:04X}, Registers: {num_regs}")
        
        if error:
            self.error(f"ERROR: {error}")
        
        self.debug("")  # Empty line for readability

class WanptekPowerSupply:
    """
    Universal Python controller for WANPTEK DC Power Supply with enhanced debugging.
    """
    
    # Voltage range mapping (series_code -> max_voltage)
    VOLTAGE_RANGES = {
        0: 15, 1: 30, 2: 60, 3: 100, 4: 120, 
        5: 150, 6: 160, 7: 200, 8: 300
    }
    
    # Current range mapping (series_code -> max_current)  
    CURRENT_RANGES = {
        0: 1, 1: 2, 2: 3, 3: 5, 4: 6, 5: 10, 6: 20,
        7: 30, 8: 40, 9: 50, 10: 60, 11: 80, 12: 100, 13: 200
    }
    
    # Standard baudrates supported by WANPTEK devices
    SUPPORTED_BAUDRATES = [2400, 4800, 9600, 19200]
    
    def __init__(self, port: Optional[str] = None, slave_addr: int = 0, 
                 baudrate: Optional[int] = None, timeout: float = 1.0, 
                 auto_detect: bool = True, debug: bool = False, 
                 debug_log_file: Optional[str] = None):
        """
        Initialize the power supply controller with debug capabilities.
        
        Args:
            port: Serial port (e.g., '/dev/ttyUSB0'). If None, auto-detect.
            slave_addr: Device address (0-31)
            baudrate: Communication speed. If None, auto-detect.
            timeout: Serial communication timeout in seconds
            auto_detect: Try to auto-detect port and baudrate
            debug: Enable debug logging
            debug_log_file: Custom debug log file path
        """
        self.slave_addr = slave_addr
        self.timeout = timeout
        self.serial = None
        
        # Initialize debug logger
        self.debug_logger = DebugLogger(debug, debug_log_file)
        
        # Device specifications (detected from device)
        self.voltage_decimal_places = 2
        self.current_decimal_places = 2
        self.voltage_series = 0
        self.current_series = 0
        self.max_voltage = 0
        self.max_current = 0
        self.nominal_voltage = 0
        self.nominal_current = 0
        self.little_endian = True
        self.device_model = "Unknown"
        
        # Connection status
        self.connected = False
        self.last_status = {}
        self._status_cache_time = 0.0   # epoch of last read_status()
        self.STATUS_CACHE_TTL = 0.15    # 150 ms cache
        
        self.debug_logger.info(f"Initializing WANPTEK controller (slave_addr={slave_addr}, timeout={timeout}s)")
        
        if auto_detect:
            self._auto_connect(port, baudrate)
        else:
            if port is None:
                raise ValueError("Port must be specified when auto_detect=False")
            if baudrate is None:
                baudrate = 9600
            self._connect(port, baudrate)
    
    def enable_debug(self, log_file: Optional[str] = None):
        """Enable debug logging"""
        self.debug_logger.enable(log_file)
        print(f"Debug logging enabled. Log file: {self.debug_logger.log_file}")
    
    def disable_debug(self):
        """Disable debug logging"""
        self.debug_logger.disable()
        print("Debug logging disabled")
    
    @staticmethod
    def find_devices() -> List[str]:
        """Find all potential WANPTEK devices on Linux"""
        devices = []
        # Check common USB serial device paths
        for pattern in ['/dev/ttyUSB*', '/dev/ttyACM*', '/dev/serial/by-id/*']:
            devices.extend(glob.glob(pattern))
        return sorted(devices)
    
    def _auto_connect(self, preferred_port: Optional[str] = None, 
                     preferred_baudrate: Optional[int] = None):
        """Auto-detect and connect to WANPTEK device"""
        self.debug_logger.info("Starting auto-detection of WANPTEK power supply...")
        print("🔍 Auto-detecting WANPTEK power supply...")
        
        # Get list of potential devices
        if preferred_port:
            ports_to_try = [preferred_port]
        else:
            ports_to_try = self.find_devices()
            if not ports_to_try:
                ports_to_try = ['/dev/ttyUSB0']  # Fallback
        
        self.debug_logger.debug(f"Ports to try: {ports_to_try}")
        
        # Get list of baudrates to try
        if preferred_baudrate:
            baudrates_to_try = [preferred_baudrate]
        else:
            baudrates_to_try = [9600, 4800, 19200, 2400]  # Most common first
        
        self.debug_logger.debug(f"Baudrates to try: {baudrates_to_try}")
        
        # Try each combination
        for port in ports_to_try:
            self.debug_logger.info(f"Trying port: {port}")
            print(f"  📡 Trying port: {port}")
            for baudrate in baudrates_to_try:
                self.debug_logger.debug(f"  Trying baudrate: {baudrate}")
                try:
                    if self._connect(port, baudrate, silent=True):
                        self.debug_logger.info(f"Successfully connected to {self.device_model} at {port} ({baudrate} baud)")
                        print(f"  ✅ Connected to {self.device_model} at {port} ({baudrate} baud)")
                        return
                except Exception as e:
                    self.debug_logger.debug(f"  Connection failed: {e}")
                    continue
        
        error_msg = "Could not auto-detect WANPTEK device. Please specify port and baudrate manually."
        self.debug_logger.error(error_msg)
        raise Exception(f"❌ {error_msg}")
    
    def _connect(self, port: str, baudrate: int, silent: bool = False) -> bool:
        """Connect to device and verify communication"""
        try:
            self.debug_logger.info(f"Attempting connection to {port} at {baudrate} baud")
            
            if self.serial and self.serial.is_open:
                self.debug_logger.debug("Closing existing serial connection")
                self.serial.close()
            
            self.serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=8,
                parity=serial.PARITY_NONE,
                stopbits=1,
                timeout=self.timeout,
                inter_byte_timeout=0.05  # unblock read() 50ms after last byte
            )
            
            self.debug_logger.debug(f"Serial port opened: {self.serial}")
            
            # Test communication by reading status
            self._detect_device_specs()
            self.connected = True
            
            self.debug_logger.info(f"Successfully connected to {self.device_model}")
            
            if not silent:
                print(f"✅ Connected to {self.device_model}")
                self._print_device_info()
            
            return True
            
        except Exception as e:
            self.debug_logger.error(f"Connection failed: {e}")
            if not silent:
                print(f"❌ Connection failed: {e}")
            if self.serial and self.serial.is_open:
                self.serial.close()
            return False
    
    def _detect_device_specs(self):
        """Detect device specifications from status response"""
        self.debug_logger.info("Detecting device specifications...")
        status = self._read_raw_status()
        
        # Extract device specifications
        data = status[3:19]
        self.debug_logger.debug("Parsing device configuration from status response")
        
        # Parse configuration bytes
        voltage_info = data[1]
        current_info = data[2]
        
        self.voltage_decimal_places = 1 if (voltage_info >> 4) & 0x0F else 2
        self.current_decimal_places = 2 if (current_info >> 4) & 0x0F else 3
        
        self.voltage_series = voltage_info & 0x0F
        self.current_series = current_info & 0x0F
        
        self.debug_logger.debug(f"Voltage info byte: 0x{voltage_info:02X} -> series={self.voltage_series}, decimals={self.voltage_decimal_places}")
        self.debug_logger.debug(f"Current info byte: 0x{current_info:02X} -> series={self.current_series}, decimals={self.current_decimal_places}")
        
        # Determine endianness
        status_byte = data[0]
        self.little_endian = not bool(status_byte & 0x08)
        self.debug_logger.debug(f"Status byte: 0x{status_byte:02X} -> endianness={'little' if self.little_endian else 'big'}")
        
        # Get nominal and max values
        self.nominal_voltage = self.VOLTAGE_RANGES.get(self.voltage_series, 0)
        self.nominal_current = self.CURRENT_RANGES.get(self.current_series, 0)
        
        # Parse actual max values from device
        max_voltage_raw = self._unpack_word(data[12:14])
        max_current_raw = self._unpack_word(data[14:16])
        
        voltage_divisor = 10 ** self.voltage_decimal_places
        current_divisor = 10**self.current_decimal_places
        
        self.max_voltage = max_voltage_raw / voltage_divisor
        self.max_current = max_current_raw / current_divisor
        
        self.debug_logger.debug(f"Max voltage raw: {max_voltage_raw} -> {self.max_voltage}V")
        self.debug_logger.debug(f"Max current raw: {max_current_raw} -> {self.max_current}A")
        
        # Determine device model
        self.device_model = f"WANPTEK {self.nominal_voltage}V/{self.nominal_current}A"
        self.debug_logger.info(f"Device detected: {self.device_model}")
    
    def _print_device_info(self):
        """Print detected device information"""
        print(f"📋 Device Information:")
        print(f"   Model: {self.device_model}")
        print(f"   Nominal: {self.nominal_voltage}V / {self.nominal_current}A")
        print(f"   Max Output: {self.max_voltage}V / {self.max_current}A")
        print(f"   Precision: {self.voltage_decimal_places} decimal places (V), {self.current_decimal_places} decimal places (A)")
        print(f"   Endianness: {'Little' if self.little_endian else 'Big'}")
        print(f"   Address: {self.slave_addr}")
    
    def _calculate_crc(self, data: bytes) -> int:
        """Calculate CRC16 with polynomial 0x8005 (MODBUS standard)"""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        
        self.debug_logger.debug(f"CRC calculated for {len(data)} bytes: 0x{crc:04X}")
        return crc
    
    def _pack_word(self, value: int) -> bytes:
        """Pack 16-bit word according to detected endianness"""
        if self.little_endian:
            result = struct.pack('<H', value)
        else:
            result = struct.pack('>H', value)
        
        self.debug_logger.debug(f"Packed word {value} ({'little' if self.little_endian else 'big'} endian): {result.hex().upper()}")
        return result
    
    def _unpack_word(self, data: bytes) -> int:
        """Unpack 16-bit word according to detected endianness"""
        if self.little_endian:
            result = struct.unpack('<H', data)[0]
        else:
            result = struct.unpack('>H', data)[0]
        
        self.debug_logger.debug(f"Unpacked word from {data.hex().upper()} ({'little' if self.little_endian else 'big'} endian): {result}")
        return result
    
    def _send_command(self, command: bytes, description: str = "Command") -> bytes:
        """Send command and receive response with enhanced error handling and logging"""
        # if not self.connected or not self.serial or not self.serial.is_open:
        #    error_msg = "Device not connected"
        #    self.debug_logger.error(error_msg)
        #    raise Exception(error_msg)
        
        start_time = time.time()
        
        # Add CRC
        crc = self._calculate_crc(command)
        full_command = command + struct.pack('<H', crc)
        
        # Clear input buffer
        self.debug_logger.debug("Clearing input buffer")
        self.serial.reset_input_buffer()
        
        # Send command
        self.debug_logger.debug(f"Sending command to serial port")
        self.debug_logger.debug(full_command)
        self.serial.write(full_command)
        self.serial.flush()
        
        # Read response with timeout handling
        response = b''
        read_start = time.time()
        bytes_expected = None
        
        while (time.time() - read_start) < self.timeout:
            chunk = self.serial.read(256)
            if chunk:
                response += chunk
                self.debug_logger.debug(f"Received {len(chunk)} bytes: {chunk.hex().upper()}")
                if len(response) >= 3 and bytes_expected is None:
                    func_code = response[1]
                    if func_code == 0x03:
                        bytes_expected = 3 + response[2] + 2
                    elif func_code == 0x10:
                        bytes_expected = 8
                if bytes_expected and len(response) >= bytes_expected:
                    break
            elif bytes_expected and len(response) >= bytes_expected:
                break
            # No sleep: inter_byte_timeout handles blocking
        
        duration = time.time() - start_time
        
        if len(response) < 3:
            error_msg = f"Timeout: received only {len(response)} bytes in {duration:.3f}s"
            self.debug_logger.log_command(description, full_command, response, error_msg, duration)
            raise Exception(error_msg)
        
        # Verify CRC
        data_part = response[:-2]
        received_crc = struct.unpack('<H', response[-2:])[0]
        calculated_crc = self._calculate_crc(data_part)
        
        if received_crc != calculated_crc:
            error_msg = f"CRC verification failed: expected 0x{calculated_crc:04X}, got 0x{received_crc:04X}"
            self.debug_logger.log_command(description, full_command, response, error_msg, duration)
            raise Exception(error_msg)
        
        # Log successful transaction
        self.debug_logger.log_command(description, full_command, response, None, duration)
        
        return response
    
    def _read_raw_status(self) -> bytes:
        """Read raw status response from device"""
        command = struct.pack('BBHH', self.slave_addr, 0x03, 0x0000, 0x0800)
        response = self._send_command(command, "Read Status")
        
        if len(response) < 21:
            error_msg = f"Invalid status response length: expected 21, got {len(response)}"
            self.debug_logger.error(error_msg)
            raise Exception(error_msg)

        
        return response
    
    def read_status(self) -> Dict:
        """
        Read complete status information from the power supply.
        
        Returns:
            dict: Complete status with all measurements and flags
        """
        self.debug_logger.info("Reading device status")
        response = self._read_raw_status()
        data = response[3:19]
        
        # Parse status flags (byte 0)
        status_byte = data[0]
        power_on = bool(status_byte & 0x01)
        ocp_enabled = bool(status_byte & 0x02)
        keyboard_locked = bool(status_byte & 0x04)
        is_big_endian = bool(status_byte & 0x08)
        constant_current = bool(status_byte & 0x10)
        alarm_active = bool(status_byte & 0x20)
        
        self.debug_logger.debug(f"Status byte 0x{status_byte:02X}: power={power_on}, OCP={ocp_enabled}, lock={keyboard_locked}, CC={constant_current}, alarm={alarm_active}")
        
        # Parse measurement values
        real_voltage_raw = self._unpack_word(data[4:6])
        real_current_raw = self._unpack_word(data[6:8])
        set_voltage_raw = self._unpack_word(data[8:10])
        set_current_raw = self._unpack_word(data[10:12])
        
        # Convert to actual values
        voltage_divisor = 10 ** self.voltage_decimal_places
        current_divisor = 10 ** self.current_decimal_places
        
        status_dict = {
            # Control flags
            'power_on': power_on,
            'ocp_enabled': ocp_enabled,
            'keyboard_locked': keyboard_locked,
            'constant_current_mode': constant_current,
            'alarm_active': alarm_active,
            
            # Measurements
            'real_voltage': real_voltage_raw / voltage_divisor,
            'real_current': real_current_raw / current_divisor,
            'set_voltage': set_voltage_raw / voltage_divisor,
            'set_current': set_current_raw / current_divisor,
            
            # Device specs
            'max_voltage': self.max_voltage,
            'max_current': self.max_current,
            'nominal_voltage': self.nominal_voltage,
            'nominal_current': self.nominal_current,
            'voltage_series': self.voltage_series,
            'current_series': self.current_series,
            'model': self.device_model,
            
            # Power calculation
            'real_power': (real_voltage_raw / voltage_divisor) * (real_current_raw / current_divisor),
            'set_power': (set_voltage_raw / voltage_divisor) * (set_current_raw / current_divisor)
        }
        
        self.debug_logger.debug(f"Parsed status: V={status_dict['real_voltage']:.2f}V, I={status_dict['real_current']:.3f}A, P={status_dict['real_power']:.2f}W")
        
        self.last_status = status_dict
        self._status_cache_time = time.time()
        return status_dict

    def read_status_cached(self) -> Dict:
        """Return cached status if fresh, else read from device."""
        if self.last_status and (time.time() - self._status_cache_time) < self.STATUS_CACHE_TTL:
            return self.last_status
        return self.read_status()
    
    def set_output(self, voltage: Optional[float] = None, current: Optional[float] = None, 
                   power_on: Optional[bool] = None, ocp_enable: Optional[bool] = None, 
                   keyboard_lock: Optional[bool] = None) -> bool:
        """
        Universal output control method with enhanced logging.
        """
        self.debug_logger.info(f"Setting output: V={voltage}, I={current}, power={power_on}, OCP={ocp_enable}, lock={keyboard_lock}")
        
        # Use cache to avoid a round-trip when only one field changes
        current_status = self.read_status_cached()
        
        # Use provided values or fall back to current settings
        target_voltage = voltage if voltage is not None else current_status['set_voltage']
        target_current = current if current is not None else current_status['set_current']
        target_power = power_on if power_on is not None else current_status['power_on']
        target_ocp = ocp_enable if ocp_enable is not None else current_status['ocp_enabled']
        target_lock = keyboard_lock if keyboard_lock is not None else current_status['keyboard_locked']
        
        self.debug_logger.debug(f"Final target values: V={target_voltage}, I={target_current}, power={target_power}, OCP={target_ocp}, lock={target_lock}")
        
        # Validate ranges
        if target_voltage > self.max_voltage:
            error_msg = f"Voltage {target_voltage}V exceeds maximum {self.max_voltage}V"
            self.debug_logger.error(error_msg)
            raise ValueError(error_msg)
        if target_current > self.max_current:
            error_msg = f"Current {target_current}A exceeds maximum {self.max_current}A"
            self.debug_logger.error(error_msg)
            raise ValueError(error_msg)
        if target_voltage < 0 or target_current < 0:
            error_msg = "Voltage and current must be non-negative"
            self.debug_logger.error(error_msg)
            raise ValueError(error_msg)
        
        # Convert to raw values
        voltage_divisor = 10 ** self.voltage_decimal_places
        current_divisor = 10 ** self.current_decimal_places
        
        voltage_raw = int(target_voltage * voltage_divisor)
        current_raw = int(target_current * current_divisor)
        
        self.debug_logger.debug(f"Raw values: voltage={voltage_raw}, current={current_raw}")
        
        # Build control byte
        control_byte = 0
        if target_power:
            control_byte |= 0x01
        if target_ocp:
            control_byte |= 0x02
        if target_lock:
            control_byte |= 0x04
        
        self.debug_logger.debug(f"Control byte: 0x{control_byte:02X}")
        
        # Build write command
        command = struct.pack('BBHHB', self.slave_addr, 0x10, 0x0000, 0x0300, 0x06)
        
        # Add data: control_byte + reserve + voltage + current
        data = struct.pack('BB', control_byte, 0x00)  # Control + Reserve
        data += self._pack_word(voltage_raw)  # Set voltage
        data += self._pack_word(current_raw)  # Set current
        
        full_command = command + data
        
        try:
            response = self._send_command(full_command, "Set Output")
            success = len(response) >= 8
            self.debug_logger.info(f"Set output {'successful' if success else 'failed'}")
            return success
        except Exception as e:
            self.debug_logger.error(f"Set output failed: {e}")
            print(f"❌ Set output failed: {e}")
            return False
    
    # Convenience methods for common operations
    def set_voltage(self, voltage: float) -> bool:
        """Set output voltage while keeping other settings unchanged"""
        return self.set_output(voltage=voltage)
    
    def set_current(self, current: float) -> bool:
        """Set output current while keeping other settings unchanged"""
        return self.set_output(current=current)
    
    def power_on(self) -> bool:
        """Turn on the power output"""
        return self.set_output(power_on=True)
    
    def power_off(self) -> bool:
        """Turn off the power output"""
        return self.set_output(power_on=False)
    
    def enable_ocp(self) -> bool:
        """Enable over-current protection"""
        return self.set_output(ocp_enable=True)
    
    def disable_ocp(self) -> bool:
        """Disable over-current protection"""
        return self.set_output(ocp_enable=False)
    
    def lock_keyboard(self) -> bool:
        """Lock device keyboard (PC control only)"""
        return self.set_output(keyboard_lock=True)
    
    def unlock_keyboard(self) -> bool:
        """Unlock device keyboard"""
        return self.set_output(keyboard_lock=False)
    
    # Quick read methods
    def read_voltage(self) -> float:
        """Read actual output voltage"""
        return self.read_status_cached()['real_voltage']
    
    def read_current(self) -> float:  
        """Read actual output current"""
        return self.read_status_cached()['real_current']
    
    def read_power(self) -> float:
        """Read actual output power (V × A)"""
        s = self.read_status_cached()
        return s['real_voltage'] * s['real_current']
    
    def is_power_on(self) -> bool:
        """Check if power output is enabled"""
        return self.read_status_cached()['power_on']
    
    def is_constant_current(self) -> bool:
        """Check if device is in constant current mode"""
        return self.read_status_cached()['constant_current_mode']
    
    def has_alarm(self) -> bool:
        """Check if device has active alarms"""
        return self.read_status_cached()['alarm_active']
    
    # Utility methods
    def get_device_info(self) -> Dict:
        """Get comprehensive device information"""
        return {
            'model': self.device_model,
            'nominal_voltage': self.nominal_voltage,
            'nominal_current': self.nominal_current,
            'max_voltage': self.max_voltage,
            'max_current': self.max_current,
            'voltage_precision': self.voltage_decimal_places,
            'current_precision': self.current_decimal_places,
            'voltage_series': self.voltage_series,
            'current_series': self.current_series,
            'endianness': 'Little' if self.little_endian else 'Big',
            'slave_address': self.slave_addr,
            'connected': self.connected,
            'port': self.serial.port if self.serial else None,
            'baudrate': self.serial.baudrate if self.serial else None,
            'debug_enabled': self.debug_logger.enabled,
            'debug_log_file': self.debug_logger.log_file if self.debug_logger.enabled else None
        }
    
    def print_status(self):
        """Print formatted status information"""
        status = self.read_status()
        print(f"\n📊 {self.device_model} Status:")
        print(f"   Power: {'🟢 ON' if status['power_on'] else '🔴 OFF'}")
        print(f"   Output: {status['real_voltage']:.{self.voltage_decimal_places}f}V / {status['real_current']:.{self.current_decimal_places}f}A ({status['real_power']:.2f}W)")
        print(f"   Settings: {status['set_voltage']:.{self.voltage_decimal_places}f}V / {status['set_current']:.{self.current_decimal_places}f}A")
        print(f"   Mode: {'CC (Constant Current)' if status['constant_current_mode'] else 'CV (Constant Voltage)'}")
        print(f"   OCP: {'🟢 Enabled' if status['ocp_enabled'] else '🔴 Disabled'}")
        print(f"   Keyboard: {'🔒 Locked' if status['keyboard_locked'] else '🔓 Unlocked'}")
        if status['alarm_active']:
            print(f"   ⚠️  ALARM ACTIVE")
        
        if self.debug_logger.enabled:
            print(f"   🐛 Debug: ON (log: {self.debug_logger.log_file})")
    
    def reconnect(self) -> bool:
        """Attempt to reconnect to the device"""
        if self.serial and hasattr(self.serial, 'port') and hasattr(self.serial, 'baudrate'):
            self.debug_logger.info(f"Attempting to reconnect to {self.serial.port}")
            return self._connect(self.serial.port, self.serial.baudrate)
        else:
            self.debug_logger.error("Cannot reconnect: no previous connection info")
            return False
    
    def close(self):
        """Close the serial connection"""
        self.debug_logger.info("Closing connection")
        if self.serial and self.serial.is_open:
            self.serial.close()
        self.connected = False
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Advanced usage examples and utilities
class WanptekMonitor:
    """Continuous monitoring utility for WANPTEK power supplies with debug support"""
    
    def __init__(self, psu: WanptekPowerSupply, interval: float = 1.0):
        self.psu = psu
        self.interval = interval
        self.monitoring = False
        
    def start_monitoring(self, callback=None):
        """Start continuous monitoring"""
        self.monitoring = True
        self.psu.debug_logger.info(f"Starting monitoring every {self.interval}s")
        print(f"🔄 Starting monitoring every {self.interval}s (Ctrl+C to stop)")
        
        try:
            while self.monitoring:
                start_time = time.time()
                status = self.psu.read_status()
                read_time = time.time() - start_time
                
                if callback:
                    callback(status)
                else:
                    # Default display
                    print(f"\r{status['real_voltage']:.2f}V {status['real_current']:.3f}A {status['real_power']:.2f}W {'ON' if status['power_on'] else 'OFF'} ({read_time:.3f}s)", end='', flush=True)
                
                time.sleep(max(0, self.interval - read_time))
                
        except KeyboardInterrupt:
            self.monitoring = False
            self.psu.debug_logger.info("Monitoring stopped by user")
            print("\n⏹️  Monitoring stopped")


# Debug test functions
def test_communication_detailed(psu: WanptekPowerSupply):
    """Perform detailed communication tests with full debug output"""
    print("\n🧪 Running detailed communication tests...")
    psu.debug_logger.info("=== STARTING DETAILED COMMUNICATION TESTS ===")
    
    # Test 1: Basic status read
    print("Test 1: Reading status...")
    try:
        status = psu.read_status()
        print(f"✅ Status read successful: {status['real_voltage']:.2f}V, {status['real_current']:.3f}A")
    except Exception as e:
        print(f"❌ Status read failed: {e}")
    
    # Test 2: Set voltage
    print("Test 2: Setting voltage to 5.0V...")
    try:
        result = psu.set_voltage(5.0)
        print(f"✅ Voltage set {'successful' if result else 'failed'}")
    except Exception as e:
        print(f"❌ Voltage set failed: {e}")
    
    # Test 3: Power control
    print("Test 3: Power on/off...")
    try:
        psu.power_off()
        time.sleep(0.5)
        psu.power_on()
        print("✅ Power control successful")
    except Exception as e:
        print(f"❌ Power control failed: {e}")
    
    # Test 4: Multiple rapid reads
    print("Test 4: Rapid status reads...")
    try:
        for i in range(5):
            status = psu.read_status()
            print(f"  Read {i+1}: {status['real_voltage']:.2f}V")
            time.sleep(0.1)
        print("✅ Rapid reads successful")
    except Exception as e:
        print(f"❌ Rapid reads failed: {e}")
    
    psu.debug_logger.info("=== DETAILED COMMUNICATION TESTS COMPLETED ===")


# Example usage and testing with debug features
if __name__ == "__main__":
    print("🔌 WANPTEK Universal Power Supply Controller with Debug Logging")
    print("=" * 60)
    
    try:
        # Initialize with debug enabled
        print("Initializing with debug logging enabled...")
        with WanptekPowerSupply(port='/dev/ttyUSB0', auto_detect=True, debug=True) as psu:
            
            print(f"\n📄 Debug log file: {psu.debug_logger.log_file}")
            
            # Display device information  
            psu.print_status()
            
            # Run detailed tests
            test_communication_detailed(psu)
            
            # Test basic operations
            print(f"\n🧪 Testing basic operations...")
            
            # Set 5V, 1A
            print("Setting 5.0V, 1.0A...")
            psu.set_output(voltage=5.0, current=1.0, power_on=False)
            
            # Turn on power
            print("Turning on power...")
            psu.power_on()
            time.sleep(0.5)
            
            # Read actual values
            voltage = psu.read_voltage()
            current = psu.read_current()
            power = psu.read_power()
            print(f"✅ Output: {voltage:.2f}V, {current:.3f}A, {power:.2f}W")
            
            # Test current limit
            print("Testing current limit at 0.5A...")
            psu.set_current(0.5)
            time.sleep(0.5)
            
            if psu.is_constant_current():
                print("✅ Device entered constant current mode")
            
            # Turn off
            print("Turning off power...")
            psu.power_off()
            
            print("✅ All tests completed successfully!")
            print(f"\n📄 Complete debug log saved to: {psu.debug_logger.log_file}")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        print("\n💡 Troubleshooting tips:")
        print("   - Check device is connected to /dev/ttyUSB0")
        print("   - Verify device address (default: 0)")
        print("   - Try different baudrates: 9600, 4800, 19200, 2400")
        print("   - Check USB cable and connections")
        print("   - Review debug log for detailed communication traces")
