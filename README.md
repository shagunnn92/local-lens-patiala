# LocalLens Patiala

LocalLens is a Patiala-focused review platform prototype. It separates a member's self-declared city from a QR check-in record and provides a small moderation workflow for reported reviews.

## Features

- Member registration and sign-in with hashed passwords
- Business listings with search by name, category, and neighborhood
- One review per member per business, with a 1–5 rating
- QR check-in flow shown separately from city membership
- Review reports and moderator actions to hide or restore content
- Exact repeated-text signal for moderator review; matching text is never hidden automatically
- `/health` endpoint for a basic service status response

## Technology

Python, Flask, SQLite, Jinja, HTML, and CSS. SQLite is created locally on first launch; no separate database service is needed.

## Run locally

Requires Python 3.9 or newer.

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app.py
```

Open <http://127.0.0.1:5000>. Application data is stored in `instance/local_lens.sqlite3`.

## Moderator account

Create a moderator from the project directory:

```powershell
flask --app app create-moderator
```

Sign in with that account and open **Moderation** to review reports and repeated-text signals.

## Prototype scope

The included businesses are fictional demo listings. Patiala membership is self-declared. A check-in records completion of the demo flow; it does not prove residence, physical presence, or purchase. The QR code is static and can be shared. Repeated review text is only a review signal and can have false positives.

The app uses Flask's built-in development server locally. For a public demo, deploy it as a Render Web Service with `pip install -r requirements.txt` as the build command and `gunicorn app:app` as the start command. Set a long random `SECRET_KEY` environment variable in the Render dashboard.

This prototype uses SQLite. Render's free web service has temporary storage, so accounts, reviews, and check-ins can be erased when the service restarts, sleeps, or redeploys. Do not use it for real customer data. Free services may also take about a minute to wake after inactivity.
