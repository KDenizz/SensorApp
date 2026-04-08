"""
main.py

Sistemin giriş noktası. AppContext'i başlatır, tüm async görevleri ayağa kaldırır
ve uvicorn aracılığıyla FastAPI + WebSocket sunucusunu çalıştırır.

Başlatma Sırası:
    1. AppContext senkron __init__ (konfigürasyon yükle)
    2. asyncio event loop başlar (asyncio.run)
    3. AppContext.initialize_async() → Queue'lar, Event, Broadcaster
    4. AsyncSerialPortManager.initialize_async() → asyncio.Lock
    5. FastAPI uygulaması oluşturulur (ws_server.create_app)
    6. asyncio.Task'lar başlatılır: hal_reader, hal_writer, data_logger,
       main_controller, broadcaster.broadcast_loop
    7. uvicorn WebSocket sunucusunu ayağa kaldırır
    8. stop_event beklenirken tüm Task'lar çalışır
    9. Kapanış sinyalinde tüm Task'lar iptal edilir

Mimari Not:
    main.py hiçbir iş mantığı içermez. Sadece nesneleri oluşturur,
    bağımlılıkları enjekte eder ve görevleri başlatır.
"""

from __future__ import annotations

import asyncio
import logging
from multiprocessing import context
import signal
import sys
from typing import List

import uvicorn

from core.app_context import AppContext
from hal.serial_port_manager import AsyncSerialPortManager
from hal.hal_reader import HALReader
from hal.hal_writer import HALWriter
from hal.data_logger import DataLogger
from controller.main_controller import MainController
from server.ws_server import create_app
from hal.modbus_config import Reg  
from hal.modbus_client import ModbusRTUClient

# Logging yapılandırması
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """
    Sistemin tüm async bileşenlerini başlatan ve koordine eden ana coroutine.
    """

    # ------------------------------------------------------------------
    # 1. AppContext Başlatma
    # ------------------------------------------------------------------
    context = AppContext()
    # initialize_async: Queue'lar, asyncio.Event ve WsBroadcaster bu adımda oluşturulur
    await context.initialize_async()

    # event_queue: ws_server → main_controller arası iletişim köprüsü
    # AppContext'e ekliyoruz (AppContext'e alan eklemek yerine lazy init)
    context.event_queue: asyncio.Queue = asyncio.Queue()

        # 2. Register Haritasını Yükle  ← YENİ BLOK
    # HALReader ve HALWriter oluşturulmadan ÖNCE çağrılmalıdır.
    # Reg.INPUT ve Reg.HOLDING bu satırdan sonra kullanılabilir.
    # ------------------------------------------------------------------
    Reg.load()
    logger.info("Modbus register haritası yüklendi.")



    # ------------------------------------------------------------------
    # 2. SerialPortManager Async Başlatma
    # ------------------------------------------------------------------
    serial_manager = AsyncSerialPortManager()
    await serial_manager.initialize_async()

    # ------------------------------------------------------------------
    # 3. FastAPI Uygulaması
    # ------------------------------------------------------------------
    app = create_app(context)
    hw = context.config.hardware
    shared_modbus = ModbusRTUClient(
        port=hw.get("port", "COM7"),
        baudrate=hw.get("baud_rate", 115200),
        timeout=hw.get("modbus_timeout", 0.1),
        slave_id=hw.get("slave_id", 1),
    )
    connected = await shared_modbus.connect()
    if not connected:
        logger.critical("Modbus bağlantısı kurulamadı, sistem başlatılamıyor.")
        return

    context.modbus_client = shared_modbus


    # ------------------------------------------------------------------
    # 4. Async Task'ları Başlat
    # ------------------------------------------------------------------
    hal_reader = HALReader(context)
    hal_writer = HALWriter(context)
    data_logger = DataLogger(context)
    controller = MainController(context)

    tasks: List[asyncio.Task] = [
        asyncio.create_task(hal_reader.run(),    name="HALReader"),
        asyncio.create_task(hal_writer.run(),    name="HALWriter"),
        asyncio.create_task(data_logger.run(),   name="DataLogger"),
        asyncio.create_task(controller.run(),    name="MainController"),
        asyncio.create_task(
            context.broadcaster.broadcast_loop(context.stop_event),
            name="BroadcastLoop"
        ),
    ]

    logger.info(f"{len(tasks)} async görev başlatıldı.")

    # ------------------------------------------------------------------
    # 5. OS Sinyal Yönetimi (Graceful Shutdown)
    # ------------------------------------------------------------------
    loop = asyncio.get_running_loop()

    def _on_shutdown_signal() -> None:
        logger.warning("Kapatma sinyali alındı. Sistem güvenli şekilde kapatılıyor...")
        asyncio.create_task(context.request_shutdown())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_shutdown_signal)
        except NotImplementedError:
            # Windows'ta add_signal_handler desteklenmez
            signal.signal(sig, lambda *_: _on_shutdown_signal())

    # ------------------------------------------------------------------
    # 6. uvicorn Sunucusu
    # ------------------------------------------------------------------
    config = uvicorn.Config(
        app=app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",   # uvicorn access loglarını sustur
        loop="none",           # Mevcut event loop'u kullan
    )
    server = uvicorn.Server(config)

    logger.info("Servo Valf Kontrol Sistemi başlatılıyor. ws://localhost:8000/ws")

    # uvicorn'u mevcut event loop'ta çalıştır
    await server.serve()

    # ------------------------------------------------------------------
    # 7. Kapanış: Tüm Task'ları İptal Et
    # ------------------------------------------------------------------
    logger.info("Sunucu kapandı. Görevler sonlandırılıyor...")
    context.stop_event.set()  # Henüz set edilmediyse garantiye al

    for task in tasks:
        if not task.done():
            task.cancel()

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for task, result in zip(tasks, results):
        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
            logger.error(f"Görev '{task.get_name()}' hata ile kapandı: {result}")

    logger.info("Tüm görevler sonlandırıldı. Sistem kapatıldı.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — sistem kapatıldı.")
    except Exception as e:
        logger.critical(f"Kritik başlatma hatası: {e}", exc_info=True)
        sys.exit(1)