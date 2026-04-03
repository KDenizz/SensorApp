import threading
import logging
from queue import PriorityQueue, Queue
from typing import Tuple

from core.signal_bus import SignalBus
from core.datatypes import MotorCommand, CommandType, SensorPacket
from core.config_parser import ConfigParser

logger = logging.getLogger(__name__)

class AppContext:
    """
    Sistem genelindeki paylaşımlı, thread-safe kaynakları barındıran Singleton bağlam sınıfı.
    Konfigürasyon, komut ve veri kuyrukları ile sinyal barasına merkezi erişim sağlar.
    """
    _instance: "AppContext | None" = None
    _initialized: bool = False

    def __new__(cls) -> "AppContext":
        if cls._instance is None:
            cls._instance = super(AppContext, cls).__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """
        AppContext sınıfını başlatır.
        Multithreaded yapıdaki thread-safe kuyrukları, sinyal barasını 
        ve salt okunur (read-only) sistem konfigürasyonlarını oluşturur.
        """
        if self._initialized:
            return

        # 1. Konfigürasyon Yöneticisi (Fail-Fast & Log)
        self.config = ConfigParser(config_root="config")
        try:
            self.config.load_all()
        except Exception as e:
            logger.critical(f"Kritik konfigürasyon yüklenemedi: {e}")
            raise  # main.py tarafında yakalanıp UI hatası olarak gösterilecek

        # 2. Merkezi Sinyal Barası (Event-Driven iletişim için)
        self.signal_bus = SignalBus()
        
        # 3. Thread-Safe Kuyruklar (Inter-Thread Haberleşme)
        # Motor komutları (Priority: 0=Acil, 1=Normal)
        self.command_queue: PriorityQueue[Tuple[int, MotorCommand]] = PriorityQueue()
        # HAL'den Controller'a akan ham donanım okumaları
        self.raw_data_queue: Queue[SensorPacket] = Queue()
        # Sistem geneli asenkron dosya yazma işlemleri için log kuyruğu
        self.log_queue: Queue[dict] = Queue()
        
        # 4. Graceful Shutdown (Güvenli Kapanış) Bayrağı
        self.stop_event = threading.Event()
        
        # 6. Shutdown koruması
        self._shutdown_requested = False
        
        self._initialized = True
        logger.info("AppContext başlatma tamamlandı.")


    def request_shutdown(self) -> None:
        """
        Tüm sistemi güvenli bir şekilde kapatmak için stop_event bayrağını tetikler.
        """
        if self._shutdown_requested:
            logger.debug("Shutdown zaten talep edilmiş.")
            return
                
        self._shutdown_requested = True
        logger.warning("Sistem kapatılıyor (EMERGENCY STOP tetiklendi).")

        self.stop_event.set()
        
        # Güvenlik önlemi: Kapanış istendiğinde donanımı anında durdurmayı garantiye al.
        emergency_stop = MotorCommand(type=CommandType.STOP_IMMEDIATE, priority=0)
        self.command_queue.put((0, emergency_stop))
        logger.debug("EMERGENCY STOP komutu command_queue'ya eklendi.")
