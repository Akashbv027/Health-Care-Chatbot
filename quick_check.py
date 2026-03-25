#!/usr/bin/env python
import requests
import sys

BASE_URL = "http://127.0.0.1:5000"

# Quick test
try:
    # Test login page (no auth needed)
    r = requests.get(f"{BASE_URL}/login", timeout=5)
    print(f"Login page: HTTP {r.status_code}")
    
    # Register new user
    user = f"testuser_{int(__import__('time').time())}"
    r = requests.post(f"{BASE_URL}/register", 
                     data={"username": user, "email": f"{user}@test.com", 
                           "password": "test123", "confirm_password": "test123"},
                     timeout=5, allow_redirects=True)
    print(f"Register: HTTP {r.status_code}")
    
    # Login
    sess = requests.Session()
    r = sess.post(f"{BASE_URL}/login", 
                 data={"username": user, "password": "test123"},
                 timeout=5, allow_redirects=True)
    print(f"Login: HTTP {r.status_code}")
    
    # Test /appointments (this is where the error occurs)
    r = sess.get(f"{BASE_URL}/appointments", timeout=5)
    print(f"Appointments: HTTP {r.status_code}")
    
    if r.status_code == 200:
        print("SUCCESS: /appointments works!")
    else:
        print(f"ERROR: Got {r.status_code}")
        if "OperationalError" in r.text:
            print("Database error found in response")
        
except Exception as e:
    print(f"Error: {e}")
    sys.exit(1)
