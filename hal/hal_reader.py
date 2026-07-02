"""
hal/hal_reader.py - DÜZELTILMIŞ VERSİYON
Modbus shared client kullanıyor (BUG #1 çözüm)
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

    ✅ DÜZELTME: Artık shared_modbus client (main.py'den) kullanıyor.
       Bağlantı yönetimi main.py tarafından yapıldığından burada
       _connect() ve _disconnect() çağrısı yoktur.

    Kullanım (main.py içinde):
        reader = HALReader(context)
        asyncio.create_task(reader.run())
    """

    def __init__(self, context: "AppContext") -> None:
        self.context = context

        hw = self.context.config.hardware
        self._port:      str   = hw.get("port",           "COM1")
        self._baudrate:  int   = hw.get("baud_rate",      230400)
        self._slave_id:  int   = hw.get("slave_id",       1)
        self._timeout:   float = hw.get("modbus_timeout", 0.2)

        rate = hw.get("sample_rate_hz", 50)
        if rate <= 0:
            logger.warning(f"Geçersiz sample_rate_hz={rate}, varsayılan 20 Hz kullanılıyor.")
            rate = 20
        self._sample_rate_hz: int   = rate
        self._loop_delay:     float = 1.0 / self._sample_rate_hz

        self._max_reconnect: int = hw.get("max_reconnect_attempts", 5)
        self._reconnect_attempts: int = 0

        # Her polling döngüsünde aynı (address, count) kullanılır — bir kez hesapla.
        #self._read_addr, self._read_count = Reg.INPUT.block()

        # ✅ DÜZELTME: main.py'den context.modbus_client kullan (shared!)
        self._client: ModbusRTUClient = context.modbus_client

        # Ardışık okuma hatası sayacı — geçici gürültüyü alarm'dan ayırt eder.
        self._consecutive_errors: int = 0
        self._max_consecutive_errors: int = hw.get("max_consecutive_errors", 10)       
        # Basınç register'ları (22-23) OPSİYONEL. Firmware desteklemiyorsa
        # ana telemetriyi/bağlantıyı bozmadan otomatik devre dışı kalır.
        self._pressure_enabled: bool = hw.get("pressure_enabled", True)
        self._pressure_errors: int = 0
        self._max_pressure_errors: int = hw.get("max_pressure_errors", 5)
        self._comms_alarm_active: bool = False

    # ------------------------------------------------------------------
    # Ana Döngü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        asyncio.Task olarak çalışan ana okuma döngüsü.
        
        ✅ DÜZELTME: Artık _connect() çağrısı yok. 
           main.py zaten bağlantıyı kuracak (context.modbus_client).
           Bağlı değilse await asyncio.sleep() ile retry yapar.
           
        stop_event set edildiğinde döngüden çıkar.
        """
        
        logger.info(
            f"HALReader başlatıldı — {self._sample_rate_hz} Hz, "
            f"port={self._port}, slave={self._slave_id}"
        )

        while not self.context.stop_event.is_set():
            loop_start = time.monotonic()

            # Modbus client'ın bağlı olup olmadığını kontrol et
            if not self._client or not self._client.is_connected:
                await asyncio.sleep(0.1)  # Bağlantı kurulana kadar bekle
                continue

            try:
                await self._poll_registers()
            except Exception as e:
                logger.error(f"HALReader polling döngüsünde beklenmeyen hata: {e}", exc_info=True)
                await asyncio.sleep(0.5)
                continue

            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, self._loop_delay - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        logger.info("HALReader güvenli şekilde sonlandırıldı.")

    # ------------------------------------------------------------------
    # Register Polling ve Parse
    # ------------------------------------------------------------------
 
    #sync def _poll_registers(self) -> None:
    #   if not self._client or not self._client.is_connected:
    #       raise ConnectionError("Modbus istemcisi bağlı değil.")

    #   # FC03 ile Holding Register oku — cihaz adres 0-23 destekliyor (0-20 kontrol + 22-23 basınç)
    #   raw = await self._client.read_holding_registers(
    #       address=0,
    #       count=24,
    #   )

    #   if raw is None or len(raw) < 21:  # minimum 21 şart, 24 olursa bonus
    #       self._consecutive_errors += 1
    #       logger.warning(f"Holding Register okunamadı ({self._consecutive_errors}/{self._max_consecutive_errors})")
    #       if self._consecutive_errors >= self._max_consecutive_errors:
    #           await self._publish_alarm(AlarmCode.COMMUNICATION_LOST, f"{self._consecutive_errors} ardışık hata.")
    #           raise ConnectionError("Ardışık hata eşiği aşıldı.")
    #       return

    #   self._consecutive_errors = 0

    #   #if len(raw) < 24:
    #   #    logger.error(f"Eksik veri: beklenen=21, gelen={len(raw)}")
    #   #    return

    #   packet = self._parse(raw)
    #   if packet is None:
    #       return

    #   await self.context.raw_data_queue.put(packet)
    #   await self.context.broadcaster.publish("SENSOR_DATA", packet)

    async def _poll_registers(self) -> None:
        if not self._client or not self._client.is_connected:
            raise ConnectionError("Modbus istemcisi bağlı değil.")

        # YENİ
        # TEK okuma: addr 0-22 (23 register) — C# referans aracıyla birebir aynı
        # (master.ReadHoldingRegisters(slaveId, 0, 23)). Basınç (21-22) dahil her şey
        # tek FC03 isteğinde gelir → 50 Hz'de saniyede 100 işlem yerine 20 Hz'de 20.
        count = 23 if self._pressure_enabled else 21
        raw = await self._client.read_holding_registers(address=0, count=count)

        # Basınç register'ı olmayan ESKİ firmware'de 23'lük okuma patlarsa bir kez 21'e
        # düş ve basıncı kalıcı devre dışı bırak — comms'u bozmadan zarif geri çekilme.
        if (raw is None or len(raw) < count) and self._pressure_enabled and count == 23:
            # Genel iletişim hatası mı, yoksa basınç-spesifik mi ayırt et.
            # Eğer aynı anda genel consecutive_errors de artıyorsa bu geçici Modbus
            # problemi — basınç sayacını artırma, sadece 21'le yeniden dene.
            if raw is None:
                # Tamamen yanıt yok → genel iletişim hatası, basınç sayacına dokunma
                logger.debug("Basınç bloğu atlandı — genel Modbus timeout (basınç sayacı değişmedi).")
            else:
                # 21 register geldi ama 23 beklendi → firmware basıncı desteklemiyor olabilir
                self._pressure_errors += 1
                if self._pressure_errors >= self._max_pressure_errors:
                    self._pressure_enabled = False
                    self._pressure_errors = 0
                    logger.warning(
                        f"{self._max_pressure_errors} ardışık basınç okuma hatası → "
                        "basınç kalıcı devre dışı (eski firmware?). 21 register ile devam."
                    )
                else:
                    logger.debug(
                        f"Basınç register eksik {self._pressure_errors}/{self._max_pressure_errors} "
                        "— geçici, ana telemetri etkilenmedi."
                    )
            raw = await self._client.read_holding_registers(address=0, count=21)
        else:
            # Başarılı 23'lük okumada basınç hata sayacını sıfırla
            if self._pressure_enabled:
                self._pressure_errors = 0

        if raw is None or len(raw) < 21:
            self._consecutive_errors += 1
            logger.warning(
                f"Holding Register okunamadı "
                f"({self._consecutive_errors}/{self._max_consecutive_errors})"
            )
            if self._consecutive_errors >= self._max_consecutive_errors:
                # Alarm henüz aktif değilse bir kez yayınla — spam önlemi
                if not self._comms_alarm_active:
                    self._comms_alarm_active = True
                    await self._publish_alarm(
                        AlarmCode.COMMUNICATION_LOST,
                        f"{self._consecutive_errors} ardışık Modbus hatası — bağlantı koptu."
                    )
                raise ConnectionError("Ardışık hata eşiği aşıldı.")
            return

        # Başarılı okuma — hata sayacını ve alarm flag'ini sıfırla
        if self._consecutive_errors > 0:
            logger.info("Modbus bağlantısı yeniden sağlandı.")
        if self._comms_alarm_active:
            self._comms_alarm_active = False
            # Bağlantı geri döndü bildirimini frontend'e ilet
            await self.context.broadcaster.publish(
                "STATE_CHANGED", {"state": "RUNNING"}
            )
        self._consecutive_errors = 0

        packet = self._parse(raw)

        if packet is None:
            return

        await self.context.raw_data_queue.put(packet)
        await self.context.broadcaster.publish("SENSOR_DATA", packet)
    
    def _parse(self, raw: list[int]) -> Optional[SensorPacket]:

        h = Reg.HOLDING

        # Konum & yük — signed dönüşüm artık RegisterDef.from_uint16 ile (elle çevirme YOK)
        position = int(h.CURRENT_POSITION.from_uint16(raw[h.CURRENT_POSITION.address]))
        load     = int(h.CURRENT_LOAD.from_uint16(raw[h.CURRENT_LOAD.address]))
        calib    = int(raw[h.CALIBRATION_STATUS.address])

        step_resolution = self.context.config.hardware.get("step_resolution", 1000)
        turns = position // step_resolution
        steps = position % step_resolution

        return SensorPacket(

            p1_raw=(raw[21] / 1000.0) if len(raw) > 21 else 0.0,
            p2_raw=(raw[22] / 1000.0) if len(raw) > 22 else 0.0,
            temp_k=0.0,

            motor_pos_ticks=position,
            motor_turns=turns,
            motor_steps=steps,
            motor_torque_pct=float(abs(load)),
            motor_current_ma=float(load),
            calibration_status=calib,
            timestamp=time.monotonic(),
            # --- Cihaz register geri-okuması ---
            mode_select=int(raw[h.MODE_SELECT.address]),
            total_turns=int(raw[h.TOTAL_TURNS.address]),
            signal_lost_flag=int(raw[h.SIGNAL_LOST_FLAG.address]),
            signal_loss_action=int(raw[h.SIGNAL_LOSS_ACTION.address]),
            seating_load=int(raw[h.SEATING_LOAD.address]),
            backoff_offset=int(raw[h.BACKOFF_OFFSET.address]),
            pid_setpoint=h.PID_SETPOINT.from_uint16(raw[h.PID_SETPOINT.address]),
            pid_kp=h.PID_KP.from_uint16(raw[h.PID_KP.address]),
            pid_ki=h.PID_KI.from_uint16(raw[h.PID_KI.address]),
            pid_kd=h.PID_KD.from_uint16(raw[h.PID_KD.address]),
            pid_deadband=h.PID_DEADBAND.from_uint16(raw[h.PID_DEADBAND.address]),
            adc_offset=h.ADC_OFFSET.from_uint16(raw[h.ADC_OFFSET.address]),
            adc_gain=h.ADC_GAIN.from_uint16(raw[h.ADC_GAIN.address]),
        )

    """
    def _check_status_bits(self, status_word: int) -> None:
        #Status word bit kontrolü
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

    """

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