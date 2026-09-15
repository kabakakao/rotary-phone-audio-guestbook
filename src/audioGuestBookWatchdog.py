#!/usr/bin/env python3
import logging
import subprocess
import time

import RPi.GPIO as GPIO

SERVICE_NAME = "audioGuestBook.service"
RED_LED_GPIO = 2
BLINK_INTERVAL = 0.5

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def service_is_active():
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE_NAME],
        check=False,
    )
    return result.returncode == 0


def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(RED_LED_GPIO, GPIO.OUT, initial=GPIO.HIGH)
    restart_requested = False
    red_is_on = False

    try:
        while True:
            if service_is_active():
                if restart_requested:
                    logger.info("Guestbook service is active again")
                restart_requested = False
                red_is_on = False
                GPIO.output(RED_LED_GPIO, GPIO.HIGH)
                time.sleep(BLINK_INTERVAL)
                continue

            if not restart_requested:
                logger.warning("Guestbook service is not active; restarting it")
                subprocess.run(["systemctl", "restart", SERVICE_NAME], check=False)
                restart_requested = True

            red_is_on = not red_is_on
            GPIO.output(RED_LED_GPIO, GPIO.LOW if red_is_on else GPIO.HIGH)
            time.sleep(BLINK_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        GPIO.output(RED_LED_GPIO, GPIO.HIGH)
        GPIO.cleanup()


if __name__ == "__main__":
    main()
