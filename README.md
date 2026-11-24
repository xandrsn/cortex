# Cortex

Una sola app para Windows con dos cosas:

- **Asistente de voz:** le hablas («Cortex, pon…», «Cortex, recuérdame…»); tiene un panel en el navegador.
- **Control de ventanas con gestos:** con la webcam, pellizcas y arrastras ventanas, las encajas en mitades de pantalla, minimizas con un movimiento, maximizas con el puño abierto y activas/desactivas con la señal de paz.

Hecho por [Charlie Andersen](https://charlieandersen.vercel.app).

## Descargar

Última versión: [Releases](https://github.com/xandrsn/cortex/releases) → `CortexSetup.exe`. No hace falta ser administrador; Windows puede avisar porque el instalador no está firmado.

1. Idioma y carpeta.
2. Tarjetas del panel (se pueden cambiar después).
3. Cuentas que quieres conectar. El instalador no pide contraseñas.
4. Al abrir Cortex, un asistente te guía por cada inicio de sesión (Claude, Spotify, correo y, si va incluido, WhatsApp). Todos se pueden saltar y repetir desde Ajustes → Cerebro.

El panel está en http://127.0.0.1:8765. Al desinstalar se conservan tus datos y se te dice en qué carpeta están.

## Requisitos

- Windows 10/11 de 64 bits.
- Webcam (gestos de ventanas), micrófono y altavoces (voz).
- Para el asistente: [Claude Code](https://claude.com/claude-code) instalado y con sesión iniciada (el asistente de configuración te ayuda), y conexión a internet (voz de Edge TTS).
- La primera vez, el reconocimiento de voz descarga su modelo.

Para compilar desde el código: Python 3.12 o 3.13 y `requirements.txt` (ver abajo).

## Privacidad

- **La webcam se procesa en tu equipo.** MediaPipe corre localmente; los fotogramas no se guardan ni se envían.
- **El asistente de voz tiene el micro siempre encendido** mientras espera «Cortex» y puede activarse por error. Se pausa con el botón del oído o en Ajustes → Voz.
- **El audio** se reconoce en local. Lo que dices se envía a Claude Code (tu cuenta) para responder, y el texto de las respuestas se convierte a voz con Edge TTS (internet).
- **WhatsApp** (opcional) usa un cliente no oficial: WhatsApp podría limitar o bloquear la cuenta. Al instalar eliges «solo leer» o «leer y escribir» (clave `whatsapp_mode`); el asistente nunca envía, solo tú desde un chat abierto. El puente escucha en `127.0.0.1:8080` sin contraseña: el permiso lo aplica Cortex, no el puente.
- Las claves y contraseñas se guardan en tu equipo: `.env` (junto al programa) y el Administrador de credenciales de Windows para las cuentas web. No van en el instalador ni en el repositorio.

## Configuración

Los ajustes están en `config.yaml` junto a `Cortex.exe` (se crea al cambiarlos en el panel). Claves útiles:

- `user_name` (o la variable `CORTEX_USER_NAME`): cómo te llama Cortex en los textos (vacío = «el usuario»).
- `window_gestures` (true): control de ventanas con gestos.
- `gestures_enabled` (false): gestos de la visión del asistente. Comparte webcam con el anterior, por eso viene apagado.
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

Resultado: `dist\CortexSetup.exe`. Comprobación sin webcam ni micro: `dist\Cortex\Cortex.exe --selftest` (sin consola, el resultado va a `logs\console.log`).
Con `-Bridge <carpeta del puente>` el instalador ofrece WhatsApp. El puente usa whatsmeow (MPL-2.0) y libsignal (GPL-3.0): revisa esas licencias antes de distribuirlo.

## Web y demo

`site/` es la web (Vercel, estática). La demo es el panel real con datos inventados y sin servidor; tras cambiar `web/`, ejecuta `python site/build.py`.

Desde el código: `.\.venv\Scripts\python cortex.py`.

## Límites conocidos

- Solo Windows (Win32, pycaw, comtypes).
- La palabra de activación es «Cortex». Se reconoce por transcripción y por sonido; el detector entrenado antes («Pepito») no sirve y no hay uno nuevo.
- Los modelos grandes no van en el instalador: se descargan o se copian a `models\` junto al programa.
- Spotify, banco y WhatsApp necesitan sus propias cuentas y claves; no están probados en esta build.
- La build no se ha probado con webcam ni micrófono reales.
