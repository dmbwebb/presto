"""Real keyboard list editing in Chromium and WebKit, using disposable slides."""
import os
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WYSIWYG_JS = Path(os.environ.get('PRESTO_WYSIWYG_JS', ROOT / 'presto/wysiwyg.js'))


class ListEditingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browsers = [
            cls.playwright.chromium.launch(headless=True),
            cls.playwright.webkit.launch(headless=True),
        ]

    @classmethod
    def tearDownClass(cls):
        for browser in cls.browsers:
            browser.close()
        cls.playwright.stop()

    def open_page(self, browser, content):
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.set_default_timeout(3000)
        self.addCleanup(page.close)
        page.set_content('<html><head></head><body><div class="ws-deck">'
                         '<section class="slide">' + content + '</section></div></body></html>')
        page.add_style_tag(path=str(ROOT / "engine/webslides.css"))
        page.add_script_tag(path=str(ROOT / "engine/webslides.js"))
        page.evaluate("() => WS.ready")
        page.add_script_tag(path=str(WYSIWYG_JS))
        page.evaluate("""() => {
            window.saved = [];
            __prestoWysiwyg.onMessage = m => {
                if (m.type === 'wysiwygChanged') saved.push(m);
            };
            __prestoWysiwyg.enable();
        }""")
        return page

    def edit_at(self, page, selector, offset=None):
        page.evaluate("""({selector, offset}) => {
            const el = document.querySelector(selector);
            __prestoWysiwyg.beginTextEdit(el);
            const range = document.createRange();
            if (offset === null) {
                range.selectNodeContents(el);
                range.collapse(false);
            } else {
                range.setStart(el.firstChild, offset);
                range.collapse(true);
            }
            const sel = getSelection();
            sel.removeAllRanges();
            sel.addRange(range);
        }""", {"selector": selector, "offset": offset})

    def test_enter_creates_repeated_sibling_bullets_and_saves(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser, '<ul><li id="first">First</li></ul>')
                self.edit_at(page, '#first')
                page.keyboard.press('Enter')
                page.keyboard.type('Second')
                page.keyboard.press('Enter')
                page.keyboard.type('Third')
                page.keyboard.press('Escape')
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['First', 'Second', 'Third'])
                source = page.evaluate('saved.at(-1).html')
                self.assertNotIn('contenteditable', source)
                self.assertNotIn('presto-wysiwyg', source)
                self.assertEqual(source.count('id="first"'), 1)
                page.evaluate('html => document.querySelector(".slide").innerHTML = html', source)
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['First', 'Second', 'Third'])

    def test_enter_splits_formatted_text_and_undo_restores_it(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser, '<ul><li id="first">Before <strong>after</strong></li></ul>')
                self.edit_at(page, '#first', 7)
                page.keyboard.press('Enter')
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['Before ', 'after'])
                self.assertEqual(page.locator('.slide li:nth-child(2) strong').inner_text(), 'after')
                # Undo lives in the native toolbar and Edit menu, which call
                # PrestoEdit.undo() (the in-page Undo button was retired).
                self.assertTrue(page.evaluate('PrestoEdit.undo()'))
                self.assertEqual(page.locator('.slide li').count(), 1)
                self.assertEqual(page.locator('#first').inner_html(), 'Before <strong>after</strong>')

    def test_nested_paragraph_and_numbered_list(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser, '<ul><li>Parent<ol><li style="color: red"><p id="p">Nested</p></li></ol></li></ul>')
                self.edit_at(page, '#p')
                page.keyboard.press('Enter')
                page.keyboard.type('Next')
                page.keyboard.press('Escape')
                self.assertEqual(page.locator('.slide ol > li').all_text_contents(), ['Nested', 'Next'])
                self.assertEqual(page.locator('.slide ol > li').nth(1).evaluate('el => el.style.color'), 'red')

    def test_toolbar_adds_after_selected_bullet_without_splitting(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser, '<ul><li id="first">Keep all this</li><li>Last</li></ul>')
                self.edit_at(page, '#first', 4)
                page.get_by_role('button', name='Add bullet', exact=True).click()
                page.keyboard.type('New')
                page.keyboard.press('Escape')
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['Keep all this', 'New', 'Last'])
                page.evaluate('__prestoWysiwyg.select("#first")')
                page.get_by_role('button', name='Add bullet', exact=True).click()
                page.keyboard.type('Another')
                page.keyboard.press('Escape')
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['Keep all this', 'Another', 'New', 'Last'])

    def test_shift_enter_keeps_one_bullet(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser, '<ul><li id="first">First</li></ul>')
                self.edit_at(page, '#first')
                page.keyboard.press('Shift+Enter')
                page.keyboard.type('continuation')
                page.keyboard.press('Escape')
                self.assertEqual(page.locator('.slide li').count(), 1)
                self.assertIn('<br>', page.locator('#first').inner_html())

    def test_split_replaces_selection_and_keeps_moved_ids(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser, '<ul><li id="first"><strong id="format">Before remove after</strong><span id="keep"> tail</span></li></ul>')
                self.edit_at(page, '#first')
                page.evaluate("""() => {
                    const text = document.querySelector('#format').firstChild;
                    const range = document.createRange();
                    range.setStart(text, 7);
                    range.setEnd(text, 14);
                    getSelection().removeAllRanges();
                    getSelection().addRange(range);
                }""")
                page.keyboard.press('Enter')
                page.keyboard.press('Escape')
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['Before ', 'after tail'])
                self.assertEqual(page.locator('#format').count(), 1)
                self.assertEqual(page.locator('li:nth-child(2) #keep').count(), 1)

    def test_empty_and_start_of_bullet(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser, '<ul><li id="first">Original</li></ul>')
                self.edit_at(page, '#first', 0)
                page.keyboard.press('Enter')
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['', 'Original'])
                self.edit_at(page, '#first')
                page.keyboard.press('Enter')
                page.keyboard.type('New')
                page.keyboard.press('Escape')
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['', 'New', 'Original'])

    def test_reveal_bullet_saves_without_generated_step_or_data_binding(self):
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser, '<ul data-reveal="each"><li id="first" data-stat="n">First</li></ul>')
                page.evaluate('WS.goToSlide(0, 1)')
                self.edit_at(page, '#first')
                page.keyboard.press('Enter')
                page.keyboard.type('Next')
                page.keyboard.press('Escape')
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['First', 'Next'])
                source = page.evaluate('saved.at(-1).html')
                self.assertNotIn('data-ws-auto-step', source)
                self.assertNotIn('data-step=', source)
                self.assertEqual(source.count('data-stat='), 1)

    def test_reveal_list_enter_gives_the_new_bullet_its_own_step(self):
        # BACKLOG 16: the new bullet used to clone its neighbour's generated step.
        for browser in self.browsers:
            with self.subTest(browser=browser.browser_type.name):
                page = self.open_page(browser, '<ul data-reveal="each"><li id="first">First</li><li>Last</li></ul>')
                page.evaluate('WS.goToSlide(0, 1)')
                self.edit_at(page, '#first')
                page.keyboard.press('Enter')
                page.keyboard.type('Middle')
                page.keyboard.press('Escape')
                self.assertEqual(page.locator('.slide li').all_text_contents(), ['First', 'Middle', 'Last'])
                steps = page.evaluate('[...document.querySelectorAll(".slide li")].map(li => li.dataset.step)')
                self.assertEqual(steps, ['1', '2', '3'])
                self.assertEqual(page.evaluate('WS.slideSteps.get(WS.slides[0])'), 3)
                source = page.evaluate('saved.at(-1).html')
                self.assertNotIn('data-step=', source)


if __name__ == '__main__':
    unittest.main()
