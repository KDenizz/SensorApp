import asyncio
import json
import time
from dataclasses import is_dataclass, asdict
from typing import Any, Dict, Set, Optional, List
from fastapi import WebSocket

class WsBroadcaster:
    """
    Sistem genelinde asenkron mesajlaşmayı ve UI'a WebSocket üzerinden veri aktarımını yöneten sınıf.
    Lazy initialization kullanılarak Event Loop'a tam uyumluluk sağlanmıştır.
    """

    ALLOWED_MSG_TYPES = {"SENSOR_DATA", "COMPUTED_DATA", "STATE_CHANGED", "ALARM_TRIGGERED"}

    def __init__(self) -> None:
        """
        Broadcaster objesini yaratır. Asenkron primitifler (Queue, Lock) 
        aktif bir Event Loop gerektirdiği için burada başlatılmaz (Lazy Init).
        """
        self.active_connections: Set[WebSocket] = set()
        self.message_queue: Optional[asyncio.Queue] = None
        self._lock: Optional[asyncio.Lock] = None

    async def initialize(self) -> None:
        """
        Main fonksiyonu içinde Event Loop ayağa kalktıktan sonra çağrılmalıdır.
        Asenkron primitiflerin güvenli bir şekilde Event Loop'a bağlanmasını sağlar.
        """
        self.message_queue = asyncio.Queue()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        """
        Yeni bir WebSocket bağlantısını kabul eder ve istemci listesine thread-safe olarak ekler.
        
        Args:
            websocket (WebSocket): İstemci bağlantı nesnesi.
        """
        if self._lock is None:
            raise RuntimeError("Broadcaster initialize() edilmeden connect() çağrılamaz.")

        await websocket.accept()
        async with self._lock:
            self.active_connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        """
        Kopan veya sonlandırılan bağlantıyı istemci listesinden güvenlice çıkarır.
        Discard atomik olsa da asenkron tutarlılık için Lock kullanılır.
        
        Args:
            websocket (WebSocket): Kaldırılacak bağlantı nesnesi.
        """
        if self._lock:
            async with self._lock:
                self.active_connections.discard(websocket)

    async def publish(self, msg_type: str, payload: Any) -> None:
        """
        Veriyi formata sokar ve yayın kuyruğuna asenkron olarak ekler. Çağrıldığı task'i bloklamaz.

        Args:
            msg_type (str): Kabul edilen 4 mesaj tipinden biri.
            payload (Any): Dataclass veya Dict formatında iletilecek veri.
        """
        if self.message_queue is None:
            raise RuntimeError("Broadcaster initialize() edilmeden publish() çağrılamaz.")

        if msg_type not in self.ALLOWED_MSG_TYPES:
            raise ValueError(f"Geçersiz mesaj tipi: {msg_type}")

        # Payload standardizasyonu
        payload_dict: Dict[str, Any]
        if is_dataclass(payload):
            payload_dict = asdict(payload)
        elif isinstance(payload, dict):
            payload_dict = payload
        else:
            payload_dict = {"data": str(payload)}

        message = {
            "type": msg_type,
            "payload": payload_dict,
            "timestamp": time.monotonic()  # NTP sapmalarına karşı korumalı sistem zamanı
        }
        
        await self.message_queue.put(message)

    async def broadcast_loop(self, stop_event: asyncio.Event) -> None:
        """
        Kuyruğa gelen mesajları tüketerek aktif istemcilere dağıtan ana asenkron döngü.

        Args:
            stop_event (asyncio.Event): Sistemin güvenli kapanması için trigger.
        """
        if self.message_queue is None or self._lock is None:
            raise RuntimeError("Broadcaster initialize() edilmeden broadcast_loop() başlatılamaz.")

        while not stop_event.is_set():
            try:
                message = await asyncio.wait_for(self.message_queue.get(), timeout=0.05)
                msg_str = json.dumps(message)

                # Set'in iterasyon sırasında boyut değiştirmesini engellemek için kopya alıyoruz.
                # I/O beklemesi yaparken (await send_text) Lock'u tutmak performansı öldüreceğinden, 
                # sadece kopyalama aşamasında Lock kullanıyoruz.
                async with self._lock:
                    clients_snapshot: List[WebSocket] = list(self.active_connections)

                dead_clients: Set[WebSocket] = set()
                
                for client in clients_snapshot:
                    try:
                        await client.send_text(msg_str)
                    except Exception:
                        dead_clients.add(client)

                # Ölü client'ları Lock koruması altında temizle
                if dead_clients:
                    async with self._lock:
                        for dead in dead_clients:
                            self.active_connections.discard(dead)

                self.message_queue.task_done()

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception as e:
                # Üretim ortamında loglanmalı
                print(f"[Broadcaster Error] {e}")