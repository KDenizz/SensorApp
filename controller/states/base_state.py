"""
controller/states/base_state.py

[Controller Layer] Tüm State Machine state'lerinin türeyeceği Abstract Base Class.

Migration Notları:
- Tüm yaşam döngüsü metodları (on_enter, update, on_exit) async def olarak tanımlandı.
- handle_event de async def olarak işaretlendi: UI'dan gelen komutlar artık
  await context.command_queue.put(...) çağrısı içerebilir.
- __init__ senkron kalmaya devam eder; asyncio primitive yaratmaz,
  sadece context referansını saklar. (Event loop bağımsız)
- TYPE_CHECKING guard ile AppContext circular import'u önlendi.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Optional, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    # Yalnızca tip denetleyici (mypy/pyright) tarafından görülür.
    # Runtime'da import edilmez → circular import riski sıfır.
    from core.app_context import AppContext

logger = logging.getLogger(__name__)


class BaseState(ABC):
    """
    [Controller Layer] Durum makinesi (State Machine) mimarisindeki tüm state'lerin
    (CalibratingState, ModulatingState, FaultSafeState vs.) türeyeceği Abstract Base Class.

    Async Sözleşmesi:
        - on_enter(), update(), on_exit() ve handle_event() coroutine'dir.
        - Alt sınıflar bu metodları 'async def' ile override ETMEK ZORUNDADIR.
        - Bu metodlar içindeki tüm queue ve broadcaster işlemleri 'await' ile çağrılır.
        - Hiçbir metod içinde time.sleep() veya blocking I/O KULLANILMAZ.
    """

    def __init__(self, context: "AppContext") -> None:
        """
        Her state, AppContext üzerinden asyncio.Queue'lara ve
        WsBroadcaster'a erişim sağlar.

        Args:
            context: Sistem genelindeki asyncio kuyrukları, broadcaster ve
                     konfigürasyonu barındıran Singleton bağlam nesnesi.

        Not:
            __init__ senkron kalır. asyncio.Queue veya asyncio.Event
            YARATILMAZ — bunlar AppContext.initialize_async() içinde
            event loop ayağa kalktıktan sonra oluşturulur.
        """
        self.context = context
        self.name: str = self.__class__.__name__

    # ------------------------------------------------------------------
    # Zorunlu (Abstract) Yaşam Döngüsü Metodları
    # ------------------------------------------------------------------

    @abstractmethod
    async def on_enter(self) -> None:
        """
        State Machine bu state'e geçiş yaptığında bir kez çağrılır.

        Tipik kullanım:
            - PID resetleme (context.pid.reset(...))
            - Başlangıç komutlarını kuyruğa atma:
              await context.command_queue.put((priority, cmd))
            - UI'ya durum bildirimi:
              await context.broadcaster.publish("STATE_CHANGED", {"state": self.name})
        """
        ...

    @abstractmethod
    async def update(self, dt: float) -> Optional["BaseState"]:
        """
        Ana kontrol döngüsünde (MainController.run) her iterasyonda çağrılır.

        Args:
            dt: Son update çağrısından bu yana geçen süre [saniye].
                200 Hz döngüde ~0.005 s olur.

        Returns:
            Optional[BaseState]:
                - None       → Mevcut state'de kalmaya devam et.
                - BaseState  → Döndürülen yeni state'e geçiş yap.
                  (MainController geçiş sırasında on_exit → on_enter zincirini yönetir.)

        Kural:
            Sensör verisi için raw_data_queue.get() çağrısı BURADA yapılır.
            Blocking bekleme YAPILMAZ; veri yoksa hemen None dönülür.
            Örnek:
                try:
                    packet = context.raw_data_queue.get_nowait()
                except asyncio.QueueEmpty:
                    return None
        """
        ...

    @abstractmethod
    async def on_exit(self) -> None:
        """
        State Machine bu state'den çıkış yaparken bir kez çağrılır.

        Tipik kullanım:
            - Motoru güvenli konuma alma komutu:
              await context.command_queue.put((1, stop_cmd))
            - Açık kaynakların serbest bırakılması.
            - Loglama / UI bildirimi.
        """
        ...

    # ------------------------------------------------------------------
    # Opsiyonel (Override Edilebilir) Metod
    # ------------------------------------------------------------------

    async def handle_event(self, event: Dict[str, Any]) -> Optional["BaseState"]:
        """
        WebSocket üzerinden UI'dan veya sistemden gelen olayları işler.
        Alt sınıflar ihtiyaç duydukları komutları override ederek karşılar.

        Args:
            event: Olayın türü ve yükünü içeren sözlük.
                   Örn: {"cmd": "START_MODULATION", "mode": "Pressure"}
                        {"cmd": "EMERGENCY_STOP"}

        Returns:
            Optional[BaseState]:
                - None      → State değişmez.
                - BaseState → Yeni state'e geçiş yapılır.

        Not:
            WsServer, UI'dan gelen her mesajı parse ettikten sonra bu metodu
            await ile çağırır. İçeride await context.command_queue.put(...)
            veya await context.broadcaster.publish(...) çağrısı yapılabilir.
        """
        return None

    # ------------------------------------------------------------------
    # Yardımcı Metodlar
    # ------------------------------------------------------------------

    async def _publish_state(self) -> None:
        """
        Mevcut state adını WebSocket üzerinden UI'ya yayınlar.
        Alt sınıfların on_enter() içinden çağırması için ortak yardımcı.

        Örnek:
            async def on_enter(self) -> None:
                await self._publish_state()
        """
        await self.context.broadcaster.publish(
            "STATE_CHANGED",
            {"state": self.name}
        )

    async def _publish_alarm(self, code: int, reason: str = "") -> None:
        """
        Alarm kodunu WebSocket üzerinden UI'ya yayınlar.
        signal_bus.alarm_triggered.emit() çağrısının async karşılığıdır.

        Args:
            code:   AlarmCode int değeri (AlarmCode enum'unun int karşılığı).
            reason: İnsan okunabilir açıklama (opsiyonel, loglama için).
        """
        payload = {"code": code}
        if reason:
            payload["reason"] = reason
        await self.context.broadcaster.publish("ALARM_TRIGGERED", payload)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"