# Thread 1: Arduino -> parse -> raw_data_queue

# hal/hal_reader.py (GÜNCELLENMİŞ)

import threading
import time
import logging
from queue import Empty

from core.app_context import AppContext
from core.datatypes import SensorPacket, AlarmCode
from hal.serial_port_manager import SerialPortManager  

logger = logging.getLogger(__name__)


class HALReader(threading.Thread):
    """
    [Thread 1] Donanımdan SADECE sensör verilerini okur.
    Mock state AppContext'te tutulur, buradan okunur.
    """

    def __init__(self, context: AppContext) -> None:
        super().__init__(name="HALReader-Thread")
        self.context = context
        self.daemon = True
        
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
        
        # Mock sabitleri (AppContext'teki state'ten bağımsız)
        self._mock_p1: float = 50.0      # Giriş basıncı sabit
        self._mock_temp: float = 298.15  # Sıcaklık sabit
        
        self._is_connected: bool = False
        self._reconnect_attempts: int = 0
        self._max_reconnect_attempts: int = 5
        self.serial_manager = SerialPortManager()

    def run(self) -> None:
        """Sadece okuma döngüsü."""
        try:
            self._connect_to_hardware()
        except ConnectionError as e:
            logger.critical(str(e))
            self.context.signal_bus.alarm_triggered.emit(int(AlarmCode.COMMUNICATION_LOST))
            return

        logger.info(f"HALReader {self.sample_rate_hz}Hz hızında başlatıldı. Port: {self.port_name}")
        
        while not self.context.stop_event.is_set():
            loop_start = time.monotonic()

            try:
                self._read_incoming_sensors()
            except Exception as e:
                logger.error(f"Okuma döngüsünde hata: {e}", exc_info=True)
                self.context.signal_bus.alarm_triggered.emit(int(AlarmCode.COMMUNICATION_LOST))
                self._handle_connection_loss()

            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, self._loop_delay - elapsed)
            if sleep_time > 0:
                time.sleep(sleep_time)

        self._disconnect_from_hardware()
        logger.info("HALReader güvenli şekilde sonlandırıldı.")

    def _connect_to_hardware(self) -> None:
        """Retry mekanizmalı donanım bağlantısı."""
        retry_count = 0
        
        while not self.context.stop_event.is_set() and retry_count < self._max_reconnect_attempts:
            try:
                self.serial_manager.open(self.port_name, self.baud_rate)
                self._is_connected = True
                self._reconnect_attempts = 0
                logger.info(f"Donanıma bağlandı: {self.port_name}")
                return
            except Exception as e:
                retry_count += 1
                logger.error(f"Bağlantı hatası ({retry_count}/{self._max_reconnect_attempts}): {e}")
                time.sleep(2)
                
        raise ConnectionError(f"Donanıma bağlanılamadı: {self.port_name}")

    def _handle_connection_loss(self) -> None:
        """Bağlantı kaybı durumunda yapılacak işlemler."""
        self._is_connected = False
        self._reconnect_attempts += 1
        
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.critical(f"Maksimum yeniden bağlanma denemesi ({self._max_reconnect_attempts}) aşıldı.")
            self.context.request_shutdown()
            return
        
        try:
            self._connect_to_hardware()
        except ConnectionError:
            logger.error("Yeniden bağlanma başarısız, sonraki döngüde tekrar denenecek.")

    def _disconnect_from_hardware(self) -> None:
        """Donanım bağlantısını kapatır."""
        self.serial_manager.close()
        self._is_connected = False
        logger.info("Donanım bağlantısı kapatıldı.")

    def _update_mock_p2(self, opening_ratio: float) -> float:
        """
        Fiziksel gerçekliğe uygun P2 simülasyonu.
        Valf açıldıkça downstream basıncı DÜŞER.
        
        Args:
            opening_ratio: 0.0 (kapalı) - 1.0 (tam açık)
        
        Returns:
            float: Simüle edilmiş P2 değeri (bar)
        """
        # P2: Valf tam kapalıyken 40 bar, tam açıkken 28 bar
        return 40.0 - (opening_ratio * 12.0)

    def _update_mock_current(self, opening_ratio: float) -> float:
        """
        Motor akımı simülasyonu.
        Hareket halinde akım artar, sabit konumda düşer.
        
        Args:
            opening_ratio: 0.0 (kapalı) - 1.0 (tam açık)
        
        Returns:
            float: Simüle edilmiş akım değeri (mA)
        """
        # Basit simülasyon: açıklık %50 civarında maksimum akım
        return 12.0 + abs(opening_ratio - 0.5) * 25.0

    def _read_incoming_sensors(self) -> None:
        """
        Sensörlerden veri okur, parse eder ve sisteme yayınlar.
        """
        if not self._is_connected:
            return

        # Mock pozisyonu AppContext'ten al (HALWriter tarafından güncellenir)
        current_pos = self.context.get_mock_position()
        
        # Açıklık oranını hesapla (max_tick = 1000 varsayımı)
        max_tick = 1000  # TODO: config'den alınacak
        opening_ratio = min(max(current_pos / max_tick, 0.0), 1.0)
        
        # Fiziksel büyüklükleri simüle et
        mock_p2 = self._update_mock_p2(opening_ratio)
        mock_current = self._update_mock_current(opening_ratio)
        
        # Mock akımı AppContext'e de yaz (tutarlılık için)
        self.context.set_mock_current(mock_current)
        
        # SensorPacket oluştur
        packet = SensorPacket(
            p1_raw=self._mock_p1,
            p2_raw=mock_p2,
            temp_k=self._mock_temp,
            motor_pos_ticks=current_pos,
            motor_current_ma=mock_current,
            timestamp=time.monotonic()
        )

        # Controller için kuyruğa at
        self.context.raw_data_queue.put(packet)
        
        # UI için sinyal yayın (object tipinde, SensorPacket taşır)
        self.context.signal_bus.sensor_data_ready.emit(packet)