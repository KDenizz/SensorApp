block_cipher = None
import os
from PyInstaller.utils.hooks import collect_all

ROOT = os.path.dirname(os.path.abspath(SPEC))

pymodbus_datas, pymodbus_binaries, pymodbus_hiddenimports = collect_all('pymodbus')

a = Analysis(
    ['modbus_simulator.py'],
    pathex=[ROOT],
    binaries=pymodbus_binaries,
    datas=pymodbus_datas,
    hiddenimports=[
        'pymodbus',
        'pymodbus.server',
        'pymodbus.datastore',
        'pymodbus.framer',
        'pymodbus.framer.rtu',
        'serial',
        'serial.serialwin32',
    ] + pymodbus_hiddenimports,
    excludes=[
        'torch', 'matplotlib', 'numpy', 'pandas',
        'scipy', 'sklearn', 'PyQt5', 'pygame',
        'jupyter', 'IPython',
    ],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Simulator",
    debug=False,
    strip=False,
    upx=True,
    console=True
)