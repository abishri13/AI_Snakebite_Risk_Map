from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    jsonify
)

import sqlite3
import pandas as pd
import os
import time
import math
import requests

from werkzeug.security import (
    generate_password_hash,
    check_password_hash
)


# --------------------------------------------------
# 1. LOAD SNAKEBITE STATISTICS DATASET
# --------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATASET_PATH = os.path.join(
    BASE_DIR,
    "datasets",
    "india_statewise_snakebite_summary_2022_2025.xlsx"
)

df = pd.read_excel(DATASET_PATH)

df.columns = (
    df.columns
    .str.replace("ï»¿", "", regex=False)
    .str.strip()
)

print("Snakebite dataset loaded successfully!")
print("Total state/UT records:", len(df))
print("Columns:", df.columns.tolist())


# --------------------------------------------------
# 2. FLASK CONFIGURATION
# --------------------------------------------------

app = Flask(__name__)

app.config["SECRET_KEY"] = os.environ.get(
    "SNAKEGUARD_SECRET_KEY",
    "change-this-to-a-random-secret-key"
)


# --------------------------------------------------
# 3. DATABASE CONNECTION
# --------------------------------------------------

def get_db_connection():

    db_path = os.path.join(BASE_DIR, "snakeguard.db")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = get_db_connection()

    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL
            )
        """)

        conn.commit()

    finally:
        conn.close()


# --------------------------------------------------
# 4. LOGIN
# --------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def home():

    if request.method == "POST":

        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        conn = get_db_connection()

        try:
            user = conn.execute(
                "SELECT * FROM users WHERE email = ?",
                (email,)
            ).fetchone()

        finally:
            conn.close()

        if user and check_password_hash(
            user["password_hash"],
            password
        ):

            session["user_id"] = user["id"]
            session["user_name"] = user["name"]

            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "error")

    return render_template("login.html")


# --------------------------------------------------
# 5. DASHBOARD
# --------------------------------------------------

@app.route("/dashboard")
def dashboard():

    if "user_id" not in session:
        return redirect(url_for("home"))

    total_states = len(df)

    total_cases_2024 = pd.to_numeric(
        df["reported_cases_2024"],
        errors="coerce"
    ).sum()

    total_deaths_2024 = pd.to_numeric(
        df["reported_deaths_2024"],
        errors="coerce"
    ).sum()

    return render_template(
        "dashboard.html",
        name=session["user_name"],
        total_states=total_states,
        total_cases_2024=int(total_cases_2024),
        total_deaths_2024=int(total_deaths_2024)
    )


# --------------------------------------------------
# 6. EXPLORE MAP
# --------------------------------------------------

@app.route("/map")
def map_page():

    if "user_id" not in session:
        return redirect(url_for("home"))

    records = df.fillna(0).to_dict(orient="records")

    return render_template(
        "map.html",
        snakebite_data=records
    )


# --------------------------------------------------
# 7. NEARBY HOSPITALS PAGE
# --------------------------------------------------

@app.route("/hospitals")
def hospitals():

    if "user_id" not in session:
        return redirect(url_for("home"))

    return render_template("hospitals.html")


# --------------------------------------------------
# 8. HOSPITAL SEARCH API
# --------------------------------------------------

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

OVERPASS_TIMEOUT = 20

HOSPITAL_CACHE = {}

CACHE_FRESH_SECONDS = 60 * 60


def haversine_km(lat1, lon1, lat2, lon2):
    """Calculate distance in kilometres between two coordinates."""

    r = 6371.0

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2) ** 2
    )

    a = max(0.0, min(1.0, a))

    return 2 * r * math.asin(math.sqrt(a))


def fetch_from_overpass(query):
    """Try the available Overpass servers."""

    for url in OVERPASS_URLS:

        try:
            response = requests.post(
                url,
                data={"data": query},
                timeout=OVERPASS_TIMEOUT,
                headers={
                    "User-Agent": "SnakeGuardAI/1.0",
                    "Accept": "application/json"
                }
            )

            response.raise_for_status()

            result = response.json()

            print("OVERPASS OK:", url)

            return result

        except (requests.RequestException, ValueError) as e:

            print(
                "OVERPASS ERROR on",
                url,
                "->",
                repr(e)
            )

    return None


def parse_facilities(result, latitude, longitude):
    """Convert OpenStreetMap results into a sorted list."""

    facilities = []

    for item in result.get("elements", []):

        tags = item.get("tags", {})

        lat = item.get("lat")
        lon = item.get("lon")

        if lat is None or lon is None:

            centre = item.get("center", {})

            lat = centre.get("lat")
            lon = centre.get("lon")

        if lat is None or lon is None:
            continue

        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            continue

        facilities.append({

            "name": tags.get(
                "name",
                "Unnamed medical facility"
            ),

            "type": tags.get(
                "amenity",
                "medical facility"
            ),

            "latitude": lat,
            "longitude": lon,

            "phone": (
                tags.get("phone")
                or tags.get("contact:phone")
            ),

            "address": ", ".join(
                filter(
                    None,
                    [
                        tags.get("addr:street"),
                        tags.get("addr:suburb"),
                        tags.get("addr:city")
                    ]
                )
            ),

            "emergency": tags.get(
                "emergency",
                "Not specified"
            ),

            "distance_km": round(
                haversine_km(
                    latitude,
                    longitude,
                    lat,
                    lon
                ),
                2
            )
        })

    facilities.sort(
        key=lambda f: (
            f["type"] != "hospital",
            f["distance_km"]
        )
    )

    return facilities


@app.route("/api/hospitals")
def search_hospitals():

    if "user_id" not in session:
        return jsonify({
            "error": "Please log in to search hospitals."
        }), 401

    try:
        latitude = float(request.args.get("lat", ""))
        longitude = float(request.args.get("lon", ""))
        radius = int(request.args.get("radius", "10000"))

    except (TypeError, ValueError):
        return jsonify({
            "error": "Enter valid coordinates."
        }), 400

    if not (-90 <= latitude <= 90):
        return jsonify({
            "error": "Invalid latitude."
        }), 400

    if not (-180 <= longitude <= 180):
        return jsonify({
            "error": "Invalid longitude."
        }), 400

    if not (1000 <= radius <= 50000):
        return jsonify({
            "error": "Search radius must be between 1 and 50 km."
        }), 400

    cache_key = (
        round(latitude, 2),
        round(longitude, 2),
        radius
    )

    cached = HOSPITAL_CACHE.get(cache_key)

    if (
        cached
        and time.time() - cached["time"] < CACHE_FRESH_SECONDS
    ):

        return jsonify({
            "count": len(cached["hospitals"]),
            "hospitals": cached["hospitals"],
            "source": "OpenStreetMap (cached)",
            "note": (
                "Listings may be incomplete. Verify availability "
                "and emergency services directly with the facility."
            )
        })

    query = f"""
    [out:json][timeout:15];
    nwr["amenity"~"^(hospital|clinic)$"]
       (around:{radius},{latitude},{longitude});
    out center 80 tags;
    """

    result = fetch_from_overpass(query)

    if result is None:

        if cached:

            return jsonify({
                "count": len(cached["hospitals"]),
                "hospitals": cached["hospitals"],
                "source": "OpenStreetMap (saved result)",
                "note": (
                    "Live search is unavailable. This is a saved "
                    "result; verify facility details directly."
                )
            })

        return jsonify({
            "error": (
                "Hospital search is temporarily unavailable. "
                "Please try again or use Google Maps."
            )
        }), 503

    hospitals_list = parse_facilities(
        result,
        latitude,
        longitude
    )

    HOSPITAL_CACHE[cache_key] = {
        "time": time.time(),
        "hospitals": hospitals_list
    }

    return jsonify({
        "count": len(hospitals_list),
        "hospitals": hospitals_list,
        "source": "OpenStreetMap",
        "note": (
            "Listings may be incomplete. Verify availability "
            "and emergency services directly with the facility."
        )
    })


# --------------------------------------------------
# 9. TAMIL NADU DISTRICTS
# --------------------------------------------------

TAMIL_NADU_DISTRICTS = [
    "Ariyalur",
    "Chengalpattu",
    "Chennai",
    "Coimbatore",
    "Cuddalore",
    "Dharmapuri",
    "Dindigul",
    "Erode",
    "Kallakurichi",
    "Kancheepuram",
    "Kanniyakumari",
    "Karur",
    "Krishnagiri",
    "Madurai",
    "Mayiladuthurai",
    "Nagapattinam",
    "Namakkal",
    "Nilgiris",
    "Perambalur",
    "Pudukkottai",
    "Ramanathapuram",
    "Ranipet",
    "Salem",
    "Sivaganga",
    "Tenkasi",
    "Thanjavur",
    "Theni",
    "Thoothukudi",
    "Tiruchirappalli",
    "Tirunelveli",
    "Tirupathur",
    "Tiruppur",
    "Tiruvallur",
    "Tiruvannamalai",
    "Tiruvarur",
    "Vellore",
    "Viluppuram",
    "Virudhunagar"
]


VALID_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December"
]


VALID_ENVIRONMENTS = [
    "Agricultural",
    "Forest",
    "Residential",
    "Wetland"
]


# --------------------------------------------------
# 10. SNAKEBITE RISK ASSESSMENT
# --------------------------------------------------

@app.route("/prediction", methods=["GET", "POST"])
def prediction():

    if "user_id" not in session:
        return redirect(url_for("home"))

    result = None

    if request.method == "POST":

        district = request.form.get(
            "district",
            ""
        ).strip()

        month = request.form.get(
            "month",
            ""
        ).strip()

        rainfall_input = request.form.get(
            "rainfall",
            ""
        ).strip()

        environment = request.form.get(
            "environment",
            ""
        ).strip()

        try:

            rainfall = float(rainfall_input)

            if not math.isfinite(rainfall):
                raise ValueError

            if district not in TAMIL_NADU_DISTRICTS:
                raise ValueError

            if month not in VALID_MONTHS:
                raise ValueError

            if environment not in VALID_ENVIRONMENTS:
                raise ValueError

            if not 0 <= rainfall <= 2000:
                raise ValueError

        except (ValueError, TypeError):

            flash(
                "Please enter valid values in all fields.",
                "error"
            )

            return render_template(
                "prediction.html",
                districts=TAMIL_NADU_DISTRICTS,
                months=VALID_MONTHS,
                result=None
            )

        # --------------------------------------------------
        # RULE-BASED ESTIMATE
        #
        # This is not a trained ML model.
        # The score is not a probability or measured risk.
        # --------------------------------------------------

        score = 0

        reasons = []

        # Rainfall contribution
        if rainfall >= 100:

            score += 3

            reasons.append(
                "The entered rainfall is high and may affect "
                "snake movement and human encounters."
            )

        elif rainfall >= 50:

            score += 2

            reasons.append(
                "The entered rainfall is moderate."
            )

        elif rainfall >= 20:

            score += 1

            reasons.append(
                "The entered rainfall contributes one point "
                "to the environmental score."
            )

        else:

            reasons.append(
                "The entered rainfall contributes no points "
                "to the environmental score."
            )

        # General seasonal factor
        wet_season_months = [
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December"
        ]

        if month in wet_season_months:

            score += 2

            reasons.append(
                "The selected month falls within a period that "
                "can be wetter in parts of Tamil Nadu. Conditions "
                "vary by district and year."
            )

        # Environment contribution
        environment_scores = {
            "Agricultural": 3,
            "Forest": 3,
            "Wetland": 3,
            "Residential": 1
        }

        score += environment_scores[environment]

        environment_messages = {

            "Agricultural": (
                "Agricultural environments may expose people "
                "to snake habitats during outdoor activities."
            ),

            "Forest": (
                "Forest environments can provide habitat for "
                "snakes. Actual encounter risk varies by location."
            ),

            "Wetland": (
                "Wetland environments may provide suitable "
                "habitats for some snake species."
            ),

            "Residential": (
                "Snake encounters can also occur in residential "
                "areas, particularly around vegetation and debris."
            )
        }

        reasons.append(
            environment_messages[environment]
        )

        # Risk category from the rule-based score
        if score >= 7:

            risk_level = "Higher"

            risk_class = "high"

            recommendation = (
                "Take additional precautions outdoors. Wear "
                "closed footwear, use a light at night, avoid "
                "tall grass and debris, and keep surroundings clear."
            )

        elif score >= 4:

            risk_level = "Moderate"

            risk_class = "moderate"

            recommendation = (
                "Remain alert outdoors, wear closed footwear, "
                "and avoid reaching into dark spaces or vegetation "
                "without checking."
            )

        else:

            risk_level = "Lower"

            risk_class = "low"

            recommendation = (
                "Continue ordinary snakebite precautions. "
                "A lower score does not mean snakebite risk is absent."
            )

        result = {
            "district": district,
            "month": month,
            "rainfall": rainfall,
            "environment": environment,
            "risk_level": risk_level,
            "risk_class": risk_class,
            "score": score,
            "reasons": reasons,
            "recommendation": recommendation
        }

    return render_template(
        "prediction.html",
        districts=TAMIL_NADU_DISTRICTS,
        months=VALID_MONTHS,
        result=result
    )


# --------------------------------------------------
# 11. SIGNUP
# --------------------------------------------------

@app.route("/signup", methods=["GET", "POST"])
def signup():

    if request.method == "POST":

        name = request.form.get(
            "name",
            ""
        ).strip()

        email = request.form.get(
            "email",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        confirm_password = request.form.get(
            "confirm_password",
            ""
        )

        if not name or not email or not password or not confirm_password:

            flash(
                "Please fill in all fields.",
                "error"
            )

            return redirect(url_for("signup"))

        if password != confirm_password:

            flash(
                "Passwords do not match.",
                "error"
            )

            return redirect(url_for("signup"))

        if len(password) < 8:

            flash(
                "Password must contain at least 8 characters.",
                "error"
            )

            return redirect(url_for("signup"))

        password_hash = generate_password_hash(password)

        conn = get_db_connection()

        try:

            conn.execute(
                """
                INSERT INTO users (name, email, password_hash)
                VALUES (?, ?, ?)
                """,
                (name, email, password_hash)
            )

            conn.commit()

        except sqlite3.IntegrityError:

            flash(
                "This email is already registered. Please log in.",
                "error"
            )

            return redirect(url_for("signup"))

        finally:
            conn.close()

        flash(
            "Account created successfully! You can now log in.",
            "success"
        )

        return redirect(url_for("home"))

    return render_template("signup.html")


# --------------------------------------------------
# 12. LOGOUT
# --------------------------------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# --------------------------------------------------
# 13. RUN APPLICATION
# --------------------------------------------------

# Initialize the database when the application starts
init_db()

if __name__ == "__main__":
    app.run(debug=True)