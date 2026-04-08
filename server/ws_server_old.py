"""
server/ws_server.py

Frontend'den gelen WebSocket komutlarını alır, parse eder ve
command_queue üzerinden HALWriter'a iletir.

Frontend → Backend mesaj formatı (WsCommand):
    { "type": "KOMUT_ADI", "payload": { ... } }

Desteklenen komutlar:
    SET_MODE       → mode_select (40001) register'ına mod numarası yazar
    CALIBRATE      → control_word (40002) = 1 (Auto-Calibration)
    OPEN_FULL      → control_word (40002) = 2 (Tam Aç TTL)
    CLOSE_FULL     → control_word (40002) = 3 (Tam Kapat TTL)
    EMERGENCY_STOP → STOP_IMMEDIATE komutu, priority=0 (en yüksek)
"""

import json
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from core.app_context import AppContext
from core.data_types  import MotorCommand, CommandType

logger = logging.getLogger(__name__)


def create_app(context: AppContext) -> FastAPI:
    """
    FastAPI uygulamasını Factory Pattern ile oluşturur.
    AppContext bağımlılığı enjekte edilerek tüm katmanların aynı
    Broadcaster ve Queue referanslarını kullanması garanti edilir.
    """
    app = FastAPI(title="Servo Control API — WebSocket Layer")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        """
        Frontend'den gelen WebSocket bağlantılarını karşılar.
        Broadcaster'a kaydeder ve mesaj döngüsünü başlatır.
        """
        await context.broadcaster.connect(websocket)
        client = websocket.client
        logger.info(f"Frontend bağlandı: {client}")

        try:
            while True:
                raw_data = await websocket.receive_text()
                await _handle_client_message(raw_data, context)

        except WebSocketDisconnect:
            logger.info(f"Frontend bağlantısı kapandı: {client}")
            await context.broadcaster.disconnect(websocket)

        except Exception as e:
            logger.error(f"WebSocket bağlantısında beklenmeyen hata: {e}")
            await context.broadcaster.disconnect(websocket)

    return app


# ----------------------------------------------------------------
# Komut İşleyici
# ----------------------------------------------------------------

async def _handle_client_message(raw_data: str, context: AppContext) -> None:
    """
    Frontend'den gelen JSON komutunu parse eder ve
    command_queue'ya (priority, MotorCommand) tuple olarak ekler.

    Öncelik sistemi:
        0 = EMERGENCY  (STOP_IMMEDIATE)
        1 = NORMAL     (diğer tüm komutlar)
    """
    try:
        msg      = json.loads(raw_data)
        cmd_type = msg.get("type", "")
        payload  = msg.get("payload", {})

    except json.JSONDecodeError:
        logger.warning("Frontend'den geçersiz JSON alındı.")
        return

    # ----------------------------------------------------------
    # ACİL DURDURMA — En yüksek öncelik
    # HALWriter → control_word = 0
    # ----------------------------------------------------------
    if cmd_type == "EMERGENCY_STOP":
        logger.warning("Frontend: ACİL DURDURMA komutu alındı!")
        command = MotorCommand(
            type=CommandType.STOP_IMMEDIATE,
            priority=0,
        )
        await context.command_queue.put((0, command))

    # ----------------------------------------------------------
    # MOD SEÇİMİ
    # HALWriter → mode_select (40001) = mode değeri
    # value alanına mod numarası taşınır (1–6)
    # ----------------------------------------------------------
    elif cmd_type == "SET_MODE":
        mode = int(payload.get("mode", 1))
        if not (1 <= mode <= 6):
            logger.warning(f"Geçersiz mod numarası: {mode}. 1–6 arasında olmalı.")
            return

        logger.info(f"Frontend: Mod seçimi → Mod {mode}")
        command = MotorCommand(
            type=CommandType.MOVE_ABSOLUTE,  # HALWriter mod yazma için bunu kullanır
            value=float(mode),
            priority=1,
            metadata={"command_intent": "SET_MODE"},
        )
        await context.command_queue.put((1, command))

    # ----------------------------------------------------------
    # KALİBRASYON BAŞLAT
    # HALWriter → control_word (40002) = 1
    # ----------------------------------------------------------
    elif cmd_type == "CALIBRATE":
        logger.info("Frontend: Kalibrasyon başlatma komutu alındı.")
        command = MotorCommand(
            type=CommandType.CALIBRATE,
            priority=1,
        )
        await context.command_queue.put((1, command))

    # ----------------------------------------------------------
    # TAM AÇ (TTL)
    # HALWriter → control_word (40002) = 2
    # STOP komutu + direction=1 → HALWriter Tam Aç olarak yorumlar
    # ----------------------------------------------------------
    elif cmd_type == "OPEN_FULL":
        logger.info("Frontend: Tam Aç (TTL) komutu alındı.")
        command = MotorCommand(
            type=CommandType.STOP,
            direction=1,   # 1 → Aç
            priority=1,
        )
        await context.command_queue.put((1, command))

    # ----------------------------------------------------------
    # TAM KAPAT (TTL)
    # HALWriter → control_word (40002) = 3
    # STOP komutu + direction=-1 → HALWriter Tam Kapat olarak yorumlar
    # ----------------------------------------------------------
    elif cmd_type == "CLOSE_FULL":
        logger.info("Frontend: Tam Kapat (TTL) komutu alındı.")
        command = MotorCommand(
            type=CommandType.STOP,
            direction=-1,  # -1 → Kapat
            priority=1,
        )
        await context.command_queue.put((1, command))

    # ----------------------------------------------------------
    # Bilinmeyen komut
    # ----------------------------------------------------------
    else:
        logger.warning(f"Bilinmeyen WebSocket komutu: '{cmd_type}'")