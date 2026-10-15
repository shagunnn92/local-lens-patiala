from __future__ import annotations

import io
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import click
import qrcode
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent
INSTANCE = ROOT / "instance"
INSTANCE.mkdir(exist_ok=True)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    DATABASE=str(INSTANCE / "local_lens.sqlite3"),
    MAX_CONTENT_LENGTH=1_000_000,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    neighborhood TEXT NOT NULL,
    description TEXT NOT NULL,
    checkin_token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    city TEXT NOT NULL DEFAULT 'Patiala',
    role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    created_at TEXT NOT NULL,
    UNIQUE(user_id, business_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    business_id INTEGER NOT NULL REFERENCES businesses(id),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    body TEXT NOT NULL CHECK(length(body) BETWEEN 20 AND 1000),
    created_at TEXT NOT NULL,
    visible INTEGER NOT NULL DEFAULT 1,
    risk_note TEXT,
    UNIQUE(user_id, business_id)
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    reporter_id INTEGER NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    UNIQUE(review_id, reporter_id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_business ON reviews(business_id, visible, created_at);
CREATE INDEX IF NOT EXISTS idx_reports_state ON reports(state, created_at);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def init_db() -> None:
    db = get_db()
    db.executescript(SCHEMA)
    count = db.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    if count == 0:
        # Fictional records keep the demo from making claims about real businesses.
        demo_businesses = [
            ("Demo Chai Corner", "Cafe", "Model Town", "Fictional sample listing for trying the review flow."),
            ("Demo Book Nook", "Bookshop", "Urban Estate", "Fictional sample listing for trying the review flow."),
            ("Demo Fresh Bakes", "Bakery", "Leela Bhawan", "Fictional sample listing for trying the review flow."),
        ]
        db.executemany(
            "INSERT INTO businesses(name, category, neighborhood, description, checkin_token) VALUES (?, ?, ?, ?, ?)",
            [(*record, secrets.token_urlsafe(18)) for record in demo_businesses],
        )
    db.commit()


@app.teardown_appcontext
def close_db(_error=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
def prepare_request() -> None:
    init_db()
    user_id = session.get("user_id")
    g.user = get_db().execute("SELECT id, username, city, role FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None


def csrf_value() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(24)
    return session["csrf_token"]


@app.context_processor
def template_globals():
    return {"csrf_token": csrf_value, "current_user": g.get("user")}


@app.before_request
def protect_forms() -> None:
    if request.method == "POST":
        token = request.form.get("csrf_token", "")
        if not token or not secrets.compare_digest(token, session.get("csrf_token", "")):
            abort(400, description="Your form expired. Go back, refresh, and try again.")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            flash("Sign in to do that.", "info")
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def moderator_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None or g.user["role"] != "moderator":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def safe_next_url(candidate: str | None) -> str | None:
    if candidate and candidate.startswith("/") and not candidate.startswith("//") and "\\" not in candidate:
        return candidate
    return None


def business_card_rows(search: str = ""):
    pattern = f"%{search}%"
    return get_db().execute(
        """SELECT b.*, COUNT(r.id) AS review_count, ROUND(AVG(r.rating), 1) AS average_rating
           FROM businesses b LEFT JOIN reviews r ON r.business_id = b.id AND r.visible = 1
           WHERE b.name LIKE ? OR b.category LIKE ? OR b.neighborhood LIKE ?
           GROUP BY b.id ORDER BY b.neighborhood, b.name""",
        (pattern, pattern, pattern),
    ).fetchall()


@app.get("/")
def index():
    search = request.args.get("q", "").strip()[:80]
    return render_template("index.html", businesses=business_card_rows(search), search=search)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not 3 <= len(username) <= 30 or not username.replace("_", "").isalnum():
            flash("Use a 3–30 character username with letters, numbers, or underscores.", "error")
        elif len(password) < 10:
            flash("Use a password with at least 10 characters.", "error")
        else:
            db = get_db()
            try:
                cursor = db.execute(
                    "INSERT INTO users(username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, generate_password_hash(password), utc_now()),
                )
                db.commit()
                session.clear()
                session["user_id"] = cursor.lastrowid
                flash("Welcome! Your Patiala connection is self-declared, not identity-verified.", "success")
                return redirect(safe_next_url(request.args.get("next")) or url_for("index"))
            except sqlite3.IntegrityError:
                flash("That username is already taken.", "error")
    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            flash("You are signed in.", "success")
            return redirect(safe_next_url(request.args.get("next")) or url_for("index"))
        flash("Username or password not recognized.", "error")
    return render_template("auth.html", mode="login")


@app.post("/logout")
@login_required
def logout():
    session.clear()
    flash("You are signed out.", "info")
    return redirect(url_for("index"))


@app.get("/business/<int:business_id>")
def business_detail(business_id: int):
    db = get_db()
    business = db.execute(
        """SELECT b.*, COUNT(r.id) AS review_count, ROUND(AVG(r.rating), 1) AS average_rating
           FROM businesses b LEFT JOIN reviews r ON r.business_id = b.id AND r.visible = 1
           WHERE b.id = ? GROUP BY b.id""", (business_id,),
    ).fetchone()
    if business is None:
        abort(404)
    reviews = db.execute(
        """SELECT r.*, u.username, EXISTS(SELECT 1 FROM checkins c
           WHERE c.user_id = r.user_id AND c.business_id = r.business_id) AS visit_recorded
           FROM reviews r JOIN users u ON u.id = r.user_id
           WHERE r.business_id = ? AND r.visible = 1 ORDER BY r.created_at DESC""",
        (business_id,),
    ).fetchall()
    mine = db.execute("SELECT id FROM reviews WHERE business_id = ? AND user_id = ?", (business_id, g.user["id"])).fetchone() if g.user else None
    checked_in = db.execute("SELECT 1 FROM checkins WHERE business_id = ? AND user_id = ?", (business_id, g.user["id"])).fetchone() if g.user else None
    return render_template("business.html", business=business, reviews=reviews, mine=mine, checked_in=bool(checked_in))


@app.post("/business/<int:business_id>/review")
@login_required
def submit_review(business_id: int):
    db = get_db()
    if db.execute("SELECT 1 FROM businesses WHERE id = ?", (business_id,)).fetchone() is None:
        abort(404)
    body = request.form.get("body", "").strip()
    try:
        rating = int(request.form.get("rating", ""))
    except ValueError:
        rating = 0
    if not 1 <= rating <= 5 or not 20 <= len(body) <= 1000:
        flash("Choose 1–5 stars and write 20–1,000 characters about your experience.", "error")
        return redirect(url_for("business_detail", business_id=business_id))
    normalized = " ".join(body.casefold().split())
    similar = db.execute("SELECT id FROM reviews WHERE lower(replace(body, ' ', '')) = ? LIMIT 1", (normalized.replace(" ", ""),)).fetchone()
    risk_note = "Repeated review text: moderator should check context." if similar else None
    try:
        db.execute(
            "INSERT INTO reviews(user_id, business_id, rating, body, created_at, risk_note) VALUES (?, ?, ?, ?, ?, ?)",
            (g.user["id"], business_id, rating, body, utc_now(), risk_note),
        )
        db.commit()
        flash("Review added. The app records a visit check-in separately from your city membership.", "success")
    except sqlite3.IntegrityError:
        flash("You can leave one review per business. Contact a moderator if you need to update it.", "error")
    return redirect(url_for("business_detail", business_id=business_id))


@app.get("/business/<int:business_id>/qr")
def qr_page(business_id: int):
    business = get_db().execute("SELECT * FROM businesses WHERE id = ?", (business_id,)).fetchone()
    if business is None:
        abort(404)
    checkin_url = url_for("check_in", token=business["checkin_token"], _external=True)
    return render_template("qr.html", business=business, checkin_url=checkin_url)


@app.get("/business/<int:business_id>/qr.png")
def qr_image(business_id: int):
    business = get_db().execute("SELECT checkin_token FROM businesses WHERE id = ?", (business_id,)).fetchone()
    if business is None:
        abort(404)
    target = url_for("check_in", token=business["checkin_token"], _external=True)
    image = qrcode.make(target)
    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", download_name="visit-checkin.png")


@app.route("/checkin/<token>", methods=["GET", "POST"])
@login_required
def check_in(token: str):
    db = get_db()
    business = db.execute("SELECT * FROM businesses WHERE checkin_token = ?", (token,)).fetchone()
    if business is None:
        abort(404)
    if request.method == "POST":
        try:
            db.execute("INSERT INTO checkins(user_id, business_id, created_at) VALUES (?, ?, ?)", (g.user["id"], business["id"], utc_now()))
            db.commit()
            flash("Visit check-in recorded. This is a demo signal, not proof of residence or purchase.", "success")
        except sqlite3.IntegrityError:
            flash("You already have a check-in record for this business.", "info")
        return redirect(url_for("business_detail", business_id=business["id"]))
    return render_template("checkin.html", business=business)


@app.post("/review/<int:review_id>/report")
@login_required
def report_review(review_id: int):
    reason = request.form.get("reason", "").strip()
    details = request.form.get("details", "").strip()[:500]
    allowed = {"not based on a real experience", "personal attack", "spam or copied text", "other"}
    if reason not in allowed:
        flash("Choose a report reason from the list.", "error")
        return redirect(request.referrer or url_for("index"))
    db = get_db()
    try:
        db.execute("INSERT INTO reports(review_id, reporter_id, reason, details, created_at) VALUES (?, ?, ?, ?, ?)", (review_id, g.user["id"], reason, details, utc_now()))
        db.commit()
        flash("Report sent to the moderation queue.", "success")
    except sqlite3.IntegrityError:
        flash("You have already reported this review, or it no longer exists.", "info")
    return redirect(request.referrer or url_for("index"))


@app.get("/moderation")
@moderator_required
def moderation():
    db = get_db()
    reports = db.execute(
        """SELECT p.*, r.body, r.rating, r.visible, r.risk_note, u.username AS reviewer,
           reporter.username AS reporter, b.name AS business_name
           FROM reports p JOIN reviews r ON r.id = p.review_id
           JOIN users u ON u.id = r.user_id JOIN users reporter ON reporter.id = p.reporter_id
           JOIN businesses b ON b.id = r.business_id WHERE p.state = 'open' ORDER BY p.created_at"""
    ).fetchall()
    flagged = db.execute(
        """SELECT r.*, u.username, b.name AS business_name FROM reviews r
           JOIN users u ON u.id = r.user_id JOIN businesses b ON b.id = r.business_id
           WHERE r.risk_note IS NOT NULL ORDER BY r.created_at DESC"""
    ).fetchall()
    hidden = db.execute(
        """SELECT r.*, u.username, b.name AS business_name FROM reviews r
           JOIN users u ON u.id = r.user_id JOIN businesses b ON b.id = r.business_id
           WHERE r.visible = 0 ORDER BY r.created_at DESC"""
    ).fetchall()
    return render_template("moderation.html", reports=reports, flagged=flagged, hidden=hidden)


@app.post("/moderation/review/<int:review_id>/<action>")
@moderator_required
def moderate_review(review_id: int, action: str):
    if action not in {"hide", "restore"}:
        abort(404)
    db = get_db()
    row = db.execute("SELECT id FROM reviews WHERE id = ?", (review_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("UPDATE reviews SET visible = ? WHERE id = ?", (1 if action == "restore" else 0, review_id))
    db.execute("UPDATE reports SET state = 'resolved' WHERE review_id = ? AND state = 'open'", (review_id,))
    db.commit()
    verb = "hidden" if action == "hide" else "restored"
    flash(f"Review {verb}; open reports resolved.", "success")
    return redirect(url_for("moderation"))


@app.post("/moderation/report/<int:report_id>/resolve")
@moderator_required
def resolve_report(report_id: int):
    db = get_db()
    db.execute("UPDATE reports SET state = 'resolved' WHERE id = ?", (report_id,))
    db.commit()
    flash("Report marked resolved.", "success")
    return redirect(url_for("moderation"))


@app.get("/health")
def health():
    get_db().execute("SELECT 1").fetchone()
    return jsonify(status="ok", service="LocalLens Patiala", database="connected")


@app.cli.command("create-moderator")
def create_moderator():
    """Create a moderator account from the terminal."""
    init_db()
    username = click.prompt("Moderator username")
    password = click.prompt("Moderator password", hide_input=True, confirmation_prompt=True)
    if len(password) < 10:
        raise click.ClickException("Password must be at least 10 characters.")
    db = get_db()
    try:
        db.execute("INSERT INTO users(username, password_hash, role, created_at) VALUES (?, ?, 'moderator', ?)", (username, generate_password_hash(password), utc_now()))
        db.commit()
    except sqlite3.IntegrityError as error:
        raise click.ClickException("That username already exists.") from error
    click.echo("Moderator created. Sign in and open /moderation.")


@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", code=400, message=getattr(error, "description", "Bad request.")), 400


@app.errorhandler(403)
def forbidden(_error):
    return render_template("error.html", code=403, message="You do not have permission to view that page."), 403


@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, message="We could not find that page."), 404


if __name__ == "__main__":
    app.run(debug=True)
