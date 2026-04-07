import asyncio
import logging
from typing import Optional, List

from pymodbus.client import AsyncModbusSerialClient
from pymodbus.exceptions import ModbusException
from pymodbus.pdu import ExceptionResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adres Dönüşüm Yardımcıları
# ---------------------------------------------------------------------------
# Modbus konvansiyonu:  40001 → Holding Register 1
#                       30001 → Input Register 1
# pymodbus adres sistemi: 0-tabanlı (40001 → 0, 40002 → 1, 30001 → 0, vb.)
# Dönüşüm: pymodbus_addr = modbus_addr - offset
#   Holding (4x): offset = 40001
#   Input   (3x): offset = 30001
# Bu modülde TÜM public metodlar 0-tabanlı pymodbus adreslerini alır.
# Dönüşüm sorumluluğu modbus_config.py'dadır.
# ---------------------------------------------------------------------------


def _is_error_response(response) -> bool:
    """
    pymodbus 3.x'te yanıt nesnesinin hata olup olmadığını güvenli şekilde kontrol eder.
    None, ExceptionResponse veya isError() True dönen durumların tamamını yakalar.
    """
    if response is None:
        return True
    if isinstance(response, ExceptionResponse):
        return True
    try:
        return response.isError()
    except Exception:
        return True


class ModbusRTUClient:
    """
    Asenkron Modbus RTU İstemcisi.

    RS-485 / Seri port üzerinden Modbus Master olarak çalışır.
    Hat çakışmalarını önlemek için tüm I/O işlemlerinde asyncio.Lock() kullanır.

    Adres sistemi:
        Tüm public metodlar 0-tabanlı pymodbus adreslerini kabul eder.
        Kullanıcı tarafında Modbus konvansiyonu (40001, 30001 vb.) ile çalışılıyorsa
        dönüşüm modbus_config.py içindeki sabitler tarafından yapılmalıdır.

    Örnek kullanım:
        client = ModbusRTUClient(port="COM3", slave_id=1)
        await client.connect()
        regs = await client.read_input_registers(address=0, count=5)  # 30001–30005
        await client.write_register(address=0, value=2)               # 40001 = 2
        await client.disconnect()
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        timeout: float = 0.1,
        slave_id: int = 1,
    ):
        """
        :param port:      Seri port adresi (örn: 'COM3' veya '/dev/ttyUSB0')
        :param baudrate:  Haberleşme hızı — varsayılan 115200
        :param timeout:   Yanıt bekleme süresi (saniye)
        :param slave_id:  Hedef Modbus Slave ID'si
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.slave_id = slave_id

        self._client: Optional[AsyncModbusSerialClient] = None

        # RS-485 yarı-çift yönlü hat: aynı anda yalnızca tek işlem yapılabilir.
        self._bus_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Bağlantı Yönetimi
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """
        Bağlantı durumunu güvenli şekilde sorgular.
        Seri port aniden çekilse bile exception üretmez.
        """
        try:
            return self._client is not None and self._client.connected
        except Exception:
            return False

    async def connect(self) -> bool:
        """
        Seri port üzerinden Modbus RTU bağlantısını asenkron olarak başlatır.

        :return: Bağlantı başarılıysa True, aksi halde False
        """
        try:
            self._client = AsyncModbusSerialClient(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout,
                stopbits=1,
                bytesize=8,
                parity="N",
            )
            connected = await self._client.connect()
            if connected:
                logger.info(
                    f"Modbus RTU bağlantısı başarılı: {self.port} @ {self.baudrate} bps "
                    f"(slave={self.slave_id})"
                )
            else:
                logger.error(f"Modbus RTU bağlantısı başarısız: {self.port}")
            return connected

        except Exception as e:
            logger.critical(f"Modbus bağlanırken kritik donanım hatası: {e}")
            return False

    async def disconnect(self) -> None:
        """Modbus bağlantısını güvenli şekilde kapatır."""
        if self.is_connected:
            self._client.close()
            logger.info("Modbus RTU bağlantısı kapatıldı.")

    # ------------------------------------------------------------------
    # Okuma Metodları
    # ------------------------------------------------------------------

    async def read_input_registers(
        self, address: int, count: int
    ) -> Optional[List[int]]:
        """
        Input Register (3x) okur — Function Code 04.
        Sensör verileri (durum, konum, akım, dış sinyal, tork) buradan okunur.

        Register haritası (0-tabanlı pymodbus adresleri):
            0 → 30001 Durum Kelimesi
            1 → 30002 Mevcut Konum (Tur)
            2 → 30003 Mevcut Konum (Adım)
            3 → 30004 Okunan Dış Sinyal (mA × 100)
            4 → 30005 Anlık Tork/Motor Yükü (%)

        :param address: 0-tabanlı başlangıç adresi
        :param count:   Okunacak register sayısı
        :return:        UInt16 değer listesi veya hata durumunda None
        """
        if not self.is_connected:
            logger.warning("Modbus bağlı değil, input register okunamadı.")
            return None

        async with self._bus_lock:
            try:
                response = await self._client.read_input_registers(
                    address=address,
                    count=count,
                    slave=self.slave_id,
                )

                if _is_error_response(response):
                    logger.error(
                        f"Input Register okuma hatası "
                        f"(adres={address}, count={count}): {response}"
                    )
                    return None

                return list(response.registers)

            except ModbusException as e:
                logger.error(f"Modbus iletişim istisnası (input read): {e}")
                return None

    async def read_holding_registers(
        self, address: int, count: int
    ) -> Optional[List[int]]:
        """
        Holding Register (4x) okur — Function Code 03.
        Mevcut yazılmış konfigürasyon/komut değerlerini geri okumak için kullanılır.

        :param address: 0-tabanlı başlangıç adresi
        :param count:   Okunacak register sayısı
        :return:        UInt16 değer listesi veya hata durumunda None
        """
        if not self.is_connected:
            logger.warning("Modbus bağlı değil, holding register okunamadı.")
            return None

        async with self._bus_lock:
            try:
                response = await self._client.read_holding_registers(
                    address=address,
                    count=count,
                    slave=self.slave_id,
                )

                if _is_error_response(response):
                    logger.error(
                        f"Holding Register okuma hatası "
                        f"(adres={address}, count={count}): {response}"
                    )
                    return None

                return list(response.registers)

            except ModbusException as e:
                logger.error(f"Modbus iletişim istisnası (holding read): {e}")
                return None

    # ------------------------------------------------------------------
    # Yazma Metodları
    # ------------------------------------------------------------------

    async def write_register(self, address: int, value: int) -> bool:
        """
        Tek bir Holding Register (4x) yazar — Function Code 06.

        :param address: 0-tabanlı hedef adres
        :param value:   Yazılacak UInt16 değer (0–65535)
        :return:        Başarılıysa True
        """
        if not self.is_connected:
            logger.warning("Modbus bağlı değil, register yazılamadı.")
            return False

        async with self._bus_lock:
            try:
                response = await self._client.write_register(
                    address=address,
                    value=value,
                    slave=self.slave_id,
                )

                if _is_error_response(response):
                    logger.error(
                        f"Holding Register yazma hatası "
                        f"(adres={address}, değer={value}): {response}"
                    )
                    return False

                return True

            except ModbusException as e:
                logger.error(f"Modbus iletişim istisnası (write): {e}")
                return False

    async def write_registers(self, address: int, values: List[int]) -> bool:
        """
        Ardışık birden fazla Holding Register (4x) yazar — Function Code 16.

        Birden fazla parametreyi atomik (tek işlem) olarak göndermek için kullanılır.
        Örn: mod seçimi (40001) + komut kelimesi (40002) aynı anda yazılmak istenirse.

        :param address: 0-tabanlı başlangıç adresi
        :param values:  Yazılacak UInt16 değerler listesi
        :return:        Başarılıysa True
        """
        if not values:
            logger.warning("write_registers: Boş değer listesi gönderildi, atlanıyor.")
            return False

        if not self.is_connected:
            logger.warning("Modbus bağlı değil, register yazılamadı.")
            return False

        async with self._bus_lock:
            try:
                response = await self._client.write_registers(
                    address=address,
                    values=values,
                    slave=self.slave_id,
                )

                if _is_error_response(response):
                    logger.error(
                        f"Çoklu Register yazma hatası "
                        f"(adres={address}, değerler={values}): {response}"
                    )
                    return False

                return True

            except ModbusException as e:
                logger.error(f"Modbus iletişim istisnası (write_registers): {e}")
                return False