"""
hal/data_logger.py

[HAL Layer] Sistem verilerini disk üzerindeki CSV dosyalarına asenkron olarak yazan görev.

Migration Notları (threading.Thread → asyncio.Task):
    1. `threading.Thread` subclass'ı             → Düz class. `run()` async def oldu.
    2. `log_queue.get(timeout=0.1)` + `Empty`    → `asyncio.wait_for(queue.get(), timeout=0.1)` + `asyncio.TimeoutError`
    3. `log_queue.task_done()`                   → `log_queue.task_done()` (aynı API)
    4. Senkron `open()` + `csv.writer`           → `aiofiles` + `asyncio.to_thread` ile disk I/O (non-blocking)
    5. `signal_bus.alarm_triggered.emit(...)`    → `await context.broadcaster.publish("ALARM_TRIGGERED", ...)`

Tasarım Notu (Disk I/O Stratejisi):
    CSV satır yazma işlemi yüksek frekanslı (200Hz) olabileceğinden
    aiofiles açık bir dosya handle'ı üzerinde sürekli flush yapmak yerine,
    satırları bellekte (in-memory buffer) biriktirir ve belirli aralıklarla
    (FLUSH_INTERVAL_SEC) diske aktar. Bu yaklaşım disk I/O sürtünmesini azaltır.

Mimari Kural:
    - Bu katman Controller veya State'e doğrudan DOKUNMAZ.
    - Komut yalnızca context.log_queue'dan okunur.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class DataLogger:
    """
    [HAL Layer] `log_queue`'yu dinleyen asenkron veri kayıt servisi.

    Kabul edilen kuyruk formatları:
        {"cmd": "START", "file_path": "logs/test.csv", "headers": ["t", "P1", "P2"]}
        {"cmd": "LOG",   "row": [1.5, 50.0, 45.0]}
        {"cmd": "STOP"}

    Kullanım (main.py içinde):
        data_logger = DataLogger(context)
        asyncio.create_task(data_logger.run())
    """

    # Bellekteki satırları her N saniyede bir diske yaz (disk I/O optimizasyonu)
    FLUSH_INTERVAL_SEC: float = 1.0
    # Tek seferde en fazla kaç satır buffer'a alınır
    MAX_BUFFER_SIZE: int = 1000
    # Kuyruk boşsa bu kadar bekle
    _QUEUE_TIMEOUT: float = 0.1

    def __init__(self, context: "AppContext") -> None:
        self.context = context

        self._file_path: Optional[Path] = None
        self._headers: List[str] = []
        self._row_buffer: List[List[Any]] = []
        self._is_recording: bool = False
        self._last_flush: float = 0.0

    # ------------------------------------------------------------------
    # Ana Döngü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        asyncio.Task olarak çalışan ana logger döngüsü.
        stop_event set edildiğinde buffer'ı flush eder ve kapatır.
        """
        import time
        self._last_flush = time.monotonic()

        logger.info("DataLogger görevi başlatıldı.")

        while not self.context.stop_event.is_set():
            try:
                log_task: Dict[str, Any] = await asyncio.wait_for(
                    self.context.log_queue.get(),
                    timeout=self._QUEUE_TIMEOUT
                )
                await self._process_task(log_task)
                self.context.log_queue.task_done()

            except asyncio.TimeoutError:
                # Zaman aşımı → periyodik flush zamanı geldiyse yaz
                await self._maybe_flush()
                continue
            except Exception as e:
                logger.error(f"DataLogger döngüsünde beklenmeyen hata: {e}", exc_info=True)

        # Sistem kapanırken açık dosya ve buffer varsa güvenle kapat
        await self._force_flush_and_close()
        logger.info("DataLogger güvenli şekilde sonlandırıldı.")

    # ------------------------------------------------------------------
    # Görev İşleyici
    # ------------------------------------------------------------------

    async def _process_task(self, task: Dict[str, Any]) -> None:
        """
        Kuyruktan alınan görev sözlüğünü yönlendirir.

        Args:
            task: {"cmd": ..., ...} formatında komut sözlüğü.
        """
        cmd = task.get("cmd")

        if cmd == "START":
            await self._start_recording(task)
        elif cmd == "LOG":
            self._buffer_row(task)           # Buffer'a at (I/O yok, senkron OK)
            await self._maybe_flush()        # Gerekirse diske yaz
        elif cmd == "STOP":
            await self._stop_recording()
        else:
            logger.warning(f"DataLogger: Bilinmeyen komut alındı → {cmd}")

    # ------------------------------------------------------------------
    # Kayıt Yönetimi
    # ------------------------------------------------------------------

    async def _start_recording(self, task: Dict[str, Any]) -> None:
        """
        Yeni bir CSV dosyası açar ve (varsa) başlık satırını yazar.
        Eğer kayıt devam ediyorsa önce mevcut dosyayı kapatır.
        """
        if self._is_recording:
            logger.warning("DataLogger: Önceki kayıt hâlâ açık. Kapatılıyor...")
            await self._force_flush_and_close()

        file_path_str: str = task.get("file_path", "default_log.csv")
        self._headers = task.get("headers", [])
        self._file_path = Path(file_path_str)
        self._row_buffer = []

        try:
            # Dizini oluştur (blocking ama tek seferlik, executor'a gerek yok)
            await asyncio.to_thread(self._file_path.parent.mkdir, parents=True, exist_ok=True)

            # Başlık satırını diske yaz
            if self._headers:
                await self._write_rows_to_disk([self._headers], mode="w")
            else:
                # Boş dosyayı oluştur
                await asyncio.to_thread(self._file_path.touch)

            self._is_recording = True
            logger.info(f"Kayıt başlatıldı: {self._file_path.absolute()}")

        except OSError as e:
            logger.error(f"Dosya oluşturulamadı ({self._file_path}): {e}")
            from core.data_types import AlarmCode
            await self.context.broadcaster.publish(
                "ALARM_TRIGGERED",
                {"code": int(AlarmCode.LIMIT_EXCEEDED), "reason": f"Log dosyası açılamadı: {e}"}
            )

    def _buffer_row(self, task: Dict[str, Any]) -> None:
        """Gelen veri satırını bellekteki buffer'a ekler."""
        if not self._is_recording:
            return  # Kayıt aktif değilse düşür

        row: List[Any] = task.get("row", [])
        if row:
            self._row_buffer.append(row)

            # Buffer doluysa zorunlu flush sinyali ver (bir sonraki maybe_flush'ta yazılır)
            if len(self._row_buffer) >= self.MAX_BUFFER_SIZE:
                logger.debug(f"Buffer doldu ({self.MAX_BUFFER_SIZE} satır), erken flush tetikleniyor.")

    async def _stop_recording(self) -> None:
        """Aktif kaydı durdurur, buffer'ı diske yazar ve dosyayı kapatır."""
        if self._is_recording:
            await self._force_flush_and_close()
            logger.info("Kayıt başarıyla durduruldu ve kaydedildi.")

    # ------------------------------------------------------------------
    # Disk I/O
    # ------------------------------------------------------------------

    async def _maybe_flush(self) -> None:
        """
        FLUSH_INTERVAL_SEC geçtiyse veya buffer MAX_BUFFER_SIZE'a ulaştıysa
        buffer'ı diske yazar.
        """
        import time
        if not self._is_recording or not self._row_buffer:
            return

        elapsed = time.monotonic() - self._last_flush
        if elapsed >= self.FLUSH_INTERVAL_SEC or len(self._row_buffer) >= self.MAX_BUFFER_SIZE:
            await self._flush_buffer()

    async def _flush_buffer(self) -> None:
        """
        Bellekteki satırları diske yazar ve buffer'ı temizler.
        Disk yazma `asyncio.to_thread` ile event loop'u bloklamadan yapılır.
        """
        if not self._row_buffer or self._file_path is None:
            return

        import time
        rows_to_write = self._row_buffer[:]
        self._row_buffer = []
        self._last_flush = time.monotonic()

        try:
            await self._write_rows_to_disk(rows_to_write, mode="a")
            logger.debug(f"DataLogger: {len(rows_to_write)} satır diske yazıldı.")
        except Exception as e:
            logger.error(f"Disk yazma hatası: {e}")
            # Yazılamayan satırları geri al (veri kaybını önle)
            self._row_buffer = rows_to_write + self._row_buffer

    async def _write_rows_to_disk(self, rows: List[List[Any]], mode: str = "a") -> None:
        """
        CSV satırlarını thread pool'da (non-blocking) diske yazar.

        Args:
            rows:  Yazılacak satır listesi.
            mode:  'w' (üzerine yaz / başlık için) veya 'a' (ekle).
        """
        file_path = self._file_path

        def _sync_write() -> None:
            """Thread pool'da çalışacak senkron yazma fonksiyonu."""
            with file_path.open(mode=mode, encoding="utf-8", newline="") as f:
                writer = csv.writer(f)
                writer.writerows(rows)

        await asyncio.to_thread(_sync_write)

    async def _force_flush_and_close(self) -> None:
        """Buffer'da kalan tüm satırları diske yazar ve kaydı kapatır."""
        if self._is_recording and self._row_buffer:
            await self._flush_buffer()

        self._file_path = None
        self._headers = []
        self._row_buffer = []
        self._is_recording = False