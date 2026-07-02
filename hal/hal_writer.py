"""
hal/hal_writer.py - DÜZELTILMIŞ VERSİYON
Modbus shared client kullanıyor (BUG #1 çözüm)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional


from core.data_types import MotorCommand, CommandType, AlarmCode
from hal.modbus_client import ModbusRTUClient
from hal.modbus_config import Reg, ControlCmd

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class HALWriter:
    """
    [HAL Layer] Donanıma SADECE Modbus Holding Register yazarak komut iletir.

    ✅ DÜZELTME: Artık shared_modbus client (main.py'den) kullanıyor.
       Bağlantı yönetimi main.py tarafından yapıldığından burada
       _connect() ve _disconnect() çağrısı yoktur.

    context.command_queue bir asyncio.PriorityQueue'dur.
    Tuple formatı: (priority_int, MotorCommand)
    Düşük sayı = yüksek öncelik  (0 = EMERGENCY, 1 = Normal)

    Kullanım (main.py içinde):
        writer = HALWriter(context)
        asyncio.create_task(writer.run())
    """

    # Kuyruk boşsa bu kadar bekle
    _QUEUE_TIMEOUT: float = 0.1

    def __init__(self, context: "AppContext") -> None:
        self.context = context

        hw = self.context.config.hardware
        self._port:     str   = hw.get("port",           "COM7")
        self._baudrate: int   = hw.get("baud_rate",      230400)
        self._slave_id: int   = hw.get("slave_id",       1)
        self._timeout:  float = hw.get("modbus_timeout", 0.2)

        self._max_reconnect: int = hw.get("max_reconnect_attempts", 5)
        self._reconnect_attempts: int = 0

        # ✅ DÜZELTME: main.py'den context.modbus_client kullan (shared!)
        self._client: ModbusRTUClient = context.modbus_client

    # ------------------------------------------------------------------
    # Ana Döngü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        asyncio.Task olarak çalışan ana yazma döngüsü.
        
        ✅ DÜZELTME: Artık _connect() çağrısı yok. 
           main.py zaten bağlantıyı kuracak (context.modbus_client).
           Bağlı değilse kuyruktaki komutları işlemez, await asyncio.sleep() ile bekler.
           
        stop_event set edildiğinde kuyruktaki kalan komutları tükettikten sonra kapatır.
        """
        
        logger.info(
            f"HALWriter başlatıldı — port={self._port}, slave={self._slave_id}"
        )

        while not self.context.stop_event.is_set():

            # Bağlantı kopuksa bekle (yeniden bağlanılması main.py tarafından yapılır)
            if not self._client or not self._client.is_connected:
                await asyncio.sleep(0.1)
                continue

            try:
                # Kuyrukta komut bekle
                priority, command = await asyncio.wait_for(
                    self.context.command_queue.get(),
                    timeout=self._QUEUE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # Kuyruk boş — normal durum
                continue
            except Exception as e:
                logger.error(f"HALWriter kuyruk okuma hatası: {e}", exc_info=True)
                continue

            try:
                await self._execute(command)
            except ValueError as e:
                # Aralık dışı / geçersiz parametre — donanıma YAZILMAZ, comms hatası DEĞİL.
                # (to_uint16'nın fırlattığı ValueError BURADA, Exception'dan ÖNCE yakalanmalı.)
                logger.warning(f"HALWriter: geçersiz parametre reddedildi [{command.type.name}]: {e}")
            except Exception as e:
                logger.error(
                    f"HALWriter komut işleme hatası [{command.type.name}]: {e}",
                    exc_info=True,
                )
                await self._publish_alarm(
                    AlarmCode.COMMUNICATION_LOST,
                    f"Komut gönderilemedi [{command.type.name}]: {e}"
                )
            finally:
                self.context.command_queue.task_done()

        logger.info("HALWriter güvenli şekilde sonlandırıldı.")

    # ------------------------------------------------------------------
    # Komut → Register Dönüşümü
    # ------------------------------------------------------------------

    async def _execute(self, command: MotorCommand) -> None:
        """
        Tek bir MotorCommand'ı Modbus Holding Register yazma işlemlerine dönüştürür.
        """

        h = Reg.HOLDING  # Kısaltma

        # ----------------------------------------------------------
        # ACİL DURDURMA — En yüksek öncelik (priority=0)
        # ----------------------------------------------------------
        if command.type == CommandType.STOP_IMMEDIATE:
            logger.warning("HALWriter: ACİL DURDURMA iletiliyor → control_word=0")
            #ok = await self._write(h.CONTROL_WORD, ControlCmd.STOP)
            ok = await self._write(h.MODE_SELECT, 0)

            if not ok:
                raise IOError("ACİL DURDURMA komutu donanıma iletilemedi!")

        # ----------------------------------------------------------
        # KALİBRASYON — Auto-Calibration başlat
        # ----------------------------------------------------------
        elif command.type == CommandType.CALIBRATE:
            logger.info("HALWriter: Kalibrasyon başlatılıyor → control_word=1")
            #ok = await self._write(h.CONTROL_WORD, ControlCmd.AUTO_CALIBRATE)
            ok = await self._write(h.MODE_SELECT, 1)

            if not ok:
                raise IOError("Kalibrasyon komutu donanıma iletilemedi.")

        # ----------------------------------------------------------
        # TAM AÇ / TAM KAPAT (TTL)
        # ----------------------------------------------------------
        elif command.type == CommandType.OPEN_FULL:
            # B-3: Tam Aç artık fiziksel durağa (Mod 4 TTL) GİTMİYOR — over-travel düzeltmesi.
            # %100 = home + total_turns. Cihazdaki total_turns'ü (addr 1 = kalibrasyon
            # referansı) geri okur, mutlak tick hedefini hesaplar, Mod 3 + addr 3'e yazar.
            step_res = int(self.context.config.hardware.get("step_resolution", 1000))
            regs = await self._client.read_holding_registers(
                address=h.TOTAL_TURNS.address, count=1
            )
            if not regs:
                raise IOError("Tam Aç: total_turns okunamadı (cihaz yanıt vermedi).")
            total_turns = int(regs[0])
            if total_turns <= 0:
                # Kalibre değil → %100 referansı yok. Vanayı sessizce kapatmamak için reddet.
                logger.warning("HALWriter: Tam Aç reddedildi — total_turns=0 (önce kalibrasyon gerekli).")
                return
            target_ticks = total_turns * step_res
            if not (0 < target_ticks <= 32767):
                # Pozisyon signed int16 → güvenli aralık dışında hedef yazma.
                logger.warning(f"HALWriter: Tam Aç reddedildi — hedef tick {target_ticks} aralık dışı (0–32767).")
                return
            logger.info(f"HALWriter: Tam Aç → Mod 3, hedef tick={target_ticks} "
                        f"(total_turns={total_turns} × PPR={step_res})")
            # Önce hedef (addr 3), SONRA mod (3): mod aktifleşince hedef hazır olsun.
            if not await self._write(h.TARGET_STEP, target_ticks):
                raise IOError("Tam Aç: hedef tick (addr 3) yazılamadı.")

        
      

        # ----------------------------------------------------------
        # SET_SPEED (Henüz register haritasında yok)
        # ----------------------------------------------------------
        elif command.type == CommandType.SET_SPEED:
            logger.warning(
                f"HALWriter: SET_SPEED komutu henüz desteklenmiyor (value={command.value})"
            )

        # Hedef Tur
        elif command.type == CommandType.SET_TARGET_TURNS:
            step_res = int(self.context.config.hardware.get("step_resolution", 1000))
            turns = int(command.value)
            target_ticks = turns * step_res
            logger.info(f"HALWriter: Hedef tur → {turns} → addr 3 mutlak tick={target_ticks}")
            ok = await self._write(h.TARGET_STEP, target_ticks)
            if not ok:
                raise IOError("Hedef tur (tick) register'a yazılamadı.")

        # Hedef Adım
        elif command.type == CommandType.SET_TARGET_STEP:
            step = int(command.value)
            logger.info(f"HALWriter: Hedef adım → {step} → target_step")
            ok = await self._write(h.TARGET_STEP, step)
            if not ok:
                raise IOError("Hedef adım register'a yazılamadı.")
        
        # ----------------------------------------------------------
        # MUTLAK KONUM (Mod 3) — GOTO kompozit. addr 3 = mutlak toplam tick.
        # Tek komut → mod+hedef sırası garantili (heap eşit öncelikte FIFO garanti etmez),
        # ve addr 1 (TOTAL_TURNS = kalibrasyon referansı) ASLA ezilmez.
        # ----------------------------------------------------------
        elif command.type == CommandType.MOVE_ABSOLUTE:
            target_ticks = int(command.value)
            if not (0 <= target_ticks <= 32767):
                logger.warning(f"HALWriter: GOTO reddedildi — hedef tick {target_ticks} aralık dışı.")
                return
            logger.info(f"HALWriter: GOTO → Mod 3, hedef tick={target_ticks} (addr 3 mutlak)")
            if not await self._write(h.TARGET_STEP, target_ticks):
                raise IOError("GOTO: hedef tick (addr 3) yazılamadı.")


        # MOD SEÇİMİ
        elif command.type == CommandType.SET_MODE:
            mode = int(command.value)
            logger.info(f"HALWriter: Mod seçimi → {mode} → mode_select (addr=0)")
            ok = await self._write(h.MODE_SELECT, mode)
            if not ok:
                raise IOError(f"Mod seçimi donanıma iletilemedi (mod={mode})")
            
        # ----------------------------------------------------------
        # PID KONFİGÜRASYONU (addr 14-18) — to_uint16 aralık kontrolü yapar
        # ----------------------------------------------------------
        elif command.type == CommandType.SET_PID_SETPOINT:
            raw = h.PID_SETPOINT.to_uint16(command.value)   # aralık dışıysa ValueError
            logger.info(f"HALWriter: PID setpoint → {command.value} Bar (raw={raw})")
            ok = await self._write(h.PID_SETPOINT, raw)
            if not ok:
                raise IOError("PID setpoint register'a yazılamadı.")

        elif command.type == CommandType.SET_PID_GAINS:
            kp = h.PID_KP.to_uint16(command.metadata.get("kp", 0.0))
            ki = h.PID_KI.to_uint16(command.metadata.get("ki", 0.0))
            kd = h.PID_KD.to_uint16(command.metadata.get("kd", 0.0))
            logger.info(f"HALWriter: PID kazançları → raw=[{kp}, {ki}, {kd}] (FC16 @ {h.PID_KP.address})")
            ok = await self._write_block(h.PID_KP.address, [kp, ki, kd])  # FC16 atomik
            if not ok:
                raise IOError("PID kazançları register'lara yazılamadı.")

        elif command.type == CommandType.SET_PID_DEADBAND:
            raw = h.PID_DEADBAND.to_uint16(command.value)
            logger.info(f"HALWriter: PID ölü bant → {command.value} Bar (raw={raw})")
            ok = await self._write(h.PID_DEADBAND, raw)
            if not ok:
                raise IOError("PID ölü bant register'a yazılamadı.")

        # ----------------------------------------------------------
        # SENSÖR (ADC) KALİBRASYONU (addr 19-20)
        # ----------------------------------------------------------
        elif command.type == CommandType.SET_ADC_OFFSET:
            raw = h.ADC_OFFSET.to_uint16(command.value)     # signed two's complement
            logger.info(f"HALWriter: ADC ofset → {command.value} Bar (raw={raw})")
            ok = await self._write(h.ADC_OFFSET, raw)
            if not ok:
                raise IOError("ADC ofset register'a yazılamadı.")

        elif command.type == CommandType.SET_ADC_GAIN:
            raw = h.ADC_GAIN.to_uint16(command.value)
            logger.info(f"HALWriter: ADC gain → {command.value} (raw={raw})")
            ok = await self._write(h.ADC_GAIN, raw)
            if not ok:
                raise IOError("ADC gain register'a yazılamadı.")            


        elif command.type == CommandType.STOP:
            logger.info("HALWriter: Yumuşak durdurma → control_word=0")
            #ok = await self._write(h.CONTROL_WORD, ControlCmd.STOP)
            ok = await self._write(h.MODE_SELECT, 0)

            if not ok:
                raise IOError("Durdurma komutu donanıma iletilemedi.")

        # ----------------------------------------------------------
        # KALİBRASYON BAŞLAT (kompozit): config'i SIRAYLA yaz, EN SON Mod 1.
        # Tek komut → sıra garantili (heap eşit öncelikte FIFO garanti etmez).
        # %100 referansı = home + total_turns (firmware total_turns kullanır).
        # ----------------------------------------------------------
        elif command.type == CommandType.START_CALIBRATION:
            m = command.metadata
            seq = [
                (h.CALIBRATION_DIRECTION, h.CALIBRATION_DIRECTION.to_uint16(m.get("calib_dir", 0))),
                (h.SEATING_LOAD,          h.SEATING_LOAD.to_uint16(m.get("seating_load", 0))),
                (h.BACKOFF_OFFSET,        h.BACKOFF_OFFSET.to_uint16(m.get("backoff_offset", 0))),
                (h.TOTAL_TURNS,           h.TOTAL_TURNS.to_uint16(m.get("total_turns", 0))),
            ]
            for reg, val in seq:
                if not await self._write(reg, val):
                    raise IOError(f"Kalibrasyon config yazılamadı: {reg.name}")
            # Parametreler hazır → kalibrasyonu tetikle (Mod 1) EN SON
            if not await self._write(h.MODE_SELECT, 1):
                raise IOError("Kalibrasyon modu (1) yazılamadı.")
            logger.info(f"HALWriter: KALİBRASYON BAŞLATILDI — dir={m.get('calib_dir')}, "
                        f"seating={m.get('seating_load')}, backoff={m.get('backoff_offset')}, "
                        f"total_turns={m.get('total_turns')}")

        elif command.type == CommandType.CLOSE_FULL:
            # OPEN_FULL simetrisi: Mod 3 + TARGET_STEP=0 (home).
            # total_turns=0 ise kalibre değil → sessiz reddet.
            regs = await self._client.read_holding_registers(
                address=h.TOTAL_TURNS.address, count=1
            )
            if not regs or int(regs[0]) <= 0:
                logger.warning("HALWriter: Tam Kapat reddedildi — total_turns=0 (önce kalibrasyon gerekli).")
                return
            logger.info("HALWriter: Tam Kapat → Mod 3, hedef tick=0 (home)")
            if not await self._write(h.TARGET_STEP, 0):
                raise IOError("Tam Kapat: hedef tick (addr 3) yazılamadı.")


        else:
            logger.warning(f"HALWriter: Bilinmeyen komut tipi: {command.type}")


    async def _write(self, register_def, value: int) -> bool:
        """
        Tek bir register'a değer yazar.

        :param register_def: RegisterDef nesnesi (Reg.HOLDING.*)
        :param value: UInt16 değeri
        :return: True başarılı, False başarısız
        """
        if not self._client or not self._client.is_connected:
            logger.error("Modbus yazma: İstemci bağlı değil.")
            return False

        try:
            result = await self._client.write_register(
                address=register_def.address,
                value=value
            )
            if result:
                logger.debug(
                    f"Modbus yazma başarılı: {register_def.name} "
                    f"(addr={register_def.address}) ← {value}"
                )
            else:
                logger.warning(
                    f"Modbus yazma başarısız: {register_def.name} "
                    f"(addr={register_def.address})"
                )
            return result
        except Exception as e:
            logger.error(f"Modbus yazma hatası: {e}", exc_info=True)
            return False

    async def _write_block(self, address: int, values: list[int]) -> bool:
        """Ardışık register'lara FC16 ile atomik yazar (örn. Kp/Ki/Kd)."""
        if not self._client or not self._client.is_connected:
            logger.error("Modbus blok yazma: İstemci bağlı değil.")
            return False
        try:
            result = await self._client.write_registers(address=address, values=values)
            if result:
                logger.debug(f"Modbus blok yazma başarılı: addr={address} ← {values}")
            else:
                logger.warning(f"Modbus blok yazma başarısız: addr={address}")
            return result
        except Exception as e:
            logger.error(f"Modbus blok yazma hatası: {e}", exc_info=True)
            return False

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