# FINDING_ZERO ve FINDING_MAX alt durumlarını (sub-states) içerir.
import logging
from queue import Empty
from enum import Enum, auto
from typing import Optional, Dict, Any, TYPE_CHECKING

from controller.states.base_state import BaseState
from core.data_types import MotorCommand, CommandType, AlarmCode

# Geçici olarak (Mock) HAL paketini import edelim, gerçek projede datatypes.py'de bulunur
if TYPE_CHECKING:
    from core.app_context import AppContext
    from core.data_types import SensorPacket

class IdleState(BaseState): pass
class FaultSafeState(BaseState): pass

logger = logging.getLogger(__name__)

class CalibSubState(Enum):
    INIT = auto()
    FINDING_ZERO = auto()
    SETTLING_ZERO = auto()
    FINDING_MAX = auto()
    SETTLING_MAX = auto()
    DONE = auto()

class CalibratingState(BaseState):
    """
    [Controller Layer - State] Mod 01: Yön/Kalibrasyon ve Sıfır Arama Modu.
    Vananın fiziksel %0 ve %100 noktalarını tork (akım) sınırına dayanarak tespit eder.
    """

    CALIBRATION_SPEED_PCT = 15.0      
    TORQUE_LIMIT_MA = 1500.0          
    BLIND_TIME_SEC = 0.5              
    TIMEOUT_SEC = 60.0                
    DEBOUNCE_COUNT_LIMIT = 5          

    def __init__(self, context: 'AppContext') -> None:
        super().__init__(context)
        self.sub_state = CalibSubState.INIT
        
        # Zamanlayıcılar ayrıştırıldı
        self._total_timer: float = 0.0  # Tüm kalibrasyon için master timeout
        self._sub_timer: float = 0.0    # Alt durumlar (Blind time, settling) için yerel timer
        
        self._torque_hit_count: int = 0
        self._zero_tick: int = 0
        self._max_tick: int = 0

    def on_enter(self) -> None:
        logger.info("Kalibrasyon (Mod 01) başlatıldı. Sıfır noktası aranıyor...")
        self.context.signal_bus.state_changed.emit("CALIBRATING")
        
        # Önceki verileri temizle
        self.context.is_calibrated = False
        self.context.zero_tick = 0
        self.context.max_tick = 0
        self.context.total_stroke_ticks = 0

    def update(self, dt: float) -> Optional[BaseState]:
        self._total_timer += dt
        self._sub_timer += dt

        # Global Timeout Koruması
        if self._total_timer > self.TIMEOUT_SEC:
            logger.error("Kalibrasyon Timeout! (Maksimum süre aşıldı)")
            return self._trigger_fault(AlarmCode.CALIBRATION_TIMEOUT)

        # HAL Katmanından Sensör Paketini Çek (Queue)
        try:
            packet = self.context.raw_data_queue.get_nowait()
            current_ma = packet.motor_current_ma
            encoder_ticks = packet.motor_pos_ticks
        except Empty:
            return None # Veri yoksa pas geç

        # State Yönlendirmesi
        if self.sub_state == CalibSubState.INIT:
            self._start_finding_zero()

        elif self.sub_state == CalibSubState.FINDING_ZERO:
            self._process_finding_zero(current_ma, encoder_ticks)

        elif self.sub_state == CalibSubState.SETTLING_ZERO:
            self._process_settling_zero(encoder_ticks)

        elif self.sub_state == CalibSubState.FINDING_MAX:
            self._process_finding_max(current_ma, encoder_ticks)

        elif self.sub_state == CalibSubState.SETTLING_MAX:
            self._process_settling_max(encoder_ticks)

        elif self.sub_state == CalibSubState.DONE:
            logger.info("Kalibrasyon başarıyla tamamlandı.")
            return IdleState(self.context)

        return None

    def _start_finding_zero(self) -> None:
        cmd = MotorCommand(
            type=CommandType.MOVE_VELOCITY, 
            value=self.CALIBRATION_SPEED_PCT,
            direction=-1,
            priority=1
        )
        self._send_command(cmd)
        
        self.sub_state = CalibSubState.FINDING_ZERO
        self._reset_sub_timer()

    def _process_finding_zero(self, current_ma: float, encoder_ticks: int) -> None:
        if self._check_torque_limit(current_ma):
            logger.info(f"Sıfır noktası mekanik limiti bulundu. (Akım: {current_ma}mA)")
            self._send_stop_command()
            
            self.sub_state = CalibSubState.SETTLING_ZERO
            self._reset_sub_timer()

    def _process_settling_zero(self, encoder_ticks: int) -> None:
        if self._sub_timer >= 0.5:
            self._zero_tick = encoder_ticks
            logger.info(f"Sıfır noktası kaydedildi: {self._zero_tick} ticks.")
            
            cmd = MotorCommand(
                type=CommandType.MOVE_VELOCITY, 
                value=self.CALIBRATION_SPEED_PCT,
                direction=1,
                priority=1
            )
            self._send_command(cmd)
            
            self.sub_state = CalibSubState.FINDING_MAX
            self._reset_sub_timer()

    def _process_finding_max(self, current_ma: float, encoder_ticks: int) -> None:
        if self._check_torque_limit(current_ma):
            logger.info(f"Max noktası mekanik limiti bulundu. (Akım: {current_ma}mA)")
            self._send_stop_command()
            
            self.sub_state = CalibSubState.SETTLING_MAX
            self._reset_sub_timer()

    def _process_settling_max(self, encoder_ticks: int) -> None:
        if self._sub_timer >= 0.5:
            self._max_tick = encoder_ticks
            logger.info(f"Max noktası kaydedildi: {self._max_tick} ticks.")
            
            total_stroke_ticks = abs(self._max_tick - self._zero_tick)
            if total_stroke_ticks < 100: 
                logger.error(f"Geçersiz kalibrasyon stroku: {total_stroke_ticks} ticks.")
                self._trigger_fault(AlarmCode.CALIBRATION_FAILED)
                return

            self.context.zero_tick = self._zero_tick
            self.context.max_tick = self._max_tick
            self.context.total_stroke_ticks = total_stroke_ticks
            self.context.is_calibrated = True
            
            self.sub_state = CalibSubState.DONE

    def _check_torque_limit(self, current_ma: float) -> bool:
        if self._sub_timer < self.BLIND_TIME_SEC:
            return False 

        if current_ma >= self.TORQUE_LIMIT_MA:
            self._torque_hit_count += 1
        else:
            self._torque_hit_count = 0 

        return self._torque_hit_count >= self.DEBOUNCE_COUNT_LIMIT

    def _reset_sub_timer(self) -> None:
        """Alt durum geçişlerinde bekleme ve koruma süresini sıfırlar."""
        self._sub_timer = 0.0
        self._torque_hit_count = 0

    def _send_command(self, cmd: MotorCommand) -> None:
        self.context.command_queue.put((cmd.priority, cmd))

    def _send_stop_command(self) -> None:
        stop_cmd = MotorCommand(type=CommandType.STOP, priority=0)
        self.context.command_queue.put((stop_cmd.priority, stop_cmd)) 

    def _trigger_fault(self, code: 'AlarmCode') -> 'FaultSafeState':
        self._send_stop_command()
        self.context.signal_bus.alarm_triggered.emit(int(code))
        return FaultSafeState(self.context)

    def on_exit(self) -> None:
        self._send_stop_command()
        logger.debug("CalibratingState çıkış yaptı.")