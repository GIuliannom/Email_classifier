import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.db.migrations import init_db
from core.ai.classify import classify_message

def run_test_set():
    db = init_db("data/test_mail.db")
    
    # Load 100-email test set (JSONL or JSON array)
    test_emails = []
    try:
        with open(os.path.join(os.path.dirname(__file__), "test_emails.json"), "r") as f:
            test_emails = json.load(f)
    except FileNotFoundError:
        print("No test_emails.json found. Generating dummy data for demonstration.")
        test_emails = [{"id": i, "subject": f"Test Subject {i}", "body": f"This is email body number {i}."} for i in range(100)]
        # Insert into DB temporarily for testing
        for i, em in enumerate(test_emails):
            db.execute("INSERT INTO messages (account_id, remote_id, subject, body_text, status) VALUES (?, ?, ?, ?, 'new')", 
                       (1, f'TST{i}', em["subject"], em["body"]))
        db.commit()

    print(f"{'Subject':<30} | {'Predicted Section':<15} | {'Confidence'}")
    print("-" * 65)
    
    for em in test_emails:
        msg_id = em["id"]
        result = classify_message(db, msg_id)
        print(f"{em['subject']:<30} | {result['section']:<15} | {result['confidence']}")

if __name__ == "__main__":
    run_test_set()
