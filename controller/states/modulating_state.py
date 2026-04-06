"""
controller/states/modulating_state.py

[Controller Layer - State] Mod 02 & 03: Kapalı Çevrim Oransal Kontrol Modu.

Migration Notları (threading → asyncio):
    1. `from queue import Empty`          → KALDIRILDI. asyncio.Queue.get_nowait()
                                            QueueEmpty (asyncio modülünden) fırlatır.
    2. `queue.get_nowait()` + `Empty`     → asyncio.Queue.get_nowait() + asyncio.QueueEmpty
    3. `command_queue.put(...)`           → `await command_queue.put(...)`
    4. `signal_bus.state_changed.emit(…)` → `await self._publish_state()`      [BaseState helper]
    5. `signal_bus.alarm_triggered.emit(…)`→ `await self._publish_alarm(...)`   [BaseState helper]
    6. `on_enter`, `update`, `on_exit`,
       `handle_event`, `_trigger_fault`   → hepsi `async def` oldu.
    7. `_calculate_pv`                    → saf hesaplama, I/O yok → senkron kaldı. (doğru)

Mimari Kural Hatırlatıcısı:
    - Bu katman HAL'e doğrudan DOKUNMAZ.
    - Sensör verisi yalnızca context.raw_data_queue üzerinden gelir.
    - Motor komutu yalnızca context.command_queue üzerinden gider.
    - UI bildirimi yalnızca context.broadcaster.publish() üzerinden yapılır.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Dict, Any, TYPE_CHECKING

from controller.states.base_state import BaseState
from core.data_types import MotorCommand, CommandType, AlarmCode, ControlMode, SensorPacket

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class ModulatingState(BaseState):
    """
    [Controller Layer - State] Kapalı Çevrim Oransal Kontrol Modu.

    Kullanıcının seçtiği Setpoint'e (Basınç, Pozisyon veya Debi) ulaşmak için
    PID algoritmasını çalıştırır ve aktüatöre sürekli pozisyon komutu gönderir.

    Döngü Akışı (MainController.run her iterasyonda await state.update(dt) çağırır):
        1. raw_data_queue'dan get_nowait() ile paket çekmeye çalış.
        2. Paket yoksa → _missing_data_cycles sayacını artır, tolerans aşıldıysa FaultSafe'e geç.
        3. Paket varsa → PV hesapla → PID çalıştır → MOVE_ABSOLUTE komutunu kuyruğa at.
    """

    # 200 Hz döngüde 10 cycle ≈ 50 ms sensör kaybı toleransı.
    _MAX_MISSING_CYCLES: int = 10

    def __init__(self, context: "AppContext") -> None:
        super().__init__(context)
        self._missing_data_cycles: int = 0

    # ------------------------------------------------------------------
    # Yaşam Döngüsü
    # ------------------------------------------------------------------

    async def on_enter(self) -> None:
        """
        Modülasyon moduna girildiğinde bir kez çağrılır.

        - UI'ya STATE_CHANGED bildirimi gönderilir.
        - Bumpless Transfer: Son bilinen sensör paketinden mevcut açıklık
          oranı hesaplanarak PID'in integral'i bu değer üzerine resetlenir.
          Böylece modülasyon başladığı anda ani bir komut sıçraması yaşanmaz.
        """
        logger.info(f"Modülasyon başlatıldı. Aktif Mod: {self.context.control_mode.name}")

        # BaseState yardımcısı: await broadcaster.publish("STATE_CHANGED", {"state": "ModulatingState"})
        await self._publish_state()

        # --- Bumpless Transfer ---
        # Queue'dan tüketme (consume) yapılmaz; sadece cache'lenmiş son paket okunur.
        # Gerçek tüketim update() döngüsünde yapılır.
        current_pct = 0.0
        packet = self.context.last_sensor_packet

        if packet:
            ticks_per_turn: int = self.context.config.hardware.get("ticks_per_turn", 10000)
            current_pct = self.context.valve_char.get_opening_from_ticks(
                packet.motor_pos_ticks, ticks_per_turn
            )
        else:
            logger.warning(
                "Bumpless transfer: son sensör paketi bulunamadı, "
                "PID çıkışı 0.0 olarak başlatılıyor."
            )

        if hasattr(self.context, "pid"):
            self.context.pid.reset(current_output=current_pct)

    async def update(self, dt: float) -> Optional[BaseState]:
        """
        Kapalı çevrim kontrol döngüsünün tek bir iterasyonu.

        Args:
            dt: Son çağrıdan bu yana geçen süre [saniye].

        Returns:
            None          → Mevcut state'de kal.
            FaultSafeState → Sensör kaybı toleransı aşıldı.
        """
        # --- 1. Sensör Paketi Okuma ---
        # get_nowait(): Kuyrukta paket yoksa hemen QueueEmpty fırlatır, döngüyü bloklamaz.
        try:
            packet: SensorPacket = self.context.raw_data_queue.get_nowait()
            self.context.last_sensor_packet = packet  # Cache güncelle
            self._missing_data_cycles = 0
        except asyncio.QueueEmpty:
            self._missing_data_cycles += 1
            if self._missing_data_cycles > self._MAX_MISSING_CYCLES:
                return await self._trigger_fault(
                    AlarmCode.COMMUNICATION_LOST,
                    "Sensör verisi akışı kesildi."
                )
            # Tolerans içindeyiz: son PID çıkışını koru, komut gönderme.
            return None

        # --- 2. Process Variable (PV) Belirleme ---
        # Saf hesaplama, I/O yok → senkron metod, await gerekmez.
        pv: float = self._calculate_pv(packet)
        sp: float = getattr(self.context, "setpoint", 0.0)

        # --- 3. PID Hesaplaması ---
        if hasattr(self.context, "pid"):
            target_opening_pct: float = self.context.pid.compute(
                setpoint=sp, pv=pv, dt=dt
            )
        else:
            # PID henüz enjekte edilmemişse setpoint'i doğrudan hedef al (passthrough).
            target_opening_pct = sp

        # --- 4. Motor Komutunu Kuyruğa At ---
        cmd = MotorCommand(
            type=CommandType.MOVE_ABSOLUTE,
            value=target_opening_pct,
            direction=0,
            priority=1,
        )
        await self.context.command_queue.put((cmd.priority, cmd))

        return None

    async def on_exit(self) -> None:
        """
        Modülasyondan çıkılırken motoru mevcut pozisyonda kilitler (Hold).
        Ani hareket veya sürüklenme (drift) riskini engeller.
        """
        cmd = MotorCommand(
            type=CommandType.STOP_IMMEDIATE,
            value=0.0,
            direction=0,
            priority=1,
        )
        await self.context.command_queue.put((cmd.priority, cmd))
        logger.info("Modülasyon sonlandırıldı. Motor pozisyonu kilitlendi (Hold).")

    # ------------------------------------------------------------------
    # Olay İşleyici
    # ------------------------------------------------------------------

    async def handle_event(self, event: Dict[str, Any]) -> Optional[BaseState]:
        """
        UI'dan WebSocket üzerinden gelen komutları işler.

        Desteklenen komutlar:
            STOP_MODULATION  → IdleState'e geçiş.
            EMERGENCY_STOP   → FaultSafeState'e geçiş + alarm yayını.
        """
        cmd = event.get("cmd")

        if cmd == "STOP_MODULATION":
            logger.info("Modülasyon UI komutuyla durduruldu.")
            from controller.states.idle_state import IdleState
            return IdleState(self.context)

        elif cmd == "EMERGENCY_STOP":
            return await self._trigger_fault(
                AlarmCode.EMERGENCY_STOP,
                "UI üzerinden acil durdurma tetiklendi."
            )

        return None

    # ------------------------------------------------------------------
    # Özel Yardımcı Metodlar
    # ------------------------------------------------------------------

    def _calculate_pv(self, packet: SensorPacket) -> float:
        """
        Aktif kontrol moduna göre Process Variable (PV) değerini döner.

        Saf hesaplama metodudur: I/O işlemi yoktur, await gerekmez.

        Desteklenen modlar:
            POSITION → Encoder tick'inden valf açıklık yüzdesi (%).
            PRESSURE → Giriş basıncı P1 (bar).
            DELTA_P  → P1 - P2 basınç farkı (bar).
            Diğer    → 0.0 (güvenli varsayılan).
        """
        mode: ControlMode = getattr(self.context, "control_mode", ControlMode.POSITION)

        if mode == ControlMode.POSITION:
            ticks_per_turn: int = self.context.config.hardware.get("ticks_per_turn", 10000)
            return self.context.valve_char.get_opening_from_ticks(
                packet.motor_pos_ticks, ticks_per_turn
            )

        elif mode == ControlMode.PRESSURE:
            return packet.p1_raw

        elif mode == ControlMode.DELTA_P:
            if hasattr(self.context, "flow_calc"):
                return self.context.flow_calc.calculate_delta_p(
                    packet.p1_raw, packet.p2_raw
                )
            return 0.0

        # FLOW, REGULATOR vb. modlar ilerleyen sprint'lerde eklenecek.
        logger.warning(f"_calculate_pv: '{mode.name}' modu için PV hesabı henüz tanımlı değil.")
        return 0.0

    async def _trigger_fault(self, code: AlarmCode, reason: str) -> BaseState:
        """
        Hata durumunda alarm yayınlar ve FaultSafeState'e geçişi tetikler.

        Eski kod:
            self.context.signal_bus.alarm_triggered.emit(int(code))
            return FaultSafeState(self.context, reason=reason)

        Yeni kod:
            await self._publish_alarm(int(code), reason)   ← BaseState helper
            return FaultSafeState(self.context, reason=reason)

        Not:
            FaultSafeState.on_enter() kendi STOP_IMMEDIATE komutunu atacağı için
            burada ek bir stop komutu gönderilmez. Çift komut gönderimi önlenir.
        """
        logger.error(f"Modülasyon hata durumuna geçiyor. Kod: {code.name} | Sebep: {reason}")

        # BaseState yardımcısı: await broadcaster.publish("ALARM_TRIGGERED", {...})
        await self._publish_alarm(int(code), reason)

        from controller.states.fault_safe_state import FaultSafeState
        return FaultSafeState(self.context, reason=reason)