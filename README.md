# TSURA – Turbo Sliders Unlimited Racing Association

A dark‑themed Flask web application that aggregates and displays racing statistics.

## Local development

```bash
# 1. Install uv (once)
pip install uv

# 2. Install dependencies into a virtualenv of your choice
uv pip install -r requirements.txt

# 3. Configure environment variables
cp .env.example .env  # then edit with real PostgreSQL credentials

# 4. Run the development server (auto‑reload enabled)
flask --app main run --debug
```

## Notes
* Dependencies are managed via **uv**; lock‑files are automatically generated.
* Database access is read‑only and handled through `psycopg.ConnectionPool`.
* All templates use Bootstrap 5 and a custom CSS theme inspired by Formula 1 branding.
