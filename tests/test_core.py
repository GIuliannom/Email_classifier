import unittest
import sqlite3
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db.migrations import init_db
from core.ai.classify import classify_message
from core.ai.summarize import generate_summary
from core.ai.llm import LocalLLM

class TestCoreEngine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db_path = "data/test_mail.db"
        cls.db = init_db(cls.db_path)
        # Seed test accounts
        cls.db.execute("INSERT OR IGNORE INTO accounts (provider, address, auth_ref_in_keychain) VALUES (?, ?, ?)", ("gmail", "test@example.com", "ref"))
        cls.db.execute("INSERT OR IGNORE INTO accounts (provider, address, auth_ref_in_keychain) VALUES (?, ?, ?)", ("gmail", "work@example.com", "ref2"))
        cls.db.commit()
        cls.acc1 = cls.db.execute("SELECT id FROM accounts WHERE address='test@example.com'").fetchone()[0]
        cls.acc2 = cls.db.execute("SELECT id FROM accounts WHERE address='work@example.com'").fetchone()[0]

    def test_duplicate_free_sync(self):
        # Simulate inserting duplicate remote_id
        self.db.execute("""INSERT INTO messages (account_id, remote_id, subject, body_text, status) 
                           VALUES (?, ?, 'Dup', 'Body', 'new')""", (self.acc1, "REM001"))
        self.db.commit()
        
        # Try inserting same again (should be ignored due to UNIQUE constraint)
        cursor = self.db.execute("""INSERT OR IGNORE INTO messages (account_id, remote_id, subject, body_text, status) 
                                    VALUES (?, ?, 'Dup2', 'Body2', 'new')""", (self.acc1, "REM001"))
        
        # Verify only one exists
        count = self.db.execute("SELECT COUNT(*) FROM messages WHERE remote_id='REM001'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_classifier_rejects_invalid(self):
        # We mock the LLM to return hallucinated/invalid JSON to test our validator
        original_generate = LocalLLM.generate_json
        
        def mock_bad_json(self, prompt, schema, max_retries=1):
            raise RuntimeError("LLM failed to return valid JSON after 1 retries. Output: {bad}")
            
        LocalLLM.generate_json = mock_bad_json

        self.db.execute("INSERT INTO messages (account_id, remote_id, subject, body_text, status) VALUES (?, 'TEST_INV', 'Test Inv', 'This is a test email.', 'new')", (self.acc1,))
        self.db.commit()
        
        result = classify_message(self.db, 999) # ID will be assigned by DB
        # Fallback to Unsorted due to retry failure
        self.assertEqual(result["section"], "Unsorted")
        
        LocalLLM.generate_json = original_generate

    def test_summaries_never_mix_accounts(self):
        # Add messages to both accounts
        self.db.execute("INSERT INTO messages (account_id, remote_id, subject, body_text, status, date) VALUES (?, 'S1', 'A', 'Body A', 'new', '2026-10-19T12:00:00')", (self.acc1,))
        self.db.execute("INSERT INTO messages (account_id, remote_id, subject, body_text, status, date) VALUES (?, 'S2', 'B', 'Body B', 'new', '2026-10-19T12:00:00')", (self.acc2,))
        self.db.commit()

        # Generate summaries with wide date range to catch them; the LLM echoes its prompt
        original_generate = LocalLLM.generate_json
        LocalLLM.generate_json = lambda self, prompt, schema, max_retries=1: {"summary": prompt}
        try:
            s1 = generate_summary(self.db, self.acc1, "daily", ("2023-01-01", "2099-01-01"))
            s2 = generate_summary(self.db, self.acc2, "daily", ("2023-01-01", "2099-01-01"))
        finally:
            LocalLLM.generate_json = original_generate
        
        # Check DB entries are separated by account_id
        sum1_db = self.db.execute("SELECT text FROM summaries WHERE account_id=? AND kind='daily'", (self.acc1,)).fetchone()[0]
        sum2_db = self.db.execute("SELECT text FROM summaries WHERE account_id=? AND kind='daily'", (self.acc2,)).fetchone()[0]
        
        # Verify isolation in DB storage
        self.assertIn("Body A", sum1_db)
        self.assertNotIn("Body B", sum1_db)
        self.assertIn("Body B", sum2_db)
        self.assertNotIn("Body A", sum2_db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()
        if os.path.exists(cls.db_path):
            os.remove(cls.db_path)

if __name__ == "__main__":
    unittest.main()
