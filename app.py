"""
SWTOR Holocron DPS — Live In-Game Overlay & Combat Log Parser
Main entry point for both the standalone Windows executable (.exe) and local browser testing.

Features:
- Automatically watches Documents/Star Wars - The Old Republic/CombatLogs for the newest combat_*.txt
- Real-time parsing of SWTOR 7.0+ and classic combat logs (DPS, eDPS, HPS, DTPS, APM, Crit%, Accuracy%, Boss HP)
- Always-On-Top desktop overlay window via pywebview (or local browser mode via --browser)
- Built-in live Combat Simulator to test and preview the meter immediately without launching the game
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Optional

from swtor_parser import SWTORLogParser
from swtor_simulator import RAID_MEMBERS, SAMPLE_ABILITIES, generate_instant_demo_lines


def get_resource_path(relative_path: str) -> Path:
    """Resolve resource path both in normal Python execution and inside a PyInstaller .exe bundle."""
    base_path = getattr(sys, "_MEIPASS", None)
    if base_path:
        return Path(base_path) / relative_path
    return Path(__file__).resolve().parent / relative_path


class LiveSimulationWorker:
    """Streams simulated SWTOR 7.0+ combat log events in real-time into the parser."""

    def __init__(self, parser: SWTORLogParser, lock: threading.Lock):
        self.parser = parser
        self.lock = lock
        self.running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        now_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        player_tag = "@DarthVex#687132173159297|(15.20,-42.10,0.05,180.0)|(342000/342000)"
        exit_line = f"[{now_str}] [{player_tag}] [=] [] [Event {{836045448945472}}: ExitCombat {{836045448945490}}] ()"
        with self.lock:
            self.parser.parse_line(exit_line)

    def _run_loop(self) -> None:
        player_tag = "@DarthVex#687132173159297|(15.20,-42.10,0.05,180.0)|(342000/342000)"
        boss_max_hp = 8500000
        boss_cur_hp = boss_max_hp
        boss_name = "Dread Master Brontes {3273941840389120:192000991122}"

        def ts() -> str:
            return datetime.now().strftime("%H:%M:%S.%f")[:-3]

        with self.lock:
            self.parser.parse_line(
                f"[{ts()}] [{player_tag}] [] [] [AreaEntered {{836045448953664}}: The Dread Fortress {{137438989912}}] (he3000) <v7.5.1>"
            )
            self.parser.parse_line(
                f"[{ts()}] [{player_tag}] [] [] [Event {{836045448945472}}: DisciplineChanged {{836045448953665}}] "
                f"(Marauder {{1614090259187123}} / Annihilation {{1614111912948912}})"
            )
            self.parser.parse_line(
                f"[{ts()}] [{player_tag}] [=] [] [Event {{836045448945472}}: EnterCombat {{836045448945489}}] ()"
            )

        tick = 0
        while self.running and boss_cur_hp > 0:
            time.sleep(0.45)
            if not self.running:
                break
            tick += 1
            cur_ts = ts()
            abil_name, abil_id, dmg_type, min_d, max_d, crit_c = random.choice(SAMPLE_ABILITIES)
            dmg_type_id = "836045448940874" if dmg_type == "energy" else "836045448940876"

            is_crit = random.random() < crit_c
            dmg = int(random.randint(min_d, max_d) * (1.68 if is_crit else 1.0))
            star = "*" if is_crit else ""
            boss_cur_hp = max(0, boss_cur_hp - dmg)
            boss_tag = f"{boss_name}|(15.20,-28.00,0.05,0.0)|({boss_cur_hp}/{boss_max_hp})"

            with self.lock:
                if tick % 3 == 1:
                    self.parser.parse_line(
                        f"[{cur_ts}] [{player_tag}] [{boss_tag}] [{abil_name} {{{abil_id}}}] "
                        f"[Event {{836045448945472}}: AbilityActivate {{836045448945479}}] ()"
                    )
                self.parser.parse_line(
                    f"[{cur_ts}] [{player_tag}] [{boss_tag}] [{abil_name} {{{abil_id}}}] "
                    f"[ApplyEffect {{836045448945477}}: Damage {{836045448945501}}] "
                    f"({dmg}{star} ~{dmg} {dmg_type} {{{dmg_type_id}}}) <{dmg}>"
                )
                # Self-heal on internal bleed
                if dmg_type == "internal":
                    heal = int(dmg * 0.20)
                    self.parser.parse_line(
                        f"[{cur_ts}] [{player_tag}] [=] [Hungering {{807788859187200}}] "
                        f"[ApplyEffect {{836045448945477}}: Heal {{836045448945500}}] ({heal}{star} ~{heal}) <{heal // 2}>"
                    )
                # Raid damage taken occasionally
                if tick % 5 == 0:
                    inc_dmg = random.randint(4500, 11200)
                    self.parser.parse_line(
                        f"[{cur_ts}] [{boss_tag}] [{player_tag}] [Lightning Arc {{3273941840399999}}] "
                        f"[ApplyEffect {{836045448945477}}: Damage {{836045448945501}}] "
                        f"({inc_dmg} energy {{836045448940874}}) <{inc_dmg}>"
                    )
                # Co-raiders attacking
                for member_handle, _spec, scale in RAID_MEMBERS[1:]:
                    if random.random() < 0.65:
                        m_abil, m_id, m_type, m_min, m_max, m_crit_c = random.choice(SAMPLE_ABILITIES)
                        m_crit = random.random() < m_crit_c
                        m_dmg = int(random.randint(m_min, m_max) * scale * (1.65 if m_crit else 1.0))
                        m_star = "*" if m_crit else ""
                        m_tag = f"{member_handle}|(12.0,-40.0,0.0,180.0)|(330000/330000)"
                        boss_cur_hp = max(0, boss_cur_hp - m_dmg)
                        boss_tag = f"{boss_name}|(15.20,-28.00,0.05,0.0)|({boss_cur_hp}/{boss_max_hp})"
                        self.parser.parse_line(
                            f"[{cur_ts}] [{m_tag}] [{boss_tag}] [{m_abil} {{{m_id}}}] "
                            f"[ApplyEffect {{836045448945477}}: Damage {{836045448945501}}] "
                            f"({m_dmg}{m_star} ~{m_dmg} {m_type} {{836045448940874}}) <{m_dmg}>"
                        )

        self.running = False


class OverlayBridgeAPI:
    """JS-to-Python API exposed to pywebview.
    IMPORTANT: Do not store non-underscore attributes on self that hold Python/WinForms objects,
    because pywebview recursively serializes all public attributes of js_api!
    """

    def __init__(self, parser: SWTORLogParser, lock: threading.Lock, sim: LiveSimulationWorker):
        self._parser = parser
        self._lock = lock
        self._sim = sim
        self._saved_full_h = 0

    def _get_window(self):
        try:
            import webview
            return webview.windows[0] if webview.windows else None
        except Exception:
            return None

    def get_snapshot(self, encounter_index: int = -2) -> dict:
        with self._lock:
            if not self._sim.running:
                self._parser.poll()
            idx = encounter_index if (encounter_index >= 0 or encounter_index == -2) else None
            return self._parser.get_snapshot(idx)

    def toggle_simulation(self) -> bool:
        if self._sim.running:
            self._sim.stop()
            return False
        else:
            self._sim.start()
            return True

    def load_sample_parse(self) -> bool:
        self._sim.stop()
        with self._lock:
            for line in generate_instant_demo_lines():
                self._parser.parse_line(line)
        return True

    def reset_encounters(self) -> bool:
        self._sim.stop()
        with self._lock:
            self._parser.reset_all()
        return True

    def undo_reset(self) -> bool:
        with self._lock:
            return self._parser.undo_reset()

    def _get_hwnd_and_scale(self) -> tuple[int, float]:
        win = self._get_window()
        if not win or not getattr(win, "native", None):
            return (0, 1.0)
        try:
            import ctypes
            hwnd = int(win.native.Handle.ToInt32())
            dpi = ctypes.windll.user32.GetDpiForWindow(hwnd)
            scale = (dpi / 96.0) if dpi > 0 else 1.0
            return (hwnd, scale)
        except Exception:
            return (0, 1.0)

    def set_always_on_top(self, on_top: bool) -> bool:
        """
        Uses native Win32 SetWindowPos with SWP_ASYNCWINDOWPOS (0x4000) instead of
        WinForms Form.TopMost. Setting Form.TopMost from a pywebview JS bridge worker thread
        calls RecreateHandle() across threads and deadlocks the WinForms STA thread!
        """
        hwnd, _ = self._get_hwnd_and_scale()
        if hwnd:
            try:
                import ctypes
                HWND_TOPMOST = -1
                HWND_NOTOPMOST = -2
                SWP_NOSIZE = 0x0001
                SWP_NOMOVE = 0x0002
                SWP_NOACTIVATE = 0x0010
                SWP_ASYNCWINDOWPOS = 0x4000
                ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    HWND_TOPMOST if on_top else HWND_NOTOPMOST,
                    0,
                    0,
                    0,
                    0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS,
                )
            except Exception:
                pass
        return bool(on_top)

    def set_compact_mode(self, compact: bool, content_height: int = 0) -> bool:
        """
        Uses native Win32 GetWindowRect / GetClientRect + SetWindowPos with
        SWP_ASYNCWINDOWPOS (0x4000) to size the outer window to the exact
        titlebar frame height + DOM content height so Mini mode never clips.
        """
        hwnd, scale = self._get_hwnd_and_scale()
        if hwnd:
            try:
                import ctypes
                from ctypes import wintypes

                SWP_NOMOVE = 0x0002
                SWP_NOZORDER = 0x0004
                SWP_NOACTIVATE = 0x0010
                SWP_ASYNCWINDOWPOS = 0x4000

                win_rect = wintypes.RECT()
                cli_rect = wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(win_rect))
                ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(cli_rect))

                win_w = max(win_rect.right - win_rect.left, int(520 * scale))
                win_h = win_rect.bottom - win_rect.top
                cli_h = cli_rect.bottom - cli_rect.top
                frame_h = max(win_h - cli_h, int(38 * scale))

                if compact:
                    if win_h > int(260 * scale):
                        self._saved_full_h = win_h
                    css_h = max(int(content_height or 116), 112)
                    target_h = frame_h + int((css_h + 6) * scale)
                else:
                    target_h = self._saved_full_h if self._saved_full_h > int(300 * scale) else int(580 * scale)

                ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    0,
                    0,
                    0,
                    win_w,
                    target_h,
                    SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_ASYNCWINDOWPOS,
                )
            except Exception:
                pass
        return bool(compact)

    def set_opacity(self, alpha_pct: int) -> int:
        """
        Sets native Win32 window opacity (50..100%) using WS_EX_LAYERED and
        SetLayeredWindowAttributes so the overlay is translucent over SWTOR.
        """
        pct = max(50, min(100, int(alpha_pct)))
        hwnd, _ = self._get_hwnd_and_scale()
        if hwnd:
            try:
                import ctypes
                GWL_EXSTYLE = -20
                WS_EX_LAYERED = 0x00080000
                LWA_ALPHA = 0x00000002
                ex_style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED)
                alpha_byte = int(round((pct / 100.0) * 255))
                ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0, alpha_byte, LWA_ALPHA)
            except Exception:
                pass
        return pct

    def apply_window_icon(self) -> bool:
        """
        Applies holocron_icon.ico to the native Win32 HWND (both ICON_BIG for Alt-Tab /
        Taskbar and ICON_SMALL for the title bar) using non-blocking PostMessageW.
        """
        hwnd, _ = self._get_hwnd_and_scale()
        if not hwnd:
            return False
        ico_path = get_resource_path("holocron_icon.ico")
        if not ico_path.exists():
            return False
        try:
            import ctypes
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x00000010
            hicon_big = ctypes.windll.user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 256, 256, LR_LOADFROMFILE)
            hicon_small = ctypes.windll.user32.LoadImageW(0, str(ico_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
            if hicon_big:
                ctypes.windll.user32.PostMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
            if hicon_small:
                ctypes.windll.user32.PostMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_small)
            return True
        except Exception:
            return False

    def choose_log_dir(self) -> str:
        """
        Runs WinForms FolderBrowserDialog on a dedicated STA thread so it never
        cross-thread blocks the WebView2 UI thread.
        """
        result_holder = [str(self._parser.log_dir)]

        def _pick():
            try:
                import System.Windows.Forms as WinForms
                dlg = WinForms.FolderBrowserDialog()
                dlg.Description = "Select SWTOR CombatLogs Folder"
                dlg.SelectedPath = str(self._parser.log_dir)
                if dlg.ShowDialog() == WinForms.DialogResult.OK and dlg.SelectedPath:
                    chosen = Path(dlg.SelectedPath)
                    if (chosen / "CombatLogs").exists() and not any(chosen.glob("combat_*.txt")):
                        chosen = chosen / "CombatLogs"
                    with self._lock:
                        self._parser.log_dir = chosen
                        self._parser.save_log_dir_preference(chosen)
                        self._parser.reset_all()
                        self._parser.current_file = None
                        self._parser.file_pos = 0
                        self._parser.poll()
                    result_holder[0] = str(chosen)
            except Exception:
                pass

        try:
            from System.Threading import ApartmentState, Thread, ThreadStart
            t = Thread(ThreadStart(_pick))
            t.SetApartmentState(ApartmentState.STA)
            t.Start()
            t.Join()
        except Exception:
            pass
        return result_holder[0]

    def set_log_dir(self, path_str: str) -> dict:
        """Sets the CombatLogs directory directly from a path string or discovered candidate."""
        raw = os.path.expandvars(path_str.strip().strip('"').strip("'"))
        if not raw:
            return {"ok": False, "error": "Please enter a valid folder path.", "log_dir": str(self._parser.log_dir)}
        chosen = Path(raw)
        # If user selected the parent "Star Wars - The Old Republic" or "Documents" folder, resolve into CombatLogs
        if (chosen / "Star Wars - The Old Republic" / "CombatLogs").exists():
            chosen = chosen / "Star Wars - The Old Republic" / "CombatLogs"
        elif (chosen / "CombatLogs").exists() and not any(chosen.glob("combat_*.txt")):
            chosen = chosen / "CombatLogs"

        with self._lock:
            self._parser.log_dir = chosen
            self._parser.save_log_dir_preference(chosen)
            self._parser.reset_all()
            self._parser.current_file = None
            self._parser.file_pos = 0
            self._parser.poll()
        return {
            "ok": True,
            "exists": chosen.exists(),
            "log_dir": str(chosen),
            "candidates": self._parser.discover_log_directories(chosen),
        }

    def discover_log_dirs(self) -> list:
        """Scans the system for all candidate SWTOR CombatLogs directories."""
        with self._lock:
            return self._parser.discover_log_directories(self._parser.log_dir)

    def open_log_dir(self) -> bool:
        try:
            target = self._parser.log_dir
            if not target.exists():
                target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))
            return True
        except Exception:
            return False

    def open_help_file(self) -> bool:
        try:
            exe_dir_help = Path(sys.executable).resolve().parent / "HELP_How_To_Enable_Combat_Logging.html"
            local_help = get_resource_path("HELP_How_To_Enable_Combat_Logging.html")
            target = exe_dir_help if exe_dir_help.exists() else local_help
            webbrowser.open(target.as_uri())
            return True
        except Exception:
            return False

    # --- Reference Points & Cross-Session History API ---

    def get_references(self) -> dict:
        with self._lock:
            return self._parser.get_references_state()

    def save_reference(self, encounter_index: int = -2, label: str = "", set_as_baseline: bool = True) -> dict:
        with self._lock:
            idx = encounter_index if (encounter_index >= 0 or encounter_index == -2) else None
            return self._parser.save_reference_point(idx, label=label, set_as_baseline=set_as_baseline)

    def set_session_name(self, name: str) -> dict:
        with self._lock:
            clean = self._parser.set_session_name(name)
            return {"ok": True, "session_name": clean}

    def set_baseline(self, ref_id: str = "") -> dict:
        with self._lock:
            return self._parser.set_active_baseline(ref_id or None)

    def rename_reference(self, ref_id: str, label: str) -> dict:
        with self._lock:
            return self._parser.update_reference_label(ref_id, label)

    def delete_reference(self, ref_id: str) -> dict:
        with self._lock:
            return self._parser.delete_reference_point(ref_id)

    def scan_past_logs(self) -> list:
        with self._lock:
            return self._parser.scan_historical_logs()

    def import_past_encounter(self, file_name: str, encounter_index: int, label: str = "") -> dict:
        with self._lock:
            return self._parser.import_historical_encounter(file_name, int(encounter_index), label=label)


def run_browser_server(parser: SWTORLogParser, lock: threading.Lock, sim: LiveSimulationWorker, port: int = 8765) -> None:
    """Runs a lightweight local FastAPI/Uvicorn server so the UI can also be tested in any browser."""
    from fastapi import FastAPI
    from fastapi.responses import FileResponse, JSONResponse
    import uvicorn

    app = FastAPI(title="SWTOR Holocron DPS Server")
    html_path = get_resource_path("overlay_ui.html")
    help_html_path = get_resource_path("HELP_How_To_Enable_Combat_Logging.html")
    help_img_path = get_resource_path("swtor_combat_logging_help.png")

    @app.get("/")
    def index():
        return FileResponse(html_path)

    @app.get("/HELP_How_To_Enable_Combat_Logging.html")
    def help_page():
        return FileResponse(help_html_path)

    @app.get("/swtor_combat_logging_help.png")
    def help_image():
        return FileResponse(help_img_path)

    @app.get("/holocron_icon.png")
    def app_icon_png():
        return FileResponse(get_resource_path("holocron_icon.png"))

    @app.get("/favicon.ico")
    def app_favicon():
        return FileResponse(get_resource_path("holocron_icon.ico"))

    @app.get("/api/snapshot")
    def api_snapshot(enc: int = -2):
        with lock:
            if not sim.running:
                parser.poll()
            idx = enc if (enc >= 0 or enc == -2) else None
            return JSONResponse(parser.get_snapshot(idx))

    @app.post("/api/toggle_sim")
    def api_toggle_sim():
        if sim.running:
            sim.stop()
        else:
            sim.start()
        return JSONResponse({"running": sim.running})

    @app.post("/api/load_sample")
    def api_load_sample():
        sim.stop()
        with lock:
            for line in generate_instant_demo_lines():
                parser.parse_line(line)
        return JSONResponse({"ok": True})

    @app.post("/api/reset")
    def api_reset():
        sim.stop()
        with lock:
            parser.reset_all()
        return JSONResponse({"ok": True})

    @app.post("/api/undo_reset")
    def api_undo_reset():
        with lock:
            ok = parser.undo_reset()
        return JSONResponse({"ok": ok})

    @app.get("/api/discover_dirs")
    def api_discover_dirs():
        with lock:
            return JSONResponse(parser.discover_log_directories(parser.log_dir))

    @app.post("/api/set_dir")
    def api_set_dir(payload: dict):
        raw = os.path.expandvars(str(payload.get("path", "")).strip().strip('"').strip("'"))
        if not raw:
            return JSONResponse({"ok": False, "error": "Empty path"})
        chosen = Path(raw)
        if (chosen / "Star Wars - The Old Republic" / "CombatLogs").exists():
            chosen = chosen / "Star Wars - The Old Republic" / "CombatLogs"
        elif (chosen / "CombatLogs").exists() and not any(chosen.glob("combat_*.txt")):
            chosen = chosen / "CombatLogs"
        with lock:
            parser.log_dir = chosen
            parser.save_log_dir_preference(chosen)
            parser.reset_all()
            parser.current_file = None
            parser.file_pos = 0
            parser.poll()
        return JSONResponse({
            "ok": True,
            "exists": chosen.exists(),
            "log_dir": str(chosen),
            "candidates": parser.discover_log_directories(chosen),
        })

    @app.get("/api/references")
    def api_get_references():
        with lock:
            return JSONResponse(parser.get_references_state())

    @app.post("/api/save_reference")
    def api_save_reference(payload: dict):
        enc_idx = int(payload.get("encounter_index", -2))
        label = str(payload.get("label", ""))
        set_base = bool(payload.get("set_as_baseline", True))
        with lock:
            idx = enc_idx if (enc_idx >= 0 or enc_idx == -2) else None
            return JSONResponse(parser.save_reference_point(idx, label=label, set_as_baseline=set_base))

    @app.post("/api/set_session_name")
    def api_set_session_name(payload: dict):
        name = str(payload.get("name", ""))
        with lock:
            clean = parser.set_session_name(name)
            return JSONResponse({"ok": True, "session_name": clean})

    @app.post("/api/set_baseline")
    def api_set_baseline(payload: dict):
        ref_id = str(payload.get("ref_id", "")).strip()
        with lock:
            return JSONResponse(parser.set_active_baseline(ref_id or None))

    @app.post("/api/rename_reference")
    def api_rename_reference(payload: dict):
        ref_id = str(payload.get("ref_id", "")).strip()
        label = str(payload.get("label", "")).strip()
        with lock:
            return JSONResponse(parser.update_reference_label(ref_id, label))

    @app.post("/api/delete_reference")
    def api_delete_reference(payload: dict):
        ref_id = str(payload.get("ref_id", "")).strip()
        with lock:
            return JSONResponse(parser.delete_reference_point(ref_id))

    @app.get("/api/scan_past_logs")
    def api_scan_past_logs():
        with lock:
            return JSONResponse(parser.scan_historical_logs())

    @app.post("/api/import_past_encounter")
    def api_import_past_encounter(payload: dict):
        fname = str(payload.get("file_name", ""))
        eidx = int(payload.get("encounter_index", 0))
        label = str(payload.get("label", ""))
        with lock:
            return JSONResponse(parser.import_historical_encounter(fname, eidx, label=label))

    print(f"SWTOR Holocron DPS Web UI running at http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def main() -> None:
    argp = argparse.ArgumentParser(description="SWTOR Live DPS Parser & In-Game Overlay")
    argp.add_argument("--log-dir", type=str, default=None, help="Custom path to SWTOR CombatLogs directory")
    argp.add_argument("--browser", action="store_true", help="Run as local web server instead of desktop overlay")
    argp.add_argument("--port", type=int, default=8765, help="Port for --browser mode")
    argp.add_argument("--demo", action="store_true", help="Pre-load a sample combat parse on startup")
    args = argp.parse_args()

    lock = threading.Lock()
    parser = SWTORLogParser(log_dir=args.log_dir)
    sim = LiveSimulationWorker(parser, lock)

    # Initial poll of existing SWTOR logs if present
    with lock:
        parser.poll()
        if args.demo:
            for line in generate_instant_demo_lines():
                parser.parse_line(line)

    if args.browser:
        run_browser_server(parser, lock, sim, port=args.port)
        return

    # Set explicit Windows AppUserModelID so Taskbar and Alt-Tab display our custom Holocron icon
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SWTOR.Holocron.DPS.Overlay.1")
    except Exception:
        pass

    # Launch native desktop Always-On-Top overlay window via pywebview
    import webview

    api = OverlayBridgeAPI(parser, lock, sim)
    html_file = get_resource_path("overlay_ui.html")
    ico_file = get_resource_path("holocron_icon.ico")

    window = webview.create_window(
        title="SWTOR Holocron DPS Overlay",
        url=html_file.as_uri(),
        js_api=api,
        width=520,
        height=580,
        min_size=(420, 120),
        on_top=True,
        background_color="#0b0f19",
    )
    window.events.shown += lambda: api.apply_window_icon()
    webview.start(debug=False, icon=str(ico_file) if ico_file.exists() else None)


if __name__ == "__main__":
    main()
