# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies (requires uv)
uv sync

# Run dev server (auto-reload)
flask --app main run --debug

# Environment setup
cp .env.example .env  # fill in TSU_HOTLAPPING_POSTGRES_URL and TSURA_STEAM_API_KEY
```

There are no tests or linters configured.

## Architecture

TSURA is a read-only Flask dashboard for the Turbo Sliders Unlimited Racing Association. It reads from a PostgreSQL database (read-only `tsura` user) and fetches live server data from the Steam API.

**Request flow:**
- `main.py` — entry point, calls `create_app()`
- `tsura/app/__init__.py` — app factory: loads `.env`, initialises the DB pool, registers blueprints
- `tsura/app/extensions.py` — `PsycopgPool` wraps `psycopg_pool.ConnectionPool`; one connection is borrowed per request via Flask's `g` and returned in `teardown_appcontext`
- `tsura/app/blueprints/main/` — the only blueprint; all routes and Jinja2 templates live here

**Database:** All SQL targets read-only views in the `mart` schema. The `tsura` DB user has SELECT only on these views:
- `mart.v_race_results` — one row per human participant per race (events + heats + casual_heat)
- `mart.v_driver_profile` — one row per driver: ELO, race stats, hotlap stats
- `mart.v_hotlap_grouped_sessions` — hotlap sessions grouped by consecutive same-track runs
- `mart.v_hotlap_group_results` — all laps within a grouped session
- `mart.v_hotlap_results` / `mart.v_hotlap_sessions` — legacy/detail views

Use `dict_row` row factory so rows come back as dicts.

**Templates** use Bootstrap 5 with a Formula 1-branded dark theme (`tsura/app/static/css/style.css`) and extend `base.html`. `base.html` has a `{% block extra_scripts %}` block for page-specific JS (used by driver profile for Chart.js).

**Server labels in DB (important):**
- `server='events'` — Liga-Event server
- `server='heats'` — Tripleheat (TEMPORARY: to be renamed 'tripleheat' next session)
- `server='casual_heat'` — Casual-Heat (TEMPORARY: most data still mislabeled as 'heats')
- `server='hotlapping'` — dedicated hotlap server

**Environment variables:**
- `TSU_HOTLAPPING_POSTGRES_URL` — required; startup fails without it (tsura read-only user)
- `TSURA_STEAM_API_KEY` — optional; missing key silently results in an empty server list on the index page

## Deployment

Production runs as systemd user service under the `tsura` user on carrot:
```bash
cd /home/tsura/tsura2 && git pull
sudo systemctl --machine=tsura@ --user restart dev_tsura.service
```
