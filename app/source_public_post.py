"""
A source post as its public t.me embed page shows it.

The bot hears edits only from channels where it is an admin; for the others
a changed price or «فروش رفت» shows only on the post's public page, which
the source watcher fetches anyway to see whether the post still exists.
"""

from html.parser import HTMLParser


class _PostText(HTMLParser):
    """Collects the post's own text block (not a replied-to post's)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.done = False
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if self.depth:
            if tag == "br":
                self.parts.append("\n")
            elif tag == "div":
                self.depth += 1
            return
        classes = (dict(attrs).get("class") or "").split()
        if tag == "div" and "js-message_text" in classes:
            self.depth = 1

    def handle_endtag(self, tag):
        if self.depth and tag == "div":
            self.depth -= 1
            self.done = not self.depth

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def public_post_text(page):
    """The text of the post on a t.me embed page, or None without one."""

    parser = _PostText()
    try:
        parser.feed(page or "")
        parser.close()
    except Exception:
        return None
    text = "".join(parser.parts).strip()
    return text or None
