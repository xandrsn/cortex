# Cortex

A personal, AI-focused side project for Windows: two small apps that work alone or together.

| | **Cortex Sense** | **Cortex Hub** |
|---|---|---|
| What | Turns webcam hand gestures into commands | Voice assistant + dashboard backed by an LLM |
| AI | MediaPipe hand/face landmarks, rule-based + few-shot custom gesture classifier, per-user calibration | Wake word (openWakeWord + transcription), local speech-to-text (faster-whisper / onnx-asr), Silero VAD, Claude Code as the reasoning layer, neural TTS (Piper / Edge TTS) |
| Panel | http://127.0.0.1:8766 | http://127.0.0.1:8765 |
| Runs | `python cortex_sense.py` | `python cortex_hub.py` |

Built by [Charlie Andersen](https://charlieandersen.vercel.app). Hobby project, not a product: Windows only, unsigned installers, tested by its author.

## How they connect

```
 webcam ──► Cortex Sense ──────────── local WebSocket (ws://127.0.0.1:8766/ws) ─────────► Cortex Hub
            landmarks → gesture        {"type":"gesture","gesture":"swipe_left",            voice, LLM, music,
            gesture → action            "action":"next","handled_by":"hub"}                 dashboard
            runs keys/media/apps/window ◄── {"type":"hello","role":"hub"}, settings, yield_cpu
```

- **Sense owns the camera and the mapping** (`sense.yaml`). Each gesture maps to one action:
  - a Hub action (`next`, `toggle`, `listen`, `new_chat`, …), sent to Hub when it is connected;
  - `media:<key>`, `keys:ctrl+shift+m`, `run:<program or URL>`, `window:<minimize|maximize|close|desktop|switch>` or `none`, run by Sense itself.
- **Without Hub**, music actions fall back to the system media keys, so Sense is useful on its own.
- **Hub without Sense** works as a voice assistant; its gesture settings are proxied to Sense when it is running.
- One webcam reader is shared between the gesture recogniser and the pinch-and-drag window controller.
- Only local panels may change settings: requests with a foreign `Origin` are rejected (CSRF from other websites).

## Cortex Sense

- **29 gestures:** 12 static poses (thumb up, fist, victory, OK, rock, shush…), 11 motions (swipes, flicks, circles, wave, push) and 6 two-hand gestures. A gesture only fires while you look at the screen (head pose from face landmarks), after a short hold and with a cooldown.
- **Custom gestures:** record a pose a few times from the panel; it is matched by normalised landmark distance.
- **Calibration wizard** (first run, or *Recalibrate*), so it adapts to each webcam and person:
  1. *Look* — measures your head pitch at the screen, room brightness and camera fps.
  2. *Hand* — hand size in frame scales the swipe distance (closer camera → longer swipes).
  3. *Pinch* — your own pinch range sets the grab/release thresholds for dragging windows.
  The report warns about dark rooms and slow cameras.
- **Window control:** pinch and drag windows, snap them to zones, minimise/maximise with gestures.
- Video is processed on the machine and never stored or uploaded.

## Cortex Hub

- Say «Cortex…» or press the button; speech is transcribed locally, answered through Claude Code (your own account) and spoken back.
- Dashboard cards: chat, music (Spotify / media keys), timers, shopping list, weather, mail, notices; optional WhatsApp and bank summaries.
- Accounts are connected from a setup wizard; the installer never asks for passwords.
- Secrets stay on the machine: `.env` next to the program and Windows Credential Manager. Nothing is bundled or committed.

## Install

Releases ship two installers, no admin rights needed (Windows may warn because they are unsigned):

- `CortexSenseSetup.exe` — webcam privacy notice, optional autostart, opens the calibration wizard.
- `CortexHubSetup.exe` — voice assistant and dashboard.

Requirements: Windows 10/11 x64; webcam for Sense; microphone and speakers plus [Claude Code](https://claude.com/claude-code) signed in for Hub.

## From source

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install uv
.\.venv\Scripts\uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
.\.venv\Scripts\python -m pytest            # unit + API tests, no webcam or mic needed
.\.venv\Scripts\python cortex_sense.py --selftest
.\.venv\Scripts\python cortex_hub.py --selftest
.\.venv\Scripts\python cortex_sense.py --open   # Sense + panel
.\.venv\Scripts\python cortex_hub.py            # Hub
powershell -ExecutionPolicy Bypass -File build.ps1 [-Only Hub|Sense]
```

Build output: `dist\CortexHubSetup.exe`, `dist\CortexSenseSetup.exe`. With `-Bridge <folder>` the Hub installer offers WhatsApp; that bridge uses whatsmeow (MPL-2.0) and libsignal (GPL-3.0), so check those licences before redistributing it.

## Layout

```
cortex_sense.py   cortex_hub.py        entry points
src/sense/        camera sharing, vision, commands, calibration, FastAPI server
src/sense/window/ pinch-and-drag window controller
src/hub/          assistant, speech, LLM bridge, integrations, FastAPI server, SenseLink client
web/sense/        Sense panel          web/hub/  Hub dashboard
site/             landing page + static demo (python site/build.py after changing web/hub)
installer/        Inno Setup scripts   tests/    pytest + node tests
```

## Configuration

- `sense.yaml` (next to Cortex Sense): `gesture_actions`, `gestures_require_look`, `window_gestures`, `gesture_hold_seconds`, `gesture_cooldown`, `camera_index`, calibration results.
- `config.yaml` (next to Cortex Hub): `user_name`, `ui_port`, `sense_url`, `data_dir`, voice and model options.
- `.env` (Hub, you provide it): `SPOTIFY_CLIENT_ID`; `EB_APP_ID`, `EB_KEY_PATH`, optional `EB_REDIRECT_URL` (Enable Banking). Without them those features stay off.

## Known limits

- Windows only (Win32 APIs, pycaw, comtypes).
- The gesture runtime and calibration need a real webcam; automated tests cover the logic, API and protocol, not live video.
- Large speech models are downloaded on first use or copied into `models\`, not bundled.
- Spotify, bank and WhatsApp need their own accounts and keys and are the least tested parts.
- WhatsApp support uses an unofficial client; WhatsApp may limit the account. The assistant never sends messages on its own.
