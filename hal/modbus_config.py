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

logger = logging.getLogger(__name__)

# YAML dosyasının varsayılan konumu — main.py'nin bulunduğu dizine göre
_DEFAULT_YAML_PATH = Path(__file__).parent.parent / "config" / "modbus_registers.yaml"


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

        # --- Kontrol ---
        self.MODE_SELECT:           RegisterDef = regs["mode_select"]
        self.CONTROL_WORD:          RegisterDef = regs["control_word"]
        self.TARGET_REVOLUTIONS:    RegisterDef = regs["target_revolutions"]
        self.TARGET_STEP:           RegisterDef = regs["target_step"]

        # --- Genel Kurulum ---
        self.MAX_REVOLUTIONS:       RegisterDef = regs["max_revolutions"]
        self.CALIBRATION_DIRECTION: RegisterDef = regs["calibration_direction"]

        # --- Kalibrasyon & Sinyal ---
        self.ZERO_TORQUE_THRESHOLD:    RegisterDef = regs["zero_torque_threshold"]
        self.PROPORTIONAL_SIGNAL_MIN:  RegisterDef = regs["proportional_signal_min"]
        self.PROPORTIONAL_SIGNAL_MAX:  RegisterDef = regs["proportional_signal_max"]

        # --- Gelişmiş Kontrol ---
        self.STEP_RESOLUTION:          RegisterDef = regs["step_resolution"]
        self.SIGNAL_LOSS_ACTION:       RegisterDef = regs["signal_loss_action"]
        self.SIGNAL_LOSS_TARGET_REV:   RegisterDef = regs["signal_loss_target_rev"]
        self.SIGNAL_LOSS_TARGET_STEP:  RegisterDef = regs["signal_loss_target_step"]
        self.CLOSING_TORQUE_LIMIT:     RegisterDef = regs["closing_torque_limit"]
        self.OPENING_KICK_TORQUE:      RegisterDef = regs["opening_kick_torque"]

    def all(self) -> dict[str, RegisterDef]:
        """Tüm Holding Register tanımlarını döndürür."""
        return self._regs


class InputRegisters:
    """
    Input Register (3x) grubu — salt okunur telemetri registerları.

    HALReader her polling döngüsünde block() ile tamamını tek seferde okur.
    """

    def __init__(self, regs: dict[str, RegisterDef]) -> None:
        self._regs = regs

        self.STATUS_WORD:          RegisterDef = regs["status_word"]
        self.CURRENT_POSITION_REV: RegisterDef = regs["current_position_rev"]
        self.CURRENT_POSITION_STEP: RegisterDef = regs["current_position_step"]
        self.EXTERNAL_SIGNAL_MA:   RegisterDef = regs["external_signal_ma"]
        self.MOTOR_TORQUE_PCT:     RegisterDef = regs["motor_torque_pct"]

    def block(self) -> Tuple[int, int]:
        """
        Tüm Input Register bloğunu tek Modbus isteğiyle okumak için
        (başlangıç_adresi, register_sayısı) tuple'ı döndürür.

        Kullanım:
            addr, count = Reg.INPUT.block()
            raw = await client.read_input_registers(addr, count)
        """
        addresses = [r.address for r in self._regs.values()]
        start = min(addresses)
        count = max(addresses) - start + 1
        return start, count

    def all(self) -> dict[str, RegisterDef]:
        """Tüm Input Register tanımlarını döndürür."""
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