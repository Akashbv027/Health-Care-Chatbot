#!/usr/bin/env python
import requests

BASE = 'http://127.0.0.1:5000'
DATA = {
    'symptoms': 'I have fever and cough for 2 days',
    'age': '35',
    'gender': 'male',
    'duration': '1-3-days',
    'severity': 'moderate'
}

s = requests.Session()
try:
    r = s.post(f'{BASE}/symptom_checker', data=DATA, timeout=10)
    open('out_symptom.html','w', encoding='utf-8').write(r.text)
    print('Status:', r.status_code)
    # quick presence check
    if 'Dosage Summary' in r.text:
        print('Dosage Summary found')
    else:
        print('Dosage Summary NOT found')
except Exception as e:
    print('Request failed:', e)
