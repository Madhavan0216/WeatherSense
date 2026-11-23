# WeatherSense

WeatherSense is a Python weather-monitoring system with simulation and ESP32/Arduino integration, HTTP and TCP services, alert handling, persistence, and a browser dashboard.

## Features

- Machine-learning based weather predictions
- Configurable sensor simulation
- HTTP telemetry and command API
- TCP socket service
- SQLite and CSV logging
- Browser dashboard and PWA assets
- Docker and Docker Compose deployment
- GitHub Actions CI

## Requirements

- Python 3.11+
- Docker and Docker Compose (optional)

## Quick Start

Create a virtual environment and install dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Copy the example environment file and update secrets before running:

```powershell
Copy-Item .env.example .env
python main.py --sim
```

Open `http://localhost:8080` for the dashboard.

## Commands

```powershell
python main.py --sim       # start with simulated sensors
python main.py --train     # retrain, then start the system
python main.py             # start the system using configuration
python train_model.py      # retrain the ML model only
pytest -q                  # run tests
```

## Docker

```powershell
Copy-Item .env.example .env
docker compose up --build
```

The HTTP dashboard is exposed on port `8080` and the TCP service on port `9000`.

## Configuration

Configuration is read from environment variables. See [.env.example](.env.example) for the supported settings.

## ESP32/Arduino

The firmware is in [arduino/weather_sensor.ino](arduino/weather_sensor.ino). It sends HTTP telemetry and reads relay commands from the backend.

The main device endpoints are:

- `GET /bm` - health ping
- `POST /api/telemetry` - sensor telemetry
- `GET /api/device/commands` - relay command text
- `POST /api/device/commands` - update relay commands

## Project Layout

- `main.py` - application entry point
- `socket_server.py` - HTTP and TCP service startup
- `service.py` / `controller.py` - application logic
- `ml_pipeline.py` / `train_model.py` - model training and inference
- `repository.py` - persistence
- `index.html`, `dashboard.js`, `styles.css` - dashboard
- `tests/` - pytest suite
- `arduino/` - ESP32/Arduino firmware
