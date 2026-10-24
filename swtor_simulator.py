"""
SWTOR Combat Log Simulator
Generates realistic SWTOR 7.0+ and classic combat log entries for testing and live demo.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional


SAMPLE_ABILITIES = [
    ("Annihilate", "807780269252608", "energy", 29500, 36500, 0.42),
    ("Rupture", "807754499448832", "internal", 9200, 13800, 0.38),
    ("Force Rend", "3441033997287424", "internal", 11400, 16200, 0.38),
    ("Vicious Slash", "807745909514240", "energy", 16800, 21900, 0.35),
    ("Dual Saber Throw", "2145366161997824", "energy", 22400, 27800, 0.40),
    ("Furious Strike", "3904521098067968", "energy", 31000, 39200, 0.48),
    ("Assault", "807737319579648", "energy", 8200, 11500, 0.30),
    ("Bleed (Tech/Force)", "807754499449088", "internal", 6400, 9100, 0.40),
]

RAID_MEMBERS = [
    ("@DarthVex#687132173159297", "Annihilation", 1.0),
    ("@KiraCarsen#687132173159881", "Virulence", 0.91),
    ("@ShaeVizla#687132173159442", "Innovative Ordnance", 0.87),
    ("@TheronShan#687132173159109", "Lightning", 0.82),
]


def generate_instant_demo_lines() -> List[str]:
    """Generates a complete 45-second Operations Training Dummy parse in SWTOR 7.0+ format."""
    lines: List[str] = []
    now = datetime.now() - timedelta(seconds=50)

    def fmt_ts(dt: datetime) -> str:
        return dt.strftime("%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"

    player_tag = "@DarthVex#687132173159297|(15.20,-42.10,0.05,180.0)|(342000/342000)"
    lines.append(
        f"[{fmt_ts(now)}] [{player_tag}] [] [] "
        f"[AreaEntered {{836045448953664}}: Gav Daragon Bridge Deck {{137438987726}}] (he3000) <v7.5.1>"
    )
    now += timedelta(milliseconds=120)
    lines.append(
        f"[{fmt_ts(now)}] [{player_tag}] [] [] "
        f"[Event {{836045448945472}}: DisciplineChanged {{836045448953665}}] (Marauder {{1614090259187123}} / Annihilation {{1614111912948912}})"
    )
    now += timedelta(milliseconds=450)
    lines.append(
        f"[{fmt_ts(now)}] [{player_tag}] [=] [] "
        f"[Event {{836045448945472}}: EnterCombat {{836045448945489}}] ()"
    )

    dummy_max_hp = 6500000
    dummy_cur_hp = dummy_max_hp

    rng = random.Random(42)
    elapsed_ms = 0
    total_ms = 45000
    step_ms = 750  # GCD + DoT ticks

    while elapsed_ms < total_ms:
        elapsed_ms += step_ms
        cur_dt = now + timedelta(milliseconds=elapsed_ms)

        # Primary player ability
        abil_name, abil_id, dmg_type, min_d, max_d, crit_chance = rng.choice(SAMPLE_ABILITIES)
        dmg_type_id = "836045448940874" if dmg_type == "energy" else "836045448940876"

        # Log AbilityActivate every ~1.4s
        if (elapsed_ms // step_ms) % 2 == 0:
            lines.append(
                f"[{fmt_ts(cur_dt)}] [{player_tag}] "
                f"[Operations Training Dummy {{2857785343475712:1920001293812}}|(15.20,-38.00,0.05,0.0)|({dummy_cur_hp}/{dummy_max_hp})] "
                f"[{abil_name} {{{abil_id}}}] [Event {{836045448945472}}: AbilityActivate {{836045448945479}}] ()"
            )

        is_crit = rng.random() < crit_chance
        base_dmg = rng.randint(min_d, max_d)
        if is_crit:
            base_dmg = int(base_dmg * 1.68)
        crit_star = "*" if is_crit else ""

        dummy_cur_hp = max(0, dummy_cur_hp - base_dmg)
        dummy_tag = f"Operations Training Dummy {{2857785343475712:1920001293812}}|(15.20,-38.00,0.05,0.0)|({dummy_cur_hp}/{dummy_max_hp})"

        lines.append(
            f"[{fmt_ts(cur_dt)}] [{player_tag}] [{dummy_tag}] "
            f"[{abil_name} {{{abil_id}}}] [ApplyEffect {{836045448945477}}: Damage {{836045448945501}}] "
            f"({base_dmg}{crit_star} ~{base_dmg} {dmg_type} {{{dmg_type_id}}}) <{base_dmg}>"
        )

        # Self-heal from Annihilation bleeds (Hungering)
        if dmg_type == "internal":
            heal_amt = int(base_dmg * 0.18)
            lines.append(
                f"[{fmt_ts(cur_dt)}] [{player_tag}] [=] "
                f"[Hungering {{807788859187200}}] [ApplyEffect {{836045448945477}}: Heal {{836045448945500}}] "
                f"({heal_amt}{crit_star} ~{heal_amt}) <{heal_amt // 2}>"
            )

        # Group members dealing damage
        for member_handle, spec, scale in RAID_MEMBERS[1:]:
            if rng.random() < 0.65:
                m_abil, m_id, m_type, m_min, m_max, m_crit_c = rng.choice(SAMPLE_ABILITIES)
                m_crit = rng.random() < m_crit_c
                m_dmg = int(rng.randint(m_min, m_max) * scale * (1.65 if m_crit else 1.0))
                m_star = "*" if m_crit else ""
                m_tag = f"{member_handle}|(12.0,-40.0,0.0,180.0)|(330000/330000)"
                dummy_cur_hp = max(0, dummy_cur_hp - m_dmg)
                dummy_tag = f"Operations Training Dummy {{2857785343475712:1920001293812}}|(15.20,-38.00,0.05,0.0)|({dummy_cur_hp}/{dummy_max_hp})"
                lines.append(
                    f"[{fmt_ts(cur_dt)}] [{m_tag}] [{dummy_tag}] "
                    f"[{m_abil} {{{m_id}}}] [ApplyEffect {{836045448945477}}: Damage {{836045448945501}}] "
                    f"({m_dmg}{m_star} ~{m_dmg} {m_type} {{836045448940874}}) <{m_dmg}>"
                )

    end_dt = now + timedelta(milliseconds=total_ms + 500)
    lines.append(
        f"[{fmt_ts(end_dt)}] [{player_tag}] [=] [] "
        f"[Event {{836045448945472}}: ExitCombat {{836045448945490}}] ()"
    )
    return lines
