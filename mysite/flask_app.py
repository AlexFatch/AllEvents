from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3 # <--- The lightweight DB
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = 'super_secret_bouncer_key'
DB_NAME = 'smart_recovery.db' # This will be a file on your disk

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row # Allows accessing columns by name
    return conn

# --- AUTOMATIC SETUP (Run once on startup) ---
def init_db():
    if not os.path.exists(DB_NAME):
        conn = get_db_connection()
        # Create Tables automatically
        conn.execute('''
            CREATE TABLE IF NOT EXISTS EventTickets (
                TicketID INTEGER PRIMARY KEY AUTOINCREMENT,
                TicketCode TEXT NOT NULL UNIQUE,
                EventName TEXT DEFAULT 'General Event',
                TicketType TEXT DEFAULT 'Standard',
                IsUsed INTEGER DEFAULT 0,
                ScannedAt TIMESTAMP,
                GuestName TEXT,
                CreatedAt TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        print("✅ New SQLite Database Created!")

# Initialize DB immediately
init_db()

# --- CORE ROUTES ---

@app.route('/')
def home():
    if 'is_bouncer' not in session:
        return render_template('login.html')
    return render_template('dashboard.html')

@app.route('/login', methods=['POST'])
def login():
    password = request.form.get('password')
    if password == 'admin123':
        session['is_bouncer'] = True
        return redirect(url_for('home'))
    return "<h3>Wrong Password</h3><a href='/'>Try Again</a>"

@app.route('/logout')
def logout():
    session.pop('is_bouncer', None)
    return redirect(url_for('home'))

@app.route('/status')
def status_check():
    # Simple heartbeat
    return "OK", 200

# --- ADMIN ROUTES ---

@app.route('/admin')
def admin_dashboard():
    if 'is_bouncer' not in session: return redirect(url_for('login'))

    conn = get_db_connection()
    # SQLite count queries
    total = conn.execute("SELECT COUNT(*) FROM EventTickets").fetchone()[0]
    inside = conn.execute("SELECT COUNT(*) FROM EventTickets WHERE IsUsed = 1").fetchone()[0]
    
    row = conn.execute("SELECT EventName FROM EventTickets LIMIT 1").fetchone()
    event_name = row['EventName'] if row else "No Event"

    tickets = conn.execute("SELECT * FROM EventTickets ORDER BY ScannedAt DESC LIMIT 50").fetchall()
    conn.close()
    
    return render_template('admin.html', total=total, inside=inside, pending=total-inside, event_name=event_name, tickets=tickets)

@app.route('/admin/reset_all', methods=['POST'])
def reset_all_tickets():
    if 'is_bouncer' not in session: return "Access Denied", 403
    conn = get_db_connection()
    conn.execute("UPDATE EventTickets SET IsUsed = 0, ScannedAt = NULL")
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/rename', methods=['POST'])
def rename_event():
    if 'is_bouncer' not in session: return "Access Denied", 403
    new_name = request.form.get('new_name')
    conn = get_db_connection()
    conn.execute("UPDATE EventTickets SET EventName = ?", (new_name,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/toggle/<int:ticket_id>')
def toggle_ticket(ticket_id):
    if 'is_bouncer' not in session: return "Access Denied", 403
    conn = get_db_connection()
    # Toggle logic for SQLite
    conn.execute("""
        UPDATE EventTickets 
        SET IsUsed = CASE WHEN IsUsed = 1 THEN 0 ELSE 1 END,
            ScannedAt = CASE WHEN IsUsed = 1 THEN NULL ELSE CURRENT_TIMESTAMP END
        WHERE TicketID = ?
    """, (ticket_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))

# --- SCANNING ROUTES ---

@app.route('/gate/<ticket_code>')
def scan_ticket(ticket_code):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM EventTickets WHERE TicketCode = ?", (ticket_code,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return render_template('gate_result.html', status='FAKE', message="Invalid Ticket", color='red')

    # SQLite rows behave like dictionaries
    ticket_id = row['TicketID']
    is_used = row['IsUsed']
    guest_name = row['GuestName']
    event_name = row['EventName']
    ticket_type = row['TicketType']

    if 'is_bouncer' in session:
        if is_used:
            conn.close()
            return render_template('gate_result.html', status='DENIED', message="Already Used!", guest=guest_name, ticket_type=ticket_type, color='red')
        
        conn.execute("UPDATE EventTickets SET IsUsed = 1, ScannedAt = CURRENT_TIMESTAMP WHERE TicketID = ?", (ticket_id,))
        conn.commit()
        conn.close()
        return render_template('gate_result.html', status='GRANTED', message="Welcome!", guest=guest_name, ticket_type=ticket_type, color='green')
    
    else:
        conn.close()
        return render_template('guest_invite.html', guest=guest_name, event=event_name, status=is_used)

@app.route('/manual_check', methods=['POST'])
def manual_check():
    raw_input = request.form.get('ticket_code', '').strip()
    if "/gate/" in raw_input:
        code = raw_input.split("/gate/")[-1]
    else:
        code = raw_input
    return redirect(url_for('scan_ticket', ticket_code=code))

# Safety Net
@app.route('/<path:undefined_path>')
def catch_all(undefined_path):
    if undefined_path in ['favicon.ico', 'static', 'status']: return "Not Found", 404
    return redirect(url_for('scan_ticket', ticket_code=undefined_path))

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)