"""
hal/modbus_config.py

Modbus register haritasını (modbus_registers.yaml) yükleyip
tip güvenli sabitler olarak sunan konfigürasyon modülü.

Kullanım:
    from hal.modbus_config import Reg, RegisterDef

    # Adres okuma:
    Reg.INPUT.STATUS_WORD.address       → 0  (30001)
    Reg.HOLDING.CONTROL_WORD.address    → 1  (40002)

    # Scaling ile ham değeri fiziksel değere çevirme:
    raw = 1200
    physical = Reg.INPUT.EXTERNAL_SIGNAL_MA.scale(raw)  → 12.00

    # HALReader toplu okuma için:
    Reg.INPUT.block()   → (address=0, count=5)

    # Sadece belirli bir register'ın access tipini kontrol etme:
    Reg.INPUT.STATUS_WORD.is_readonly   → True
    Reg.HOLDING.CONTROL_WORD.is_readonly → False
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Tuple

import yaml
import sys

logger = logging.getLogger(__name__)


if getattr(sys, "frozen", False):
    _DEFAULT_YAML_PATH = (
        Path(sys.executable).parent
        / "config"
        / "modbus_registers.yaml"
    )
else:
    _DEFAULT_YAML_PATH = (
        Path(__file__).resolve().parent.parent
        / "config"
        / "modbus_registers.yaml"
    )


# ---------------------------------------------------------------------------
# Veri Yapısı
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RegisterDef:
    """
    Tek bir Modbus register'ını tanımlayan değişmez (immutable) veri yapısı.

    Alanlar:
        name     : YAML anahtar adı (örn: 'control_word')
        address  : 0-tabanlı pymodbus adresi
        access   : 'rw' (Holding) veya 'ro' (Input)
        scaling  : Ham değeri fiziksel değere çevirmek için bölen (varsayılan 1)
        description : Parametrenin kısa açıklaması
        mode     : İlgili servo modu
    """
    name: str
    address: int
    access: str
    scaling: int
    description: str
    mode: str
    signed: bool = False  

    @property
    def is_readonly(self) -> bool:
        """Input Register (salt okunur) mu?"""
        return self.access == "ro"

    def scale(self, raw_value: int) -> float:
        """
        Ham UInt16 değeri fiziksel değere çevirir.

        Örn: raw=1200, scaling=100  →  12.00 (mA)
             raw=75,   scaling=1    →  75.0  (%)
        """
        if self.scaling == 1:
            return float(raw_value)
        return round(raw_value / self.scaling, 4)

    def unscale(self, physical_value: float) -> int:
        """
        Fiziksel değeri register'a yazılacak ham UInt16 değerine çevirir.

        Örn: physical=12.00, scaling=100  →  1200
             physical=75.0,  scaling=1    →  75
        """
        return int(round(physical_value * self.scaling))
    
    def from_uint16(self, raw: int) -> float:
        """Ham uint16 register değerini fiziksel değere çevirir (signed + scaling).
        Tek doğru kaynak burası — reader artık elle two's complement yapmaz."""
        val = raw
        if self.signed and val >= 0x8000:      # 32768
            val -= 0x10000                      # 65536
        if self.scaling == 1:
            return float(val)
        return round(val / self.scaling, 4)

    def to_uint16(self, physical: float) -> int:
        """Fiziksel değeri register'a yazılacak uint16'ya çevirir (scaling + two's complement).
        Aralık dışı değerde SESSİZCE WRAP ETMEZ — ValueError fırlatır (güvenlik)."""
        raw = int(round(physical * self.scaling))
        if self.signed:
            if not (-32768 <= raw <= 32767):
                raise ValueError(
                    f"{self.name}: {physical} (ham={raw}) signed int16 aralığı dışında [-32768, 32767]."
                )
            if raw < 0:
                raw += 0x10000
        else:
            if not (0 <= raw <= 65535):
                raise ValueError(
                    f"{self.name}: {physical} (ham={raw}) uint16 aralığı dışında [0, 65535]."
                )
        return raw & 0xFFFF


    def __repr__(self) -> str:
        modbus_addr = (30001 if self.is_readonly else 40001) + self.address
        return (
            f"RegisterDef(name='{self.name}', "
            f"modbus={modbus_addr}, pyaddr={self.address}, "
            f"access='{self.access}', scaling={self.scaling})"
        )


# ---------------------------------------------------------------------------
# Register Grupları
# ---------------------------------------------------------------------------

class HoldingRegisters:
    """
    Holding Register (4x) grubu — yazılabilir konfigürasyon ve komut registerları.

    Tüm nitelikler RegisterDef nesnesidir ve modbus_registers.yaml'dan yüklenir.
    """

    def __init__(self, regs: dict[str, RegisterDef]) -> None:
        self._regs = regs
        self.MODE_SELECT          = regs["mode_select"]
        self.TOTAL_TURNS          = regs["total_turns"]
        self.PROPORTIONAL_SIGNAL  = regs["proportional_signal"]
        self.TARGET_STEP          = regs["target_step"]
        self.FAST_OPEN_CLOSE      = regs["fast_open_close"]
        self.CALIBRATION_DIRECTION = regs["calibration_direction"]
        self.CURRENT_POSITION     = regs["current_position"]
        self.CURRENT_LOAD         = regs["current_load"]
        self.CALIBRATION_STATUS   = regs["calibration_status"]
        self.SIGNAL_LOST_FLAG     = regs["signal_lost_flag"]
        self.SIGNAL_LOSS_ACTION   = regs["signal_loss_action"]
        self.SEATING_LOAD         = regs["seating_load"]
        self.BACKOFF_OFFSET       = regs["backoff_offset"]
        self.PID_SETPOINT         = regs["pid_setpoint"]
        self.PID_KP               = regs["pid_kp"]
        self.PID_KI               = regs["pid_ki"]
        self.PID_KD               = regs["pid_kd"]
        self.PID_DEADBAND         = regs["pid_deadband"]
        self.ADC_OFFSET           = regs["adc_offset"]
        self.ADC_GAIN             = regs["adc_gain"]

    def all(self):
        return self._regs



class InputRegisters:
    def __init__(self, regs: dict[str, RegisterDef]) -> None:
        self._regs = regs

    def block(self):
        return 0, 1  # dummy

    def all(self):
        return self._regs


# ---------------------------------------------------------------------------
# Status Word Bit Maskeleri
# ---------------------------------------------------------------------------

class StatusBits:
    """
    30001 (status_word) register'ının bit tanımları.

    Kullanım:
        raw_status = raw_regs[0]
        is_calibrated = bool(raw_status & StatusBits.CALIBRATION_DONE)
        is_moving     = bool(raw_status & StatusBits.MOVING)
    """
    CALIBRATION_DONE: int = 0b0000_0001   # Bit 0
    MOVING:           int = 0b0000_0010   # Bit 1
    SIGNAL_ERROR:     int = 0b0000_0100   # Bit 2
    # Bit 3–15: Rezerve


# ---------------------------------------------------------------------------
# Control Word Komut Sabitleri
# ---------------------------------------------------------------------------

class ControlCmd:
    """
    40002 (control_word) register'ına yazılacak komut değerleri.

    Kullanım:
        await client.write_register(
            address=Reg.HOLDING.CONTROL_WORD.address,
            value=ControlCmd.AUTO_CALIBRATE
        )
    """
    STOP:           int = 0
    AUTO_CALIBRATE: int = 1
    OPEN_FULL:      int = 2
    CLOSE_FULL:     int = 3


# ---------------------------------------------------------------------------
# Register Haritası Yükleyici
# ---------------------------------------------------------------------------

class _RegisterMap:
    """
    Singleton register haritası. Reg sabiti üzerinden erişilir.

    İlk import sırasında YAML otomatik yüklenir.
    Farklı bir YAML yolu için: Reg.load(path=Path("..."))
    """

    def __init__(self) -> None:
        self.HOLDING: HoldingRegisters | None = None
        self.INPUT:   InputRegisters | None = None
        self._loaded: bool = False

    def load(self, path: Path = _DEFAULT_YAML_PATH) -> None:
        """
        YAML dosyasını okuyup register gruplarını oluşturur.

        :param path: modbus_registers.yaml dosyasının tam yolu
        :raises FileNotFoundError: Dosya bulunamazsa
        :raises KeyError: YAML'da beklenen alan eksikse
        """
        if not path.exists():
            raise FileNotFoundError(
                f"modbus_registers.yaml bulunamadı: {path}\n"
                f"Beklenen konum: config/modbus_registers.yaml"
            )

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        holding_raw = data.get("holding_registers", {})
        input_raw   = data.get("input_registers", {})

        if not holding_raw:
            raise KeyError("YAML'da 'holding_registers' bölümü bulunamadı.")
        if not input_raw:
            raise KeyError("YAML'da 'input_registers' bölümü bulunamadı.")

        holding_defs = self._parse_block(holding_raw, expected_access="rw")
        input_defs   = self._parse_block(input_raw,   expected_access="ro")

        self.HOLDING = HoldingRegisters(holding_defs)
        self.INPUT   = InputRegisters(input_defs)
        self._loaded = True

        logger.info(
            f"modbus_registers.yaml yüklendi: "
            f"{len(holding_defs)} holding, {len(input_defs)} input register."
        )

    def _parse_block(
        self,
        block: dict,
        expected_access: str,
    ) -> dict[str, RegisterDef]:
        """
        YAML bloğunu RegisterDef sözlüğüne çevirir.

        :param block:           YAML'dan gelen ham sözlük
        :param expected_access: Bu bloğun beklenen erişim tipi ('rw' veya 'ro')
        """
        result: dict[str, RegisterDef] = {}

        for name, fields in block.items():
            try:
                access = fields.get("access", expected_access)
                if access != expected_access:
                    logger.warning(
                        f"Register '{name}': access='{access}' beklenen "
                        f"'{expected_access}' ile uyuşmuyor."
                    )

                result[name] = RegisterDef(
                    name=name,
                    address=int(fields["address"]),
                    access=access,
                    scaling=int(fields.get("scaling", 1)),
                    description=str(fields.get("description", "")).strip(),
                    mode=str(fields.get("mode", "")).strip(),
                    signed=bool(fields.get("signed", False)),   # ← YENİ

                )
            except (KeyError, TypeError, ValueError) as e:
                raise KeyError(
                    f"Register '{name}' YAML alanı hatalı: {e}"
                ) from e

        return result

    def __getattr__(self, name: str):
        """Yüklemeden önce erişim girişimini yakala ve anlaşılır hata ver."""
        if name in ("HOLDING", "INPUT") and not self._loaded:
            raise RuntimeError(
                "Register haritası henüz yüklenmedi. "
                "Önce Reg.load() çağrılmalıdır (main.py veya AppContext içinde)."
            )
        raise AttributeError(f"'_RegisterMap' nesnesinde '{name}' niteliği yok.")


# ---------------------------------------------------------------------------
# Modül Düzeyinde Singleton — Her yerden erişim noktası
# ---------------------------------------------------------------------------

Reg = _RegisterMap()

# AppContext.initialize_async() veya main.py içinde şu satır çağrılmalıdır:
#   from hal.modbus_config import Reg
#   Reg.load()                          # varsayılan: config/modbus_registers.yaml
#   Reg.load(path=Path("custom.yaml"))  # özel yol