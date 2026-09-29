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

    def test_missing_body_raises(self):
        with self.assertRaises(parse_kb.ParseError):
            parse_kb.parse_article(b"<html><body>nothing</body></html>")


if __name__ == "__main__":
    unittest.main()
