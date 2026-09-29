"""Offline regression tests for scripts/parse_kb.py. Reads Phase 1 fixtures only."""
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import parse_kb  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

FIX = ROOT / "data" / "samples" / "html"
IDS = ["4535", "5913", "1027", "508", "233", "8721", "6260", "1866", "352", "430",
       "9079", "4613", "8318"]
STAFF_IDS = {"4535", "508", "233", "6260", "1866", "430", "9079"}
CACHE: dict = {}


def parsed(i):
    if i not in CACHE:
        CACHE[i] = parse_kb.parse_article((FIX / f"{i}.html").read_bytes())
    return CACHE[i]


def wrap(body_html):
    """SYNTHETIC page around a body fragment (not real KB content)."""
    return (
        '<html><head><meta property="og:url" content="https://td.usd.edu/TDClient/33/Portal/KB/Article/1/Synthetic">'
        '<meta property="og:title" content="Synthetic"></head><body>'
        '<div id="ctl00_ctl00_cpContent_cpContent_divBody">' + body_html + "</div>"
        '<div id="ctl00_ctl00_cpContent_cpContent_divKB"><h2>Related Articles (1)</h2>'
        "<a href='/x'>RELATED-ONLY</a></div></body></html>").encode()


class FixtureTests(unittest.TestCase):
    def test_all_samples_parse(self):
        for i in IDS:
            a = parsed(i)
            self.assertTrue(a["fullText"], i)
            self.assertEqual(a["sourceArticleId"], i)
            self.assertIn(f"/Article/{i}/", a["url"])

    def test_related_and_feedback_excluded(self):
        for i in IDS:
            t = parsed(i)["fullText"]
            self.assertNotIn("Related Articles", t, i)
            self.assertNotIn("Related Services", t, i)
        raw = (FIX / "4535.html").read_bytes().decode()
        rel = BeautifulSoup(raw, "html.parser").select_one("#ctl00_ctl00_cpContent_cpContent_divKB")
        body = BeautifulSoup(raw, "html.parser").select_one("#ctl00_ctl00_cpContent_cpContent_divBody")
        body_text = " ".join(body.get_text().split())
        titles = [" ".join(a.get_text().split()) for a in rel.select("a[href*='/KB/Article/']")]
        only_related = [t for t in titles if t and t not in body_text]
        self.assertTrue(only_related)  # titles that exist only in the Related Articles sidebar
        for title in only_related:
            self.assertNotIn(title, parsed("4535")["fullText"])

    def test_feedback_excluded(self):
        for i in IDS:
            t = parsed(i)["fullText"].lower()
            for word in ("was this article helpful", "feedback", "thumbs"):
                self.assertNotIn(word, t, i)

    def test_inline_punctuation_no_false_spaces(self):
        a = parsed("8721")
        blob = a["fullText"] + "\n" + (a["footer"] or "")
        self.assertIn("eAccounts, students, faculty", a["footer"])
        self.assertNotIn("eAccounts ,", blob)
        for i in IDS:
            self.assertNotRegex(parsed(i)["fullText"], r"\w [,.;:] ", i)

    def test_metadata(self):
        a = parsed("4535")
        self.assertEqual(a["category"], "Network & WiFi")
        self.assertEqual(a["tags"], ["wifi", "eduroam"])
        self.assertEqual(a["publishedTime"], "2020-01-22T20:28Z")
        self.assertEqual(a["modifiedTime"], "2026-08-25T19:25Z")
        self.assertEqual(a["title"], "Connecting to Eduroam WiFi")
        self.assertTrue(a["description"].startswith("Connecting to eduroam"))
        for i in ("8721", "1866", "352", "430", "8318"):
            self.assertEqual(parsed(i)["tags"], [], i)
        self.assertEqual(len(parsed("5913")["tags"]), 8)

    def test_issue_environment_cause_resolution(self):
        for i in ("1866", "352", "430", "4613", "8318", "233"):
            a = parsed(i)
            for f in ("issue", "environment", "cause", "resolution"):
                self.assertTrue(a[f], (i, f))
            self.assertIsNone(a["question"])
            self.assertIsNone(a["answer"])
        self.assertEqual([s["heading"] for s in parsed("233")["sections"]],
                         ["Issue/Question", "Environment", "Cause", "Resolution"])
        self.assertIn("Clear the print queue via command prompt", parsed("233")["resolution"])

    def test_missing_cause(self):
        for i in ("1027", "508", "8721", "6260"):
            a = parsed(i)
            self.assertIsNone(a["cause"], i)
            self.assertTrue(a["issue"], i)
            self.assertTrue(a["environment"], i)
        self.assertIsNone(parsed("508")["resolution"])  # heading present, body empty
        self.assertIn("Resolution", [s["heading"] for s in parsed("508")["sections"]])

    def test_combined_heading_maps_to_issue(self):
        self.assertEqual([s["heading"] for s in parsed("1027")["sections"]][0], "Issue/Question")
        self.assertTrue(parsed("1027")["issue"].startswith("I cannot send a message"))
        self.assertIsNone(parsed("1027")["question"])

    def test_products_instructions_template(self):
        for i in ("4535", "5913", "9079"):
            a = parsed(i)
            self.assertTrue(a["question"], i)
            self.assertIsNone(a["answer"], i)
            heads = [s["heading"] for s in a["sections"]]
            self.assertEqual(heads[:4], ["Question", "Products or Services Involved",
                                         "Instructions", "Need More Help?"], i)
            self.assertNotIn("ITS Staff Only", heads)

    def test_staff_only_separate(self):
        for i in IDS:
            a = parsed(i)
            if i in STAFF_IDS:
                self.assertTrue(a["staffOnly"] and a["staffOnly"]["text"], i)
                self.assertTrue(a["staffOnly"]["links"], i)
                self.assertNotIn("staff only", a["fullText"].lower(), i)
                self.assertNotIn("staff only", a["staffOnly"]["text"].lower(), i)
                for s in a["sections"]:
                    self.assertNotIn("staff only", (s["heading"] or "").lower())
                for link in a["staffOnly"]["links"]:
                    self.assertNotIn(link["text"], a["fullText"], (i, link["text"]))
                self.assertNotIn("staff_only_unbounded", a["warnings"], i)
                self.assertNotIn("staff_only_unmatched", a["warnings"], i)
            else:
                self.assertIsNone(a["staffOnly"], i)

    def test_staff_shapes(self):
        self.assertIn("Rapid Identity", parsed("9079")["staffOnly"]["text"])  # panel shape
        self.assertIn("Vlan of the port", parsed("233")["staffOnly"]["text"])  # box, mid-body
        self.assertIn("Shared Account Prompting", parsed("508")["staffOnly"]["text"])  # box in footer

    def test_footer_separate(self):
        self.assertEqual(parsed("233")["footer"],
                         "Please contact the Service Desk if you need further assistance")
        self.assertNotIn("contact the Service Desk", parsed("233")["fullText"])
        self.assertEqual(parsed("233")["warnings"], [])
        self.assertIn("footer_nonstandard", parsed("508")["warnings"])
        self.assertIn("footer_nonstandard", parsed("8721")["warnings"])
        self.assertIsNone(parsed("430")["footer"])
        self.assertNotIn("staff only", parsed("508")["footer"].lower())

    def test_standard_footer_only_adds_nothing(self):
        a = parsed("233")  # footer is only the standard line
        self.assertEqual(a["warnings"], [])
        self.assertTrue(a["fullText"].endswith(a["resolution"].splitlines()[-1]))
        self.assertNotIn("further assistance", a["fullText"])

    def test_8721_footer_content_in_fulltext(self):
        a = parsed("8721")
        self.assertIn("eAccounts, students, faculty", a["fullText"])
        self.assertIn("Add Coyote Cash or Add-on Flex", a["fullText"])
        self.assertIn("eAccounts, students", a["footer"])  # original preserved
        self.assertTrue(a["fullText"].endswith(a["footer"].splitlines()[-1]))  # document order: last
        self.assertNotIn("eAccounts ,", a["fullText"])
        self.assertEqual(a["warnings"], ["footer_nonstandard"])
        self.assertNotIn("Add Coyote Cash or Add-on Flex", a["resolution"])  # footer not in fields

    def test_no_fulltext_contains_standard_line(self):
        for i in IDS:
            for line in parsed(i)["fullText"].splitlines():
                self.assertIsNone(parse_kb.STANDARD_FOOTER_RE.match(parse_kb.norm_key(line)), (i, line))
            self.assertNotIn("if you need further assistance", parsed(i)["fullText"].lower().replace(
                "if you need further assistance, please", ""), i)

    def test_508_footer_kept_and_flagged_staff_out(self):
        a = parsed("508")
        self.assertIn("Please contact the Service Desk with the following information:", a["fullText"])
        self.assertIn("email address you are trying to access", a["fullText"])
        self.assertIn("footer_nonstandard", a["warnings"])
        self.assertIn("footer_unclassified", a["warnings"])
        self.assertNotIn("Shared Account Prompting", a["fullText"])  # staff block inside footer
        self.assertIn("Shared Account Prompting", a["staffOnly"]["text"])

    def test_only_expected_warnings_on_fixtures(self):
        got = {i: parsed(i)["warnings"] for i in IDS if parsed(i)["warnings"]}
        self.assertEqual(got, {"508": ["footer_nonstandard", "footer_unclassified"],
                               "8721": ["footer_nonstandard"]})

    def test_deterministic(self):
        for i in IDS:
            raw = (FIX / f"{i}.html").read_bytes()
            self.assertEqual(parse_kb.dumps(parse_kb.parse_article(raw)),
                             parse_kb.dumps(parse_kb.parse_article(raw)))
        code = ("import sys;sys.path.insert(0,r'%s');import parse_kb;"
                "print(parse_kb.dumps(parse_kb.parse_article(open(r'%s','rb').read())))"
                % (ROOT / "scripts", FIX / "233.html"))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True,
                             env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"}).stdout
        self.assertEqual(out.decode("utf-8").strip(), parse_kb.dumps(parsed("233")))


class SyntheticTests(unittest.TestCase):
    """SYNTHETIC HTML snippets, not real KB content."""

    def test_question_answer_template(self):
        a = parse_kb.parse_article(wrap(
            "<h2>Question</h2><p>How do I X?</p><h2>Answer</h2><p>Do <b>Y</b>, then Z.</p>"))
        self.assertEqual(a["question"], "How do I X?")
        self.assertEqual(a["answer"], "Do Y, then Z.")
        self.assertIsNone(a["issue"])
        self.assertNotIn("RELATED-ONLY", a["fullText"])
        self.assertEqual(a["fullText"], "Question\nHow do I X?\nAnswer\nDo Y, then Z.")

    def test_no_headings_only_fulltext(self):
        a = parse_kb.parse_article(wrap("<p>Just text.</p>"))
        self.assertEqual(a["fullText"], "Just text.")
        for f in ("question", "answer", "issue", "environment", "cause", "resolution"):
            self.assertIsNone(a[f])

    def test_inline_vs_block_spacing(self):
        a = parse_kb.parse_article(wrap(
            '<p>Use <a href="#">eAccounts</a>, students<strong>!</strong> and<span>x</span>y</p>'
            "<p>next</p><ul><li>a</li><li>b</li></ul>line<br>two"))
        self.assertEqual(a["fullText"], "Use eAccounts, students! andxy\nnext\na\nb\nline\ntwo")

    def test_unknown_heading_preserved(self):
        a = parse_kb.parse_article(wrap("<p>pre</p><h2>Weird Heading</h2><p>x</p>"))
        self.assertEqual([s["heading"] for s in a["sections"]], [None, "Weird Heading"])
        self.assertIsNone(a["resolution"])

    def test_repeated_heading_concatenates(self):
        a = parse_kb.parse_article(wrap("<h2>Resolution</h2><p>one</p><h2>Resolution</h2><p>two</p>"))
        self.assertEqual(a["resolution"], "one\ntwo")

    def test_staff_marker_unbounded_warns_and_keeps_body(self):
        a = parse_kb.parse_article(wrap("<h2>Resolution</h2><p>ITS STAFF ONLY</p><p>secret list</p>"))
        self.assertIn("staff_only_unbounded", a["warnings"])
        self.assertIsNone(a["staffOnly"])
        self.assertIn("secret list", a["fullText"])

    def test_staff_text_unmatched_warns(self):
        a = parse_kb.parse_article(wrap("<h2>Resolution</h2><p>Staff Only notes: see below</p>"))
        self.assertIn("staff_only_unmatched", a["warnings"])

    def test_staff_box_bounded(self):
        a = parse_kb.parse_article(wrap(
            '<h2>Resolution</h2><p>Do it.</p><div style="x"><p>ITS Staff Only:</p>'
            '<ol><li><a href="/k">Tech doc</a></li></ol></div><p>after</p>'))
        self.assertEqual(a["staffOnly"],
                         {"text": "Tech doc", "links": [{"text": "Tech doc", "href": "/k"}]})
        self.assertEqual(a["fullText"], "Resolution\nDo it.\nafter")

    STD = "<p>Please contact the Service Desk if you need further assistance</p>"

    def test_footer_only_standard_line(self):
        a = parse_kb.parse_article(wrap("<h2>Resolution</h2><p>Do it.</p><footer>" + self.STD + "</footer>"))
        self.assertEqual(a["fullText"], "Resolution\nDo it.")
        self.assertEqual(a["footer"], "Please contact the Service Desk if you need further assistance")
        self.assertEqual(a["warnings"], [])

    def test_footer_standard_line_removed_anywhere_and_rest_kept_in_order(self):
        a = parse_kb.parse_article(wrap(
            "<h2>Resolution</h2><p>Do it.</p><footer><p>Extra tip.</p>" + self.STD +
            "<p>Another tip.</p></footer>"))
        self.assertEqual(a["fullText"], "Resolution\nDo it.\nExtra tip.\nAnother tip.")
        self.assertEqual(a["resolution"], "Do it.")
        self.assertEqual(a["footer"].splitlines()[1],
                         "Please contact the Service Desk if you need further assistance")
        self.assertEqual(a["warnings"], ["footer_nonstandard"])

    def test_footer_staff_only_stays_in_staff_only(self):
        a = parse_kb.parse_article(wrap(
            "<h2>Resolution</h2><p>Do it.</p><footer>" + self.STD +
            '<div style="x"><p>ITS STAFF ONLY</p><ol><li><a href="/k">Doc</a></li></ol></div></footer>'))
        self.assertEqual(a["fullText"], "Resolution\nDo it.")
        self.assertEqual(a["staffOnly"]["text"], "Doc")
        self.assertNotIn("Doc", a["footer"])
        self.assertEqual(a["warnings"], [])

    def test_footer_unclassifiable_kept_and_warned(self):
        a = parse_kb.parse_article(wrap(
            "<h2>Resolution</h2><p>Do it.</p><footer><p>Call the help desk at extension 5</p></footer>"))
        self.assertIn("Call the help desk at extension 5", a["fullText"])
        self.assertEqual(a["warnings"], ["footer_nonstandard", "footer_unclassified"])

    def test_nested_footer_kept_in_place_and_warned(self):
        a = parse_kb.parse_article(wrap(
            "<h2>Resolution</h2><div><p>Do it.</p><footer><p>Nested note.</p></footer></div>"))
        self.assertIn("Nested note.", a["fullText"])
        self.assertIn("footer_unclassified", a["warnings"])

    def test_zero_width_characters_removed(self):
        zw = "".join(map(chr, (0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)))
        a = parse_kb.parse_article(wrap("<p>ab" + zw + "cd</p><p>" + zw + "</p><p>e" + zw + " f</p>"))
        self.assertEqual(a["fullText"], "abcd\ne f")
        for i in IDS:
            for ch in zw:
                self.assertNotIn(ch, parsed(i)["fullText"])

    def test_missing_body_raises(self):
        with self.assertRaises(parse_kb.ParseError):
            parse_kb.parse_article(b"<html><body>nothing</body></html>")


RAW = ROOT / "data" / "raw" / "articles"  # git-ignored corpus; real-world tests skip if absent


def raw_parsed(i):
    return parse_kb.parse_article((RAW / f"{i}.html").read_bytes())


def panel(title, body="<p>Follow <a href='/k'>Tech doc</a></p>"):
    return (f'<div class="panel panel-default"><div class="panel-heading"><h2 class="h3 panel-title">'
            f'<strong>{title}</strong></h2></div><div class="panel-body">{body}</div></div>')


class StaffFamilySyntheticTests(unittest.TestCase):
    """SYNTHETIC snippets isolating the Internal-Staff-Only panel, empty-box and audience rules."""

    def test_internal_panels_top_level_go_to_staff_only(self):
        for title in ("Internal Staff Only", "Internal ITS Staff Only", "Instructions - Internal Staff Only"):
            a = parse_kb.parse_article(wrap(panel("Question", "<p>How?</p>") + panel(title)))
            self.assertEqual(a["staffOnly"]["links"], [{"text": "Tech doc", "href": "/k"}], title)
            self.assertNotIn("Tech doc", a["fullText"], title)
            self.assertNotIn("Staff Only", a["fullText"], title)
            self.assertEqual([x["heading"] for x in a["sections"]], ["Question"], title)
            self.assertNotIn("staff_only_unmatched", a["warnings"], title)

    def test_nested_internal_panel_not_extracted(self):
        a = parse_kb.parse_article(wrap(panel("Description", panel("Internal Staff Only", "<p>Follow article XYZ</p>"))))
        self.assertIsNone(a["staffOnly"])
        self.assertIn("Follow article XYZ", a["fullText"])
        self.assertIn("staff_only_unmatched", a["warnings"])  # unchanged behavior for docs

    def test_empty_its_boxes(self):
        for extra in ("<p> </p>", ""):
            a = parse_kb.parse_article(wrap(
                '<h2>Resolution</h2><p>Do it.</p><div style="background-color: #F6F6F6">'
                '<p style="text-align:center">ITS STAFF ONLY</p>' + extra + "</div>"))
            self.assertEqual(a["fullText"], "Resolution\nDo it.")
            self.assertEqual(a["warnings"], [])
            self.assertEqual(a["audienceLabels"], [])

    def test_bare_its_label_still_unbounded(self):
        a = parse_kb.parse_article(wrap("<p>ITS STAFF ONLY</p><p>secret list</p>"))
        self.assertIn("staff_only_unbounded", a["warnings"])
        self.assertIn("secret list", a["fullText"])

    def test_audience_label_box(self):
        a = parse_kb.parse_article(wrap(
            '<h2>Resolution</h2><div style="background-color: #F6F6F6"><p>USD STAFF ONLY</p>'
            '<ol><li>Follow <a href="/k">Tech doc</a></li></ol></div>'))
        self.assertEqual(a["audienceLabels"], ["USD STAFF ONLY"])
        self.assertEqual(a["fullText"], "Resolution\nFollow Tech doc")
        self.assertEqual(a["resolution"], "Follow Tech doc")
        self.assertIsNone(a["staffOnly"])
        self.assertEqual(a["warnings"], [])

    def test_audience_label_bare_keeps_following_content(self):
        a = parse_kb.parse_article(wrap(
            "<h2>Resolution</h2><p>HR STAFF ONLY</p><ol><li>step</li></ol><h2>Cause</h2><p>why</p>"))
        self.assertEqual(a["audienceLabels"], ["HR STAFF ONLY"])
        self.assertEqual(a["fullText"], "Resolution\nstep\nCause\nwhy")
        self.assertEqual(a["resolution"], "step")
        self.assertEqual(a["cause"], "why")
        self.assertIsNone(a["staffOnly"])

    def test_audience_labels_order_dedupe_default(self):
        a = parse_kb.parse_article(wrap(
            "<p>USD STAFF ONLY</p><p>a</p><p>EPC STAFF ONLY</p><p>b</p><p>USD STAFF ONLY</p><p>c</p>"))
        self.assertEqual(a["audienceLabels"], ["USD STAFF ONLY", "EPC STAFF ONLY"])
        self.assertEqual(a["fullText"], "a\nb\nc")
        self.assertEqual(parse_kb.parse_article(wrap("<p>plain</p>"))["audienceLabels"], [])

    def test_prose_is_not_an_audience_label(self):
        for txt in ("Available for Faculty/Staff only", "usd staff only", "This is USD STAFF ONLY content",
                    "ABCDEFGHIJK STAFF ONLY", "A STAFF ONLY"):
            a = parse_kb.parse_article(wrap(f"<p>{txt}</p>"))
            self.assertEqual(a["audienceLabels"], [], txt)
            self.assertIn(txt, a["fullText"], txt)
            self.assertIn("staff_only_unmatched", a["warnings"], txt)

    def test_its_staff_box_unchanged_with_audience_field(self):
        a = parse_kb.parse_article(wrap(
            '<h2>Resolution</h2><p>Do it.</p><div style="x"><p>ITS STAFF ONLY</p>'
            '<ol><li><a href="/k">Tech doc</a></li></ol></div>'))
        self.assertEqual(a["staffOnly"]["text"], "Tech doc")
        self.assertEqual(a["audienceLabels"], [])


@unittest.skipUnless(RAW.exists(), "saved corpus data/raw/articles not present")
class RealWorldStaffFamilyTests(unittest.TestCase):
    def need(self, i):
        if not (RAW / f"{i}.html").exists():
            self.skipTest(f"{i}.html not saved")

    def test_family_a_panels(self):
        for i in ("107", "1208", "2369", "3402", "349"):
            self.need(i)
            a = raw_parsed(i)
            self.assertTrue(a["staffOnly"] and a["staffOnly"]["links"], i)
            self.assertNotIn("Internal Staff Only", a["fullText"], i)
            self.assertNotIn("staff_only_unmatched", a["warnings"], i)
            self.assertNotIn("staff_only_unbounded", a["warnings"], i)

    def test_family_a_nested_example_untouched(self):
        self.need("300")
        a = raw_parsed("300")  # example panel is nested inside a panel body
        self.assertIsNone(a["staffOnly"])
        self.assertIn("staff_only_unmatched", a["warnings"])

    def test_289_top_level_example_panel_matches_structural_rule(self):
        # Measured: 289 (KCS style guide) has an example "Internal ITS Staff Only" panel as a
        # direct body child, so the structural rule extracts it. Warning stays (prose mentions).
        self.need("289")
        a = raw_parsed("289")
        self.assertEqual(a["staffOnly"]["text"], "Follow article XYZ")
        self.assertIn("staff_only_unmatched", a["warnings"])

    def test_family_b_and_c_audience_labels(self):
        for i, label in (("8331", "HR STAFF ONLY"), ("8525", "USD STAFF ONLY"), ("9050", "FINANCE STAFF ONLY"),
                         ("9758", "EPC STAFF ONLY"), ("9341", "USD STAFF ONLY")):
            self.need(i)
            a = raw_parsed(i)
            self.assertEqual(a["audienceLabels"], [label], i)
            self.assertNotIn("STAFF ONLY", a["fullText"], i)
            self.assertIsNone(a["staffOnly"], i)
            self.assertNotIn("staff_only_unmatched", a["warnings"], i)
        a = raw_parsed("8331")
        self.assertIn("Resolving Printing to PDF", a["fullText"])
        self.assertIn("Resolving Printing to PDF", a["resolution"])

    def test_10177_resolution_stays_searchable(self):
        self.need("10177")
        a = raw_parsed("10177")
        self.assertEqual(a["audienceLabels"], ["USD STAFF ONLY"])
        self.assertIsNone(a["staffOnly"])
        self.assertIn("USD Data Governance Program", a["fullText"])
        self.assertIn("USD Data Governance Program", a["resolution"])
        self.assertNotIn("staff_only_unmatched", a["warnings"])

    def test_family_d_empty_its_boxes(self):
        for i in ("4201", "9866"):
            self.need(i)
            a = raw_parsed(i)
            self.assertNotIn("STAFF ONLY", a["fullText"], i)
            self.assertNotIn("staff_only_unbounded", a["warnings"], i)
            self.assertNotIn("staff_only_unmatched", a["warnings"], i)

    def test_family_e_unchanged(self):
        for i in ("288", "289", "300", "2506"):
            self.need(i)
            self.assertIn("staff_only_unmatched", raw_parsed(i)["warnings"], i)

    def test_real_its_box_unchanged_and_deterministic(self):
        self.need("1001")
        raw = (RAW / "1001.html").read_bytes()
        a = parse_kb.parse_article(raw)
        self.assertEqual(a["audienceLabels"], [])
        self.assertTrue(a["staffOnly"])
        self.assertNotIn("ITS STAFF ONLY", a["fullText"])
        self.assertEqual(parse_kb.dumps(a), parse_kb.dumps(parse_kb.parse_article(raw)))


if __name__ == "__main__":
    unittest.main()
