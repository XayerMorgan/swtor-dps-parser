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

    def _get_window(self):
        try:
            import webview
            return webview.windows[0] if webview.windows else None
        except Exception:
            return None

    def get_snapshot(self, encounter_index: int = -1) -> dict:
        with self._lock:
            if not self._sim.running:
                self._parser.poll()
            idx = encounter_index if encounter_index >= 0 else None
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

    def set_always_on_top(self, on_top: bool) -> bool:
        win = self._get_window()
        if win:
            try:
                win.on_top = bool(on_top)
            except Exception:
                pass
        return bool(on_top)

    def set_compact_mode(self, compact: bool) -> bool:
        win = self._get_window()
        if win:
            try:
                if compact:
                    win.resize(460, 128)
                else:
                    win.resize(460, 560)
            except Exception:
                pass
        return bool(compact)

    def choose_log_dir(self) -> str:
        win = self._get_window()
        if not win:
            return str(self._parser.log_dir)
        try:
            import webview
            folders = win.create_file_dialog(webview.FOLDER_DIALOG)
            if folders and len(folders) > 0:
                chosen = Path(folders[0])
                with self._lock:
                    self._parser.log_dir = chosen
                    self._parser.current_file = None
                    self._parser.file_pos = 0
                    self._parser.poll()
                return str(chosen)
        except Exception:
            pass
        return str(self._parser.log_dir)

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

    @app.get("/api/snapshot")
    def api_snapshot(enc: int = -1):
        with lock:
            if not sim.running:
                parser.poll()
            idx = enc if enc >= 0 else None
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

    # Launch native desktop Always-On-Top overlay window via pywebview
    import webview

    api = OverlayBridgeAPI(parser, lock, sim)
    html_file = get_resource_path("overlay_ui.html")

    window = webview.create_window(
        title="SWTOR Holocron DPS Overlay",
        url=html_file.as_uri(),
        js_api=api,
        width=460,
        height=560,
        min_size=(340, 115),
        on_top=True,
        background_color="#0b0f19",
    )
    webview.start(debug=False)


if __name__ == "__main__":
    main()
