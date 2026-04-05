import threading
import logging
from queue import Empty
import time

from core.app_context import AppContext
from core.data_types import MotorCommand, CommandType, AlarmCode
from hal.serial_port_manager import SerialPortManager

logger = logging.getLogger(__name__)

class HALWriter(threading.Thread):
    """
    [Thread 3] Donanıma SADECE komut yazar.
    Blocking Queue mantığı ile CPU dostu çalışır.
    """

    def __init__(self, context: AppContext) -> None:
        super().__init__(name="HALWriter-Thread")
        self.context = context
        self.daemon = True
        
        hw_config = self.context.config.hardware
        self.port_name: str = hw_config.get("port", "COM1")
        self.baud_rate: int = hw_config.get("baud_rate", 115200)
        
        # Singleton Port Manager referansı
        self.serial_manager = SerialPortManager()
        self._is_connected: bool = False

    def run(self) -> None:
        """CPU'yu yormayan (blocking) yazma döngüsü."""
        try:
            self._connect_to_hardware()
        except Exception as e:
            logger.critical(f"Bağlantı hatası: {e}")
            self.context.signal_bus.alarm_triggered.emit(int(AlarmCode.COMMUNICATION_LOST))
            return

        logger.info(f"HALWriter başlatıldı. Port: {self.port_name}")
        
        while not self.context.stop_event.is_set():
            if not self._is_connected:
                self._handle_connection_loss()
                continue

            try:
                # [DÜZELTME] CPU'yu blokla, boşsa uyu (sleep'e gerek yok)
                priority, command = self.context.command_queue.get(timeout=0.01)
                self._execute_command(command)
                self.context.command_queue.task_done()
            except Empty:
                # Kuyruk boş, döngü başa dönecek (stop_event kontrolü için)
                continue
            except Exception as e:
                logger.error(f"Yazma döngüsünde hata: {e}", exc_info=True)
                self.context.signal_bus.alarm_triggered.emit(int(AlarmCode.COMMUNICATION_LOST))
                self._is_connected = False

        self._disconnect_from_hardware()
        logger.info("HALWriter güvenli şekilde sonlandırıldı.")

    def _connect_to_hardware(self) -> None:
        """Aynı portu Manager üzerinden açar."""
        self.serial_manager.open(self.port_name, self.baud_rate)
        self._is_connected = True

    def _handle_connection_loss(self) -> None:

        logger.error("Writer bağlantısı koptu, yeniden deneniyor...")
        time.sleep(1.0)
        try:
            self._connect_to_hardware()
        except Exception as e:
            logger.error(f"Writer yeniden bağlanamadı: {e}")

    def _disconnect_from_hardware(self) -> None:
        self.serial_manager.close()
        self._is_connected = False

    def _execute_command(self, command: MotorCommand) -> None:
        """Tek bir komutu donanıma (Manager üzerinden) iletir."""
        if command.type == CommandType.STOP_IMMEDIATE:
            logger.warning("HALWriter: ACİL DURDURMA KOMUTU İLETİLDİ!")
            # self.serial_manager.write(b'STOP\n')
            
        elif command.type == CommandType.MOVE_ABSOLUTE:
            target_pos = int(command.value)
            # self.serial_manager.write(f'GOTO {target_pos}\n'.encode())
            self.context.set_mock_position(target_pos)
            logger.debug(f"HALWriter: MOVE_ABSOLUTE -> {target_pos} ticks")

        elif command.type == CommandType.MOVE_RELATIVE:
            current = self.context.get_mock_position()
            new_pos = current + int(command.value)
            # self.serial_manager.write(f'MOVE {int(command.value)}\n'.encode())
            self.context.set_mock_position(new_pos)
            
        elif command.type == CommandType.CALIBRATE:
            logger.info("HALWriter: KALİBRASYON KOMUTU İLETİLDİ")
            # self.serial_manager.write(b'CALIBRATE\n')
            self.context.set_mock_position(0)