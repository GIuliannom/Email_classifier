"""Offline check for classify --all-new and the label/accuracy commands (model is faked).
Run: python -m unittest tests.test_classify_eval
"""
import contextlib
import io
import json
import math
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ai import evaluate
from core.ai import llm as llm_mod
from core.ai.classify import classify_all_new, classify_message
from core.db.migrations import init_db

# subject -> what the fake model answers
ANSWERS = {
    "Weekly digest": ("News", 0.9),
    "Card statement": ("News", 0.9),     # confidently wrong
    "Big sale": ("Social", 0.4),         # unsure
    "Dinner Friday?": ("Personal", 0.5),  # right but under the threshold
}


PROMPTS = []
REAL_GENERATE = llm_mod.LocalLLM.generate_json  # saved before setUp swaps in fake_model


def fake_model(self, prompt, schema, max_retries=1, json_schema=None):
    PROMPTS.append(prompt)
    for subject, (section, conf) in ANSWERS.items():
        if f"Subject: {subject}" in prompt:
            return {"section": section, "confidence": conf}
    raise AssertionError("unknown email")


class TestClassifyEval(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with contextlib.redirect_stdout(io.StringIO()):
            self.db = init_db(os.path.join(self.tmp.name, "mail.db"))
        self.db.execute("INSERT INTO accounts (provider, address, auth_ref_in_keychain) VALUES ('gmail', 'me@x.com', 'ref')")
        # odd ids are held out for scoring; id 2 is an example candidate (even)
        for i, subject in zip((1, 3, 5, 7), ANSWERS):
            self.db.execute("INSERT INTO messages (id, account_id, remote_id, subject, body_text, date) "
                            "VALUES (?, 1, ?, ?, 'body', ?)", (i, f"m{i}", subject, f"2026-10-19T0{i}:00:00"))
        self.db.execute("INSERT INTO messages (id, account_id, remote_id, sender, subject, status) "
                        "VALUES (2, 1, 'm2', 'Digest Co <d@x.com>', 'Example newsletter', 'classified')")
        self.db.commit()
        PROMPTS.clear()
        self.labels = os.path.join(self.tmp.name, "labels.csv")
        patcher = mock.patch.object(llm_mod.LocalLLM, "generate_json", fake_model)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_all_new_classifies_once_and_logs_moves(self):
        with contextlib.redirect_stdout(io.StringIO()):
            placed = classify_all_new(self.db, labels_path=self.labels)
            again = classify_all_new(self.db, labels_path=self.labels)
        self.assertEqual(placed, {"News": 2, "Unsorted": 2})
        self.assertEqual(sum(again.values()), 0)  # nothing left marked 'new'
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM actions_log").fetchone()[0], 2)

    def test_model_down_is_reported_and_mail_waits(self):
        with mock.patch.object(llm_mod.LocalLLM, "generate_json", side_effect=RuntimeError("connection refused")), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Ollama"):
                classify_all_new(self.db, labels_path=self.labels)
        self.assertEqual(self.db.execute("SELECT count(*) FROM messages WHERE status='new'").fetchone()[0], 4)

    def test_predict_only_writes_nothing(self):
        self.assertEqual(classify_message(self.db, 1, apply=False, labels_path=self.labels)["section"], "News")
        self.assertEqual(self.db.execute("SELECT status FROM messages WHERE id=1").fetchone()[0], "new")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM actions_log").fetchone()[0], 0)

    def test_label_records_and_resumes(self):
        with mock.patch("builtins.input", side_effect=["99", "1", "banks", "Shopping", "q"]), \
             contextlib.redirect_stdout(io.StringIO()):
            evaluate.label(self.db, n=5, path=self.labels)
        self.assertEqual(sorted(evaluate.read_labels(self.labels).values()), ["Banks", "News", "Shopping"])

        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=["Ads803", "5\x1b", "s", "q"]), contextlib.redirect_stdout(out):
            evaluate.label(self.db, n=5, path=self.labels)  # typos are re-asked, then skipped
        self.assertIn("3/5 already labeled", out.getvalue())
        self.assertEqual(len(evaluate.read_labels(self.labels)), 3)

    def test_accuracy_counts(self):
        with open(self.labels, "w") as f:
            f.write("message_id,label\n1,News\n3,Banks\n5,Shopping\n7,Personal\n2,News\n")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = evaluate.accuracy(self.db, path=self.labels)
        # 1 right, 2 wrongly moved to News, 3 is a new-section idea correctly left in Unsorted,
        # 4 right section but too unsure to move
        self.assertEqual(result, {"n": 4, "correct": 2, "moved": 2, "wrong_moves": 1})
        self.assertIn("Shopping (1)", out.getvalue())
        self.assertIn("Scoring 4 held-out emails", out.getvalue())
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM actions_log").fetchone()[0], 0)
        # the labeled even-id email is shown as an example, never scored
        self.assertIn("Digest Co <d@x.com> | Example newsletter -> News", PROMPTS[0])
        self.assertEqual(len(PROMPTS), 4)

    def test_same_sender_examples(self):
        with open(self.labels, "w") as f:
            f.write("message_id,label\n2,News\n3,Banks\n")
        # same address as example id 2, different display name and case
        self.db.execute("UPDATE messages SET sender='The Digest <D@X.com>' WHERE id=1")
        classify_message(self.db, 1, apply=False, labels_path=self.labels)
        classify_message(self.db, 5, apply=False, labels_path=self.labels)  # other sender
        self.assertIn("same sender", PROMPTS[0])
        self.assertIn("Digest Co <d@x.com> | Example newsletter -> News", PROMPTS[0].split("same sender")[1])
        self.assertNotIn("same sender", PROMPTS[1])
        self.assertNotIn("-> Banks", PROMPTS[0])  # held-out (odd id) labels never appear in prompts

    def test_your_moves_become_same_sender_examples(self):
        from core.actions import move_message
        with open(self.labels, "w") as f:
            f.write("message_id,label\n5,Ads\n")  # id 5 is held out
        for i in (1, 3, 5):
            self.db.execute("UPDATE messages SET sender='Shop <s@shop.com>' WHERE id=?", (i,))
        move_message(self.db, 3, "Ads", "user_move")  # your correction
        move_message(self.db, 5, "Banks", "user_move")  # a held-out email: must not leak
        classify_message(self.db, 1, apply=False, labels_path=self.labels)
        same = PROMPTS[0].split("same sender")[1]
        self.assertIn("Weekly digest", PROMPTS[0])  # sanity: email 1 itself is in the prompt
        self.assertIn("Card statement -> Ads", same)
        self.assertNotIn("-> Banks", same)

    def test_confidence_from_token_probability(self):
        llm_mod._requests_since_load = 0  # no reload call mixed into the request below
        tokens = ['{"', 'section', '":', ' "', 'News', '"}']
        resp = mock.Mock()
        resp.json.return_value = {"response": '{"section": "News"}', "logprobs": [
            {"token": t, "logprob": math.log(0.62) if t == "News" else 0.0} for t in tokens]}
        with mock.patch.object(llm_mod.LocalLLM, "generate_json", REAL_GENERATE), \
             mock.patch.object(llm_mod.requests, "post", return_value=resp) as post:
            r = classify_message(self.db, 1, apply=False, labels_path=self.labels)
        self.assertEqual(r["section"], "News")
        self.assertAlmostEqual(r["confidence"], 0.62)
        # Ollama is told the only allowed answers; Unsorted is never one of them
        allowed = post.call_args.kwargs["json"]["format"]["properties"]["section"]["enum"]
        self.assertIn("Ads", allowed)
        self.assertNotIn("Unsorted", allowed)


if __name__ == "__main__":
    unittest.main()
