import unittest

from uplink.report import build_report, format_report

EVENTS = [
    {"event": "poll", "status": "baseline", "taken_at": "2026-11-02T12:00:00+01:00", "nodes": 2118, "reachable": 1960},
    {"event": "poll", "status": "ok", "taken_at": "2026-11-02T12:01:00+01:00", "nodes": 2118, "reachable": 1957},
    {"event": "poll_failed", "error": "timeout"},
    {"event": "incident_opened", "id": "i1", "kind": "cascade", "size": 3, "delay_s": 200},
    {"event": "incident_grew", "id": "i1", "kind": "cascade", "size": 7, "detail": "+4"},
    {"event": "incident_opened", "id": "i2", "kind": "single", "size": 1, "delay_s": 220},
    {"event": "incident_opened", "id": "i3", "kind": "single", "size": 1, "delay_s": 240},
    {"event": "incident_merged", "id": "i3", "kind": "single", "size": 1, "detail": "i2"},
    {"event": "incident_suppressed", "id": "i2", "kind": "site", "size": 2, "detail": "routine"},
    {"event": "alert_sent", "id": "i1", "notice": "alert", "source": "muse", "delay_s": 260},
    {"event": "alert_sent", "id": "i4", "notice": "alert", "source": "template", "delay_s": 300},
    {"event": "alert_sent", "id": "i1", "notice": "resolved", "source": "muse"},
    {"event": "alert_failed", "id": "i5", "notice": "alert", "error": "x"},
    {"event": "incident_resolved", "id": "i1", "kind": "cascade", "size": 7, "minutes": 42},
    {"event": "reply_sent", "kind": "audio"},
    {"event": "reply_sent", "kind": "text"},
    {"event": "reply_sent", "kind": "text"},
    {"event": "poll", "status": "ok", "taken_at": "2026-11-02T14:00:00+01:00", "nodes": 2118, "reachable": 1961},
]


class ReportTest(unittest.TestCase):
    def test_numbers(self):
        report = build_report(EVENTS)
        self.assertEqual(report["watching_hours"], 2.0)
        self.assertEqual(report["polls"], {"baseline": 1, "ok": 2, "failed": 1})
        self.assertEqual(report["feed_availability_pct"], 75.0)
        self.assertEqual(report["network"], {"reachable": 1961, "nodes": 2118})
        incidents = report["incidents"]
        self.assertEqual((incidents["total"], incidents["with_several_nodes"], incidents["largest"]), (2, 2, 7))
        self.assertEqual(incidents["by_kind"], {"cascade": 1, "site": 1})
        self.assertEqual(
            (incidents["resolved"], incidents["median_minutes"], incidents["median_seconds_to_detect"]), (1, 42, 220)
        )
        alerts = report["alerts"]
        self.assertEqual((alerts["sent"], alerts["recoveries_sent"], alerts["failed"]), (2, 1, 1))
        self.assertEqual(alerts["written_by"], {"muse": 1, "template": 1})
        self.assertEqual(alerts["median_seconds_from_failure"], 280)
        self.assertEqual(alerts["held_back"], {"routine": 1})
        self.assertEqual(report["replies"], {"text": 2, "audio": 1})

    def test_text_rendering(self):
        text = format_report(build_report(EVENTS))
        self.assertIn("2 (2 involving several nodes, largest 7)", text)
        self.assertIn("written by muse 1, template 1", text)

    def test_an_empty_log(self):
        report = build_report([])
        self.assertEqual(report["incidents"]["total"], 0)
        self.assertIsNone(report["feed_availability_pct"])
        self.assertIn("none", format_report(report))


if __name__ == "__main__":
    unittest.main()
