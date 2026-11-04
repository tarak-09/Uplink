import contextlib
import io
import json
import re
import tempfile
import unittest
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path

from uplink.cli import main
from uplink.correlate import Situation, correlate
from uplink.feed import read_snapshot
from uplink.incident_page import COLUMN, PAD_X, ROW_LIMIT, render_page
from uplink.incidents import Incident

from .support import FIXTURE, MeshBuilder, make_settings

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class TagBalance(HTMLParser):
    """Records every closing tag that does not close the element opened last."""

    def __init__(self):
        super().__init__()
        self.open = []
        self.errors = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.open.append(tag)

    def handle_startendtag(self, tag, attrs):
        pass  # <circle .../> closes itself

    def handle_endtag(self, tag):
        if self.open and self.open[-1] == tag:
            self.open.pop()
        else:
            self.errors.append(f"</{tag}> while <{self.open[-1] if self.open else 'nothing'}> is open")


def outages_in(snapshot, settings, hours=24):
    """The incidents and the numbered outages, the way `uplink analyze` finds them."""
    down = snapshot.down_since(timedelta(seconds=settings.stale_after))
    recent = [node_id for node_id, at in down.items() if snapshot.taken_at - at <= timedelta(hours=hours)]
    outages = correlate(recent, Situation(snapshot.nodes, down, snapshot.links))
    incidents = [Incident.from_outage(o, snapshot.nodes, detected_at=snapshot.taken_at) for o in outages]
    notable = sorted(
        (i for i in incidents if i.size > 1 or i.kind == "gateway"),
        key=lambda i: (-i.size, -i.started_at.timestamp()),
    )
    return incidents, notable


def render(snapshot, folder, **overrides):
    settings = make_settings(Path(folder), **overrides)
    incidents, notable = outages_in(snapshot, settings)
    return render_page(snapshot, settings, incidents, notable, 24)


def sections(page):
    return re.findall(r'<article class="outage".*?</article>', page, flags=re.S)


def timeline(page):
    return re.search(r'<figure class="timeline">.*?</figure>', page, flags=re.S).group()


def boxes(section):
    """Name and status line of every node drawn in one outage."""
    names = re.findall(r'<text class="name"[^>]*>(.*?)</text>', section)
    states = re.findall(r'<text class="state"[^>]*>(.*?)</text>', section)
    return dict(zip(names, states, strict=True))


def assert_well_formed(test, page):
    checker = TagBalance()
    checker.feed(page)
    checker.close()
    test.assertEqual(checker.errors, [])
    test.assertEqual(checker.open, [])


class RealSnapshotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as folder:
            snapshot = read_snapshot(FIXTURE)
            cls.page = render(snapshot, folder)
            cls.anonymized = render(snapshot, folder, anonymize=True)
        cls.outages = sections(cls.page)

    def test_the_summary_counts_the_noise_uplink_removes(self):
        self.assertIn("<h1>79 routers went quiet in the last 24 hours.</h1>", self.page)
        self.assertIn("22 of them failed together, in 6 outages with one likely cause each.", self.page)
        self.assertIn("6 alerts instead of 79, 92% fewer.", self.page)
        self.assertIn("The other 57 went quiet alone", self.page)

    def test_the_timeline_has_a_dot_for_every_node_that_went_quiet(self):
        strip = timeline(self.page)
        legend = 1
        self.assertEqual(strip.count('class="together"'), 22 + legend)
        self.assertEqual(strip.count('class="alone"'), 57 + legend)
        numbers = re.findall(r'class="stack-label"[^>]*>(\d+)<', strip)
        self.assertEqual(sorted(numbers, key=int), ["1", "2", "3", "4", "5", "6"])

    def test_one_section_and_one_picture_per_outage_numbered_as_analyze_prints_them(self):
        self.assertEqual(len(self.outages), 6)
        headlines = [re.search(r"Outage \d+: </span>(.*?)</h2>", section).group(1) for section in self.outages]
        self.assertEqual(headlines[0], "site-bcg5 is down, and the 6 nodes behind it went offline too")
        self.assertEqual(headlines[1], "6 nodes at one location went offline together")
        for number, section in enumerate(self.outages, 1):
            self.assertIn(f'id="outage-{number}"', section)
            self.assertEqual(section.count("<svg"), 1)

    def test_the_cascade_shows_what_stayed_up_and_when_each_node_went_quiet(self):
        cascade = self.outages[0]
        self.assertIn("Cascade from 22/10 16:15. 7 nodes went quiet within 15 seconds.", cascade)
        drawn = boxes(cascade)
        self.assertEqual(len(drawn), 8)
        self.assertEqual(drawn["site-bkl"], "still online")
        self.assertEqual(drawn["site-bca8"], "offline 16:15:35", "the first to go quiet shows the time")
        self.assertEqual(drawn["site-bcg5"], "offline +1 s")
        self.assertEqual(drawn["site-bca7"], "offline +15 s")
        self.assertEqual(cascade.count('class="node down hub"'), 1, "the others reached the internet through it")
        self.assertEqual(cascade.count('class="cut"'), 1, "the lost connection between site-bkl and site-bcg5")
        self.assertIn(">check first</text>", cascade)
        self.assertIn("Power and cabling at site-bcg5, then its mesh link to site-bkl", cascade)

    def test_a_site_outage_shows_the_gateways_that_stayed_up(self):
        site = self.outages[1]
        drawn = boxes(site)
        gateways = sorted(name for name in drawn if name.startswith("gw"))
        self.assertEqual(gateways, ["gw04.ext.ffmuc.net", "gw06.ext.ffmuc.net", "gw07.ext.ffmuc.net"])
        self.assertTrue(all(drawn[name] == "gateway, still online" for name in gateways))
        self.assertEqual(site.count('class="cut"'), 6)
        self.assertIn("check first: this location", site)
        self.assertNotIn(" hub", site)

    def test_a_shared_neighbour_outage_points_at_the_neighbour(self):
        shared = self.outages[3]
        self.assertIn("Shared neighbour from 04:17. 2 nodes went quiet within 2 seconds.", shared)
        self.assertEqual(boxes(shared)["site-bml"], "still online")
        self.assertIn("The radio and antenna at site-bml", shared)

    def test_the_page_stands_alone(self):
        for outside in ("http://", "https://", "<script", "src=", "href=", "url(", "@import"):
            self.assertNotIn(outside, self.page)

    def test_the_page_is_well_formed(self):
        assert_well_formed(self, self.page)

    def test_anonymized_pages_hide_router_names_but_keep_gateways(self):
        self.assertNotIn("site-", self.anonymized)
        self.assertIn("node d477b2", self.anonymized)
        self.assertIn("gw07.ext.ffmuc.net", self.anonymized)
        self.assertIn("Node names are replaced with short ids", self.anonymized)
        self.assertNotIn("replaced with short ids", self.page)


class BuiltMeshTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)

    def render(self, mesh):
        return render(mesh.snapshot(), self.folder.name)

    def test_names_cannot_break_out_of_the_page(self):
        mesh = (
            MeshBuilder()
            .gateway("gw")
            .node("hub", via="gw", name='<script>alert("hi")</script>')
            .node("leaf", via="hub", name='Café & "Bar" <b>')
        )
        page = self.render(mesh.tick().drop("hub", "leaf", ago=40))
        self.assertNotIn("<script", page)
        self.assertNotIn("<b>", page)
        self.assertIn("&lt;script&gt;alert(&quot;hi&quot;)&lt;/script&gt; is down", page)
        self.assertIn("Café &amp; &quot;Bar&quot; &lt;b&gt;", page)
        assert_well_formed(self, page)

    def test_a_quiet_network_says_so(self):
        page = self.render(MeshBuilder().gateway("gw").node("a", via="gw"))
        self.assertIn("<h1>No routers went quiet in the last 24 hours.</h1>", page)
        self.assertNotIn("<article", page)
        self.assertNotIn('<figure class="timeline">', page)
        assert_well_formed(self, page)

    def test_nodes_that_went_quiet_alone_get_no_alert(self):
        mesh = MeshBuilder().gateway("gw").node("a", via="gw").node("b", via="gw")
        page = self.render(mesh.tick().drop("a", ago=3600).drop("b", ago=40))
        self.assertIn("<h1>2 routers went quiet in the last 24 hours.</h1>", page)
        self.assertIn("so Uplink sends no alert", page)
        self.assertNotIn("<article", page)
        self.assertEqual(timeline(page).count('class="alone"'), 2 + 1)

    def test_a_big_outage_is_drawn_in_part_and_says_so(self):
        mesh = MeshBuilder().gateway("gw").node("hub", via="gw")
        leaves = [f"n{i:02d}" for i in range(30)]
        for leaf in leaves:
            mesh.node(leaf, via="hub")
        page = self.render(mesh.tick().drop("hub", *leaves, ago=40))
        (outage,) = sections(page)
        self.assertIn("31 nodes went quiet in the same second.", outage)
        self.assertIn(f"Showing {1 + ROW_LIMIT} of 31 nodes.", outage)
        width = float(re.search(r'<svg viewBox="0 0 ([\d.]+) ', outage).group(1))
        self.assertLessEqual(width, 2 * PAD_X + ROW_LIMIT * COLUMN)
        self.assertIn(">+19</text>", timeline(page), "the timeline stack stops at 12 dots and counts the rest")

    def test_a_long_chain_is_cut_after_four_rows(self):
        mesh = MeshBuilder().gateway("gw")
        upstream = "gw"
        for name in "abcdef":
            mesh.node(name, via=upstream)
            upstream = name
        (outage,) = sections(self.render(mesh.tick().drop(*"abcdef", ago=40)))
        self.assertIn("Showing 4 of 6 nodes.", outage)
        self.assertEqual(len(boxes(outage)), 4 + 1, "four failed nodes and the gateway above them")


class CommandTest(unittest.TestCase):
    def test_analyze_writes_the_page_and_keeps_json_output_clean(self):
        with tempfile.TemporaryDirectory() as folder:
            env = Path(folder, ".env")
            env.write_text(f"DATA_DIR={Path(folder, 'data')}\nFEED_URL=http://127.0.0.1:9/m.json\n", encoding="utf-8")
            target = Path(folder, "outages.html")
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = main(
                    ["analyze", "--env-file", str(env), "--file", str(FIXTURE), "--json", "--html", str(target)]
                )
            page = target.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertEqual(len(json.loads(out.getvalue())), 6)
        self.assertIn(f"Wrote {target}", err.getvalue())
        self.assertEqual(len(sections(page)), 6)


if __name__ == "__main__":
    unittest.main()
