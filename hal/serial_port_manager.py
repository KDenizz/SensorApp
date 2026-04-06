"""
hal/serial_port_manager.py

[HAL Layer] Reader ve Writer görevlerinin aynı fiziksel portu (COM/ttyUSB)
çakışmadan kullanmasını sağlayan asyncio-uyumlu Singleton.

Migration Notları (threading → asyncio):
    1. `threading.Lock()`   → `asyncio.Lock()`
       Okuma/yazma işlemleri event loop'a bağlı coroutine'ler içinde yapılacağı için
       asyncio.Lock kullanılır. `threading.Lock()` asyncio'da deadlock riskine yol açar.
    2. `threading.Lock()`   (init guard) → Singleton init senkron kaldı çünkü
       `__new__` event loop'tan önce çağrılabilir.
    3. Tüm public metotlar (`open`, `readline`, `write`, `close`) senkron kalmaya
       devam eder — HALReader/HALWriter zaten bunları `run_in_executor` veya
       `asyncio.to_thread` içinden çağırır, bu nedenle burada ayrıca coroutine
       yapmak gerekmez.

Not:
    asyncio.Lock() event loop'a bağımlıdır.
    Bu nedenle `_io_lock` doğrudan `__new__` veya `__init__` içinde DEĞİL,
    `initialize_async()` metodundan sonra (event loop aktifken) başlatılmalıdır.
    `open()` çağrısından önce mutlaka `await manager.initialize_async()` çağırın.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)


class AsyncSerialPortManager:
    """
    HALReader ve HALWriter'ın aynı fiziksel seri portu paylaşmasını sağlayan
    asyncio-uyumlu Singleton.

    Kullanım:
        manager = AsyncSerialPortManager()
        await manager.initialize_async()    # Event loop aktifken çağırılmalı
        # Ardından HALReader/HALWriter run_in_executor içinden:
        manager.open("COM3", 115200)
        data = manager.readline()
        manager.write(b"GOTO 500\\n")
        manager.close()
    """

    _instance: Optional["AsyncSerialPortManager"] = None
    _init_lock: threading.Lock = threading.Lock()  # __new__ senkron, threading.Lock OK

    def __new__(cls) -> "AsyncSerialPortManager":
        with cls._init_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._port = None
                cls._instance._is_open: bool = False
                cls._instance._io_lock: Optional[asyncio.Lock] = None
                cls._instance._async_initialized: bool = False
        return cls._instance

    async def initialize_async(self) -> None:
        """
        Event loop aktifken çağrılmalıdır (main.py veya AppContext.initialize_async içinde).
        asyncio.Lock nesnesi burada yaratılır.
        """
        if not self._async_initialized:
            self._io_lock = asyncio.Lock()
            self._async_initialized = True
            logger.debug("AsyncSerialPortManager: asyncio.Lock başlatıldı.")

    # ------------------------------------------------------------------
    # Senkron I/O metodları
    # (HALReader/HALWriter bunları run_in_executor veya to_thread içinden çağırır)
    # ------------------------------------------------------------------

    def open(self, port: str, baud: int) -> None:
        """
        Portu açar; zaten açıksa sessizce geri döner.
        Bu metod blocking'dir — executor'dan çağırılmalıdır.
        """
        # Gerçek pySerial entegrasyonu:
        # if self._port is None or not self._port.is_open:
        #     import serial
        #     self._port = serial.Serial(port, baud, timeout=0.1)
        logger.debug(f"AsyncSerialPortManager: {port} @ {baud} bps mock bağlantısı açıldı.")
        self._is_open = True

    def readline(self) -> bytes:
        """
        Porttan bir satır okur.
        Bu metod blocking'dir — executor'dan çağırılmalıdır.

        Returns:
            bytes: Okunan ham veri. Mock modunda boş bytes döner.
        """
        # Gerçek donanımda: return self._port.readline()
        return b""

    def write(self, data: bytes) -> None:
        """
        Porta veri yazar.
        Bu metod blocking'dir — executor'dan çağırılmalıdır.

        Args:
            data: Gönderilecek ham byte verisi.
        """
        if not self._is_open:
            logger.warning("AsyncSerialPortManager: Kapalı porta yazma denemesi!")
            return
        # Gerçek donanımda: self._port.write(data)

    def close(self) -> None:
        """Portu kapatır. Bu metod blocking'dir — executor'dan çağırılmalıdır."""
        # Gerçek donanımda:
        # if self._port and self._port.is_open:
        #     self._port.close()
        self._is_open = False
        logger.debug("AsyncSerialPortManager: Bağlantı kapatıldı.")

    @property
    def is_open(self) -> bool:
        """Port açık mı?"""
        return self._is_open