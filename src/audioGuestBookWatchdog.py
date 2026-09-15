#!/usr/bin/env python3
import logging
import shutil
import subprocess
import time

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


def set_red_led(on):
    level = "dl" if on else "dh"
    subprocess.run(
        ["pinctrl", "set", str(RED_LED_GPIO), "op", level],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main():
    if shutil.which("pinctrl") is None:
        raise RuntimeError("pinctrl is required for the LED watchdog")

    set_red_led(False)
    restart_requested = False
    red_is_on = False

    try:
        while True:
            if service_is_active():
                if restart_requested:
                    logger.info("Guestbook service is active again")
                restart_requested = False
                red_is_on = False
                set_red_led(False)
                time.sleep(BLINK_INTERVAL)
                continue

            if not restart_requested:
                logger.warning("Guestbook service is not active; restarting it")
                subprocess.run(["systemctl", "restart", SERVICE_NAME], check=False)
                restart_requested = True

            red_is_on = not red_is_on
            set_red_led(red_is_on)
            time.sleep(BLINK_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        set_red_led(False)


if __name__ == "__main__":
    main()
