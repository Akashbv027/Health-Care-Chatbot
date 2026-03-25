import os
import pytest

from app import app, db, User


def test_dashboard_includes_chatbot_reply(tmp_path):
    # Ensure app is in testing mode
    app.testing = True

    # Create a test user in the database
    with app.app_context():
        db.create_all()
        # Create or get a test user
        user = User.query.filter_by(username='test_user_for_dashboard').first()
        if not user:
            user = User(username='test_user_for_dashboard', email='test@example.com', password='x')
            db.session.add(user)
            db.session.commit()

        user_id = user.id

    # Read the canonical chatbot reply text
    reply_path = os.path.join(os.path.dirname(__file__), '..', 'templates', 'chatbot_reply.txt')
    # Normalize path
    reply_path = os.path.normpath(reply_path)
    assert os.path.exists(reply_path), f"Expected {reply_path} to exist"
    with open(reply_path, 'r', encoding='utf-8') as f:
        canned = f.read().strip()

    # Use test client and set session user_id
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess['user_id'] = user_id

        resp = client.get('/dashboard')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)

        # The template includes the chatbot_reply.txt contents in a hidden textarea
        # so verify a snippet from the canned file is present in the rendered HTML
        snippet = canned.splitlines()[0].strip() if canned else ''
        assert snippet and snippet in html, "Dashboard did not include chatbot_reply.txt contents"
