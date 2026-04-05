# 0-100% arası tablodan CV/CD interpolasyonu yapar.
import logging
import yaml
import numpy as np
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

class ValveCharacteristic:
    """
    [Computation Layer] Valf karakteristik eğrilerini ve geometrik sınırlarını
    hesaplayan sınıf. Konfigürasyon dosyasından (YAML) okunan ayrık veri noktaları 
    üzerinde lineer interpolasyon yaparak anlık C_v değerini ve strok pozisyonunu hesaplar.
    """

    def __init__(self, profile_path: str | Path) -> None:
        self.profile_path = Path(profile_path)
        
        # Geometri ve limitler
        self.pitch_mm: float = 0.0
        self.max_stroke_mm: float = 0.0
        
        # İnterpolasyon eksenleri (Hızlı okuma için numpy array olarak tutulur)
        self._opening_array: np.ndarray = np.array([])
        self._cv_array: np.ndarray = np.array([])

        self._load_profile()

    def _load_profile(self) -> None:
        """
        YAML dosyasını okur, ayrıştırır ve interpolasyon dizilerini oluşturur.
        Hatalar çağrıcıya (caller) fırlatılır, log kirliliği önlenir.
        """
        if not self.profile_path.exists():
            raise FileNotFoundError(f"Valf profil dosyası bulunamadı: {self.profile_path}")

        try:
            with open(self.profile_path, 'r', encoding='utf-8') as file:
                data: Dict[str, Any] = yaml.safe_load(file)

            # Metadata okuma
            metadata = data.get("metadata", {})
            self.pitch_mm = float(metadata.get("pitch_mm", 0.0))
            self.max_stroke_mm = float(metadata.get("max_stroke_mm", 0.0))

            if self.pitch_mm <= 0 or self.max_stroke_mm <= 0:
                raise ValueError(f"Pitch ({self.pitch_mm}) veya max_stroke_mm ({self.max_stroke_mm}) geçersiz.")

            # Karakteristik eğriyi okuma ve sıralama
            char_table: Dict[float, float] = data.get("characteristic_table", {})
            if not char_table:
                raise ValueError("characteristic_table bulunamadı veya boş.")

            # NumPy interp fonksiyonu için x ekseninin artan sırada olması şarttır
            sorted_items = sorted(char_table.items())
            self._opening_array = np.array([float(k) for k, v in sorted_items])
            self._cv_array = np.array([float(v) for k, v in sorted_items])

        except yaml.YAMLError as e:
            raise ValueError(f"YAML format hatası ({self.profile_path.name}): {e}") from e
        except Exception as e:
            raise RuntimeError(f"Profil yüklenirken beklenmeyen hata: {e}") from e

    def is_characterized(self) -> bool:
        """
        Valf karakteristik tablosunun kullanılabilir olup olmadığını döner.
        Kalibrasyon tamamlanmadan veya profil yüklenmeden False döner.
        """
        return (
            len(self._opening_array) >= 2 and
            self.pitch_mm > 0 and
            self.max_stroke_mm > 0
        )

    def get_cv(self, opening_pct: float) -> float:
        """
        Verilen valf açıklığı (%) için C_v değerini hesaplar.
        """
        clamped_pct = max(0.0, min(100.0, opening_pct))
        return float(np.interp(clamped_pct, self._opening_array, self._cv_array))

    def get_position_mm(self, opening_pct: float) -> float:
        """
        Valf açıklık yüzdesini, milimetre cinsinden fiziksel strok pozisyonuna çevirir.
        """
        clamped_pct = max(0.0, min(100.0, opening_pct))
        return float((clamped_pct / 100.0) * self.max_stroke_mm)

    def get_motor_turns(self, position_mm: float) -> float:
        """
        Fiziksel pozisyona ulaşmak için servo motorun atması gereken tur sayısını hesaplar.
        """
        if self.pitch_mm == 0:
            return 0.0
        return float(position_mm / self.pitch_mm)

    def get_opening_from_ticks(self, ticks: int, ticks_per_turn: int) -> float:
        """
        Encoder tick'inden (pulse) valf açıklığını (%) hesaplar.
        Controller'ın sensör verisinden (HAL) kapalı çevrim geribildirim alması için kullanılır.
        
        Args:
            ticks (int): Encoder'dan okunan anlık pulse değeri.
            ticks_per_turn (int): Motorun bir tam turundaki encoder tick sayısı (örn: 10000).
            
        Returns:
            float: Normalize edilmiş valf açıklığı (%) [0.0 - 100.0]
        """
        if ticks_per_turn == 0 or self.max_stroke_mm == 0:
            return 0.0
            
        position_mm = (ticks / ticks_per_turn) * self.pitch_mm
        
        # Yüzdeyi hesapla ve %0 ile %100 arasına clamp et
        opening_pct = (position_mm / self.max_stroke_mm) * 100.0
        return float(max(0.0, min(100.0, opening_pct)))