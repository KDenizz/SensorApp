# Thread 4: log_queue -> CSV/TXT
import threading
import logging
import csv
from queue import Empty
from typing import Dict, Any, Optional
from pathlib import Path

from core.app_context import AppContext
from core.data_types import AlarmCode

logger = logging.getLogger(__name__)

class DataLogger(threading.Thread):
    """
    [Thread 4] Sistem verilerini disk üzerindeki dosyalara (CSV/TXT) asenkron olarak yazar.
    UI veya Controller tarafından 'log_queue' içerisine bırakılan komut dict'lerini işler.
    CPU'yu bloklamaz, disk I/O işlemlerinin sistemi yavaşlatmasını engeller.
    """

    def __init__(self, context: AppContext) -> None:
        super().__init__(name="DataLogger-Thread")
        self.context = context
        self.daemon = True
        
        self._current_file: Optional[Any] = None
        self._csv_writer: Optional[Any] = None
        self._is_recording: bool = False

    def run(self) -> None:
        """
        Thread'in ana yaşam döngüsü. Kuyruğu dinler ve diske yazma komutlarını işler.
        """
        logger.info("DataLogger thread'i başlatıldı.")

        while not self.context.stop_event.is_set():
            try:
                # 0.1 saniyelik timeout ile kuyruğu bekle. (Graceful shutdown için gerekli)
                log_task: Dict[str, Any] = self.context.log_queue.get(timeout=0.1)
                self._process_task(log_task)
                self.context.log_queue.task_done()
                
            except Empty:
                continue
            except Exception as e:
                logger.error(f"DataLogger döngüsünde beklenmeyen hata: {e}", exc_info=True)

        # Sistem kapanırken açık dosya varsa güvenlice kapat
        self._close_current_file()
        logger.info("DataLogger güvenli şekilde sonlandırıldı.")

    def _process_task(self, task: Dict[str, Any]) -> None:
        """
        Kuyruktan alınan görev sözlüğünü (dict) işler.
        
        Beklenen formatlar:
        - {"cmd": "START", "file_path": "C:/logs/test.csv", "headers": ["Zaman", "P1", "P2", "Debi"]}
        - {"cmd": "LOG", "row": [1.5, 50.0, 45.0, 12.3]}
        - {"cmd": "STOP"}
        """
        cmd = task.get("cmd")

        if cmd == "START":
            self._start_recording(task)
        elif cmd == "LOG":
            self._write_row(task)
        elif cmd == "STOP":
            self._stop_recording()
        else:
            logger.warning(f"DataLogger: Bilinmeyen komut alındı -> {cmd}")

    def _start_recording(self, task: Dict[str, Any]) -> None:
        """Yeni bir kayıt dosyası açar ve (varsa) başlıkları yazar."""
        if self._is_recording:
            logger.warning("DataLogger: Halihazırda bir kayıt sürüyor. Önceki dosya kapatılıyor.")
            self._close_current_file()

        file_path_str = task.get("file_path", "default_log.csv")
        headers = task.get("headers", [])
        
        file_path = Path(file_path_str)
        
        # Dizinin var olduğundan emin ol, yoksa oluştur
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            # newline='' parametresi CSV yazımında boş satırları engellemek için önemlidir
            self._current_file = file_path.open(mode='w', encoding='utf-8', newline='')
            self._csv_writer = csv.writer(self._current_file)
            
            if headers:
                self._csv_writer.writerow(headers)
                
            self._is_recording = True
            logger.info(f"Kayıt başlatıldı: {file_path.absolute()}")
            
        except OSError as e:
            logger.error(f"Dosya oluşturulamadı ({file_path}): {e}")
            # Disk hatası durumunda sisteme alarm verilebilir (UI'da göstermek için)
            # Opsiyonel: self.context.signal_bus.alarm_triggered.emit(int(AlarmCode.LIMIT_EXCEEDED))

    def _write_row(self, task: Dict[str, Any]) -> None:
        """Kayıt açıksa gelen veri satırını dosyaya yazar."""
        if not self._is_recording or self._csv_writer is None:
            # Kayıt aktif değilse veriyi düşür (Drop)
            return
            
        row = task.get("row", [])
        if row:
            try:
                self._csv_writer.writerow(row)
            except Exception as e:
                logger.error(f"Satır yazılırken hata: {e}")

    def _stop_recording(self) -> None:
        """Aktif kaydı durdurur ve dosyayı güvenle kapatır."""
        if self._is_recording:
            self._close_current_file()
            logger.info("Kayıt başarıyla durduruldu ve kaydedildi.")

    def _close_current_file(self) -> None:
        """Açık olan I/O objesini kapatan yardımcı metod."""
        if self._current_file and not self._current_file.closed:
            try:
                self._current_file.flush() # Buffer'daki veriyi diske zorla
                self._current_file.close()
            except Exception as e:
                logger.error(f"Dosya kapatılırken hata: {e}")
                
        self._current_file = None
        self._csv_writer = None
        self._is_recording = False