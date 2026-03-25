#!/usr/bin/env python3
"""
Extract unique diseases and their related symptoms + treatments from the CSV dataset
and append them to chatbot_reply.txt with formatted guidance.
"""

import csv
from collections import defaultdict

def load_and_parse_dataset(filepath):
    """Load CSV and extract disease-symptom-treatment mappings."""
    disease_data = defaultdict(lambda: {'symptoms': set(), 'home_meds': set(), 'doctor_meds': set()})
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                disease = row.get('disease', '').strip()
                symptoms = row.get('symptoms', '').strip()
                home_med = row.get('home_medication', '').strip()
                doctor_med = row.get('doctor_prescription', '').strip()
                
                if disease:
                    if symptoms:
                        disease_data[disease]['symptoms'].add(symptoms)
                    if home_med:
                        disease_data[disease]['home_meds'].add(home_med)
                    if doctor_med:
                        disease_data[disease]['doctor_meds'].add(doctor_med)
        
        print(f"✓ Extracted {len(disease_data)} unique diseases from dataset")
        return disease_data
    except Exception as e:
        print(f"✗ Error loading dataset: {e}")
        return {}

def generate_disease_sections(disease_data):
    """Generate formatted disease guidance sections."""
    sections = []
    
    # Sort diseases alphabetically
    for disease in sorted(disease_data.keys()):
        data = disease_data[disease]
        symptoms_list = sorted(list(data['symptoms']))[:3]  # Top 3 symptom combos
        home_meds_list = sorted(list(data['home_meds']))[:2]  # Top 2 home treatments
        doctor_meds_list = sorted(list(data['doctor_meds']))[:2]  # Top 2 doctor treatments
        
        section = f"""--------------------------------------------------
{disease.upper()}
--------------------------------------------------
Reported symptoms:
- {chr(10).join(['- ' + s for s in symptoms_list])}

Home Care / Self-medication:
{chr(10).join(['- ' + m for m in home_meds_list]) if home_meds_list else '- Rest and supportive care'}

Doctor may prescribe:
{chr(10).join(['- ' + m for m in doctor_meds_list]) if doctor_meds_list else '- Consult a healthcare professional'}

When to seek medical help:
- Symptoms persist for more than 3-5 days
- Symptoms worsen significantly
- Additional severe symptoms develop
- High fever (above 103°F) or severe pain

"""
        sections.append(section)
    
    return sections

def update_chatbot_reply(sections):
    """Add disease sections to chatbot_reply.txt."""
    filepath = 'templates/chatbot_reply.txt'
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Find insertion point (before "LONG-DURATION SYMPTOMS RULE")
        insert_marker = "====================================================================\nLONG-DURATION SYMPTOMS RULE"
        
        new_content = "\n".join(sections)
        
        if insert_marker in content:
            content = content.replace(
                insert_marker,
                f"{new_content}\n{insert_marker}"
            )
            print(f"✓ Inserted {len(sections)} disease sections into chatbot_reply.txt")
        else:
            # Append before the last section if marker not found
            content = content.rstrip() + f"\n\n{new_content}\n"
            print(f"✓ Appended {len(sections)} disease sections to chatbot_reply.txt")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return True
    except Exception as e:
        print(f"✗ Error updating chatbot_reply.txt: {e}")
        return False

def main():
    print("=" * 70)
    print("INTEGRATING HEALTHCARE DATASET INTO CHATBOT KNOWLEDGE BASE")
    print("=" * 70)
    
    # Load dataset
    disease_data = load_and_parse_dataset('datasets.csv')
    if not disease_data:
        print("No data to process.")
        return
    
    print(f"\nDiseases found:")
    for disease in sorted(disease_data.keys()):
        count = len(disease_data[disease]['symptoms'])
        print(f"  • {disease}: {count} symptom combinations")
    
    # Generate sections
    print(f"\n▸ Generating disease guidance sections...")
    sections = generate_disease_sections(disease_data)
    
    # Update file
    print(f"\n▸ Updating chatbot_reply.txt...")
    if update_chatbot_reply(sections):
        print("\n" + "=" * 70)
        print("✓ SUCCESS - Dataset integrated into chatbot knowledge base")
        print("=" * 70)
        print("\nThe dashboard chatbot now has:")
        print(f"  • {len(disease_data)} diseases with detailed guidance")
        print(f"  • Home care recommendations from {len(disease_data)} sources")
        print(f"  • Doctor prescription guidelines")
        print(f"  • Emergency warning signs")
    else:
        print("\n✗ Failed to update chatbot knowledge base")

if __name__ == '__main__':
    main()
