# Native Steam Deck FPV Controller (`deck_controller/`)

A zero-compile, native 1280×800 Steam Deck (and macOS/Linux) FPV controller for **Mondo Robotics Beni** built with `uv`, `pygame-ce` (SDL2), `av` (PyAV), and `eclipse-zenoh`.

---

## 1. Features

- **Live Low-Latency FPV Video (`UCP` Channel 1):** Reassembles Annex-B H.265 (HEVC) / H.264 frames across UCP MTU fragments and decodes them with PyAV (`SLICE` low-latency threading) directly to a 1280×800 fullscreen HUD.
- **Live Telemetry HUD:** Connection status badge, Battery %, live FPS & codec, UCP RTT (ms), Speed Mode (`CHILL` / `NORMAL` / `SPORT`), Standing Height bar, Gimbal Pitch indicator, free storage (GB), and REC timer.
- **Full Gamepad & Steam Deck Controls:** Dual-stick driving + camera tilt, standing height (`LT`/`RT` or back paddles `L4`/`R4`), jump (`A` / `L5`), record (`X` / `R5`), photo (`Y`), audio mute (`LB`), optional gyro aim (`RB`), and an on-screen Quick Settings menu (`Menu` / `Start`).
- **Live Audio (`UCP` Channel 2):** Decodes Beni's 48 kHz Opus downlink audio stream with instant mute toggle.
- **Beni Voice (Offline TTS + Steam Deck On-Screen Keyboard):** Press `B` / `View` (`T` on keyboard) to open the Beni Voice overlay, trigger the native SteamOS On-Screen Keyboard (`SDL_StartTextInput` + `steam://open/keyboard`), synthesize speech offline (`espeak-ng` / `say` / built-in synth) with an optional robotic WALL-E voice filter, and stream 48 kHz Opus audio straight to Beni's speaker (`app/data/audio/playback_data` + UCP channel 2, with SSH `aplay` fallback for custom `/md` firmware).
- **Local Media Gallery (`:8084`):** Press `D-Pad Left` (`L` on keyboard) to browse photos/videos stored on Beni (`md_media_server` `/media/files`), preview thumbnails, and download clips to `~/Pictures/Beni/`.
- **Built-in `--mock` Beni Simulator:** Runs a real loopback UCP + Zenoh + HTTP `:8084` Beni simulator with a live encoded H.264 horizon/crosshair video stream that reacts to every stick, gimbal, height, jump, record, photo, and TTS input—so you can test on macOS or Steam Deck without hardware!

---

## 2. Quick Start

### Test Right Now on macOS or Steam Deck (No Hardware Needed)
```bash
cd open_ineb
uv run deck_controller/run.py --mock
```

### Connect to a Real Beni (Wi-Fi AP `192.168.7.1`)
```bash
uv run deck_controller/run.py --fullscreen --robot-ip 192.168.7.1
```

### One-Step Steam Deck Non-Steam Game Setup
In Steam Deck **Desktop Mode**, open Konsole and run:
```bash
git clone https://github.com/k5njm/open_ineb.git
cd open_ineb
./deck_controller/install_steam_deck.sh
```
Then open **Steam → Games → Add a Non-Steam Game to My Library…**, select **`Beni FPV Controller (open_ineb)`**, and launch it from Gaming Mode!

---

## 3. Controls Reference

| Steam Deck / Gamepad | Keyboard (macOS/Dev) | Action |
| :--- | :--- | :--- |
| **Left Stick** | `W` `A` `S` `D` | Drive forward / back / strafe |
| **Right Stick** | Arrow Keys | Turn (yaw) + Camera gimbal tilt |
| **LT / RT** (or **L4 / R4**) | `Q` / `E` | Standing height down / up |
| **A** (or **L5**) | `Space` | Jump |
| **X** (or **R5**) | `R` | Start / Stop video recording |
| **Y** | `P` | Capture photo |
| **B** / **View** | `T` | Open **Beni Voice (TTS + Steam OSK)** |
| **LB** | `M` | Mute / Unmute audio |
| **RB** | `G` | Toggle Gyro camera tilt |
| **D-Pad Left** | `L` | Open **Media Gallery** (`:8084`) |
| **Menu / Start** | `Tab` / `Esc` | Open **Quick Settings** menu |

---

## 4. Wire Protocol Summary (AV, Control & Media)

| Channel / Port | Protocol | Details |
| :--- | :--- | :--- |
| **UCP Channel 1** (`:9007` UDP) | Raw Annex-B **H.265 (HEVC)** | Reassembled across `UCP_FLAG_MORE_FRAG` (`0x0200`) until `more_frag == 0` (verified from `native_rx_video_callback`) |
| **UCP Channel 2** (`:9007` UDP) | **Opus** 48 kHz mono, 20 ms | Downlink mic / intercom stream (`native_rx_audio_callback`) |
| **UCP Channel 3** (`:9007` UDP) | **Zenoh-over-UCP** (`application/protobuf`) | Control & telemetry topics (`app/cmd/control/ctrl_cmd`, `app/cmd/camera/*`, `app/data/audio/playback_data`, `device/status/*`) |
| **HTTP `:8084`** | `md_media_server` REST | `GET /media/files`, `GET /media/files/{id}/thumbnail`, `GET /media/files/{id}/content` |
