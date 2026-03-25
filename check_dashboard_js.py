import os,sys
os.environ['FORCE_TEMP_DB']='1'
sys.path.insert(0, os.getcwd())
from app import app, db, User
app.testing=True
with app.app_context():
    db.create_all()
    user=User.query.filter_by(username='check_js_user').first()
    if not user:
        user=User(username='check_js_user', email=f'check_js_{os.getpid()}@example.com', password='x')
        db.session.add(user); db.session.commit()
    uid=user.id
with app.test_client() as c:
    with c.session_transaction() as s: s['user_id']=uid
    r=c.get('/dashboard')
    html=r.get_data(as_text=True)
    for token in ['startProfileFlow','awaiting_surgeries','promptNextProfileQuestion','extractPrescription']:
        print(token, 'FOUND' if token in html else 'MISSING')
