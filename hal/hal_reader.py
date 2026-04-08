"""
hal/hal_reader.py

[HAL Layer] Modbus RTU üzerinden Input Register'ları asenkron olarak
polling eden görev (asyncio.Task).

Mock tamamen kaldırıldı. Artık:
    - ModbusRTUClient ile 30001–30005 arası 5 Input Register okunur.
    - Ham değerler RegisterDef.scale() ile fiziksel birimlere çevrilir.
    - status_word içindeki StatusBits kontrol edilir, alarm üretilir.
    - SensorPacket oluşturulup raw_data_queue ve broadcaster'a gönderilir.

Mimari Kural:
    - Bu katman Controller'a doğrudan DOKUNMAZ.
    - Veri yalnızca context.raw_data_queue'ya bırakılır.
    - UI bildirimi yalnızca context.broadcaster.publish() ile yapılır.
    - Modbus bağlantısı yalnızca bu sınıf tarafından yönetilir
      (HALWriter kendi bağlantısını ayrıca kurar — iki ayrı istemci,
       pymodbus async client thread-safe değildir).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

from core.data_types import SensorPacket, AlarmCode
from hal.modbus_client import ModbusRTUClient
from hal.modbus_config import Reg, StatusBits

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class HALReader:
    """
    [HAL Layer] Modbus RTU Input Register'larını polling eder,
    parse eder ve sisteme yayınlar.

    Kullanım (main.py içinde):
        reader = HALReader(context)
        asyncio.create_task(reader.run())
    """

    def __init__(self, context: "AppContext") -> None:
        self.context = context

        hw = self.context.config.hardware
        self._port:      str   = hw.get("port",           "COM1")
        self._baudrate:  int   = hw.get("baud_rate",      115200)
        self._slave_id:  int   = hw.get("slave_id",       1)
        self._timeout:   float = hw.get("modbus_timeout", 0.1)

        rate = hw.get("sample_rate_hz", 50)
        if rate <= 0:
            logger.warning(f"Geçersiz sample_rate_hz={rate}, varsayılan 50 Hz kullanılıyor.")
            rate = 50
        self._sample_rate_hz: int   = rate
        self._loop_delay:     float = 1.0 / self._sample_rate_hz

        self._max_reconnect: int = hw.get("max_reconnect_attempts", 5)
        self._reconnect_attempts: int = 0

        # Her polling döngüsünde aynı (address, count) kullanılır — bir kez hesapla.
        self._read_addr, self._read_count = Reg.INPUT.block()  # (0, 5)

        self._client: ModbusRTUClient = context.modbus_client

        # Ardışık okuma hatası sayacı — geçici gürültüyü alarm'dan ayırt eder.
        self._consecutive_errors: int = 0
        self._max_consecutive_errors: int = hw.get("max_consecutive_errors", 10)

    # ------------------------------------------------------------------
    # Ana Döngü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        asyncio.Task olarak çalışan ana okuma döngüsü.
        stop_event set edildiğinde döngüden çıkar ve bağlantıyı kapatır.
        """
        connected = await self._connect()
        if not connected:
            await self._publish_alarm(
                AlarmCode.COMMUNICATION_LOST,
                f"HALReader başlangıç bağlantısı başarısız: {self._port}"
            )
            return

        logger.info(
            f"HALReader başlatıldı — {self._sample_rate_hz} Hz, "
            f"port={self._port}, slave={self._slave_id}"
        )

        while not self.context.stop_event.is_set():
            loop_start = time.monotonic()

            try:
                await self._poll_registers()
            except Exception as e:
                logger.error(f"HALReader polling döngüsünde beklenmeyen hata: {e}", exc_info=True)
                recovered = await self._handle_connection_loss()
                if not recovered:
                    break

            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, self._loop_delay - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        await self._disconnect()
        logger.info("HALReader güvenli şekilde sonlandırıldı.")

    # ------------------------------------------------------------------
    # Bağlantı Yönetimi
    # ------------------------------------------------------------------
    
    
    """
    async def _connect(self) -> bool:
       
        #Yeni bir ModbusRTUClient oluşturur ve bağlantıyı dener. Her deneme arasında 2 saniye bekler.
        
        for attempt in range(1, self._max_reconnect + 1):
            if self.context.stop_event.is_set():
                return False

            self._client = ModbusRTUClient(
                port=self._port,
                baudrate=self._baudrate,
                timeout=self._timeout,
                slave_id=self._slave_id,
            )
            success = await self._client.connect()
            if success:
                self._reconnect_attempts = 0
                self._consecutive_errors = 0
                return True

            logger.warning(
                f"HALReader bağlantı denemesi {attempt}/{self._max_reconnect} başarısız."
            )
            await asyncio.sleep(2.0)

        logger.critical(f"HALReader: {self._max_reconnect} denemede bağlanılamadı.")
        return False
    """


    async def _handle_connection_loss(self) -> bool:
        """
        Bağlantı kaybında kademeli yeniden bağlanma.
        Maksimum deneme aşılırsa sistemi kapatır.
        """
        self._reconnect_attempts += 1

        if self._reconnect_attempts >= self._max_reconnect:
            logger.critical(
                f"HALReader: Maksimum yeniden bağlanma ({self._max_reconnect}) aşıldı. "
                "Sistem kapatılıyor."
            )
            await self.context.request_shutdown()
            return False

        logger.warning(
            f"HALReader bağlantısı koptu — "
            f"yeniden bağlanılıyor ({self._reconnect_attempts}/{self._max_reconnect})..."
        )

        if self._client:
            await self._client.disconnect()

        return await self._connect()
    

    """
    async def _disconnect(self) -> None:
        # Modbus bağlantısını güvenle kapatır.
        if self._client:
            await self._client.disconnect()
            self._client = None
    """


    # ------------------------------------------------------------------
    # Register Polling ve Parse
    # ------------------------------------------------------------------

    async def _poll_registers(self) -> None:
        """
        5 Input Register'ı tek Modbus isteğiyle okur, parse eder ve yayınlar.

        Hata durumları:
            - None dönerse → ardışık hata sayacı artar.
            - Sayaç eşiği aşarsa → COMMUNICATION_LOST alarmı tetiklenir.
            - Eşik aşılmamışsa → paket üretilmez, döngü devam eder.
        """
        if not self._client or not self._client.is_connected:
            raise ConnectionError("Modbus istemcisi bağlı değil.")

        raw = await self._client.read_input_registers(
            address=self._read_addr,
            count=self._read_count,
        )

        # None → okuma başarısız
        if raw is None:
            self._consecutive_errors += 1
            logger.warning(
                f"Input Register okunamadı "
                f"({self._consecutive_errors}/{self._max_consecutive_errors} ardışık hata)"
            )

            if self._consecutive_errors >= self._max_consecutive_errors:
                await self._publish_alarm(
                    AlarmCode.COMMUNICATION_LOST,
                    f"{self._consecutive_errors} ardışık Modbus okuma hatası."
                )
                raise ConnectionError("Ardışık Modbus okuma hatası eşiği aşıldı.")
            return

        # Başarılı okuma — hata sayacını sıfırla
        self._consecutive_errors = 0

        # Register listesi beklenen boyutta mı?
        if len(raw) < self._read_count:
            logger.error(
                f"Eksik register verisi: beklenen={self._read_count}, gelen={len(raw)}"
            )
            return

        packet = self._parse(raw)
        if packet is None:
            return

        # Controller kuyruğuna (non-blocking)
        await self.context.raw_data_queue.put(packet)

        # WebSocket üzerinden UI'a
        await self.context.broadcaster.publish("SENSOR_DATA", packet)

    def _parse(self, raw: list[int]) -> Optional[SensorPacket]:
        """
        Ham UInt16 register listesini SensorPacket'e çevirir.

        Register sırası (Reg.INPUT.block() → address=0, count=5):
            raw[0] → 30001  status_word          (bit bazlı, scaling=1)
            raw[1] → 30002  current_position_rev (tur,       scaling=1)
            raw[2] → 30003  current_position_step(adım,      scaling=1)
            raw[3] → 30004  external_signal_ma   (mA×100,    scaling=100)
            raw[4] → 30005  motor_torque_pct     (%,         scaling=1)

        SensorPacket alanlarına eşleme:
            motor_pos_ticks  ← tur * step_resolution + step (encoder tick karşılığı)
            motor_current_ma ← external_signal_ma (dış sinyal mA olarak)
            p1_raw / p2_raw  ← Bu register haritasında YOK.
                               Basınç sensörleri farklı bir slave veya analog
                               giriş üzerindeyse ileride genişletilebilir.
                               Şimdilik 0.0 bırakılır.
        """
        r = Reg.INPUT  # Kısaltma

        status_word:  int   = raw[r.STATUS_WORD.address]
        pos_rev:      int   = raw[r.CURRENT_POSITION_REV.address]
        pos_step:     int   = raw[r.CURRENT_POSITION_STEP.address]
        signal_raw:   int   = raw[r.EXTERNAL_SIGNAL_MA.address]
        torque_raw:   int   = raw[r.MOTOR_TORQUE_PCT.address]

        # Ölçekleme
        signal_ma:  float = r.EXTERNAL_SIGNAL_MA.scale(signal_raw)   # 1200 → 12.00
        torque_pct: float = r.MOTOR_TORQUE_PCT.scale(torque_raw)      # 75 → 75.0

        # Status Word bit kontrolü — alarmlar asenkron olarak üretilir.
        self._check_status_bits(status_word)

        # Tick hesabı: AppContext'teki step_resolution ayarına göre normalize.
        step_resolution: int = self.context.config.hardware.get("step_resolution", 1000)
        motor_pos_ticks: int = pos_rev * step_resolution + pos_step

        return SensorPacket(
            p1_raw=r.PRESSURE_INLET_BAR.scale(raw[r.PRESSURE_INLET_BAR.address]), # Basınç sensörü bu haritada tanımlı değil, geçici eklendi sim. için
            p2_raw=r.PRESSURE_OUTLET_BAR.scale(raw[r.PRESSURE_OUTLET_BAR.address]), # Genişletme gerekirse buraya eklenecek, geçici eklendi sim. için
            temp_k=0.0,              # Sıcaklık sensörü bu haritada tanımlı değil
            motor_pos_ticks=motor_pos_ticks,
            motor_current_ma=signal_ma,
            timestamp=time.monotonic(),
        )

    def _check_status_bits(self, status_word: int) -> None:
        """
        Durum kelimesinin bit'lerini kontrol eder.
        İlgili alarm koşulu varsa broadcaster'a asenkron görev olarak gönderir.

        asyncio.create_task kullanılır — _parse() senkron bir metot olduğundan
        doğrudan await yapılamaz.
        """
        if status_word & StatusBits.SIGNAL_ERROR:
            asyncio.create_task(
                self._publish_alarm(
                    AlarmCode.SENSOR_OUT_OF_RANGE,
                    "Donanım sinyal hatası bildirdi (status_word Bit2)."
                )
            )

        if status_word & StatusBits.MOVING:
            logger.debug("Durum: Vana hareket halinde (status_word Bit1).")

        if status_word & StatusBits.CALIBRATION_DONE:
            logger.debug("Durum: Kalibrasyon tamamlandı (status_word Bit0).")

    # ------------------------------------------------------------------
    # Yardımcı
    # ------------------------------------------------------------------

    async def _publish_alarm(self, code: AlarmCode, reason: str) -> None:
        """Alarm olayını WebSocket broadcaster üzerinden yayınlar."""
        logger.error(f"ALARM [{code.name}]: {reason}")
        await self.context.broadcaster.publish(
            "ALARM_TRIGGERED",
            {"code": int(code), "reason": reason},
        )