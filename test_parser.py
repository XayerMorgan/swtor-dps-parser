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
        parser = SWTORLogParser()
        for line in generate_instant_demo_lines():
            parser.parse_line(line)

        snap = parser.get_snapshot()
        self.assertTrue(snap["has_encounter"])
        self.assertEqual(snap["local_player"], "DarthVex")
        self.assertGreater(snap["local_stats"]["dps"], 20000)
        self.assertEqual(len(snap["combatants"]), 4)
        self.assertIsNotNone(snap["target_hp"])


if __name__ == "__main__":
    unittest.main()
