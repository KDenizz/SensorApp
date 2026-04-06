# AppContext'i inject olarak alır. Döngüyü işletir.
"""
controller/main_controller.py

[Controller Layer] State Machine'i yöneten merkezi asenkron döngü.

Sorumluluklar:
    1. AppContext'i dependency injection ile alır.
    2. Başlangıç state'ini (IdleState) oluşturur.
    3. Sabit frekanslı (TARGET_HZ) ana döngüyü çalıştırır:
        a. Mevcut state'in update(dt) metodunu çağırır.
        b. Dönen yeni state varsa geçiş zincirini (on_exit → on_enter) yönetir.
    4. WebSocket üzerinden UI'dan gelen event'leri handle_event()'e yönlendirir.
    5. stop_event set edildiğinde temiz kapanışı sağlar.

Mimari Kural:
    - Bu katman HAL'e DOĞRUDAN DOKUNMAZ.
    - Sensör verisi SADECE context.raw_data_queue üzerinden gelir.
    - Motor komutu SADECE context.command_queue üzerinden gider.
    - UI bildirimi SADECE context.broadcaster.publish() üzerinden yapılır.

Event Entegrasyonu (ws_server.py ile):
    ws_server.py, UI'dan gelen komutları parse ettikten sonra
    `await context.event_queue.put(event_dict)` ile bu döngüye gönderir.
    MainController, her iterasyonda event_queue'yu da kontrol eder.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional, TYPE_CHECKING

from controller.states.base_state import BaseState

if TYPE_CHECKING:
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class MainController:
    """
    [Controller Layer] State Machine'in yaşam döngüsünü yöneten servis.

    Kullanım (main.py içinde):
        controller = MainController(context)
        asyncio.create_task(controller.run())
    """

    # Hedef döngü frekansı. 200Hz → her döngü ~5ms.
    TARGET_HZ: int = 200

    def __init__(self, context: "AppContext") -> None:
        self.context = context
        self._current_state: Optional[BaseState] = None
        self._loop_interval: float = 1.0 / self.TARGET_HZ

    # ------------------------------------------------------------------
    # Ana Döngü
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """
        asyncio.Task olarak çalışan State Machine döngüsü.
        """
        from controller.states.idle_state import IdleState

        self._current_state = IdleState(self.context)
        await self._current_state.on_enter()

        logger.info(f"MainController başlatıldı. Başlangıç state: {self._current_state}")

        while not self.context.stop_event.is_set():
            loop_start = time.monotonic()

            # --- 1. UI Event'lerini işle ---
            await self._drain_event_queue()

            # --- 2. State güncelle ---
            dt = self._loop_interval  # Sabit dt (gerçek ölçüm aşağıda)
            try:
                next_state = await self._current_state.update(dt)
            except Exception as e:
                logger.error(
                    f"State update() sırasında beklenmeyen hata: {e}. "
                    "FaultSafeState'e geçiliyor.",
                    exc_info=True
                )
                next_state = await self._create_fault_state(str(e))

            # --- 3. State geçişi ---
            if next_state is not None:
                await self._transition_to(next_state)

            # --- 4. Döngü hızını sabitle ---
            elapsed = time.monotonic() - loop_start
            sleep_time = max(0.0, self._loop_interval - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

        # Kapanış: mevcut state'in temizlik metodunu çağır
        if self._current_state:
            try:
                await self._current_state.on_exit()
            except Exception as e:
                logger.error(f"Kapanış sırasında on_exit() hatası: {e}")

        logger.info("MainController güvenli şekilde sonlandırıldı.")

    # ------------------------------------------------------------------
    # State Geçiş Yönetimi
    # ------------------------------------------------------------------

    async def _transition_to(self, new_state: BaseState) -> None:
        """
        Mevcut state'den yeni state'e geçiş zincirini yönetir.

        Zincir: on_exit() [eski] → on_enter() [yeni]

        Args:
            new_state: Geçilecek yeni state nesnesi.
        """
        old_name = str(self._current_state)
        new_name = str(new_state)

        logger.info(f"State geçişi: {old_name} → {new_name}")

        try:
            await self._current_state.on_exit()
        except Exception as e:
            logger.error(f"on_exit() hatası ({old_name}): {e}", exc_info=True)

        self._current_state = new_state

        try:
            await self._current_state.on_enter()
        except Exception as e:
            logger.error(f"on_enter() hatası ({new_name}): {e}", exc_info=True)
            # on_enter başarısız olursa FaultSafe'e düş
            fault = await self._create_fault_state(f"on_enter() başarısız: {new_name}")
            self._current_state = fault
            await fault.on_enter()

    # ------------------------------------------------------------------
    # Event Yönetimi
    # ------------------------------------------------------------------

    async def _drain_event_queue(self) -> None:
        """
        context.event_queue'daki tüm bekleyen event'leri tüketerek
        mevcut state'in handle_event() metoduna iletir.

        Kuyruk boşsa hemen geri döner (non-blocking).

        ws_server.py tarafından kuyruğa bırakılan event formatı:
            {"cmd": "START_MODULATION", "mode": "Pressure"}
            {"cmd": "EMERGENCY_STOP"}
            {"cmd": "SET_SETPOINT", "value": 2.5}
        """
        if not hasattr(self.context, "event_queue") or self.context.event_queue is None:
            return

        while True:
            try:
                event = self.context.event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            try:
                next_state = await self._current_state.handle_event(event)
                if next_state is not None:
                    await self._transition_to(next_state)
            except Exception as e:
                logger.error(f"handle_event() hatası: {e}", exc_info=True)

            self.context.event_queue.task_done()

    # ------------------------------------------------------------------
    # Yardımcılar
    # ------------------------------------------------------------------

    async def _create_fault_state(self, reason: str) -> BaseState:
        """
        Verilen sebeple FaultSafeState oluşturur.
        Circular import'tan kaçınmak için lazy import kullanılır.
        """
        from controller.states.fault_safe_state import FaultSafeState
        return FaultSafeState(self.context, reason=reason)

    @property
    def current_state_name(self) -> str:
        """Mevcut state'in adını döner (UI veya loglama için)."""
        return str(self._current_state) if self._current_state else "None"