import unittest

from uplink import templates
from uplink.muse import ModelAPIError
from uplink.writer import Writer, allowed_numbers, check_draft, numbers_in, tidy

from .support import ScriptedModel

CASCADE = {
    "event": "outage",
    "network": "Freifunk München",
    "kind": "cascade",
    "started": "16:15",
    "nodes_offline": 7,
    "root": "ffNoe-Stadt-Daniel5",
    "nodes_behind_root": 6,
    "still_online_above": "ffNoe-Rathaus",
    "people_affected": 23,
    "failed_within_seconds": 15,
    "likely_cause": "ffNoe-Stadt-Daniel5 failed and the nodes behind it had no other path",
    "check_first": "power and cabling at ffNoe-Stadt-Daniel5",
    "affected_nodes": ["ffNoe-Stadt-Daniel5", "ffNoe-Stadt-St.Georg8"],
}
GOOD_TELUGU = (
    "*ffNoe-Stadt-Daniel5 ఆఫ్‌లైన్ అయింది*\n"
    "16:15 నుండి 7 నోడ్‌లు ఆఫ్‌లైన్, సుమారు 23 మంది వినియోగదారులకు ఇంటర్నెట్ లేదు.\n"
    "ముందుగా ffNoe-Stadt-Daniel5 వద్ద విద్యుత్ చూడండి."
)


class DraftCheckTest(unittest.TestCase):
    def test_a_faithful_draft_passes(self):
        self.assertEqual(check_draft(GOOD_TELUGU, CASCADE, 700), "")

    def test_an_invented_number_fails(self):
        problem = check_draft("*Daniel5 is down*\nAbout 40 people are offline.", CASCADE, 700)
        self.assertIn("40", problem)

    def test_twelve_hour_times_and_grouped_digits_are_fine(self):
        self.assertEqual(check_draft("Down since 4:15 PM, 7 nodes.", CASCADE, 700), "")
        facts = {**CASCADE, "people_affected": 2118}
        self.assertEqual(check_draft("2,118 people affected", facts, 700), "")

    def test_numbers_in_other_scripts_are_checked_too(self):
        self.assertEqual(numbers_in("౧౬:౧౫ నుండి ౭ నోడ్‌లు"), {16, 15, 7})
        self.assertEqual(check_draft("౭ నోడ్‌లు", CASCADE, 700), "")
        self.assertIn("9", check_draft("౯ నోడ్‌లు", CASCADE, 700))

    def test_links_empty_and_long_drafts_fail(self):
        self.assertEqual(check_draft("", CASCADE, 700), "was empty")
        self.assertEqual(check_draft("see https://map.ffmuc.net", CASCADE, 700), "contained a link")
        self.assertIn("characters", check_draft("x" * 701, CASCADE, 700))

    def test_allowed_numbers_include_twelve_hour_forms(self):
        self.assertTrue({16, 15, 4, 7, 6, 23}.issubset(allowed_numbers(CASCADE)))

    def test_tidy_turns_markdown_into_whatsapp(self):
        self.assertEqual(tidy('"## Alert\n**Daniel5 is down**"'), "Alert\n*Daniel5 is down*")

    def test_tidy_closes_up_spaces_inside_bold(self):
        # A real Muse Spark headline. WhatsApp shows "* text *" as plain text with stars around it.
        self.assertEqual(
            tidy("* Cascade outage: site-bcg5 plus 6 nodes offline *\n7 nodes offline since 22/10 16:15."),
            "*Cascade outage: site-bcg5 plus 6 nodes offline*\n7 nodes offline since 22/10 16:15.",
        )
        self.assertEqual(tidy("** site-bcg5 ఆఫ్‌లైన్ **"), "*site-bcg5 ఆఫ్‌లైన్*")
        self.assertEqual(tidy("Root *site-bcg5 * is down, check * power*."), "Root *site-bcg5* is down, check *power*.")
        # Not bold, so left alone: list bullets, including one whose line ends in real bold.
        for untouched in ("* site-bca3\n* site-bca5", "* Check first: power at *site-bcg5*", "*7 nodes* offline"):
            with self.subTest(untouched=untouched):
                self.assertEqual(tidy(untouched), untouched)


class WriterTest(unittest.TestCase):
    def test_uses_the_model_draft_when_it_checks_out(self):
        model = ScriptedModel([GOOD_TELUGU])
        draft = Writer(model, language="telugu", network="Freifunk München").write(CASCADE)
        self.assertEqual((draft.text, draft.source), (GOOD_TELUGU, "muse"))
        system, user = model.calls[0]
        self.assertEqual(system["role"], "developer")
        self.assertIn("Telugu", system["content"])
        self.assertIn("Freifunk München", system["content"])
        self.assertIn('"root": "ffNoe-Stadt-Daniel5"', user["content"])

    def test_asks_for_one_rewrite_then_falls_back(self):
        model = ScriptedModel(["About 40 people are offline.", "Still 40 people."])
        draft = Writer(model, language="english", network="Freifunk München").write(CASCADE)
        self.assertEqual(draft.source, "template")
        self.assertIn("40", draft.note)
        self.assertEqual(draft.text, templates.render(CASCADE, "english"))
        retry = model.calls[1]
        self.assertEqual(retry[-2], {"role": "assistant", "content": "About 40 people are offline."})
        self.assertIn("not in the facts", retry[-1]["content"])

    def test_a_good_rewrite_is_accepted(self):
        model = ScriptedModel(["About 40 people are offline.", "*Daniel5 is down*\n23 people offline since 16:15."])
        draft = Writer(model, language="english", network="Freifunk München").write(CASCADE)
        self.assertEqual(draft.source, "muse")

    def test_falls_back_when_the_model_is_unreachable(self):
        model = ScriptedModel([ModelAPIError("HTTP 503")])
        draft = Writer(model, language="hindi", network="Freifunk München").write(CASCADE)
        self.assertEqual(draft.source, "template")
        self.assertIn("503", draft.note)
        self.assertIn("ffNoe-Stadt-Daniel5", draft.text)

    def test_without_a_model_templates_are_used(self):
        draft = Writer(None, language="english", network="X").write(CASCADE)
        self.assertEqual(draft.source, "template")

    def test_feed_problems_never_go_through_the_model(self):
        model = ScriptedModel([])
        facts = {"event": "feed_stale", "network": "Freifunk München", "since": "14:05"}
        draft = Writer(model, language="english", network="Freifunk München").write(facts)
        self.assertEqual(draft.source, "template")
        self.assertEqual(model.calls, [])

    def test_answers_see_the_state_and_the_question(self):
        model = ScriptedModel(["7 nodes are down since 16:15."])
        state = {"open_outages": [{"started": "16:15", "nodes": 7}]}
        draft = Writer(model, language="english", network="N").answer("what is down?", state, "fallback")
        self.assertEqual(draft.source, "muse")
        roles = [m["role"] for m in model.calls[0]]
        self.assertEqual(roles, ["developer", "developer", "user"])

    def test_answers_may_repeat_numbers_from_the_question(self):
        model = ScriptedModel(["Nothing changed in the last 3 hours."])
        draft = Writer(model, language="english", network="N").answer("anything in the last 3 hours?", {}, "fallback")
        self.assertEqual(draft.source, "muse")


class TemplateTest(unittest.TestCase):
    def facts(self, kind: str, **extra):
        base = {
            "event": "outage",
            "network": "Freifunk München",
            "kind": kind,
            "started": "16:15",
            "nodes_offline": 3,
            "root": "node-a",
            "people_affected": 12,
        }
        return {**base, **extra}

    def test_every_kind_renders_in_every_language(self):
        cases = [
            self.facts("cascade", nodes_behind_root=1),
            self.facts("cascade", nodes_behind_root=2),
            self.facts("shared_neighbor", still_online_above="hub-1"),
            self.facts("shared_neighbor"),
            self.facts("site"),
            self.facts("gateway"),
            self.facts("single", nodes_offline=1, people_affected=0),
            {
                "event": "resolved",
                "network": "N",
                "kind": "cascade",
                "root": "node-a",
                "duration": "1 h 5 min",
                "duration_minutes": 65,
                "nodes_back": 2,
                "nodes_total": 3,
                "still_down": ["node-c"],
                "came_back_on_new_firmware": "v2026.1",
            },
            {"event": "feed_stale", "network": "N", "since": "14:05"},
            {"event": "network_wide", "network": "N", "nodes_offline": 900, "started": "14:05"},
        ]
        for language in ("english", "telugu", "hindi"):
            for facts in cases:
                with self.subTest(language=language, facts=facts):
                    text = templates.render(facts, language)
                    self.assertNotIn("{", text)
                    self.assertTrue(text.startswith("*"))
                    self.assertEqual(check_draft(text, facts, 700), "", "templates obey the writer's own rules")

    def test_no_people_line_when_nobody_was_counted(self):
        text = templates.render(self.facts("single", nodes_offline=1, people_affected=0), "english")
        self.assertNotIn("People", text)

    def test_status_summary(self):
        facts = {
            "network": "Freifunk München",
            "nodes_online": 1969,
            "nodes_total": 2118,
            "people_online": 2410,
            "open_outages": [{"started": "16:15", "root": "node-a", "nodes": 7}],
        }
        for language in ("english", "telugu", "hindi"):
            text = templates.render_status(facts, language)
            self.assertIn("1969", text)
            self.assertIn("node-a", text)
        self.assertIn("No ongoing outages.", templates.render_status({**facts, "open_outages": []}, "english"))

    def test_durations(self):
        self.assertEqual(templates.duration(1, "english"), "1 min")
        self.assertEqual(templates.duration(65, "english"), "1 h 5 min")
        self.assertEqual(templates.duration(120, "telugu"), "2 గంటలు")
        self.assertEqual(templates.duration(61, "hindi"), "1 घंटा 1 मिनट")
        self.assertEqual(templates.duration(0, "english"), "0 min")

    def test_language_detection(self):
        self.assertEqual(templates.detect_language("ఇప్పుడు ఏమి డౌన్ అయింది?"), "telugu")
        self.assertEqual(templates.detect_language("अभी क्या बंद है?"), "hindi")
        self.assertIsNone(templates.detect_language("what is down?"))


if __name__ == "__main__":
    unittest.main()
