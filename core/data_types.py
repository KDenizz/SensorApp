# SensorPacket, ComputedPacket, AlarmCode, CommandType ve MotorCommand
from dataclasses import dataclass, field
from enum import Enum, IntEnum, auto
from typing import Optional, Dict

class AlarmCode(IntEnum):
    """Sistem genelinde kritik hata ve uyarı kodları."""
    NONE = 0
    EMERGENCY_STOP = 1
    COMMUNICATION_LOST = 2
    SENSOR_OUT_OF_RANGE = 3
    MOTOR_OVER_TORQUE = 4
    LIMIT_EXCEEDED = 5

class CommandType(Enum):
    """Motor ve sistem kontrol komut tipleri."""
    MOVE_ABSOLUTE = auto()  # Belirli bir tick/tur noktasına git
    MOVE_RELATIVE = auto()  # Mevcut konumdan X kadar git
    SET_SPEED = auto()      # Hız limitini güncelle
    SET_TORQUE = auto()     # Tork limitini güncelle
    STOP_IMMEDIATE = auto() # Acil durdurma
    CALIBRATE = auto()      # Kalibrasyon rutinini başlat

class ControlMode(Enum):
    """Aktif kontrol algoritmaları."""
    POSITION = "Position"
    PRESSURE = "Pressure"
    FLOW = "Flow"
    REGULATOR = "Regulator"

class FlowRegime(Enum):
    NORMAL = "Normal"
    CHOKED = "Choked"

class SystemState(Enum):
    IDLE = auto()
    CALIBRATING = auto()
    RUNNING = auto()
    FAULT_SAFE = auto()
    LIMIT_EXCEEDED = auto()
    TORQUE_LIMITED = auto()


@dataclass(frozen=True)
class SensorPacket:
    """Hardware Layer'dan (HAL) gelen ham sensör verileri."""
    p1_raw: float           # Giriş Basıncı (bar) [cite: 1, 643]
    p2_raw: float           # Çıkış Basıncı (bar) [cite: 3, 643]
    temp_k: float           # Akışkan Sıcaklığı (Kelvin) [cite: 644, 645]
    motor_pos_ticks: int    # Encoder'dan okunan ham tick değeri [cite: 647]
    motor_current_ma: float # Motordan okunan akım (mA) [cite: 502, 691]
    timestamp: float        # Verinin alındığı sistem zamanı

@dataclass(frozen=True)
class ComputedPacket:
    """Computation Layer tarafından işlenmiş fiziksel büyüklükler."""
    delta_p: float          # P1 - P2 (bar) [cite: 6, 647]
    mass_flow: float        # Kütlesel debi (kg/s) [cite: 151, 644]
    opening_pct: float      # Valf açıklık oranı (%) [cite: 37, 648]
    strok_mm: float         # İğne ilerleme mesafesi (mm) [cite: 648]
    cv_value: float         # Hesaplanan akış katsayısı [cite: 645, 646]
    flow_regime: FlowRegime = FlowRegime.NORMAL        # 'Normal' veya 'Choked' (Boğulmuş) [cite: 665, 666]
    alarms: tuple[AlarmCode, ...] = field(default_factory=tuple)  # Aktif alarmlar (varsa)

@dataclass(frozen=True)
class MotorCommand:
    """PriorityQueue üzerinden HAL'e iletilen komut nesnesi."""
    type: CommandType
    value: float = 0.0      # Hedef konum, hız veya tork değeri
    priority: int = 1       # 0: Emergency, 1: Normal [Finalize Mimari]
    metadata: Dict[str, float] = field(default_factory=dict)  # Ek bilgi (örneğin, hız değişimi için hız limiti, konum değişimi için tolerans vb.)""

    def __lt__(self, other):
        """PriorityQueue için öncelik karşılaştırması."""
        return self.priority < other.priority