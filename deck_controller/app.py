"""
Native 1280x800 Steam Deck FPV Controller & HUD (`pygame-ce` / SDL2).

Controls (Gamepad / Steam Deck | Keyboard Fallback):
  Left Stick  / WASD        : Drive (vx, vy)
  Right Stick / Arrows      : Yaw + Camera Gimbal Tilt
  LT/RT or L4/R4            : Standing Height Down / Up  (Q / E)
  A  or L5                  : Jump                       (Space)
  X  or R5                  : Start / Stop Recording     (R)
  Y                         : Take Photo                 (P)
  B  or View/Select         : Beni Voice (TTS + OSK)     (T)
  LB                        : Mute / Unmute Audio        (M)
  RB                        : Toggle Gyro Camera Tilt    (G)
  D-Pad Left                : Local Media Gallery        (L)
  Menu / Start              : Quick Settings Menu        (Tab / Esc)
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pygame

from .client import BeniDeckClient
from .media import BeniMediaClient, MEDIA_DEFAULT_PORT, MediaItem
from .mock_beni import MockBeniServer
from .tts import QUICK_PHRASES, SAMPLE_RATE

SCREEN_W = 1280
SCREEN_H = 800
SPEED_MODES = [("CHILL", 0.55), ("NORMAL", 1.0), ("SPORT", 1.45)]


def _deadzone(val: float, dz: float = 0.10) -> float:
    if abs(val) < dz:
        return 0.0
    return (val - dz) / (1.0 - dz) if val > 0 else (val + dz) / (1.0 - dz)


def _open_steam_osk() -> None:
    """Trigger SteamOS native On-Screen Keyboard in Gaming Mode if available."""
    pygame.key.start_text_input()
    pygame.key.set_text_input_rect(pygame.Rect(240, 180, 800, 60))
    if sys.platform.startswith("linux") and (
        os.environ.get("SteamDeck") or os.environ.get("STEAM_GAMEPADUI") or os.environ.get("SteamOS")
    ):
        try:
            subprocess.Popen(
                ["xdg-open", "steam://open/keyboard"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


class DeckControllerApp:
    def __init__(
        self,
        *,
        robot_ip: str = "192.168.7.1",
        ucp_port: int = 9007,
        media_port: int = MEDIA_DEFAULT_PORT,
        zenoh_mode: str = "peer",
        mock: bool = False,
        fullscreen: bool = False,
        download_dir: Path | None = None,
    ) -> None:
        self.robot_ip = "127.0.0.1" if mock else robot_ip
        self.ucp_port = ucp_port
        self.media_port = media_port
        self.zenoh_mode = zenoh_mode
        self.mock = mock
        self.fullscreen = fullscreen
        self.download_dir = download_dir or (Path.home() / "Pictures" / "Beni")

        self._mock_srv: MockBeniServer | None = None
        self._client: BeniDeckClient | None = None
        self._media: BeniMediaClient | None = None

        # Runtime settings
        self.speed_idx = 1
        self.eis_enabled = True
        self.high_res_vt = True
        self.gyro_enabled = False
        self.invert_pitch = False
        self.robot_tts_voice = True
        self.show_hud = True
        self.audio_muted = False

        # Control state
        self.gimbal_pitch = 0.0  # -35..+35 deg
        self.height_norm = 0.5  # 0.0..1.0
        self.recording = False

        # Active overlay: None | "settings" | "tts" | "gallery"
        self.overlay: str | None = None
        self.settings_idx = 0

        # TTS overlay state
        self.tts_input = ""
        self.tts_phrase_idx = 0

        # Gallery overlay state
        self.gallery_items: list[MediaItem] = []
        self.gallery_idx = 0
        self.gallery_thumbs: dict[str, pygame.Surface] = {}
        self.gallery_loading = False

        # Toast banner
        self._toast_text = ""
        self._toast_until = 0.0

        # Pygame / Audio
        self._screen: pygame.Surface | None = None
        self._audio_ok = False
        self._audio_channel: pygame.mixer.Channel | None = None
        self._video_surf: pygame.Surface | None = None
        self._last_frame_seq = -1
        self._joystick: pygame.joystick.JoystickType | None = None

    def show_toast(self, msg: str, duration: float = 2.6) -> None:
        self._toast_text = msg
        self._toast_until = time.monotonic() + duration

    def _init_pygame(self) -> None:
        pygame.init()
        pygame.joystick.init()
        flags = pygame.FULLSCREEN if self.fullscreen else pygame.RESIZABLE
        self._screen = pygame.display.set_mode((SCREEN_W, SCREEN_H), flags)
        pygame.display.set_caption(
            "open_ineb — Beni Steam Deck Controller" + (" [MOCK]" if self.mock else "")
        )
        self._font_lg = pygame.font.SysFont("DejaVu Sans,Helvetica,Arial", 28, bold=True)
        self._font_md = pygame.font.SysFont("DejaVu Sans,Helvetica,Arial", 20, bold=False)
        self._font_sm = pygame.font.SysFont("DejaVu Sans,Helvetica,Arial", 16, bold=False)

        if pygame.joystick.get_count() > 0:
            self._joystick = pygame.joystick.Joystick(0)
            self._joystick.init()

        try:
            pygame.mixer.init(frequency=SAMPLE_RATE, size=-16, channels=1, buffer=1024)
            self._audio_channel = pygame.mixer.Channel(0)
            self._audio_ok = True
        except Exception:
            self._audio_ok = False

    def _play_pcm(self, pcm_i16: np.ndarray) -> None:
        if not self._audio_ok or self.audio_muted or self._audio_channel is None:
            return
        try:
            init = pygame.mixer.get_init()
            ch = init[2] if init else 1
            buf = np.repeat(pcm_i16, ch) if ch > 1 else pcm_i16
            snd = pygame.mixer.Sound(buffer=buf.tobytes())
            if self._audio_channel.get_busy():
                self._audio_channel.queue(snd)
            else:
                self._audio_channel.play(snd)
        except Exception:
            pass

    def _start_backend(self) -> None:
        if self.mock:
            self._mock_srv = MockBeniServer()
            self._mock_srv.start()
            self.ucp_port = self._mock_srv.ucp_port
            self.media_port = self._mock_srv.media_port

        self._media = BeniMediaClient(self.robot_ip, self.media_port)
        self._client = BeniDeckClient(
            robot_ip=self.robot_ip,
            ucp_port=self.ucp_port,
            zenoh_mode=self.zenoh_mode,
            on_audio_pcm=self._play_pcm,
        )
        try:
            self._client.connect(timeout=6.0)
            self.show_toast(
                f"Connected to {'Mock Beni' if self.mock else self.robot_ip}:{self.ucp_port}"
            )
        except Exception as exc:
            self.show_toast(f"Offline ({exc})", duration=5.0)

    def _shutdown(self) -> None:
        if self._client is not None:
            self._client.close()
        if self._mock_srv is not None:
            self._mock_srv.close()
        pygame.quit()

    # ---- Overlay Actions ---------------------------------------------------

    def _open_tts(self) -> None:
        self.overlay = "tts"
        self.tts_input = ""
        _open_steam_osk()

    def _close_overlay(self) -> None:
        if self.overlay == "tts":
            pygame.key.stop_text_input()
        self.overlay = None

    def _trigger_speak(self, text: str) -> None:
        phrase = text.strip()
        if not phrase:
            phrase = QUICK_PHRASES[self.tts_phrase_idx]
        if self._client is not None:
            self._client.speak_tts_async(
                phrase,
                robot_voice=self.robot_tts_voice,
                ssh_fallback=not self.mock,
                local_preview_cb=self._play_pcm if self.mock else None,
            )
        self.show_toast(f'Beni says: "{phrase}"', duration=3.0)
        self._close_overlay()

    def _open_gallery(self) -> None:
        self.overlay = "gallery"
        self.gallery_idx = 0
        self.gallery_loading = True
        threading.Thread(target=self._load_gallery_worker, daemon=True).start()

    def _load_gallery_worker(self) -> None:
        if self._media is None:
            self.gallery_loading = False
            return
        try:
            items = self._media.list_files()
            self.gallery_items = items
            for item in items[:12]:
                raw = self._media.fetch_thumbnail(item.file_id)
                if raw:
                    try:
                        surf = pygame.image.load(io.BytesIO(raw))
                        self.gallery_thumbs[item.file_id] = pygame.transform.smoothscale(
                            surf, (360, 202)
                        )
                    except Exception:
                        pass
        except Exception as exc:
            self.show_toast(f"Gallery error: {exc}", duration=3.0)
        finally:
            self.gallery_loading = False

    def _download_selected_media(self) -> None:
        if not self.gallery_items or self._media is None:
            return
        item = self.gallery_items[self.gallery_idx]
        self.show_toast(f"Downloading {item.filename}...")

        def _worker() -> None:
            assert self._media is not None
            try:
                out = self._media.download_file(item, self.download_dir)
                self.show_toast(f"Saved: {out.name}", duration=3.5)
            except Exception as exc:
                self.show_toast(f"Download failed: {exc}", duration=3.5)

        threading.Thread(target=_worker, daemon=True).start()

    def _settings_entries(self) -> list[tuple[str, str]]:
        return [
            ("Speed Mode", SPEED_MODES[self.speed_idx][0]),
            ("EIS Stabilization", "ON" if self.eis_enabled else "OFF"),
            ("Video Stream Quality", "1080p" if self.high_res_vt else "720p"),
            ("Gyro Camera Tilt", "ON" if self.gyro_enabled else "OFF"),
            ("Invert Pitch", "ON" if self.invert_pitch else "OFF"),
            ("Robot Voice Filter (TTS)", "ON" if self.robot_tts_voice else "OFF"),
            ("Audio Output", "MUTED" if self.audio_muted else "ON"),
            ("HUD Overlay", "ON" if self.show_hud else "OFF"),
            ("Open Media Gallery", "->"),
            ("Beni Voice (TTS)", "->"),
            ("Quit Controller", "EXIT"),
        ]

    def _activate_setting(self) -> bool:
        """Returns False if user chose Quit."""
        idx = self.settings_idx
        if idx == 0:
            self.speed_idx = (self.speed_idx + 1) % len(SPEED_MODES)
        elif idx == 1:
            self.eis_enabled = not self.eis_enabled
            if self._client:
                self._client.set_camera_settings(
                    eis_enable=self.eis_enabled, vt_index=1 if self.high_res_vt else 0
                )
        elif idx == 2:
            self.high_res_vt = not self.high_res_vt
            if self._client:
                self._client.set_camera_settings(
                    eis_enable=self.eis_enabled, vt_index=1 if self.high_res_vt else 0
                )
        elif idx == 3:
            self.gyro_enabled = not self.gyro_enabled
        elif idx == 4:
            self.invert_pitch = not self.invert_pitch
        elif idx == 5:
            self.robot_tts_voice = not self.robot_tts_voice
        elif idx == 6:
            self.audio_muted = not self.audio_muted
            if self._client:
                self._client.audio_muted = self.audio_muted
        elif idx == 7:
            self.show_hud = not self.show_hud
        elif idx == 8:
            self._open_gallery()
        elif idx == 9:
            self._open_tts()
        elif idx == 10:
            return False
        return True

    # ---- Input Sampling ----------------------------------------------------

    def _sample_drive_inputs(self, dt: float) -> tuple[float, float, float, bool]:
        keys = pygame.key.get_pressed()
        vx = vy = yaw = tilt_in = 0.0
        jump = False

        if self.overlay is None:
            if keys[pygame.K_w]:
                vx += 1.0
            if keys[pygame.K_s]:
                vx -= 1.0
            if keys[pygame.K_a]:
                vy += 1.0
            if keys[pygame.K_d]:
                vy -= 1.0
            if keys[pygame.K_LEFT]:
                yaw += 1.0
            if keys[pygame.K_RIGHT]:
                yaw -= 1.0
            if keys[pygame.K_UP]:
                tilt_in += 1.0
            if keys[pygame.K_DOWN]:
                tilt_in -= 1.0
            if keys[pygame.K_q]:
                self.height_norm = max(0.0, self.height_norm - 0.6 * dt)
            if keys[pygame.K_e]:
                self.height_norm = min(1.0, self.height_norm + 0.6 * dt)

            if self._joystick is not None:
                n_axes = self._joystick.get_numaxes()
                if n_axes >= 4:
                    vy = max(-1.0, min(1.0, vy - _deadzone(self._joystick.get_axis(0))))
                    vx = max(-1.0, min(1.0, vx - _deadzone(self._joystick.get_axis(1))))
                    yaw = max(-1.0, min(1.0, yaw - _deadzone(self._joystick.get_axis(2))))
                    tilt_in = max(-1.0, min(1.0, tilt_in - _deadzone(self._joystick.get_axis(3))))
                if n_axes >= 6:
                    lt = (self._joystick.get_axis(4) + 1.0) * 0.5
                    rt = (self._joystick.get_axis(5) + 1.0) * 0.5
                    if lt > 0.15:
                        self.height_norm = max(0.0, self.height_norm - 0.6 * lt * dt)
                    if rt > 0.15:
                        self.height_norm = min(1.0, self.height_norm + 0.6 * rt * dt)

        sign = -1.0 if self.invert_pitch else 1.0
        self.gimbal_pitch = max(-35.0, min(35.0, self.gimbal_pitch + sign * tilt_in * 45.0 * dt))
        scale = SPEED_MODES[self.speed_idx][1]
        return vx * scale, vy * scale, yaw * scale, jump

    # ---- Rendering ---------------------------------------------------------

    def _draw_video(self, screen: pygame.Surface) -> None:
        if self._client is not None:
            rgb, seq = self._client.get_latest_frame()
            if rgb is not None and seq != self._last_frame_seq:
                self._last_frame_seq = seq
                # pygame.surfarray expects (W, H, 3)
                surf = pygame.surfarray.make_surface(np.transpose(rgb, (1, 0, 2)))
                self._video_surf = pygame.transform.smoothscale(surf, screen.get_size())

        if self._video_surf is not None:
            screen.blit(self._video_surf, (0, 0))
        else:
            screen.fill((14, 18, 28))
            txt = self._font_lg.render("Waiting for Beni FPV Video Stream...", True, (170, 190, 215))
            screen.blit(txt, txt.get_rect(center=(screen.get_width() // 2, screen.get_height() // 2)))

    def _draw_hud(self, screen: pygame.Surface) -> None:
        w, h = screen.get_size()
        t = self._client.telemetry if self._client else None

        # Top translucent bar
        bar = pygame.Surface((w, 54), pygame.SRCALPHA)
        bar.fill((10, 14, 22, 175))
        screen.blit(bar, (0, 0))

        conn_ok = bool(t and t.connected)
        badge_col = (46, 204, 113) if conn_ok else (231, 76, 60)
        badge_txt = ("MOCK LIVE" if self.mock else "LIVE") if conn_ok else "OFFLINE"
        pygame.draw.rect(screen, badge_col, (16, 11, 128, 32), border_radius=8)
        lbl = self._font_sm.render(badge_txt, True, (10, 14, 18))
        screen.blit(lbl, lbl.get_rect(center=(80, 27)))

        soc = t.battery_soc if t else 0
        fps = t.video_fps if t else 0.0
        rtt = t.rtt_ms if t else 0.0
        codec = (t.video_codec if t else "hevc").upper()
        free_gb = t.storage_free_gb if t else 0.0
        speed_name = SPEED_MODES[self.speed_idx][0]

        info = (
            f"BAT {soc:3d}%   |   {fps:4.1f} FPS ({codec})   |   RTT {rtt:4.1f} ms"
            f"   |   {speed_name}   |   FREE {free_gb:.1f} GB"
        )
        screen.blit(self._font_md.render(info, True, (235, 242, 250)), (164, 15))

        # Right status pills (REC / MUTE / GYRO)
        rx = w - 18
        if self.recording or (t and t.recording):
            dur = t.rec_duration_s if t else 0
            rec_lbl = self._font_sm.render(f"REC {dur // 60:02d}:{dur % 60:02d}", True, (255, 255, 255))
            rw = rec_lbl.get_width() + 24
            rx -= rw
            pygame.draw.rect(screen, (215, 38, 48), (rx, 11, rw, 32), border_radius=8)
            screen.blit(rec_lbl, (rx + 12, 18))
            rx -= 10
        if self.gyro_enabled:
            g_lbl = self._font_sm.render("GYRO", True, (15, 20, 25))
            gw = g_lbl.get_width() + 18
            rx -= gw
            pygame.draw.rect(screen, (72, 201, 240), (rx, 11, gw, 32), border_radius=8)
            screen.blit(g_lbl, (rx + 9, 18))
            rx -= 10
        if self.audio_muted:
            m_lbl = self._font_sm.render("MUTED", True, (255, 255, 255))
            mw = m_lbl.get_width() + 18
            rx -= mw
            pygame.draw.rect(screen, (110, 118, 130), (rx, 11, mw, 32), border_radius=8)
            screen.blit(m_lbl, (rx + 9, 18))

        # Left standing-height bar
        bar_h = 220
        bx, by = 24, (h - bar_h) // 2
        pygame.draw.rect(screen, (20, 26, 38), (bx, by, 14, bar_h), border_radius=6)
        fill_h = int(bar_h * self.height_norm)
        pygame.draw.rect(
            screen, (80, 210, 165), (bx, by + bar_h - fill_h, 14, fill_h), border_radius=6
        )
        screen.blit(self._font_sm.render("HT", True, (200, 215, 230)), (bx - 2, by - 24))

        # Right gimbal pitch bar
        gx, gy = w - 38, (h - bar_h) // 2
        pygame.draw.rect(screen, (20, 26, 38), (gx, gy, 14, bar_h), border_radius=6)
        p_norm = (self.gimbal_pitch + 35.0) / 70.0
        py = gy + int(bar_h * (1.0 - p_norm))
        pygame.draw.circle(screen, (255, 215, 64), (gx + 7, py), 8)
        screen.blit(self._font_sm.render("TILT", True, (200, 215, 230)), (gx - 10, gy - 24))

        # Bottom prompt legend
        bot = pygame.Surface((w, 42), pygame.SRCALPHA)
        bot.fill((10, 14, 22, 165))
        screen.blit(bot, (0, h - 42))
        legend = (
            "[LS/WASD] Drive   [RS/Arrows] Turn+Tilt   [LT/RT/L4/R4] Height   "
            "[A/Space] Jump   [X/R] Rec   [Y/P] Photo   [B/T] Speak(TTS)   "
            "[D-Left/L] Gallery   [Menu/Tab] Settings"
        )
        screen.blit(self._font_sm.render(legend, True, (205, 218, 232)), (18, h - 30))

    def _draw_overlay(self, screen: pygame.Surface) -> None:
        w, h = screen.get_size()
        dim = pygame.Surface((w, h), pygame.SRCALPHA)
        dim.fill((6, 10, 18, 195))
        screen.blit(dim, (0, 0))

        if self.overlay == "settings":
            panel = pygame.Rect(w // 2 - 310, 90, 620, 620)
            pygame.draw.rect(screen, (22, 28, 42), panel, border_radius=16)
            pygame.draw.rect(screen, (72, 140, 210), panel, width=2, border_radius=16)
            screen.blit(self._font_lg.render("Quick Settings", True, (240, 246, 255)), (panel.x + 28, panel.y + 22))
            for idx, (label, val) in enumerate(self._settings_entries()):
                row = pygame.Rect(panel.x + 22, panel.y + 74 + idx * 46, panel.w - 44, 40)
                if idx == self.settings_idx:
                    pygame.draw.rect(screen, (52, 110, 185), row, border_radius=8)
                screen.blit(self._font_md.render(label, True, (235, 242, 252)), (row.x + 14, row.y + 8))
                vs = self._font_md.render(val, True, (255, 220, 90))
                screen.blit(vs, (row.right - vs.get_width() - 14, row.y + 8))

        elif self.overlay == "tts":
            panel = pygame.Rect(w // 2 - 380, 100, 760, 560)
            pygame.draw.rect(screen, (22, 28, 42), panel, border_radius=16)
            pygame.draw.rect(screen, (90, 205, 165), panel, width=2, border_radius=16)
            mode_tag = "ROBOT VOICE" if self.robot_tts_voice else "NORMAL VOICE"
            screen.blit(
                self._font_lg.render(f"Beni Voice (TTS)  —  {mode_tag}", True, (240, 248, 255)),
                (panel.x + 28, panel.y + 22),
            )
            # Text input box
            box = pygame.Rect(panel.x + 28, panel.y + 74, panel.w - 56, 54)
            pygame.draw.rect(screen, (12, 16, 26), box, border_radius=10)
            pygame.draw.rect(screen, (100, 220, 180), box, width=2, border_radius=10)
            shown = self.tts_input + "_" if self.tts_input else "Type with Steam OSK / Keyboard (or pick a quick phrase below)..."
            col = (245, 250, 255) if self.tts_input else (130, 148, 170)
            screen.blit(self._font_md.render(shown[:62], True, col), (box.x + 14, box.y + 15))

            screen.blit(
                self._font_sm.render("Quick Phrases (D-Pad Up/Down, A/Enter to Speak, X to Toggle Robot Filter, B/Esc Close):", True, (170, 190, 210)),
                (panel.x + 28, panel.y + 146),
            )
            for idx, phrase in enumerate(QUICK_PHRASES):
                row = pygame.Rect(panel.x + 28, panel.y + 180 + idx * 54, panel.w - 56, 46)
                if idx == self.tts_phrase_idx:
                    pygame.draw.rect(screen, (42, 122, 102), row, border_radius=8)
                screen.blit(self._font_md.render(phrase, True, (240, 246, 252)), (row.x + 16, row.y + 10))

        elif self.overlay == "gallery":
            panel = pygame.Rect(80, 75, w - 160, h - 150)
            pygame.draw.rect(screen, (22, 28, 42), panel, border_radius=16)
            pygame.draw.rect(screen, (120, 165, 235), panel, width=2, border_radius=16)
            screen.blit(
                self._font_lg.render("Beni Media Gallery (:8084)  —  [A/Enter] Download   [B/Esc] Close", True, (240, 246, 255)),
                (panel.x + 24, panel.y + 20),
            )
            if self.gallery_loading:
                screen.blit(self._font_md.render("Loading media list...", True, (190, 205, 225)), (panel.x + 30, panel.y + 90))
            elif not self.gallery_items:
                screen.blit(self._font_md.render("No media files found on Beni.", True, (190, 205, 225)), (panel.x + 30, panel.y + 90))
            else:
                for idx, item in enumerate(self.gallery_items[:10]):
                    row = pygame.Rect(panel.x + 24, panel.y + 76 + idx * 52, 620, 44)
                    if idx == self.gallery_idx:
                        pygame.draw.rect(screen, (52, 110, 185), row, border_radius=8)
                    tag = "VID" if item.media_type == "video" else "IMG"
                    kb = item.size_bytes / 1024.0
                    text = f"[{tag}] {item.filename}  ({kb:.0f} KB)"
                    screen.blit(self._font_md.render(text, True, (238, 244, 252)), (row.x + 12, row.y + 10))

                sel = self.gallery_items[self.gallery_idx]
                thumb = self.gallery_thumbs.get(sel.file_id)
                tx, ty = panel.right - 390, panel.y + 90
                if thumb is not None:
                    screen.blit(thumb, (tx, ty))
                else:
                    pygame.draw.rect(screen, (14, 18, 28), (tx, ty, 360, 202), border_radius=10)
                meta = [
                    f"File: {sel.filename}",
                    f"Type: {sel.media_type.upper()}  ({sel.storage})",
                    f"Date: {sel.created_at or 'N/A'}",
                    f"Save to: {self.download_dir}",
                ]
                for i, line in enumerate(meta):
                    screen.blit(self._font_sm.render(line, True, (200, 215, 232)), (tx, ty + 220 + i * 26))

    def _draw_toast(self, screen: pygame.Surface) -> None:
        if time.monotonic() >= self._toast_until or not self._toast_text:
            return
        lbl = self._font_md.render(self._toast_text, True, (255, 255, 255))
        bw = lbl.get_width() + 36
        bx = (screen.get_width() - bw) // 2
        by = 68
        pygame.draw.rect(screen, (20, 28, 42), (bx, by, bw, 42), border_radius=10)
        pygame.draw.rect(screen, (90, 200, 160), (bx, by, bw, 42), width=2, border_radius=10)
        screen.blit(lbl, (bx + 18, by + 9))

    # ---- Main Loop ---------------------------------------------------------

    def run(self, *, max_seconds: float | None = None) -> int:
        self._init_pygame()
        self._start_backend()
        assert self._screen is not None

        clock = pygame.time.Clock()
        start_t = time.monotonic()
        last_ctrl_t = 0.0
        last_stats_t = 0.0
        running = True

        try:
            while running:
                dt = clock.tick(60) / 1000.0
                if max_seconds is not None and (time.monotonic() - start_t) >= max_seconds:
                    break

                jump_triggered = False

                for ev in pygame.event.get():
                    if ev.type == pygame.QUIT:
                        running = False
                    elif ev.type == pygame.TEXTINPUT and self.overlay == "tts":
                        self.tts_input += ev.text
                    elif ev.type == pygame.KEYDOWN:
                        if self.overlay == "tts":
                            if ev.key == pygame.K_ESCAPE:
                                self._close_overlay()
                            elif ev.key == pygame.K_BACKSPACE:
                                self.tts_input = self.tts_input[:-1]
                            elif ev.key == pygame.K_RETURN:
                                self._trigger_speak(self.tts_input)
                            elif ev.key == pygame.K_UP:
                                self.tts_phrase_idx = (self.tts_phrase_idx - 1) % len(QUICK_PHRASES)
                            elif ev.key == pygame.K_DOWN:
                                self.tts_phrase_idx = (self.tts_phrase_idx + 1) % len(QUICK_PHRASES)
                            continue

                        if self.overlay == "settings":
                            n = len(self._settings_entries())
                            if ev.key in (pygame.K_ESCAPE, pygame.K_TAB):
                                self._close_overlay()
                            elif ev.key == pygame.K_UP:
                                self.settings_idx = (self.settings_idx - 1) % n
                            elif ev.key == pygame.K_DOWN:
                                self.settings_idx = (self.settings_idx + 1) % n
                            elif ev.key in (pygame.K_RETURN, pygame.K_SPACE):
                                if not self._activate_setting():
                                    running = False
                            continue

                        if self.overlay == "gallery":
                            if ev.key in (pygame.K_ESCAPE, pygame.K_l):
                                self._close_overlay()
                            elif ev.key == pygame.K_UP and self.gallery_items:
                                self.gallery_idx = (self.gallery_idx - 1) % len(self.gallery_items)
                            elif ev.key == pygame.K_DOWN and self.gallery_items:
                                self.gallery_idx = (self.gallery_idx + 1) % len(self.gallery_items)
                            elif ev.key in (pygame.K_RETURN, pygame.K_SPACE):
                                self._download_selected_media()
                            continue

                        # Main HUD shortcuts
                        if ev.key in (pygame.K_TAB, pygame.K_ESCAPE):
                            self.overlay = "settings"
                        elif ev.key == pygame.K_t:
                            self._open_tts()
                        elif ev.key == pygame.K_l:
                            self._open_gallery()
                        elif ev.key == pygame.K_SPACE:
                            jump_triggered = True
                            self.show_toast("JUMP!", duration=1.0)
                        elif ev.key == pygame.K_r:
                            self.recording = not self.recording
                            if self._client:
                                self._client.toggle_record(self.recording)
                            self.show_toast("Recording started" if self.recording else "Recording stopped")
                        elif ev.key == pygame.K_p:
                            if self._client:
                                self._client.take_photo()
                            self.show_toast("Photo captured!")
                        elif ev.key == pygame.K_m:
                            self.audio_muted = not self.audio_muted
                            if self._client:
                                self._client.audio_muted = self.audio_muted
                            self.show_toast("Audio muted" if self.audio_muted else "Audio unmuted")
                        elif ev.key == pygame.K_g:
                            self.gyro_enabled = not self.gyro_enabled
                            self.show_toast("Gyro tilt ON" if self.gyro_enabled else "Gyro tilt OFF")

                    elif ev.type == pygame.JOYBUTTONDOWN:
                        btn = ev.button
                        if self.overlay == "settings":
                            if btn == 0:  # A
                                if not self._activate_setting():
                                    running = False
                            elif btn in (1, 7):  # B / Start
                                self._close_overlay()
                        elif self.overlay == "tts":
                            if btn == 0:  # A
                                self._trigger_speak(self.tts_input)
                            elif btn == 1:  # B
                                self._close_overlay()
                            elif btn == 2:  # X
                                self.robot_tts_voice = not self.robot_tts_voice
                        elif self.overlay == "gallery":
                            if btn == 0:  # A
                                self._download_selected_media()
                            elif btn == 1:  # B
                                self._close_overlay()
                        else:
                            if btn in (0, 12):  # A / L5 paddle -> Jump
                                jump_triggered = True
                                self.show_toast("JUMP!", duration=1.0)
                            elif btn in (1, 6):  # B / Select -> TTS + OSK
                                self._open_tts()
                            elif btn in (2, 13):  # X / R5 paddle -> Record
                                self.recording = not self.recording
                                if self._client:
                                    self._client.toggle_record(self.recording)
                                self.show_toast("Recording started" if self.recording else "Recording stopped")
                            elif btn == 3:  # Y -> Photo
                                if self._client:
                                    self._client.take_photo()
                                self.show_toast("Photo captured!")
                            elif btn == 4:  # LB -> Mute
                                self.audio_muted = not self.audio_muted
                                if self._client:
                                    self._client.audio_muted = self.audio_muted
                            elif btn == 5:  # RB -> Gyro toggle
                                self.gyro_enabled = not self.gyro_enabled
                                self.show_toast("Gyro tilt ON" if self.gyro_enabled else "Gyro tilt OFF")
                            elif btn == 7:  # Start -> Settings
                                self.overlay = "settings"

                    elif ev.type == pygame.JOYHATMOTION:
                        hx, hy = ev.value
                        if self.overlay == "settings":
                            n = len(self._settings_entries())
                            if hy == 1:
                                self.settings_idx = (self.settings_idx - 1) % n
                            elif hy == -1:
                                self.settings_idx = (self.settings_idx + 1) % n
                        elif self.overlay == "tts":
                            if hy == 1:
                                self.tts_phrase_idx = (self.tts_phrase_idx - 1) % len(QUICK_PHRASES)
                            elif hy == -1:
                                self.tts_phrase_idx = (self.tts_phrase_idx + 1) % len(QUICK_PHRASES)
                        elif self.overlay == "gallery" and self.gallery_items:
                            if hy == 1:
                                self.gallery_idx = (self.gallery_idx - 1) % len(self.gallery_items)
                            elif hy == -1:
                                self.gallery_idx = (self.gallery_idx + 1) % len(self.gallery_items)
                        elif self.overlay is None and hx == -1:
                            self._open_gallery()

                vx, vy, yaw, _ = self._sample_drive_inputs(dt)

                now = time.monotonic()
                if self._client is not None and (now - last_ctrl_t >= 0.02 or jump_triggered):
                    last_ctrl_t = now
                    self._client.send_drive(
                        vx=vx,
                        vy=vy,
                        yaw_rate=yaw,
                        gimbal_pitch=self.gimbal_pitch,
                        height=self.height_norm,
                        loco_profile=self.speed_idx,
                        jump=jump_triggered,
                    )
                if self._client is not None and (now - last_stats_t >= 0.25):
                    last_stats_t = now
                    self._client.refresh_link_stats()

                self._draw_video(self._screen)
                if self.show_hud:
                    self._draw_hud(self._screen)
                if self.overlay is not None:
                    self._draw_overlay(self._screen)
                self._draw_toast(self._screen)
                pygame.display.flip()
        finally:
            self._shutdown()
        return 0
