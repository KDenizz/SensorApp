"""
core/log_setup.py

Merkezi loglama kurulumu — konsol + dönen dosya (RotatingFileHandler).

Amaç:
    Sahada (Oktay senaryosu) konsol penceresi kapandığında hiçbir iz
    kalmaması sorununu çözmek. Tüm modüller zaten logging.getLogger(__name__)
    kullandığı için root logger'ı yapılandırmak yeterlidir — hiçbir modülde
    değişiklik gerekmez.

Kullanım (main.py'nin EN BAŞINDA, Reg.load() ve AppContext()'ten ÖNCE):
    from core.log_setup import setup_logging
    setup_logging()                    # varsayılan: INFO

Ek yardımcılar:
    install_asyncio_exception_handler(loop)  → sessizce ölen task'ları yakalar
    log_port_diagnostics(expected_port)      → bağlantı hatasında port listesi
    enable_modbus_debug()                    → pymodbus frame-seviye DEBUG

Not:
    Bu modül SADECE stdlib kullanır (serial import'u fonksiyon içinde, lazy).
    PyInstaller'a ek 'hiddenimport' gerekmez; main.py import ettiği için
    otomatik pakete girer.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

LOG_DIR_NAME = "logs"
LOG_FILE_NAME = "servoapp.log"
MAX_BYTES = 5 * 1024 * 1024      # 5 MB / dosya
BACKUP_COUNT = 3                 # servoapp.log + .1 + .2 + .3 → en fazla ~20 MB
LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured: bool = False


# ---------------------------------------------------------------------------
# Yol Tespiti (AppContext ile aynı mantık — exe yanı / proje kökü)
# ---------------------------------------------------------------------------

def _base_path() -> Path:
    """Exe modunda sys.executable yanı, dev modunda proje kökü."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    # core/log_setup.py → core/ → proje kökü
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Ana Kurulum
# ---------------------------------------------------------------------------

def setup_logging(level: int = logging.INFO) -> Optional[Path]:
    """
    Root logger'ı konsol + dönen dosya handler'ı ile yapılandırır.

    - Konsol çıkışı aynen korunur (dev deneyimi değişmez).
    - Dosya: logs/servoapp.log, 5 MB × 3 rotasyon, UTF-8.
    - sys.excepthook kurulur: yakalanmamış hatalar traceback ile dosyaya düşer.
    - İkinci kez çağrılırsa hiçbir şey yapmaz (idempotent) — /settings restart
      senaryosunda çift handler / çift satır oluşmaz.

    :param level: Dosya ve konsol için minimum seviye (varsayılan INFO).
    :return: Log dosyasının tam yolu; dosya açılamadıysa None.
    """
    global _configured

    log_path: Optional[Path] = _base_path() / LOG_DIR_NAME / LOG_FILE_NAME

    if _configured:
        return log_path

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    # --- Konsol ---
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    # --- Dosya (yazma izni yoksa program AYAKTA kalır, sadece uyarır) ---
    file_handler: Optional[logging.Handler] = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:
        log_path = None
        root.warning(
            f"Log dosyası açılamadı ({e}) — sadece konsola yazılacak. "
            f"Programın yazma izni olan bir dizinde çalıştığından emin olun."
        )

    _install_excepthook()
    _configured = True

    boot = logging.getLogger(__name__)
    boot.info("=" * 62)
    boot.info(f"Servo Valf Kontrol Sistemi — loglama başlatıldı (seviye={logging.getLevelName(level)})")
    if log_path is not None:
        boot.info(f"Log dosyası: {log_path}")
    boot.info(f"Python {sys.version.split()[0]} | frozen={getattr(sys, 'frozen', False)}")
    boot.info("=" * 62)

    return log_path


# ---------------------------------------------------------------------------
# Yakalanmamış Hata Kancaları
# ---------------------------------------------------------------------------

def _install_excepthook() -> None:
    """
    sys.excepthook: main thread'de yakalanmamış her hata, program çökmeden
    hemen önce tam traceback ile log dosyasına yazılır.
    KeyboardInterrupt normal davranışına bırakılır (Ctrl+C spam yaratmasın).
    """

    def _hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logging.getLogger("uncaught").critical(
            "YAKALANMAMIŞ HATA — program sonlanıyor.",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _hook


def install_asyncio_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """
    Event loop'a exception handler kurar.

    Bir asyncio.Task içinde yakalanmayan hata oluşursa (örn. HALReader
    beklenmedik şekilde ölürse) traceback ANINDA dosyaya düşer — kapanışta
    asyncio.gather'ın toplamasını beklemez.

    Kullanım (main() içinde, loop alındıktan hemen sonra):
        loop = asyncio.get_running_loop()
        install_asyncio_exception_handler(loop)
    """

    def _handler(_loop: asyncio.AbstractEventLoop, ctx: Dict[str, Any]) -> None:
        exc = ctx.get("exception")
        msg = ctx.get("message", "")
        task = ctx.get("task") or ctx.get("future")
        task_name = "?"
        if task is not None:
            get_name = getattr(task, "get_name", None)
            if callable(get_name):
                task_name = get_name()

        log = logging.getLogger("asyncio.unhandled")
        if exc is not None:
            log.error(
                f"Task '{task_name}' içinde yakalanmamış hata: {msg}",
                exc_info=exc,
            )
        else:
            log.error(f"Asyncio hatası (task='{task_name}'): {msg} | ctx={ctx!r}")

    loop.set_exception_handler(_handler)


# ---------------------------------------------------------------------------
# Bağlantı Teşhisi (Oktay senaryosu)
# ---------------------------------------------------------------------------

def log_port_diagnostics(expected_port: str) -> None:
    """
    Modbus bağlantısı kurulamadığında çağrılır: sistemdeki TÜM seri portları
    açıklamalarıyla loglar. 'COM7 yok ama COM3 ve COM9 var' gibi kök nedenler
    tek bakışta görünür.

    :param expected_port: hardware.yaml'da yapılandırılmış port (örn. 'COM7')
    """
    log = logging.getLogger("port_diag")
    try:
        from serial.tools import list_ports  # lazy import — pyserial zaten bağımlılık
        ports = list(list_ports.comports())
    except Exception as e:
        log.error(f"COM port listesi alınamadı: {e}")
        return

    if not ports:
        log.error(
            f"Sistemde HİÇ seri port bulunamadı (beklenen: {expected_port}). "
            f"USB-RS485 çevirici takılı mı? Sürücü (FTDI/CH340) kurulu mu?"
        )
        return

    names = [p.device for p in ports]
    log.error(
        f"'{expected_port}' portuna bağlanılamadı. "
        f"Sistemde bulunan portlar: {', '.join(names)}"
    )
    for p in ports:
        log.info(f"  → {p.device}: {p.description}")

    if expected_port not in names:
        log.error(
            f"'{expected_port}' bu sistemde YOK — hardware.yaml'daki port adı "
            f"güncellenmeli (kurulum sayfası: http://localhost:8000/setup)."
        )
    else:
        log.error(
            f"'{expected_port}' sistemde mevcut ama açılamadı — port başka bir "
            f"uygulama (örn. C# test aracı, önceki backend süreci) tarafından "
            f"kilitlenmiş olabilir."
        )


# ---------------------------------------------------------------------------
# Derin Modbus Teşhisi (opsiyonel — hardware.yaml: debug_modbus: true)
# ---------------------------------------------------------------------------

def enable_modbus_debug() -> None:
    """
    pymodbus logger'ını DEBUG'a çeker: gönderilen/alınan her frame dosyaya
    yazılır. 'Cihaz hiç yanıt vermiyor mu, bozuk yanıt mı geliyor?' sorusunu
    kesin cevaplar. Log dosyasını hızlı büyüttüğü için normalde KAPALI tutun.
    """
    logging.getLogger("pymodbus").setLevel(logging.DEBUG)
    logging.getLogger(__name__).warning(
        "pymodbus DEBUG modu AÇIK — frame seviyesinde loglama yapılıyor, "
        "log dosyası hızlı büyüyebilir. Teşhis bitince debug_modbus: false yapın."
    )