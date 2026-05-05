"""
hal/hal_writer.py - DÜZELTILMIŞ VERSİYON
Modbus shared client kullanıyor (BUG #1 çözüm)
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

from click import command

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
    _QUEUE_TIMEOUT: float = 0.01

    def __init__(self, context: "AppContext") -> None:
        self.context = context

        hw = self.context.config.hardware
        self._port:     str   = hw.get("port",           "COM7")
        self._baudrate: int   = hw.get("baud_rate",      115200)
        self._slave_id: int   = hw.get("slave_id",       1)
        self._timeout:  float = hw.get("modbus_timeout", 0.1)

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
                # Hata olsa bile kuyruğu serbest bırak
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
            logger.info("HALWriter: Tam Aç (TTL) → control_word=2")
            #ok = await self._write(h.CONTROL_WORD, ControlCmd.OPEN_FULL)
            ok = await self._write(h.FAST_OPEN_CLOSE, 1)

            if not ok:
                raise IOError("Tam Aç komutu donanıma iletilemedi.")

        elif command.type == CommandType.CLOSE_FULL:
            logger.info("HALWriter: Tam Kapat (TTL) → control_word=3")
            #ok = await self._write(h.CONTROL_WORD, ControlCmd.CLOSE_FULL)
            ok = await self._write(h.FAST_OPEN_CLOSE, 0)
            
            if not ok:
                raise IOError("Tam Kapat komutu donanıma iletilemedi.")
        
            """      
        # ----------------------------------------------------------
        # MUTLAK KONUM — Dijital Adım Modu (Mod 3)
        # ----------------------------------------------------------
        elif command.type == CommandType.MOVE_ABSOLUTE:
            # Mod 3: Toplam tick değerinden tur ve adımı hesapla
            step_resolution: int = self.context.config.hardware.get("step_resolution", 1000)
            total_ticks = int(command.value)
            target_rev  = total_ticks // step_resolution
            target_step = total_ticks % step_resolution

            logger.info(f"HALWriter: MOVE_ABSOLUTE → rev={target_rev}, step={target_step}")

            ok_rev  = await self._write(h.TARGET_REVOLUTIONS, target_rev)
            ok_step = await self._write(h.TARGET_STEP, target_step)

            if not (ok_rev and ok_step):
                raise IOError("Mutlak konum register'lara yazılamadı.")
            """


        # ----------------------------------------------------------
        # SET_SPEED (Henüz register haritasında yok)
        # ----------------------------------------------------------
        elif command.type == CommandType.SET_SPEED:
            logger.warning(
                f"HALWriter: SET_SPEED komutu henüz desteklenmiyor (value={command.value})"
            )

        # Hedef Tur
        elif command.type == CommandType.SET_TARGET_TURNS:
                turns = int(command.value)
                logger.info(f"HALWriter: Hedef tur → {turns} → target_revolutions")
                #ok = await self._write(h.TARGET_REVOLUTIONS, turns)
                ok = await self._write(h.TOTAL_TURNS, turns)

                if not ok:
                    raise IOError("Hedef tur register'a yazılamadı.")

        # Hedef Adım
        elif command.type == CommandType.SET_TARGET_STEP:
            step = int(command.value)
            logger.info(f"HALWriter: Hedef adım → {step} → target_step")
            ok = await self._write(h.TARGET_STEP, step)
            if not ok:
                raise IOError("Hedef adım register'a yazılamadı.")

        # MOD SEÇİMİ
        elif command.type == CommandType.SET_MODE:
            mode = int(command.value)
            logger.info(f"HALWriter: Mod seçimi → {mode} → mode_select (addr=0)")
            ok = await self._write(h.MODE_SELECT, mode)
            if not ok:
                raise IOError(f"Mod seçimi donanıma iletilemedi (mod={mode})")


        elif command.type == CommandType.STOP:
            logger.info("HALWriter: Yumuşak durdurma → control_word=0")
            #ok = await self._write(h.CONTROL_WORD, ControlCmd.STOP)
            ok = await self._write(h.MODE_SELECT, 0)

            if not ok:
                raise IOError("Durdurma komutu donanıma iletilemedi.")

        
            """
            # ----------------------------------------------------------
        # SET_TORQUE
        # ----------------------------------------------------------
        elif command.type == CommandType.SET_TORQUE:
            if command.direction >= 0:
                ok = await self._write(h.OPENING_KICK_TORQUE, int(command.value))
            else:
                ok = await self._write(h.CLOSING_TORQUE_LIMIT, int(command.value))
            if not ok:
                raise IOError("Tork komutu donanıma iletilemedi.")

        else:
            logger.warning(f"HALWriter: Bilinmeyen komut tipi: {command.type}")
            
            """
        


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