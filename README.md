# Cortex

Una sola app para Windows con dos cosas:

- **Asistente de voz (Pepito):** le hablas («Pepito, pon…», «Pepito, recuérdame…»); tiene un panel en el navegador.
- **Control de ventanas con gestos:** con la webcam, pellizcas y arrastras ventanas, las encajas en mitades de pantalla, minimizas con un movimiento, maximizas con el puño abierto y activas/desactivas con la señal de paz.

Hecho por [Charlie Andersen](https://charlieandersen.vercel.app).

## Descargar

Última versión: [Releases](https://github.com/xandrsn/cortex/releases) → `Cortex-windows.zip` (255 MB).
Descomprime la zip en una carpeta y abre `Cortex.exe`. El panel se abre en http://127.0.0.1:8765.

## Requisitos

- Windows 10/11 de 64 bits.
- Webcam (gestos de ventanas), micrófono y altavoces (voz).
- Para el asistente: [Claude Code](https://claude.com/claude-code) instalado (`claude` en el PATH) con sesión iniciada, y conexión a internet (voz de Edge TTS).
- La primera vez, el reconocimiento de voz descarga su modelo.

Para compilar desde el código: Python 3.12 o 3.13 y `requirements.txt` (ver abajo).

## Privacidad

- **La webcam se procesa en tu equipo.** MediaPipe corre localmente; los fotogramas no se guardan ni se envían.
- **El audio** se reconoce en local. Lo que dices se envía a Claude Code (tu cuenta) para responder, y el texto de las respuestas se convierte a voz con Edge TTS (internet).
- Las claves y contraseñas se guardan en tu equipo: `.env` (junto al programa) y el Administrador de credenciales de Windows para las cuentas web. No van en la zip ni al repositorio.

## Configuración

Los ajustes están en `config.yaml` junto a `Cortex.exe` (se crea al cambiarlos en el panel). Claves útiles:

- `user_name` (o la variable `CORTEX_USER_NAME`): cómo te llama Pepito en los textos (vacío = «el usuario»).
- `window_gestures` (true): control de ventanas con gestos.
- `gestures_enabled` (false): gestos de la visión de Pepito. Comparte webcam con el anterior, por eso viene apagado.
- `ui_port` (8765): puerto del panel.
- `data_dir`: carpeta de datos (por defecto `datos-local`).

Claves que debes aportar tú, en un `.env` junto a `Cortex.exe` (nunca se incluyen): `SPOTIFY_CLIENT_ID` (Spotify), `EB_APP_ID`, `EB_KEY_PATH` y `EB_REDIRECT_URL` opcional (banco, Enable Banking). Sin ellas esas funciones quedan desactivadas. Las cuentas de correo se pueden conectar desde el panel.

## Ejecutar desde el código y compilar

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install uv
.\.venv\Scripts\uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest
powershell -ExecutionPolicy Bypass -File build.ps1
```

Resultado: `dist\Cortex-windows.zip`. Comprobación sin webcam ni micro: `dist\Cortex\Cortex.exe --selftest`.

Desde el código: `.\.venv\Scripts\python cortex.py`.

## Límites conocidos

- Solo Windows (Win32, pycaw, comtypes).
- La palabra de activación es «Pepito» (el modelo está entrenado con ese nombre).
- Los modelos grandes no van en la zip: se descargan o se copian a `models\` junto al programa.
- Spotify, banco y WhatsApp necesitan sus propias cuentas y claves; no están probados en esta build.
- La build no se ha probado con webcam ni micrófono reales.
