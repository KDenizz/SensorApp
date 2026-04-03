import yaml
from pathlib import Path
from typing import Dict, Any

class ConfigError(Exception):
    """Konfigürasyon yönetimi ile ilgili temel hata sınıfı."""
    pass

class ConfigNotFoundError(ConfigError):
    """Beklenen konfigürasyon dosyası veya dizini bulunamadığında fırlatılır."""
    pass

class ConfigParseError(ConfigError):
    """YAML dosyası bozuk veya formatı geçersiz olduğunda fırlatılır."""
    pass


class ConfigParser:
    """
    Sistemin tüm statik konfigürasyonlarını (Donanım, PID, Akışkan, Valf) 
    tek bir noktadan yöneten, startup sırasında yüklenip read-only 
    kullanım sunan sınıf.
    """

    def __init__(self, config_root: str = "config"):
        """
        ConfigParser'ı başlatır ancak dosyaları henüz okumaz.
        
        Args:
            config_root: Konfigürasyon dosyalarının bulunduğu ana dizin yolu.
        """
        self._root_path = Path(config_root)
        
        # İç stateler (Yüklendikten sonra read-only olarak dışarı açılacak)
        self._hardware_config: Dict[str, Any] = {}
        self._pid_defaults: Dict[str, Any] = {}
        self._fluid_tables: Dict[str, Dict[str, Any]] = {}
        self._valve_profiles: Dict[str, Dict[str, Any]] = {}
        
        self._is_loaded = False

    def load_all(self) -> None:
        """
        Tüm konfigürasyon hiyerarşisini okur. 
        Eğer herhangi bir dosya bulunamazsa veya format hatalıysa exception fırlatır.
        """
        if self._is_loaded:
            return

        if not self._root_path.exists() or not self._root_path.is_dir():
            raise ConfigNotFoundError(f"Ana konfigürasyon dizini bulunamadı: {self._root_path.absolute()}")

        # 1. Tekil konfigürasyon dosyalarını oku
        self._hardware_config = self._load_yaml(self._root_path / "hardware.yaml")
        self._pid_defaults = self._load_yaml(self._root_path / "pid_defaults.yaml")

        # 2. Dizin altındaki tüm akışkan tablolarını oku
        fluids_dir = self._root_path / "fluid_tables"
        if fluids_dir.exists() and fluids_dir.is_dir():
            for yaml_file in fluids_dir.glob("*.yaml"):
                self._fluid_tables[yaml_file.stem] = self._load_yaml(yaml_file)
        if not fluids_dir.exists():
            import warnings
            warnings.warn(f"fluid_tables dizini bulunamadı: {fluids_dir}")
        
        # 3. Dizin altındaki tüm valf profellerini oku
        valves_dir = self._root_path / "valve_profiles"
        if valves_dir.exists() and valves_dir.is_dir():
            for yaml_file in valves_dir.glob("*.yaml"):
                self._valve_profiles[yaml_file.stem] = self._load_yaml(yaml_file)

        self._is_loaded = True

    def _load_yaml(self, file_path: Path) -> Dict[str, Any]:
        """
        Verilen dosya yolundaki YAML dosyasını güvenli bir şekilde okur.
        
        Args:
            file_path: Okunacak .yaml dosyasının yolu.
            
        Returns:
            Dict[str, Any]: Ayrıştırılmış YAML verisi.
            
        Raises:
            ConfigNotFoundError: Dosya diskte yoksa.
            ConfigParseError: YAML formatı hatalıysa.
        """
        if not file_path.exists() or not file_path.is_file():
            raise ConfigNotFoundError(f"Kritik konfigürasyon dosyası eksik: {file_path.absolute()}")

        try:
            with file_path.open('r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
                return data if data is not None else {}
        except yaml.YAMLError as e:
            raise ConfigParseError(f"YAML ayrıştırma hatası ({file_path.name}): {str(e)}")
        except Exception as e:
            raise ConfigError(f"Dosya okunurken beklenmeyen hata ({file_path.name}): {str(e)}")

    # Read-Only Property'ler (Dış katmanlar veriyi değiştiremez)
    
    @property
    def hardware(self) -> Dict[str, Any]:
        """Donanım ayarları ve limitleri."""
        self._ensure_loaded()
        return self._hardware_config

    @property
    def pid_defaults(self) -> Dict[str, Any]:
        """Varsayılan PID katsayıları (Kp, Ki, Kd) ve sampling süreleri."""
        self._ensure_loaded()
        return self._pid_defaults

    @property
    def fluid_tables(self) -> Dict[str, Dict[str, Any]]:
        """Sisteme tanıtılmış tüm akışkanların fiziksel özellikleri (gamma vb.)."""
        self._ensure_loaded()
        return self._fluid_tables

    @property
    def valve_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Valflerin mekanik (pitch, strok) ve karakteristik (Cv) profelleri."""
        self._ensure_loaded()
        return self._valve_profiles

    def _ensure_loaded(self) -> None:
        """Veriye erişilmeden önce load_all metodunun çağrıldığını doğrular."""
        if not self._is_loaded:
            raise ConfigError("Konfigürasyonlar yüklenmeden erişim sağlanamaz. Önce load_all() çağrılmalı.")