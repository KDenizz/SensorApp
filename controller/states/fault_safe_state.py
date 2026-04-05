# HAL'e dokunmaz, command_queue'ya EMERGENCY (priority=0) komut atar.
from __future__ import annotations
import logging
from typing import Optional, Dict, Any, TYPE_CHECKING

from controller.states.base_state import BaseState
from core.data_types import MotorCommand, CommandType

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)

class FaultSafeState(BaseState):
    """
    [Controller Layer - State] Sistem arıza (Fault) durumuna geçtiğinde 
    çalıştırılan güvenlik modülü.
    Tüm operasyonu kilitler, donanımı doğrudan manipüle etmez ancak 
    command_queue üzerinden Priority=0 (EMERGENCY) durdurma komutları gönderir.
    """

    def __init__(self, context: AppContext, reason: str = "Bilinmeyen Arıza") -> None:
        super().__init__(context)
        self.reason = reason

    def on_enter(self) -> None:
        logger.critical(f"SİSTEM ARIZA DURUMUNA GEÇTİ. Sebep: {self.reason}")
        self.context.signal_bus.state_changed.emit("FAULT")
        self._send_emergency_stop()

    def update(self, dt: float) -> Optional[BaseState]:
        """Arıza durumunda sistem kilitli kalır, yalnızca RESET eventi bekler."""
        return None

    def handle_event(self, event: Dict[str, Any]) -> Optional[BaseState]:
        cmd = event.get("cmd")
        
        if cmd == "RESET_FAULT":
            logger.info("Arıza durumu UI üzerinden sıfırlandı. Bekleme moduna dönülüyor.")
            from controller.states.idle_state import IdleState
            return IdleState(self.context)
            
        else:
            logger.warning(f"Arıza durumundayken geçersiz komut reddedildi: {cmd}")
            self._send_emergency_stop()
            
        return None

    def _send_emergency_stop(self) -> None:
        cmd = MotorCommand(
            type=CommandType.STOP_IMMEDIATE, 
            value=0.0, 
            direction=0,
            priority=0
        )
        self.context.command_queue.put((cmd.priority, cmd))

    def on_exit(self) -> None:
        logger.info("Sistem Arıza (Fault) durumundan çıkış yapıyor.")