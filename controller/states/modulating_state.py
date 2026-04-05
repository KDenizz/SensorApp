from __future__ import annotations
import logging
from queue import Empty
from typing import Optional, Dict, Any, TYPE_CHECKING

from controller.states.base_state import BaseState
from core.data_types import MotorCommand, CommandType, AlarmCode, ControlMode

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)

class ModulatingState(BaseState):
    """
    [Controller Layer - State] Mod 02 & 03: Kapalı Çevrim Oransal Kontrol Modu.
    Kullanıcının seçtiği Setpoint'e (Basınç, Pozisyon veya Debi) ulaşmak için 
    PID algoritmasını çalıştırır ve aktüatöre sürekli pozisyon komutu gönderir.
    """

    def __init__(self, context: AppContext) -> None:
        super().__init__(context)
        self._missing_data_cycles: int = 0
        self._MAX_MISSING_CYCLES = 10  # 200Hz'de ~50ms sensör kaybı toleransı

    def on_enter(self) -> None:
        logger.info(f"Modülasyon başlatıldı. Aktif Mod: {self.context.control_mode.name}")
        self.context.signal_bus.state_changed.emit("MODULATING")
        
        # Bumpless Transfer: Queue'yu tüketmeden (consume), cache'lenmiş son paketi al
        current_pct = 0.0
        packet = self.context.last_sensor_packet
        
        if packet:
            # ConfigParser üzerinden dict erişimi düzeltildi
            ticks_per_turn = getattr(self.context.config, 'hardware', {}).get("ticks_per_turn", 10000)
            current_pct = self.context.valve_char.get_opening_from_ticks(packet.motor_pos_ticks, ticks_per_turn)
        else:
            logger.warning("Bumpless transfer için son sensör paketi bulunamadı, 0.0 kabul ediliyor.")

        if hasattr(self.context, 'pid'):
            self.context.pid.reset(current_output=current_pct)

    def update(self, dt: float) -> Optional[BaseState]:
        try:
            # Gerçek okuma (Consume) döngü içinde yapılır ve cache güncellenir
            packet = self.context.raw_data_queue.get_nowait()
            self.context.last_sensor_packet = packet 
            self._missing_data_cycles = 0
        except Empty:
            self._missing_data_cycles += 1
            if self._missing_data_cycles > self._MAX_MISSING_CYCLES:
                return self._trigger_fault(AlarmCode.COMMUNICATION_LOST, "Sensör verisi akışı kesildi.")
            
            # Veri gelene kadar hesaplama yapma, son PID çıkışını koru
            return None 

        # 1. Process Variable (PV) Belirleme
        pv = self._calculate_pv(packet)
        sp = getattr(self.context, 'setpoint', 0.0)

        # 2. PID Hesaplaması
        if hasattr(self.context, 'pid'):
            target_opening_pct = self.context.pid.compute(setpoint=sp, pv=pv, dt=dt)
        else:
            target_opening_pct = sp 

        # 3. Komut Gönderimi (Motoru hedef açıklığa sür)
        cmd = MotorCommand(
            type=CommandType.MOVE_ABSOLUTE,
            value=target_opening_pct,
            direction=0,
            priority=1
        )
        self.context.command_queue.put((cmd.priority, cmd))

        return None

    def _calculate_pv(self, packet: Any) -> float:
        """Sistemin seçili moduna göre (Basınç, Pozisyon vs.) doğru sensör verisini çeker."""
        mode = getattr(self.context, 'control_mode', ControlMode.POSITION)

        if mode == ControlMode.POSITION:
            ticks_per_turn = getattr(self.context.config, 'hardware', {}).get("ticks_per_turn", 10000)
            return self.context.valve_char.get_opening_from_ticks(packet.motor_pos_ticks, ticks_per_turn)
            
        elif mode == ControlMode.PRESSURE:
            return packet.p1_raw
            
        elif mode == ControlMode.DELTA_P:
            if hasattr(self.context, 'flow_calc'):
                return self.context.flow_calc.calculate_delta_p(packet.p1_raw, packet.p2_raw)
            return 0.0
            
        return 0.0

    def handle_event(self, event: Dict[str, Any]) -> Optional[BaseState]:
        cmd = event.get("cmd")
        
        if cmd == "STOP_MODULATION":
            from controller.states.idle_state import IdleState
            return IdleState(self.context)
            
        elif cmd == "EMERGENCY_STOP":
            return self._trigger_fault(AlarmCode.EMERGENCY_STOP, "UI üzerinden acil durdurma tetiklendi.")
            
        return None

    def _trigger_fault(self, code: AlarmCode, reason: str) -> BaseState:
        """Hata durumunda güvenli moda geçiş yapar."""
        self.context.signal_bus.alarm_triggered.emit(int(code))
        from controller.states.fault_safe_state import FaultSafeState
        return FaultSafeState(self.context, reason=reason)

    def on_exit(self) -> None:
        """Modülasyon bittiğinde valfi olduğu pozisyonda kilitler (Hold)."""
        cmd = MotorCommand(type=CommandType.STOP_IMMEDIATE, value=0.0, direction=0, priority=1)
        self.context.command_queue.put((cmd.priority, cmd))
        logger.info("Modülasyon sonlandırıldı. Motor pozisyonu kilitlendi (Hold).")