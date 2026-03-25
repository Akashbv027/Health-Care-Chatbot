#!/usr/bin/env python
import requests
import json

BASE_URL = "http://127.0.0.1:5000"

# Test 1: Check if server is up
try:
    r = requests.get(f"{BASE_URL}/login")
    print(f"[TEST 1] Server check: HTTP {r.status_code}")
except Exception as e:
    print(f"[TEST 1] FAILED: {e}")
    exit(1)

# Test 2: Register a test user
try:
    register_data = {
        "username": "testuser_quick",
        "email": "testquick@example.com",
        "password": "password123",
        "confirm_password": "password123"
    }
    r = requests.post(f"{BASE_URL}/register", data=register_data, allow_redirects=False)
    print(f"[TEST 2] Register user: HTTP {r.status_code}")
    session_cookie = requests.Session()
except Exception as e:
    print(f"[TEST 2] Register attempt: {e}")

# Test 3: Login
session = requests.Session()
try:
    login_data = {"username": "testuser_quick", "password": "password123"}
    r = session.post(f"{BASE_URL}/login", data=login_data, allow_redirects=True)
    print(f"[TEST 3] Login: HTTP {r.status_code}")
except Exception as e:
    print(f"[TEST 3] Login FAILED: {e}")

# Test 4: Fetch /appointments (critical - tests if DB schema fixed)
try:
    r = session.get(f"{BASE_URL}/appointments")
    print(f"[TEST 4] /appointments (DB schema test): HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"        Response preview: {r.text[:200]}")
    elif "facility-map-single" in r.text:
        print("        PASS: Leaflet map markup found in /appointments")
    else:
        print("        WARNING: Leaflet map markup NOT found in /appointments")
except Exception as e:
    print(f"[TEST 4] /appointments FAILED: {e}")

# Test 5: Fetch /book_appointment
try:
    r = session.get(f"{BASE_URL}/book_appointment")
    print(f"[TEST 5] /book_appointment: HTTP {r.status_code}")
    if r.status_code == 200:
        if "facility-map" in r.text and "L.marker" in r.text:
            print("        PASS: Leaflet map markup found in /book_appointment")
        else:
            print("        WARNING: Leaflet map markup NOT found")
except Exception as e:
    print(f"[TEST 5] /book_appointment FAILED: {e}")

print("\n[SUMMARY] Core functionality tests complete.")
