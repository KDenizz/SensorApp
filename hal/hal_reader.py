"""
hal/hal_reader.py

[HAL Layer] Donanımdan sensör verilerini asenkron olarak okuyan görev (asyncio.Task).

Migration Notları (threading.Thread → asyncio.Task):
    1. `threading.Thread` subclass'ı                → Düz class. `run()` metodu `async def` oldu.
    2. `time.sleep(sleep_time)`                      → `await asyncio.sleep(sleep_time)`
    3. `self.context.stop_event.is_set()`            → `self.context.stop_event.is_set()` (aynı API, asyncio.Event)
    4. `self.context.raw_data_queue.put(packet)`     → `await self.context.raw_data_queue.put(packet)`
    5. `self.context.signal_bus.alarm_triggered...`  → `await self.context.broadcaster.publish("ALARM_TRIGGERED", ...)`
    6. `self.context.signal_bus.sensor_data_ready...`→ `await self.context.broadcaster.publish("SENSOR_DATA", ...)`
    7. Seri port okuma (blocking `readline`)         → `loop.run_in_executor(None, ...)` ile thread pool'da

Mimari Kural:
    - Bu katman Controller'a doğrudan DOKUNMAZ.
    - Veri yalnızca context.raw_data_queue'ya bırakılır.
    - UI bildirimi yalnızca context.broadcaster.publish() ile yapılır.

Not (Mock):
    AppContext üzerindeki `get_mock_position()` ve `set_mock_current()` metodları
    gerçek donanım geldiğinde kaldırılacak. Tüm fizik simülasyonu bu sınıfta kalmaya devam eder.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from core.data_types import SensorPacket, AlarmCode
from hal.serial_port_manager import AsyncSerialPortManager

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class HALReader:
    """
    [HAL Layer] Donanımdan SADECE sensör verilerini okur ve `raw_data_queue`'ya bırakır.

    Kullanım (main.py içinde):
        reader = HALReader(context)
        asyncio.create_task(reader.run())
    """

    def __init__(self, context: "AppContext") -> None:
        self.context = context

        hw_config = self.context.config.hardware
        self.port_name: str = hw_config.get("port", "COM1")
        self.baud_rate: int = hw_config.get("baud_rate", 115200)

        rate = hw_config.get("sample_rate_hz", 100)
        if rate <= 0:
            logger.warning(f"Geçersiz sample_rate_hz: {rate}, varsayılan 100Hz kullanılıyor.")
            self.sample_rate_hz = 100
        else:
            self.sample_rate_hz = rate

        self._loop_delay: float = 1.0 / self.sample_rate_hz

        # Mock sabitleri
        self._mock_p1: float = 50.0
        self._mock_temp: float = 298.15

        self._is_connected: bool = False
        self._reconnect_attempts: int = 0
        self._max_reconnect_attempts: int = 5

        self.serial_manager = AsyncSerialPortManager()

    # ------------------------------------------------------------------
    # Ana Döngü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        asyncio.Task olarak çalışan ana okuma döngüsü.
        stop_event set edildiğinde döngüden çıkar ve bağlantıyı kapatır.
        """
        connected = await self._connect_to_hardware()
        if not connected:
            await self.context.broadcaster.publish(
                "ALARM_TRIGGERED",
                {"code": int(AlarmCode.COMMUNICATION_LOST), "reason": "HALReader başlangıç bağlantısı başarısız."}
            )
            return

        logger.info(f"HALReader {self.sample_rate_hz}Hz hızında başlatıldı. Port: {self.port_name}")

        while not self.context.stop_event.is_set():
            loop_start = time.monotonic()

            try:
                await self._read_incoming_sensors()
            except Exception as e:
                logger.error(f"Okuma döngüsünde hata: {e}", exc_info=True)
                await self.context.broadcaster.publish(
                    "ALARM_TRIGGERED",
                    {"code": int(AlarmCode.COMMUNICATION_LOST), "reason": str(e)}
                )
                recovered = await self._handle_connection_loss()
                if not recovered:
                    break

            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, self._loop_delay - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)  # ← blocking time.sleep() KALDIRILDI

        await self._disconnect_from_hardware()
        logger.info("HALReader güvenli şekilde sonlandırıldı.")

    # ------------------------------------------------------------------
    # Bağlantı Yönetimi
    # ------------------------------------------------------------------

    async def _connect_to_hardware(self) -> bool:
        """
        Retry mekanizmalı donanım bağlantısı.
        Blocking `serial.Serial()` çağrısı run_in_executor ile thread pool'a gönderilir.

        Returns:
            bool: Bağlantı başarılıysa True, tüm denemeler tükendiyse False.
        """
        loop = asyncio.get_running_loop()
        retry_count = 0

        while not self.context.stop_event.is_set() and retry_count < self._max_reconnect_attempts:
            try:
                # Blocking I/O → executor'a taşındı
                await loop.run_in_executor(
                    None,
                    self.serial_manager.open,
                    self.port_name,
                    self.baud_rate
                )
                self._is_connected = True
                self._reconnect_attempts = 0
                logger.info(f"Donanıma bağlandı: {self.port_name}")
                return True
            except Exception as e:
                retry_count += 1
                logger.error(f"Bağlantı hatası ({retry_count}/{self._max_reconnect_attempts}): {e}")
                await asyncio.sleep(2.0)  # ← blocking time.sleep() değil

        logger.critical(f"Donanıma bağlanılamadı: {self.port_name}")
        return False

    async def _handle_connection_loss(self) -> bool:
        """
        Bağlantı kaybı durumunda kademeli retry.

        Returns:
            bool: Yeniden bağlantı başarılıysa True, maksimum deneme aşıldıysa False.
        """
        self._is_connected = False
        self._reconnect_attempts += 1

        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.critical(
                f"Maksimum yeniden bağlanma denemesi ({self._max_reconnect_attempts}) aşıldı. "
                "Sistem kapatılıyor."
            )
            await self.context.request_shutdown()
            return False

        logger.warning(
            f"Bağlantı koptu. Yeniden bağlanılıyor... "
            f"({self._reconnect_attempts}/{self._max_reconnect_attempts})"
        )
        return await self._connect_to_hardware()

    async def _disconnect_from_hardware(self) -> None:
        """Donanım bağlantısını kapatır."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self.serial_manager.close)
        self._is_connected = False
        logger.info("Donanım bağlantısı kapatıldı.")

    # ------------------------------------------------------------------
    # Sensör Okuma
    # ------------------------------------------------------------------

    async def _read_incoming_sensors(self) -> None:
        """
        Sensörlerden veri okur, parse eder ve sisteme yayınlar.

        Gerçek donanımda:
            raw_bytes = await loop.run_in_executor(None, self.serial_manager.readline)
            packet = self._parser.parse_raw(raw_bytes)

        Şu an mock verisi üretilmektedir.
        """
        if not self._is_connected:
            return

        # --- Mock Fizik ---
        current_pos = self.context.get_mock_position()
        max_tick = self.context.config.hardware.get("max_tick", 1000)
        opening_ratio = min(max(current_pos / max_tick, 0.0), 1.0)

        mock_p2 = self._update_mock_p2(opening_ratio)
        mock_current = self._update_mock_current(opening_ratio)

        # Tutarlılık için mock akımı context'e yaz
        self.context.set_mock_current(mock_current)

        packet = SensorPacket(
            p1_raw=self._mock_p1,
            p2_raw=mock_p2,
            temp_k=self._mock_temp,
            motor_pos_ticks=current_pos,
            motor_current_ma=mock_current,
            timestamp=time.monotonic()
        )

        # Controller için kuyruğa at (non-blocking put)
        await self.context.raw_data_queue.put(packet)

        # UI için WebSocket üzerinden yayınla (signal_bus.sensor_data_ready → broadcaster)
        await self.context.broadcaster.publish("SENSOR_DATA", packet)

    # ------------------------------------------------------------------
    # Mock Yardımcılar (Gerçek donanımda kaldırılacak)
    # ------------------------------------------------------------------

    def _update_mock_p2(self, opening_ratio: float) -> float:
        """Valf açıldıkça downstream basıncı DÜŞER. (P2: 40 bar → 28 bar)"""
        return 40.0 - (opening_ratio * 12.0)

    def _update_mock_current(self, opening_ratio: float) -> float:
        """Hareket halinde akım artar, %50 açıklıkta zirve yapar."""
        return 12.0 + abs(opening_ratio - 0.5) * 25.0