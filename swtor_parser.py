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

    def __init__(self, log_dir: Optional[str] = None):
        saved_dir = self._load_saved_log_dir()
        self.log_dir = self._resolve_default_log_dir(log_dir or saved_dir)
        self.current_file: Optional[Path] = None
        self.file_pos: int = 0
        self.base_date: datetime = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.last_parsed_time: Optional[datetime] = None

        self.local_player_name: str = ""
        self.local_discipline: str = ""
        self.player_disciplines: Dict[str, str] = {}
        self.current_area: str = "Unknown Area"
        self.include_companions_in_player: bool = True

        self.encounters: List[Encounter] = []
        self.active_encounter: Optional[Encounter] = None
        self._encounter_counter: int = 0
        self._last_timeline_bucket: int = -1

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
        self.encounters.clear()
        self.active_encounter = None
        self._encounter_counter = 0
        self._last_timeline_bucket = -1

    def set_log_file(self, file_path: Path, read_from_start: bool = True) -> None:
        self.current_file = file_path
        self.file_pos = 0
        self.last_parsed_time = None
        self.base_date = self._extract_date_from_filename(file_path.name)

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
            if file_size == self.file_pos:
                return 0

            lines_processed = 0
            with open(self.current_file, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self.file_pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith("\n") and not line.endswith("\r"):
                        break
                    self.file_pos = f.tell()
                    self.parse_line(line)
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
        if action_id == ID_AREA_ENTERED or "AreaEntered" in action_name:
            if effect_name:
                self.current_area = effect_name
            if src.is_player and src.name:
                self.local_player_name = src.name
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
            if src.is_player and src.name:
                self.local_player_name = src.name
            self._start_encounter(timestamp)
            return

        if effect_id == ID_EXIT_COMBAT or "ExitCombat" in effect_name:
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

        # Auto-start encounter if we receive a Damage event while not in combat
        if is_damage and not self.active_encounter:
            if src.is_player or src.is_companion or tgt.is_player or tgt.is_companion:
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

    def get_snapshot(self, encounter_index: Optional[int] = None) -> dict:
        """
        Returns a serializable dictionary snapshot of the requested encounter
        (or the current/latest encounter if encounter_index is None) for UI rendering.
        """
        self._prune_empty_encounters()
        enc: Optional[Encounter] = None
        if encounter_index is not None and 0 <= encounter_index < len(self.encounters):
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
                "has_encounter": False,
                "in_combat": False,
                "log_dir": str(self.log_dir),
                "log_file": self.current_file.name if self.current_file else "Waiting for combat_*.txt...",
                "local_player": self.local_player_name or "Detecting...",
                "discipline": self.local_discipline,
                "area": self.current_area,
                "encounters": encounter_list,
            }

        dur = enc.duration_seconds
        combatants_list = []
        local_stats = None

        for c in enc.combatants.values():
            if not (c.is_player or c.is_companion):
                continue
            dps = c.damage_dealt / dur
            edps = c.effective_damage_dealt / dur
            hps = c.healing_done / dur
            ehps = c.effective_healing_done / dur
            dtps = c.damage_taken / dur
            apm = (c.actions / dur) * 60.0
            total_attempts = c.hits + c.misses
            acc = (c.hits / total_attempts * 100.0) if total_attempts > 0 else 100.0
            crit_pct = (c.crits / c.hits * 100.0) if c.hits > 0 else 0.0

            abilities_sorted = sorted(
                c.abilities.values(),
                key=lambda a: (a.total_damage, a.total_heal),
                reverse=True,
            )
            abil_data = []
            for a in abilities_sorted:
                if a.total_damage == 0 and a.total_heal == 0 and a.misses == 0:
                    continue
                pct = (a.total_damage / c.damage_dealt * 100.0) if c.damage_dealt > 0 else 0.0
                abil_data.append({
                    "name": a.name,
                    "id": a.ability_id,
                    "type": a.damage_type,
                    "total_damage": a.total_damage,
                    "dps": round(a.total_damage / dur, 1),
                    "pct": round(pct, 1),
                    "hits": a.hits,
                    "crits": a.crits,
                    "misses": a.misses,
                    "crit_pct": round(a.crit_rate, 1),
                    "avg_hit": round(a.avg_hit, 0),
                    "max_hit": a.max_hit,
                    "total_heal": a.total_heal,
                    "hps": round(a.total_heal / dur, 1),
                })

            entry = {
                "name": c.name,
                "is_player": c.is_player,
                "is_companion": c.is_companion,
                "is_local": c.name == self.local_player_name,
                "discipline": c.discipline,
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
                "abilities": abil_data,
            }
            combatants_list.append(entry)
            if c.name == self.local_player_name:
                local_stats = entry

        combatants_list.sort(key=lambda x: x["damage"], reverse=True)
        if not local_stats and combatants_list:
            local_stats = combatants_list[0]

        title = enc.get_title()
        target_hp_info = None
        if title in enc.target_hp:
            cur_hp, max_hp = enc.target_hp[title]
            target_hp_info = {
                "current": cur_hp,
                "max": max_hp,
                "pct": round(cur_hp / max_hp * 100.0, 1) if max_hp > 0 else 0.0,
            }

        return {
            "has_encounter": True,
            "encounter_id": enc.encounter_id,
            "in_combat": enc.in_combat,
            "title": title,
            "area": enc.area_name,
            "duration": enc.formatted_duration,
            "duration_seconds": round(dur, 1),
            "target_hp": target_hp_info,
            "log_dir": str(self.log_dir),
            "log_file": self.current_file.name if self.current_file else "Simulated / Loaded",
            "local_player": self.local_player_name or (local_stats["name"] if local_stats else "Player"),
            "discipline": self.local_discipline,
            "local_stats": local_stats,
            "combatants": combatants_list,
            "timeline": enc.dps_timeline[-120:],
            "encounters": encounter_list,
        }
