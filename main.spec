import os
from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
ROOT = os.path.dirname(os.path.abspath(SPEC))

pymodbus_datas, pymodbus_binaries, pymodbus_hiddenimports = collect_all('pymodbus')
fastapi_datas,  fastapi_binaries,  fastapi_hiddenimports  = collect_all('fastapi')
uvicorn_datas,  uvicorn_binaries,  uvicorn_hiddenimports  = collect_all('uvicorn')

a = Analysis(
    [os.path.join(ROOT, 'main.py')],
    pathex=[ROOT],
    binaries=pymodbus_binaries + fastapi_binaries + uvicorn_binaries,
    datas=[
    
        (
            r"E:\SAGAY\Servo\SensorAppGui\sensor_gui\dist",
            os.path.join('sensor_gui', 'dist')
        ),
        (os.path.join(ROOT, 'setup.html'),         '.'),
    ] + pymodbus_datas + fastapi_datas + uvicorn_datas,
    hiddenimports=[
        'uvicorn.logging',
        'uvicorn.loops', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
        'uvicorn.protocols', 'uvicorn.protocols.http', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets', 'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan', 'uvicorn.lifespan.off', 'uvicorn.lifespan.on',
        'fastapi', 'starlette', 'starlette.middleware', 'starlette.middleware.cors',
        'starlette.routing', 'starlette.staticfiles',
        'pymodbus', 'pymodbus.client', 'pymodbus.client.serial',
        'pymodbus.framer', 'pymodbus.framer.rtu',
        'pyserial', 'serial', 'serial.serialwin32',
        'yaml', 'asyncio', 'asyncio.queues', 'websockets',
    ]
    + pymodbus_hiddenimports + fastapi_hiddenimports + uvicorn_hiddenimports
    + collect_submodules('pymodbus')
    + collect_submodules('uvicorn')
    + collect_submodules('starlette'),
    hookspath=[],
    runtime_hooks=[],
    excludes=['torch','matplotlib','numpy','pandas','scipy','sklearn','PyQt5','pygame','jupyter','IPython','PIL','cv2'],
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
    name="Backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
)


