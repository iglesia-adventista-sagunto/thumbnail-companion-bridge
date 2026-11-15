# Automatic Thumbnail

[![Python](https://img.shields.io/badge/Python-3.10+-3776AB.svg?style=flat-square&logo=python&logoColor=white)](https://python.org)
[![Proxmox LXC](https://img.shields.io/badge/Proxmox-LXC%20Appliance-E57000.svg?style=flat-square&logo=proxmox&logoColor=white)](https://www.proxmox.com)
[![Docker](https://img.shields.io/badge/Docker-Container%20Ready-2496ED.svg?style=flat-square&logo=docker&logoColor=white)](https://www.docker.com)
[![Systemd](https://img.shields.io/badge/Systemd-Managed%20Service-CC0000.svg?style=flat-square&logo=linux&logoColor=white)](https://systemd.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-success.svg?style=flat-square)](LICENSE)

REST HTTP service and CLI tool to generate branded `1920x1080` livestreaming thumbnails on demand from **Bitfocus Companion**, **Stream Deck**, **OBS**, or automated batch tasks — designed to run natively in a **Proxmox LXC**, **Docker**, or **Windows**.

---

## Architecture Overview

```mermaid
flowchart LR
    client["Bitfocus Companion<br/>/ Automation Client"]
    obs["OBS / vMix / YouTube<br/>Production"]
    subgraph appliance["LXC Container / Docker Host"]
        bridge["Automatic Thumbnail Bridge<br/>(Flask + Gunicorn)"]
        engine["Pillow Rendering Engine<br/>(1920x1080 + Watermark)"]
        storage["./thumbnails<br/>(PNGs + attributions.txt)"]
    end
    unsplash["Unsplash API /<br/>Curated Collections"]

    client -->|"HTTP GET/POST<br/>:5000/generate"| bridge
    bridge --> engine
    engine <-->|"Fetch Backgrounds"| unsplash
    engine -->|"Save PNG + Credits"| storage
    obs -->|"GET /latest<br/>(Image Source)"| bridge
```

---

## Key Features

- **Bitfocus Companion & Stream Deck Ready**: Trigger thumbnail generation via simple `GET` or `POST` requests (`/generate`, `/<preset>/generate`, `/batch`), with plaintext `/is_busy` and `/is_ready` endpoints for live button feedback.
- **Livestreaming Presets**: Define reusable broadcast templates (`sabado`, `viernes`, `estudio`, etc.) in `config.json` and override `title`, `subtitle`, or `query` dynamically from Companion variables.
- **Live Asset Serving (`/latest` & `/thumbnails/<file>`)**: Point OBS Image Sources or Companion dynamic button previews directly to `http://<server-ip>:5000/latest` with `Cache-Control: no-store`.
- **Automatic YouTube Attributions**: Tracks Unsplash photographer credits in `attributions.txt` and exposes them via `GET /attributions`.
- **Dynamic Typography & Branding**: Auto-sizes and wraps titles/subtitles with soft drop shadows, dims backgrounds for contrast, and composites the right sidebar gradient watermark and logo.
- **LXC & Container First**: Includes a native systemd unit, automated Proxmox LXC installer, `update` hook, Docker Compose setup, and backward-compatible CLI (`python script.py miniaturas.csv`).

---

## Repository Structure

```text
automatic-thumbnail/
├── app.py                        # Core Flask REST API service
├── script.py                     # Rendering engine & CLI batch processor
├── config.template.json          # Service & preset template configuration
├── miniaturas.csv                # Sample batch CSV input
├── assets/                       # Watermark & logo PNG assets
├── segoe-ui-black.ttf            # Primary display font
├── segoe-ui-bold.ttf             # Fallback bold font
├── pyproject.toml                # PEP 621 package metadata & CLI entrypoints
├── requirements.txt              # Python dependencies
├── Dockerfile                    # Container image with HEALTHCHECK
├── docker-compose.yml            # Container orchestration with persistent volumes
├── .dockerignore                 # Build exclusion rules (secrets & outputs)
├── install-lxc.sh                # One-command Proxmox / Debian LXC installer
├── update.sh                     # Automated service updater (git pull + pip + restart)
├── automatic-thumbnail.service   # Systemd service unit definition
├── test_app.py                   # Automated test suite (API + rendering)
├── start.sh                      # Linux startup wrapper
├── start.bat                     # Windows startup script
├── LICENSE                       # MIT License
└── README.md                     # Documentation
```

---

## Setup & Configuration

1. Create `config.json` from the template:
   ```bash
   cp config.template.json config.json
   ```

2. Edit `config.json` with your website, optional Unsplash API key, and livestreaming presets:
   ```json
   {
     "port": 5000,
     "output_dir": "./thumbnails",
     "attributions_file": "./thumbnails/attributions.txt",
     "default_website": "sagunto.adventista.es",
     "unsplash_access_key": "",
     "presets": [
       {
         "name": "sabado",
         "title": "Culto de Adoración",
         "subtitle": "Sábado en Directo",
         "query": "iglesia",
         "filename": "miniatura_sabado",
         "variations": 1
       }
     ]
   }
   ```

> Note: `config.json` and `.env` are ignored by git to prevent leaking API keys. Environment variables (`UNSPLASH_ACCESS_KEY`, `PORT`, `OUTPUT_DIR`, `DEFAULT_WEBSITE`, `CONFIG_PATH`) override `config.json` values when set.

---

## Deployment Options

### Option A: Proxmox / Debian LXC (Recommended)

1. Clone or copy the repository inside your LXC container:
   ```bash
   git clone <REPO_URL> /opt/automatic-thumbnail
   cd /opt/automatic-thumbnail
   ```

2. Run the automated installer as root:
   ```bash
   sudo bash install-lxc.sh
   ```
   *The installer provisions dependencies, creates the Python venv, enables `automatic-thumbnail.service`, and links the service to the standard LXC `update` command.*

3. Edit `/opt/automatic-thumbnail/config.json` if needed and restart:
   ```bash
   nano /opt/automatic-thumbnail/config.json
   systemctl restart automatic-thumbnail.service
   ```

#### Updating the LXC Service

Run:
```bash
update
```
*(Or `/opt/automatic-thumbnail/update.sh`)* to pull the latest code, update Python dependencies, and restart `automatic-thumbnail.service`.

---

### Option B: Docker Compose

1. Start the container:
   ```bash
   cd automatic-thumbnail
   docker compose up -d --build
   ```

2. Verify:
   ```bash
   curl http://<server-ip>:5000/health
   ```

---

### Option C: Windows Native

1. Create a virtual environment and install dependencies:
   ```cmd
   python -m venv venv
   venv\Scripts\pip install -r requirements.txt
   ```

2. Start the HTTP service:
   ```cmd
   venv\Scripts\python app.py
   ```
   Or double-click `start.bat`.

3. **CLI Batch Mode** (optional, standalone):
   ```cmd
   venv\Scripts\python script.py miniaturas.csv
   ```

---

## Testing

Run the automated test suite:

```bash
venv/Scripts/python test_app.py
```

The test suite validates:
- Health (`/health`) and status (`/status`, `/is_busy`, `/is_ready`) endpoints.
- Preset resolution and dynamic overrides (`/<preset>/generate`).
- Synchronous and asynchronous (`async=true`) `1920x1080` PNG generation.
- Safe asset serving (`/thumbnails`, `/latest`, `/attributions`, `DELETE`) with path traversal protection.
- Batch CSV processing (`POST /batch`).

---

## API Reference

### System & Status Endpoints

| Method | URL | Description |
| :--- | :--- | :--- |
| GET | `/` or `/health` | Health check with service status, font/Unsplash readiness, and preset count |
| GET | `/status` | Current job state (`idle`, `generating`, `ready`, `error`), last file, and counters |
| GET | `/is_busy` | Plaintext `true` or `false` while rendering (ideal for Companion feedback) |
| GET | `/is_ready` | Plaintext `true` or `false` when idle and thumbnails are available |
| POST | `/reload` | Reload `config.json` presets without restarting |

### Generation Endpoints

| Action | Method | URL | Description |
| :--- | :--- | :--- | :--- |
| Generate | GET / POST | `/generate` | Render from `title`, `subtitle`, `query`, `filename`, `variations`, `async` |
| Preset Generate | GET / POST | `/<preset>/generate` | Render using a named preset (supports query/JSON overrides) |
| List Presets | GET | `/presets` | List all configured presets |
| Batch CSV | GET / POST | `/batch` | Process `csv_path` (default `miniaturas.csv`) or raw `csv_content` |

### Asset & Attribution Endpoints

| Action | Method | URL | Description |
| :--- | :--- | :--- | :--- |
| List Thumbnails | GET | `/thumbnails` | List generated PNGs sorted newest-first |
| Get Thumbnail | GET | `/thumbnails/<filename>` | Serve a specific PNG (`image/png`) |
| Delete Thumbnail | DELETE | `/thumbnails/<filename>` | Remove a specific PNG |
| Latest Thumbnail | GET | `/latest` | Serve the most recently generated PNG (`?format=json` for metadata) |
| Attributions | GET | `/attributions` | Plaintext Unsplash credits (`?format=json` for JSON) |

---

## Bitfocus Companion Setup

### 1. One-Button Preset Generation

1. Open Companion web UI at `http://<companion-ip>:8000`.
2. Under **Connections**, add **Generic HTTP Requests**.
3. Select an empty button:
   - Set button text to `Thumbnail\nSábado`.
   - Under **Press Actions**, add **generic-http: HTTP POST** (or **HTTP GET**).
   - Set URL:
     ```text
     http://$(custom:thumbnail_server_ip):5000/sabado/generate?async=true
     ```
4. Press the button to trigger generation in the background.

### 2. Live Busy / Ready Button Feedback

1. **Create Custom Variables**:
   - `thumbnail_busy` (default `false`)
   - `thumbnail_ready` (default `false`)

2. **Create Periodic Polling Trigger** (Interval: `2000 ms`):
   - Action 1: **generic-http: HTTP GET** ➔ `http://$(custom:thumbnail_server_ip):5000/is_busy` ➔ Store result as text in `custom:thumbnail_busy`.
   - Action 2: **generic-http: HTTP GET** ➔ `http://$(custom:thumbnail_server_ip):5000/is_ready` ➔ Store result as text in `custom:thumbnail_ready`.

3. **Add Button Feedbacks**:
   - When `custom:thumbnail_busy == true` ➔ Background **Amber/Yellow** (`#ffaa00`).
   - When `custom:thumbnail_ready == true` ➔ Background **Green** (`#00aa44`).

### 3. OBS / vMix Integration

Add a **Browser Source** or **Image Source** pointing to:
```text
http://<server-ip>:5000/latest
```
Every time a button in Companion generates a new thumbnail, `/latest` automatically serves the newest `1920x1080` PNG.
