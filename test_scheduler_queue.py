"""Run the actual candidate SQL against a queue with >50 pending duplicates."""
import ast
import sqlite3
import unittest
from pathlib import Path


class Cursor:
    def __init__(self, db): self.db=db
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,sql,args): self.result=self.db.execute(sql.replace('%s','?').replace('NOW()',"datetime('now')"),args)
    def fetchall(self): return self.result.fetchall()


class Connection(Cursor):
    def cursor(self): return Cursor(self.db)


class QueueTest(unittest.TestCase):
    def test_pending_duplicates_do_not_starve_ready_or_approved(self):
        db=sqlite3.connect(':memory:')
        db.executescript('''
            CREATE TABLE properties(id INTEGER,status TEXT);
            CREATE TABLE publish_queue(id INTEGER,property_id INTEGER,queue_status TEXT,scheduled_for TEXT,created_at TEXT,priority INTEGER);
            CREATE TABLE duplicate_reviews(property_id INTEGER,review_status TEXT);
        ''')
        for i in range(1,97):
            db.execute("INSERT INTO properties VALUES (?,'ACTIVE')",(i,))
            db.execute("INSERT INTO publish_queue VALUES (?,?,?,NULL,'2026-10-01',0)",(i,i,'PENDING_DUPLICATE' if i<=56 else 'READY'))
            if i<=56: db.execute("INSERT INTO duplicate_reviews VALUES (?,'PENDING')",(i,))
        source=Path(__file__).parent.joinpath('app/scheduler.py').read_text()
        tree=ast.parse(source)
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='get_queue_candidates')
        ns={'db_connect':lambda:Connection(db)}
        exec(compile(ast.Module(body=[node],type_ignores=[]),'scheduler.py','exec'),ns)
        self.assertEqual([r[0] for r in ns['get_queue_candidates']()],list(range(57,97)))
        db.execute("UPDATE duplicate_reviews SET review_status='APPROVED' WHERE property_id=1")
        self.assertEqual(ns['get_queue_candidates']()[0][0],1)
        self.assertEqual(len(ns['get_queue_candidates']()),41)
        db.close()


if __name__=='__main__': unittest.main()
