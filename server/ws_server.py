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
import asyncio

from core.app_context import AppContext
from core.data_types  import MotorCommand, CommandType

logger = logging.getLogger(__name__)
_last_goto: dict = {"ticks": -1, "time": 0.0}
GOTO_DEBOUNCE_S = 0.3

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
                try:
                    await _handle_client_message(raw_data, context)
                except Exception as e:
                    logger.error(f"WebSocket mesaj işleme hatası: {e}", exc_info=True)

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
            logger.warning(f"Geçersiz mod numarası: {mode}")
            return
        logger.info(f"Frontend: Mod seçimi → Mod {mode}")
        command = MotorCommand(
            type=CommandType.SET_MODE,
            value=float(mode),
            priority=1,
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
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.SET_MODE, value=3.0, priority=1
        )))
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.OPEN_FULL, priority=1
        )))

    # ----------------------------------------------------------
    # TAM KAPAT (TTL)
    # HALWriter → control_word (40002) = 3
    # STOP komutu + direction=-1 → HALWriter Tam Kapat olarak yorumlar
    # ----------------------------------------------------------
    elif cmd_type == "CLOSE_FULL":
        logger.info("Frontend: Tam Kapat (TTL) komutu alındı.")
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.SET_MODE, value=3.0, priority=1
        )))
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.CLOSE_FULL, priority=1
        )))



    # process_message içinde, GOTO_POSITION bloğuna girmeden önce:
    elif cmd_type == "GOTO_POSITION":
        turns    = int(payload.get("turns", 0))
        step     = int(payload.get("step", 0))
        step_res = int(context.config.hardware.get("step_resolution", 1000))
        target_ticks = turns * step_res + step

        # Debounce: aynı hedef 300ms içinde tekrar gelirse yok say
        now = asyncio.get_running_loop().time()
        if target_ticks == _last_goto["ticks"] and (now - _last_goto["time"]) < GOTO_DEBOUNCE_S:
            logger.debug(f"GOTO debounce: aynı hedef {target_ticks} tick, atlanıyor.")
            return
        _last_goto["ticks"] = target_ticks
        _last_goto["time"]  = now

        if not (0 <= target_ticks <= 32767):
            logger.warning(f"GOTO reddedildi: hedef tick {target_ticks} aralık dışı (0–32767).")
            return
        logger.info(f"Frontend: GOTO_POSITION → {turns} tur + {step} adım = {target_ticks} tick")
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.SET_MODE, value=3.0, priority=1
        )))
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.MOVE_ABSOLUTE, value=float(target_ticks), priority=1
        )))
        
    elif cmd_type == "STOP":
        logger.info("Frontend: DURDURMA komutu alındı.")
        command = MotorCommand(
            type=CommandType.STOP,
            priority=1,
        )
        await context.command_queue.put((1, command))


    elif cmd_type == "SET_TARGET_TURNS":
        target_turns = float(payload.get("turns", 0.0))
        if target_turns < 0:
                logger.warning(f"Geçersiz tur sayısı: {target_turns}")
                return
        logger.info(f"Frontend: Hedef Tur Sayısı → {target_turns} tur")
        command = MotorCommand(
                type=CommandType.SET_TARGET_TURNS,
                value=float(target_turns),
                priority=1,
        )
        await context.command_queue.put((1, command))

    elif cmd_type == "SET_TARGET_STEP":
        step = int(payload.get("step", 0))
        if step < 0:
            logger.warning(f"Geçersiz adım: {step}")
            return
        logger.info(f"Frontend: Hedef adım → {step}")
        command = MotorCommand(
            type=CommandType.SET_TARGET_STEP,
            value=float(step),
            priority=1,
        )
        await context.command_queue.put((1, command))

    elif cmd_type == "SET_PID_SETPOINT":
        setpoint = float(payload.get("setpoint", 0.0))
        if setpoint < 0:
            logger.warning(f"Geçersiz setpoint: {setpoint}")
            return
        if setpoint > 500:
            logger.warning(f"Setpoint güvenlik sınırı aşıldı: {setpoint:.1f} > 500 bar — reddedildi.")
            return
        
        logger.info(f"Frontend: PID setpoint → {setpoint} Bar")
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.SET_PID_SETPOINT, value=setpoint, priority=1
        )))


    elif cmd_type == "SET_PID_GAINS":
        kp = float(payload.get("kp", 0.0))
        ki = float(payload.get("ki", 0.0))
        kd = float(payload.get("kd", 0.0))
        logger.info(f"Frontend: PID kazançları → Kp={kp}, Ki={ki}, Kd={kd}")
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.SET_PID_GAINS, priority=1,
            metadata={"kp": kp, "ki": ki, "kd": kd},
        )))

    elif cmd_type == "SET_PID_DEADBAND":
        deadband = float(payload.get("deadband", 0.0))
        if deadband < 0:
            logger.warning(f"Geçersiz ölü bant: {deadband}")
            return
        logger.info(f"Frontend: PID ölü bant → {deadband} Bar")
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.SET_PID_DEADBAND, value=deadband, priority=1
        )))

    elif cmd_type == "SET_ADC_OFFSET":
        offset = float(payload.get("offset", 0.0))   # signed olabilir
        logger.info(f"Frontend: ADC ofset → {offset} Bar")
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.SET_ADC_OFFSET, value=offset, priority=1
        )))

    elif cmd_type == "SET_ADC_GAIN":
        gain = float(payload.get("gain", 1.0))
        if gain <= 0:
            logger.warning(f"Geçersiz gain (>0 olmalı): {gain}")
            return
        logger.info(f"Frontend: ADC gain → {gain}")
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.SET_ADC_GAIN, value=gain, priority=1
        )))

    elif cmd_type == "START_CALIBRATION":
        calib_dir   = int(payload.get("calib_dir", 0))
        seating     = float(payload.get("seating_load", 0))
        backoff     = float(payload.get("backoff_offset", 0))
        total_turns = int(payload.get("total_turns", 0))
        if total_turns <= 0:
            logger.warning(f"Kalibrasyon reddedildi: total_turns geçersiz ({total_turns}).")
            return
        if calib_dir not in (0, 1):
            logger.warning(f"Kalibrasyon reddedildi: calib_dir 0/1 olmalı ({calib_dir}).")
            return
        logger.info(f"Frontend: KALİBRASYON BAŞLAT → dir={calib_dir}, seating={seating}, "
                    f"backoff={backoff}, total_turns={total_turns}")
        await context.command_queue.put((1, MotorCommand(
            type=CommandType.START_CALIBRATION, priority=1,
            metadata={
                "calib_dir": float(calib_dir),
                "seating_load": seating,
                "backoff_offset": backoff,
                "total_turns": float(total_turns),
            },
        )))
    # ----------------------------------------------------------
    # Bilinmeyen komut
    # ----------------------------------------------------------
    else:
        logger.warning(f"Bilinmeyen WebSocket komutu: '{cmd_type}'")