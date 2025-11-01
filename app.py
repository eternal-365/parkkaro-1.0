from flask import Flask, render_template, request, redirect, url_for, session, jsonify
import pickle
import cv2
import cvzone
import numpy as np
import threading
import time
import os
import sqlite3
import qrcode
import secrets
import io
import base64
from datetime import datetime, timedelta
from charging_monitor import charging_monitor

import random

app = Flask(__name__)
app.secret_key = 'parkaro-secret-2024'

# Import and initialize the parking detector
from parking_detector import ParkingDetector


@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response


# Global parking detector instance
parking_detector = ParkingDetector()

# Global variables to store parking data
parking_data = {
    'free_spaces': 0,
    'total_spaces': 0,
    'free_lots': [],
    'occupied_lots': [],
    'last_update': 'Never'
}

# Load parking spaces
try:
    with open('assets/positions.pkl', 'rb') as f:
        posList = pickle.load(f)
    print(f"✅ Loaded {len(posList)} parking spaces")
except Exception as e:
    posList = []
    print(f"❌ No parking spaces defined: {e}")

width, height = 107, 48


# Database setup
def init_db():
    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()

    # Create users table with additional fields
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            qr_code TEXT UNIQUE NOT NULL,
            qr_image TEXT,
            vehicle_type TEXT DEFAULT 'ev',
            phone TEXT,
            pan_card TEXT,
            driving_license TEXT,
            vehicle_number TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Create parking_sessions table
    c.execute('''
        CREATE TABLE IF NOT EXISTS parking_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            qr_code TEXT,
            lot_number INTEGER,
            check_in_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            check_out_time TIMESTAMP,
            status TEXT DEFAULT 'active',
            total_amount REAL DEFAULT 0.0,
            duration_minutes INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users (id)
        )
    ''')

    # Create charging_sessions table
    c.execute('''
        CREATE TABLE IF NOT EXISTS charging_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            parking_session_id INTEGER,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            start_charge_level INTEGER DEFAULT 0,
            end_charge_level INTEGER DEFAULT 0,
            current_charge_level INTEGER DEFAULT 0,
            status TEXT DEFAULT 'active',
            charging_rate REAL DEFAULT 1.0, -- kW
            total_energy REAL DEFAULT 0.0, -- kWh
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (parking_session_id) REFERENCES parking_sessions (id)
        )
    ''')

    c.execute('''
            CREATE TABLE IF NOT EXISTS charging_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                parking_session_id INTEGER,
                start_charge_level INTEGER DEFAULT 0,
                current_charge_level INTEGER DEFAULT 0,
                end_charge_level INTEGER,
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP,
                status TEXT DEFAULT 'active',
                charging_rate REAL DEFAULT 7.4,
                total_energy REAL DEFAULT 0,
                FOREIGN KEY (user_id) REFERENCES users (id),
                FOREIGN KEY (parking_session_id) REFERENCES parking_sessions (id)
            )
        ''')


    # Create payments table
    c.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            session_id INTEGER,
            amount REAL NOT NULL,
            payment_status TEXT DEFAULT 'pending',
            payment_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users (id),
            FOREIGN KEY (session_id) REFERENCES parking_sessions (id)
        )
    ''')

    # Check if we need to add missing columns to users table
    try:
        c.execute("PRAGMA table_info(users)")
        columns = [column[1] for column in c.fetchall()]

        # Add missing columns if they don't exist
        missing_columns = []
        if 'pan_card' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN pan_card TEXT')
            missing_columns.append('pan_card')

        if 'driving_license' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN driving_license TEXT')
            missing_columns.append('driving_license')

        if 'vehicle_number' not in columns:
            c.execute('ALTER TABLE users ADD COLUMN vehicle_number TEXT')
            missing_columns.append('vehicle_number')

        if missing_columns:
            print(f"🔄 Added columns to users table: {missing_columns}")
        # Check if we need to add completion_time column
        if 'completion_time' not in columns:
            c.execute('ALTER TABLE charging_sessions ADD COLUMN completion_time TIMESTAMP')
            print("✅ Added completion_time column to charging_sessions")

    except Exception as e:
        print(f"⚠️ Database upgrade check failed: {e}")

    conn.commit()
    conn.close()
    print("✅ Database initialized and upgraded")


def generate_unique_qr():
    """Generate a unique QR code identifier"""
    return f"PARKARO_{secrets.token_hex(8)}"


def create_qr_image(qr_data):
    """Create QR code image and return as base64"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(qr_data)
        qr.make(fit=True)

        img = qr.make_image(fill_color="black", back_color="white")
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        return f"data:image/png;base64,{img_str}"

    except Exception as e:
        print(f"Error creating QR image: {e}")
        return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="


def sync_parking_status():
    """Sync parking detector data with global parking_data"""
    while True:
        try:
            detector_data = parking_detector.get_parking_data()

            conn = sqlite3.connect('parking_system.db')
            c = conn.cursor()
            c.execute('SELECT lot_number FROM parking_sessions WHERE status = "active"')
            occupied_lots_db = [row[0] for row in c.fetchall()]
            conn.close()

            all_lots = list(range(1, len(posList) + 1)) if posList else []
            physically_occupied = detector_data['occupied_lots']
            reserved_occupied = occupied_lots_db

            combined_occupied = list(set(physically_occupied + reserved_occupied))
            combined_free = [lot for lot in all_lots if lot not in combined_occupied]

            parking_data.update({
                'free_spaces': len(combined_free),
                'total_spaces': len(posList),
                'free_lots': combined_free,
                'occupied_lots': combined_occupied,
                'last_update': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            })

            print(f"🅿️  Status: {len(combined_free)}/{len(posList)} free")

        except Exception as e:
            print(f"Error in parking sync: {e}")

        time.sleep(3)


# Routes
@app.route('/')
def index():
    return render_template('index.html', parking_data=parking_data)


@app.route('/register')
def register_page():
    return render_template('register.html')


@app.route('/login')
def login_page():
    return render_template('login.html')


@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login_page'))

    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()
    c.execute('SELECT username, qr_code, qr_image, vehicle_type FROM users WHERE id = ?', (session['user_id'],))
    user = c.fetchone()

    if not user:
        conn.close()
        session.clear()
        return redirect(url_for('login_page'))

    username, qr_code, qr_image, vehicle_type = user

    c.execute('SELECT lot_number, check_in_time FROM parking_sessions WHERE user_id = ? AND status = "active"',
              (session['user_id'],))
    active_session = c.fetchone()
    conn.close()

    return render_template('dashboard.html',
                           username=username,
                           qr_code=qr_code,
                           qr_image=qr_image,
                           vehicle_type=vehicle_type,
                           active_session=active_session,
                           parking_data=parking_data)


@app.route('/scanning-station')
def scanning_station():
    return render_template('scanning_station.html', parking_data=parking_data)


@app.route('/parking-map')
def parking_map():
    return render_template('parking_map.html', parking_data=parking_data)


# API Routes
@app.route('/api/parking-status')
def api_parking_status():
    return jsonify(parking_data)


@app.route('/api/register', methods=['POST'])
def api_register():
    """API for user registration with additional details"""
    data = request.json
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')
    vehicle_type = data.get('vehicle_type', 'ev')
    phone = data.get('phone', '')
    pan_card = data.get('pan_card', '')
    driving_license = data.get('driving_license', '')
    vehicle_number = data.get('vehicle_number', '')

    # Validate required fields
    if not all([username, email, password]):
        return jsonify({'error': 'Missing required fields'}), 400

    # Validate PAN card format (basic validation)
    if pan_card and len(pan_card) != 10:
        return jsonify({'error': 'PAN card must be 10 characters long'}), 400

    # Generate unique QR code
    qr_data = generate_unique_qr()
    qr_image = create_qr_image(qr_data)

    print(f"🔐 Registering user: {username}")
    print(f"📱 Generated QR code: {qr_data}")

    try:
        conn = sqlite3.connect('parking_system.db')
        c = conn.cursor()
        c.execute('''
            INSERT INTO users (username, email, password, qr_code, qr_image, vehicle_type, phone, pan_card, driving_license, vehicle_number) 
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (username, email, password, qr_data, qr_image, vehicle_type, phone, pan_card, driving_license, vehicle_number))

        user_id = c.lastrowid
        conn.commit()
        conn.close()

        # Auto-login after registration
        session['user_id'] = user_id
        session['username'] = username

        print(f"✅ User {username} registered successfully with ID {user_id}")

        return jsonify({
            'success': True,
            'message': 'Registration successful!',
            'user_id': user_id,
            'username': username
        })

    except sqlite3.IntegrityError as e:
        print(f"❌ Registration error: {e}")
        return jsonify({'error': 'Username or email already exists'}), 400
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        return jsonify({'error': 'Registration failed'}), 500


@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()
    c.execute('SELECT id, username FROM users WHERE username = ? AND password = ?', (username, password))
    user = c.fetchone()
    conn.close()

    if user:
        session['user_id'] = user[0]
        session['username'] = user[1]
        return jsonify({
            'success': True,
            'message': 'Login successful!',
            'user_id': user[0],
            'username': user[1]
        })
    else:
        return jsonify({'error': 'Invalid credentials'}), 401


@app.route('/api/user/profile')
def api_user_profile():
    """Get current user profile with all details"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()
    c.execute('''
        SELECT username, email, vehicle_type, qr_image, phone, pan_card, driving_license, vehicle_number 
        FROM users WHERE id = ?
    ''', (session['user_id'],))
    user = c.fetchone()
    conn.close()

    if user:
        return jsonify({
            'username': user[0],
            'email': user[1],
            'vehicle_type': user[2],
            'qr_image': user[3],
            'phone': user[4],
            'pan_card': user[5],
            'driving_license': user[6],
            'vehicle_number': user[7]
        })
    return jsonify({'error': 'User not found'}), 404


@app.route('/api/scan-qr', methods=['POST'])
def api_scan_qr():
    """API to scan QR code for both check-in and check-out"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        qr_code = data.get('qr_code', '').strip()
        print(f"🔍 Scanning QR code: {qr_code}")

        if not qr_code:
            return jsonify({'error': 'No QR code provided'}), 400

        # Create ONE database connection for the entire function
        conn = sqlite3.connect('parking_system.db')
        c = conn.cursor()

        # Find user by QR code
        c.execute('SELECT id, username, vehicle_type FROM users WHERE qr_code = ?', (qr_code,))
        user = c.fetchone()

        if not user:
            conn.close()  # Close only if user not found
            return jsonify({'error': 'Invalid QR code - User not found'}), 404

        user_id, username, vehicle_type = user
        print(f"✅ User found: {username} (ID: {user_id})")

        # Check if user has active session (for check-out)
        c.execute('''
            SELECT id, check_in_time, lot_number FROM parking_sessions 
            WHERE user_id = ? AND status = 'active'
        ''', (user_id,))
        active_session = c.fetchone()

        if active_session:
            # CHECK-OUT PROCESS
            session_id, check_in_time, lot_number = active_session
            print(f"🔄 Check-out process for user {username}, Lot {lot_number}")
            print(f"📅 Check-in time from DB: {check_in_time}")

            # ✅ FIXED: Use the SAME connection for battery counter check
            post_100_duration_display = None
            try:
                # Get charging session for this parking session - USE EXISTING CONNECTION
                c.execute('''
                    SELECT id, completion_time, current_charge_level 
                    FROM charging_sessions 
                    WHERE parking_session_id = ? AND status = "active"
                ''', (session_id,))
                charging_session = c.fetchone()

                if charging_session:
                    charging_session_id, completion_time, current_charge = charging_session

                    if current_charge >= 100 and completion_time:
                        # Calculate time since 100% charge
                        completion_datetime = datetime.strptime(completion_time, '%Y-%m-%d %H:%M:%S')
                        check_out_time = datetime.now()
                        post_100_duration = check_out_time - completion_datetime

                        total_seconds = post_100_duration.total_seconds()
                        minutes = int(total_seconds // 60)
                        seconds = int(total_seconds % 60)

                        # PRINT THE COUNTER VALUE IN TERMINAL (EXACT FORMAT FROM SCREENSHOT)
                        print("\n" + "=" * 50)
                        print("🔋 BATTERY FULLY CHARGED!")
                        print("Your vehicle reached 100% charge. Please")
                        print("unplug immediately to preserve battery health.")
                        print("-" * 50)
                        print(f"**{minutes}m {seconds}s**")
                        print("=" * 50 + "\n")

                        post_100_duration_display = f"{minutes}m {seconds}s"

            except Exception as e:
                print(f"⚠️ Could not calculate battery full counter: {e}")

            # Get current system time for check-out
            check_out_time = datetime.now()
            print(f"📅 Current system time: {check_out_time}")

            # Convert check_in_time to datetime object
            try:
                if isinstance(check_in_time, str):
                    check_in_datetime = datetime.strptime(check_in_time, '%Y-%m-%d %H:%M:%S')
                else:
                    check_in_datetime = check_in_time

                print(f"🕒 Check-in datetime: {check_in_datetime}")
                print(f"🕒 Check-out datetime: {check_out_time}")

                total_seconds = (check_out_time - check_in_datetime).total_seconds()
                duration_minutes = int(total_seconds / 60)
                duration_hours = total_seconds / 3600

                print(f"⏱️ Raw duration: {total_seconds} seconds")
                print(f"⏱️ Duration in minutes: {duration_minutes} minutes")
                print(f"⏱️ Duration in hours: {duration_hours:.2f} hours")

            except Exception as time_error:
                print(f"❌ Time calculation error: {time_error}")
                duration_minutes = 1
                duration_hours = 1 / 60

            # Calculate amount based on pricing rules
            amount = 0.0
            rate_used = ""

            if duration_minutes <= 5:
                amount = 0.0
                rate_used = "Free (0-5 minutes)"
            elif duration_minutes <= 60:
                amount = round(duration_minutes * 1.25, 2)
                rate_used = f"₹1.25/min ({duration_minutes} minutes)"
            else:
                amount = round(duration_minutes * 1.05, 2)
                rate_used = f"₹1.05/min ({duration_minutes} minutes)"

            print(f"💰 Amount: ₹{amount} ({rate_used})")

            # Stop any active charging session
            charging_monitor.stop_user_charging_session(user_id)

            # Update parking session with proper check_out_time
            check_out_time_str = check_out_time.strftime('%Y-%m-%d %H:%M:%S')
            c.execute('''
                UPDATE parking_sessions 
                SET check_out_time = ?, status = 'completed', 
                    duration_minutes = ?, total_amount = ?
                WHERE id = ?
            ''', (check_out_time_str, duration_minutes, amount, session_id))

            # Free up the parking lot
            try:
                if parking_detector.free_parking_lot(lot_number):
                    print(f"✅ Freed lot {lot_number} in detector")
            except Exception as det_error:
                print(f"⚠️ Detector error: {det_error}")

            conn.commit()  # Commit all changes
            conn.close()  # ✅ CLOSE CONNECTION ONLY HERE AT THE END

            # Format duration for display
            if duration_minutes < 60:
                duration_display = f"{duration_minutes} minutes"
            else:
                hours = duration_minutes // 60
                minutes_remaining = duration_minutes % 60
                if minutes_remaining > 0:
                    duration_display = f"{hours} hours {minutes_remaining} minutes"
                else:
                    duration_display = f"{hours} hours"

            print(f"✅ Check-out successful for {username}")
            print(f"📊 Final: {duration_display}, ₹{amount}")

            # Prepare response data
            response_data = {
                'success': True,
                'session_type': 'check_out',
                'user': {
                    'id': user_id,
                    'name': username,
                    'vehicle_type': vehicle_type
                },
                'session_id': session_id,
                'duration_minutes': duration_minutes,
                'duration_display': duration_display,
                'total_amount': amount,
                'rate_used': rate_used,
                'check_in_time': str(check_in_time),
                'check_out_time': check_out_time_str,
                'lot_number': lot_number,
                'message': f'Check-out successful for {username}! Duration: {duration_display}. Amount: ₹{amount} ({rate_used})'
            }

            # Add battery counter info if applicable
            if post_100_duration_display:
                response_data['battery_full_duration'] = post_100_duration_display

            return jsonify(response_data)

        else:
            # CHECK-IN PROCESS - AUTO START CHARGING
            print(f"🔄 Check-in process for user {username}")

            current_data = parking_detector.get_parking_data()
            free_lots = current_data.get('free_lots', [])

            if not free_lots:
                conn.close()  # Close connection before returning error
                print("❌ No parking spaces available for check-in")
                return jsonify({'error': 'No parking spaces available'}), 400

            assigned_lot = free_lots[0]
            print(f"🅿️ Assigning lot {assigned_lot} to {username}")

            # Use current system time for check-in
            current_time = datetime.now()
            check_in_time_str = current_time.strftime('%Y-%m-%d %H:%M:%S')
            print(f"🕒 Setting check-in time: {check_in_time_str}")

            # Mark as occupied in detector
            try:
                if parking_detector.assign_parking_lot(assigned_lot):
                    print(f"✅ Assigned lot {assigned_lot} in detector")
            except Exception as det_error:
                print(f"⚠️ Detector error: {det_error}")

            # Create parking session with current time
            c.execute('''
                INSERT INTO parking_sessions (user_id, qr_code, lot_number, status, check_in_time) 
                VALUES (?, ?, ?, 'active', ?)
            ''', (user_id, qr_code, assigned_lot, check_in_time_str))

            parking_session_id = c.lastrowid
            conn.commit()
            conn.close()  # ✅ Close connection after check-in operations

            # Start charging session (outside database transaction)
            start_charge_level = random.randint(10, 45)  # Random between 10-45%
            charging_session_id = charging_monitor.start_charging_session(
                user_id,
                parking_session_id,
                start_charge_level
            )

            print(f"✅ Check-in successful for {username}")
            print(f"🔌 Auto-started charging at {start_charge_level}% (Session: {charging_session_id})")

            return jsonify({
                'success': True,
                'session_type': 'check_in',
                'user': {
                    'id': user_id,
                    'name': username,
                    'vehicle_type': vehicle_type
                },
                'assigned_lot': assigned_lot,
                'free_spaces': len(free_lots) - 1,
                'total_spaces': current_data.get('total_spaces', len(posList)),
                'session_id': parking_session_id,
                'charging_session_id': charging_session_id,
                'start_charge_level': start_charge_level,
                'check_in_time': check_in_time_str,
                'message': f'Welcome {username}! Assigned to Lot {assigned_lot}. Charging started at {start_charge_level}%. Free parking for first 5 minutes!'
            })

    except Exception as e:
        print(f"❌ Error in QR scan API: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Scanning failed: {str(e)}'}), 500


@app.route('/fix-sessions', methods=['POST'])
def fix_sessions():
    """Fix any problematic sessions"""
    try:
        conn = sqlite3.connect('parking_system.db')
        c = conn.cursor()

        # Get all active sessions
        c.execute('SELECT id, user_id, check_in_time FROM parking_sessions WHERE status = "active"')
        active_sessions = c.fetchall()

        result = f"<h1>Fixed {len(active_sessions)} Active Sessions</h1>"

        for session_id, user_id, check_in_time in active_sessions:
            # Verify check_in_time is reasonable (not in future, not too far in past)
            if isinstance(check_in_time, str):
                check_in_datetime = datetime.strptime(check_in_time, '%Y-%m-%d %H:%M:%S')
            else:
                check_in_datetime = check_in_time

            current_time = datetime.now()
            time_diff = (current_time - check_in_datetime).total_seconds() / 3600  # hours

            # If check-in time is more than 24 hours ago, it's probably wrong
            if time_diff > 24:
                # Update with current time minus 1 minute
                new_check_in = (current_time - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
                c.execute('UPDATE parking_sessions SET check_in_time = ? WHERE id = ?', (new_check_in, session_id))
                result += f"<p>Fixed session {session_id}: {check_in_time} -> {new_check_in}</p>"

        conn.commit()
        conn.close()
        return result

    except Exception as e:
        return jsonify({'error': f'Fix failed: {str(e)}'}), 500


@app.route('/debug/sessions')
def debug_sessions():
    """Debug endpoint to see all active sessions"""
    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()
    c.execute('''
        SELECT ps.id, u.username, ps.lot_number, ps.check_in_time, ps.status 
        FROM parking_sessions ps 
        JOIN users u ON ps.user_id = u.id 
        ORDER BY ps.check_in_time DESC
    ''')
    sessions = c.fetchall()
    conn.close()

    result = "<h1>Parking Sessions Debug</h1>"
    result += "<table border='1' style='border-collapse: collapse; width: 100%;'>"
    result += "<tr><th>ID</th><th>User</th><th>Lot</th><th>Check-in Time</th><th>Status</th></tr>"

    for session in sessions:
        session_id, username, lot_number, check_in_time, status = session
        result += f"""
        <tr>
            <td>{session_id}</td>
            <td>{username}</td>
            <td>{lot_number}</td>
            <td>{check_in_time}</td>
            <td>{status}</td>
        </tr>
        """
    result += "</table>"
    return result

@app.route('/create-test-user')
def create_test_user():
    """Create a test user for debugging"""
    try:
        qr_data = generate_unique_qr()
        qr_image = create_qr_image(qr_data)

        conn = sqlite3.connect('parking_system.db')
        c = conn.cursor()

        c.execute('SELECT id FROM users WHERE username = ?', ('testuser',))
        existing_user = c.fetchone()

        if existing_user:
            conn.close()
            return jsonify({'message': 'Test user already exists', 'user_id': existing_user[0]})

        c.execute('''
            INSERT INTO users (username, email, password, qr_code, qr_image, vehicle_type) 
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('testuser', 'test@parkaro.com', 'test123', qr_data, qr_image, 'ev'))

        user_id = c.lastrowid
        conn.commit()
        conn.close()

        return jsonify({
            'success': True,
            'message': 'Test user created successfully!',
            'user_id': user_id,
            'qr_code': qr_data
        })

    except Exception as e:
        return jsonify({'error': f'Failed to create test user: {str(e)}'}), 500


@app.route('/test-qr')
def test_qr():
    """Test page with QR code"""
    try:
        conn = sqlite3.connect('parking_system.db')
        c = conn.cursor()
        c.execute('SELECT username, qr_code, qr_image FROM users LIMIT 1')
        user = c.fetchone()
        conn.close()

        if user:
            username, qr_code, qr_image = user
        else:
            qr_code = "PARKARO_TEST_123"
            qr_image = create_qr_image(qr_code)

        return render_template('test_qr.html', qr_code=qr_code, qr_image=qr_image)

    except Exception as e:
        qr_code = "PARKARO_EMERGENCY_TEST"
        qr_image = create_qr_image(qr_code)
        return render_template('test_qr.html', qr_code=qr_code, qr_image=qr_image)


@app.route('/debug/users')
def debug_users():
    """Debug endpoint to see all users with complete details"""
    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()
    c.execute('SELECT id, username, email, vehicle_type, phone, pan_card, driving_license, vehicle_number, qr_code FROM users')
    users = c.fetchall()
    conn.close()

    result = "<h1>Users Debug - Complete Details</h1>"
    if users:
        result += "<table border='1' style='border-collapse: collapse; width: 100%; font-size: 12px;'>"
        result += "<tr><th>ID</th><th>Username</th><th>Email</th><th>Vehicle</th><th>Phone</th><th>PAN</th><th>DL No</th><th>Vehicle No</th><th>QR Code</th></tr>"
        for user in users:
            result += f"""
            <tr>
                <td>{user[0]}</td>
                <td>{user[1]}</td>
                <td>{user[2]}</td>
                <td>{user[3]}</td>
                <td>{user[4] or 'N/A'}</td>
                <td>{user[5] or 'N/A'}</td>
                <td>{user[6] or 'N/A'}</td>
                <td>{user[7] or 'N/A'}</td>
                <td><code style='font-size: 10px;'>{user[8]}</code></td>
            </tr>
            """
        result += "</table>"
    else:
        result += "<p>No users found in database!</p>"
    return result




# Add this after your other imports

@app.route('/api/start-charging', methods=['POST'])
def api_start_charging():
    """Start a charging session"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.json
    start_charge_level = data.get('start_charge_level', 0)

    # Get user's active parking session
    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()
    c.execute('SELECT id FROM parking_sessions WHERE user_id = ? AND status = "active"', (session['user_id'],))
    parking_session = c.fetchone()
    conn.close()

    if not parking_session:
        return jsonify({'error': 'No active parking session found'}), 400

    parking_session_id = parking_session[0]

    # Start charging session
    session_id = charging_monitor.start_charging_session(
        session['user_id'],
        parking_session_id,
        start_charge_level
    )

    if session_id:
        return jsonify({
            'success': True,
            'message': 'Charging session started',
            'charging_session_id': session_id,
            'start_charge_level': start_charge_level
        })
    else:
        return jsonify({'error': 'Failed to start charging session'}), 500


@app.route('/api/update-charge', methods=['POST'])
def api_update_charge():
    """Update charge level - handle first-time completion"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        charge_level = data.get('charge_level')

        print(f"🔋 API Update Charge - Session: {session_id}, Level: {charge_level}")

        if not session_id:
            return jsonify({'error': 'Session ID is required'}), 400

        if charge_level is None:
            return jsonify({'error': 'Charge level is required'}), 400

        try:
            charge_level = int(charge_level)
        except ValueError:
            return jsonify({'error': 'Charge level must be a number'}), 400

        if not (0 <= charge_level <= 100):
            return jsonify({'error': 'Charge level must be between 0 and 100'}), 400

        result = charging_monitor.update_charge_level(session_id, charge_level)

        if result == 'first_time_complete':
            return jsonify({
                'success': True,
                'message': 'Vehicle fully charged for the first time!',
                'charge_level': 100,
                'status': 'first_time_complete'
            })
        elif result == 'complete':
            return jsonify({
                'success': True,
                'message': 'Vehicle remains fully charged.',
                'charge_level': 100,
                'status': 'complete'
            })
        elif result:
            return jsonify({
                'success': True,
                'message': f'Charge level updated to {charge_level}%',
                'charge_level': charge_level,
                'status': 'charging'
            })
        else:
            return jsonify({'error': 'Failed to update charge level. Check if session exists.'}), 400

    except Exception as e:
        print(f"❌ API Error in update-charge: {str(e)}")
        return jsonify({'error': f'Server error: {str(e)}'}), 500


@app.route('/api/charging-status')
def api_charging_status():
    """Get current user's charging status"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    status = charging_monitor.get_user_charging_status(session['user_id'])

    if status:
        return jsonify({
            'success': True,
            'charging_status': status
        })
    else:
        return jsonify({
            'success': True,
            'charging_status': None,
            'message': 'No active charging session'
        })


@app.route('/api/stop-charging', methods=['POST'])
def api_stop_charging():
    """Stop charging session"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    data = request.json
    session_id = data.get('session_id')

    if charging_monitor.complete_charging_session(session_id):
        return jsonify({
            'success': True,
            'message': 'Charging session stopped'
        })
    else:
        return jsonify({'error': 'Failed to stop charging session'}), 500


@app.route('/charging-station')
def charging_station():
    """Charging station interface for manual input"""
    return render_template('charging_station.html')


@app.route('/debug/charging-sessions')
def debug_charging_sessions():
    """Debug page for charging sessions"""
    active_sessions = charging_monitor.get_all_active_charging_sessions()

    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()
    c.execute('''
        SELECT cs.id, u.username, cs.start_charge_level, cs.current_charge_level, 
               cs.start_time, cs.status, cs.total_energy
        FROM charging_sessions cs
        JOIN users u ON cs.user_id = u.id
        ORDER BY cs.start_time DESC
        LIMIT 10
    ''')
    recent_sessions = c.fetchall()
    conn.close()

    result = "<h1>🔌 Charging Sessions Debug</h1>"

    result += "<h2>Active Sessions</h2>"
    if active_sessions:
        result += "<table border='1'><tr><th>Session ID</th><th>User</th><th>Charge Level</th><th>Start Time</th></tr>"
        for session in active_sessions:
            result += f"<tr><td>{session['session_id']}</td><td>{session['username']}</td><td>{session['current_charge']}%</td><td>{session['start_time']}</td></tr>"
        result += "</table>"
    else:
        result += "<p>No active charging sessions</p>"

    result += "<h2>Recent Sessions (Last 10)</h2>"
    result += "<table border='1'><tr><th>ID</th><th>User</th><th>Start %</th><th>Current %</th><th>Start Time</th><th>Status</th><th>Energy (kWh)</th></tr>"
    for session in recent_sessions:
        result += f"<tr><td>{session[0]}</td><td>{session[1]}</td><td>{session[2]}%</td><td>{session[3]}%</td><td>{session[4]}</td><td>{session[5]}</td><td>{session[6] or 0:.2f}</td></tr>"
    result += "</table>"

    return result


@app.route('/debug/qr-codes')
def debug_qr_codes():
    """Debug QR codes"""
    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()
    c.execute('SELECT id, username, qr_code FROM users')
    users = c.fetchall()
    conn.close()

    result = "<h1>QR Codes Debug</h1><table border='1'><tr><th>ID</th><th>Username</th><th>QR Code</th></tr>"
    for user in users:
        result += f"<tr><td>{user[0]}</td><td>{user[1]}</td><td><code>{user[2]}</code></td></tr>"
    result += "</table>"
    return result


@app.route('/debug-routes')
def debug_routes():
    """Show all available routes"""
    routes = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint != 'static':
            routes.append(f"{rule.rule} - {list(rule.methods)} - {rule.endpoint}")
    return "<br>".join(sorted(routes))


@app.route('/api/test')
def api_test():
    """Test API endpoint"""
    return jsonify({
        'message': 'API is working!',
        'status': 'success',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    })


@app.route('/api/user/active-session')
def api_user_active_session():
    """Get user's active parking session"""
    if 'user_id' not in session:
        return jsonify({'error': 'Not logged in'}), 401

    conn = sqlite3.connect('parking_system.db')
    c = conn.cursor()

    c.execute('''
        SELECT ps.lot_number, ps.check_in_time, u.vehicle_type 
        FROM parking_sessions ps
        JOIN users u ON ps.user_id = u.id
        WHERE ps.user_id = ? AND ps.status = 'active'
        ORDER BY ps.check_in_time DESC
        LIMIT 1
    ''', (session['user_id'],))

    active_session = c.fetchone()
    conn.close()

    if active_session:
        return jsonify({
            'success': True,
            'active_session': {
                'lot_number': active_session[0],
                'check_in_time': active_session[1],
                'vehicle_type': active_session[2]
            }
        })
    else:
        return jsonify({
            'success': True,
            'active_session': None
        })


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


if __name__ == '__main__':
    init_db()

    if posList:
        parking_detector.start_detection()
        sync_thread = threading.Thread(target=sync_parking_status, daemon=True)
        sync_thread.start()
        print("✅ Parking detection and sync started")
    else:
        print("⚠️  No parking spaces defined")

    print("🚀 ParKaro EV Charging System Started!")
    print("📍 Main Dashboard: http://localhost:5000")
    print("👤 User Registration: http://localhost:5000/register")
    print("👤 User Login: http://localhost:5000/login")
    print("📱 Scanning Station: http://localhost:5000/scanning-station")
    print("🧪 Test QR Page: http://localhost:5000/test-qr")
    print("🐛 Debug Users: http://localhost:5000/debug/users")
    print("🔧 Create Test User: http://localhost:5000/create-test-user")

    app.run(debug=True, host='0.0.0.0', port=5000)