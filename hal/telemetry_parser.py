
import time
from core.datatypes import SensorPacket

class TelemetryParser:
    """
    Donanımdan gelen ham byte/string verilerini anlamlandırıp 
    SensorPacket dataclass'ına dönüştüren katman.
    """
    def __init__(self):
        # Gerçek uygulamada silinecek Mock durumları
        self.mock_state = {
            "motor_pos": 0,
            "p1": 50.0,
            "p2": 40.0,
            "temp": 298.15,
            "current": 12.0
        }

    def update_mock_physics(self) -> None:
        """Kullanıcının belirlediği fizik kurallarına göre mock verileri günceller."""
        opening_ratio = min(max(self.mock_state["motor_pos"] / 1000.0, 0.0), 1.0)
        # Valf açıldıkça downstream basıncı düşer (pressure drop simülasyonu)
        self.mock_state["p2"] = 40.0 - (opening_ratio * 5.0)
        self.mock_state["current"] = 12.0 + abs(opening_ratio - 0.5) * 20.0

    def parse_mock(self) -> SensorPacket:
        """Gerçek donanım bağlanana kadar sahte (mock) paket üretir."""
        self.update_mock_physics()
        return SensorPacket(
            p1_raw=self.mock_state["p1"],
            p2_raw=self.mock_state["p2"],
            temp_k=self.mock_state["temp"],
            motor_pos_ticks=self.mock_state["motor_pos"],
            motor_current_ma=self.mock_state["current"],
            timestamp=time.monotonic()  # NTP atlamalarından etkilenmeyen zaman damgası
        )

    def parse_raw(self, raw_data: bytes) -> SensorPacket:
        """
        TODO: Gerçek donanım geldiğinde bu fonksiyon kullanılacak.
        Örn: struct.unpack ile raw_data'yı çöz.
        """
        pass