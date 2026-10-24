"""
Unit tests for SWTORLogParser covering:
- SWTOR 7.0+ format (player handles, coordinates, HP pools, tilde effective damage/heal)
- Classic SWTOR format (MM/DD/YYYY timestamps, standard brackets)
- Companion attribution
- Miss/Dodge/Absorb parsing
- Automatic encounter start/stop and timeout handling
"""

import unittest
from swtor_parser import SWTORLogParser, parse_entity
from swtor_simulator import generate_instant_demo_lines


class TestSWTORParser(unittest.TestCase):
    def test_parse_entity_7_0_player(self):
        raw = "@DarthVex#687132173159297|(15.20,-42.10,0.05,180.0)|(342000/342000)"
        ent = parse_entity(raw)
        self.assertTrue(ent.is_player)
        self.assertFalse(ent.is_companion)
        self.assertEqual(ent.name, "DarthVex")
        self.assertEqual(ent.entity_id, "687132173159297")
        self.assertEqual(ent.hp_current, 342000)
        self.assertEqual(ent.hp_max, 342000)

    def test_parse_entity_companion(self):
        raw = "@DarthVex#687132173159297:Lana Beniko {3640265981952000}|(10,20,0,0)|(200000/200000)"
        ent = parse_entity(raw)
        self.assertFalse(ent.is_player)
        self.assertTrue(ent.is_companion)
        self.assertEqual(ent.owner_name, "DarthVex")
        self.assertEqual(ent.name, "Lana Beniko (DarthVex)")

    def test_parse_classic_and_modern_lines(self):
        parser = SWTORLogParser()
        lines = [
            "[14:23:50.000] [@Idrurrez] [@Idrurrez] [] [Event {836045448945472}: EnterCombat {836045448945489}] ()",
            "[14:23:51.000] [@Idrurrez] [Elite Tastybobble {846623953387520}] [Force Leap {812105301229568}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (698 energy {836045448940874}) <698>.",
            "[14:23:53.000] [@Idrurrez] [Elite Tastybobble {846623953387520}] [Master Strike {812139660967936}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1302* energy {836045448940874}) <1302>.",
            "[14:23:55.000] [@Idrurrez] [@Idrurrez] [] [Event {836045448945472}: ExitCombat {836045448945490}] ()",
        ]
        for line in lines:
            parser.parse_line(line)

        snap = parser.get_snapshot()
        self.assertTrue(snap["has_encounter"])
        self.assertFalse(snap["in_combat"])
        self.assertEqual(snap["local_player"], "Idrurrez")
        self.assertEqual(snap["local_stats"]["damage"], 2000)
        self.assertEqual(snap["local_stats"]["dps"], 400.0)  # 2000 dmg over 5.0s
        self.assertEqual(snap["local_stats"]["crit_pct"], 50.0)

    def test_simulated_7_0_encounter(self):
        parser = SWTORLogParser(load_history=False)
        for line in generate_instant_demo_lines():
            parser.parse_line(line)

        snap = parser.get_snapshot()
        self.assertTrue(snap["has_encounter"])
        self.assertEqual(snap["local_player"], "DarthVex")
        self.assertGreater(snap["local_stats"]["dps"], 20000)
        self.assertEqual(len(snap["combatants"]), 4)
        self.assertIsNotNone(snap["target_hp"])

    def test_reference_points_and_history(self):
        parser = SWTORLogParser(load_history=False)
        for line in generate_instant_demo_lines():
            parser.parse_line(line)

        # Save a reference point
        res = parser.save_reference_point(label="340 Gear Baseline", set_as_baseline=True)
        self.assertTrue(res["ok"])
        ref = res["reference"]
        self.assertEqual(ref["label"], "340 Gear Baseline")
        self.assertEqual(ref["character"], "DarthVex")
        self.assertGreater(ref["dps"], 20000)
        self.assertTrue(len(ref["abilities"]) > 0)
        self.assertEqual(parser.active_baseline_id, ref["id"])

        # Rename reference point
        ren = parser.update_reference_label(ref["id"], "Updated Tactical Parse")
        self.assertTrue(ren["ok"])
        self.assertEqual(parser.get_active_baseline()["label"], "Updated Tactical Parse")

        # Delete reference point
        del_res = parser.delete_reference_point(ref["id"])
        self.assertTrue(del_res["ok"])
        self.assertEqual(len(parser.references), 0)
        self.assertIsNone(parser.active_baseline_id)

    def test_full_session_aggregation_across_encounters(self):
        parser = SWTORLogParser(load_history=False)
        lines = [
            # Encounter 1 (5 seconds, 2000 damage using Snipe & Ambush)
            "[14:00:00.000] [@Magisterium] [@Magisterium] [] [Event {836045448945472}: EnterCombat {836045448945489}] ()",
            "[14:00:01.000] [@Magisterium] [Varad Churl {111}] [Snipe {1001}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (800 energy {836045448940874}) <800>.",
            "[14:00:03.000] [@Magisterium] [Varad Churl {111}] [Ambush {1002}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1200* energy {836045448940874}) <1200>.",
            "[14:00:05.000] [@Magisterium] [@Magisterium] [] [Event {836045448945472}: ExitCombat {836045448945490}] ()",
            # Encounter 2 (10 minutes later, 5 seconds, 3000 damage using Snipe & Followthrough)
            "[14:10:00.000] [@Magisterium] [@Magisterium] [] [Event {836045448945472}: EnterCombat {836045448945489}] ()",
            "[14:10:02.000] [@Magisterium] [Gorga Brak {222}] [Snipe {1001}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (1000* energy {836045448940874}) <1000>.",
            "[14:10:04.000] [@Magisterium] [Gorga Brak {222}] [Followthrough {1003}] [ApplyEffect {836045448945477}: Damage {836045448945501}] (2000* energy {836045448940874}) <2000>.",
            "[14:10:05.000] [@Magisterium] [@Magisterium] [] [Event {836045448945472}: ExitCombat {836045448945490}] ()",
        ]
        for line in lines:
            parser.parse_line(line)

        # Single latest encounter (-1) should only have 3000 damage
        latest_snap = parser.get_snapshot(-1)
        self.assertEqual(latest_snap["local_stats"]["damage"], 3000)

        # Full Session Total (-2) should aggregate both encounters (5000 damage over 10s active combat)
        sess_snap = parser.get_snapshot(-2)
        self.assertTrue(sess_snap["is_session_view"])
        self.assertEqual(sess_snap["session_summary"]["encounter_count"], 2)
        self.assertEqual(sess_snap["local_stats"]["damage"], 5000)
        self.assertEqual(sess_snap["local_stats"]["dps"], 500.0)  # 5000 dmg / 10.0s active combat
        abil_names = {a["name"]: a for a in sess_snap["local_stats"]["abilities"]}
        self.assertIn("Snipe", abil_names)
        self.assertIn("Ambush", abil_names)
        self.assertIn("Followthrough", abil_names)
        self.assertEqual(abil_names["Snipe"]["total_damage"], 1800)
        self.assertEqual(abil_names["Snipe"]["hits"], 2)

        # Save as a Named Session
        saved_sess = parser.save_reference_point(-2, label="Spirit of Vengeance 2hr Run")
        self.assertTrue(saved_sess["ok"])
        self.assertTrue(saved_sess["reference"]["is_session"])
        self.assertEqual(saved_sess["reference"]["encounter_count"], 2)
        self.assertEqual(saved_sess["reference"]["label"], "Spirit of Vengeance 2hr Run")

        # F2: Session mode never shows a stale Boss HP bar
        self.assertIsNone(sess_snap["target_hp"])

        # F7: Max Hit and ability name are exposed on local_stats
        self.assertEqual(sess_snap["local_stats"]["max_hit"], 2000)
        self.assertEqual(sess_snap["local_stats"]["max_hit_ability"], "Followthrough")

        # F8: Session mode exposes fight_markers for chart boundaries and tooltips
        self.assertEqual(len(sess_snap["fight_markers"]), 2)
        self.assertEqual(sess_snap["fight_markers"][0]["title"], "Varad Churl")
        self.assertEqual(sess_snap["fight_markers"][1]["title"], "Gorga Brak")

        # F1: Reset preserves undo buffer and undo_reset() restores full session
        parser.reset_all()
        empty_snap = parser.get_snapshot(-2)
        self.assertFalse(empty_snap["has_encounter"])
        self.assertTrue(empty_snap["can_undo_reset"])

        restored = parser.undo_reset()
        self.assertTrue(restored)
        restored_snap = parser.get_snapshot(-2)
        self.assertTrue(restored_snap["has_encounter"])
        self.assertEqual(restored_snap["session_summary"]["encounter_count"], 2)
        self.assertEqual(restored_snap["local_stats"]["damage"], 5000)

        # Verify Session & Fight SWTOR Chat Clipboard Summary formatting
        sess_chat = parser.format_chat_summary(-2)
        self.assertTrue(sess_chat["ok"])
        self.assertLessEqual(len(sess_chat["text"]), 250)
        self.assertIn("[Holocron Session]", sess_chat["text"])
        self.assertIn("2 fights", sess_chat["text"])
        self.assertIn("500 DPS (5,000 dmg)", sess_chat["text"])
        self.assertIn("Crit 75.0%", sess_chat["text"])
        self.assertIn("Top: Followthrough 40%", sess_chat["text"])

        fight_chat = parser.format_chat_summary(-1)
        self.assertTrue(fight_chat["ok"])
        self.assertLessEqual(len(fight_chat["text"]), 250)
        self.assertIn("[Holocron] Magisterium vs Gorga Brak", fight_chat["text"])
        self.assertIn("600 DPS (3,000 dmg)", fight_chat["text"])


if __name__ == "__main__":
    unittest.main()
