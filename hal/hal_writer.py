"""
hal/hal_writer.py

[HAL Layer] Komut kuyruğundan MotorCommand'ları asenkron olarak tüketen ve
donanıma ileten görev (asyncio.Task).

Migration Notları (threading.Thread → asyncio.Task):
    1. `threading.Thread` subclass'ı                 → Düz class. `run()` async def oldu.
    2. `queue.get(timeout=0.01)` + `Empty` catch      → `asyncio.wait_for(queue.get(), timeout=0.01)` + `asyncio.TimeoutError`
    3. `context.command_queue.task_done()`            → `context.command_queue.task_done()` (aynı API)
    4. `signal_bus.alarm_triggered.emit(...)`         → `await context.broadcaster.publish("ALARM_TRIGGERED", ...)`
    5. Blocking seri port yazma (`serial_manager.write`) → `loop.run_in_executor(None, ...)` ile thread pool'da
    6. `time.sleep(1.0)`                              → `await asyncio.sleep(1.0)`

Mimari Kural:
    - Bu katman Controller veya State'e doğrudan DOKUNMAZ.
    - Komut yalnızca context.command_queue'dan okunur.
    - UI bildirimi yalnızca context.broadcaster.publish() ile yapılır.
    - Mock state güncellemeleri (set_mock_position) gerçek donanımda kaldırılacak.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from core.data_types import MotorCommand, CommandType, AlarmCode
from hal.serial_port_manager import AsyncSerialPortManager

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class HALWriter:
    """
    [HAL Layer] Donanıma SADECE komut yazar.

    `context.command_queue` bir asyncio.PriorityQueue'dur. Tuple formatı: (priority_int, MotorCommand).
    Düşük sayı = yüksek öncelik (0 = EMERGENCY).

    Kullanım (main.py içinde):
        writer = HALWriter(context)
        asyncio.create_task(writer.run())
    """

    # Kuyruk boşsa bu kadar bekle (saniye). Çok küçük değer → spin, çok büyük → komut gecikmesi.
    _QUEUE_TIMEOUT: float = 0.01

    def __init__(self, context: "AppContext") -> None:
        self.context = context

        hw_config = self.context.config.hardware
        self.port_name: str = hw_config.get("port", "COM1")
        self.baud_rate: int = hw_config.get("baud_rate", 115200)

        self.serial_manager = AsyncSerialPortManager()
        self._is_connected: bool = False

    # ------------------------------------------------------------------
    # Ana Döngü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        asyncio.Task olarak çalışan ana yazma döngüsü.
        stop_event set edildiğinde kalan komutları tükettikten sonra kapatır.
        """
        try:
            await self._connect_to_hardware()
        except Exception as e:
            logger.critical(f"HALWriter bağlantı hatası: {e}")
            await self.context.broadcaster.publish(
                "ALARM_TRIGGERED",
                {"code": int(AlarmCode.COMMUNICATION_LOST), "reason": str(e)}
            )
            return

        logger.info(f"HALWriter başlatıldı. Port: {self.port_name}")

        while not self.context.stop_event.is_set():
            if not self._is_connected:
                await self._handle_connection_loss()
                continue

            try:
                # Kuyrukta komut bekle; timeout ile periyodik olarak stop_event'i kontrol etmeye izin ver
                priority, command = await asyncio.wait_for(
                    self.context.command_queue.get(),
                    timeout=self._QUEUE_TIMEOUT
                )
                await self._execute_command(command)
                self.context.command_queue.task_done()

            except asyncio.TimeoutError:
                # Kuyruk boş → döngü başa döner, stop_event kontrol edilir
                continue
            except Exception as e:
                logger.error(f"Yazma döngüsünde hata: {e}", exc_info=True)
                await self.context.broadcaster.publish(
                    "ALARM_TRIGGERED",
                    {"code": int(AlarmCode.COMMUNICATION_LOST), "reason": str(e)}
                )
                self._is_connected = False

        await self._disconnect_from_hardware()
        logger.info("HALWriter güvenli şekilde sonlandırıldı.")

    # ------------------------------------------------------------------
    # Bağlantı Yönetimi
    # ------------------------------------------------------------------

    async def _connect_to_hardware(self) -> None:
        """
        Blocking serial.Serial() → executor'a taşındı.
        Raises:
            Exception: Bağlantı kurulamazsa.
        """
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self.serial_manager.open,
            self.port_name,
            self.baud_rate
        )
        self._is_connected = True
        logger.info(f"HALWriter donanıma bağlandı: {self.port_name}")

    async def _handle_connection_loss(self) -> None:
        """Bağlantı kaybında kısa gecikme sonrası yeniden bağlanmayı dener."""
        logger.error("HALWriter bağlantısı koptu, yeniden deneniyor...")
        await asyncio.sleep(1.0)  # ← blocking time.sleep() değil
        try:
            await self._connect_to_hardware()
        except Exception as e:
            logger.error(f"HALWriter yeniden bağlanamadı: {e}")

    async def _disconnect_from_hardware(self) -> None:
        """Bağlantıyı executor üzerinden güvenle kapatır."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.serial_manager.close)
        self._is_connected = False

    # ------------------------------------------------------------------
    # Komut İşleyici
    # ------------------------------------------------------------------

    async def _execute_command(self, command: MotorCommand) -> None:
        """
        Tek bir MotorCommand'ı donanıma iletir.

        Blocking seri port yazma işlemleri run_in_executor ile
        thread pool'a gönderilir; event loop bloklanmaz.

        Args:
            command: İşlenecek MotorCommand nesnesi.
        """
        loop = asyncio.get_running_loop()

        if command.type == CommandType.STOP_IMMEDIATE:
            logger.warning("HALWriter: ACİL DURDURMA KOMUTU İLETİLDİ!")
            # Gerçek donanımda:
            # await loop.run_in_executor(None, self.serial_manager.write, b'STOP\n')
            self.context.set_mock_position(self.context.get_mock_position())  # Konumu dondur

        elif command.type == CommandType.MOVE_ABSOLUTE:
            target_pos = int(command.value)
            # await loop.run_in_executor(None, self.serial_manager.write, f'GOTO {target_pos}\n'.encode())
            self.context.set_mock_position(target_pos)
            logger.debug(f"HALWriter: MOVE_ABSOLUTE → {target_pos} ticks")

        elif command.type == CommandType.MOVE_RELATIVE:
            current = self.context.get_mock_position()
            new_pos = current + int(command.value)
            # await loop.run_in_executor(None, self.serial_manager.write, f'MOVE {int(command.value)}\n'.encode())
            self.context.set_mock_position(new_pos)
            logger.debug(f"HALWriter: MOVE_RELATIVE → Δ{int(command.value)} ticks → yeni: {new_pos}")

        elif command.type == CommandType.MOVE_VELOCITY:
            speed_pct = command.value
            direction = command.direction
            # await loop.run_in_executor(None, self.serial_manager.write, f'VEL {direction} {speed_pct}\n'.encode())
            logger.debug(f"HALWriter: MOVE_VELOCITY → yön={direction}, hız={speed_pct}%")

        elif command.type == CommandType.CALIBRATE:
            logger.info("HALWriter: KALİBRASYON KOMUTU İLETİLDİ")
            # await loop.run_in_executor(None, self.serial_manager.write, b'CALIBRATE\n')
            self.context.set_mock_position(0)

        elif command.type == CommandType.SET_SPEED:
            logger.debug(f"HALWriter: SET_SPEED → {command.value}")
            # await loop.run_in_executor(None, self.serial_manager.write, f'SPEED {command.value}\n'.encode())

        elif command.type == CommandType.SET_TORQUE:
            logger.debug(f"HALWriter: SET_TORQUE → {command.value}")
            # await loop.run_in_executor(None, self.serial_manager.write, f'TORQUE {command.value}\n'.encode())

        else:
            logger.warning(f"HALWriter: Bilinmeyen komut tipi → {command.type}")