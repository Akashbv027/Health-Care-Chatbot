from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from flask_sqlalchemy import SQLAlchemy

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import json
from datetime import datetime, timedelta
import os
import time
from flask_login import LoginManager, current_user
from datetime import datetime
import logging
from dotenv import load_dotenv
from email.message import EmailMessage
import smtplib
import ssl

# Suppress noisy python-dotenv parse warnings (they indicate malformed lines in .env)
logging.getLogger('dotenv').setLevel(logging.ERROR)

# Load environment variables from a local .env file (for development only).
load_dotenv()

from sqlalchemy.exc import OperationalError
from sqlalchemy import text
import requests
import math

# Import Gemini client after loading .env so it can read GEMINI_API_KEY at import time.
from gemini_client import generate_response

# Create Flask app and initialize extensions before any routes
app = Flask(__name__)
app.secret_key = 'your_secret_key_here_change_in_production'
# Prefer using a DB in `instance/` but fall back to a writable temp directory if OneDrive or permissions block file creation.
instance_db = os.path.join('instance', 'healthcare_app.db')
# Allow forcing use of a temp DB via env var for recovery/testing
force_temp = os.environ.get('FORCE_TEMP_DB', '0') == '1'
temp_path_env = os.environ.get('TEMP_DB_PATH')

if force_temp:
    import tempfile
    if temp_path_env:
        temp_db = os.path.abspath(temp_path_env)
    else:
        temp_db = os.path.join(tempfile.gettempdir(), 'healthcare_app.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{temp_db}'
else:
    # Ensure instance directory exists
    os.makedirs(os.path.dirname(instance_db) or '.', exist_ok=True)

    # Resolve absolute paths for SQLite and try to open the DB. If the file is locked or corrupted,
    # back it up and fall back to a temp DB or in-memory DB as a last resort.
    try:
        import sqlite3, shutil, time, tempfile
        abs_path = os.path.abspath(instance_db)

        # If DB file exists, try opening it to detect locks/corruption
        if os.path.exists(abs_path):
            try:
                conn = sqlite3.connect(abs_path, timeout=5)
                conn.execute('PRAGMA integrity_check;')
                conn.close()
                app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{abs_path}'
            except sqlite3.DatabaseError:
                # Backup the problematic DB
                ts = time.strftime('%Y%m%d%H%M%S')
                backup = f"{abs_path}.corrupt.{ts}"
                try:
                    shutil.move(abs_path, backup)
                    print(f'Backed up corrupted database to {backup}')
                except Exception as move_err:
                    print('Failed to backup corrupted DB:', move_err)
                # Try creating a new DB in the instance path
                try:
                    conn = sqlite3.connect(abs_path)
                    conn.close()
                    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{abs_path}'
                except Exception:
                    # Fallback to tempdir
                    fallback = os.path.join(tempfile.gettempdir(), 'healthcare_app.db')
                    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{fallback}'
        else:
            # DB file doesn't exist; try creating it to verify write perms
            try:
                conn = sqlite3.connect(abs_path)
                conn.close()
                app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{abs_path}'
            except Exception:
                # Fallback to tempdir
                fallback = os.path.join(tempfile.gettempdir(), 'healthcare_app.db')
                try:
                    conn = sqlite3.connect(fallback)
                    conn.close()
                    app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{fallback}'
                except Exception:
                    # Final fallback to in-memory DB
                    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
    except Exception as ex:
        # If anything unexpected happens, fall back to an in-memory DB to keep the app running.
        print('Database initialization warning, falling back to in-memory DB:', ex)
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)


@app.context_processor
def inject_google_maps_key():
    """Inject Google Maps API key and a flag into all templates for convenience."""
    key = os.environ.get('GOOGLE_MAPS_API_KEY', '')
    return dict(google_api_key=key or '', use_google_maps=bool(key))


@app.context_processor
def inject_now():
    """Provide a `now()` helper to templates that returns the current UTC datetime."""
    from datetime import datetime
    return dict(now=lambda: datetime.utcnow())


# Health check: ensure the configured DB can be opened before handling requests
@app.before_request
def check_database_available():
    # Avoid checking during static file requests
    try:
        # Try a lightweight DB operation (use text() to avoid SQLAlchemy warning)
        db.session.execute(text('SELECT 1'))
    except Exception as e:
        # If DB is not available, return a friendly error page with guidance
        # Log the full traceback to error.log via the app logger
        import traceback, logging
        logging.getLogger('healthcare_app').error(traceback.format_exc())
        # If this is an API/json request, return JSON
        if request.path.startswith('/api') or request.is_json:
            return jsonify({'error': 'Database unavailable', 'details': str(e)}), 503
        # Otherwise render an explanatory template
        return render_template('error_db.html', message=str(e)), 503

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Database Models
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    age = db.Column(db.Integer)
    gender = db.Column(db.String(10))
    phone = db.Column(db.String(15))
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class LocationLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    doctor_name = db.Column(db.String(100), nullable=False)
    appointment_date = db.Column(db.DateTime, nullable=False)
    facility_location = db.Column(db.String(100), nullable=True)
    symptoms = db.Column(db.Text)
    status = db.Column(db.String(20), default='Scheduled')

class Prescription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    doctor_name = db.Column(db.String(100), nullable=False)
    prescription_date = db.Column(db.DateTime, default=datetime.utcnow)
    medications = db.Column(db.Text)
    diagnosis = db.Column(db.Text)
    instructions = db.Column(db.Text)


# Generated prescriptions (created by the app based on user input)
class GeneratedPrescription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    filename = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    doctor_name = db.Column(db.String(150))
    patient_name = db.Column(db.String(150))
    patient_age = db.Column(db.String(50))
    patient_gender = db.Column(db.String(50))
    medications = db.Column(db.Text)
    diagnosis = db.Column(db.Text)
    instructions = db.Column(db.Text)
    is_cancelled = db.Column(db.Boolean, default=False)


    # Ensure any newly added tables (GeneratedPrescription, LocationLog) are created when code is updated
try:
    with app.app_context():
        db.create_all()
except Exception as e:
    print('Failed to create additional DB tables:', e)

class MedicationReminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_name = db.Column(db.String(100), nullable=False)
    dosage = db.Column(db.String(50))
    frequency = db.Column(db.String(50))
    reminder_time = db.Column(db.String(50))
    is_active = db.Column(db.Boolean, default=True)

class EmergencyContact(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(15), nullable=False)
    relationship = db.Column(db.String(50))


# Additional models for doctors and prescription uploads
class Doctor(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    specialization = db.Column(db.String(150))
    facility_name = db.Column(db.String(200))
    facility_address = db.Column(db.String(300))
    phone = db.Column(db.String(50))
    availability = db.Column(db.String(200))
    lat = db.Column(db.Float)
    lng = db.Column(db.Float)


class PrescriptionUpload(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    filename = db.Column(db.String(300), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    acknowledged = db.Column(db.Boolean, default=False)
    acknowledged_at = db.Column(db.DateTime)
    notes = db.Column(db.Text)


# Load packaged clinics data (optional) for realistic clinic names and coordinates
_CLINICS_DATA = []
try:
    clinics_file = os.path.join(os.path.dirname(__file__), 'data', 'clinics.json')
    if os.path.exists(clinics_file):
        with open(clinics_file, 'r', encoding='utf-8') as cf:
            _CLINICS_DATA = json.load(cf)
except Exception as e:
    app.logger.info('No clinics data loaded: %s', e)

# Initialize database
with app.app_context():
    # Ensure the SQLite database is usable. If corrupted, back it up and recreate a fresh DB.
    try:
        # Try a simple integrity check using sqlite3 directly
        if app.config.get('SQLALCHEMY_DATABASE_URI', '').startswith('sqlite:'):
            import sqlite3
            db_path = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                cur.execute('PRAGMA integrity_check;')
                row = cur.fetchone()
                conn.close()
                if not row or row[0] != 'ok':
                    # Backup corrupted DB and create a new one
                    import shutil, time
                    ts = time.strftime('%Y%m%d%H%M%S')
                    backup = f"{db_path}.corrupt.{ts}"
                    try:
                        shutil.move(db_path, backup)
                        print(f"Backed up corrupted database to {backup}")
                    except Exception as e:
                        print('Failed to backup corrupted DB:', e)
            except sqlite3.DatabaseError:
                # Couldn't open DB at all; attempt to move it out of the way
                import shutil, time
                ts = time.strftime('%Y%m%d%H%M%S')
                backup = f"{db_path}.corrupt.{ts}"
                try:
                    shutil.move(db_path, backup)
                    print(f"Moved unreadable database to {backup}")
                except Exception as e:
                    print('Failed to move unreadable DB:', e)

    except Exception as ex:
        # Non-fatal: log and continue to attempt to create tables
        print('Database integrity check failed:', ex)

    # Create tables (this will create a new DB if the old one was moved)
    try:
        db.create_all()
    except Exception as e:
        print('Failed to create DB tables:', e)



# File upload helpers and route
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    # Serve uploaded files from instance/uploads if present, else fall back to temp uploads
    instance_upload = os.path.join('instance', 'uploads')
    if os.path.exists(os.path.join(instance_upload, filename)):
        return send_from_directory(instance_upload, filename)
    # Fallback to system temp uploads
    import tempfile
    tmp_upload = os.path.join(tempfile.gettempdir(), 'uploads')
    return send_from_directory(tmp_upload, filename)


def render_prescription_image(pres_text_lines, save_path):
    """Render a clinical-style prescription image from lines of text and save as PNG."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        raise RuntimeError('Pillow library is required to render prescription images')

    # Basic image size parameters
    width = 800
    line_height = 28
    padding = 30
    height = padding * 2 + line_height * (len(pres_text_lines) + 4)

    img = Image.new('RGB', (width, height), color='white')
    draw = ImageDraw.Draw(img)

    # Try to load a default TTF; fall back to default font
    try:
        font_path = os.path.join(os.path.dirname(__file__), 'fonts', 'DejaVuSans.ttf')
        if os.path.exists(font_path):
            font = ImageFont.truetype(font_path, 18)
            font_bold = ImageFont.truetype(font_path, 20)
        else:
            font = ImageFont.load_default()
            font_bold = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()
        font_bold = ImageFont.load_default()

    y = padding
    # Header
    draw.text((padding, y), "Hospital / Clinic Prescription", fill='black', font=font_bold)
    y += line_height * 1.5

    for line in pres_text_lines:
        # Normalize to ASCII to avoid font encoding issues with default fonts
        try:
            import unicodedata
            safe_line = unicodedata.normalize('NFKD', line)
            safe_line = safe_line.encode('ascii', 'ignore').decode('ascii')
        except Exception:
            safe_line = line
        draw.text((padding, y), safe_line, fill='black', font=font)
        y += line_height

    # Signature line
    y += line_height
    draw.line((padding, y, width - padding, y), fill='black', width=1)
    y += 6
    draw.text((padding, y), "Dr. Signature", fill='black', font=font)

    # Ensure directory exists
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    img.save(save_path, format='PNG')
    return save_path


def _haversine_distance(lat1, lon1, lat2, lon2):
    """Return distance in meters between two lat/lon points using the haversine formula."""
    try:
        if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
            return None
        from math import radians, sin, cos, asin, sqrt
        R = 6371000.0
        dlat = radians(lat2 - lat1)
        dlon = radians(lon2 - lon1)
        a = sin(dlat/2)**2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon/2)**2
        c = 2 * asin(sqrt(a))
        return R * c
    except Exception:
        return None

# AI Models and Data
class HealthPredictor:
    def __init__(self):
        # Delay heavy model training until first prediction to avoid import-time failures
        self.diabetes_model = None
        self.heart_model = None
        self.scaler = None
    
    def train_diabetes_model(self):
        # Sample diabetes dataset
        data = {
            'Pregnancies': [6, 1, 8, 1, 0, 5, 3, 10, 2, 8],
            'Glucose': [148, 85, 183, 89, 137, 116, 78, 115, 197, 125],
            'BloodPressure': [72, 66, 64, 66, 40, 74, 50, 0, 70, 96],
            'SkinThickness': [35, 29, 0, 23, 35, 0, 32, 0, 45, 0],
            'Insulin': [0, 0, 0, 94, 168, 0, 88, 0, 543, 0],
            'BMI': [33.6, 26.6, 23.3, 28.1, 43.1, 25.6, 31.0, 35.3, 30.5, 0.0],
            'DiabetesPedigreeFunction': [0.627, 0.351, 0.672, 0.167, 2.288, 0.201, 0.248, 0.134, 0.158, 0.232],
            'Age': [50, 31, 32, 21, 33, 30, 26, 29, 53, 54],
            'Outcome': [1, 0, 1, 0, 1, 0, 1, 0, 1, 1]
        }
        
        # Local imports to avoid requiring heavy ML libs unless this functionality is used
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import train_test_split

        df = pd.DataFrame(data)
        X = df.drop('Outcome', axis=1)
        y = df['Outcome']

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        model = RandomForestClassifier(n_estimators=100, random_state=42)
        model.fit(X_train, y_train)
        return model
    
    def train_heart_model(self):
        # Sample heart disease dataset
        data = {
            'age': [63, 37, 41, 56, 57, 57, 56, 44, 52, 57],
            'sex': [1, 1, 0, 1, 0, 1, 0, 1, 1, 1],
            'cp': [3, 2, 1, 1, 0, 0, 1, 1, 2, 2],
            'trestbps': [145, 130, 130, 120, 120, 140, 140, 120, 172, 150],
            'chol': [233, 250, 204, 236, 354, 192, 294, 263, 199, 168],
            'fbs': [1, 0, 0, 0, 0, 0, 0, 0, 1, 0],
            'restecg': [0, 1, 0, 1, 1, 1, 0, 1, 1, 1],
            'thalach': [150, 187, 172, 178, 163, 148, 153, 173, 162, 174],
            'exang': [0, 0, 0, 0, 1, 0, 0, 0, 0, 0],
            'oldpeak': [2.3, 3.5, 1.4, 0.8, 0.6, 0.4, 1.3, 0.0, 0.5, 1.6],
            'slope': [0, 0, 2, 2, 2, 1, 1, 2, 2, 2],
            'ca': [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
            'thal': [1, 2, 2, 2, 2, 1, 2, 3, 3, 2],
            'target': [1, 0, 0, 0, 1, 0, 0, 0, 0, 1]
        }
        
        import pandas as pd
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.model_selection import train_test_split

        df = pd.DataFrame(data)
        X = df.drop('target', axis=1)
        y = df['target']

        X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
        model = RandomForestClassifier(n_estimators=100, random_state=42)
        model.fit(X_train, y_train)
        return model
    
    def predict_diabetes(self, features):
        if self.diabetes_model is None:
            self.diabetes_model = self.train_diabetes_model()
        prediction = self.diabetes_model.predict([features])
        probability = self.diabetes_model.predict_proba([features])
        return prediction[0], probability[0][1]
    
    def predict_heart_disease(self, features):
        if self.heart_model is None:
            self.heart_model = self.train_heart_model()
        prediction = self.heart_model.predict([features])
        probability = self.heart_model.predict_proba([features])
        return prediction[0], probability[0][1]

# Lazily initialize the heavy HealthPredictor to avoid long imports and startup failures
_health_predictor = None
def get_health_predictor():
    global _health_predictor
    if _health_predictor is None:
        _health_predictor = HealthPredictor()
    return _health_predictor

# Symptom Checker Knowledge Base
symptom_knowledge_base = {
    'body': {
        'conditions': ['Migraine'],
        'severity': 'low',
        'advice': "Home: Rest in dark room, avoid screen. Doctor: Paracetamol 650mg + Domperidone once during pain",
        'emergency': False
    },
    'chest': {
        'conditions': ['Heart Problem'],
        'severity': 'high',
        'advice': "Home: Do not self-medicate. Doctor: Immediate hospital visit – ECG and cardiologist consultation required",
        'emergency': True
    },
    'cold': {
        'conditions': ['Migraine'],
        'severity': 'low',
        'advice': "Home: Rest in dark room, avoid screen. Doctor: Paracetamol 650mg + Domperidone once during pain",
        'emergency': False
    },
    'cough': {
        'conditions': ['Migraine'],
        'severity': 'low',
        'advice': "Home: Rest in dark room, avoid screen. Doctor: Paracetamol 650mg + Domperidone once during pain",
        'emergency': False
    },
    'diarrhea': {
        'conditions': ['Heart Problem'],
        'severity': 'high',
        'advice': "Home: Do not self-medicate. Doctor: Immediate hospital visit – ECG and cardiologist consultation required",
        'emergency': True
    },
    'fatigue': {
        'conditions': ['Heart Problem'],
        'severity': 'high',
        'advice': "Home: Do not self-medicate. Doctor: Immediate hospital visit – ECG and cardiologist consultation required",
        'emergency': True
    },
    'fever': {
        'conditions': ['Viral Infection'],
        'severity': 'medium',
        'advice': "Home: Paracetamol 500mg, warm fluids, rest. Doctor: Paracetamol 650mg twice daily after food for 3 days",
        'emergency': False
    },
    'headache': {
        'conditions': ['Migraine'],
        'severity': 'low',
        'advice': "Home: Rest in dark room, avoid screen. Doctor: Paracetamol 650mg + Domperidone once during pain",
        'emergency': False
    },
    'nausea': {
        'conditions': ['Viral Infection'],
        'severity': 'medium',
        'advice': "Home: Paracetamol 500mg, warm fluids, rest. Doctor: Paracetamol 650mg twice daily after food for 3 days",
        'emergency': False
    },
    'nose': {
        'conditions': ['Viral Infection'],
        'severity': 'medium',
        'advice': "Home: Paracetamol 500mg, warm fluids, rest. Doctor: Paracetamol 650mg twice daily after food for 3 days",
        'emergency': False
    },
    'pain': {
        'conditions': ['Migraine'],
        'severity': 'low',
        'advice': "Home: Rest in dark room, avoid screen. Doctor: Paracetamol 650mg + Domperidone once during pain",
        'emergency': False
    },
    'runny': {
        'conditions': ['Viral Infection'],
        'severity': 'medium',
        'advice': "Home: Paracetamol 500mg, warm fluids, rest. Doctor: Paracetamol 650mg twice daily after food for 3 days",
        'emergency': False
    },
    'sore': {
        'conditions': ['Food Poisoning'],
        'severity': 'medium',
        'advice': "Home: ORS, light food, fluids. Doctor: ORS after every loose stool + Ondansetron 4mg if vomiting",
        'emergency': False
    },
    'throat': {
        'conditions': ['Food Poisoning'],
        'severity': 'medium',
        'advice': "Home: ORS, light food, fluids. Doctor: ORS after every loose stool + Ondansetron 4mg if vomiting",
        'emergency': False
    },
    'vomiting': {
        'conditions': ['Viral Infection'],
        'severity': 'medium',
        'advice': "Home: Paracetamol 500mg, warm fluids, rest. Doctor: Paracetamol 650mg twice daily after food for 3 days",
        'emergency': False
    },
}

# Routes
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template('index.html')


# ...existing code...

@app.route('/api/location', methods=['POST'])
def receive_location():
    data = request.get_json()
    lat = data.get('latitude')
    lng = data.get('longitude')
    user_id = session.get('user_id')  # Or pass user/device ID in the request
    if lat is None or lng is None:
        return jsonify({'error': 'Missing latitude or longitude'}), 400
    log = LocationLog(user_id=user_id, latitude=lat, longitude=lng)
    db.session.add(log)
    db.session.commit()
    return jsonify({'status': 'success'})
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password_raw = request.form.get('password', '')

        if not username or not email or not password_raw:
            return render_template('register.html', error='All fields are required', username=username, email=email)

        if User.query.filter_by(username=username).first():
            return render_template('register.html', error='Username already exists', username=username, email=email)

        if User.query.filter_by(email=email).first():
            return render_template('register.html', error='Email already registered', username=username, email=email)

        password = generate_password_hash(password_raw)
        user = User(username=username, email=email, password=password)
        db.session.add(user)
        db.session.commit()

        session['user_id'] = user.id
        session['username'] = user.username
        return redirect(url_for('dashboard'))

    return render_template('register.html')
# ...existing code...

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            session['user_id'] = user.id
            session['username'] = user.username
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error='Invalid credentials')
    
    return render_template('login.html')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user = User.query.get(session['user_id'])
    appointments = Appointment.query.filter_by(user_id=session['user_id']).all()
    reminders = MedicationReminder.query.filter_by(user_id=session['user_id'], is_active=True).all()
    google_key = os.environ.get('GOOGLE_MAPS_API_KEY')
    use_google = bool(google_key)

    return render_template('dashboard.html', 
                         user=user, 
                         appointments=appointments,
                         reminders=reminders,
                         google_api_key=google_key,
                         use_google_maps=use_google)

# ...existing code...
@app.route('/symptom_checker', methods=['GET', 'POST'])
def symptom_checker():
    if request.method == 'POST':
        symptoms_text = request.form.get('symptoms', '').strip()
        age = request.form.get('age', '')
        gender = request.form.get('gender', '')
        duration = request.form.get('duration', '')
        severity_input = request.form.get('severity', '').lower()
        
        # Get severity level and matched symptoms from client-side analysis
        severity_level = request.form.get('severity_level', severity_input or 'moderate')
        matched_symptoms_json = request.form.get('matched_symptoms_json', '{}')
        try:
            matched_symptoms = json.loads(matched_symptoms_json)
        except:
            matched_symptoms = []

        # Handle optional uploaded image
        image_filename = None
        try:
            if 'symptom_image' in request.files:
                file = request.files['symptom_image']
                if file and file.filename:
                    if allowed_file(file.filename):
                        filename = secure_filename(file.filename)
                        ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
                        uid = session.get('user_id', 'anon')
                        save_name = f"{uid}_{ts}_{filename}"
                        upload_dir = os.path.join('instance', 'uploads')
                        os.makedirs(upload_dir, exist_ok=True)
                        file_path = os.path.join(upload_dir, save_name)
                        file.save(file_path)
                        image_filename = save_name
                    else:
                        return render_template('symptom_checker.html', error='Unsupported image type', symptoms=symptoms_text, age=age, gender=gender, duration=duration, image_url=None)
        except Exception as e:
            app.logger.exception('Failed to save uploaded image')
            return render_template('symptom_checker.html', error='Failed to save image', symptoms=symptoms_text, age=age, gender=gender, duration=duration, image_url=None)

        if not symptoms_text:
            return render_template(
                'symptom_checker.html',
                error='Please describe your symptoms',
                symptoms=symptoms_text,
                age=age,
                gender=gender,
                duration=duration,
                image_url=(url_for('uploaded_file', filename=image_filename) if image_filename else None)
            )

        # GENERATE SEVERITY-BASED PRESCRIPTION ANALYSIS
        text = symptoms_text.lower()
        image_findings = None
        try:
            img_raw = request.form.get('image_findings')
            if img_raw:
                try:
                    image_findings = json.loads(img_raw)
                except Exception:
                    image_findings = None
        except Exception:
            image_findings = None

        # Build assessment based on matched symptoms and severity level
        def generate_severity_based_assessment(matched_symp, severity_level, age_val, gender_val, duration_val):
            """Generate severity-based prescription assessment"""
            if not matched_symp:
                return "No specific symptoms matched in training database."
            
            assessment_parts = []
            conditions_found = set()
            emergency_detected = False
            
            for symptom_info in matched_symp:
                conditions_found.update(symptom_info.get('conditions', []))
                if symptom_info.get('emergency'):
                    emergency_detected = True
            
            # Build contextual assessment based on severity
            if severity_level == 'severe' or severity_level == 'high':
                assessment_parts.append(
                    f"HIGH SEVERITY SYMPTOMS REPORTED: {', '.join([s.get('symptom', 'unknown') for s in matched_symp])}\n"
                    f"Identified conditions: {', '.join(conditions_found)}\n"
                    f"URGENT: Please seek immediate medical evaluation by a licensed healthcare provider."
                )
                if emergency_detected:
                    assessment_parts.append("⚠️ EMERGENCY CONDITIONS DETECTED - Call emergency services if symptoms worsen.")
            
            elif severity_level == 'moderate' or severity_level == 'medium':
                assessment_parts.append(
                    f"MODERATE SEVERITY: {', '.join([s.get('symptom', 'unknown') for s in matched_symp])}\n"
                    f"Possible conditions: {', '.join(conditions_found)}\n"
                    f"Recommendation: Schedule a doctor's appointment within 1-2 days."
                )
            
            else:  # mild or low
                assessment_parts.append(
                    f"MILD SYMPTOMS: {', '.join([s.get('symptom', 'unknown') for s in matched_symp])}\n"
                    f"Possible conditions: {', '.join(conditions_found)}\n"
                    f"Recommendation: Start with home care. Contact doctor if symptoms persist beyond 3-5 days."
                )
            
            # Add demographic-specific guidance
            if age_val and int(age_val) < 12:
                assessment_parts.append("⚠️ Pediatric patient - Recommend pediatrician consultation.")
            elif age_val and int(age_val) > 65:
                assessment_parts.append("⚠️ Senior patient - Additional monitoring recommended.")
            
            if duration_val:
                if 'more-than-2-weeks' in duration_val:
                    assessment_parts.append("⚠️ Prolonged symptoms - Professional evaluation strongly recommended.")
            
            return "\n\n".join(assessment_parts)
        
        # Generate medications based on severity
        def get_severity_based_medications(matched_symp, severity_level):
            """Generate medication recommendations based on severity"""
            meds = []
            
            if not matched_symp:
                return meds
            
            # Extract medications from matched symptoms
            for symptom_info in matched_symp:
                advice = symptom_info.get('advice', '')
                # Parse home vs doctor medications from advice
                if 'Doctor:' in advice:
                    doctor_part = advice.split('Doctor:')[1].strip()
                    # Extract medication names (simplified parsing)
                    if 'Paracetamol' in advice and 'Paracetamol' not in meds:
                        meds.append('Paracetamol 500-650mg')
                    if 'Domperidone' in advice and 'Domperidone' not in meds:
                        meds.append('Domperidone (as prescribed)')
                    if 'Cough syrup' in advice and 'Cough syrup' not in meds:
                        meds.append('Cough syrup 10ml twice daily')
                    if 'Loperamide' in advice and 'Loperamide' not in meds:
                        meds.append('Loperamide 2mg')
                    if 'Ibuprofen' in advice and 'Ibuprofen' not in meds:
                        meds.append('Ibuprofen 400-600mg')
                    if 'ORS' in advice and 'ORS' not in meds:
                        meds.append('Oral Rehydration Solution (ORS)')
            
            # Add severity-specific recommendations
            if severity_level in ['severe', 'high']:
                meds.insert(0, "URGENT: Consult doctor immediately - Do not self-medicate")
            elif severity_level in ['moderate', 'medium']:
                if not meds:
                    meds = [
                        'Rest and hydration',
                        'Paracetamol 500-650mg (for fever/pain)',
                        'Monitor symptoms - Contact doctor if worsening'
                    ]
            else:  # mild
                if not meds:
                    meds = [
                        'Rest',
                        'Increased fluid intake',
                        'Paracetamol 500mg if needed (for fever/pain)'
                    ]
            
            return meds
        
        # Load trained dataset for analysis
        import os
        import pandas as pd
        
        dataset_path = os.path.join(os.path.dirname(__file__), 'datasets.csv')
        df_trained = None
        
        try:
            if os.path.exists(dataset_path):
                df_trained = pd.read_csv(dataset_path)
        except Exception as e:
            app.logger.error(f"Failed to load datasets.csv: {e}")
        
        # Find best matching symptoms from trained dataset
        best_match = None
        best_match_count = 0
        all_symptoms_in_input = set()
        
        # Extract all symptoms from input text
        for key in symptom_knowledge_base.keys():
            if key in text:
                all_symptoms_in_input.add(key)
        
        # If we have symptoms, find the best matching row in trained data
        if df_trained is not None and len(all_symptoms_in_input) > 0:
            # Split symptoms column and compare
            for idx, row in df_trained.iterrows():
                row_symptoms = set(str(row['symptoms']).lower().split())
                matching_symptoms = all_symptoms_in_input.intersection(row_symptoms)
                
                if len(matching_symptoms) > best_match_count:
                    best_match = row
                    best_match_count = len(matching_symptoms)
        
        # Generate assessment based on trained data match
        if best_match is not None:
            disease = str(best_match['disease'])
            home_med = str(best_match['home_medication'])
            doctor_rx = str(best_match['doctor_prescription'])
            symptoms_matched = str(best_match['symptoms'])
            
            # Determine severity based on disease type
            if disease in ['Heart Problem', 'Stroke']:
                severity = 'HIGH'
                severity_class = 'high'
            elif disease in ['Food Poisoning', 'Viral Infection', 'Common Cold']:
                severity = 'MODERATE'
                severity_class = 'moderate'
            else:
                severity = 'MILD'
                severity_class = 'mild'
            
            result = (
                f"CITY CLINIC HEALTH - SYMPTOM ASSESSMENT REPORT\n"
                f"{'='*50}\n\n"
                f"PATIENT INFORMATION:\n"
                f"Age: {age if age else 'Not provided'} | Gender: {gender if gender else 'Not provided'} | Duration: {duration if duration else 'Not specified'}\n\n"
                f"SYMPTOMS ANALYSIS:\n"
                f"Matched Symptoms: {symptoms_matched}\n"
                f"Input Symptoms: {', '.join(all_symptoms_in_input) if all_symptoms_in_input else symptoms_text}\n\n"
                f"DIAGNOSIS:\n"
                f"Identified Condition: {disease}\n"
                f"Severity Level: {severity} ({'⚠️ HIGH - SEEK IMMEDIATE MEDICAL CARE' if severity_class == 'high' else '⚠️ MODERATE - Schedule Doctor Visit' if severity_class == 'moderate' else '✓ MILD - Monitor and Rest'})\n\n"
                f"TREATMENT RECOMMENDATIONS:\n"
                f"{'─'*50}\n"
                f"HOME CARE INSTRUCTIONS:\n{home_med}\n\n"
                f"DOCTOR'S PRESCRIPTION:\n{doctor_rx}\n\n"
                f"FOLLOW-UP:\n"
            )
            
            if severity_class == 'high':
                result += "URGENT: Seek medical attention immediately. Do not delay.\n"
                meds_list = ['EMERGENCY: Consult doctor immediately', doctor_rx]
            elif severity_class == 'moderate':
                result += "Schedule doctor appointment within 1-2 days. Monitor symptoms closely.\n"
                meds_list = [home_med, doctor_rx]
            else:
                result += "Monitor symptoms. Contact doctor if symptoms persist beyond 3-5 days.\n"
                meds_list = [home_med, doctor_rx]
            
            # Emergency check
            emergency_flag = severity_class == 'high'
        else:
            # No trained match found - use knowledge base fallback
            matched_keys = []
            conditions = set()
            home_treatments = []
            doctor_treatments = []
            
            for key, info in symptom_knowledge_base.items():
                if key in text:
                    matched_keys.append(key)
                    conditions.update(info.get('conditions', []))
            
            if matched_keys:
                cond_list = ', '.join(sorted(conditions))
                result = (
                    f"CITY CLINIC HEALTH - SYMPTOM ASSESSMENT REPORT\n"
                    f"{'='*50}\n\n"
                    f"PATIENT INFORMATION:\n"
                    f"Age: {age if age else 'Not provided'} | Gender: {gender if gender else 'Not provided'} | Duration: {duration if duration else 'Not specified'}\n\n"
                    f"SYMPTOMS IDENTIFIED:\n"
                    f"{', '.join(matched_keys)}\n\n"
                    f"PRELIMINARY ASSESSMENT:\n"
                    f"Possible Conditions: {cond_list}\n"
                    f"Severity: REQUIRES PROFESSIONAL EVALUATION\n\n"
                    f"RECOMMENDATION:\n"
                    f"Please consult a healthcare professional for detailed diagnosis and treatment recommendations.\n"
                )
                meds_list = ['Consult healthcare provider for personalized medication recommendations']
                emergency_flag = 'high' in [symptom_knowledge_base[k].get('severity') for k in matched_keys if k in symptom_knowledge_base]
            else:
                # No matches at all — provide clearer guidance and examples
                result = (
                    f"CITY CLINIC HEALTH - SYMPTOM ASSESSMENT REPORT\n"
                    f"{'='*50}\n\n"
                    f"PATIENT INFORMATION:\n"
                    f"Age: {age if age else 'Not provided'} | Gender: {gender if gender else 'Not provided'} | Duration: {duration if duration else 'Not specified'}\n\n"
                    f"ASSESSMENT RESULT:\n"
                    f"We could not find a confident match in our trained symptom database based on the information provided.\n\n"
                    f"HOW YOU CAN HELP US ASSESS BETTER:\n"
                    f"- Exact symptoms (for example: sore throat, localized pain, cough, rash)\n"
                    f"- Location of the symptom (for example: chest, lower right abdomen, left arm)\n"
                    f"- When it started and how it has changed (hours, days, gradual, sudden)\n"
                    f"- Severity and whether symptoms are improving or worsening\n"
                    f"- Any known triggers, recent travel, contacts, or chronic conditions\n"
                    f"- Current medications and allergies\n\n"
                    f"RECOMMENDATION:\n"
                    f"Please update your report with the details above so our symptom checker can provide a more specific assessment.\n"
                    f"If you are unsure what to enter, examples include: 'fever 38.9°C for 2 days with productive cough' or 'sharp lower right abdominal pain starting 6 hours ago'.\n\n"
                    f"WHEN TO SEEK URGENT CARE:\n"
                    f"If you have any of the following, seek emergency care or call emergency services immediately:\n"
                    f"- Chest pain or pressure, sudden difficulty breathing, choking\n"
                    f"- Fainting, seizures, severe head injury, or uncontrolled bleeding\n"
                    f"- Signs of severe infection: very high fever, confusion, very rapid heartbeat\n\n"
                    f"NON-URGENT OPTIONS:\n"
                    f"- For non-urgent concerns, consider booking a telemedicine visit or routine clinic appointment.\n"
                    f"- If symptoms persist or worsen despite home care, contact your primary care provider for evaluation.\n\n"
                    f"GENERAL CARE INSTRUCTIONS (until you see a clinician):\n"
                    f"1. Rest and avoid strenuous activities\n"
                    f"2. Stay hydrated and use over-the-counter symptom relief as appropriate\n"
                    f"3. Monitor symptoms closely and note any changes\n"
                    f"4. Seek medical advice if new concerning symptoms develop\n"
                )
                meds_list = []
                emergency_flag = False

        # If the client provided image analysis findings (from the client-side analyzer),
        # merge a concise image summary into the result
        try:
            image_findings_raw = request.form.get('image_findings')
            if image_findings_raw:
                try:
                    image_findings = json.loads(image_findings_raw)
                except Exception:
                    image_findings = None
                if image_findings:
                    parts = []
                    for f in image_findings:
                        try:
                            if isinstance(f, dict):
                                msg = f.get('message') or f.get('type') or str(f)
                                pct = f.get('percent')
                                if pct is not None:
                                    parts.append(f"{msg} ({pct}%)")
                                else:
                                    parts.append(msg)
                            else:
                                parts.append(str(f))
                        except Exception:
                            continue
                    if parts:
                        image_summary = "Image findings: " + "; ".join(parts)
                        result = result + "\n\n" + image_summary
        except Exception:
            pass

        # Log meds_list for debugging
        try:
            app.logger.info('symptom_checker meds_list: %s', meds_list)
        except Exception:
            pass

        # Load dosage guidance from static chatbot file and pick relevant entries
        dosage_info = []
        try:
            dosage_map = load_chatbot_dosage_map()
            # Use matched_keys and conditions to select relevant dosage snippets
            seen_d = set()
            # prefer matched keywords from user input
            for k in matched_keys:
                k0 = k.lower()
                for key in dosage_map:
                    if k0 in key or key in k0:
                        v = dosage_map.get(key)
                        if v and v not in seen_d:
                            dosage_info.append(v)
                            seen_d.add(v)
            # also try conditions set
            for cond in conditions:
                cond_low = cond.lower()
                for key in dosage_map:
                    if key in cond_low or cond_low in key:
                        v = dosage_map.get(key)
                        if v and v not in seen_d:
                            dosage_info.append(v)
                            seen_d.add(v)
            # fallback: if meds_list contains drug names, try matching those
            for medline in meds_list:
                ml = medline.lower()
                for key in dosage_map:
                    if key in ml or ml in key:
                        v = dosage_map.get(key)
                        if v and v not in seen_d:
                            dosage_info.append(v)
                            seen_d.add(v)
        except Exception:
            dosage_info = []

        return render_template(
            'symptom_checker.html',
            result=result,
            meds_list=meds_list,
            dosage_info=dosage_info,
            symptoms=symptoms_text,
            age=age,
            gender=gender,
            duration=duration,
            image_url=(url_for('uploaded_file', filename=image_filename) if image_filename else None)
        )

    # GET
    return render_template('symptom_checker.html')
# ...existing code...

@app.route('/predict_disease', methods=['POST'])
def predict_disease():
    if 'user_id' not in session:
        return jsonify({'error': 'Please login first'})
    
    data = request.json
    disease_type = data.get('disease_type')
    features = data.get('features', [])
    
    try:
        if disease_type == 'diabetes':
            hp = get_health_predictor()
            prediction, probability = hp.predict_diabetes(features)
            result = 'Diabetic' if prediction == 1 else 'Non-Diabetic'
        elif disease_type == 'heart':
            hp = get_health_predictor()
            prediction, probability = hp.predict_heart_disease(features)
            result = 'Heart Disease Risk' if prediction == 1 else 'Low Heart Disease Risk'
        else:
            return jsonify({'error': 'Invalid disease type'})
        
        return jsonify({
            'prediction': result,
            'probability': round(probability * 100, 2),
            'message': f'Prediction: {result} with {probability:.2%} confidence'
        })
    
    except Exception as e:
        return jsonify({'error': str(e)})

# ...existing code...
@app.route('/book_appointment', methods=['GET', 'POST'])
def book_appointment():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    error = None
    # preserve submitted values to re-populate the form on error
    form_values = {
        'department': '',
        'doctor_name': '',
        'appointment_date': '',
        'facility': '',
        'symptoms': ''
    }

    # Load clinics once and make available to template and POST handling
    try:
        clinics_file = os.path.join(os.path.dirname(__file__), 'data', 'clinics.json')
        with open(clinics_file, 'r', encoding='utf-8') as cf:
            clinics_raw = json.load(cf)
        import re
        def slugify(name):
            return re.sub(r"[^a-z0-9]+", '_', name.lower()).strip('_')

        clinics_list = []
        clinic_map = {}
        for c in clinics_raw:
            cid = slugify(c.get('name', ''))
            clinics_list.append({
                'id': cid,
                'name': c.get('name'),
                'address': c.get('address'),
                'lat': c.get('lat'),
                'lng': c.get('lng'),
                'doctors': c.get('doctors', [])
            })
            clinic_map[cid] = c
    except Exception:
        clinics_list = []
        clinic_map = {}

    if request.method == 'POST':
        form_values['department'] = request.form.get('department', '').strip()
        form_values['doctor_name'] = request.form.get('doctor_name', '').strip()
        form_values['appointment_date'] = request.form.get('appointment_date', '').strip()
        form_values['facility'] = request.form.get('facility', '').strip()
        form_values['symptoms'] = request.form.get('symptoms', '').strip()

        if not form_values['doctor_name'] or not form_values['appointment_date']:
            error = 'Please provide a doctor and appointment date/time.'
            return render_template('book_appointment.html', error=error, clinics=clinics_list, **form_values)

        # parse common HTML datetime-local and date formats
        try:
            raw = form_values['appointment_date']
            if 'T' in raw:
                appt_dt = datetime.strptime(raw, '%Y-%m-%dT%H:%M')
            else:
                appt_dt = datetime.strptime(raw, '%Y-%m-%d')
        except ValueError:
            error = 'Invalid date/time format. Use the date/time picker.'
            return render_template('book_appointment.html', error=error, clinics=clinics_list, **form_values)

        # Map facility code to display name using preloaded clinic_map
        try:
            chosen = clinic_map.get(form_values['facility']) if clinic_map else None
            if chosen:
                facility_name = chosen.get('name')
                if chosen.get('address'):
                    facility_name += ' - ' + chosen.get('address')
            else:
                facility_name = 'Not specified'
        except Exception:
            facility_name = 'Not specified'

        try:
            appointment = Appointment(
                user_id=session['user_id'],
                doctor_name=form_values['doctor_name'],
                appointment_date=appt_dt,
                facility_location=facility_name,
                symptoms=form_values['symptoms']
            )
            db.session.add(appointment)
            db.session.commit()
            app.logger.info('Appointment created: user_id=%s, facility=%s, doctor=%s', session['user_id'], facility_name, form_values['doctor_name'])
            return redirect(url_for('appointments'))
        except Exception as e:
            db.session.rollback()
            error = 'Failed to save appointment: ' + str(e)
            return render_template('book_appointment.html', error=error, clinics=clinics_list, **form_values)

    # GET - load clinics from data/clinics.json and pass to template
    try:
        clinics_file = os.path.join(os.path.dirname(__file__), 'data', 'clinics.json')
        with open(clinics_file, 'r', encoding='utf-8') as cf:
            clinics_raw = json.load(cf)

        # create simple ids for option values
        import re
        def slugify(name):
            return re.sub(r"[^a-z0-9]+", '_', name.lower()).strip('_')

        clinics_list = []
        for c in clinics_raw:
            cid = slugify(c.get('name', ''))
            clinics_list.append({
                'id': cid,
                'name': c.get('name'),
                'address': c.get('address'),
                'lat': c.get('lat'),
                'lng': c.get('lng'),
                'doctors': c.get('doctors', [])
            })
    except Exception:
        clinics_list = []

    return render_template('book_appointment.html', clinics=clinics_list)
# ...existing code...

@app.route('/appointments')
def appointments():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    user_appointments = Appointment.query.filter_by(user_id=session['user_id']).all()
    return render_template('appointments.html', appointments=user_appointments)

@app.route('/medication_reminders', methods=['GET', 'POST'])
def medication_reminders():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    if request.method == 'POST':
        medication_name = request.form['medication_name']
        dosage = request.form['dosage']
        frequency = request.form['frequency']
        reminder_time = request.form['reminder_time']
        
        reminder = MedicationReminder(
            user_id=session['user_id'],
            medication_name=medication_name,
            dosage=dosage,
            frequency=frequency,
            reminder_time=reminder_time
        )
        db.session.add(reminder)
        db.session.commit()
        
        return redirect(url_for('medication_reminders'))
    
    reminders = MedicationReminder.query.filter_by(user_id=session['user_id']).all()
    return render_template('medication_reminders.html', reminders=reminders)

@app.route('/emergency_services')
def emergency_services():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    return render_template('emergency_services.html')


def _haversine_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * (2 * math.atan2(math.sqrt(a), math.sqrt(1-a)))


# Simple in-memory cache for IP -> (lat, lng) lookups with TTL to reduce external requests
_ip_location_cache = {}
_IP_CACHE_TTL = int(os.environ.get('IP_LOCATION_CACHE_TTL', '3600'))  # seconds
_DEV_LOCATION = None
_DEV_LOCATION_RAW = os.environ.get('DEV_LOCATION', '')
if _DEV_LOCATION_RAW:
    try:
        parts = _DEV_LOCATION_RAW.split(',')
        _DEV_LOCATION = (float(parts[0].strip()), float(parts[1].strip()))
    except Exception:
        _DEV_LOCATION = None

def get_location_from_ip(ip):
    """Return (lat, lng) for an IP using a cached lookup. Returns (None, None) on failure."""
    now = time.time()
    rec = _ip_location_cache.get(ip)
    if rec:
        lat, lng, ts = rec
        if now - ts < _IP_CACHE_TTL:
            return lat, lng
    try:
        # For local development, allow overriding via DEV_LOCATION env var when IP is loopback
        if ip in ('127.0.0.1', '::1') and _DEV_LOCATION is not None:
            return _DEV_LOCATION
        r = requests.get(f'http://ip-api.com/json/{ip}?fields=status,message,lat,lon', timeout=5)
        j = r.json()
        if j.get('status') == 'success':
            lat = float(j.get('lat'))
            lng = float(j.get('lon'))
            _ip_location_cache[ip] = (lat, lng, now)
            return lat, lng
    except Exception:
        pass
    return None, None


def parse_med_suggestions(text):
    """Heuristically parse medication suggestions from a block of text.
    Returns a list of medication lines (strings). Tries JSON first, then
    falls back to line-based heuristics (bullets, lines containing doses/frequencies).
    """
    meds = []
    if not text:
        return meds
    import re
    # Try to find a JSON object in the text and extract a `medications` list
    try:
        jmatch = re.search(r"(\{[\s\S]*\})", text)
        if jmatch:
            try:
                parsed = json.loads(jmatch.group(1))
                if isinstance(parsed, dict):
                    cand = parsed.get('medications') or parsed.get('meds') or parsed.get('medication')
                    if isinstance(cand, list):
                        for it in cand:
                            if isinstance(it, str) and it.strip():
                                meds.append(it.strip())
                        if meds:
                            return meds
                    elif isinstance(cand, str) and cand.strip():
                        # split by lines
                        for ln in cand.splitlines():
                            ln = ln.strip()
                            if ln:
                                meds.append(ln)
                        if meds:
                            return meds
            except Exception:
                pass
    except Exception:
        pass

    # Line-based fallback: collect lines that look like medication recommendations
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        # Typical clues: dose units, frequency words, bullets, or hyphen-separated drug entries
        if re.search(r"\b(mg|mcg|g|ml|tablet|tab|cap|drop|once|daily|twice|thrice|bd|tid|qds|hourly|every)\b", ln, re.I):
            meds.append(ln)
            continue
        if ln.startswith('-') or ln.startswith('*'):
            meds.append(ln.lstrip('-* ').strip())
            continue
        # Some LLM outputs use formats like "Paracetamol - 500mg - twice daily"
        if '-' in ln and any(ch.isalpha() for ch in ln):
            meds.append(ln)
            continue
        # Numeric lead (e.g. "1. Paracetamol 500mg")
        if re.match(r"^\d+\.?\s+", ln):
            meds.append(ln)
            continue

    # Deduplicate while preserving order
    seen = set()
    out = []
    for m in meds:
        if m not in seen:
            seen.add(m)
            out.append(m)
    # If we already found plausible medication lines, try to split any lines that
    # contain multiple medication entries (e.g., "...paracetamol 500mg twice daily and loratadine 10mg...")
    if out:
        refined = []
        # reuse some of the embedded logic to split multi-med lines
        dose_unit_pattern = r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units)\b"
        # More inclusive frequency tokens and non-greedy name capture
        freq_tokens = r"once daily|twice daily|once a day|twice a day|every\s+\d+\s+hours|for\s+\d+\s+(?:days|weeks)|q8h|q6h|bd|tid|qds|hourly|daily|per day|per week|once|twice|every|day|at night|in the morning"
        capture_pattern = re.compile(
            rf"([A-Za-z][A-Za-z0-9\-\s\(\)/]{{1,60}}?\s+\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units)\b(?:\s*(?:{freq_tokens}))?)",
            re.I
        )
        for line in out:
            line = line.strip()
            if not re.search(dose_unit_pattern, line, re.I):
                if line not in refined:
                    refined.append(line)
                continue

            found = []
            # Split likely multi-med lines on conjunctions/commas and try to capture from each part
            parts = re.split(r",| and |\band\b|;", line, flags=re.I)
            # If a part is missing a trailing frequency but the next part begins with a frequency token,
            # attach that frequency to the current part (e.g., "paracetamol 500mg" + "twice daily and loratadine...").
            for i, p in enumerate(parts):
                p = p.strip()
                # lookahead: if this part lacks a frequency but next part starts with frequency, attach it
                next_attach = ''
                if i + 1 < len(parts):
                    nxt = parts[i+1].strip()
                    if re.match(rf"^({freq_tokens})", nxt, re.I):
                        mgrp = re.match(rf"^({freq_tokens}(?:\s+for\s+\d+\s+(?:days|weeks))?)", nxt, re.I)
                        if mgrp:
                            next_attach = ' ' + mgrp.group(0)
                combined = (p + next_attach).strip()
                for m in capture_pattern.findall(combined):
                    frag = m.strip().strip(' ,;.-')
                    frag = re.sub(r'^(took|used)\s+', '', frag, flags=re.I).strip()
                    # If the capture looks like a dose-only (e.g. '500 mg'), prefer the combined part
                    if re.match(r'^\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units)$', frag, re.I):
                        alt = combined.strip().strip(' ,;.-')
                        alt = re.sub(r'^(took|used)\s+', '', alt, flags=re.I).strip()
                        if alt and alt not in found:
                            found.append(alt)
                        continue
                    if frag and frag not in found:
                        found.append(frag)
                # remove any remaining dose-only captures
                found = [f for f in found if not re.match(r'^\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units)$', f, re.I)]
            # If nothing found in parts, fall back to trying whole line
            if not found:
                for m in capture_pattern.findall(line):
                    frag = m.strip().strip(' ,;.-')
                    frag = re.sub(r'^(took|used)\s+', '', frag, flags=re.I).strip()
                    if frag and frag not in found:
                        found.append(frag)

            if not found:
                parts = re.split(r",| and |\band\b|;", line, flags=re.I)
                for p in parts:
                    p = p.strip().strip(' ,;.-')
                    if re.search(dose_unit_pattern, p, re.I):
                        if p and p not in found:
                            found.append(p)

            if found:
                for f in found:
                    if f not in refined:
                        refined.append(f)
            else:
                if line not in refined:
                    refined.append(line)

        if refined:
            return refined

    # Secondary pass: try to extract medication phrases embedded inside running text.
    # This handles inputs like: "Took paracetamol 500mg twice daily and loratadine 10mg once daily."
    embedded = []
    # Split text into sentence-like chunks, then further split on conjunctions when dose clues exist
    chunks = re.split(r'[\n\.;]', text)
    dose_unit_pattern = r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units)\b"
    freq_words = r"once daily|twice daily|once a day|twice a day|every\s+\d+\s+hours|for\s+\d+\s+(?:days|weeks)|q8h|q6h|bd|tid|qds|hourly|daily|per day|per week|once|twice|every|day|at night|in the morning"
    # Pattern to capture "DrugName 500mg [frequency]" fragments (avoid greedy capture)
    freq_tokens = r"once|twice|once daily|twice daily|once a day|twice a day|daily|per day|per week|bd|tid|qds|q8h|q6h|hourly|every\s+\d+\s+hours|every|for\s+\d+\s+(?:days|weeks)"
    capture_pattern = re.compile(
        rf"([A-Za-z][A-Za-z0-9\-\s\(\)/]{{1,60}}?\s+\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units)\b(?:\s*(?:{freq_tokens}))?)",
        re.I
    )
    for ch in chunks:
        ch = ch.strip()
        if not ch:
            continue
        # If the chunk contains a dose unit, try splitting on ' and ' or commas to isolate meds
        if re.search(dose_unit_pattern, ch, re.I):
            # Split chunk on conjunctions/commas and try to capture from each sub-part first
            parts = re.split(r",| and |\band\b|;", ch, flags=re.I)
            part_found_any = False
            for i, p in enumerate(parts):
                p = p.strip()
                # Attach frequency from next part when appropriate
                next_attach = ''
                if i + 1 < len(parts):
                    nxt = parts[i+1].strip()
                    if re.match(rf"^({freq_words})", nxt, re.I):
                        mgrp = re.match(rf"^({freq_words}(?:\s+for\s+\d+\s+(?:days|weeks))?)", nxt, re.I)
                        if mgrp:
                            next_attach = ' ' + mgrp.group(0)
                combined = (p + next_attach).strip()
                for m in capture_pattern.findall(combined):
                    frag = m.strip().strip(' ,;.-')
                    frag = re.sub(r'^(took|used)\s+', '', frag, flags=re.I).strip()
                    # If capture is dose-only, fall back to the full combined part which may include the drug name
                    if re.match(r'^\d+(?:\.\d+)?\s*(?:mg|mcg|g|ml|units)$', frag, re.I):
                        alt = combined.strip().strip(' ,;.-')
                        alt = re.sub(r'^(took|used)\s+', '', alt, flags=re.I).strip()
                        if alt and alt.lower() not in seen:
                            embedded.append(alt)
                            seen.add(alt.lower())
                            part_found_any = True
                        continue
                    if frag and frag.lower() not in seen:
                        embedded.append(frag)
                        seen.add(frag.lower())
                        part_found_any = True

            # If nothing found in parts, fall back to trying whole chunk
            if not part_found_any:
                for m in capture_pattern.findall(ch):
                    frag = m.strip().strip(' ,;.-')
                    frag = re.sub(r'^(took|used)\s+', '', frag, flags=re.I).strip()
                    if frag and frag.lower() not in seen:
                        embedded.append(frag)
                        seen.add(frag.lower())

    # Normalize and return embedded matches preserving order
    final = []
    for e in embedded:
        if e and e not in final:
            final.append(e)
    return final


def load_chatbot_dosage_map():
    """Load dosage guidance from templates/chatbot_reply.txt and return a map
    of keyword -> dosage summary text.
    This is a heuristic parser that looks for 'Medicine:', 'Dosage:', 'Maximum:', 'Duration:' blocks.
    """
    import re
    try:
        path = os.path.join(app.root_path, 'templates', 'chatbot_reply.txt')
        if not os.path.exists(path):
            return {}
        with open(path, 'r', encoding='utf-8') as f:
            txt = f.read()
    except Exception:
        return {}

    # Split by section separators used in the file
    sections = [s.strip() for s in txt.split('\n--------------------------------------------------\n') if s.strip()]
    dosage_map = {}

    def extract_field(block, field):
        lines = block.splitlines()
        for i, ln in enumerate(lines):
            if ln.strip().lower().startswith(field.lower() + ':'):
                vals = []
                j = i + 1
                while j < len(lines) and lines[j].strip().startswith('-'):
                    vals.append(lines[j].strip().lstrip('-').strip())
                    j += 1
                return ' '.join(vals)
        return None

    # Known short keys to match to blocks
    keys = ['fever', 'cough', 'breathless', 'breathlessness', 'chest pain', 'stomach', 'vomit', 'diarrhea', 'dizziness', 'rash', 'urinary', 'anxiety', 'stress', 'headache']

    for sec in sections:
        header = ''
        for line in sec.splitlines():
            if line.strip():
                header = line.strip()
                break
        header_low = header.lower()
        # build a summary from fields
        med = extract_field(sec, 'Medicine') or ''
        dose = extract_field(sec, 'Dosage') or ''
        maximum = extract_field(sec, 'Maximum') or ''
        duration = extract_field(sec, 'Duration') or ''

        summary_parts = []
        if med:
            summary_parts.append(f"Medicine: {med}")
        if dose:
            summary_parts.append(f"Dosage: {dose}")
        if maximum:
            summary_parts.append(f"Maximum: {maximum}")
        if duration:
            summary_parts.append(f"Duration: {duration}")

        summary = ' | '.join(p for p in summary_parts if p)
        if not summary:
            continue

        # Try to map to keys found in header or section body
        mapped = False
        for k in keys:
            if k in header_low or k in sec.lower():
                dosage_map.setdefault(k, []).append(summary)
                mapped = True
        # fallback: use header words as keys
        if not mapped:
            # pick any word tokens from header to index
            for token in re.findall(r"[A-Za-z]{3,}", header_low):
                if len(token) > 2:
                    dosage_map.setdefault(token, []).append(summary)

    # join multi-entry lists into single string per key
    for k in list(dosage_map.keys()):
        dosage_map[k] = '\n'.join(dosage_map[k])

    return dosage_map


@app.route('/nearby_facilities', methods=['POST'])
def nearby_facilities():
    data = request.get_json() or {}
    lat = data.get('lat')
    lng = data.get('lng')
    if lat is None or lng is None:
        # Attempt to infer location from client IP if coordinates not provided (cached)
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        lat, lng = get_location_from_ip(ip)
        if lat is None or lng is None:
            return jsonify({'error': 'Invalid or missing lat/lng'}), 400
    else:
        lat = float(lat)
        lng = float(lng)
    radius = int(data.get('radius', 5000))
    results = []
    google_key = os.environ.get('GOOGLE_MAPS_API_KEY')
    if google_key:
        try:
            url = 'https://maps.googleapis.com/maps/api/place/nearbysearch/json'
            params = {'location': f'{lat},{lng}', 'radius': radius, 'type': 'hospital', 'key': google_key}
            r = requests.get(url, params=params, timeout=8)
            data = r.json()
            for p in data.get('results', [])[:30]:
                loc = p.get('geometry', {}).get('location', {})
                d = _haversine_distance(lat, lng, loc.get('lat'), loc.get('lng'))
                results.append({'name': p.get('name'), 'address': p.get('vicinity') or p.get('formatted_address'), 'lat': loc.get('lat'), 'lng': loc.get('lng'), 'place_id': p.get('place_id'), 'distance_m': round(d,1), 'phone': None})
        except Exception:
            pass
    if not results:
        try:
            overpass_url = 'https://overpass-api.de/api/interpreter'
            query = f"[out:json];(node(around:{radius},{lat},{lng})[amenity=hospital];node(around:{radius},{lat},{lng})[amenity=clinic];way(around:{radius},{lat},{lng})[amenity=hospital];way(around:{radius},{lat},{lng})[amenity=clinic];);out center;"
            r = requests.post(overpass_url, data={'data': query}, timeout=10)
            data = r.json()
            for el in data.get('elements', [])[:50]:
                tags = el.get('tags', {})
                name = tags.get('name') or tags.get('operator') or 'Unknown'
                if el.get('type') == 'node':
                    lat2 = el.get('lat')
                    lon2 = el.get('lon')
                else:
                    center = el.get('center', {})
                    lat2 = center.get('lat')
                    lon2 = center.get('lon')
                d = _haversine_distance(lat, lng, lat2, lon2)
                results.append({'name': name, 'address': tags.get('addr:full') or tags.get('addr:street') or '', 'lat': lat2, 'lng': lon2, 'distance_m': round(d,1), 'phone': tags.get('phone') or tags.get('contact:phone')})
        except Exception:
            pass
    results = sorted(results, key=lambda r: r.get('distance_m', 999999))
    return jsonify({'facilities': results[:50]})


@app.route('/doctors_nearby', methods=['POST'])
def doctors_nearby():
    data = request.get_json() or {}
    lat = data.get('lat')
    lng = data.get('lng')
    try:
        if lat is None or lng is None:
            ip = request.headers.get('X-Forwarded-For', request.remote_addr)
            lat, lng = get_location_from_ip(ip)
            if lat is None or lng is None:
                return jsonify({'error': 'Invalid lat/lng'}), 400
        else:
            lat = float(lat)
            lng = float(lng)
    except Exception:
        return jsonify({'error': 'Invalid lat/lng'}), 400
    radius = int(data.get('radius', 5000))
    doctors = []
    try:
        all_docs = Doctor.query.all()
        for d in all_docs:
            if d.lat is None or d.lng is None:
                continue
            dist = _haversine_distance(lat, lng, d.lat, d.lng)
            if dist <= radius:
                doctors.append({'id': d.id, 'name': d.name, 'specialization': d.specialization, 'facility': d.facility_name, 'address': d.facility_address, 'phone': d.phone, 'availability': d.availability, 'distance_m': round(dist,1)})
    except Exception:
        pass
    if doctors:
        doctors = sorted(doctors, key=lambda x: x.get('distance_m', 999999))
        return jsonify({'doctors': doctors})
    fac_resp = nearby_facilities()
    facs = []
    try:
        facs = fac_resp.get_json().get('facilities', [])
    except Exception:
        try:
            facs = fac_resp[0].get('facilities', [])
        except Exception:
            facs = []
    specializations = ['General Practitioner','Cardiologist','Dermatologist','Pediatrician','ENT Specialist','Orthopedic']
    mock = []
    try:
        clinics_data = _CLINICS_DATA if '_CLINICS_DATA' in globals() else []
        # Prefer facilities from response; if none, use our packaged clinic data
        source_facs = facs if facs else clinics_data
        for fi, f in enumerate(source_facs[:10]):
            # f may come from Overpass/Google (dict) or our packaged clinics (dict)
            lat_f = f.get('lat') if isinstance(f, dict) else None
            lng_f = f.get('lng') if isinstance(f, dict) else None
            # If the Overpass element used 'center'
            if not lat_f and isinstance(f.get('center', {}), dict):
                lat_f = f.get('center', {}).get('lat')
                lng_f = f.get('center', {}).get('lon')

            # Get doctors list from the facility entry if present
            doctors_list = []
            if isinstance(f, dict) and f.get('doctors'):
                doctors_list = f.get('doctors')
            elif clinics_data and fi < len(clinics_data):
                doctors_list = clinics_data[fi].get('doctors', [])
            else:
                # Fallback to a single generic doctor entry
                sample_names = ['Smith','Lee','Patel','Garcia','Nguyen','Brown']
                doctors_list = [{'name': f"Dr. {sample_names[fi % len(sample_names)]}", 'specialization': specializations[fi % len(specializations)], 'phone': None, 'availability': 'Mon-Fri 09:00-17:00'}]

            for doc in doctors_list:
                name = doc.get('name') if isinstance(doc, dict) else str(doc)
                spec = doc.get('specialization') if isinstance(doc, dict) else specializations[fi % len(specializations)]
                phone = doc.get('phone') if isinstance(doc, dict) else None
                availability = doc.get('availability') if isinstance(doc, dict) else 'Mon-Fri 09:00-17:00'
                dist = f.get('distance_m', None) if isinstance(f, dict) else None
                facility_name = f.get('name') if isinstance(f, dict) else (clinics_data[fi].get('name') if clinics_data and fi < len(clinics_data) else '')
                address = f.get('address') if isinstance(f, dict) else (clinics_data[fi].get('address') if clinics_data and fi < len(clinics_data) else '')
                mock.append({'name': name, 'specialization': spec, 'facility': facility_name, 'address': address, 'phone': phone, 'availability': availability, 'distance_m': dist, 'lat': lat_f, 'lng': lng_f})
    except Exception:
        pass
    return jsonify({'doctors': mock})


@app.route('/api/book_doctor', methods=['POST'])
def book_doctor():
    """Handle doctor booking requests from the dashboard"""
    try:
        data = request.get_json() or {}
        doctor_name = data.get('doctor_name')
        doctor_phone = data.get('doctor_phone')
        facility = data.get('facility')
        specialization = data.get('specialization')
        appointment_date = data.get('appointment_date')
        appointment_time = data.get('appointment_time')
        health_concern = data.get('health_concern')
        
        # Validate required fields
        if not all([doctor_name, appointment_date, appointment_time, health_concern]):
            return jsonify({'ok': False, 'error': 'Missing required fields'}), 400
        
        # Get current user
        user = current_user if current_user.is_authenticated else None
        user_email = user.email if user else 'anonymous@example.com'
        
        # Log the booking (in production, save to database)
        app.logger.info(f"Doctor Booking: {doctor_name} for {user_email}")
        app.logger.info(f"Date: {appointment_date}, Time: {appointment_time}")
        app.logger.info(f"Facility: {facility}, Specialization: {specialization}")
        app.logger.info(f"Concern: {health_concern}")
        
        # Return success
        return jsonify({
            'ok': True,
            'success': True,
            'message': f'Booking request submitted for {doctor_name} on {appointment_date} at {appointment_time}. The clinic will contact you shortly.',
            'doctor_name': doctor_name,
            'facility': facility,
            'appointment_date': appointment_date,
            'appointment_time': appointment_time
        }), 200
    except Exception as e:
        app.logger.error(f"Booking error: {str(e)}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/location_estimate', methods=['GET'])
def location_estimate():
    """Return an approximate location (lat/lng) inferred from the client's IP address.
    This is a best-effort approximate fallback when browser geolocation is not available/denied.
    """
    try:
        ip = request.headers.get('X-Forwarded-For', request.remote_addr)
        lat, lng = get_location_from_ip(ip)
        if lat is not None and lng is not None:
            return jsonify({'lat': float(lat), 'lng': float(lng)})

        # If the request came from a private/local IP and lookup failed, try to determine
        # the server's external IP and lookup that as a best-effort fallback.
        try:
            # Only attempt external lookup if ip-api failed
            ext = requests.get('https://api.ipify.org?format=json', timeout=3).json().get('ip')
            if ext:
                lat, lng = get_location_from_ip(ext)
                if lat is not None and lng is not None:
                    app.logger.info('Used external IP %s for location_estimate', ext)
                    return jsonify({'lat': float(lat), 'lng': float(lng)})
        except Exception as e:
            app.logger.debug('External IP lookup failed: %s', e)

        # Developer override: return DEV_LOCATION if configured (useful for localhost development)
        if _DEV_LOCATION is not None:
            app.logger.info('Using DEV_LOCATION for location_estimate')
            return jsonify({'lat': float(_DEV_LOCATION[0]), 'lng': float(_DEV_LOCATION[1])})

    except Exception as ex:
        app.logger.exception('location_estimate failure')
    # As a last resort, return nothing and let client handle
    return jsonify({'error': 'Could not determine location'}), 400


@app.route('/api/location_logs', methods=['GET'])
def api_location_logs():
    """Return recent LocationLog entries.
    Optional query params:
      - lat,lng: center to filter by distance (meters)
      - radius: radius in meters (default 2000)
      - since: seconds back to include (default 300)
      - limit: max results (default 100)
    """
    try:
        lat = request.args.get('lat', type=float)
        lng = request.args.get('lng', type=float)
        radius = float(request.args.get('radius', 2000))
        limit = int(request.args.get('limit', 100))
        since = int(request.args.get('since', 300))

        cutoff = datetime.utcnow() - timedelta(seconds=since)
        q = LocationLog.query.filter(LocationLog.timestamp >= cutoff)

        results = []
        if lat is not None and lng is not None:
            # Load candidates and filter by haversine distance
            candidates = q.all()
            for c in candidates:
                try:
                    # Calculate distance in meters
                    d = _haversine_distance(lat, lng, c.latitude, c.longitude)
                except Exception:
                    d = None
                if d is not None and d <= radius:
                    results.append({'id': c.id, 'user_id': c.user_id, 'lat': float(c.latitude), 'lng': float(c.longitude), 'timestamp': c.timestamp.isoformat(), 'distance_m': round(d,1)})
            results = sorted(results, key=lambda x: x.get('timestamp', ''), reverse=True)[:limit]
        else:
            rows = q.order_by(LocationLog.timestamp.desc()).limit(limit).all()
            for c in rows:
                results.append({'id': c.id, 'user_id': c.user_id, 'lat': float(c.latitude), 'lng': float(c.longitude), 'timestamp': c.timestamp.isoformat()})

        return jsonify({'locations': results})
    except Exception as e:
        app.logger.exception('api_location_logs failed')
        return jsonify({'error': 'server failure'}), 500


@app.route('/upload_prescription', methods=['POST'])
def upload_prescription():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 403
    if 'prescription' not in request.files:
        return jsonify({'error': 'No file sent'}), 400
    f = request.files['prescription']
    if f.filename == '':
        return jsonify({'error': 'Empty filename'}), 400
    if not allowed_file(f.filename):
        return jsonify({'error': 'Unsupported file type'}), 400
    filename = secure_filename(f.filename)
    os.makedirs(os.path.join('instance','uploads'), exist_ok=True)
    ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
    save_name = f"pres_{session.get('user_id')}_{ts}_{filename}"
    path = os.path.join('instance','uploads', save_name)
    f.save(path)
    pu = PrescriptionUpload(user_id=session.get('user_id'), filename=save_name)
    db.session.add(pu)
    db.session.commit()
    return jsonify({'ok': True, 'id': pu.id, 'filename': save_name, 'url': url_for('uploaded_file', filename=save_name)})


@app.route('/create_prescription', methods=['GET','POST'])
def create_prescription():
    if 'user_id' not in session:
        return redirect(url_for('login'))

    if request.method == 'POST':
        # Collect clinical-style input
        patient_name = request.form.get('patient_name', '').strip()
        age = request.form.get('age', '').strip()
        gender = request.form.get('gender', '').strip()
        doctor_name = request.form.get('doctor_name', '').strip() or session.get('username')
        medications = request.form.get('medications', '').strip()
        diagnosis = request.form.get('diagnosis', '').strip()
        instructions = request.form.get('instructions', '').strip()
        symptoms = request.form.get('symptoms', '').strip()
        ai_generate = bool(request.form.get('ai_generate'))

        if not patient_name:
            return render_template('create_prescription.html', error='Patient name is required', patient_name=patient_name, age=age, gender=gender, medications=medications, diagnosis=diagnosis, instructions=instructions, doctor_name=doctor_name, symptoms=symptoms)

        # If user requested AI generation, call the LLM to suggest medications/diagnosis/instructions
        ai_used = False
        if ai_generate:
            if not symptoms:
                return render_template('create_prescription.html', error='Please provide symptoms for AI to generate a draft', patient_name=patient_name, age=age, gender=gender, medications=medications, diagnosis=diagnosis, instructions=instructions, doctor_name=doctor_name, symptoms=symptoms)
            try:
                prompt = (
                    "You are a cautious medical assistant. Given a short clinical presentation, produce a suggested prescription draft. "
                    "Return JSON only with fields: {\"medications\": [\"Drug - dose - frequency (duration)\", ...], \"diagnosis\": \"text\", \"instructions\": \"text\"}. "
                    "Do NOT provide any definitive medical advice — include a short disclaimer field if necessary.\n\n"
                    f"Clinical presentation: {symptoms}\n"
                )
                ai_text = generate_response(prompt, temperature=0.1, max_tokens=300)
                ai_used = True
                # Try to parse JSON from the model
                try:
                    parsed = json.loads(ai_text)
                    meds_list = parsed.get('medications') or []
                    if isinstance(meds_list, list):
                        medications = '\n'.join(meds_list)
                    else:
                        medications = str(meds_list)
                    diagnosis = parsed.get('diagnosis', diagnosis)
                    instructions = parsed.get('instructions', instructions)
                except Exception:
                    # Fallback: try to extract lines from raw text
                    lines = [ln.strip() for ln in (ai_text or '').splitlines() if ln.strip()]
                    meds = []
                    for ln in lines:
                        if ln.lower().startswith('med') or ln.startswith('-') or any(ch.isdigit() for ch in ln):
                            meds.append(ln)
                    if meds:
                        medications = '\n'.join(meds)
                    else:
                        # as last resort, put entire AI text into instructions
                        instructions = (instructions + '\n\nAI draft:\n' + ai_text) if ai_text else instructions
            except Exception as e:
                app.logger.exception('AI generation failed')
                return render_template('create_prescription.html', error='AI generation failed: ' + str(e), patient_name=patient_name, age=age, gender=gender, medications=medications, diagnosis=diagnosis, instructions=instructions, doctor_name=doctor_name, symptoms=symptoms)

        # Build lines for prescription image
        lines = []
        lines.append(f"Patient: {patient_name}    Age: {age or 'N/A'}    Gender: {gender or 'N/A'}")
        # Mark AI generation in the header if used
        if ai_used:
            lines.append(f"Prescribing Physician: {doctor_name} (AI-generated draft)")
        else:
            lines.append(f"Prescribing Physician: {doctor_name}")
        lines.append(f"Date: {datetime.utcnow().strftime('%Y-%m-%d')}")
        lines.append("")
        lines.append("Medications:")
        for m in medications.split('\n'):
            m = m.strip()
            if m:
                lines.append(f" - {m}")
        if diagnosis:
            lines.append("")
            lines.append(f"Diagnosis: {diagnosis}")
        if instructions:
            lines.append("")
            lines.append(f"Instructions: {instructions}")

        # Save image
        ts = datetime.utcnow().strftime('%Y%m%d%H%M%S')
        filename = f"genpres_{session.get('user_id')}_{ts}.png"
        save_dir = os.path.join('instance','uploads')
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, filename)
        try:
            render_prescription_image(lines, save_path)
        except Exception as e:
            app.logger.exception('Failed to render prescription image')
            return render_template('create_prescription.html', error='Failed to create prescription image: ' + str(e))

        # Persist record
        gp = GeneratedPrescription(user_id=session.get('user_id'), filename=filename, doctor_name=doctor_name, patient_name=patient_name, patient_age=age, patient_gender=gender, medications=medications, diagnosis=diagnosis, instructions=instructions)
        db.session.add(gp)
        db.session.commit()

        return redirect(url_for('generated_prescriptions'))

    return render_template('create_prescription.html')


@app.route('/generated_prescriptions')
def generated_prescriptions():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    uid = session.get('user_id')
    items = GeneratedPrescription.query.filter_by(user_id=uid).order_by(GeneratedPrescription.created_at.desc()).all()
    uploads = []
    for p in items:
        uploads.append({'id': p.id, 'filename': p.filename, 'created_at': p.created_at, 'url': url_for('uploaded_file', filename=p.filename), 'is_cancelled': p.is_cancelled, 'doctor_name': p.doctor_name, 'patient_name': p.patient_name})
    return render_template('generated_prescriptions.html', uploads=uploads)


@app.route('/cancel_generated_prescription', methods=['POST'])
def cancel_generated_prescription():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 403
    data = request.get_json() or {}
    pid = data.get('id')
    if not pid:
        return jsonify({'error': 'Missing id'}), 400
    gp = GeneratedPrescription.query.get(pid)
    if not gp or gp.user_id != session.get('user_id'):
        return jsonify({'error': 'Not found'}), 404
    gp.is_cancelled = True
    db.session.commit()
    return jsonify({'ok': True, 'id': gp.id})


@app.route('/ack_prescription', methods=['POST'])
def ack_prescription():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 403
    data = request.get_json() or {}
    pid = data.get('id')
    if not pid:
        return jsonify({'error': 'Missing id'}), 400
    pu = PrescriptionUpload.query.get(pid)
    if not pu or pu.user_id != session.get('user_id'):
        return jsonify({'error': 'Not found'}), 404
    pu.acknowledged = True
    pu.acknowledged_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'ok': True, 'id': pu.id})


@app.route('/my_prescriptions')
def my_prescriptions():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    uid = session.get('user_id')
    items = PrescriptionUpload.query.filter_by(user_id=uid).order_by(PrescriptionUpload.uploaded_at.desc()).all()
    uploads = [{'id': p.id, 'filename': p.filename, 'uploaded_at': p.uploaded_at, 'acknowledged': p.acknowledged, 'url': url_for('uploaded_file', filename=p.filename)} for p in items]
    return render_template('my_prescriptions.html', uploads=uploads)


@app.route('/api/reminders', methods=['GET','POST','PUT'])
def api_reminders():
    if 'user_id' not in session:
        return jsonify({'error': 'Authentication required'}), 403
    uid = session.get('user_id')
    if request.method == 'GET':
        rems = MedicationReminder.query.filter_by(user_id=uid).all()
        out = [{'id': r.id, 'medication_name': r.medication_name, 'dosage': r.dosage, 'frequency': r.frequency, 'reminder_time': r.reminder_time, 'is_active': r.is_active} for r in rems]
        return jsonify({'reminders': out})
    data = request.get_json() or {}
    if request.method == 'POST':
        name = data.get('medication_name')
        dosage = data.get('dosage')
        freq = data.get('frequency')
        time_str = data.get('reminder_time')
        if not name:
            return jsonify({'error': 'Missing medication_name'}), 400
        r = MedicationReminder(user_id=uid, medication_name=name, dosage=dosage, frequency=freq, reminder_time=time_str, is_active=True)
        db.session.add(r)
        db.session.commit()
        return jsonify({'ok': True, 'id': r.id})
    if request.method == 'PUT':
        rid = data.get('id')
        r = MedicationReminder.query.get(rid)
        if not r or r.user_id != uid:
            return jsonify({'error': 'Not found'}), 404
        r.medication_name = data.get('medication_name', r.medication_name)
        r.dosage = data.get('dosage', r.dosage)
        r.frequency = data.get('frequency', r.frequency)
        r.reminder_time = data.get('reminder_time', r.reminder_time)
        r.is_active = data.get('is_active', r.is_active)
        db.session.commit()
        return jsonify({'ok': True})





def _send_email(to_address, subject, body):
    """Send a simple plaintext email using SMTP configured via env vars.
    Returns (True, message) on success, (False, error_message) on failure.
    """
    smtp_host = os.environ.get('SMTP_HOST')
    smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    smtp_user = os.environ.get('SMTP_USER')
    smtp_pass = os.environ.get('SMTP_PASS')
    from_email = os.environ.get('FROM_EMAIL', smtp_user or 'no-reply@example.com')
    use_tls = os.environ.get('SMTP_USE_TLS', '1') != '0'

    if not smtp_host or not smtp_user or not smtp_pass:
        app.logger.warning('SMTP not configured; skipping email send')
        return False, 'SMTP not configured'

    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = from_email
    msg['To'] = to_address
    msg.set_content(body)

    try:
        if use_tls:
            context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls(context=context)
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=10) as server:
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        return True, 'Email sent'
    except Exception as e:
        app.logger.exception('Failed to send email')
        return False, str(e)




@app.route('/send_emergency_alert', methods=['POST'])
def send_emergency_alert():
    if 'user_id' not in session:
        return jsonify({'error': 'Please login first'})
    
    user = User.query.get(session['user_id'])
    
    # Simulate emergency alert
    alert_data = {
        'message': f'Emergency alert sent for {user.username}',
        'timestamp': datetime.utcnow().isoformat(),
        'status': 'Alert sent to emergency contacts',
        'user_location': request.json.get('location', 'Unknown') if request.is_json else 'Unknown'
    }

    # Attempt to email the user about the emergency alert
    email_subject = f"Emergency alert for {user.username}"
    email_body = (
        f"{alert_data['message']}\n\n"
        f"Status: {alert_data['status']}\n"
        f"Timestamp: {alert_data['timestamp']}\n"
        f"Location: {alert_data['user_location']}\n"
    )

    email_sent = False
    email_error = None
    try:
        if user and user.email:
            ok, msg = _send_email(user.email, email_subject, email_body)
            email_sent = bool(ok)
            email_error = None if ok else msg
        else:
            app.logger.warning('User has no email; cannot send emergency notification')
            email_error = 'No user email configured'
    except Exception as e:
        app.logger.exception('Error while attempting to send emergency email')
        email_error = str(e)

    alert_data['email_sent'] = email_sent
    if email_error:
        alert_data['email_error'] = email_error

    return jsonify(alert_data)

@app.route('/voice_assistant', methods=['POST'])
def voice_assistant():
    if 'user_id' not in session:
        return jsonify({'error': 'Please login first'})
    
    try:
        # Accept a JSON payload with a `transcript` field from the browser
        if request.is_json:
            data = request.get_json()
            transcript = data.get('transcript', '').strip()
            if not transcript:
                return jsonify({'error': 'No transcript provided'}), 400

            # Process the command locally and return a short response.
            response_text = process_voice_command(transcript)
            return jsonify({'response': response_text, 'transcript': transcript})

        # If not JSON, return a helpful error
        return jsonify({'error': 'Expected JSON with a `transcript` field.'}), 400
    except Exception as e:
        # Log the error and return a generic message
        app.logger.exception('voice_assistant error')
        return jsonify({'error': 'Internal server error'}), 500

def process_voice_command(command):
    command = command.lower()
    
    if 'appointment' in command:
        return "I can help you book an appointment. Please use the appointment booking page."
    elif 'symptom' in command or 'pain' in command:
        return "I can check your symptoms. Please describe them in the symptom checker."
    elif 'medicine' in command or 'medication' in command:
        return "I can set up medication reminders for you."
    elif 'emergency' in command:
        return "I can help with emergency services. Please use the emergency button if needed."
    else:
        return "I'm here to help with your healthcare needs. How can I assist you?"

@app.route('/chatbot', methods=['POST'])
def chatbot():
    try:
        if 'user_id' not in session:
            return jsonify({'error': 'Please login first'})

        user_message = request.json.get('message', '')
        # If enabled, forward the user message to Gemini for a richer response.
        try:
            use_gemini = os.environ.get('USE_GEMINI', '0')
            if use_gemini == '1' and user_message.strip():
                prompt = (
                    "You are a concise, cautious medical assistant interacting with a user. "
                    "Provide helpful, non-judgmental, and safety-conscious health guidance. "
                    "Keep responses short and include red flags when they apply.\n\n"
                    f"User: {user_message}\n\nAssistant:" 
                )
                try:
                    ai_resp = generate_response(prompt, temperature=0.2, max_tokens=300)
                    # In case the LLM returns a structured object, coerce to string
                    if ai_resp:
                        return jsonify({'response': str(ai_resp)})
                except Exception as exc:
                    print('Gemini chatbot error:', exc)
        except Exception as e:
            # Log and fall back to simple responses
            print('Gemini chatbot error:', e)

        # Improved fallback responses with safe, non-prescriptive guidance and disclaimers.
        msg = user_message.lower()

        # Simple intent/keyword mapping (expandable). Use substring checks to tolerate minor typos.
        if 'fever' in msg:
            response = (
                "A fever often indicates an infection. General self-care includes rest, hydration, and "
                "measuring temperature. For symptomatic relief, over-the-counter options commonly used are "
                "paracetamol (acetaminophen) or ibuprofen — follow the package instructions and consult a "
                "pharmacist or doctor before use, especially for children, pregnancy, or other medical conditions. "
                "If you have very high fever, difficulty breathing, severe headache, neck stiffness, or other "
                "worrisome signs, seek urgent medical care."
            )
            return jsonify({'response': response})

        if 'cold' in msg or 'cough' in msg or 'sore throat' in msg:
            response = (
                "Sounds like cold-like symptoms. Rest, fluids, saline nasal spray, throat lozenges and paracetamol/ibuprofen "
                "for discomfort can help. If you have difficulty breathing, high fever, signs of dehydration, or symptoms "
                "worsen or persist beyond a week, see a healthcare professional."
            )
            return jsonify({'response': response})

        if 'headache' in msg or 'migraine' in msg:
            response = (
                "For headache, rest in a quiet/dark room, stay hydrated, and consider over-the-counter pain relief such as "
                "paracetamol or ibuprofen if appropriate. Severe or sudden-onset headaches, headaches with fever, confusion, "
                "weakness, or visual changes warrant immediate medical evaluation."
            )
            return jsonify({'response': response})

        # Match medicine requests including common misspellings like 'madicines', 'medicines', 'medicin'
        if 'medic' in msg or 'medicines' in msg or 'madicin' in msg or 'madicines' in msg:
            response = (
                "I can provide general information about common over-the-counter options, but I cannot prescribe. "
                "Common OTC choices for fever/pain include paracetamol (acetaminophen) and ibuprofen. For coughs/colds, "
                "symptomatic measures (rest, fluids, saline) are usually recommended. Tell me what symptoms you're most "
                "concerned about and any medications or allergies you have, so I can give safer, more specific guidance."
            )
            return jsonify({'response': response})

        if 'appointment' in msg:
            return jsonify({'response': 'You can book appointments with doctors through our booking system.'})

        if 'emergency' in msg or 'urgent' in msg or 'help now' in msg:
            return jsonify({'response': 'For emergencies, please call your local emergency number or use the emergency services section immediately.'})

        # Fallback generic prompt requesting more detail
        return jsonify({'response': "I understand you're concerned about your health. Could you provide more details (age, duration, key symptoms)?"})
    except Exception as e:
        # Return full traceback in response for debugging (temporary)
        import traceback
        tb = traceback.format_exc()
        return jsonify({'error': 'Internal Server Error', 'traceback': tb}), 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# Global error handler: log full tracebacks to error.log and return a generic 500 message
import traceback
import logging
logger = logging.getLogger('healthcare_app')
logger.setLevel(logging.INFO)
if not logger.handlers:
    fh = logging.FileHandler('error.log')
    fh.setLevel(logging.ERROR)
    logger.addHandler(fh)


@app.route('/analyze_image', methods=['POST'])
def analyze_image():
    """Analyze an uploaded image for simple wound/rash indicators.
    This is heuristic and not diagnostic. Returns JSON with findings.
    """
    # Accept file in multipart/form-data under 'image'
    if 'image' not in request.files:
        return jsonify({'error': 'No image provided'}), 400

    file = request.files['image']
    if not file or file.filename == '':
        return jsonify({'error': 'Empty filename'}), 400

    filename = secure_filename(file.filename)
    # Process in-memory where possible
    try:
        from io import BytesIO
        try:
            from PIL import Image
        except Exception:
            # Pillow not available — return mock response
            return jsonify({'warning': 'Pillow not installed; analysis skipped', 'findings': []})

        img_bytes = file.read()
        img = Image.open(BytesIO(img_bytes)).convert('RGB')
        # Resize for speed
        img_small = img.copy()
        img_small.thumbnail((400, 400))
        pixels = list(img_small.getdata())
        total = len(pixels)

        red_pixels = 0
        yellow_pixels = 0
        blue_pixels = 0
        var_samples = []

        for (r, g, b) in pixels:
            # red-dominant (possible bleeding/inflammation)
            if r > 140 and r > g + 30 and r > b + 30:
                red_pixels += 1
            # yellow-ish (pus/scab/yellow discoloration)
            if r > 180 and g > 140 and b < 120:
                yellow_pixels += 1
            # bluish/purple (bruising)
            if b > 110 and r > 70 and g < 120 and r < 200:
                blue_pixels += 1
            var_samples.append((r, g, b))

        # basic color variance as proxy for rash texture
        import statistics
        rs = [p[0] for p in var_samples]
        gs = [p[1] for p in var_samples]
        bs = [p[2] for p in var_samples]
        try:
            var_rgb = statistics.pstdev(rs) + statistics.pstdev(gs) + statistics.pstdev(bs)
        except Exception:
            var_rgb = 0

        findings = []
        def pct(n):
            return 0 if total == 0 else (n / total) * 100

        if pct(red_pixels) > 3.0:
            findings.append({'type': 'red_areas', 'message': 'Significant red areas detected — possible bleeding or active inflammation', 'percent': round(pct(red_pixels),2)})
        if pct(yellow_pixels) > 1.0:
            findings.append({'type': 'yellow_areas', 'message': 'Yellowish discoloration detected — possible scab, crust, or infection', 'percent': round(pct(yellow_pixels),2)})
        if pct(blue_pixels) > 1.0:
            findings.append({'type': 'blue_areas', 'message': 'Bluish/purplish areas detected — possible bruising', 'percent': round(pct(blue_pixels),2)})
        # rash heuristic: moderate color variance and not dominated by single color
        if var_rgb > 30 and len(findings) == 0:
            findings.append({'type': 'rash_like', 'message': 'Texture/color variation suggests possible rash or skin irritation', 'variance': round(var_rgb,2)})

        # If nothing detected, provide cautious guidance
        if not findings:
            findings.append({'type': 'no_obvious_sign', 'message': 'No obvious wound/bleeding/bruising detected. Visual analysis is limited; consult a clinician for diagnosis.'})

        # Always add allergy guidance disclaimer
        findings.append({'type': 'allergy_note', 'message': "Allergy cannot be reliably diagnosed from an image — check for hives, swelling, breathing issues and seek medical care if suspicious."})

        return jsonify({'findings': findings})

    except Exception:
        app.logger.exception('Image analysis failed')
        return jsonify({'error': 'Internal error during image analysis'}), 500


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    tb = traceback.format_exc()
    logger.error(tb)
    # Return a generic 500 page (do not expose internals in production)
    return ("<h1>Internal Server Error</h1>\n" \
            f"<p>{str(e)}</p>\n" \
            "<p>Details have been written to error.log</p>"), 500

# Register medication reminder Blueprint (from medication_reminder.py)
try:
    from medication_reminder import medication_bp
    app.register_blueprint(medication_bp)
except Exception as _:
    app.logger.info('Medication reminder blueprint not registered: %s', _)

if __name__ == '__main__':
    # Allow disabling the automatic reloader for stable automated runs
    no_reload = os.environ.get('NO_RELOAD', '0') == '1'
    # If NO_RELOAD is set, run without debug/reloader to avoid restarts during tests
    if no_reload:
        app.run(debug=False, use_reloader=False, host='0.0.0.0', port=5000)
    else:
        # default: preserve debug behavior for development
        app.run(debug=True, host='0.0.0.0', port=5000)