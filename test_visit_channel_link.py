"""Visit notifications link to the listing's post in our own channel.

Advisors in the visits group only saw the source post button, so they could
not tell which post in our channel the customer was asking about.
"""
import ast
import sys
import unittest
from pathlib import Path

APP = Path(__file__).parent / "app"
sys.path.insert(0, str(APP))

from phone_utils import normalize_iran_phone  # noqa: E402


def _publisher_function(name):
    tree = ast.parse((APP / "publisher.py").read_text(encoding="utf-8"))
    node = next(n for n in tree.body if getattr(n, "name", None) == name)
    ns = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "publisher.py", "exec"), ns)
    return ns[name]


build_channel_post_url = _publisher_function("build_channel_post_url")

NAMES = {
    "CHANNEL_POST_BUTTON_TEXT",
    "_our_channel_post_url",
    "_customer_name",
    "_format_price",
    "_build_operator_lead_text",
    "_build_operator_source_keyboard",
}


class Button:
    def __init__(self, text, url=None, callback_data=None):
        self.text, self.url, self.callback_data = text, url, callback_data


class Markup:
    def __init__(self, rows):
        self.rows = rows


def load(source_context):
    tree = ast.parse((APP / "visit_flow.py").read_text(encoding="utf-8"))
    nodes = [
        n for n in tree.body
        if getattr(n, "name", None) in NAMES
        or (isinstance(n, ast.Assign) and getattr(n.targets[0], "id", None) in NAMES)
    ]
    ns = {
        "build_channel_post_url": build_channel_post_url,
        "normalize_iran_phone": normalize_iran_phone,
        "get_operator_source_context": lambda property_id: source_context,
        "InlineKeyboardButton": Button,
        "InlineKeyboardMarkup": Markup,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "visit_flow.py", "exec"), ns)
    return ns


# get_visitable_property columns: ..., channel_post_url [9], published_message_id [10]
LIVE = (295, "27310", None, None, None, None, None, None, None, None, 1282)
DESTINATION = (-1002222, "کانال ما", "nimatmelk")
DETAIL = (81, None, 295, "27310", None, None, 7, None, "مشتری", None, None,
          "ولیعصر جنوبی", None, 81, 2, "5 از 4", 10.5, None)


class ChannelLinkTest(unittest.TestCase):
    def test_link_built_from_our_channel(self):
        ns = load(None)
        self.assertEqual(ns["_our_channel_post_url"](LIVE, DESTINATION), "https://t.me/nimatmelk/1282")
        stored = LIVE[:9] + ("https://t.me/nimatmelk/1282",) + LIVE[10:]
        self.assertEqual(ns["_our_channel_post_url"](stored, None), "https://t.me/nimatmelk/1282")
        self.assertIsNone(ns["_our_channel_post_url"](None, DESTINATION))
        self.assertIsNone(ns["_our_channel_post_url"](LIVE[:10] + (None,), DESTINATION))

    def test_lead_text_shows_link_under_unit_code(self):
        ns = load(None)
        text = ns["_build_operator_lead_text"](DETAIL, "https://t.me/nimatmelk/1282")
        lines = text.split("\n")
        code_line = lines.index("🔢 کد واحد: 27310")
        self.assertEqual(lines[code_line + 1], "📢 فایل در کانال ما: https://t.me/nimatmelk/1282")
        self.assertNotIn("کانال ما", ns["_build_operator_lead_text"](DETAIL))

    def test_keyboard_has_channel_button_first(self):
        ns = load({"direct_post_url": "https://t.me/src/7102", "source_is_active": False})
        rows = ns["_build_operator_source_keyboard"](295, "https://t.me/nimatmelk/1282").rows
        self.assertEqual([(b.text, b.url) for b in rows[0]],
                         [("📢 مشاهده فایل در کانال ما", "https://t.me/nimatmelk/1282")])
        self.assertEqual(rows[1][0].callback_data, "visitsrc_295")
        self.assertEqual(len(ns["_build_operator_source_keyboard"](295).rows), 1)


if __name__ == "__main__":
    unittest.main()
