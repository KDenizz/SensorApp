import threading
import logging
# import serial # Gerçek donanımda açılacak

logger = logging.getLogger(__name__)

class SerialPortManager:
    """
    Reader ve Writer thread'lerinin aynı fiziksel portu (COM/ttyUSB) 
    çarpışmadan (race condition) kullanmasını sağlayan thread-safe Singleton.
    """
    _instance = None
    _init_lock = threading.Lock()
    
    def __new__(cls):
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._port = None
                cls._instance._io_lock = threading.Lock() # Okuma/Yazma işlemleri için kilit
        return cls._instance

    def open(self, port: str, baud: int) -> None:
        """Portu açar, zaten açıksa hiçbir şey yapmaz."""
        with self._io_lock:
            # TODO: Gerçek pySerial entegrasyonu
            # if self._port is None or not self._port.is_open:
            #     self._port = serial.Serial(port, baud, timeout=0.1)
            logger.debug(f"SerialPortManager: {port} @ {baud} bps mock bağlantısı açıldı.")
            self._is_open = True

    def readline(self) -> bytes:
        """Reader thread'i tarafından çağrılır. (Thread-safe)"""
        with self._io_lock:
            # TODO: return self._port.readline()
            return b"" # Mock return

    def write(self, data: bytes) -> None:
        """Writer thread'i tarafından çağrılır. (Thread-safe)"""
        with self._io_lock:
            # TODO: self._port.write(data)
            pass

    def close(self) -> None:
        with self._io_lock:
            # TODO: if self._port and self._port.is_open: self._port.close()
            self._is_open = False
            logger.debug("SerialPortManager: Bağlantı kapatıldı.")