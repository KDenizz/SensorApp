"""
modbus_simulator.py

Gerçek donanım olmadan sistemi test etmek için Modbus RTU Slave simülatörü.
pymodbus 3.8+ API'siyle uyumludur.

Çalışma mantığı:
    - COM4'te (veya başka bir portta) Modbus Slave olarak dinler.
    - Backend COM3'ten Master olarak bağlanır — com0com ile COM3↔COM4 köprüsü kurulur.
    - Input Register (3x) alanlarına periyodik sahte veri yazar.
    - Holding Register (4x) yazma komutlarını kabul eder ve terminale basar.
    - Pozisyon sinüs dalgasıyla yavaşça salınır — grafiklerde hareket görünür.

Kurulum:
    pip install pymodbus

Kullanım:
    python modbus_simulator.py                  # varsayılan COM4, slave_id=1
    python modbus_simulator.py --port COM7      # farklı port
    python modbus_simulator.py --port COM4 --slave-id 2

com0com kurulumu (Windows):
    https://sourceforge.net/projects/com0com/
    COM3 <-> COM4 (veya COM6 <-> COM7 gibi) çifti oluştur.
    Backend hardware.yaml → port: "COM3"   (veya COM6)
    Bu simülatör  → port: "COM4"           (veya COM7)
"""

import argparse
import logging
import math
import threading
import time

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartSerialServer
from pymodbus.framer import FramerType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("simulator")

# ---------------------------------------------------------------------------
# Register Adresleri (0-tabanlı pymodbus)
# modbus_registers.yaml ile eşleşmeli
# ---------------------------------------------------------------------------

# Input Registers (3x) — FC 04 ile okunur
IR_STATUS_WORD        = 0   # 30001
IR_CURRENT_POS_REV    = 1   # 30002
IR_CURRENT_POS_STEP   = 2   # 30003
IR_EXTERNAL_SIGNAL_MA = 3   # 30004  (ham: mA x 100, örn: 1200 = 12.00mA)
IR_MOTOR_TORQUE_PCT   = 4   # 30005
# GEÇİCİ: Basınç sensörü register adresleri
IR_PRESSURE_INLET  = 5   # 30006 — GEÇİCİ
IR_PRESSURE_OUTLET = 6   # 30007 — GEÇİCİ

# Holding Registers (4x) — FC 03/06/16 ile okunur/yazılır
HR_MODE_SELECT        = 0   # 40001
HR_CONTROL_WORD       = 1   # 40002
HR_TARGET_REV         = 2   # 40003
HR_TARGET_STEP        = 3   # 40004

# ---------------------------------------------------------------------------
# Veri Bloğu Oluştur
# ---------------------------------------------------------------------------

def build_context() -> ModbusServerContext:
    """
    Modbus slave veri bloğunu oluşturur.
    Tüm register'lar sıfırla başlar, simülatör döngüsü günceller.
    """
    device = ModbusDeviceContext(
        ir=ModbusSequentialDataBlock(0, [0] * 10),
        hr=ModbusSequentialDataBlock(0, [0] * 25),
    )
    # pymodbus 3.8+: 'slaves' kwarg kaldırıldı, pozisyonel argüman
    return ModbusServerContext(device, single=True)


# ---------------------------------------------------------------------------
# Simülasyon Döngüsü
# ---------------------------------------------------------------------------

def simulation_loop(context: ModbusServerContext) -> None:
    """
    Arka planda çalışan simülasyon döngüsü.
    Her 100ms'de Input Register değerlerini günceller.

    Simüle edilen davranış:
        - Pozisyon (tur + adım): 0-10-0 sinüs dalgası, 30 saniyelik periyot
        - Dis sinyal (mA): 4-20 mA arasi salinım  (ham: 400-2000)
        - Tork (%): hareket hizina göre 20-80 arasi
        - Status word:
            Bit0 = Kalibrasyon Tamam  (5. saniyeden itibaren 1)
            Bit1 = Hareket Halinde    (her zaman 1)
            Bit2 = Sinyal Hatasi      (her zaman 0)
    """
    logger.info("Simülasyon döngüsü başladı.")

    start_time   = time.monotonic()
    PERIOD       = 30.0
    MAX_REV      = 10
    STEP_RES     = 1000
    UPDATE_DELAY = 0.1

    tick = 0

    while True:
        elapsed = time.monotonic() - start_time

        # Pozisyon: sinüs dalgası
        ratio    = (math.sin(2 * math.pi * elapsed / PERIOD) + 1) / 2
        total    = ratio * MAX_REV
        pos_rev  = int(total)
        pos_step = int((total - pos_rev) * STEP_RES)

        # Dis sinyal: 4-20 mA
        signal_physical = 4.0 + ratio * 16.0
        signal_raw      = int(signal_physical * 100)

        # Tork
        speed  = abs(math.cos(2 * math.pi * elapsed / PERIOD))
        torque = int(20 + speed * 60)

        # Status Word
        cal_done    = 1 if elapsed > 5.0 else 0
        status_word = (cal_done << 0) | (1 << 1)

        # GEÇİCİ: Basınç sensörü register'ları
        p1_raw = int((8.0 + ratio * 4.0) * 100)   # 8.00–12.00 bar arası
        p2_raw = int((p1_raw/100 - ratio*3) * 100) # P1'den düşük
    

        # Input Register'lara yaz
        slave = context[0x00]
        slave.setValues(4, IR_STATUS_WORD,        [status_word])
        slave.setValues(4, IR_CURRENT_POS_REV,    [pos_rev])
        slave.setValues(4, IR_CURRENT_POS_STEP,   [pos_step])
        slave.setValues(4, IR_EXTERNAL_SIGNAL_MA, [signal_raw])
        slave.setValues(4, IR_MOTOR_TORQUE_PCT,   [torque])
        # GEÇİCİ: Basınç sensörü register'ları
        slave.setValues(4, IR_PRESSURE_INLET,  [p1_raw])
        slave.setValues(4, IR_PRESSURE_OUTLET, [p2_raw])

        # Holding Register'lara gelen yazmaları her 2 saniyede logla
        if tick % 20 == 0:
            hr       = slave.getValues(3, HR_MODE_SELECT, count=4)
            mode     = hr[0]
            ctrl     = hr[1]
            tgt_rev  = hr[2]
            tgt_step = hr[3]

            logger.info(
                f"Tur={pos_rev:2d}  Adim={pos_step:04d}  "
                f"Sinyal={signal_physical:.2f}mA  Tork=%{torque:2d}  |  "
                f"[Backend] Mod={mode}  Cmd={ctrl}  "
                f"HedefTur={tgt_rev}  HedefAdim={tgt_step}"
            )

        tick += 1
        time.sleep(UPDATE_DELAY)


# ---------------------------------------------------------------------------
# Ana Giris
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Modbus RTU Slave Simulatörü — Servo Vana Test Araci"
    )
    parser.add_argument(
        "--port",
        default="COM8",
        help="Seri port (varsayilan: COM8). com0com ciftinin slave tarafi.",
    )
    parser.add_argument(
        "--slave-id",
        type=int,
        default=1,
        help="Modbus Slave ID (varsayilan: 1). hardware.yaml slave_id ile eslesmelidir.",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=115200,
        help="Seri port hizi (varsayilan: 115200).",
    )
    args = parser.parse_args()

    logger.info(
        f"Modbus RTU Slave Simulatörü baslatiliyor...\n"
        f"  Port     : {args.port}\n"
        f"  Slave ID : {args.slave_id}\n"
        f"  Baudrate : {args.baudrate}"
    )

    context = build_context()

    sim_thread = threading.Thread(
        target=simulation_loop,
        args=(context,),
        daemon=True,
        name="SimLoop",
    )
    sim_thread.start()

    logger.info(f"{args.port} portunda dinleniyor... (Durdurmak icin Ctrl+C)")

    StartSerialServer(
        context,
        framer=FramerType.RTU,
        port=args.port,
        baudrate=args.baudrate,
        bytesize=8,
        parity="N",
        stopbits=1,
        timeout=1,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Simulatör durduruldu.")