"""
controller/states/fault_safe_state.py

[Controller Layer - State] Sistem arıza (Fault) durumu.

Migration Notları (threading → asyncio):
    1. `on_enter`, `update`, `on_exit`, `handle_event` → `async def`
    2. `self.context.signal_bus.state_changed.emit(...)` → `await self._publish_state()`
    3. `self.context.command_queue.put(...)`             → `await self.context.command_queue.put(...)`
    4. `_send_emergency_stop()` senkron metod           → `async def _send_emergency_stop()`

Mimari Kural:
    - HAL'e doğrudan erişim YOKTUR.
    - Motor komutu SADECE context.command_queue üzerinden gider (priority=0 EMERGENCY).
    - UI bildirimi SADECE context.broadcaster.publish() üzerinden yapılır.
"""

from __future__ import annotations

import logging
from typing import Optional, Dict, Any, TYPE_CHECKING

from controller.states.base_state import BaseState
from core.data_types import MotorCommand, CommandType, AlarmCode

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class FaultSafeState(BaseState):
    """
    [Controller Layer - State] Sistem arıza durumuna geçtiğinde çalışan güvenlik modülü.

    Tüm operasyonu kilitler; donanıma doğrudan müdahale etmez ancak
    command_queue üzerinden Priority=0 (EMERGENCY) durdurma komutu gönderir.

    Tek geçerli çıkış: RESET_FAULT eventi → IdleState
    """

    def __init__(self, context: "AppContext", reason: str = "Bilinmeyen Arıza") -> None:
        super().__init__(context)
        self.reason = reason

    # ------------------------------------------------------------------
    # Yaşam Döngüsü
    # ------------------------------------------------------------------

    async def on_enter(self) -> None:
        """
        Arıza durumuna girildiğinde:
        - ALARM_TRIGGERED + STATE_CHANGED WebSocket üzerinden yayınlanır.
        - EMERGENCY STOP komutu Priority=0 ile kuyruğa atılır.
        """
        logger.critical(f"SİSTEM ARIZA DURUMUNA GEÇTİ. Sebep: {self.reason}")

        # UI'ya STATE_CHANGED bildirimi
        await self._publish_state()

        # UI'ya ALARM bildirimi
        await self._publish_alarm(
            int(AlarmCode.EMERGENCY_STOP),
            self.reason
        )

        # HAL'e EMERGENCY STOP komutu (katman kuralı korunuyor)
        await self._send_emergency_stop()

    async def update(self, dt: float) -> Optional[BaseState]:
        """Arıza durumunda sistem kilitli kalır, yalnızca RESET_FAULT eventi bekler."""
        return None

    async def on_exit(self) -> None:
        """Arıza durumundan çıkılırken log bırakır."""
        logger.info("Sistem Arıza (Fault) durumundan çıkış yapıyor.")

    # ------------------------------------------------------------------
    # Olay İşleyici
    # ------------------------------------------------------------------

    async def handle_event(self, event: Dict[str, Any]) -> Optional[BaseState]:
        """
        RESET_FAULT komutu alındığında IdleState'e geçiş yapar.
        Diğer tüm komutlar reddedilir ve yeni bir STOP komutu gönderilir.
        """
        cmd = event.get("cmd")

        if cmd == "RESET_FAULT":
            logger.info("Arıza durumu UI üzerinden sıfırlandı. Bekleme moduna dönülüyor.")
            from controller.states.idle_state import IdleState
            return IdleState(self.context)

        else:
            logger.warning(f"Arıza durumundayken geçersiz komut reddedildi: {cmd}")
            # Güvenlik: Arıza durumundayken gelen her yabancı komuta STOP ile yanıt ver
            await self._send_emergency_stop()

        return None

    # ------------------------------------------------------------------
    # Yardımcı
    # ------------------------------------------------------------------

    async def _send_emergency_stop(self) -> None:
        """
        Priority=0 (EMERGENCY) STOP_IMMEDIATE komutunu kuyruğa atar.

        Eski kod (senkron):
            self.context.command_queue.put((cmd.priority, cmd))
        Yeni kod (async):
            await self.context.command_queue.put((cmd.priority, cmd))
        """
        cmd = MotorCommand(
            type=CommandType.STOP_IMMEDIATE,
            value=0.0,
            direction=0,
            priority=0  # EMERGENCY
        )
        await self.context.command_queue.put((cmd.priority, cmd))