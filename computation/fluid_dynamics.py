import logging
import yaml
import math
import numpy as np
from pathlib import Path
from typing import Dict, Any

logger = logging.getLogger(__name__)

class FluidDynamics:
    """
    [Computation Layer] Akışkan mekaniği ve termodinamik özellik hesaplamalarını
    yürüten sınıf. İdeal gaz denklemi, Spesifik Gravite (SG), sıcaklığa bağlı 
    Özgül Isı Oranı (Gamma) interpolasyonu ve izentropik akış ilişkilerini içerir.
    """

    # Sabitler
    R_U = 8314.46  # Evrensel gaz sabiti [J / (kmol * K)]
    M_AIR = 28.9647 # Havanın molar kütlesi [kg/kmol]
    
    # Standart Koşullar (Specific Gravity hesabı için referans)
    T_STD = 288.15 # 15 °C [K]
    P_STD = 1.01325 # 1 atm [bar]

    def __init__(self, fluid_profile_path: str | Path) -> None:
        """
        Akışkan profili (YAML) verilerini yükler ve hazırlıkları yapar.
        
        Args:
            fluid_profile_path (str | Path): Gaz/Sıvı konfigürasyon dosyasının yolu.
        """
        self.profile_path = Path(fluid_profile_path)
        
        # Akışkan sabitleri
        self.fluid_name: str = "Unknown"
        self.molar_mass: float = 0.0     # [kg/kmol]
        self.compressibility_z: float = 1.0 # Sıkıştırılabilirlik çarpanı (Z)
        
        # İnterpolasyon dizileri (Sıcaklığa [K] bağlı Gamma değerleri)
        self._temp_array: np.ndarray = np.array([])
        self._gamma_array: np.ndarray = np.array([])

        self._load_profile()

    def _load_profile(self) -> None:
        """
        Belirtilen YAML dosyasını okuyarak akışkanın temel fiziksel özelliklerini
        ve sıcaklığa bağlı gamma_table ayrık noktalarını yükler.
        """
        if not self.profile_path.exists():
            raise FileNotFoundError(f"Akışkan profili bulunamadı: {self.profile_path}")

        try:
            with open(self.profile_path, 'r', encoding='utf-8') as file:
                data: Dict[str, Any] = yaml.safe_load(file)

            self.fluid_name = data.get("name", "Unknown Fluid")
            self.molar_mass = float(data.get("molar_mass", 0.0))
            self.compressibility_z = float(data.get("compressibility_z", 1.0))

            if self.molar_mass <= 0:
                raise ValueError(f"Molar kütle ({self.molar_mass}) sıfır veya negatif olamaz.")

            # Sıcaklık(K) - Gamma tablosunu okuyup sırala
            gamma_table: Dict[float, float] = data.get("gamma_table", {})
            if gamma_table:
                sorted_items = sorted(gamma_table.items())
                self._temp_array = np.array([float(k) for k, v in sorted_items])
                self._gamma_array = np.array([float(v) for k, v in sorted_items])
            else:
                logger.warning(f"'{self.fluid_name}' için gamma_table bulunamadı, varsayılan (1.4) kullanılacak.")

        except yaml.YAMLError as e:
            raise ValueError(f"YAML format hatası ({self.profile_path.name}): {e}") from e
        except Exception as e:
            raise RuntimeError(f"Akışkan profili yüklenirken hata: {e}") from e

    def get_sg(self) -> float:
        """
        Akışkanın Spesifik Gravitesini (Specific Gravity - SG) hesaplar.
        Gazlar için standart tanım: Akışkanın molar kütlesi / Havanın molar kütlesi.
        
        Returns:
            float: Boyutsuz SG değeri.
        """
        return float(self.molar_mass / self.M_AIR)

    def get_gamma(self, temp_k: float) -> float:
        """
        Sensörden okunan anlık sıcaklığa [K] göre özgül ısı oranını (Gamma, k) 
        numpy lineer interpolasyonu ile hesaplar.
        
        Args:
            temp_k (float): Anlık durgun hal sıcaklığı [K].
            
        Returns:
            float: İnterpole edilmiş boyutsuz Gamma (C_p / C_v) değeri.
        """
        if len(self._temp_array) == 0:
            return 1.4 # İdeal hava yaklaşımı
            
        # Düşük/Yüksek sıcaklık limitleri gelirse clamp işlemi numpy interp içinde 
        # varsayılan olarak uç değerleri döndürecektir.
        return float(np.interp(temp_k, self._temp_array, self._gamma_array))

    def calculate_gas_density(self, p_bar: float, temp_k: float) -> float:
        """
        Gerçek gaz denklemini (Real Gas Law) kullanarak çalışma koşullarındaki 
        gaz yoğunluğunu hesaplar. (P = Z * rho * R * T)
        
        Args:
            p_bar (float): Anlık statik/durgun basınç [bar]
            temp_k (float): Anlık sıcaklık [K]
            
        Returns:
            float: Yoğunluk (rho) [kg/m^3]. Fiziksel olmayan değerler için 0.0 döner.
        """
        if p_bar <= 0 or temp_k <= 0:
            return 0.0

        # Basıncı Pascal'a çevir: 1 bar = 100,000 Pa
        p_pa = p_bar * 1e5
        
        # Gaz sabiti R = R_U / Molar Kütle
        r_specific = self.R_U / self.molar_mass
        
        # rho = P / (Z * R * T)
        rho = p_pa / (self.compressibility_z * r_specific * temp_k)
        
        return float(rho)

    def calculate_isentropic_temperature(self, t1_k: float, p1_bar: float, p2_bar: float, gamma: float) -> float:
        """
        İzentropik genişleme formülünü kullanarak çıkış noktasındaki tahmini 
        soğuma sıcaklığını (T_2) hesaplar.
        T_2 = T_1 * (P_2 / P_1) ^ ((gamma - 1) / gamma)
        
        Args:
            t1_k (float): Giriş sıcaklığı [K]
            p1_bar (float): Giriş basıncı [bar]
            p2_bar (float): Çıkış basıncı [bar]
            gamma (float): Akışkanın özgül ısı oranı
            
        Returns:
            float: İzentropik çıkış sıcaklığı (T_2) [K]. Geçersiz verilerde T_1 döner.
        """
        if p1_bar <= 0 or p2_bar <= 0 or t1_k <= 0 or gamma <= 1.0:
            return t1_k

        # Ters akış veya P2 > P1 durumunda izentropik genleşme geçersizdir.
        if p2_bar >= p1_bar:
            return t1_k

        pressure_ratio = p2_bar / p1_bar
        exponent = (gamma - 1.0) / gamma
        
        t2_k = t1_k * math.pow(pressure_ratio, exponent)
        
        return float(t2_k)