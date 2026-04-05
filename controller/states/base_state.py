import logging
from abc import ABC, abstractmethod
from typing import Any, Optional, Dict

# core veya app_context içerisindeki bağımlılıklar (Type hinting için)
# from core.app_context import AppContext (Runtime circular import olmaması için "if TYPE_CHECKING" kullanılabilir)

logger = logging.getLogger(__name__)

class BaseState(ABC):
    """
    [Controller Layer] Durum makinesi (State Machine) mimarisindeki tüm state'lerin 
    (Calibrating, Modulating, FaultSafe vs.) türeyeceği Abstract Base Class.
    """

    def __init__(self, context: Any) -> None:
        """
        Her state, sistemin tüm kuyruk ve sinyallerine erişebilmek için 
        AppContext (veya MainController referansı) alır.
        
        Args:
            context (AppContext): Sistem queue'larına ve konfigürasyonlarına erişim objesi.
        """
        self.context = context
        self.name: str = self.__class__.__name__

    @abstractmethod
    def on_enter(self) -> None:
        """
        State makinesi bu state'e geçiş yaptığında bir kere çağrılır.
        Örn: PID resetleme, başlangıç komutlarını command_queue'ya atma işlemleri.
        """
        pass

    @abstractmethod
    def update(self, dt: float) -> Optional['BaseState']:
        """
        Ana kontrol döngüsünde (örn. 200 Hz) her iterasyonda çağrılır.
        Sensör verilerini okur, hesaplamaları yapar, PID'yi çalıştırır.
        
        Args:
            dt (float): Son update'ten bu yana geçen zaman [saniye].
            
        Returns:
            Optional[BaseState]: Eğer durum değiştirilecekse (Örn: Kalibrasyon bitti, 
                                 Modülasyona geç) yeni state objesi dönülür. 
                                 Aynı kalacaksa None dönülür.
        """
        pass

    def handle_event(self, event: Dict[str, Any]) -> Optional['BaseState']:
        """
        UI'dan veya sistemden asenkron bir olay/komut (örn: Mod Değiştirme, Acil Durdurma)
        geldiğinde çağrılır. İsteğe bağlı olarak override edilebilir.
        
        Args:
            event (Dict): Olayın türü ve yükünü içeren sözlük. (Örn: {"cmd": "MODE_CHANGE", "mode": "02"})
            
        Returns:
            Optional[BaseState]: Durum değiştirilecekse yeni state, değilse None.
        """
        return None

    @abstractmethod
    def on_exit(self) -> None:
        """
        State makinesi bu state'den çıkış yaptığında bir kere çağrılır.
        Örn: Açık kaynakları serbest bırakma, motoru güvenli duruma alma.
        """
        pass

    def __str__(self) -> str:
        return self.name