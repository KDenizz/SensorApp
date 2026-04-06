"""
controller/states/calibrating_state.py

[Controller Layer - State] Mod 01: Yön/Kalibrasyon ve Sıfır Arama Modu.
Vananın fiziksel %0 ve %100 noktalarını tork (akım) sınırına dayanarak tespit eder.

Migration Notları (threading → asyncio):
    1. `from queue import Empty`              → KALDIRILDI. asyncio.QueueEmpty kullanıldı.
    2. `queue.get_nowait()` + `Empty`         → asyncio.Queue.get_nowait() + asyncio.QueueEmpty
    3. `command_queue.put(...)`               → `await command_queue.put(...)`
    4. `signal_bus.state_changed.emit(...)`   → `await self._publish_state()`
    5. `signal_bus.alarm_triggered.emit(...)`  → `await self._publish_alarm(...)`
    6. `on_enter`, `update`, `on_exit`        → `async def`
    7. `_send_command`, `_send_stop_command`,
       `_trigger_fault`                       → `async def` (await içerdiği için)
    8. `_start_finding_zero`, `_process_*`    → `async def` (async helper çağırdığı için)

Bug Düzeltmesi:
    _process_settling_max içinde `self._trigger_fault(...)` çağrısının dönüş değeri
    yok sayılıyordu → state geçişi hiç gerçekleşmiyordu. Düzeltme: `update()` içinde
    sub-state metodlarının dönüş değerleri artık kontrol edilir ve zincir yukarı taşınır.

Mimari Hatırlatıcı:
    - HAL'e doğrudan erişim YOKTUR.
    - Sensör verisi → context.raw_data_queue
    - Motor komutu → context.command_queue
    - UI bildirimi → context.broadcaster.publish()

Stub Temizliği:
    Dosyanın tepesindeki `class IdleState(BaseState): pass` ve
    `class FaultSafeState(BaseState): pass` stub'ları KALDIRILDI.
    Gerçek sınıflar lazy import (fonksiyon içi) ile yükleniyor — circular import riski sıfır.
"""

from __future__ import annotations

import asyncio
import logging
from enum import Enum, auto
from typing import Optional, TYPE_CHECKING

from controller.states.base_state import BaseState
from core.data_types import MotorCommand, CommandType, AlarmCode, SensorPacket

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class CalibSubState(Enum):
    INIT         = auto()
    FINDING_ZERO = auto()
    SETTLING_ZERO = auto()
    FINDING_MAX  = auto()
    SETTLING_MAX = auto()
    DONE         = auto()


class CalibratingState(BaseState):
    """
    [Controller Layer - State] Mod 01: Kalibrasyon Modu.

    Alt Durum Zinciri:
        INIT → FINDING_ZERO → SETTLING_ZERO → FINDING_MAX → SETTLING_MAX → DONE → IdleState

    Her sub-state, update() tarafından her döngüde bir kez değerlendirilir.
    Geçiş koşulu sağlandığında sub_state güncellenir; bir sonraki iterasyonda
    yeni sub-state devreye girer.
    """

    CALIBRATION_SPEED_PCT: float = 15.0   # Kalibrasyon hareketi hız yüzdesi
    TORQUE_LIMIT_MA: float       = 1500.0 # Mekanik limit tespiti için akım eşiği (mA)
    BLIND_TIME_SEC: float        = 0.5    # Hareket başladıktan sonra torque kontrolü bekleme süresi
    TIMEOUT_SEC: float           = 60.0   # Tüm kalibrasyon için master timeout
    DEBOUNCE_COUNT_LIMIT: int    = 5      # Torque sinyali gürültü filtresi (ardışık okuma sayısı)

    def __init__(self, context: "AppContext") -> None:
        super().__init__(context)
        self.sub_state: CalibSubState = CalibSubState.INIT

        self._total_timer: float = 0.0  # Master timeout sayacı
        self._sub_timer: float   = 0.0  # Settling / blind-time sayacı

        self._torque_hit_count: int = 0
        self._zero_tick: int        = 0
        self._max_tick: int         = 0

    # ------------------------------------------------------------------
    # Yaşam Döngüsü
    # ------------------------------------------------------------------

    async def on_enter(self) -> None:
        logger.info("Kalibrasyon (Mod 01) başlatıldı. Sıfır noktası aranıyor...")
        await self._publish_state()

        # Önceki kalibrasyon verilerini sıfırla
        self.context.is_calibrated    = False
        self.context.zero_tick        = 0
        self.context.max_tick         = 0
        self.context.total_stroke_ticks = 0

    async def update(self, dt: float) -> Optional[BaseState]:
        """
        Kalibrasyon döngüsünün tek iterasyonu.

        Akış:
            1. Timeout kontrolü.
            2. Kuyruktan sensör paketi çek (yoksa beklemeden dön).
            3. Aktif sub-state'e göre ilgili işleyiciyi çağır.
            4. İşleyici bir BaseState döndürürse (FaultSafe veya Idle),
               onu yukarı taşı — MainController geçişi yönetir.
        """
        self._total_timer += dt
        self._sub_timer   += dt

        # --- Global Timeout ---
        if self._total_timer > self.TIMEOUT_SEC:
            logger.error("Kalibrasyon Timeout! (Maksimum süre aşıldı)")
            return await self._trigger_fault(AlarmCode.CALIBRATION_TIMEOUT)

        # --- Sensör Paketi Okuma ---
        try:
            packet: SensorPacket = self.context.raw_data_queue.get_nowait()
        except asyncio.QueueEmpty:
            return None  # Veri yoksa bu iterasyonu pas geç

        current_ma: float    = packet.motor_current_ma
        encoder_ticks: int   = packet.motor_pos_ticks

        # --- Sub-State Yönlendirmesi ---
        # Her işleyici:
        #   - None döner   → aynı sub-state'de kal
        #   - BaseState döner → FaultSafe'e geç (hata durumu)
        # DONE sub-state'i doğrudan burada ele alınır (IdleState geçişi).

        if self.sub_state == CalibSubState.INIT:
            return await self._start_finding_zero()

        elif self.sub_state == CalibSubState.FINDING_ZERO:
            return await self._process_finding_zero(current_ma)

        elif self.sub_state == CalibSubState.SETTLING_ZERO:
            return await self._process_settling_zero(encoder_ticks)

        elif self.sub_state == CalibSubState.FINDING_MAX:
            return await self._process_finding_max(current_ma)

        elif self.sub_state == CalibSubState.SETTLING_MAX:
            return await self._process_settling_max(encoder_ticks)

        elif self.sub_state == CalibSubState.DONE:
            logger.info("Kalibrasyon başarıyla tamamlandı. Bekleme moduna geçiliyor.")
            from controller.states.idle_state import IdleState
            return IdleState(self.context)

        return None

    async def on_exit(self) -> None:
        """
        Her türlü çıkışta (başarılı veya hatalı) motoru durdur.
        """
        await self._send_stop()
        logger.debug("CalibratingState çıkış yaptı.")

    # ------------------------------------------------------------------
    # Sub-State İşleyicileri
    # ------------------------------------------------------------------

    async def _start_finding_zero(self) -> None:
        """INIT → FINDING_ZERO: Motoru kapatma yönünde sabit hızda sürer."""
        cmd = MotorCommand(
            type=CommandType.MOVE_VELOCITY,
            value=self.CALIBRATION_SPEED_PCT,
            direction=-1,
            priority=1,
        )
        await self._send_command(cmd)
        self.sub_state = CalibSubState.FINDING_ZERO
        self._reset_sub_timer()

    async def _process_finding_zero(self, current_ma: float) -> None:
        """FINDING_ZERO: Mekanik sıfır limitine çarpıldığında SETTLING_ZERO'ya geç."""
        if self._check_torque_limit(current_ma):
            logger.info(f"Sıfır noktası mekanik limiti bulundu. (Akım: {current_ma:.1f} mA)")
            await self._send_stop()
            self.sub_state = CalibSubState.SETTLING_ZERO
            self._reset_sub_timer()

    async def _process_settling_zero(self, encoder_ticks: int) -> None:
        """
        SETTLING_ZERO: Motorun tamamen durmasını bekle (blind time),
        ardından encoder değerini sıfır referansı olarak kaydet.
        """
        if self._sub_timer >= self.BLIND_TIME_SEC:
            self._zero_tick = encoder_ticks
            logger.info(f"Sıfır noktası kaydedildi: {self._zero_tick} ticks.")

            cmd = MotorCommand(
                type=CommandType.MOVE_VELOCITY,
                value=self.CALIBRATION_SPEED_PCT,
                direction=1,
                priority=1,
            )
            await self._send_command(cmd)
            self.sub_state = CalibSubState.FINDING_MAX
            self._reset_sub_timer()

    async def _process_finding_max(self, current_ma: float) -> None:
        """FINDING_MAX: Mekanik maksimum limitine çarpıldığında SETTLING_MAX'a geç."""
        if self._check_torque_limit(current_ma):
            logger.info(f"Max noktası mekanik limiti bulundu. (Akım: {current_ma:.1f} mA)")
            await self._send_stop()
            self.sub_state = CalibSubState.SETTLING_MAX
            self._reset_sub_timer()

    async def _process_settling_max(self, encoder_ticks: int) -> Optional[BaseState]:
        """
        SETTLING_MAX: Motorun durmasını bekle, max tick'i kaydet.
        Strok geçerliyse kalibrasyon verilerini context'e yaz ve DONE'a geç.
        Strok geçersizse FaultSafeState döndür.

        Bug Düzeltmesi:
            Eski kodda `self._trigger_fault(...)` çağrısının dönüş değeri
            yok sayılıyordu → FaultSafe'e geçiş hiç gerçekleşmiyordu.
            Artık `return await self._trigger_fault(...)` ile zincir korunuyor.
        """
        if self._sub_timer < self.BLIND_TIME_SEC:
            return None

        self._max_tick = encoder_ticks
        logger.info(f"Max noktası kaydedildi: {self._max_tick} ticks.")

        total_stroke_ticks = abs(self._max_tick - self._zero_tick)

        if total_stroke_ticks < 100:
            logger.error(
                f"Geçersiz kalibrasyon stroku: {total_stroke_ticks} ticks. "
                f"(zero={self._zero_tick}, max={self._max_tick})"
            )
            return await self._trigger_fault(AlarmCode.CALIBRATION_FAILED)

        # Kalibrasyon verilerini context'e işle
        self.context.zero_tick          = self._zero_tick
        self.context.max_tick           = self._max_tick
        self.context.total_stroke_ticks = total_stroke_ticks
        self.context.is_calibrated      = True

        self.sub_state = CalibSubState.DONE
        return None

    # ------------------------------------------------------------------
    # Yardımcı Metodlar
    # ------------------------------------------------------------------

    def _check_torque_limit(self, current_ma: float) -> bool:
        """
        Mekanik limit tespiti için debounce filtreli akım kontrolü.

        Saf hesaplama: I/O yok → senkron kaldı.

        Blind time boyunca False döner (hareket başlar başlamaz yanlış
        tetiklenmeyi önlemek için). Sonrasında DEBOUNCE_COUNT_LIMIT kadar
        ardışık aşım sayılırsa True döner.
        """
        if self._sub_timer < self.BLIND_TIME_SEC:
            return False

        if current_ma >= self.TORQUE_LIMIT_MA:
            self._torque_hit_count += 1
        else:
            self._torque_hit_count = 0  # Gürültü sıfırlama

        return self._torque_hit_count >= self.DEBOUNCE_COUNT_LIMIT

    def _reset_sub_timer(self) -> None:
        """Sub-state geçişlerinde yerel timer ve debounce sayacını sıfırlar."""
        self._sub_timer        = 0.0
        self._torque_hit_count = 0

    async def _send_command(self, cmd: MotorCommand) -> None:
        """Verilen komutu command_queue'ya async olarak ekler."""
        await self.context.command_queue.put((cmd.priority, cmd))

    async def _send_stop(self) -> None:
        """
        Acil durdurma komutunu (priority=0) kuyruğa atar.

        Not: CommandType.STOP kullanıldı (STOP_IMMEDIATE değil).
        Kalibrasyon sırasındaki duruşlar controlled stop'tur;
        acil durum (STOP_IMMEDIATE) yalnızca FaultSafeState tarafından gönderilir.
        """
        stop_cmd = MotorCommand(type=CommandType.STOP, priority=0)
        await self.context.command_queue.put((stop_cmd.priority, stop_cmd))

    async def _trigger_fault(self, code: AlarmCode) -> BaseState:
        """
        Hata durumunda önce motoru durdurur, ardından alarm yayınlar
        ve FaultSafeState döndürür.

        Eski kod:
            self._send_stop_command()                          # senkron
            self.context.signal_bus.alarm_triggered.emit(...)
            return FaultSafeState(self.context)

        Yeni kod:
            await self._send_stop()
            await self._publish_alarm(...)                     # BaseState helper
            return FaultSafeState(self.context, reason=...)
        """
        await self._send_stop()
        await self._publish_alarm(int(code), code.name)

        from controller.states.fault_safe_state import FaultSafeState
        return FaultSafeState(self.context, reason=code.name)