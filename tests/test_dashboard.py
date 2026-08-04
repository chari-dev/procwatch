import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(os.path.dirname(HERE), "procwatch", "static", "index.html")


def _script():
    with open(PAGE) as handle:
        return re.findall(r"<script>(.*?)</script>", handle.read(), re.S)[-1]


class TestDashboardLoads(unittest.TestCase):
    """The page has to survive being loaded.

    Every JavaScript fault so far has had the same symptom -- the whole script
    aborts on the line that fails, nothing after it ever runs, and the reader
    sees "Loading" forever with no error anywhere. A missing element, a
    variable used above its definition, a reference left behind by a rewrite:
    all three shipped, and all three would have failed here.
    """

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_script_runs_to_completion(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(_script())
            path = handle.name
        try:
            result = subprocess.run(
                ["node", os.path.join(HERE, "harness.mjs"), path],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0,
                             "the dashboard threw while loading:\n" + result.stdout)
        finally:
            os.unlink(path)

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_script_parses(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(_script())
            path = handle.name
        try:
            result = subprocess.run(["node", "--check", path],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.unlink(path)


class TestExplanationPopover(unittest.TestCase):
    """The explanation hangs under the icon that asked for it.

    It began as a centred dialog with a dimmed backdrop, which was wrong in a
    specific way: the number that prompted the question was underneath it, so
    the answer had to be dismissed before the thing it was about could be seen
    again. Anchored to the icon, the table stays visible and stays updating.
    """

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_pressing_the_icon_opens_it_under_the_icon(self):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(_script())
            path = handle.name
        try:
            result = subprocess.run(
                ["node", os.path.join(HERE, "harness_pop.mjs"), path],
                capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        finally:
            os.unlink(path)

    def test_the_markup_declares_the_state_the_script_assumes(self):
        # The interaction harness starts these elements in the state a browser
        # would have produced from the markup. If the markup stops declaring it,
        # the harness would be testing a page that no longer exists -- so the
        # markup is asserted here rather than trusted there.
        with open(PAGE) as handle:
            page = handle.read()
        for fragment in ('<div id="whypanel" hidden>',
                         '<div id="eventsbody" hidden>',
                         '<div id="whatpop" hidden'):
            self.assertIn(fragment, page)
        self.assertRegex(page, r'id="whytoggle"[^>]*aria-expanded="false"')

    def test_the_verdict_lives_in_the_toolbar_not_in_a_card(self):
        with open(PAGE) as handle:
            page = handle.read()
        self.assertNotIn('id="whycard"', page)
        # Inside the toolbar, beside the search box and the menu.
        self.assertLess(page.index('id="whytoggle"'), page.index("</header>"))
        self.assertLess(page.index('id="q"'), page.index('id="whytoggle"'))

    def test_section_headings_are_not_set_in_shouty_capitals(self):
        # A one-word column label -- CPU, SWAP, PORTS -- reads fine in tracked
        # capitals. A phrase does not: "WHAT TO DO" is slower to read than
        # "What to do" and louder than the sentence it introduces. The rules
        # that style phrase headings are asserted here because the shouting
        # crept back in once already, from copying a nearby rule.
        with open(PAGE) as handle:
            page = handle.read()
        for selector in (".whatpart h3", ".evpart h3", ".powergrid h3"):
            match = re.search(re.escape(selector) + r"\{([^}]*)\}", page)
            self.assertIsNotNone(match, selector + " has no rule")
            rule = match.group(1)
            self.assertNotIn("text-transform:uppercase", rule, selector)
            self.assertIn("letter-spacing:0", rule, selector)

    def test_the_year_grid_stretches_to_the_card(self):
        # It was drawn at a fixed 11px per cell, so on a wide card it stopped
        # two thirds of the way across and looked like a rendering fault. The
        # columns are fractions of the width, the count is handed to the grid,
        # and the cells stay square by aspect ratio rather than by a pixel size.
        with open(PAGE) as handle:
            page = handle.read()
        rule = re.search(r"\.yeargrid\{([^}]*)\}", page)
        self.assertIsNotNone(rule, "the year grid has no rule")
        # minmax(0,...) rather than a plain 1fr: a 1fr column cannot shrink
        # below the width of its content, so the twelve columns carrying a month
        # name came out three times wider than the rest and the grid looked like
        # a broken picket fence.
        self.assertIn("repeat(var(--weeks),minmax(0,1fr))", rule.group(1))
        self.assertIn('style="--weeks:', page)
        cell = re.search(r"\.yeargrid i\{([^}]*)\}", page)
        self.assertIsNotNone(cell)
        self.assertIn("aspect-ratio:1", cell.group(1))
        self.assertIn("width:100%", cell.group(1))

    def test_a_selected_row_outranks_the_zebra_stripe(self):
        """Four rules, one specificity, decided by source order.

        `table.rows tr.pinned`, `tr.lit`, `tr:hover` and the even-row stripe are
        all (0,2,2) -- two elements and two classes-or-pseudo-classes -- so the
        last one in the file wins. Selection was declared four hundred lines
        before the stripe, so an even-numbered row kept its stripe when clicked
        and the selection showed as a 2px edge and nothing else. It read as the
        click doing nothing.

        Asserted by position, because that is the whole mechanism: the rules can
        be correct and still lose.
        """
        with open(PAGE) as handle:
            page = handle.read()
        stripe = page.index("table.rows tr:nth-child(even")
        hover = page.index("table.rows tr:hover{")
        lit = page.index("table.rows tr.lit{")
        pinned = page.index("table.rows tr.pinned,")
        self.assertGreater(lit, stripe, "the lit row loses to the stripe")
        self.assertGreater(pinned, stripe, "the selected row loses to the stripe")
        self.assertGreater(pinned, hover, "the selected row loses to hover")
        # And it must not simply be the hover colour, or a selected row and a
        # row the pointer happens to be over look identical.
        rule = re.search(r"table\.rows tr\.pinned,[^}]*\}", page).group(0)
        # The background specifically, not the rule as a whole -- the accent
        # also appears in the edge, so looking anywhere in the block passes even
        # when the fill has been set back to the hover colour.
        fill = re.search(r"background:([^;}]+)", rule).group(1).strip()
        self.assertNotEqual(fill, "var(--raise)",
                            "a selected row looks the same as a hovered one")
        self.assertIn("--s1", fill)
        # And the edge, which is what survives when a row is scrolled to where
        # the background is hard to compare against its neighbours.
        self.assertRegex(rule, r"box-shadow:inset [^;}]*--s1")

    def test_the_toggles_are_big_enough_to_hit_and_to_read(self):
        """A control smaller than the text beside it reads as punctuation.

        The disclosure was an 11px glyph in a 26px box -- a target you could
        reach and a mark you could not see -- and the round inline buttons were
        10px marks in 16px circles. Asserted as numbers because "looks small" is
        exactly the kind of regression that arrives one careless edit at a time.
        """
        with open(PAGE) as handle:
            page = handle.read()

        def rule(selector):
            found = re.search(re.escape(selector) + r"\{([^}]*)\}", page)
            self.assertIsNotNone(found, selector + " has no rule")
            return found.group(1)

        def px(text, prop):
            found = re.search(prop + r":([\d.]+)px", text)
            self.assertIsNotNone(found, prop + " missing from " + text[:60])
            return float(found.group(1))

        disc = rule(".disc")
        self.assertGreaterEqual(px(disc, "width"), 28)
        self.assertGreaterEqual(px(disc, "font-size"), 14)
        for selector in (".whatq", ".info"):
            box = rule(selector)
            self.assertGreaterEqual(px(box, "width"), 18, selector)
            self.assertGreaterEqual(px(box, "font-size"), 11.5, selector)
        self.assertGreaterEqual(px(rule(".alertq .chev"), "font-size"), 12)

    def test_one_disclosure_glyph_everywhere(self):
        # Five places drew the small triangles and two the solid ones, so the
        # same control looked like two different controls depending on the panel
        # it was in.
        with open(PAGE) as handle:
            page = handle.read()
        self.assertNotIn("\\u25b8", page)
        self.assertNotIn("\\u25be", page)
        self.assertIn("\\u25b6", page)
        self.assertIn("\\u25bc", page)

    def test_every_landing_page_screenshot_gets_the_wide_column(self):
        """`order` moves an item between cells; it does not move the track.

        The spreads alternate, and the flipped ones set lg:order-1 on the image
        -- which places it in the FIRST grid column. With the tracks written
        0.78fr 1.22fr for every row, that put half the screenshots in the narrow
        column and made them visibly smaller than their neighbours. A flipped row
        needs its tracks mirrored too.
        """
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "docs", "index.html")) as handle:
            page = handle.read()
        spreads = re.findall(r"<!-- Feature: [^-]*?-->(.*?)</section>", page, re.S)
        self.assertGreater(len(spreads), 8)
        checked = 0
        for body in spreads:
            grid = re.search(r"lg:grid-cols-\[([\d.]+)fr_([\d.]+)fr\]", body)
            if not grid:
                continue          # the one section that carries no screenshot
            first, second = float(grid.group(1)), float(grid.group(2))
            image_first = "lg:order-1" in body
            image = first if image_first else second
            text = second if image_first else first
            self.assertGreater(image, text,
                               "a screenshot is in the narrow column")
            checked += 1
        self.assertGreaterEqual(checked, 12)

    def test_every_in_page_link_lands_somewhere(self):
        """A dead anchor is silent.

        Regenerating the feature spreads dropped id="features" off the first
        one, so the hero button and the header link both pointed at nothing. The
        browser does nothing at all in that case and the reader assumes they
        misclicked.
        """
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "docs", "index.html")) as handle:
            page = handle.read()
        targets = set(re.findall(r'\bid="([^"]+)"', page))
        wanted = {h for h in re.findall(r'href="#([^"]+)"', page) if h}
        self.assertTrue(wanted, "the page has no in-page links at all")
        self.assertEqual(sorted(wanted - targets), [])

    def test_no_javascript_escape_is_written_into_the_markup(self):
        """`\\u25a6` in HTML is four characters of text, not a glyph.

        It shipped on the disk-space button and read as literal backslash-u.
        Easy to write by accident: the same string is correct inside the
        JavaScript a few lines below, where most of this file's text lives.
        """
        with open(PAGE) as handle:
            page = handle.read()
        # Only the markup: the script legitimately contains these escapes.
        body = page[:page.index("<script>")] + page[page.rindex("</script>"):]
        self.assertNotRegex(body, r"\\u[0-9a-fA-F]{4}")

    def test_the_landing_page_uses_no_em_dashes(self):
        # A house style, asked for directly. Easier to keep than to notice: an
        # em dash is what a rewritten sentence reaches for by default.
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "docs", "index.html")) as handle:
            page = handle.read()
        self.assertNotIn("\u2014", page)

    def test_it_is_not_a_modal_with_a_backdrop(self):
        with open(PAGE) as handle:
            page = handle.read()
        # A .sheet is the full-screen dimmed layer the settings dialog uses.
        # The explanation must not be one of those.
        self.assertNotIn('class="sheet" id="whatpop"', page)
        self.assertIn("#whatpop{position:fixed", page)


if __name__ == "__main__":
    unittest.main()
