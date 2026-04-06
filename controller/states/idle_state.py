"""
controller/states/idle_state.py

[Controller Layer - State] Bekleme / Park Modu.

Migration Notları (threading → asyncio):
    1. `on_enter`, `update`, `on_exit`, `handle_event` → `async def`
    2. `self.context.signal_bus.state_changed.emit(...)` → `await self._publish_state()`
    3. `self.context.signal_bus.alarm_triggered.emit(...)` → `await self._publish_alarm(...)`
    4. `self.context.command_queue.put(...)`               → `await self.context.command_queue.put(...)`

Mimari Kural:
    - HAL'e doğrudan erişim YOKTUR.
    - Motor komutu SADECE context.command_queue üzerinden gider.
    - UI bildirimi SADECE context.broadcaster.publish() üzerinden yapılır.
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any, TYPE_CHECKING

from controller.states.base_state import BaseState
from core.data_types import MotorCommand, CommandType, AlarmCode, ControlMode

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class IdleState(BaseState):
    """
    [Controller Layer - State] Bekleme / Park Modu.
    Sistem kalibre edilmiş, motor hareketsiz, komut bekliyor.
    """

    # ------------------------------------------------------------------
    # Yaşam Döngüsü
    # ------------------------------------------------------------------

    async def on_enter(self) -> None:
        """Bekleme moduna girildiğinde motora STOP_IMMEDIATE gönderilir."""
        logger.info("Sistem Bekleme (Idle) durumuna geçti. Motor hareketsiz.")

        # UI'ya STATE_CHANGED bildirimi (BaseState helper)
        await self._publish_state()

        # Motora STOP_IMMEDIATE gönder (priority=1, normal)
        cmd = MotorCommand(
            type=CommandType.STOP_IMMEDIATE,
            value=0.0,
            direction=0,
            priority=1
        )
        await self.context.command_queue.put((cmd.priority, cmd))

    async def update(self, dt: float) -> Optional[BaseState]:
        """Sadece bekler, aktif bir döngü koşturmaz."""
        return None

    async def on_exit(self) -> None:
        """Durumdan çıkılırken log bırakır."""
        logger.debug("Idle durumundan çıkılıyor.")

    # ------------------------------------------------------------------
    # Olay İşleyici
    # ------------------------------------------------------------------

    async def handle_event(self, event: Dict[str, Any]) -> Optional[BaseState]:
        """
        Kullanıcı/Arayüz komutlarına göre diğer State'lere geçiş yapar.

        Desteklenen komutlar:
            START_MODULATION  → ModulatingState (kalibrasyon şartı aranır)
            CALIBRATE         → CalibratingState
            EMERGENCY_STOP    → FaultSafeState
            SET_SETPOINT      → Setpoint güncellenir, state değişmez
        """
        cmd = event.get("cmd")

        if cmd == "START_MODULATION":
            # Fiziksel sınırları bilmeden PID koşturulmasını önleyen güvenlik kilidi
            if not self.context.is_calibrated:
                logger.warning("Kalibrasyon yapılmadan Modülasyon başlatılamaz!")
                await self._publish_alarm(
                    int(AlarmCode.NOT_CALIBRATED),
                    "Modülasyon başlatmak için önce kalibrasyon yapılmalı."
                )
                return None

            # UI'dan gelen hedef modu Enum'a çevirip Context'e işle
            target_mode = event.get("mode", "Position")
            try:
                self.context.control_mode = ControlMode(target_mode)
            except ValueError:
                logger.error(f"Geçersiz kontrol modu: {target_mode}")
                return None

            from controller.states.modulating_state import ModulatingState
            return ModulatingState(self.context)

        elif cmd == "CALIBRATE":
            from controller.states.calibrating_state import CalibratingState
            return CalibratingState(self.context)

        elif cmd == "EMERGENCY_STOP":
            from controller.states.fault_safe_state import FaultSafeState
            return FaultSafeState(self.context, reason="UI üzerinden Acil Stop basıldı.")

        elif cmd == "SET_SETPOINT":
            value = event.get("value")
            if value is not None:
                self.context.setpoint = float(value)
                logger.debug(f"Setpoint güncellendi (Idle modunda): {self.context.setpoint}")

        return None