import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from core.app_context import AppContext

def create_app(context: AppContext) -> FastAPI:
    """
    FastAPI uygulamasını Factory Pattern ile oluşturur.
    AppContext bağımlılığı enjekte edilerek, sistemdeki tüm katmanların (Controller, HAL)
    aynı Broadcaster ve Queue referansları üzerinden haberleşmesi garanti altına alınır.
    
    Args:
        context (AppContext): Sistem genelindeki asenkron primitifleri barındıran obje.
        
    Returns:
        FastAPI: Ayağa kalkmaya hazır ASGI uygulaması.
    """
    app = FastAPI(title="Servo Control API - WebSocket Layer")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """
        Electron UI'dan gelen asenkron WebSocket bağlantılarını karşılar.
        Bağlantıyı ortak Broadcaster'a kaydeder ve UI komutlarını dinler.
        """
        await context.broadcaster.connect(websocket)
        
        try:
            while True:
                # UI'dan gelen komutları (örn: yeni setpoint, PID ayarı, mod değişimi) dinle
                raw_data = await websocket.receive_text()
                await _handle_client_message(raw_data, context)
                
        except WebSocketDisconnect:
            # İstemci kapandığında veya bağlantı koptuğunda kaydı sil
            await context.broadcaster.disconnect(websocket)
            
        except Exception as e:
            logging.error(f"[WebSocket Server] İstemci bağlantısında beklenmeyen hata: {e}")
            await context.broadcaster.disconnect(websocket)

    return app

async def _handle_client_message(raw_data: str, context: AppContext) -> None:
    """
    UI'dan gelen komutları parse eder ve doğrudan Controller katmanının işleyeceği
    command_queue (PriorityQueue) yapısına yönlendirir.

    Args:
        raw_data (str): UI'dan gelen ham JSON stringi.
        context (AppContext): Sistem context'i.
    """
    try:
        msg = json.loads(raw_data)
        cmd_type = msg.get("type")
        payload = msg.get("payload", {})

        # TODO: Controller/DataTypes katmanına göre MotorCommand objesi oluşturulacak.
        # Mimari Kurallara göre command_queue tuple formatındadır: (priority_int, MotorCommand)
        # Örnek kullanım:
        # if cmd_type == "SET_MODE":
        #     command = MotorCommand(action="MODE_CHANGE", value=payload.get("mode"))
        #     await context.command_queue.put((1, command))  # 1: NORMAL_PRIORITY
        # elif cmd_type == "EMERGENCY_STOP":
        #     command = MotorCommand(action="STOP")
        #     await context.command_queue.put((0, command))  # 0: EMERGENCY_PRIORITY

    except json.JSONDecodeError:
        logging.warning("[WebSocket Server] UI'dan geçersiz JSON formatı alındı.")
    except Exception as e:
        logging.error(f"[WebSocket Server] Komut işleme hatası: {e}")