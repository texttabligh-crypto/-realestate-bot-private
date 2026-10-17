"""«📄 دانلود PDF فایل‌ها» takes a typed price range straight from its menu.

The owner opened the PDF menu, typed «۹ تا ۱۱ میلیارد» and got «از دکمه‌های
پنل مدیریت استفاده کنید»: a typed range only worked after «✍️ بازه دلخواه».
"""
import ast
import asyncio
import re
import types
import unittest
from pathlib import Path

APP = Path(__file__).parent / "app"
NAMES = {"fa_num", "norm_digits", "_fa_billion_value", "_parse_pdf_price_range", "_pdf_price_label",
         "handle_aux_text"}


def load_final_features(**extra):
    source = (APP / "final_features.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [n for n in tree.body if getattr(n, "name", None) in NAMES]
    namespace = {"re": re, **extra}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "final_features.py", "exec"), namespace)
    return namespace


class Message:
    def __init__(self, text):
        self.text, self.replies = text, []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class PdfPriceRangeTest(unittest.TestCase):
    def setUp(self):
        self.sent = []

        async def send_pdf(update, context, session, min_billion=None, max_billion=None, label=""):
            self.sent.append((min_billion, max_billion, label))

        self.ns = load_final_features(_send_property_pdf=send_pdf, _has_perm=lambda session, key: True)

    def test_parse(self):
        parse = self.ns["_parse_pdf_price_range"]
        self.assertEqual(parse("۹ تا ۱۱ میلیارد"), (9, 11))
        self.assertEqual(parse("۱۲ ۱۸"), (12, 18))
        self.assertEqual(parse("۱۲٫۵ تا ۱۸"), (12.5, 18))
        self.assertEqual(parse("۱۱ تا ۹"), (9, 11))
        self.assertEqual(parse("زیر ۸ میلیارد"), (None, 8))
        self.assertEqual(parse("تا ۸"), (None, 8))
        self.assertEqual(parse("بالای ۲۰"), (20, None))
        for text in ("سلام", "۱۰", "۳۵۸۲۹", "۰ ۰", ""):
            self.assertIsNone(parse(text), text)

    def test_label(self):
        label = self.ns["_pdf_price_label"]
        self.assertEqual(label(9, 11), "۹ تا ۱۱ میلیارد تومان")
        self.assertEqual(label(None, 8), "تا ۸ میلیارد تومان")
        self.assertEqual(label(20.5, None), "بالای ۲۰.۵ میلیارد تومان")

    def type_text(self, text, mode):
        context = types.SimpleNamespace(user_data={"pdf_price_custom": mode})
        message = Message(text)
        handled = asyncio.run(self.ns["handle_aux_text"](types.SimpleNamespace(message=message), context, (1,)))
        return handled, context.user_data, message.replies

    def test_a_range_typed_at_the_menu_makes_the_pdf(self):
        handled, state, replies = self.type_text("۹ تا ۱۱ میلیارد", "menu")
        self.assertTrue(handled)
        self.assertEqual(self.sent, [(9, 11, "۹ تا ۱۱ میلیارد تومان")])
        self.assertEqual((state, replies), ({}, []))

    def test_other_text_at_the_menu_is_left_for_other_features(self):
        handled, state, replies = self.type_text("سلام", "menu")
        self.assertFalse(handled)
        self.assertEqual((self.sent, state, replies), ([], {}, []))

    def test_after_custom_range_a_bad_range_is_explained(self):
        handled, state, replies = self.type_text("سلام", True)
        self.assertTrue(handled)
        self.assertEqual(state, {"pdf_price_custom": True})
        self.assertIn("بازه قابل تشخیص نیست", replies[0])
        self.assertTrue(self.type_text("بالای ۲۰", True)[0])
        self.assertEqual(self.sent, [(20, None, "بالای ۲۰ میلیارد تومان")])

    def test_wiring(self):
        source = (APP / "final_features.py").read_text(encoding="utf-8")
        menu = source[source.index("    if a=='export_properties_pdf':"):]
        self.assertLess(menu.index("context.user_data['pdf_price_custom'] = 'menu'"),
                        menu.index("reply_markup=_pdf_price_menu()"))
        self.assertIn("یا بنویسید؛ مثلاً «۹ تا ۱۱»، «زیر ۸» یا «بالای ۲۰».", menu)
        # Any other button closes the menu, so a later price is not taken for a PDF.
        bot = (APP / "bot.py").read_text(encoding="utf-8")
        handler = bot[bot.index("async def button_handler("):]
        self.assertIn('if not action.startswith("export_properties_pdf"):\n'
                      '        context.user_data.pop("pdf_price_custom", None)', handler)
        self.assertLess(handler.index('context.user_data.pop("pdf_price_custom", None)'),
                        handler.index("await handle_final_button(update, context, session)"))


if __name__ == "__main__":
    unittest.main()
