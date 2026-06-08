"""
modbus_simulator.py  —  Gerçekçi Modbus RTU Slave Simülatörü
pymodbus 3.8+ uyumlu

Register Haritası (C# kodundan doğrulanmış):
    WRITE (FC06):
        addr=0  → mode_select         (0=dur, 1=kalibrasyon, 2=sinyal, 3=dijital adım, ...)
        addr=1  → total_turns         (hedef toplam tur)
        addr=2  → proportional_signal (0-1000)
        addr=3  → target_step         (hedef adım)
        addr=5  → fast_open_close     (0=kapat, 1=aç)
        addr=8  → calibration_direction (0=eksi, 1=artı)

    READ (FC03):
        addr=9  → current_position    (signed int16)
        addr=10 → current_load        (signed int16)
        addr=11 → calibration_status  (0=kalibre değil, 1=kalibre)
"""

import argparse
import logging
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
# Register Adresleri — Gerçek Donanım Haritası
# ---------------------------------------------------------------------------

# Yazılabilir (FC06 Write) — Holding Register
HR_MODE_SELECT          = 0
HR_TOTAL_TURNS          = 1
HR_PROPORTIONAL_SIGNAL  = 2
HR_TARGET_STEP          = 3
HR_FAST_OPEN_CLOSE      = 5
HR_CALIBRATION_DIR      = 8

# Okunabilir (FC03 Read) — Holding Register (aynı blok, farklı adresler)
HR_CURRENT_POSITION     = 9
HR_CURRENT_LOAD         = 10
HR_CALIBRATION_STATUS   = 11

# mode_select değerleri
MODE_STOP       = 0
MODE_CALIBRATE  = 1
MODE_SIGNAL     = 2   # Oransal sinyal modu
MODE_DIGITAL    = 3   # Dijital adım modu
#MODE_RESERVED_4 = 4
#MODE_RESERVED_5 = 5

# fast_open_close değerleri
FAST_CLOSE = 0
FAST_OPEN  = 1
# Yeni register adresleri (C# test aracından doğrulanmış)
#HR_SIGNAL_LOST_FLAG     = 6
HR_SIGNAL_LOSS_ACTION   = 7
HR_SEATING_LOAD         = 12
HR_BACKOFF_OFFSET       = 13
HR_PID_SETPOINT         = 14
HR_PID_KP               = 15
HR_PID_KI               = 16
HR_PID_KD               = 17
HR_PID_DEADBAND         = 18
HR_ADC_OFFSET           = 19
HR_ADC_GAIN             = 20

# Yeni mod sabitleri
MODE_TTL    = 4   # fast open/close — artık mode_select üzerinden
MODE_PID    = 5   # dahili PID
MODE_SMART  = 6   # akıllı tork

# ---------------------------------------------------------------------------
# Motor Durumu
# ---------------------------------------------------------------------------

class MotorState:
    def __init__(self, step_res: int, max_rev: int):
        self.lock     = threading.Lock()
        self.step_res = step_res   # PPR (pulse per revolution)
        self.max_rev  = max_rev    # Maksimum tur sayısı

        # Tek pozisyon kaynağı: toplam adım (float)
        self._total_steps_f: float = 0.0

        self.target_total_steps_f: float = 0.0

        self.speed_steps_per_sec: float = 200.0  # adım/sn

        self.is_moving:      bool  = False
        self.is_calibrated:  bool  = False
        self.calibrating:    bool  = False
        self.load:           int   = 0   # signed int16 — yük/tork

    @property
    def current_position(self) -> int:
        """Mevcut pozisyon — signed int16 olarak döner."""
        return int(self._total_steps_f)

    @property
    def max_total_steps(self) -> float:
        return float(self.max_rev * self.step_res)

    def set_target_steps(self, total_steps: float) -> None:
        """Hedef adımı sınırlar içinde günceller."""
        self.target_total_steps_f = max(0.0, min(self.max_total_steps, total_steps))


# ---------------------------------------------------------------------------
# Simülasyon Döngüsü
# ---------------------------------------------------------------------------

def simulation_loop(context: ModbusServerContext, state: MotorState) -> None:
    logger.info("Simülasyon döngüsü başladı.")

    UPDATE_DELAY = 0.05   # 50ms = 20 Hz
    tick         = 0

    while True:
        slave = context[0x00]

        # ── 1. Tüm Holding Register'ları oku ─────────────────────────
        hr = slave.getValues(3, 0, count=21)  # 0-20 arası tüm register'ları oku (yeni eklenenler dahil)

        mode      = hr[HR_MODE_SELECT]
        turns     = hr[HR_TOTAL_TURNS]
        step      = hr[HR_TARGET_STEP]
        fast      = hr[HR_FAST_OPEN_CLOSE]
        cal_dir   = hr[HR_CALIBRATION_DIR]

        with state.lock:

            # ── 2. Komutları her döngüde değerlendir ─────────────────
            # Değişim tespiti yok: her döngüde mevcut register değeri okunur.
            # fast_open_close önceliklidir.

            if fast == FAST_OPEN:
                if state.target_total_steps_f != state.max_total_steps:
                    state.set_target_steps(state.max_total_steps)
                    state.calibrating = False
                    logger.info(f"[FAST] TAM AÇ — hedef: {state.max_rev} tur")
                    
            elif mode == MODE_STOP:
                # fast_open_close aktifse önce sıfırla
                if fast == FAST_OPEN:
                    slave.setValues(3, HR_FAST_OPEN_CLOSE, [0])
                    logger.info("[MODE] STOP — fast_open_close sıfırlandı.")
                state.set_target_steps(state._total_steps_f)
                state.calibrating = False
                logger.info("[MODE] STOP — mevcut konumda kal.")

            elif mode == MODE_CALIBRATE:
                if not state.calibrating and not state.is_calibrated:
                    if cal_dir == 0:
                        state.set_target_steps(0.0)
                        logger.info("[MODE] KALİBRASYON — sıfıra gidiliyor (yön=eksi).")
                    else:
                        state.set_target_steps(state.max_total_steps)
                        logger.info("[MODE] KALİBRASYON — maksimuma gidiliyor (yön=artı).")
                    state.calibrating   = True
                    state.is_calibrated = False

            elif mode == MODE_DIGITAL:
                # addr 3 = mutlak toplam tick (turns/addr1 KULLANILMAZ — çift sayım giderildi)
                target = float(step)
                if state.target_total_steps_f != target:
                    state.set_target_steps(target)
                    logger.info(f"[MOD3] Hedef (mutlak tick): {int(target)}")

            elif mode == MODE_SIGNAL:
                signal_val = hr[HR_PROPORTIONAL_SIGNAL]
                ratio      = max(0, min(1000, signal_val)) / 1000.0
                target     = ratio * state.max_total_steps
                state.set_target_steps(target)

            elif mode == MODE_TTL:
                # OPEN_FULL / CLOSE_FULL: hal_writer hedefi TARGET_STEP'e yazıp MODE=3 yapar.
                # Simülatörde Mod 4 seçilirse: fast_open_close register'ına bak.
                if fast == FAST_OPEN:
                    state.set_target_steps(state.max_total_steps)
                    logger.info("[MOD4] TAM AÇ")
                else:
                    state.set_target_steps(0.0)
                    logger.info("[MOD4] TAM KAPAT")

            elif mode == MODE_PID:
                # PID: firmware halleder. Simülatörde mevcut konumda kal.
                # Setpoint register'ını oku — sadece log amaçlı.
                sp_raw = hr[HR_PID_SETPOINT]
                sp_bar = sp_raw / 100.0
                if tick % 40 == 0:
                    logger.info(f"[MOD5/PID] Setpoint: {sp_bar:.2f} bar — simülatörde pozisyon sabit.")

            elif mode == MODE_SMART:
                logger.debug("[MOD6] Akıllı tork — simülatörde pasif.")

            # ── 6. Pozisyonu güncelle ─────────────────────────────────
            current = state._total_steps_f
            target  = state.target_total_steps_f
            delta   = target - current
            max_move = state.speed_steps_per_sec * UPDATE_DELAY

            if abs(delta) < 1.0:
                state._total_steps_f = target
                was_moving      = state.is_moving
                state.is_moving = False
                state.load      = 3  # Boşta düşük yük

                if was_moving:
                    logger.info(f"[POS] Hedefe ulaşıldı: {int(target)} adım")

                # Kalibrasyon tamamlandı mı?
                if state.calibrating:
                    if cal_dir == 0 and state._total_steps_f < 1.0:
                        state._total_steps_f = 0.0
                        state.is_calibrated  = True
                        state.calibrating    = False
                        logger.info("[CAL] Kalibrasyon tamamlandı (sıfır noktası).")
                        # mode_select'i STOP'a çek
                        slave.setValues(3, HR_MODE_SELECT, [MODE_STOP])
                    elif cal_dir == 1 and state._total_steps_f >= state.max_total_steps - 1:
                        state.is_calibrated  = True
                        state.calibrating    = False
                        logger.info("[CAL] Kalibrasyon tamamlandı (maksimum nokta).")
                        slave.setValues(3, HR_MODE_SELECT, [MODE_STOP])

            else:
                state.is_moving = True
                move = min(abs(delta), max_move) * (1 if delta > 0 else -1)
                new_pos = max(0.0, min(state.max_total_steps, state._total_steps_f + move))
                state._total_steps_f = new_pos

                # Hareket sırasında yük simülasyonu
                state.load = int(min(80, 15 + abs(move) / max_move * 60))

            # ── 7. Çıkış değerlerini hazırla ─────────────────────────
            pos_out   = state.current_position  # signed int16
            load_out  = state.load              # signed int16
            calib_out = 1 if state.is_calibrated else 0

            # signed int16 → uint16 dönüşümü (negatif değerler için)
            if pos_out < 0:
                pos_out = pos_out + 65536
            if load_out < 0:
                load_out = load_out + 65536

        # ── 8. Register'lara yaz (lock dışında) ──────────────────────
        slave.setValues(3, HR_CURRENT_POSITION,   [pos_out])
        slave.setValues(3, HR_CURRENT_LOAD,        [load_out])
        slave.setValues(3, HR_CALIBRATION_STATUS,  [calib_out])

        # ── 9. Periyodik log ──────────────────────────────────────────
        if tick % 20 == 0:
            with state.lock:
                pos_display = state._total_steps_f
                moving_str  = "EVET" if state.is_moving else "HAYIR"
                calib_str   = "✔" if state.is_calibrated else "✘"
            logger.info(
                f"POS: {pos_display:8.1f} adım  |  "
                f"Yük: {state.load:3d}  "
                f"Hareket: {moving_str}  "
                f"Kalibre: {calib_str}  "
                f"[Mod={mode} Fast={fast} Hedef={state.target_total_steps_f:.0f} "
                f"PID_SP={hr[HR_PID_SETPOINT]/100.0:.2f}bar ADC_gain={hr[HR_ADC_GAIN]}]"
            )

        tick += 1
        time.sleep(UPDATE_DELAY)


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def build_context() -> ModbusServerContext:
    """
    32 adet Holding Register (addr 0–31) içeren datastore oluşturur.
    Addr 0–20 aktif kullanımda; 21–31 rezerv.
    """
    device = ModbusDeviceContext(
        hr=ModbusSequentialDataBlock(0, [0] * 32),
    )
    return ModbusServerContext(device, single=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Servo Vana Modbus RTU Simülatörü")
    parser.add_argument("--port",     default="COM8",   help="Seri port (varsayılan: COM8)")
    parser.add_argument("--slave-id", type=int, default=1, help="Modbus Slave ID")
    parser.add_argument("--baudrate", type=int, default=230400, help="Baud rate (varsayılan: 230400)")
    parser.add_argument("--max-rev",  type=int, default=10,  help="Maksimum tur sayısı")
    parser.add_argument("--step-res", type=int, default=1000, help="Adım çözünürlüğü (PPR)")
    args = parser.parse_args()

    logger.info(
        f"Simülatör başlatılıyor...\n"
        f"  Port={args.port}  SlaveID={args.slave_id}  "
        f"Baud={args.baudrate}  MaxTur={args.max_rev}  PPR={args.step_res}\n"
        f"  Yazma  (FC06): addr=0 mode | addr=1 turns | addr=3 step | "
        f"addr=5 fast | addr=8 cal_dir\n"
        f"  Okuma  (FC03): addr=9 pos  | addr=10 load | addr=11 calib_status"
    )

    state   = MotorState(step_res=args.step_res, max_rev=args.max_rev)
    context = build_context()

    threading.Thread(
        target=simulation_loop,
        args=(context, state),
        daemon=True,
        name="SimLoop",
    ).start()

    logger.info(f"{args.port} dinleniyor... (Ctrl+C ile durdur)")

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
        logger.info("Simülatör durduruldu.")