"""Every active published property must be checked for channel deletion, not only recent ones."""
import ast
import sqlite3
import unittest
from pathlib import Path


class Cursor:
    def __init__(self, db): self.db = db
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def execute(self, sql, args): self.result = self.db.execute(sql.replace('%s', '?'), args)
    def fetchall(self): return self.result.fetchall()


class Connection(Cursor):
    def cursor(self): return Cursor(self.db)


class DeleteWatchScopeTest(unittest.TestCase):
    def test_old_published_properties_are_checked(self):
        db = sqlite3.connect(':memory:')
        db.executescript('''
            CREATE TABLE properties(id INTEGER, status TEXT, published_message_id INTEGER,
                                    deleted_from_channel_at TEXT, published_at TEXT, created_at TEXT);
            INSERT INTO properties VALUES (1,'ACTIVE',1174,NULL,'2026-06-01','2026-06-01');  -- months old
            INSERT INTO properties VALUES (2,'ACTIVE',1500,NULL,'2026-10-13','2026-10-13');
            INSERT INTO properties VALUES (3,'ACTIVE',1400,'2026-10-01','2026-09-01','2026-09-01');
            INSERT INTO properties VALUES (4,'INACTIVE',1300,NULL,'2026-09-01','2026-09-01');
            INSERT INTO properties VALUES (5,'ACTIVE',NULL,NULL,NULL,'2026-10-13');
        ''')
        source = Path(__file__).parent.joinpath('app/bot.py').read_text(encoding='utf-8')
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'get_delete_watch_property_ids')
        ns = {'db_connect': lambda: Connection(db)}
        exec(compile(ast.Module(body=[node], type_ignores=[]), 'bot.py', 'exec'), ns)
        self.assertEqual(ns['get_delete_watch_property_ids'](after_id=0, limit=25), [1, 2])
        self.assertEqual(ns['get_delete_watch_property_ids'](after_id=1, limit=1), [2])
        self.assertEqual(ns['get_delete_watch_property_ids'](after_id=2, limit=1), [1])


if __name__ == '__main__':
    unittest.main()
