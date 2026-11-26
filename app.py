from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import joblib, requests, os, secrets
from dotenv import load_dotenv
from flask_bcrypt import Bcrypt
from datetime import datetime
from pymongo import MongoClient, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

# -----------------------------
# App Setup
# -----------------------------
app = Flask(__name__)
load_dotenv()
APP_ENV = os.getenv("APP_ENV", "development").lower()
IS_PRODUCTION = APP_ENV == "production"
SECRET_KEY = os.getenv("SECRET_KEY")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

if APP_ENV not in {"development", "production"}:
    raise RuntimeError("APP_ENV must be either 'development' or 'production'")
if IS_PRODUCTION:
    missing_settings = [
        setting
        for setting, value in (
            ("SECRET_KEY", SECRET_KEY),
            ("ADMIN_PASSWORD", ADMIN_PASSWORD),
            ("OPENWEATHER_API_KEY", OPENWEATHER_API_KEY),
        )
        if not value
    ]
    if missing_settings:
        raise RuntimeError(
            "Required production settings are missing: "
            + ", ".join(missing_settings)
        )

app.secret_key = SECRET_KEY or secrets.token_hex(32)
bcrypt = Bcrypt(app)
MONGODB_URI = os.getenv("MONGODB_URI")
if not MONGODB_URI:
    raise RuntimeError("MONGODB_URI must be set to connect to MongoDB Atlas")
mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db = mongo_client["users"]
users_collection = db["users"]
reports_collection = db["reports"]
alerts_collection = db["alerts"]
counters_collection = db["counters"]
API_KEY = OPENWEATHER_API_KEY or "demo_api_key"

# -----------------------------
# Database Initialization
# -----------------------------
def init_db():
    mongo_client.admin.command("ping")
    users_collection.create_index(
        "email",
        unique=True,
        partialFilterExpression={"email": {"$type": "string"}},
    )
    for collection_name, collection in (
        ("users", users_collection),
        ("reports", reports_collection),
        ("alerts", alerts_collection),
    ):
        latest = collection.find_one(
            {"_id": {"$type": "number"}}, sort=[("_id", DESCENDING)]
        )
        counters_collection.update_one(
            {"_id": collection_name},
            {"$max": {"sequence": latest["_id"] if latest else 0}},
            upsert=True,
        )
    create_admin_if_not_exists()

def next_id(collection_name):
    counter = counters_collection.find_one_and_update(
        {"_id": collection_name},
        {"$inc": {"sequence": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return counter["sequence"]

def create_admin_if_not_exists():
    admin = users_collection.find_one({"email": "admin@example.com"})
    if admin:
        if ADMIN_PASSWORD:
            current_password = admin.get("password")
            if not current_password or not bcrypt.check_password_hash(
                current_password, ADMIN_PASSWORD
            ):
                password_hash = bcrypt.generate_password_hash(
                    ADMIN_PASSWORD
                ).decode("utf-8")
                users_collection.update_one(
                    {"_id": admin["_id"]},
                    {"$set": {"password": password_hash}},
                )
        return

    if not ADMIN_PASSWORD:
        raise RuntimeError(
            "Set ADMIN_PASSWORD before creating the admin account"
        )

    password_hash = bcrypt.generate_password_hash(ADMIN_PASSWORD).decode("utf-8")
    users_collection.insert_one({
        "_id": next_id("users"),
        "name": "admin",
        "email": "admin@example.com",
        "password": password_hash,
    })
    print("Admin account created: admin@example.com")

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
        user = users_collection.find_one({"email": email})
        if user and bcrypt.check_password_hash(user["password"], password):
            session['user'] = user["name"]
            session['email'] = user["email"]
            session['user_id'] = user["_id"]
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
        try:
            users_collection.insert_one({
                "_id": next_id("users"),
                "name": name,
                "email": email,
                "password": password,
            })
        except DuplicateKeyError:
            return render_template('register.html', error="Email already registered")
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
    reports_collection.insert_one({
        "_id": next_id("reports"),
        "user_id": user_id,
        "city": city,
        "report_type": report_type,
        "description": description,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    return jsonify({'message': 'Report submitted successfully'})

# -----------------------------
# Admin Page
# -----------------------------
@app.route('/admin')
def admin_page():
    if 'user' not in session or session.get('email') != 'admin@example.com':
        return redirect(url_for('login_page'))

    users = [
        (user["_id"], user.get("name"), user.get("email"))
        for user in users_collection.find(
            {}, {"name": 1, "email": 1}
        ).sort("_id", 1)
    ]
    total_users = len(users)

    reports = []
    for report in reports_collection.find().sort("timestamp", DESCENDING):
        user = users_collection.find_one({"_id": report.get("user_id")}, {"name": 1})
        reports.append((
            report["_id"],
            user.get("name", "Unknown") if user else "Unknown",
            report.get("city"),
            report.get("report_type"),
            report.get("description"),
            report.get("timestamp"),
        ))

    alerts = [
        (alert["_id"], alert.get("title"), alert.get("message"), alert.get("timestamp"))
        for alert in alerts_collection.find().sort("_id", DESCENDING).limit(50)
    ]
    return render_template('admin.html', users=users, total_users=total_users, reports=reports, alerts=alerts)

# -----------------------------
# Delete routes
# -----------------------------
@app.route('/delete_user/<int:user_id>', methods=['DELETE'])
def delete_user_api(user_id):
    if 'user' not in session or session.get('email') != 'admin@example.com':
        return jsonify({'error': 'Unauthorized'}), 403
    users_collection.delete_one({"_id": user_id})
    return jsonify({'message': 'User deleted successfully'})

@app.route('/delete_report/<int:report_id>', methods=['DELETE'])
def delete_report(report_id):
    if 'user' not in session or session.get('email') != 'admin@example.com':
        return jsonify({'error': 'Unauthorized'}), 403
    reports_collection.delete_one({"_id": report_id})
    return jsonify({'message': 'Report deleted successfully'})

@app.route('/delete_alert/<int:alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    if 'user' not in session or session.get('email') != 'admin@example.com':
        return jsonify({'error': 'Unauthorized'}), 403
    alerts_collection.delete_one({"_id": alert_id})
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
    alerts_collection.insert_one({
        "_id": next_id("alerts"),
        "title": title,
        "message": message,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    return jsonify({"message": "Alert created successfully!"})

@app.route("/user_alerts", methods=["GET"])
def user_alerts():
    alerts = [
        {
            "title": alert.get("title"),
            "message": alert.get("message"),
            "timestamp": alert.get("timestamp"),
        }
        for alert in alerts_collection.find(
            {}, {"title": 1, "message": 1, "timestamp": 1}
        ).sort("_id", DESCENDING).limit(10)
    ]
    return jsonify(alerts)

# -----------------------------
# Run App
# -----------------------------
if __name__ == '__main__':
    app.run(debug=not IS_PRODUCTION)
