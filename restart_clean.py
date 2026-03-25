#!/usr/bin/env python
"""Quick cleanup and restart server to fix DB schema."""
import os
import tempfile
import subprocess
import time
import sys

# Delete temp DB
temp_db = os.path.join(tempfile.gettempdir(), 'healthcare_app.db')
if os.path.exists(temp_db):
    try:
        os.remove(temp_db)
        print(f"✓ Deleted old temp DB: {temp_db}")
    except Exception as e:
        print(f"✗ Failed to delete: {e}")
else:
    print(f"✓ No old temp DB found")

print("\n" + "="*60)
print("Starting Flask server with fresh database...")
print("="*60 + "\n")

# Set environment and start server
env = os.environ.copy()
env['FORCE_TEMP_DB'] = '1'
env['NO_RELOAD'] = '1'

try:
    # Start Flask server in background
    proc = subprocess.Popen([sys.executable, 'app.py'], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    print(f"Started process PID: {proc.pid}")
    
    # Give server time to start and create DB
    time.sleep(4)
    
    # Quick test
    import requests
    try:
        r = requests.get('http://127.0.0.1:5000/login', timeout=3)
        print(f"\n✓ Server responding: HTTP {r.status_code}")
        print("✓ Database schema created successfully")
    except Exception as e:
        print(f"\n✗ Server test failed: {e}")
    
    print("\n" + "="*60)
    print("Server is running on http://127.0.0.1:5000")
    print("="*60)
    print("\nKeeping server process alive (Ctrl+C to stop)...")
    
    # Keep the process running
    while True:
        time.sleep(1)
        if proc.poll() is not None:
            print(f"Process exited with code {proc.returncode}")
            break
            
except KeyboardInterrupt:
    print("\nShutting down...")
    proc.terminate()
    proc.wait()
    print("Done")
except Exception as e:
    print(f"Error: {e}")
