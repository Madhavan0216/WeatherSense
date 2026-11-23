"""
main.py — Entry point for the Weather Monitoring System.

Usage:
    python main.py            # start full system (backend + HTTP server)
    python main.py --train    # train/retrain ML model first, then start
    python main.py --sim      # force simulation mode regardless of config
"""

import sys
import logging
import os
import subprocess

# ── Logging Setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("wms.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

import config


def main() -> None:
    args = sys.argv[1:]

    if "--sim" in args:
        config.SIMULATION_MODE = True
        logger.info("Forced simulation mode via CLI flag.")

    if "--train" in args:
        logger.info("Training ML model …")
        train_script = os.path.join(os.path.dirname(__file__), "train_model.py")
        subprocess.run([sys.executable, train_script], check=True)
        logger.info("Model training complete.")

    logger.info("=" * 60)
    logger.info("  Weather Monitoring System")
    logger.info("  Dashboard → http://localhost:%d", config.HTTP_PORT)
    logger.info("  TCP Socket → port %d", config.SOCKET_PORT)
    logger.info("  Simulation mode: %s", config.SIMULATION_MODE)
    logger.info("=" * 60)

    from socket_server import start_all
    start_all()


if __name__ == "__main__":
    main()
