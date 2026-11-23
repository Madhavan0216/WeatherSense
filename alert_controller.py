"""
alert_controller.py — Controls physical LEDs and buzzer based on stage.
Simulation mode logs actions instead of writing to GPIO.
"""

import time
import logging
import threading
from typing import Any

import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import config
from interfaces import AlertPort

logger = logging.getLogger(__name__)


class AlertController(AlertPort):
    """
    Manages three LEDs and a buzzer.

    LED pin mapping (BCM):
      LED1 (Yellow/Orange) → GPIO 18  — Temperature alert
      LED2 (Blue)          → GPIO 23  — Light rain
      LED3 (Red)           → GPIO 24  — Critical / heavy rain
      Buzzer               → GPIO 25  — Critical alert
    """

    LED_PINS    = {1: 18, 2: 23, 3: 24}
    BUZZER_PIN  = 25

    def __init__(self, simulation: bool = config.SIMULATION_MODE):
        self.simulation    = simulation
        self._gpio_ok      = False
        self._gpio: Any | None = None
        self._buzzer_thread: threading.Thread | None = None
        self._buzzer_stop  = threading.Event()
        self._current_stage = 0

        if not simulation:
            self._init_gpio()

        logger.info("AlertController ready (simulation=%s)", simulation)

    # ── GPIO Initialisation ──────────────────────────────────────────────────
    def _init_gpio(self) -> None:
        try:
            import RPi.GPIO as GPIO    # type: ignore
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)
            for pin in list(self.LED_PINS.values()) + [self.BUZZER_PIN]:
                GPIO.setup(pin, GPIO.OUT, initial=GPIO.LOW)
            self._gpio    = GPIO
            self._gpio_ok = True
            logger.info("AlertController GPIO initialised.")
        except Exception as exc:
            logger.warning("AlertController GPIO failed (%s) — simulation mode.", exc)
            self.simulation = True

    # ── Public API ───────────────────────────────────────────────────────────
    def apply(self, stage: int) -> None:
        """Set LED/buzzer state for the given stage (0–2)."""
        if stage == self._current_stage:
            return
        self._current_stage = stage
        self._clear_all()

        if stage == 0:     # NORMAL
            pass
        elif stage == 1:   # WARNING — temperature OR light rain
            self._set_led(1, True)
        elif stage == 2:   # WARNING — light rain
            self._set_led(2, True)
        else:              # CRITICAL
            self._set_led(3, True)
            self._start_buzzer()

    def shutdown(self) -> None:
        """Turn everything off and release resources."""
        self._clear_all()
        if self._gpio_ok:
            try:
                gpio = self._gpio
                if gpio is not None:
                    gpio.cleanup()
            except Exception:
                pass
        logger.info("AlertController shut down.")

    # ── Internal ─────────────────────────────────────────────────────────────
    def _set_led(self, led_id: int, state: bool) -> None:
        if self.simulation:
            logger.debug("SIM LED%d → %s", led_id, "ON" if state else "OFF")
            return
        gpio = self._gpio
        if gpio is None:
            return
        pin = self.LED_PINS[led_id]
        gpio.output(pin, gpio.HIGH if state else gpio.LOW)

    def _set_buzzer(self, state: bool) -> None:
        if self.simulation:
            logger.debug("SIM BUZZER → %s", "ON" if state else "OFF")
            return
        gpio = self._gpio
        if gpio is None:
            return
        gpio.output(self.BUZZER_PIN, gpio.HIGH if state else gpio.LOW)

    def _clear_all(self) -> None:
        self._stop_buzzer()
        for i in self.LED_PINS:
            self._set_led(i, False)

    def _start_buzzer(self) -> None:
        self._stop_buzzer()
        self._buzzer_stop.clear()

        def _beep() -> None:
            while not self._buzzer_stop.is_set():
                self._set_buzzer(True)
                time.sleep(config.BUZZER_BEEP_INTERVAL)
                self._set_buzzer(False)
                time.sleep(config.BUZZER_BEEP_INTERVAL)

        self._buzzer_thread = threading.Thread(target=_beep, daemon=True)
        self._buzzer_thread.start()

    def _stop_buzzer(self) -> None:
        if self._buzzer_thread and self._buzzer_thread.is_alive():
            self._buzzer_stop.set()
            self._buzzer_thread.join(timeout=2)
        self._set_buzzer(False)
