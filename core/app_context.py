"""
AppContext: Sistem genelindeki paylaşımlı, asenkron kaynakları barındıran Singleton bağlam sınıfı.
"""

import asyncio
import logging
from typing import Tuple, Dict, Any, Optional

from core.data_types import MotorCommand, CommandType, SensorPacket, ControlMode
from core.config_parser import ConfigParser
from server.ws_broadcaster import WsBroadcaster

logger = logging.getLogger(__name__)

class AppContext:
    """
    Sistem genelindeki paylaşımlı, asenkron kaynakları barındıran Singleton bağlam sınıfı.
    Konfigürasyon, komut ve veri kuyrukları ile WebSocket broadcaster'a merkezi erişim sağlar.
    """
    _instance: Optional["AppContext"] = None
    _initialized: bool = False

    def __new__(cls) -> "AppContext":
        if cls._instance is None:
            cls._instance = super(AppContext, cls).__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        """
        AppContext sınıfının senkron (Event Loop bağımsız) kısmını başlatır.
        Sadece konfigürasyon gibi I/O engeli yaratmayan bileşenler burada yüklenir.
        Asenkron primitifler initialize_async() içerisinde yaratılacaktır.
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

        # 2. Asenkron Primitiflerin Referansları (Lazy Init için None ataması)
        self.broadcaster: Optional[WsBroadcaster] = None
        self.command_queue: Optional[asyncio.PriorityQueue[Tuple[int, MotorCommand]]] = None
        self.raw_data_queue: Optional[asyncio.Queue[SensorPacket]] = None
        self.log_queue: Optional[asyncio.Queue[Dict[str, Any]]] = None
        self.stop_event: Optional[asyncio.Event] = None
        
        # 3. Durum Kontrol ve Korumalar
        self._shutdown_requested: bool = False
        self.last_sensor_packet: Optional[SensorPacket] = None
        self.control_mode: ControlMode = ControlMode.POSITION
        
        # 4. Kalibrasyon Verileri (Tip güvenliği için açıkça tanımlandı)
        self.is_calibrated: bool = False
        self.zero_tick: int = 0
        self.max_tick: int = 0
        self.total_stroke_ticks: int = 0

        self._initialized = True
        logger.info("AppContext senkron başlatması tamamlandı. Event Loop bekleniyor.")

    async def initialize_async(self) -> None:
        """
        Event Loop (asyncio.run) ayağa kalktıktan sonra çağrılmalıdır.
        Sistemin can damarı olan asenkron kuyrukları, event'leri ve Broadcaster'ı yaratır.
        """
        # Event Loop'a bağlı kuyruklar
        self.command_queue = asyncio.PriorityQueue()
        self.raw_data_queue = asyncio.Queue()
        self.log_queue = asyncio.Queue()
        self.stop_event = asyncio.Event()

        # WebSocket Broadcaster'ı başlat
        self.broadcaster = WsBroadcaster()
        await self.broadcaster.initialize()

        logger.info("AppContext asenkron bileşenleri (Broadcaster, Queue, Event) başlatıldı.")

    async def request_shutdown(self) -> None:
        """
        Tüm sistemi güvenli bir şekilde kapatmak için stop_event bayrağını asenkron olarak tetikler.
        """
        if self._shutdown_requested:
            logger.debug("Shutdown zaten talep edilmiş.")
            return
                
        self._shutdown_requested = True
        logger.warning("Sistem kapatılıyor (EMERGENCY STOP tetiklendi).")

        if self.stop_event:
            self.stop_event.set()
        
        # Güvenlik önlemi: Kapanış istendiğinde donanımı anında durdurmayı garantiye al.
        if self.command_queue:
            emergency_stop = MotorCommand(type=CommandType.STOP_IMMEDIATE, priority=0)
            # await kullanmak, kuyruk mekanizmasının asenkron yapısıyla tam uyum sağlar
            await self.command_queue.put((0, emergency_stop))
            logger.debug("EMERGENCY STOP komutu command_queue'ya eklendi.")