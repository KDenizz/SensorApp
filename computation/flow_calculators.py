# ValveCharacteristic bağımlılığını alır.
import logging
import math
from typing import Tuple

from computation.valve_characteristic import ValveCharacteristic
from core.data_types import FlowRegime

logger = logging.getLogger(__name__)

class FlowCalculator:
    """
    [Computation Layer] Gaz ve sıvı akışkanlar için kütlesel debi, akış rejimi ve 
    akış katsayısı (C_V) hesaplamalarını gerçekleştiren stateless (durumsuz) sınıf.
    'Yazilim_Hesaplar_2.txt' referans alınarak endüstriyel formüller uygulanmıştır.
    """

    def __init__(self, valve_characteristic: ValveCharacteristic) -> None:
        """
        FlowCalculator başlatılırken valfin fiziksel özelliklerini okuyabilmek için
        ValveCharacteristic objesi enjekte edilir.
        """
        self.valve = valve_characteristic

    def calculate_delta_p(self, p1_bar: float, p2_bar: float) -> float:
        """
        Basınç farkını hesaplar. Negatif fark (ters akış veya sensör gürültüsü)
        durumunda uyarı logu basar ve sıfır döner.
        """
        delta = p1_bar - p2_bar
        if delta < 0:
            # Not: 200Hz loop'ta log spam yaratmaması için ileride log filtrelenebilir.
            logger.warning(f"Negatif ΔP (Ters akış/Gürültü) tespit edildi: P1={p1_bar:.2f}, P2={p2_bar:.2f}")
        return max(0.0, delta)

    def determine_flow_regime(self, p1_bar: float, p2_bar: float, gamma: float) -> Tuple[FlowRegime, float]:
        """
        Akış rejiminin (Boğulmuş/Kritik veya Normal) durumunu ve kritik basınç oranını hesaplar.
        
        Returns:
            Tuple[FlowRegime, float]: (Akış Rejimi Enum, Kritik Basınç Oranı p_Cr)
        """
        if p1_bar <= 0 or gamma <= 1.0:
            return FlowRegime.UNKNOWN, 0.0

        # p_Cr = ((gamma + 1) / 2) ^ (-gamma / (gamma - 1))
        p_cr = math.pow((gamma + 1) / 2.0, -gamma / (gamma - 1.0))
        
        pressure_ratio = p2_bar / p1_bar

        if pressure_ratio <= p_cr:
            return FlowRegime.CHOKED, p_cr
        else:
            return FlowRegime.NORMAL, p_cr

    def calculate_gas_mass_flow(
        self, 
        opening_pct: float, 
        p1_bar: float, 
        p2_bar: float, 
        temp_k: float, 
        sg: float, 
        rho_kg_m3: float
    ) -> float:
        """
        C_V tabanlı formüllerle gazlar için kütlesel debiyi hesaplar.
        Tüm basınç parametreleri [bar] cinsindendir.
        """
        if p1_bar <= 0 or temp_k <= 0 or sg <= 0:
            return 0.0

        if p2_bar >= p1_bar:
            return 0.0

        cv = self.valve.get_cv(opening_pct)
        if cv <= 0.0:
            return 0.0

        # Denklem 1: Yaklaşık Boğulmuş Akış (Choked)
        if p1_bar >= 2.0 * p2_bar:
            term1 = (0.1248968 * p1_bar) / math.sqrt(sg * temp_k)
            mass_flow = cv * term1 * rho_kg_m3
        # Denklem 2: Subsonik / Normal Akış
        else:
            delta_p_sq = (p1_bar**2) - (p2_bar**2)
            if delta_p_sq <= 0:
                return 0.0
                
            term1 = math.sqrt(delta_p_sq / (sg * temp_k))
            mass_flow = cv * 0.1472435 * term1 * rho_kg_m3

        return float(mass_flow)

    def calculate_liquid_mass_flow(
        self, 
        p1_bar: float, 
        p2_bar: float, 
        rho_kg_m3: float, 
        cd: float, 
        a_orifis_m2: float
    ) -> float:
        """
        Bernoulli tabanlı sıvı debisi hesabı. API Contract gereği basınçlar [bar] 
        alınır ve metot içinde metrik formül için Pascal'a [Pa] çevrilir.
        """
        if rho_kg_m3 <= 0.0 or p1_bar <= p2_bar:
            return 0.0

        # Birim dönüşümü: 1 bar = 100,000 Pascal
        p1_pa = p1_bar * 1e5
        p2_pa = p2_bar * 1e5
        
        delta_p = p1_pa - p2_pa
        
        # m_dot = C_d * A_orifis * sqrt( 2 * (P1 - P2) / rho )
        velocity_term = math.sqrt((2.0 * delta_p) / rho_kg_m3)
        mass_flow = cd * a_orifis_m2 * velocity_term
        
        return float(mass_flow)