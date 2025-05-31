"""Entry‑point for the TSURA application."""

from tsura.app import create_app

app = create_app()

if __name__ == "__main__":
    # For local development only (use a proper WSGI server in production)
    app.run(debug=True)
