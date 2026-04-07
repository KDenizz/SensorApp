"""
hal/hal_writer.py

[HAL Layer] Komut kuyruğundan MotorCommand'ları asenkron olarak tüketen,
Modbus RTU Holding Register'larına yazarak donanıma ileten görev (asyncio.Task).

Mock tamamen kaldırıldı. Artık:
    - ModbusRTUClient ile Holding Register (4x) yazılır.
    - Her CommandType, register haritasındaki karşılığına (Reg.HOLDING.*) dönüştürülür.
    - STOP_IMMEDIATE her zaman kuyruğun önünde işlenir (priority=0).
    - Yazma başarısız olursa COMMUNICATION_LOST alarmı üretilir.

Mimari Kural:
    - Bu katman Controller veya State'e doğrudan DOKUNMAZ.
    - Komut yalnızca context.command_queue'dan okunur.
    - UI bildirimi yalnızca context.broadcaster.publish() ile yapılır.
    - HALReader ile AYRI bir ModbusRTUClient kullanılır.
      (pymodbus async client thread-safe değildir; paylaşım yasaktır.)

MotorCommand → Modbus Register Eşlemesi:
    STOP_IMMEDIATE  → control_word  = ControlCmd.STOP        (40002 = 0)
    CALIBRATE       → control_word  = ControlCmd.AUTO_CAL    (40002 = 1)
    OPEN_FULL       → control_word  = ControlCmd.OPEN_FULL   (40002 = 2)
    CLOSE_FULL      → control_word  = ControlCmd.CLOSE_FULL  (40002 = 3)
    MOVE_ABSOLUTE   → target_rev + target_step               (40003, 40004)
    SET_SPEED       → (Henüz register haritasında yok — loglanır, atlanır)
    SET_TORQUE      → closing_torque_limit veya
                      opening_kick_torque                    (40019, 40020)
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

    context.command_queue bir asyncio.PriorityQueue'dur.
    Tuple formatı: (priority_int, MotorCommand)
    Düşük sayı = yüksek öncelik  (0 = EMERGENCY, 1 = Normal)

    Kullanım (main.py içinde):
        writer = HALWriter(context)
        asyncio.create_task(writer.run())
    """

    # Kuyruk boşsa bu kadar bekle. Çok küçük → CPU spin, çok büyük → komut gecikmesi.
    _QUEUE_TIMEOUT: float = 0.01

    def __init__(self, context: "AppContext") -> None:
        self.context = context

        hw = self.context.config.hardware
        self._port:     str   = hw.get("port",           "COM1")
        self._baudrate: int   = hw.get("baud_rate",      115200)
        self._slave_id: int   = hw.get("slave_id",       1)
        self._timeout:  float = hw.get("modbus_timeout", 0.1)

        self._max_reconnect: int = hw.get("max_reconnect_attempts", 5)
        self._reconnect_attempts: int = 0

        self._client: Optional[ModbusRTUClient] = None

    # ------------------------------------------------------------------
    # Ana Döngü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        asyncio.Task olarak çalışan ana yazma döngüsü.
        stop_event set edildiğinde kuyruktaki kalan komutları tükettikten sonra kapatır.
        """
        connected = await self._connect()
        if not connected:
            await self._publish_alarm(
                AlarmCode.COMMUNICATION_LOST,
                f"HALWriter başlangıç bağlantısı başarısız: {self._port}"
            )
            return

        logger.info(
            f"HALWriter başlatıldı — port={self._port}, slave={self._slave_id}"
        )

        while not self.context.stop_event.is_set():

            # Bağlantı kopuksa önce yeniden bağlan
            if not self._client or not self._client.is_connected:
                recovered = await self._handle_connection_loss()
                if not recovered:
                    break
                continue

            try:
                # Kuyrukta komut bekle; timeout ile stop_event periyodik kontrol edilir
                priority, command = await asyncio.wait_for(
                    self.context.command_queue.get(),
                    timeout=self._QUEUE_TIMEOUT,
                )
            except asyncio.TimeoutError:
                # Kuyruk boş — normal durum, döngü başa döner
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

        await self._disconnect()
        logger.info("HALWriter güvenli şekilde sonlandırıldı.")

    # ------------------------------------------------------------------
    # Bağlantı Yönetimi
    # ------------------------------------------------------------------

    async def _connect(self) -> bool:
        """
        Yeni bir ModbusRTUClient oluşturur ve bağlantı kurar.
        Her deneme arasında 2 saniye bekler.
        """
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
                return True

            logger.warning(
                f"HALWriter bağlantı denemesi {attempt}/{self._max_reconnect} başarısız."
            )
            await asyncio.sleep(2.0)

        logger.critical(f"HALWriter: {self._max_reconnect} denemede bağlanılamadı.")
        return False

    async def _handle_connection_loss(self) -> bool:
        """
        Bağlantı kaybında kademeli yeniden bağlanma.
        Maksimum deneme aşılırsa sistemi kapatır.
        """
        self._reconnect_attempts += 1

        if self._reconnect_attempts >= self._max_reconnect:
            logger.critical(
                f"HALWriter: Maksimum yeniden bağlanma ({self._max_reconnect}) aşıldı. "
                "Sistem kapatılıyor."
            )
            await self.context.request_shutdown()
            return False

        logger.warning(
            f"HALWriter bağlantısı koptu — "
            f"yeniden bağlanılıyor ({self._reconnect_attempts}/{self._max_reconnect})..."
        )

        if self._client:
            await self._client.disconnect()

        return await self._connect()

    async def _disconnect(self) -> None:
        """Modbus bağlantısını güvenle kapatır."""
        if self._client:
            await self._client.disconnect()
            self._client = None

    # ------------------------------------------------------------------
    # Komut → Register Dönüşümü
    # ------------------------------------------------------------------

    async def _execute(self, command: MotorCommand) -> None:
        """
        Tek bir MotorCommand'ı Modbus Holding Register yazma işlemlerine dönüştürür.

        Her komut tipi için hangi register'a ne yazıldığı açıkça belirtilmiştir.
        Yazma başarısız olursa (False dönerse) exception fırlatılır — üst katman yakalar.
        """

        h = Reg.HOLDING  # Kısaltma

        # ----------------------------------------------------------
        # ACİL DURDURMA — En yüksek öncelik (priority=0)
        # control_word = 0  →  40002 = STOP
        # ----------------------------------------------------------
        if command.type == CommandType.STOP_IMMEDIATE:
            logger.warning("HALWriter: ACİL DURDURMA iletiliyor → control_word=0")
            ok = await self._write(h.CONTROL_WORD, ControlCmd.STOP)
            if not ok:
                raise IOError("ACİL DURDURMA komutu donanıma iletilemedi!")

        # ----------------------------------------------------------
        # KALİBRASYON — Auto-Calibration başlat
        # control_word = 1  →  40002 = AUTO_CALIBRATE
        # ----------------------------------------------------------
        elif command.type == CommandType.CALIBRATE:
            logger.info("HALWriter: Kalibrasyon başlatılıyor → control_word=1")
            ok = await self._write(h.CONTROL_WORD, ControlCmd.AUTO_CALIBRATE)
            if not ok:
                raise IOError("Kalibrasyon komutu donanıma iletilemedi.")

        # ----------------------------------------------------------
        # TAM AÇ / TAM KAPAT (TTL)
        # command.direction:  1 → Aç (control_word=2)
        #                    -1 → Kapat (control_word=3)
        # ----------------------------------------------------------
        elif command.type == CommandType.STOP:
            # STOP komutu yön bilgisine göre tam aç veya tam kapat olarak yorumlanır
            if command.direction >= 0:
                logger.info("HALWriter: Tam Aç (TTL) → control_word=2")
                ok = await self._write(h.CONTROL_WORD, ControlCmd.OPEN_FULL)
            else:
                logger.info("HALWriter: Tam Kapat (TTL) → control_word=3")
                ok = await self._write(h.CONTROL_WORD, ControlCmd.CLOSE_FULL)
            if not ok:
                raise IOError("Tam Aç/Kapat komutu donanıma iletilemedi.")

        # ----------------------------------------------------------
        # MUTLAK KONUM — Dijital Adım Modu (Mod 3)
        # command.value → toplam tick (tur * step_resolution + adım)
        # Tur ve adım ayrıştırılarak ayrı register'lara yazılır.
        # ----------------------------------------------------------
        elif command.type == CommandType.MOVE_ABSOLUTE:
            step_resolution: int = self.context.config.hardware.get(
                "step_resolution", 1000
            )
            total_ticks = int(command.value)
            target_rev  = total_ticks // step_resolution
            target_step = total_ticks  % step_resolution

            logger.debug(
                f"HALWriter: MOVE_ABSOLUTE → "
                f"ticks={total_ticks}, tur={target_rev}, adım={target_step}"
            )

            # Önce hedef tur, sonra hedef adım — sıra önemli
            ok_rev  = await self._write(h.TARGET_REVOLUTIONS, target_rev)
            ok_step = await self._write(h.TARGET_STEP, target_step)

            if not ok_rev or not ok_step:
                raise IOError(
                    f"MOVE_ABSOLUTE yazma hatası "
                    f"(tur_ok={ok_rev}, adım_ok={ok_step})"
                )

        # ----------------------------------------------------------
        # GÖRECELI HAREKET — Şu an desteklenmiyor
        # Slave tarafında göreceli hareket register'ı yok.
        # Controller katmanı mutlak konuma çevirerek MOVE_ABSOLUTE göndermelidir.
        # ----------------------------------------------------------
        elif command.type == CommandType.MOVE_RELATIVE:
            logger.warning(
                "HALWriter: MOVE_RELATIVE bu donanım haritasında desteklenmiyor. "
                "Controller katmanı bunu MOVE_ABSOLUTE'a çevirmelidir."
            )

        # ----------------------------------------------------------
        # HIZ SINIRI — Register haritasında henüz tanımlı değil
        # ----------------------------------------------------------
        elif command.type == CommandType.SET_SPEED:
            logger.warning(
                f"HALWriter: SET_SPEED (değer={command.value}) için "
                "register haritasında tanımlı adres yok — atlandı."
            )

        # ----------------------------------------------------------
        # TORK LİMİTİ
        # command.direction:  1 → Açılış kalkış torku  (40020)
        #                    -1 → Kapanış tork sınırı  (40019)
        # command.value → % değer (0–100)
        # ----------------------------------------------------------
        elif command.type == CommandType.SET_TORQUE:
            torque_pct = int(command.value)
            if command.direction >= 0:
                logger.debug(
                    f"HALWriter: SET_TORQUE (Açılış Kalkış) → "
                    f"opening_kick_torque={torque_pct}%"
                )
                ok = await self._write(h.OPENING_KICK_TORQUE, torque_pct)
            else:
                logger.debug(
                    f"HALWriter: SET_TORQUE (Kapanış Sınırı) → "
                    f"closing_torque_limit={torque_pct}%"
                )
                ok = await self._write(h.CLOSING_TORQUE_LIMIT, torque_pct)

            if not ok:
                raise IOError(f"SET_TORQUE komutu donanıma iletilemedi (yön={command.direction}).")

        # ----------------------------------------------------------
        # Bilinmeyen komut tipi
        # ----------------------------------------------------------
        else:
            logger.warning(f"HALWriter: Bilinmeyen komut tipi → {command.type}")

    # ------------------------------------------------------------------
    # Yazma Yardımcısı
    # ------------------------------------------------------------------

    async def _write(self, reg: "RegisterDef", value: int) -> bool:  # type: ignore[name-defined]
        """
        Tek bir Holding Register'a değer yazar.
        Başarı/başarısızlığı loglar ve bool döndürür.

        :param reg:   Hedef RegisterDef (Reg.HOLDING.* üzerinden gelinir)
        :param value: Yazılacak ham UInt16 değer
        """
        ok = await self._client.write_register(
            address=reg.address,
            value=value,
        )
        if ok:
            logger.debug(
                f"Yazma OK  → {reg.name} "
                f"(pyaddr={reg.address}, modbus={40001 + reg.address}) = {value}"
            )
        else:
            logger.error(
                f"Yazma FAIL → {reg.name} "
                f"(pyaddr={reg.address}, modbus={40001 + reg.address}) = {value}"
            )
        return ok

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