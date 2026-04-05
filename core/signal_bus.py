# QObject alt sınıfı olarak tanımlanmış Singleton PyQt5 sinyal havuzu

from PyQt5.QtCore import QObject, pyqtSignal
from core.data_types import SensorPacket, ComputedPacket, SystemState, AlarmCode

class SignalBus(QObject):
    """
    Sistem genelindeki tüm asenkron olayların (event) rotalandığı merkezi sinyal barası.
    Katmanların (UI, Controller, Computation, HAL) birbirini doğrudan tanımadan
    veri alışverişi yapmasını (decoupling) sağlar.
    """
    
    # 1. HAL -> Computation / UI / Controller
    # Sensörlerden donanımsal veriler (basınç, sıcaklık, akım, enkoder) başarıyla okunduğunda tetiklenir.
    sensor_data_ready = pyqtSignal(SensorPacket)
    
    # 2. Computation -> UI / Controller
    # Sensör verileri fiziksel formüllere sokulup debi, Cv, valf açıklığı gibi değerler hesaplandığında tetiklenir.
    computed_data_ready = pyqtSignal(ComputedPacket)
    
    # 3. Controller -> UI / HAL
    # Sistem durum makinesi (State Machine) yeni bir faza (örn. CALIBRATING -> RUNNING) geçtiğinde tetiklenir.
    state_changed = pyqtSignal(SystemState)
    
    # 4. Any Layer -> UI / Controller
    # Sistemde kritik bir hata (limit aşımı, acil stop, sensör kopması) meydana geldiğinde tetiklenir.
    alarm_triggered = pyqtSignal(AlarmCode)

    _instance: "SignalBus | None" = None

    def __new__(cls, parent: QObject = None) -> "SignalBus":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, parent: QObject = None) -> None:
        """
        SignalBus sınıfını başlatır.
        
        Args:
            parent: QObject hiyerarşisinde üst nesne (varsayılan: None). 
                    Genellikle AppContext içinde singleton olarak yaratılacağı için None bırakılır.
        """
        if not hasattr(self, "_initialized"):  # __init__'in birden fazla çağrılmasını önler
            super().__init__(parent)
            self._initialized = True