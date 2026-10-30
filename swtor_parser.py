"""
SWTOR Combat Log Parser Engine
Supports both 7.0+ (Legacy of the Sith / modern) and classic SWTOR combat log formats.
Provides real-time tailing, encounter segmentation, DPS/HPS/DTPS/APM metrics,
and ability-level breakdowns.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple


APP_VERSION = "v2.6.0"
GITHUB_REPO_URL = "https://github.com/XayerMorgan/swtor-dps-parser"
GITHUB_RAW_VERSION_URL = "https://raw.githubusercontent.com/XayerMorgan/swtor-dps-parser/main/swtor_parser.py"
GITHUB_RAW_EXE_URL = "https://github.com/XayerMorgan/swtor-dps-parser/raw/main/SWTOR_DPS_Parser.exe"


def parse_semver(ver_str: str) -> Tuple[int, int, int]:
    """Parses 'v2.5.0' or '2.5.0' into an integer tuple (2, 5, 0) for version comparison."""
    clean = re.sub(r"[^0-9.]", "", str(ver_str or ""))
    parts = [int(p) for p in clean.split(".") if p.isdigit()]
    while len(parts) < 3:
        parts.append(0)
    return (parts[0], parts[1], parts[2])


# Known SWTOR Action/Event IDs
ID_EVENT = "836045448945472"
ID_SPEND = "836045448945473"
ID_RESTORE = "836045448945476"
ID_APPLY_EFFECT = "836045448945477"
ID_REMOVE_EFFECT = "836045448945478"
ID_ABILITY_ACTIVATE = "836045448945479"
ID_ENTER_COMBAT = "836045448945489"
ID_EXIT_COMBAT = "836045448945490"
ID_DEATH = "836045448945493"
ID_HEAL = "836045448945500"
ID_DAMAGE = "836045448945501"
ID_AREA_ENTERED = "836045448953664"
ID_DISCIPLINE_CHANGED = "836045448953665"


# Top-level bracket extractor: matches [Timestamp] [Source] [Target] [Ability] [Effect] Remainder
LINE_PATTERN = re.compile(
    r"^\[([^\]]+)\]\s*"      # 1: Timestamp
    r"\[([^\]]*)\]\s*"       # 2: Source
    r"\[([^\]]*)\]\s*"       # 3: Target
    r"\[([^\]]*)\]\s*"       # 4: Ability
    r"\[([^\]]*)\]\s*"       # 5: Action:Effect
    r"(.*)$"                 # 6: Remainder: (Value) <Threat/Effective>
)

# Parse (Value Details) and optional <Total/Threat>
VALUE_PAREN_PATTERN = re.compile(r"\(([^)]*)\)")
ANGLE_BRACKET_PATTERN = re.compile(r"<([^>]*)>")

# Inside (Value): e.g. "5382* kinetic {836045448940873} (1200 absorbed {836045448945511})"
# Or "15242* ~12245 energy {836045448940874}"
# Or "94174 ~44489"
# Or "0 -miss {836045448945502}"
DAMAGE_VALUE_PATTERN = re.compile(
    r"^\s*(\d+)(\*?)"                      # 1: raw amount, 2: crit asterisk
    r"(?:\s*~(\d+)\*?)?"                   # 3: optional ~effective amount (7.0+ heal/overkill)
    r"(?:\s+([a-zA-Z_-]+)\s*(?:\{\d+\})?)?" # 4: damage type or miss/dodge word
)

ABSORB_PATTERN = re.compile(r"\((\d+)\s+absorbed")
ID_EXTRACT_PATTERN = re.compile(r"\{(\d+)\}")


@dataclass
class EntityInfo:
    raw: str
    name: str
    is_player: bool = False
    is_companion: bool = False
    owner_name: str = ""
    entity_id: str = ""
    hp_current: Optional[int] = None
    hp_max: Optional[int] = None


def parse_entity(raw_field: str) -> EntityInfo:
    """
    Parses a SWTOR Source or Target field.
    Examples:
      7.0+ Player:   @Magisterium#689986867496718|(4631.27,4520.45,694.02,106.86)|(423858/439744)
      Classic Player: @CharacterName
      7.0+ Companion: @Magisterium#689986867496718/Lana Beniko {3600471084236800}:25415150171147|(4631.85,...)|(289720/289720)
      Classic Comp:   @CharacterName:CompanionName {12345}
      7.0+ NPC:       Varad Churl {4418731073601536}:34835000736357|(1104.83,-44.60,76.82,-101.26)|(20358/30274)
      Classic NPC:    Elite Tastybobble {846623953387520}
    """
    raw_field = raw_field.strip()
    if not raw_field or raw_field == "=":
        return EntityInfo(raw=raw_field, name="", is_player=False)

    parts = raw_field.split("|")
    ident = parts[0].strip()

    hp_cur, hp_max = None, None
    if len(parts) >= 3:
        hp_part = parts[2].strip("() ")
        if "/" in hp_part:
            try:
                c_str, m_str = hp_part.split("/", 1)
                hp_cur = int(c_str)
                hp_max = int(m_str)
            except ValueError:
                pass

    if ident.startswith("@"):
        # Player or Companion (7.0+ uses '/' between @Player#ID and CompanionName {ID}:InstanceID; classic uses ':')
        comp_sep = "/" if "/" in ident else (":" if ":" in ident else None)
        if comp_sep:
            owner_part, comp_part = ident.split(comp_sep, 1)
            owner_clean = owner_part.lstrip("@").split("#", 1)[0].strip()
            # Strip {ID} and trailing :InstanceID from companion name
            comp_name = re.sub(r"\{[^}]*\}(?::\d+)?", "", comp_part).split(":", 1)[0].strip()
            m_id = ID_EXTRACT_PATTERN.search(comp_part)
            ent_id = m_id.group(1) if m_id else ""
            return EntityInfo(
                raw=raw_field,
                name=f"{comp_name} ({owner_clean})",
                is_player=False,
                is_companion=True,
                owner_name=owner_clean,
                entity_id=ent_id,
                hp_current=hp_cur,
                hp_max=hp_max,
            )
        else:
            name_part = ident.lstrip("@")
            if "#" in name_part:
                p_name, p_id = name_part.split("#", 1)
            else:
                p_name = re.sub(r"\{[^}]*\}", "", name_part)
                m_id = ID_EXTRACT_PATTERN.search(name_part)
                p_id = m_id.group(1) if m_id else ""
            return EntityInfo(
                raw=raw_field,
                name=p_name.strip(),
                is_player=True,
                is_companion=False,
                owner_name=p_name.strip(),
                entity_id=p_id.strip(),
                hp_current=hp_cur,
                hp_max=hp_max,
            )
    else:
        # NPC or Environment (strip {ID} and any trailing :InstanceID like :34835000736357)
        m_id = ID_EXTRACT_PATTERN.search(ident)
        ent_id = m_id.group(1) if m_id else ""
        npc_name = re.sub(r"\{[^}]*\}(?::\d+)?", "", ident).split(":", 1)[0].strip()
        return EntityInfo(
            raw=raw_field,
            name=npc_name,
            is_player=False,
            is_companion=False,
            entity_id=ent_id,
            hp_current=hp_cur,
            hp_max=hp_max,
        )


def parse_name_and_id(raw_field: str) -> Tuple[str, str]:
    """Extracts human-readable name and {ID} from an ability or effect segment."""
    raw_field = raw_field.strip()
    if not raw_field:
        return ("", "")
    m = ID_EXTRACT_PATTERN.search(raw_field)
    item_id = m.group(1) if m else ""
    name = re.sub(r"\s*\{[^}]*\}", "", raw_field).strip()
    return (name, item_id)


@dataclass
class AbilityStats:
    name: str
    ability_id: str
    damage_type: str = ""
    hits: int = 0
    crits: int = 0
    misses: int = 0
    total_damage: int = 0
    effective_damage: int = 0
    total_heal: int = 0
    effective_heal: int = 0
    max_hit: int = 0
    min_hit: int = 0

    @property
    def crit_rate(self) -> float:
        return (self.crits / self.hits * 100.0) if self.hits > 0 else 0.0

    @property
    def avg_hit(self) -> float:
        return (self.total_damage / self.hits) if self.hits > 0 else 0.0


@dataclass
class CombatantStats:
    name: str
    is_player: bool = True
    is_companion: bool = False
    owner_name: str = ""
    discipline: str = ""
    damage_dealt: int = 0
    effective_damage_dealt: int = 0
    healing_done: int = 0
    effective_healing_done: int = 0
    damage_taken: int = 0
    absorbed_damage: int = 0
    actions: int = 0
    hits: int = 0
    crits: int = 0
    misses: int = 0
    active_combat_seconds: float = 0.0
    fight_count: int = 0
    abilities: Dict[str, AbilityStats] = field(default_factory=dict)


@dataclass
class Encounter:
    encounter_id: int
    start_time: datetime
    end_time: datetime
    in_combat: bool = True
    area_name: str = "Unknown Area"
    primary_target: str = "Combat Encounter"
    target_damage: Dict[str, int] = field(default_factory=dict)
    target_hp: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    combatants: Dict[str, CombatantStats] = field(default_factory=dict)
    dps_timeline: List[Tuple[float, float]] = field(default_factory=list)  # (elapsed_sec, player_dps)
    last_combat_event_time: Optional[datetime] = None

    @property
    def duration_seconds(self) -> float:
        delta = (self.end_time - self.start_time).total_seconds()
        return max(delta, 1.0)

    @property
    def formatted_duration(self) -> str:
        secs = int(round((self.end_time - self.start_time).total_seconds()))
        mins, rem = divmod(max(secs, 0), 60)
        return f"{mins:02d}:{rem:02d}"

    def get_title(self) -> str:
        if self.target_damage:
            best_target = max(self.target_damage.items(), key=lambda kv: kv[1])[0]
            if best_target:
                return best_target
        return self.primary_target


class SWTORLogParser:
    """
    Real-time and batch SWTOR Combat Log Parser.
    Watches the SWTOR CombatLogs directory, tails the newest log file,
    and maintains live & historical encounters.
    """

    COMBAT_TIMEOUT_SECONDS = 25.0  # Auto-close encounter if no combat events for 25s
    SETTINGS_FILE = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "SWTOR_Holocron_DPS" / "settings.json"
    REFERENCES_FILE = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "SWTOR_Holocron_DPS" / "history_references.json"

    def __init__(self, log_dir: Optional[str] = None, load_history: bool = True):
        saved_dir = self._load_saved_log_dir() if load_history else None
        self.log_dir = self._resolve_default_log_dir(log_dir or saved_dir)
        self.current_file: Optional[Path] = None
        self.file_pos: int = 0
        self.base_date: datetime = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.last_parsed_time: Optional[datetime] = None

        self.local_player_name: str = ""
        self._local_player_locked: bool = False
        self.local_discipline: str = ""
        self.player_disciplines: Dict[str, str] = {}
        self.current_area: str = "Unknown Area"
        self.current_area_only: bool = False
        self.include_companions_in_player: bool = True

        self.encounters: List[Encounter] = []
        self.active_encounter: Optional[Encounter] = None
        self._encounter_counter: int = 0
        self._last_timeline_bucket: int = -1
        self.session_name: str = ""
        self._undo_encounters: List[Encounter] = []
        self._undo_active: Optional[Encounter] = None
        self._undo_counter: int = 0

        self.references: List[dict] = []
        self.active_baseline_id: Optional[str] = None
        self._persist_history: bool = load_history
        if load_history:
            self._load_references_from_disk()

        self.remote_version: Optional[str] = None
        self.update_available: bool = False
        self.last_update_check: Optional[str] = None

    def check_github_update(self) -> dict:
        """
        Checks GitHub (main branch swtor_parser.py) for APP_VERSION to signal when a
        newer version has been published. Uses a 4-second timeout and no-cache query
        so it never blocks the UI and bypasses stale CDN cache.
        """
        import urllib.request

        url = f"{GITHUB_RAW_VERSION_URL}?t={int(time.time())}"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": f"SWTOR-Holocron-DPS/{APP_VERSION}",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                body = resp.read(8192).decode("utf-8", errors="ignore")
            m = re.search(r'APP_VERSION\s*=\s*["\'](v?\d+\.\d+(?:\.\d+)?)["\']', body)
            if m:
                rem_ver = m.group(1).strip()
                if not rem_ver.startswith("v"):
                    rem_ver = "v" + rem_ver
                self.remote_version = rem_ver
                self.update_available = parse_semver(rem_ver) > parse_semver(APP_VERSION)
                self.last_update_check = datetime.now().strftime("%H:%M:%S")
                return {
                    "ok": True,
                    "local_version": APP_VERSION,
                    "remote_version": self.remote_version,
                    "update_available": self.update_available,
                    "checked_at": self.last_update_check,
                    "repo_url": GITHUB_REPO_URL,
                }
            return {"ok": False, "error": "Version string not found in remote file", "local_version": APP_VERSION}
        except Exception as e:
            return {"ok": False, "error": str(e), "local_version": APP_VERSION}

    @classmethod
    def _load_saved_log_dir(cls) -> Optional[str]:
        try:
            if cls.SETTINGS_FILE.exists():
                import json
                data = json.loads(cls.SETTINGS_FILE.read_text(encoding="utf-8"))
                saved = data.get("log_dir", "").strip()
                if saved and Path(saved).exists():
                    return saved
        except Exception:
            pass
        return None

    @classmethod
    def save_log_dir_preference(cls, log_dir: Path) -> None:
        try:
            import json
            cls.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            cls.SETTINGS_FILE.write_text(
                json.dumps({"log_dir": str(log_dir)}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    @staticmethod
    def discover_log_directories(current_dir: Optional[Path] = None) -> List[dict]:
        """
        Intelligently scans the user's Windows system for all candidate SWTOR CombatLogs
        directories across:
        - Windows Shell Folders registry (Personal / Documents, including custom drive relocation)
        - OneDrive, OneDriveCommercial, OneDriveConsumer environment variables
        - Standard UserProfile Documents & OneDrive Documents
        - Localized SWTOR folder names (English, German, French)
        - Secondary drives (C:, D:, E:, F:, G:)
        Returns a ranked list of candidate dicts with path, label, exists, log_count, latest_file, latest_mtime.
        """
        user_profile = Path(os.environ.get("USERPROFILE", os.path.expanduser("~")))
        swtor_folder_names = [
            "Star Wars - The Old Republic",
            "Star Wars - The Old Republic - Die Alte Republik",  # German client sometimes uses standard, check both
        ]

        raw_candidates: List[Tuple[Path, str]] = []

        if current_dir:
            raw_candidates.append((Path(current_dir), "Current / Custom Selection"))

        # 1. Windows Registry Shell Folders (handles relocated Documents folders on D:\, E:\, etc.)
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                personal_val, _ = winreg.QueryValueEx(key, "Personal")
                expanded = os.path.expandvars(str(personal_val))
                raw_candidates.append(
                    (Path(expanded) / "Star Wars - The Old Republic" / "CombatLogs", "Windows Registry 'My Documents' Path")
                )
        except Exception:
            pass

        # 2. OneDrive Environment Variables (Personal & Business/University OneDrive)
        for env_key, desc in [
            ("OneDrive", "OneDrive Documents"),
            ("OneDriveConsumer", "OneDrive Personal Documents"),
            ("OneDriveCommercial", "OneDrive Work/School Documents"),
        ]:
            val = os.environ.get(env_key)
            if val:
                raw_candidates.append(
                    (Path(val) / "Documents" / "Star Wars - The Old Republic" / "CombatLogs", desc)
                )
                # Localized German/French Documents inside OneDrive
                for doc_alias in ("Dokumente", "Mes documents"):
                    p_alias = Path(val) / doc_alias / "Star Wars - The Old Republic" / "CombatLogs"
                    if p_alias.exists():
                        raw_candidates.append((p_alias, f"{desc} ({doc_alias})"))

        # 3. Standard UserProfile paths
        raw_candidates.append(
            (user_profile / "Documents" / "Star Wars - The Old Republic" / "CombatLogs", "Standard Local Documents")
        )
        raw_candidates.append(
            (user_profile / "OneDrive" / "Documents" / "Star Wars - The Old Republic" / "CombatLogs", "OneDrive Redirected Documents")
        )

        # 4. Scan any OneDrive-* folders in UserProfile (e.g. "OneDrive - Personal", "OneDrive - Company")
        try:
            for item in user_profile.glob("OneDrive*"):
                if item.is_dir():
                    raw_candidates.append(
                        (item / "Documents" / "Star Wars - The Old Republic" / "CombatLogs", f"{item.name} Documents")
                    )
        except Exception:
            pass

        # 5. Check other drives (D:, E:, F:, G:) for relocated Documents or Users folders
        username = user_profile.name
        for drive in ("D:", "E:", "F:", "G:"):
            drive_root = Path(f"{drive}\\")
            if not drive_root.exists():
                continue
            for cand_path, label in [
                (drive_root / "Documents" / "Star Wars - The Old Republic" / "CombatLogs", f"Relocated {drive}\\Documents"),
                (drive_root / "Users" / username / "Documents" / "Star Wars - The Old Republic" / "CombatLogs", f"{drive}\\Users\\{username}\\Documents"),
                (drive_root / "Users" / username / "OneDrive" / "Documents" / "Star Wars - The Old Republic" / "CombatLogs", f"{drive}\\Users\\{username}\\OneDrive\\Documents"),
            ]:
                if cand_path.exists() or cand_path.parent.exists():
                    raw_candidates.append((cand_path, label))

        # Deduplicate while preserving order and gathering stats
        seen_norm: set[str] = set()
        results: List[dict] = []

        for path_obj, label in raw_candidates:
            try:
                norm = os.path.normcase(os.path.abspath(str(path_obj)))
            except Exception:
                norm = str(path_obj).lower()
            if norm in seen_norm:
                continue
            seen_norm.add(norm)

            exists = False
            parent_exists = False
            log_count = 0
            latest_file = ""
            latest_mtime = 0.0
            latest_time_str = "No logs yet"

            try:
                exists = path_obj.exists()
                parent_exists = path_obj.parent.exists()
                if exists:
                    files = list(path_obj.glob("combat_*.txt")) or list(path_obj.glob("*.txt"))
                    log_count = len(files)
                    if files:
                        newest = max(files, key=lambda p: p.stat().st_mtime)
                        latest_file = newest.name
                        latest_mtime = newest.stat().st_mtime
                        latest_time_str = datetime.fromtimestamp(latest_mtime).strftime("%Y-%m-%d %H:%M")
            except OSError:
                pass

            # Include if it exists, or its parent "Star Wars - The Old Republic" folder exists, or it's one of the primary 2 defaults
            if exists or parent_exists or len(results) < 2:
                results.append({
                    "path": str(path_obj),
                    "label": label,
                    "exists": exists,
                    "parent_exists": parent_exists,
                    "log_count": log_count,
                    "latest_file": latest_file,
                    "latest_mtime": latest_mtime,
                    "latest_time": latest_time_str,
                })

        # Sort: folders with the most recent log files first, then folders that exist, then others
        results.sort(key=lambda r: (r["log_count"] > 0, r["latest_mtime"], r["exists"], r["parent_exists"]), reverse=True)
        return results

    @classmethod
    def _resolve_default_log_dir(cls, custom_dir: Optional[str] = None) -> Path:
        if custom_dir:
            p = Path(custom_dir)
            if p.exists() and any(p.glob("*.txt")):
                return p
            # If user passed a parent folder (e.g. "...\Star Wars - The Old Republic"), check its CombatLogs child
            if (p / "CombatLogs").exists():
                return p / "CombatLogs"

        discovered = cls.discover_log_directories(Path(custom_dir) if custom_dir else None)
        for cand in discovered:
            if cand["log_count"] > 0:
                return Path(cand["path"])
        for cand in discovered:
            if cand["exists"]:
                return Path(cand["path"])
        if custom_dir:
            return Path(custom_dir)
        return Path(discovered[0]["path"]) if discovered else Path.home() / "Documents" / "Star Wars - The Old Republic" / "CombatLogs"

    def reset_all(self) -> None:
        if self.encounters:
            self._undo_encounters = list(self.encounters)
            self._undo_active = self.active_encounter
            self._undo_counter = self._encounter_counter
        self.encounters.clear()
        self.active_encounter = None
        self._encounter_counter = 0
        self._last_timeline_bucket = -1

    def undo_reset(self) -> bool:
        """Restores the session encounters cleared by the most recent reset_all() call."""
        if not self._undo_encounters:
            return False
        self.encounters = list(self._undo_encounters)
        self.active_encounter = self._undo_active
        self._encounter_counter = max(self._encounter_counter, self._undo_counter)
        self._undo_encounters.clear()
        self._undo_active = None
        return True

    def set_log_file(self, file_path: Path, read_from_start: bool = True) -> None:
        self.current_file = file_path
        self.file_pos = 0
        self.last_parsed_time = None
        self.base_date = self._extract_date_from_filename(file_path.name)
        if read_from_start:
            self.encounters.clear()
            self.active_encounter = None
            self._encounter_counter = 0
            self._last_timeline_bucket = -1
            self._local_player_locked = False

    @staticmethod
    def _decode_log_line(raw_bytes: bytes) -> str:
        """
        SWTOR writes non-ASCII character names (e.g. accented Latin characters)
        in Windows-1252 (cp1252). Try UTF-8 first, then fall back to cp1252 so
        accented names never turn into replacement diamonds.
        """
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return raw_bytes.decode("cp1252", errors="replace")

    def load_entire_file(self, file_path: Path) -> int:
        """Reads and parses an entire combat_*.txt log file from start to finish."""
        self.set_log_file(file_path, read_from_start=True)
        if not file_path.exists():
            return 0
        count = 0
        try:
            with open(file_path, "rb") as f:
                for raw_line in f:
                    self.parse_line(self._decode_log_line(raw_line))
                    count += 1
            self._prune_empty_encounters()
        except OSError:
            pass
        return count

    @staticmethod
    def _extract_date_from_filename(filename: str) -> datetime:
        # Format: combat_2026-10-24_10_18_24_123456.txt
        m = re.search(r"combat_(\d{4})-(\d{2})-(\d{2})", filename)
        if m:
            try:
                return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                pass
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def _parse_timestamp(self, ts_str: str) -> datetime:
        ts_str = ts_str.strip()
        try:
            if "/" in ts_str:
                if "." in ts_str:
                    dt = datetime.strptime(ts_str, "%m/%d/%Y %H:%M:%S.%f")
                else:
                    dt = datetime.strptime(ts_str, "%m/%d/%Y %H:%M:%S")
                self.base_date = dt.replace(hour=0, minute=0, second=0, microsecond=0)
                self.last_parsed_time = dt
                return dt
            else:
                parts = ts_str.split(":")
                hour = int(parts[0])
                minute = int(parts[1])
                if "." in parts[2]:
                    sec_s, ms_s = parts[2].split(".", 1)
                    sec = int(sec_s)
                    micro = int(ms_s.ljust(6, "0")[:6])
                else:
                    sec = int(parts[2])
                    micro = 0

                dt = self.base_date.replace(hour=hour, minute=minute, second=sec, microsecond=micro)
                if self.last_parsed_time and dt < self.last_parsed_time - timedelta(hours=12):
                    self.base_date += timedelta(days=1)
                    dt += timedelta(days=1)
                self.last_parsed_time = dt
                return dt
        except Exception:
            return self.last_parsed_time or datetime.now()

    def find_newest_log_file(self) -> Optional[Path]:
        if not self.log_dir.exists():
            return None
        try:
            files = list(self.log_dir.glob("combat_*.txt"))
            if not files:
                files = list(self.log_dir.glob("*.txt"))
            if not files:
                return None
            return max(files, key=lambda p: p.stat().st_mtime)
        except OSError:
            return None

    def poll(self) -> int:
        """
        Checks for new lines in the current log file (or switches if a newer log file appeared).
        Returns the number of lines processed.
        """
        newest = self.find_newest_log_file()
        if newest and newest != self.current_file:
            self.set_log_file(newest, read_from_start=True)

        if not self.current_file or not self.current_file.exists():
            return 0

        try:
            file_size = self.current_file.stat().st_size
            if file_size < self.file_pos:
                self.file_pos = 0
                self.encounters.clear()
                self.active_encounter = None
                self._encounter_counter = 0
                self._last_timeline_bucket = -1
            if file_size == self.file_pos:
                return 0

            lines_processed = 0
            with open(self.current_file, "rb") as f:
                f.seek(self.file_pos)
                while True:
                    raw_line = f.readline()
                    if not raw_line:
                        break
                    if not raw_line.endswith(b"\n") and not raw_line.endswith(b"\r"):
                        break
                    self.file_pos = f.tell()
                    self.parse_line(self._decode_log_line(raw_line))
                    lines_processed += 1

            # Drop empty zero-damage encounters if another encounter followed
            self._prune_empty_encounters()
            return lines_processed
        except OSError:
            return 0

    def _prune_empty_encounters(self) -> None:
        """Removes closed encounters that had 0 total damage and 0 healing (e.g. false combat triggers)."""
        if len(self.encounters) <= 1:
            return
        cleaned: List[Encounter] = []
        for enc in self.encounters:
            if enc is self.active_encounter:
                cleaned.append(enc)
                continue
            total_activity = sum(c.damage_dealt + c.healing_done for c in enc.combatants.values())
            if total_activity > 0:
                cleaned.append(enc)
        self.encounters = cleaned

    def _start_encounter(self, timestamp: datetime) -> Encounter:
        if self.active_encounter and self.active_encounter.in_combat:
            self._close_encounter(timestamp)

        self._encounter_counter += 1
        enc = Encounter(
            encounter_id=self._encounter_counter,
            start_time=timestamp,
            end_time=timestamp,
            in_combat=True,
            area_name=self.current_area,
            last_combat_event_time=timestamp,
        )
        self.active_encounter = enc
        self.encounters.append(enc)
        self._last_timeline_bucket = 0
        return enc

    def _close_encounter(self, timestamp: datetime) -> None:
        if not self.active_encounter:
            return
        if self.active_encounter.last_combat_event_time:
            self.active_encounter.end_time = max(
                self.active_encounter.start_time,
                self.active_encounter.last_combat_event_time,
            )
        else:
            self.active_encounter.end_time = max(self.active_encounter.start_time, timestamp)
        self.active_encounter.in_combat = False
        self._record_timeline_point(self.active_encounter, force=True)
        self.active_encounter = None

    def _get_or_create_combatant(self, enc: Encounter, ent: EntityInfo) -> CombatantStats:
        key = ent.name or "Environment"
        if key not in enc.combatants:
            spec = self.player_disciplines.get(key, "")
            if not spec and key == self.local_player_name:
                spec = self.local_discipline
            enc.combatants[key] = CombatantStats(
                name=key,
                is_player=ent.is_player,
                is_companion=ent.is_companion,
                owner_name=ent.owner_name,
                discipline=spec,
            )
        else:
            if not enc.combatants[key].discipline and key in self.player_disciplines:
                enc.combatants[key].discipline = self.player_disciplines[key]
        return enc.combatants[key]

    def _record_timeline_point(self, enc: Encounter, force: bool = False) -> None:
        elapsed = (enc.end_time - enc.start_time).total_seconds()
        bucket = int(elapsed)
        if not force and bucket <= self._last_timeline_bucket:
            return
        self._last_timeline_bucket = bucket

        player_dmg = 0
        if self.local_player_name and self.local_player_name in enc.combatants:
            player_dmg = enc.combatants[self.local_player_name].damage_dealt
        elif enc.combatants:
            player_dmg = max((c.damage_dealt for c in enc.combatants.values()), default=0)

        dps = player_dmg / max(elapsed, 1.0)
        enc.dps_timeline.append((round(elapsed, 1), round(dps, 1)))

    def parse_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return

        m = LINE_PATTERN.match(line)
        if not m:
            return

        ts_raw, src_raw, tgt_raw, abil_raw, act_raw, rem_raw = m.groups()
        timestamp = self._parse_timestamp(ts_raw)

        src = parse_entity(src_raw)
        if tgt_raw.strip() == "=":
            tgt = src
        else:
            tgt = parse_entity(tgt_raw)

        # Parse Action : Effect
        if ":" in act_raw:
            action_part, effect_part = act_raw.split(":", 1)
        else:
            action_part, effect_part = act_raw, ""

        action_name, action_id = parse_name_and_id(action_part)
        effect_name, effect_id = parse_name_and_id(effect_part)
        ability_name, ability_id = parse_name_and_id(abil_raw)

        # Check AreaEntered (only logged for the local player!)
        if (
            action_id == ID_AREA_ENTERED
            or effect_id == ID_AREA_ENTERED
            or "AreaEntered" in action_name
            or "AreaEntered" in effect_name
            or "AreaEntered" in act_raw
        ):
            area_candidate = ""
            if ":" in effect_part and "AreaEntered" in effect_part:
                _, after_ae = effect_part.split(":", 1)
                area_candidate, _ = parse_name_and_id(after_ae)
            elif "AreaEntered" not in effect_name and effect_name:
                area_candidate = effect_name
            elif ability_name and "AreaEntered" not in ability_name:
                area_candidate = ability_name
            if not area_candidate and rem_raw:
                vm = VALUE_PAREN_PATTERN.search(rem_raw)
                if vm:
                    area_candidate = re.sub(r"\s*\{[^}]*\}", "", vm.group(1)).strip()
            if area_candidate:
                self.current_area = area_candidate
            if src.is_player and src.name:
                self.local_player_name = src.name
                self._local_player_locked = True
            return

        # Check DisciplineChanged
        if action_id == ID_DISCIPLINE_CHANGED or "DisciplineChanged" in action_name or "DisciplineChanged" in effect_name:
            if src.is_player and src.name:
                if not self.local_player_name:
                    self.local_player_name = src.name
                # Extract clean spec name (e.g., "Sniper / Marksmanship" or "Marksmanship")
                raw_spec = effect_name or ability_name
                if not raw_spec and rem_raw:
                    vm = VALUE_PAREN_PATTERN.search(rem_raw)
                    if vm:
                        raw_spec = re.sub(r"\s*\{[^}]*\}", "", vm.group(1)).strip()
                if "/" in raw_spec:
                    cls_p, spec_p = raw_spec.split("/", 1)
                    clean_spec = f"{spec_p.strip()} {cls_p.strip()}"
                else:
                    clean_spec = raw_spec.strip()
                self.player_disciplines[src.name] = clean_spec
                if src.name == self.local_player_name:
                    self.local_discipline = clean_spec
                if self.active_encounter and src.name in self.active_encounter.combatants:
                    self.active_encounter.combatants[src.name].discipline = clean_spec
            return

        if src.is_player and not self.local_player_name:
            self.local_player_name = src.name

        # Check timeout on existing encounter before processing new event
        if (
            self.active_encounter
            and self.active_encounter.in_combat
            and self.active_encounter.last_combat_event_time
        ):
            gap = (timestamp - self.active_encounter.last_combat_event_time).total_seconds()
            if gap > self.COMBAT_TIMEOUT_SECONDS:
                self._close_encounter(self.active_encounter.last_combat_event_time)

        # Handle explicit EnterCombat / ExitCombat
        if effect_id == ID_ENTER_COMBAT or "EnterCombat" in effect_name:
            if src.is_player and src.name and not self._local_player_locked:
                self.local_player_name = src.name
                self._local_player_locked = True
            # If already in an active group encounter, do not split the fight when a teammate enters combat
            if self.active_encounter and self.active_encounter.in_combat:
                self.active_encounter.end_time = max(self.active_encounter.end_time, timestamp)
                self.active_encounter.last_combat_event_time = timestamp
                return
            # If a fight just closed <= 3.0s ago in the same area (e.g. combat stealth / brief drop), reopen it
            if (
                self.encounters
                and self.encounters[-1].area_name == self.current_area
                and 0 <= (timestamp - self.encounters[-1].end_time).total_seconds() <= 3.0
            ):
                self.active_encounter = self.encounters[-1]
                self.active_encounter.in_combat = True
                self.active_encounter.end_time = max(self.active_encounter.end_time, timestamp)
                self.active_encounter.last_combat_event_time = timestamp
                return
            self._start_encounter(timestamp)
            return

        if effect_id == ID_EXIT_COMBAT or "ExitCombat" in effect_name:
            # Only close the encounter if ExitCombat is for the local player (or unattributed)
            if not src.name or not self.local_player_name or src.name == self.local_player_name:
                if self.active_encounter and self.active_encounter.in_combat:
                    self.active_encounter.last_combat_event_time = timestamp
                    self._close_encounter(timestamp)
            return

        # Handle Death event
        if effect_id == ID_DEATH or "Death" in effect_name:
            if tgt.name and self.active_encounter:
                self.active_encounter.last_combat_event_time = timestamp
                self.active_encounter.end_time = timestamp
                if tgt.name == self.local_player_name and len(self.active_encounter.combatants) <= 2:
                    self._close_encounter(timestamp)
            return

        # Count AbilityActivate for APM
        if effect_id == ID_ABILITY_ACTIVATE or "AbilityActivate" in effect_name:
            if self.active_encounter and self.active_encounter.in_combat and (src.is_player or src.is_companion):
                c = self._get_or_create_combatant(self.active_encounter, src)
                c.actions += 1
                self.active_encounter.end_time = timestamp
                self.active_encounter.last_combat_event_time = timestamp
            return

        # Check Damage and Heal under ApplyEffect
        is_damage = (effect_id == ID_DAMAGE) or ("Damage" in effect_name and action_id == ID_APPLY_EFFECT)
        is_heal = (effect_id == ID_HEAL) or ("Heal" in effect_name and action_id == ID_APPLY_EFFECT)

        if not (is_damage or is_heal):
            return

        val_match = VALUE_PAREN_PATTERN.search(rem_raw)
        if not val_match:
            return
        val_inside = val_match.group(1).strip()

        dmg_match = DAMAGE_VALUE_PATTERN.match(val_inside)
        if not dmg_match:
            return

        raw_amount = int(dmg_match.group(1))
        is_crit = dmg_match.group(2) == "*"
        tilde_effective = int(dmg_match.group(3)) if dmg_match.group(3) is not None else None
        sub_token = (dmg_match.group(4) or "").strip()

        # Auto-start (or reopen recent) encounter if we receive a Damage event while not in combat
        if is_damage and not self.active_encounter:
            if src.is_player or src.is_companion or tgt.is_player or tgt.is_companion:
                if (
                    self.encounters
                    and self.encounters[-1].area_name == self.current_area
                    and 0 <= (timestamp - self.encounters[-1].end_time).total_seconds() <= 3.0
                ):
                    self.active_encounter = self.encounters[-1]
                    self.active_encounter.in_combat = True
                else:
                    self._start_encounter(timestamp)

        enc = self.active_encounter
        if not enc:
            return

        enc.end_time = timestamp
        enc.last_combat_event_time = timestamp

        abil_display = ability_name or effect_name or "Basic Attack"
        abil_key = f"{abil_display} ({ability_id})" if ability_id else abil_display

        if is_damage:
            is_miss = (raw_amount == 0 and sub_token.startswith("-")) or any(
                w in val_inside.lower() for w in ("-miss", "-dodge", "-parry", "-deflect", "-immune", "-resist", "-evade")
            )

            eff_amount = tilde_effective if tilde_effective is not None else raw_amount

            abs_match = ABSORB_PATTERN.search(val_inside)
            absorbed_val = int(abs_match.group(1)) if abs_match else 0

            # Record on attacker
            if src.name:
                attacker = self._get_or_create_combatant(enc, src)
                attacker.damage_dealt += raw_amount
                attacker.effective_damage_dealt += eff_amount
                if is_miss and raw_amount == 0:
                    attacker.misses += 1
                else:
                    attacker.hits += 1
                    if is_crit:
                        attacker.crits += 1

                if abil_key not in attacker.abilities:
                    attacker.abilities[abil_key] = AbilityStats(
                        name=abil_display,
                        ability_id=ability_id,
                        damage_type=sub_token.lstrip("-"),
                    )
                ab = attacker.abilities[abil_key]
                if not ab.damage_type and not sub_token.startswith("-"):
                    ab.damage_type = sub_token
                if is_miss and raw_amount == 0:
                    ab.misses += 1
                else:
                    ab.hits += 1
                    if is_crit:
                        ab.crits += 1
                    ab.total_damage += raw_amount
                    ab.effective_damage += eff_amount
                    if raw_amount > ab.max_hit:
                        ab.max_hit = raw_amount
                    if ab.min_hit == 0 or (raw_amount > 0 and raw_amount < ab.min_hit):
                        ab.min_hit = raw_amount

            # Record on target (damage taken & primary boss/dummy tracking)
            if tgt.name:
                if tgt.is_player or tgt.is_companion:
                    defender = self._get_or_create_combatant(enc, tgt)
                    defender.damage_taken += raw_amount
                    defender.absorbed_damage += absorbed_val
                else:
                    enc.target_damage[tgt.name] = enc.target_damage.get(tgt.name, 0) + raw_amount
                    if tgt.hp_current is not None and tgt.hp_max is not None and tgt.hp_max > 0:
                        enc.target_hp[tgt.name] = (tgt.hp_current, tgt.hp_max)

            self._record_timeline_point(enc)

        elif is_heal:
            eff_heal = tilde_effective if tilde_effective is not None else raw_amount
            if src.name and (src.is_player or src.is_companion):
                healer = self._get_or_create_combatant(enc, src)
                healer.healing_done += raw_amount
                healer.effective_healing_done += eff_heal

                if abil_key not in healer.abilities:
                    healer.abilities[abil_key] = AbilityStats(
                        name=abil_display,
                        ability_id=ability_id,
                        damage_type="heal",
                    )
                ab = healer.abilities[abil_key]
                ab.hits += 1
                if is_crit:
                    ab.crits += 1
                ab.total_heal += raw_amount
                ab.effective_heal += eff_heal
                if raw_amount > ab.max_hit:
                    ab.max_hit = raw_amount

    @staticmethod
    def _format_hms(seconds: float) -> str:
        secs = max(int(round(seconds)), 0)
        if secs >= 3600:
            hrs = secs // 3600
            mins = (secs % 3600) // 60
            rem = secs % 60
            return f"{hrs:02d}:{mins:02d}:{rem:02d}"
        mins, rem = divmod(secs, 60)
        return f"{mins:02d}:{rem:02d}"

    def _get_session_encounters(self) -> List[Encounter]:
        """Returns the encounters included in Session Mode (all or filtered to current_area)."""
        if not self.encounters:
            return []
        if self.current_area_only and self.current_area and self.current_area != "Unknown Area":
            filtered = [e for e in self.encounters if e.area_name == self.current_area]
            if filtered:
                return filtered
        return self.encounters

    def set_current_area_only(self, enabled: bool) -> bool:
        self.current_area_only = bool(enabled)
        return self.current_area_only

    def _compute_session_summary(self) -> dict:
        sess_encs = self._get_session_encounters()
        distinct_areas = []
        for e in self.encounters:
            if e.area_name and e.area_name not in distinct_areas:
                distinct_areas.append(e.area_name)
        if not sess_encs:
            return {
                "encounter_count": 0,
                "total_log_encounters": len(self.encounters),
                "current_area_only": self.current_area_only,
                "distinct_areas": distinct_areas,
                "combat_duration": "00:00",
                "combat_seconds": 0.0,
                "session_span": "00:00",
                "session_span_seconds": 0.0,
                "session_start": "--:--:--",
                "session_end": "--:--:--",
                "session_name": self.session_name,
            }
        total_combat = sum(e.duration_seconds for e in sess_encs)
        first_start = sess_encs[0].start_time
        last_end = sess_encs[-1].end_time
        span_secs = max((last_end - first_start).total_seconds(), total_combat)
        return {
            "encounter_count": len(sess_encs),
            "total_log_encounters": len(self.encounters),
            "current_area_only": self.current_area_only,
            "distinct_areas": distinct_areas,
            "combat_duration": self._format_hms(total_combat),
            "combat_seconds": round(total_combat, 1),
            "session_span": self._format_hms(span_secs),
            "session_span_seconds": round(span_secs, 1),
            "session_start": first_start.strftime("%H:%M:%S"),
            "session_end": last_end.strftime("%H:%M:%S"),
            "session_name": self.session_name,
        }

    def _build_session_aggregate_encounter(self) -> Optional[Encounter]:
        """
        Aggregates all encounters in the current play session into a single unified
        Encounter object so the player can analyze all skills, abilities, hits, crits,
        and total damage across 7-10+ pulls or a full 2+ hour gaming session.
        """
        sess_encs = self._get_session_encounters()
        if not sess_encs:
            return None

        first_start = sess_encs[0].start_time
        last_end = sess_encs[-1].end_time
        in_combat = any(e.in_combat for e in sess_encs)
        sess_title = self.session_name.strip() if self.session_name.strip() else f"{self.current_area} — Session Total"

        agg = Encounter(
            encounter_id=0,
            start_time=first_start,
            end_time=last_end,
            in_combat=in_combat,
            area_name=self.current_area,
            primary_target=f"{sess_title} ({len(sess_encs)} fights)",
        )

        # Aggregate combatants and unify abilities by human-readable skill name
        for e in sess_encs:
            e_dur = e.duration_seconds
            for tgt_name, dmg_val in e.target_damage.items():
                agg.target_damage[tgt_name] = agg.target_damage.get(tgt_name, 0) + dmg_val

            for c_name, c in e.combatants.items():
                if not (c.is_player or c.is_companion):
                    continue
                if c_name not in agg.combatants:
                    agg.combatants[c_name] = CombatantStats(
                        name=c.name,
                        is_player=c.is_player,
                        is_companion=c.is_companion,
                        owner_name=c.owner_name,
                        discipline=c.discipline,
                    )
                ac = agg.combatants[c_name]
                if c.discipline and not ac.discipline:
                    ac.discipline = c.discipline
                ac.damage_dealt += c.damage_dealt
                ac.effective_damage_dealt += c.effective_damage_dealt
                ac.healing_done += c.healing_done
                ac.effective_healing_done += c.effective_healing_done
                ac.damage_taken += c.damage_taken
                ac.absorbed_damage += c.absorbed_damage
                ac.actions += c.actions
                ac.hits += c.hits
                ac.crits += c.crits
                ac.misses += c.misses
                if c.damage_dealt > 0 or c.healing_done > 0 or c.damage_taken > 0 or c.actions > 0:
                    ac.active_combat_seconds += e_dur
                    ac.fight_count += 1

                for ab in c.abilities.values():
                    skill_key = ab.name or ab.ability_id or "Basic Attack"
                    if skill_key not in ac.abilities:
                        ac.abilities[skill_key] = AbilityStats(
                            name=ab.name or skill_key,
                            ability_id=ab.ability_id,
                            damage_type=ab.damage_type,
                        )
                    aab = ac.abilities[skill_key]
                    if not aab.damage_type and ab.damage_type:
                        aab.damage_type = ab.damage_type
                    aab.hits += ab.hits
                    aab.crits += ab.crits
                    aab.misses += ab.misses
                    aab.total_damage += ab.total_damage
                    aab.effective_damage += ab.effective_damage
                    aab.total_heal += ab.total_heal
                    aab.effective_heal += ab.effective_heal
                    if ab.max_hit > aab.max_hit:
                        aab.max_hit = ab.max_hit
                    if ab.min_hit > 0 and (aab.min_hit == 0 or ab.min_hit < aab.min_hit):
                        aab.min_hit = ab.min_hit

        # Build session timeline: if multiple encounters, plot per-encounter DPS progression
        # or concatenate intra-encounter timeline points across cumulative combat time
        cum_time = 0.0
        timeline_pts: List[Tuple[float, float]] = []
        for e in sess_encs:
            if e.dps_timeline:
                for t_rel, val_dps in e.dps_timeline:
                    timeline_pts.append((round(cum_time + t_rel, 1), round(val_dps, 1)))
            else:
                dur_e = e.duration_seconds
                p_c = e.combatants.get(self.local_player_name)
                if p_c:
                    timeline_pts.append((round(cum_time + dur_e, 1), round(p_c.damage_dealt / dur_e, 1)))
            cum_time += e.duration_seconds

        if len(timeline_pts) > 120:
            step = max(1, len(timeline_pts) // 120)
            agg.dps_timeline = timeline_pts[::step][-120:]
        else:
            agg.dps_timeline = timeline_pts

        return agg

    def set_session_name(self, name: str) -> str:
        self.session_name = name.strip()
        return self.session_name

    def get_snapshot(self, encounter_index: Optional[int] = None) -> dict:
        """
        Returns a serializable dictionary snapshot of the requested encounter:
        - encounter_index == -2: Full Session Total (aggregates all encounters & skills in session)
        - encounter_index is None or -1: Live / Latest single encounter
        - encounter_index >= 0: Specific historical encounter in current session
        """
        self._prune_empty_encounters()
        is_session_view = (encounter_index == -2)
        sess_summary = self._compute_session_summary()

        enc: Optional[Encounter] = None
        if is_session_view:
            enc = self._build_session_aggregate_encounter()
        elif encounter_index is not None and 0 <= encounter_index < len(self.encounters):
            enc = self.encounters[encounter_index]
        elif self.active_encounter:
            enc = self.active_encounter
        elif self.encounters:
            enc = self.encounters[-1]

        encounter_list = [
            {
                "index": idx,
                "id": e.encounter_id,
                "title": e.get_title(),
                "duration": e.formatted_duration,
                "in_combat": e.in_combat,
                "start_time": e.start_time.strftime("%H:%M:%S"),
            }
            for idx, e in enumerate(self.encounters)
        ]

        if not enc:
            return {
                "app_version": APP_VERSION,
                "remote_version": self.remote_version,
                "update_available": self.update_available,
                "last_update_check": self.last_update_check,
                "has_encounter": False,
                "is_session_view": is_session_view,
                "current_area_only": self.current_area_only,
                "area_name": self.current_area,
                "in_combat": False,
                "can_undo_reset": bool(self._undo_encounters),
                "log_dir": str(self.log_dir),
                "log_file": self.current_file.name if self.current_file else "Waiting for combat_*.txt...",
                "local_player": self.local_player_name or "Detecting...",
                "discipline": self.local_discipline,
                "area": self.current_area,
                "encounters": encounter_list,
                "session_summary": sess_summary,
                "active_baseline": self.get_active_baseline(),
                "references_count": len(self.references),
            }

        # For Full Session view, denominator for DPS/APM is total active combat seconds
        # so out-of-combat walking time between pulls does not artificially zero out DPS.
        dur = max(sess_summary["combat_seconds"], 1.0) if is_session_view else enc.duration_seconds
        formatted_dur = sess_summary["combat_duration"] if is_session_view else enc.formatted_duration

        combatants_list = []
        local_stats = None
        sess_encs_for_runs = self._get_session_encounters() if is_session_view else ([enc] if enc else [])

        for c in enc.combatants.values():
            if not (c.is_player or c.is_companion):
                continue
            c_dur = max(c.active_combat_seconds, 1.0) if (is_session_view and c.active_combat_seconds > 0) else dur
            c_fights = c.fight_count if (is_session_view and c.fight_count > 0) else (sess_summary["encounter_count"] if is_session_view else 1)
            dps = c.damage_dealt / c_dur
            edps = c.effective_damage_dealt / c_dur
            hps = c.healing_done / c_dur
            ehps = c.effective_healing_done / c_dur
            dtps = c.damage_taken / c_dur
            apm = (c.actions / c_dur) * 60.0
            total_attempts = c.hits + c.misses
            acc = (c.hits / total_attempts * 100.0) if total_attempts > 0 else 100.0
            crit_pct = (c.crits / c.hits * 100.0) if c.hits > 0 else 0.0

            abilities_sorted = sorted(
                c.abilities.values(),
                key=lambda a: (a.total_damage, a.total_heal),
                reverse=True,
            )
            abil_data = []
            max_hit_val = 0
            max_hit_abil = ""
            for a in abilities_sorted:
                if (a.total_damage > 0 or c.damage_dealt == 0) and a.max_hit > max_hit_val:
                    max_hit_val = a.max_hit
                    max_hit_abil = a.name
                if a.total_damage == 0 and a.total_heal == 0 and a.misses == 0:
                    continue
                pct = (a.total_damage / c.damage_dealt * 100.0) if c.damage_dealt > 0 else 0.0
                abil_data.append({
                    "name": a.name,
                    "id": a.ability_id,
                    "type": a.damage_type,
                    "total_damage": a.total_damage,
                    "dps": round(a.total_damage / c_dur, 1),
                    "pct": round(pct, 1),
                    "hits": a.hits,
                    "crits": a.crits,
                    "misses": a.misses,
                    "crit_pct": round(a.crit_rate, 1),
                    "avg_hit": round(a.avg_hit, 0),
                    "max_hit": a.max_hit,
                    "total_heal": a.total_heal,
                    "hps": round(a.total_heal / c_dur, 1),
                })

            fight_runs = []
            peak_fight_dps = 0.0
            peak_fight_title = ""
            for idx_e, e_item in enumerate(self.encounters):
                if is_session_view and self.current_area_only and e_item not in sess_encs_for_runs:
                    continue
                if not is_session_view and e_item is not enc:
                    continue
                ec = e_item.combatants.get(c.name)
                if not ec or (ec.damage_dealt == 0 and ec.healing_done == 0 and ec.actions == 0):
                    continue
                e_dur = max(e_item.duration_seconds, 1.0)
                e_dps = round(ec.damage_dealt / e_dur, 1)
                e_crit = round((ec.crits / ec.hits * 100.0) if ec.hits > 0 else 0.0, 1)
                e_title = e_item.get_title()
                if e_dps > peak_fight_dps:
                    peak_fight_dps = e_dps
                    peak_fight_title = e_title
                fight_runs.append({
                    "index": idx_e,
                    "id": e_item.encounter_id,
                    "title": e_title,
                    "duration": e_item.formatted_duration,
                    "damage": ec.damage_dealt,
                    "dps": e_dps,
                    "crit_pct": e_crit,
                })

            entry = {
                "name": c.name,
                "is_player": c.is_player,
                "is_companion": c.is_companion,
                "is_local": c.name == self.local_player_name,
                "discipline": c.discipline,
                "fight_count": c_fights,
                "active_combat_seconds": round(c_dur, 1),
                "active_duration": self._format_hms(c_dur),
                "damage": c.damage_dealt,
                "dps": round(dps, 1),
                "edps": round(edps, 1),
                "heal": c.healing_done,
                "hps": round(hps, 1),
                "ehps": round(ehps, 1),
                "damage_taken": c.damage_taken,
                "dtps": round(dtps, 1),
                "absorbed": c.absorbed_damage,
                "apm": round(apm, 1),
                "crit_pct": round(crit_pct, 1),
                "accuracy_pct": round(acc, 1),
                "hits": c.hits,
                "misses": c.misses,
                "max_hit": max_hit_val,
                "max_hit_ability": max_hit_abil,
                "peak_fight_dps": round(peak_fight_dps, 1),
                "peak_fight_title": peak_fight_title,
                "abilities": abil_data,
                "fight_runs": fight_runs,
            }
            combatants_list.append(entry)
            if c.name == self.local_player_name:
                local_stats = entry

        # In Session Mode, sort combatants by DPS (or damage in single-fight mode) so teammates
        # from earlier Flashpoints don't outrank current Flashpoint teammates who have higher DPS
        combatants_list.sort(key=lambda x: (x["dps"], x["damage"]), reverse=True)
        if not local_stats and combatants_list:
            local_stats = combatants_list[0]

        fight_markers = []
        if is_session_view:
            sess_encs = self._get_session_encounters()
            sess_label = self.session_name.strip() or self.current_area
            title = f"{sess_label} ({len(sess_encs)} fights)"
            # Never show a Boss HP bar in Session mode (fixes F2 stale 0% red HP bar)
            target_hp_info = None
            cum_t = 0.0
            for idx, e_item in enumerate(self.encounters):
                if self.current_area_only and e_item not in sess_encs:
                    continue
                dur_e = e_item.duration_seconds
                p_c = e_item.combatants.get(self.local_player_name)
                if not p_c and e_item.combatants:
                    p_c = max(e_item.combatants.values(), key=lambda x: x.damage_dealt, default=None)
                e_dps = round(p_c.damage_dealt / max(dur_e, 1.0), 1) if p_c else 0.0
                fight_markers.append({
                    "index": idx,
                    "id": e_item.encounter_id,
                    "title": e_item.get_title(),
                    "duration": e_item.formatted_duration,
                    "start_sec": round(cum_t, 1),
                    "end_sec": round(cum_t + dur_e, 1),
                    "dps": e_dps,
                })
                cum_t += dur_e
        else:
            title = enc.get_title()
            target_hp_info = None
            if title in enc.target_hp:
                cur_hp, max_hp = enc.target_hp[title]
                if max_hp > 0 and cur_hp > 0:
                    target_hp_info = {
                        "current": cur_hp,
                        "max": max_hp,
                        "pct": round(cur_hp / max_hp * 100.0, 1),
                    }

        return {
            "app_version": APP_VERSION,
            "remote_version": self.remote_version,
            "update_available": self.update_available,
            "last_update_check": self.last_update_check,
            "has_encounter": True,
            "is_session_view": is_session_view,
            "current_area_only": self.current_area_only,
            "area_name": self.current_area,
            "can_undo_reset": bool(self._undo_encounters),
            "encounter_id": enc.encounter_id,
            "in_combat": enc.in_combat,
            "title": title,
            "area": enc.area_name,
            "duration": formatted_dur,
            "duration_seconds": round(dur, 1),
            "session_summary": sess_summary,
            "target_hp": target_hp_info,
            "fight_markers": fight_markers,
            "log_dir": str(self.log_dir),
            "log_file": self.current_file.name if self.current_file else "Simulated / Loaded",
            "local_player": self.local_player_name or (local_stats["name"] if local_stats else "Player"),
            "discipline": self.local_discipline,
            "local_stats": local_stats,
            "combatants": combatants_list,
            "timeline": enc.dps_timeline[-120:],
            "encounters": encounter_list,
            "active_baseline": self.get_active_baseline(),
            "references_count": len(self.references),
        }

    @staticmethod
    def _fmt_compact_dmg(val: float) -> str:
        n = float(val or 0)
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}M"
        if n >= 100_000:
            return f"{n / 1_000:.0f}K"
        return f"{int(round(n)):,}"

    @staticmethod
    def _clean_chat_str(s: str) -> str:
        return (s or "").replace("\ufffd", "").strip()

    def format_chat_summary(
        self,
        encounter_index: Optional[int] = -2,
        mode: str = "personal",
        combatant_name: str = "",
    ) -> dict:
        """
        Formats a single-line, SWTOR-chat-safe (<250 chars, pure ASCII/Latin text)
        summary of the selected Session (-2) or Fight (-1 / 0..N) ready to paste
        into SWTOR in-game chat (/p, /ops, /g, /s) via Ctrl+V.

        Supported modes:
        - "personal": Local player's (or specified combatant_name's) DPS, Crit, Acc, APM, Max Hit & Top Skills
        - "group" / "team": Whole-team Group DPS Leaderboard (Team Total DPS + Ranked 1..N players with DPS, % share & Crit)
        - "healing" / "heal": Whole-team Group Heal & Tank Leaderboard (HPS & DTPS across the group)
        """
        snap = self.get_snapshot(encounter_index)
        if not snap.get("has_encounter") or not snap.get("local_stats"):
            return {"ok": False, "text": "", "error": "No combat data to copy yet."}

        is_sess = bool(snap.get("is_session_view"))
        sess_sum = snap.get("session_summary") or {}
        combatants = snap.get("combatants") or []
        dur_str = snap.get("duration", "00:00")
        area_or_title = self._clean_chat_str(
            (self.session_name.strip() or snap.get("area") or "Session")
            if is_sess
            else (snap.get("title") or "Fight")
        )

        norm_mode = (mode or "personal").strip().lower()

        # --- GROUP / TEAM DPS LEADERBOARD MODE ---
        if norm_mode in ("group", "team") and not combatant_name:
            active_c = [c for c in combatants if (c.get("damage", 0) > 0 or c.get("dps", 0) > 0)]
            if not active_c:
                active_c = combatants
            total_team_dps = sum(c.get("dps", 0.0) for c in active_c)
            total_team_dmg = sum(c.get("damage", 0) for c in active_c)

            if is_sess:
                f_cnt = sess_sum.get("encounter_count", 1)
                hdr = f"[Holocron Team] {area_or_title} ({f_cnt} fights, {dur_str}) | Team: {int(round(total_team_dps)):,}/s ({self._fmt_compact_dmg(total_team_dmg)})"
            else:
                hdr = f"[Holocron Team] vs {area_or_title} ({dur_str}) | Team: {int(round(total_team_dps)):,}/s ({self._fmt_compact_dmg(total_team_dmg)})"

            # Build ranked entries with progressive compactness so 4-8+ players fit inside <=250 chars
            for detail_level in ("full", "medium", "compact", "minimal"):
                parts = []
                limit_count = 6 if detail_level in ("full", "medium") else 8
                for idx, c in enumerate(active_c[:limit_count]):
                    c_name = self._clean_chat_str(c.get("name", "Player"))
                    # Shorten companion owner suffix if space is tight
                    if detail_level in ("compact", "minimal") and "(" in c_name:
                        c_name = c_name.split("(", 1)[0].strip()
                    c_dps = int(round(c.get("dps", 0)))
                    c_crit = c.get("crit_pct", 0.0)
                    c_share = (c.get("damage", 0) / total_team_dmg * 100.0) if total_team_dmg > 0 else 0.0
                    c_spec = self._clean_chat_str(c.get("discipline", ""))

                    if detail_level == "full" and len(active_c) <= 4 and c_spec:
                        parts.append(f"{idx + 1}. {c_name} ({c_spec}) {c_dps:,}/s ({c_share:.0f}%, {c_crit:.0f}%c)")
                    elif detail_level in ("full", "medium"):
                        parts.append(f"{idx + 1}. {c_name} {c_dps:,}/s ({c_share:.0f}%, {c_crit:.0f}%c)")
                    elif detail_level == "compact":
                        parts.append(f"{idx + 1}.{c_name} {c_dps:,}/s ({c_share:.0f}%)")
                    else:
                        parts.append(f"{idx + 1}.{c_name} {c_dps:,}/s")

                candidate = f"{hdr} || " + " | ".join(parts)
                if len(candidate) <= 250 or detail_level == "minimal":
                    return {"ok": True, "text": candidate[:250], "is_session": is_sess, "mode": "group"}

        # --- GROUP HEAL & TANK MODE ---
        if norm_mode in ("healing", "heal", "tank") and not combatant_name:
            heal_c = sorted(combatants, key=lambda x: (x.get("hps", 0.0), x.get("dtps", 0.0)), reverse=True)
            total_hps = sum(c.get("hps", 0.0) for c in heal_c)
            if is_sess:
                f_cnt = sess_sum.get("encounter_count", 1)
                hdr = f"[Holocron Heal/Tank] {area_or_title} ({f_cnt} fights, {dur_str}) | Team HPS: {int(round(total_hps)):,}/s"
            else:
                hdr = f"[Holocron Heal/Tank] vs {area_or_title} ({dur_str}) | Team HPS: {int(round(total_hps)):,}/s"
            parts = []
            for idx, c in enumerate(heal_c[:6]):
                c_name = self._clean_chat_str(c.get("name", "Player")).split("(", 1)[0].strip()
                parts.append(f"{idx + 1}. {c_name}: {int(round(c.get('hps', 0))):,} HPS / {int(round(c.get('dtps', 0))):,} DTPS")
            line = (f"{hdr} || " + " | ".join(parts))[:250]
            return {"ok": True, "text": line, "is_session": is_sess, "mode": "healing"}

        # --- PERSONAL OR SPECIFIC COMBATANT SUMMARY MODE ---
        p = snap["local_stats"]
        rank_prefix = ""
        if combatant_name:
            for idx, c in enumerate(combatants):
                if c.get("name") == combatant_name:
                    p = c
                    if len(combatants) > 1:
                        total_team_dmg = max(sum(x.get("damage", 0) for x in combatants), 1)
                        share_pct = (c.get("damage", 0) / total_team_dmg) * 100.0
                        rank_prefix = f"#{idx + 1}/{len(combatants)} ({share_pct:.0f}% team dmg) "
                    break

        player = self._clean_chat_str(p.get("name") or snap.get("local_player") or "Player")
        spec = self._clean_chat_str(p.get("discipline") or (snap.get("discipline") if not combatant_name else "") or "")
        spec_str = f" ({spec})" if spec else ""

        if is_sess:
            f_cnt = sess_sum.get("encounter_count", 1)
            span_s = sess_sum.get("session_span", dur_str)
            header = f"[Holocron Session] {rank_prefix}{player}{spec_str} ({f_cnt} fights, {dur_str} combat / {span_s} span)"
        else:
            header = f"[Holocron] {rank_prefix}{player}{spec_str} vs {area_or_title} ({dur_str})"

        core = (
            f"{int(round(p.get('dps', 0))):,} DPS ({int(round(p.get('damage', 0))):,} dmg)"
            f" | Crit {p.get('crit_pct', 0.0):.1f}% | Acc {p.get('accuracy_pct', 100.0):.1f}% | {p.get('apm', 0.0):.1f} APM"
        )

        extras = []
        if p.get("max_hit", 0) > 0:
            mh_abil = self._clean_chat_str(p.get("max_hit_ability") or "Hit")
            extras.append(f"Max {int(round(p['max_hit'])):,} ({mh_abil})")
        if p.get("hps", 0) >= 10:
            extras.append(f"{int(round(p['hps'])):,} HPS")
        if p.get("dtps", 0) >= 10:
            extras.append(f"{int(round(p['dtps'])):,} DTPS")

        base = snap.get("active_baseline")
        if not combatant_name and base and base.get("dps", 0) > 0:
            diff = p.get("dps", 0) - base["dps"]
            sign = "+" if diff >= 0 else ""
            extras.append(f"vs Ref {sign}{int(round(diff)):,}/s")

        abils = [
            f"{self._clean_chat_str(a['name'])} {a.get('pct', 0):.0f}%"
            for a in (p.get("abilities") or [])[:3]
            if a.get("total_damage", 0) > 0
        ]

        ext_str = (" | " + " | ".join(extras)) if extras else ""
        top_str = (f" | Top: {', '.join(abils)}") if abils else ""

        full_line = f"{header}: {core}{ext_str}{top_str}"
        if len(full_line) > 250 and abils:
            short_top = f" | Top: {abils[0]}"
            full_line = f"{header}: {core}{ext_str}{short_top}"
        if len(full_line) > 250:
            full_line = f"{header}: {core}{ext_str}"[:250]

        return {"ok": True, "text": full_line, "is_session": is_sess, "mode": "personal"}

    # -------------------------------------------------------------------------
    # Historical Reference Points & Cross-Session Analysis
    # -------------------------------------------------------------------------

    def _load_references_from_disk(self) -> None:
        try:
            if self.REFERENCES_FILE.exists():
                import json
                data = json.loads(self.REFERENCES_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.references = list(data.get("references", []))
                    self.active_baseline_id = data.get("active_baseline_id")
                elif isinstance(data, list):
                    self.references = data
        except Exception:
            self.references = []
            self.active_baseline_id = None

    def _save_references_to_disk(self) -> None:
        if not self._persist_history:
            return
        try:
            import json
            self.REFERENCES_FILE.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "active_baseline_id": self.active_baseline_id,
                "references": self.references,
            }
            self.REFERENCES_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def get_active_baseline(self) -> Optional[dict]:
        if not self.active_baseline_id:
            return None
        for r in self.references:
            if r.get("id") == self.active_baseline_id:
                return r
        return None

    def get_references_state(self) -> dict:
        return {
            "references": self.references,
            "active_baseline_id": self.active_baseline_id,
            "active_baseline": self.get_active_baseline(),
        }

    def _build_reference_from_snapshot(self, snap: dict, label: str = "") -> Optional[dict]:
        if not snap.get("has_encounter") or not snap.get("local_stats"):
            return None
        p = snap["local_stats"]
        abilities = p.get("abilities", [])
        max_hit = 0
        max_hit_ability = ""
        for a in abilities:
            if (a.get("total_damage", 0) > 0 or p.get("damage", 0) == 0) and a.get("max_hit", 0) > max_hit:
                max_hit = a["max_hit"]
                max_hit_ability = a.get("name", "")

        now = datetime.now()
        ref_id = f"ref_{int(now.timestamp() * 1000)}_{len(self.references) + 1}"
        is_session = bool(snap.get("is_session_view", False))
        sess_sum = snap.get("session_summary") or {}
        enc_count = sess_sum.get("encounter_count", 1) if is_session else 1
        session_span = sess_sum.get("session_span", snap.get("duration", "00:00"))

        target_name = snap.get("title") or "Combat Encounter"
        discipline = p.get("discipline") or snap.get("discipline") or ""
        char_name = p.get("name") or snap.get("local_player") or "Player"
        clean_label = label.strip() if label else ""
        if not clean_label:
            if is_session:
                clean_label = self.session_name.strip() or f"Session: {snap.get('area', 'SWTOR')} ({enc_count} fights)"
            else:
                clean_label = f"{target_name} ({snap.get('duration', '00:00')})"

        log_file = snap.get("log_file") or ""
        session_date = now.strftime("%Y-%m-%d")
        m_date = re.search(r"combat_(\d{4}-\d{2}-\d{2})", log_file)
        if m_date:
            session_date = m_date.group(1)

        return {
            "id": ref_id,
            "is_session": is_session,
            "encounter_count": enc_count,
            "session_span": session_span,
            "saved_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "session_date": session_date,
            "label": clean_label,
            "character": char_name,
            "discipline": discipline,
            "target": target_name,
            "area": snap.get("area") or "Unknown Area",
            "log_file": log_file,
            "duration": snap.get("duration", "00:00"),
            "duration_seconds": snap.get("duration_seconds", 1.0),
            "dps": p.get("dps", 0.0),
            "edps": p.get("edps", 0.0),
            "total_damage": p.get("damage", 0),
            "hps": p.get("hps", 0.0),
            "total_heal": p.get("heal", 0),
            "dtps": p.get("dtps", 0.0),
            "damage_taken": p.get("damage_taken", 0),
            "apm": p.get("apm", 0.0),
            "crit_pct": p.get("crit_pct", 0.0),
            "accuracy_pct": p.get("accuracy_pct", 100.0),
            "hits": p.get("hits", 0),
            "misses": p.get("misses", 0),
            "max_hit": max_hit,
            "max_hit_ability": max_hit_ability,
            "abilities": [
                {
                    "name": a["name"],
                    "dps": a["dps"],
                    "pct": a["pct"],
                    "crit_pct": a["crit_pct"],
                    "avg_hit": a["avg_hit"],
                    "max_hit": a["max_hit"],
                    "hits": a["hits"],
                    "total_damage": a["total_damage"],
                }
                for a in abilities[:40]
            ],
            "combatants": [
                {
                    "name": c.get("name", "Player"),
                    "discipline": c.get("discipline", ""),
                    "dps": c.get("dps", 0.0),
                    "damage": c.get("damage", 0),
                    "crit_pct": c.get("crit_pct", 0.0),
                    "apm": c.get("apm", 0.0),
                    "hps": c.get("hps", 0.0),
                    "dtps": c.get("dtps", 0.0),
                }
                for c in (snap.get("combatants") or [])[:16]
            ],
        }

    def save_reference_point(
        self,
        encounter_index: Optional[int] = None,
        label: str = "",
        set_as_baseline: bool = True,
    ) -> dict:
        if encounter_index == -2 and label.strip():
            self.session_name = label.strip()
        snap = self.get_snapshot(encounter_index)
        ref = self._build_reference_from_snapshot(snap, label=label)
        if not ref:
            return {
                "ok": False,
                "error": "No active or completed combat encounter to save yet.",
                **self.get_references_state(),
            }
        self.references.insert(0, ref)
        if set_as_baseline or not self.active_baseline_id:
            self.active_baseline_id = ref["id"]
        self._save_references_to_disk()
        return {
            "ok": True,
            "reference": ref,
            **self.get_references_state(),
        }

    def set_active_baseline(self, ref_id: Optional[str]) -> dict:
        if not ref_id or ref_id == self.active_baseline_id:
            self.active_baseline_id = None
        else:
            if any(r.get("id") == ref_id for r in self.references):
                self.active_baseline_id = ref_id
        self._save_references_to_disk()
        return {"ok": True, **self.get_references_state()}

    def update_reference_label(self, ref_id: str, label: str) -> dict:
        clean = label.strip()
        if not clean:
            return {"ok": False, **self.get_references_state()}
        for r in self.references:
            if r.get("id") == ref_id:
                r["label"] = clean
                break
        self._save_references_to_disk()
        return {"ok": True, **self.get_references_state()}

    def delete_reference_point(self, ref_id: str) -> dict:
        self.references = [r for r in self.references if r.get("id") != ref_id]
        if self.active_baseline_id == ref_id:
            self.active_baseline_id = self.references[0]["id"] if self.references else None
        self._save_references_to_disk()
        return {"ok": True, **self.get_references_state()}

    def scan_historical_logs(self, max_files: int = 8, min_duration: float = 12.0, max_results: int = 60) -> List[dict]:
        """
        Scans past combat_*.txt log files in self.log_dir across gaming sessions
        and returns both the Full Session Summary (encounter_index = -2) for each log file
        and its notable individual encounters.
        """
        if not self.log_dir.exists():
            return []
        try:
            files = sorted(
                self.log_dir.glob("combat_*.txt"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )[:max_files]
        except OSError:
            return []

        results: List[dict] = []
        for fpath in files:
            temp = SWTORLogParser(log_dir=str(self.log_dir), load_history=False)
            temp.load_entire_file(fpath)
            temp._prune_empty_encounters()
            if not temp.encounters:
                continue

            m_date = re.search(r"combat_(\d{4}-\d{2}-\d{2})_(\d{2})_(\d{2})", fpath.name)
            session_str = f"{m_date.group(1)} {m_date.group(2)}:{m_date.group(3)}" if m_date else fpath.name

            # 1. Add the Full Aggregated Play Session entry (encounter_index = -2)
            sess_snap = temp.get_snapshot(-2)
            sess_p = sess_snap.get("local_stats")
            sess_sum = sess_snap.get("session_summary") or {}
            if sess_p and (sess_p.get("damage", 0) > 0 or sess_p.get("heal", 0) > 0):
                results.append({
                    "is_session": True,
                    "file_name": fpath.name,
                    "session_time": session_str,
                    "encounter_index": -2,
                    "encounter_id": 0,
                    "encounter_count": len(temp.encounters),
                    "session_span": sess_sum.get("session_span", sess_snap.get("duration", "00:00")),
                    "start_time": sess_sum.get("session_start", ""),
                    "character": sess_p.get("name") or sess_snap.get("local_player") or "Player",
                    "discipline": sess_p.get("discipline") or sess_snap.get("discipline") or "",
                    "target": f"Full Play Session ({len(temp.encounters)} fights - {sess_snap.get('area', 'SWTOR')})",
                    "area": sess_snap.get("area") or "Unknown Area",
                    "duration": sess_snap.get("duration", "00:00"),
                    "duration_seconds": sess_snap.get("duration_seconds", 1.0),
                    "dps": sess_p.get("dps", 0.0),
                    "total_damage": sess_p.get("damage", 0),
                    "crit_pct": sess_p.get("crit_pct", 0.0),
                    "apm": sess_p.get("apm", 0.0),
                })

            # 2. Add top individual boss/mob encounters from that session
            file_items: List[dict] = []
            for idx, enc in enumerate(temp.encounters):
                if enc.duration_seconds < min_duration:
                    continue
                snap = temp.get_snapshot(idx)
                p = snap.get("local_stats")
                if not p or (p.get("damage", 0) <= 0 and p.get("heal", 0) <= 0):
                    continue
                file_items.append({
                    "is_session": False,
                    "file_name": fpath.name,
                    "session_time": session_str,
                    "encounter_index": idx,
                    "encounter_id": enc.encounter_id,
                    "encounter_count": 1,
                    "session_span": snap.get("duration", "00:00"),
                    "start_time": enc.start_time.strftime("%H:%M:%S"),
                    "character": p.get("name") or snap.get("local_player") or "Player",
                    "discipline": p.get("discipline") or snap.get("discipline") or "",
                    "target": snap.get("title") or "Combat Encounter",
                    "area": snap.get("area") or "Unknown Area",
                    "duration": snap.get("duration", "00:00"),
                    "duration_seconds": snap.get("duration_seconds", 1.0),
                    "dps": p.get("dps", 0.0),
                    "total_damage": p.get("damage", 0),
                    "crit_pct": p.get("crit_pct", 0.0),
                    "apm": p.get("apm", 0.0),
                })
            file_items.sort(key=lambda x: x["total_damage"], reverse=True)
            results.extend(file_items[:12])
            if len(results) >= max_results:
                break
        return results[:max_results]

    def import_historical_encounter(self, file_name: str, encounter_index: int, label: str = "") -> dict:
        """Imports either a Full Session (encounter_index == -2) or a specific encounter from a past combat_*.txt file."""
        fpath = self.log_dir / file_name
        if not fpath.exists():
            return {"ok": False, "error": f"Log file not found: {file_name}", **self.get_references_state()}
        temp = SWTORLogParser(log_dir=str(self.log_dir), load_history=False)
        temp.load_entire_file(fpath)
        temp._prune_empty_encounters()
        if encounter_index != -2 and (encounter_index < 0 or encounter_index >= len(temp.encounters)):
            return {"ok": False, "error": "Encounter index out of range", **self.get_references_state()}
        snap = temp.get_snapshot(encounter_index)
        snap["log_file"] = fpath.name
        ref = self._build_reference_from_snapshot(snap, label=label)
        if not ref:
            return {"ok": False, "error": "Could not build reference from encounter", **self.get_references_state()}
        self.references.insert(0, ref)
        if not self.active_baseline_id:
            self.active_baseline_id = ref["id"]
        self._save_references_to_disk()
        return {"ok": True, "reference": ref, **self.get_references_state()}
