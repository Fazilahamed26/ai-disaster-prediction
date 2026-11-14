from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import joblib, requests, sqlite3, os
from dotenv import load_dotenv
from flask_bcrypt import Bcrypt

# -----------------------------
# App Setup
# -----------------------------
app = Flask(__name__)
load_dotenv()
app.secret_key = os.getenv("SECRET_KEY", "supersecretkey")
bcrypt = Bcrypt(app)
DB = "users.db"
API_KEY = os.getenv("OPENWEATHER_API_KEY", "demo_api_key")  # fallback

# -----------------------------
# Database Initialization
# -----------------------------
def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # Users table
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            password TEXT
        )
    """)

    # Reports table with user_id
    c.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            city TEXT,
            report_type TEXT,
            description TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Alerts table
    c.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            message TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()
    create_admin_if_not_exists()

def create_admin_if_not_exists():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email=?", ("admin@example.com",))
    if not c.fetchone():
        password_hash = bcrypt.generate_password_hash("admin123").decode("utf-8")
        c.execute("INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                  ("admin", "admin@example.com", password_hash))
        conn.commit()
        print("✅ Admin account created: admin@example.com / admin123")
    conn.close()

init_db()

# -----------------------------
# Optional: Load ML models (if present)
# -----------------------------
try:
    flood_model = joblib.load("models/flood_model.pkl")
    cyclone_model = joblib.load("models/cyclone_model.pkl")
except Exception:
    flood_model = None
    cyclone_model = None

# -----------------------------
# Helper Functions
# -----------------------------
def get_live_weather(city):
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={API_KEY}&units=metric"
    try:
        r = requests.get(url, timeout=8)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    d = r.json()
    return {
        "temp": d.get("main", {}).get("temp"),
        "humidity": d.get("main", {}).get("humidity"),
        "pressure": d.get("main", {}).get("pressure"),
        "rainfall": d.get("rain", {}).get("1h", 0.0) if d.get("rain") else 0.0,
        "lat": d.get("coord", {}).get("lat"),
        "lon": d.get("coord", {}).get("lon"),
        "wind_speed": d.get("wind", {}).get("speed")
    }

def get_5day_forecast(city):
    url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={API_KEY}&units=metric"
    try:
        r = requests.get(url, timeout=8)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    data = r.json()
    forecast_list = []
    for item in data.get('list', []):
        forecast_list.append({
            "datetime": item.get("dt_txt"),
            "temp": item.get("main", {}).get("temp"),
            "humidity": item.get("main", {}).get("humidity"),
            "weather": item.get("weather", [{}])[0].get("description")
        })
    return forecast_list

def risk_level(prob):
    if prob is None:
        return "Unknown"
    if prob < 0.4:
        return "Low"
    elif prob < 0.7:
        return "Medium"
    else:
        return "High"

# -----------------------------
# Auth Routes
# -----------------------------
@app.route('/')
def home_redirect():
    return redirect(url_for('login_page'))

@app.route('/login', methods=['GET','POST'])
def login_page():
    if request.method == 'POST':
        email = request.form['email'].strip()
        password = request.form['password']
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE email=?", (email,))
        user = c.fetchone()
        conn.close()
        if user and bcrypt.check_password_hash(user[3], password):
            session['user'] = user[1]
            session['email'] = user[2]
            session['user_id'] = user[0]
            if email == "admin@example.com":
                return redirect(url_for('admin_page'))
            return redirect(url_for('dashboard_page'))
        return render_template('login.html', error="Invalid credentials")
    return render_template('login.html')

@app.route('/register', methods=['GET','POST'])
def register_page():
    if request.method == 'POST':
        name = request.form['name'].strip()
        email = request.form['email'].strip()
        password = bcrypt.generate_password_hash(request.form['password']).decode('utf-8')
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        try:
            c.execute("INSERT INTO users (name, email, password) VALUES (?,?,?)", (name, email, password))
            conn.commit()
        except Exception:
            conn.close()
            return render_template('register.html', error="Email already registered")
        conn.close()
        return redirect(url_for('login_page'))
    return render_template('register.html')

@app.route('/logout')
def logout_page():
    session.clear()
    return redirect(url_for('login_page'))

# -----------------------------
# Dashboard
# -----------------------------
@app.route('/dashboard')
def dashboard_page():
    if 'user' not in session:
        return redirect(url_for('login_page'))
    return render_template('dashboard.html', user=session['user'], openweather_api_key=API_KEY)

# -----------------------------
# Prediction API
# -----------------------------
@app.route('/predict', methods=['POST'])
def predict_api():
    data = request.get_json() or {}
    city = data.get("city")
    if not city:
        return jsonify({"error": "City is required"}), 400
    weather = get_live_weather(city)
    if not weather:
        return jsonify({"error": "City not found or API error"}), 404
    # simple heuristic probabilities
    try:
        flood_prob = round((weather["humidity"]/100) * (weather["rainfall"]/10 + 0.3), 2)
    except Exception:
        flood_prob = 0.0
    try:
        cyclone_prob = round((weather["temp"]/40) * (weather["pressure"]/1000 + 0.2), 2)
    except Exception:
        cyclone_prob = 0.0

    result = {
        "city": city.capitalize(),
        "weather": weather,
        "flood_prob": flood_prob,
        "flood_risk": risk_level(flood_prob),
        "cyclone_prob": cyclone_prob,
        "cyclone_risk": risk_level(cyclone_prob)
    }
    return jsonify(result)

@app.route('/get_5day_forecast', methods=['GET'])
def get_5day_forecast_api():
    city = request.args.get('city', 'Chennai')
    forecast = get_5day_forecast(city)
    if not forecast:
        return jsonify({"error": "City not found or API error"}), 404
    return jsonify(forecast)

# -----------------------------
# Submit Disaster Report
# -----------------------------
@app.route('/submit_report', methods=['POST'])
def submit_report_api():
    if 'user' not in session:
        return jsonify({'error': 'Unauthorized'}), 403
    data = request.get_json() or {}
    city = data.get('city')
    report_type = data.get('report_type')
    description = data.get('description')
    if not city or not report_type or not description:
        return jsonify({'error': 'city, report_type, description required'}), 400

    user_id = session.get('user_id')
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO reports (user_id, city, report_type, description) VALUES (?,?,?,?)",
              (user_id, city, report_type, description))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Report submitted successfully'})

# -----------------------------
# Admin Page
# -----------------------------
@app.route('/admin')
def admin_page():
    if 'user' not in session or session.get('email') != 'admin@example.com':
        return redirect(url_for('login_page'))

    conn = sqlite3.connect(DB)
    c = conn.cursor()

    # Users
    c.execute("SELECT id, name, email FROM users")
    users = c.fetchall()
    total_users = len(users)

    # Reports with user names (left join so rows without user_id still appear)
    c.execute("""
        SELECT reports.id, COALESCE(users.name, 'Unknown') as user_name, reports.city,
               reports.report_type, reports.description, reports.timestamp
        FROM reports
        LEFT JOIN users ON reports.user_id = users.id
        ORDER BY reports.timestamp DESC
    """)
    reports = c.fetchall()

    # Alerts
    c.execute("SELECT id, title, message, timestamp FROM alerts ORDER BY id DESC LIMIT 50")
    alerts = c.fetchall()

    conn.close()
    return render_template('admin.html', users=users, total_users=total_users, reports=reports, alerts=alerts)

# -----------------------------
# Delete routes
# -----------------------------
@app.route('/delete_user/<int:user_id>', methods=['DELETE'])
def delete_user_api(user_id):
    if 'user' not in session or session.get('email') != 'admin@example.com':
        return jsonify({'error': 'Unauthorized'}), 403
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id=?", (user_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'User deleted successfully'})

@app.route('/delete_report/<int:report_id>', methods=['DELETE'])
def delete_report(report_id):
    if 'user' not in session or session.get('email') != 'admin@example.com':
        return jsonify({'error': 'Unauthorized'}), 403
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM reports WHERE id=?", (report_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Report deleted successfully'})

@app.route('/delete_alert/<int:alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    if 'user' not in session or session.get('email') != 'admin@example.com':
        return jsonify({'error': 'Unauthorized'}), 403
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Alert deleted successfully'})

# -----------------------------
# Emergency Alerts
# -----------------------------
@app.route("/create_alert", methods=["POST"])
def create_alert():
    if "user" not in session or session.get("email") != "admin@example.com":
        return jsonify({"error": "Unauthorized"}), 403
    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    message = (data.get("message") or "").strip()
    if not title or not message:
        return jsonify({"error": "title and message required"}), 400
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT INTO alerts (title, message) VALUES (?, ?)", (title, message))
    conn.commit()
    conn.close()
    return jsonify({"message": "Alert created successfully!"})

@app.route("/user_alerts", methods=["GET"])
def user_alerts():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT title, message, timestamp FROM alerts ORDER BY id DESC LIMIT 10")
    rows = c.fetchall()
    conn.close()
    alerts = [{"title": r[0], "message": r[1], "timestamp": r[2]} for r in rows]
    return jsonify(alerts)

# -----------------------------
# Run App
# -----------------------------
if __name__ == '__main__':
    app.run(debug=True)
