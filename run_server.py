#!/usr/bin/env python
import os
import sys
import tempfile
import traceback

os.environ['FORCE_TEMP_DB'] = '1'
os.environ['NO_RELOAD'] = '1'

temp_db = os.path.join(tempfile.gettempdir(), 'healthcare_app.db')
print(f"[INFO] Database: {temp_db}")

sys.path.insert(0, os.getcwd())

try:
    from app import app, db
    
    with app.app_context():
        db.create_all()
        print("[OK] Schema created")
    
    print("[INFO] Starting server...")
    sys.stdout.flush()
    sys.stderr.flush()
    
    # Run with threaded to allow background requests
    app.run(host='127.0.0.1', port=5000, debug=False, use_reloader=False, threaded=True)
    
except Exception as e:
    print(f"[ERROR] {e}")
    traceback.print_exc()
    sys.exit(1)
