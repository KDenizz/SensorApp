# Bumpless transfer, reset_integral() ve output tracking içerir.
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)

class PIDController:
    """
    [Controller Layer] Endüstriyel standartlarda, Anti-Windup ve Türev Filtresi (Low-Pass)
    içeren kapalı çevrim kontrol algoritması. 
    State Machine tarafından her döngüde (dt periyodu ile) çağrılır.
    """

    def __init__(
        self, 
        kp: float, 
        ki: float, 
        kd: float, 
        out_min: float, 
        out_max: float, 
        integral_limit: float,
        fc_hz: Optional[float] = None
    ) -> None:
        self.kp: float = kp
        self.ki: float = ki
        self.kd: float = kd
        
        self.out_min: float = out_min
        self.out_max: float = out_max
        self.integral_limit: float = integral_limit
        
        self.tau: float = 0.0
        if fc_hz is not None and fc_hz > 0:
            self.tau = 1.0 / (2.0 * math.pi * fc_hz)
            
        self.integral: float = 0.0
        self.prev_error: float = 0.0
        self.prev_derivative: float = 0.0
        self._last_output: float = 0.0  # dt <= 0 durumunda sistemi korumak için

    def update_gains(
        self, 
        kp: float, 
        ki: float, 
        kd: float,
        fc_hz: Optional[float] = None
    ) -> None:
        """Çalışma anında kazanç güncelleme (UI'dan parametre değişimi için)."""
        self.kp = kp
        self.ki = ki
        self.kd = kd
        if fc_hz is not None and fc_hz > 0:
            self.tau = 1.0 / (2.0 * math.pi * fc_hz)
        else:
            self.tau = 0.0
        logger.info(f"PID kazançları güncellendi: Kp={kp}, Ki={ki}, Kd={kd}, fc_hz={fc_hz}")

    def compute(self, setpoint: float, pv: float, dt: float) -> float:
        if dt <= 0.0:
            logger.warning(f"Geçersiz dt ({dt}). Son çıkış ({self._last_output}) korunuyor.")
            return self._last_output

        error = setpoint - pv
        p_term = self.kp * error

        # Integral hesabı ve clamp (Windup koruması - I_term bazlı)
        provisional_integral = self.integral + (error * dt)
        provisional_integral = max(-self.integral_limit, min(self.integral_limit, provisional_integral))
        i_term = self.ki * provisional_integral

        # Türev hesabı ve filtreleme
        raw_derivative = (error - self.prev_error) / dt
        if self.tau > 0:
            alpha = dt / (self.tau + dt)
            filtered_derivative = (alpha * raw_derivative) + ((1.0 - alpha) * self.prev_derivative)
        else:
            filtered_derivative = raw_derivative
            
        self.prev_derivative = filtered_derivative
        d_term = self.kd * filtered_derivative

        output = p_term + i_term + d_term

        # Output Saturation (Anti-Windup - Sistem bazlı)
        if output > self.out_max:
            output = self.out_max
            if error < 0: 
                self.integral = provisional_integral
        elif output < self.out_min:
            output = self.out_min
            if error > 0:
                self.integral = provisional_integral
        else:
            self.integral = provisional_integral

        self.prev_error = error
        self._last_output = output
        return output

    def reset(self, current_output: float = 0.0) -> None:
        """Bumpless Transfer için integrali ön-yükler."""
        self.prev_error = 0.0
        self.prev_derivative = 0.0
        
        if self.ki != 0:
            self.integral = current_output / self.ki
            self.integral = max(-self.integral_limit, min(self.integral_limit, self.integral))
        else:
            self.integral = 0.0
            
        logger.debug(f"PID Resetlendi. Bumpless Transfer için Integral = {self.integral:.3f}")