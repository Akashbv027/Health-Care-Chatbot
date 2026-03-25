from flask import Blueprint, render_template, request, redirect, url_for, session

# Create a Blueprint for medication reminders
medication_bp = Blueprint('medication', __name__)


@medication_bp.route('/medication_reminders', methods=['GET', 'POST'])
def medication_reminders():
    # Import here to avoid circular imports
    from app import db, MedicationReminder
    
    # Require login (app uses session['user_id'])
    if 'user_id' not in session:
        return redirect(url_for('login'))

    error = None
    if request.method == 'POST':
        name = request.form.get('medication_name', '').strip()
        dosage = request.form.get('dosage', '').strip()
        frequency = request.form.get('frequency', '').strip()
        reminder_time = request.form.get('reminder_time', '').strip()

        if not name:
            error = 'Medication name is required.'
        else:
            try:
                mr = MedicationReminder(
                    user_id=session['user_id'],
                    medication_name=name,
                    dosage=dosage or None,
                    frequency=frequency or None,
                    reminder_time=reminder_time or None,
                    is_active=True
                )
                db.session.add(mr)
                db.session.commit()
                return redirect(url_for('medication.medication_reminders'))
            except Exception as e:
                db.session.rollback()
                error = 'Failed to save reminder: ' + str(e)

    reminders = []
    try:
        from app import MedicationReminder
        reminders = MedicationReminder.query.filter_by(user_id=session.get('user_id')).order_by(MedicationReminder.id.desc()).all()
    except Exception:
        reminders = []

    return render_template('medication_reminders.html', reminders=reminders, error=error)


@medication_bp.route('/medication_reminders/delete/<int:reminder_id>', methods=['POST'])
def delete_medication_reminder(reminder_id):
    from app import db, MedicationReminder
    
    if 'user_id' not in session:
        return redirect(url_for('login'))
    try:
        mr = MedicationReminder.query.get(reminder_id)
        if mr and mr.user_id == session.get('user_id'):
            db.session.delete(mr)
            db.session.commit()
        return redirect(url_for('medication.medication_reminders'))
    except Exception:
        db.session.rollback()
        return redirect(url_for('medication.medication_reminders'))

