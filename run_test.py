#!/usr/bin/env python
"""Run final smoke test."""
import requests
import time
import sys

time.sleep(4)

print("=== SMOKE TEST ===")
print("Attempting POST to http://127.0.0.1:5000/symptom_checker")

try:
    response = requests.post(
        'http://127.0.0.1:5000/symptom_checker',
        data={
            'symptoms': 'I have fever, sore throat and cough. Took paracetamol 500mg twice daily and loratadine 10mg once daily.'
        },
        timeout=10
    )
    
    print(f"✓ Status: {response.status_code}")
    print(f"✓ Response length: {len(response.text)} characters")
    
    # Save response
    with open('final_test.html', 'w', encoding='utf-8') as f:
        f.write(response.text)
    print("✓ Saved to final_test.html")
    
    # Check for key elements
    checks = {
        'prescription card': 'prescription' in response.text.lower(),
        'paracetamol': 'paracetamol' in response.text.lower(),
        'loratadine': 'loratadine' in response.text.lower(),
        'med-item class': 'med-item' in response.text.lower(),
        'med-box section': 'med-box' in response.text.lower(),
    }
    
    print("\n=== RESPONSE CHECKS ===")
    for check_name, result in checks.items():
        status = "✓" if result else "✗"
        print(f"{status} {check_name}: {result}")
    
    # Extract med items if present
    if 'med-item' in response.text.lower():
        import re
        meds = re.findall(r'<div class="med-item">([^<]+)</div>', response.text)
        if meds:
            print(f"\n=== MEDICATIONS FOUND ({len(meds)}) ===")
            for i, med in enumerate(meds, 1):
                print(f"{i}. {med}")
    
    print("\n✓ TEST COMPLETE")
    sys.exit(0)

except Exception as e:
    print(f"✗ Error: {type(e).__name__}: {e}")
    sys.exit(1)
