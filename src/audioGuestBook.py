#!/usr/bin/env python3
import logging
import RPi.GPIO as GPIO
import subprocess
import time
import threading
import yaml
from datetime import datetime
from pathlib import Path
import os
import shutil
import sys
import tempfile

# Optional WS2812B LED support (install rpi-ws281x to enable)
try:
    from rpi_ws281x import PixelStrip, Color as _WS2812Color
    _WS2812_AVAILABLE = True
except ImportError:
    _WS2812_AVAILABLE = False
    _WS2812Color = None

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def load_config(config_path):
    """Load configuration from YAML file."""
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError as e:
        logger.error(f"Configuration file not found: {e}")
        sys.exit(1)

# Global state
recording_proc = None
recording_start_ts = None
record_greeting_proc = None

# WS2812B LED strip instance (None = disabled or not initialised)
led_strip = None

# LED hardware constants (sensible defaults for RPi WS2812B wiring)
_LED_FREQ_HZ = 800000   # 800 kHz signal
_LED_DMA     = 10       # DMA channel
_LED_INVERT  = False    # True if using NPN transistor level-shift
_LED_CHANNEL = 0        # 0 for GPIO 18/12, 1 for GPIO 13/19


def setup_led(config):
    """Initialise a single WS2812B LED on the configured GPIO pin.
    Set led_gpio: 0 in config to disable."""
    global led_strip
    led_gpio = int(config.get('led_gpio', 0))
    if not _WS2812_AVAILABLE or led_gpio == 0:
        if led_gpio != 0 and not _WS2812_AVAILABLE:
            logger.warning("led_gpio is set but rpi_ws281x is not installed – LED disabled. "
                           "Install with: sudo pip3 install rpi-ws281x")
        return
    brightness = max(0, min(int(config.get('led_brightness', 128)), 255))
    try:
        strip = PixelStrip(1, led_gpio, _LED_FREQ_HZ, _LED_DMA, _LED_INVERT,
                           brightness, _LED_CHANNEL)
        strip.begin()
        led_strip = strip
        logger.info(f"WS2812B LED initialised on GPIO {led_gpio}, brightness={brightness}")
    except Exception as e:
        logger.warning(f"WS2812B LED setup failed (check wiring / run as root): {e}")


def set_led_color(r, g, b):
    """Set the LED to the given RGB colour. No-op if LED is not initialised."""
    if led_strip is None:
        return
    try:
        led_strip.setPixelColor(0, _WS2812Color(r, g, b))
        led_strip.show()
    except Exception as e:
        logger.debug(f"LED colour update failed: {e}")


def led_off():
    """Turn the LED off."""
    set_led_color(0, 0, 0)


def _clamp_float(value, default, minimum, maximum):
    """Parse a float-like value and clamp it to a safe range."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))

def set_volume(volume_pct, mixer_control):
    """Set system volume using amixer."""
    vol = max(0, min(int(volume_pct * 100), 100))
    subprocess.run(["amixer", "set", mixer_control, f"{vol}%"], check=False, 
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def set_capture_volume(volume_pct, mixer_control):
    """Set capture/input level using amixer; tolerate card-specific control quirks."""
    vol = max(0, min(int(volume_pct * 100), 100))
    subprocess.run(["amixer", "set", mixer_control, f"{vol}%", "cap"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["amixer", "set", mixer_control, f"{vol}%"], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def is_on_hook(pin, hook_type, invert_hook):
    """
    Determine if handset is on-hook based on GPIO state and configuration.
    
    For NC (Normally Closed) with pull-up:
      - When on-hook: circuit closed, GPIO pulled to GND → reads LOW
      - When off-hook: circuit open, pull-up resistor → reads HIGH
      - Therefore: HIGH = on-hook, LOW = off-hook
      
    Actually, based on working simple implementation:
      - NC: HIGH = on-hook, LOW = off-hook (handset down = high)
      
    For NO (Normally Open):
      - When on-hook: circuit open → reads HIGH (with pull-up)
      - When off-hook: circuit closed → reads LOW
      - Therefore: LOW = on-hook, HIGH = off-hook
    
    invert_hook flips the logic.
    """
    state = GPIO.input(pin)
    
    if hook_type == "NC":
        # NC: HIGH = on-hook, LOW = off-hook (based on working simple implementation)
        on_hook = (state == GPIO.HIGH)
    else:  # NO
        # NO: LOW = on-hook, HIGH = off-hook
        on_hook = (state == GPIO.LOW)
    
    if invert_hook:
        on_hook = not on_hook
    
    return on_hook

# File types that arecord/aplay can handle natively.
NATIVE_FILE_TYPES = {"wav", "raw", "au", "voc"}
# File types that require ffmpeg for encoding/decoding (not understood by arecord/aplay).
FFMPEG_FILE_TYPES = {"mp3", "ogg"}


def play_audio_interruptible(file_path, pin_hook, hw_mapping, volume, mixer_control, hook_type, invert_hook, playback_gain=1.0):
    """
    Play an audio file (wav via aplay, or mp3/ogg via ffmpeg), checking GPIO during playback.
    Returns True if played to completion, False if interrupted by on-hook.
    """
    if not Path(file_path).exists():
        logger.error(f"Missing audio file: {file_path}")
        return False
    
    logger.info(f"Playing: {Path(file_path).name}")
    set_volume(volume, mixer_control)
    playback_gain = _clamp_float(playback_gain, 1.0, 0.1, 4.0)
    
    ext = Path(file_path).suffix.lower().lstrip('.')
    if ext in FFMPEG_FILE_TYPES or abs(playback_gain - 1.0) > 1e-6:
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(file_path)]
        if abs(playback_gain - 1.0) > 1e-6:
            cmd.extend(["-filter:a", f"volume={playback_gain}"])
        cmd.extend(["-f", "alsa", hw_mapping])
    else:
        cmd = ["aplay", "-q", "-D", hw_mapping, str(file_path)]
    
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        logger.error(f"'{cmd[0]}' not found. Install it to play .{ext} files (e.g. 'sudo apt install ffmpeg').")
        return False
    
    try:
        while proc.poll() is None:
            # Check if handset is on-hook
            if is_on_hook(pin_hook, hook_type, invert_hook):
                logger.info(f"Interrupted {Path(file_path).name} (on-hook)")
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return False
            time.sleep(0.05)
    except Exception as e:
        logger.error(f"Playback error: {e}")
        return False
    
    return True

def record_audio(out_file, config):
    """
    Start a recording process writing to out_file. Uses arecord for native
    formats (wav/raw/au/voc) and ffmpeg for mp3/ogg, chosen by out_file's extension.
    """
    ext = Path(out_file).suffix.lower().lstrip('.')
    recording_gain = _clamp_float(config.get('recording_gain', 1.0), 1.0, 0.1, 4.0)
    requires_ffmpeg = ext in FFMPEG_FILE_TYPES or abs(recording_gain - 1.0) > 1e-6

    # For compressed targets we capture PCM first to avoid ffmpeg startup latency
    # clipping the first words of a message, then transcode on stop.
    if ext in FFMPEG_FILE_TYPES:
        if shutil.which("ffmpeg") is None:
            logger.error("'ffmpeg' not found. Install it to record compressed formats (e.g. 'sudo apt install ffmpeg').")
            return None

        tmp_handle = tempfile.NamedTemporaryFile(
            prefix=f"{Path(out_file).stem}.",
            suffix=".capture.wav",
            dir=str(Path(out_file).parent),
            delete=False,
        )
        tmp_capture = Path(tmp_handle.name)
        tmp_handle.close()

        cmd = [
            "arecord", "-q",
            "-f", config['format'],
            "-t", "wav",
            "-D", config['alsa_hw_mapping'],
            "-r", str(config['sample_rate']),
            "-c", str(config['channels']),
            str(tmp_capture)
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            logger.error("'arecord' not found. Is ALSA installed?")
            try:
                tmp_capture.unlink(missing_ok=True)
            except TypeError:
                if tmp_capture.exists():
                    tmp_capture.unlink()
            return None

        proc._agb_finalize = {
            "mode": "transcode",
            "temp_file": str(tmp_capture),
            "out_file": str(out_file),
            "recording_gain": recording_gain,
        }
        return proc
    
    if requires_ffmpeg:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "alsa",
            "-ar", str(config['sample_rate']),
            "-ac", str(config['channels']),
            "-i", config['alsa_hw_mapping'],
        ]
        if abs(recording_gain - 1.0) > 1e-6:
            cmd.extend(["-filter:a", f"volume={recording_gain}"])
        cmd.append(str(out_file))
    else:
        arecord_type = ext if ext in NATIVE_FILE_TYPES else "wav"
        cmd = [
            "arecord", "-q",
            "-f", config['format'],
            "-t", arecord_type,
            "-D", config['alsa_hw_mapping'],
            "-r", str(config['sample_rate']),
            "-c", str(config['channels']),
            str(out_file)
        ]
    
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        if requires_ffmpeg and ext not in FFMPEG_FILE_TYPES:
            logger.warning("'ffmpeg' not found, falling back to arecord without recording_gain boost.")
            arecord_type = ext if ext in NATIVE_FILE_TYPES else "wav"
            fallback_cmd = [
                "arecord", "-q",
                "-f", config['format'],
                "-t", arecord_type,
                "-D", config['alsa_hw_mapping'],
                "-r", str(config['sample_rate']),
                "-c", str(config['channels']),
                str(out_file)
            ]
            try:
                return subprocess.Popen(fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                logger.error("'arecord' not found. Is ALSA installed?")
                return None
        logger.error(f"'{cmd[0]}' not found. Install it to record .{ext} files (e.g. 'sudo apt install ffmpeg').")
        return None


def _finalize_compressed_recording_async(temp_file, out_file, recording_gain, name="recording"):
    """Transcode a finished WAV capture in the background.

    The WAV remains on disk if transcoding fails so the recording is never lost.
    """

    def worker(source_wav, target_file, gain, label):
        source_path = Path(source_wav)
        target_path = Path(target_file)

        if not source_path.exists():
            logger.error(f"Temporary capture missing; cannot finalize {label}: {source_path}")
            return

        if shutil.which("ffmpeg") is None:
            logger.warning(f"'ffmpeg' not found while finalizing {label}; keeping WAV fallback: {source_path.name}")
            return

        partial_target = target_path.with_name(f"{target_path.name}.partial")
        try:
            if partial_target.exists():
                partial_target.unlink()
        except OSError:
            pass

        transcode_cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source_path)]
        if abs(gain - 1.0) > 1e-6:
            transcode_cmd.extend(["-filter:a", f"volume={gain}"])
        transcode_cmd.append(str(partial_target))

        try:
            result = subprocess.run(
                transcode_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            logger.warning(f"ffmpeg vanished while finalizing {label}; keeping WAV fallback: {source_path.name}")
            return

        if result and result.returncode == 0 and partial_target.exists():
            try:
                partial_target.replace(target_path)
                source_path.unlink()
                logger.info(f"Finalized {label}: {target_path.name}")
            except OSError as e:
                logger.error(f"Failed to publish finalized {label}; keeping WAV fallback {source_path.name}: {e}")
                try:
                    if partial_target.exists():
                        partial_target.unlink()
                except OSError:
                    pass
            return

        logger.error(
            f"Failed to transcode {label} to {target_path.suffix}; keeping WAV fallback {source_path.name}."
        )
        try:
            if partial_target.exists():
                partial_target.unlink()
        except OSError:
            pass

    thread = threading.Thread(
        target=worker,
        args=(str(temp_file), str(out_file), recording_gain, name),
        daemon=True,
        name=f"finalize-{Path(out_file).stem}",
    )
    thread.start()

def start_recording(config):
    """Start recording process for guest recording."""
    timestamp = datetime.now().isoformat().replace(':','-')
    recordings_path = Path(config['recordings_path'])
    recordings_path.mkdir(exist_ok=True)
    
    ext = config.get('file_type', 'wav')
    out_file = recordings_path / f"{timestamp}.{ext}"
    logger.info(f"Recording to: {out_file.name}")

    capture_control = config.get('capture_mixer_control_name', 'Capture')
    capture_volume = _clamp_float(config.get('capture_volume', 1.0), 1.0, 0.0, 1.0)
    set_capture_volume(capture_volume, capture_control)
    
    return record_audio(out_file, config)

def start_recording_greeting(config):
    """Start recording process for recording greeting message."""
    greeting_path = Path(config['greeting'])
    greeting_path.parent.mkdir(exist_ok=True)
    
    logger.info(f"Recording greeting to: {greeting_path.name}")

    capture_control = config.get('capture_mixer_control_name', 'Capture')
    capture_volume = _clamp_float(config.get('capture_volume', 1.0), 1.0, 0.0, 1.0)
    set_capture_volume(capture_volume, capture_control)
    
    return record_audio(greeting_path, config)

def stop_recording(proc, name="recording"):
    """Stop an arecord process if running."""
    if proc and proc.poll() is None:
        logger.info(f"Stopping {name}")
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    finalize = getattr(proc, "_agb_finalize", None)
    if not finalize or finalize.get("mode") != "transcode":
        return

    temp_file = Path(finalize["temp_file"])
    out_file = Path(finalize["out_file"])
    gain = _clamp_float(finalize.get("recording_gain", 1.0), 1.0, 0.1, 4.0)
    if not temp_file.exists():
        logger.error(f"Temporary capture missing, cannot finalize {name}: {temp_file}")
        return

    fallback_file = out_file.with_suffix(".wav")
    try:
        fallback_file.parent.mkdir(parents=True, exist_ok=True)
        if fallback_file.exists():
            fallback_file.unlink()
        temp_file.rename(fallback_file)
    except OSError as e:
        logger.error(f"Failed to store WAV fallback for {name}: {e}")
        return

    _finalize_compressed_recording_async(fallback_file, out_file, gain, name=name)

def check_shutdown_button(pin_shutdown, hold_time=4.0):
    """
    Check if shutdown button is held LOW for hold_time seconds.
    If so, initiate system shutdown.
    """
    if GPIO.input(pin_shutdown) == GPIO.LOW:
        start = time.time()
        while GPIO.input(pin_shutdown) == GPIO.LOW:
            if time.time() - start >= hold_time:
                logger.warning(f"Shutdown button held for {hold_time}s -> shutting down...")
                stop_recording(recording_proc)
                stop_recording(record_greeting_proc, "greeting recording")
                logger.warning("System shutting down...")
                os.system("sudo shutdown now")
                return True
            time.sleep(0.1)
    return False

def main():
    global recording_proc, recording_start_ts, record_greeting_proc
    
    # Load configuration
    config_path = Path(__file__).parent / "../config.yaml"
    config = load_config(config_path)
    
    logger.info(f"Loaded configuration from: {config_path}")
    
    # Setup GPIO
    GPIO.setmode(GPIO.BCM)
    
    # Hook GPIO (handset)
    GPIO.setup(config['hook_gpio'], GPIO.IN, pull_up_down=GPIO.PUD_UP)

    # LED setup – red = on hook (idle)
    setup_led(config)
    set_led_color(255, 0, 0)
    
    # Record greeting button (optional)
    has_record_greeting = config.get('record_greeting_gpio', 0) != 0
    if has_record_greeting:
        GPIO.setup(config['record_greeting_gpio'], GPIO.IN, pull_up_down=GPIO.PUD_UP)
        prev_record_greeting_state = GPIO.input(config['record_greeting_gpio'])
    
    # Shutdown button (optional)
    has_shutdown = config.get('shutdown_gpio', 0) != 0
    if has_shutdown:
        GPIO.setup(config['shutdown_gpio'], GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
    logger.info("=" * 50)
    logger.info("Rotary Phone Audio Guest Book - Ready")
    logger.info("Lift handset to begin recording a message")
    logger.info("=" * 50)
    
    # Get hook configuration
    hook_type = config.get('hook_type', 'NC')
    invert_hook = config.get('invert_hook', False)
    hook_bounce_time = config.get('hook_bounce_time', 0.1)  # Default 0.1s
    
    prev_was_on_hook = is_on_hook(config['hook_gpio'], hook_type, invert_hook)
    
    try:
        while True:
            # Check current hook state
            currently_on_hook = is_on_hook(config['hook_gpio'], hook_type, invert_hook)
            
            # Detect state change
            if currently_on_hook != prev_was_on_hook:
                # State changed - verify it's stable for bounce_time before acting
                change_time = time.time()
                stable_state = currently_on_hook
                
                # Wait and verify stability
                while time.time() - change_time < hook_bounce_time:
                    current_check = is_on_hook(config['hook_gpio'], hook_type, invert_hook)
                    if current_check != stable_state:
                        # State bounced back, ignore this change
                        stable_state = current_check
                        change_time = time.time()
                    time.sleep(0.01)  # Check every 10ms during debounce
                
                # After debounce period, verify final state
                final_state = is_on_hook(config['hook_gpio'], hook_type, invert_hook)
                if final_state != prev_was_on_hook:
                    # State change confirmed after debounce
                    currently_on_hook = final_state
                else:
                    # State bounced back to original, ignore
                    currently_on_hook = prev_was_on_hook
            
            # ========== MAIN HANDSET HOOK LOGIC ==========
            
            # OFF-HOOK: User lifted handset
            if prev_was_on_hook and not currently_on_hook:
                logger.info("\n[OFF-HOOK] Handset lifted")
                set_led_color(255, 100, 0)  # Yellow – greeting / beep playing

                # Greeting start delay
                delay = config.get('greeting_start_delay', 0)
                if delay > 0:
                    logger.info(f"Waiting {delay}s before greeting...")
                    time.sleep(delay)
                    # Check if user hung up during delay
                    if is_on_hook(config['hook_gpio'], hook_type, invert_hook):
                        logger.info("Handset replaced during delay - aborting")
                        prev_was_on_hook = is_on_hook(config['hook_gpio'], hook_type, invert_hook)
                        continue
                
                # Play greeting (interruptible)
                if not play_audio_interruptible(
                    config['greeting'],
                    config['hook_gpio'],
                    config['alsa_hw_mapping'],
                    config['greeting_volume'],
                    config['mixer_control_name'],
                    hook_type,
                    invert_hook,
                    config.get('playback_gain', 1.0)
                ):
                    prev_was_on_hook = is_on_hook(config['hook_gpio'], hook_type, invert_hook)
                    continue
                
                # Beep delay
                beep_delay = config.get('beep_start_delay', 0)
                if beep_delay > 0:
                    time.sleep(beep_delay)
                
                # Play beep (interruptible)
                if not play_audio_interruptible(
                    config['beep'],
                    config['hook_gpio'],
                    config['alsa_hw_mapping'],
                    config['beep_volume'],
                    config['mixer_control_name'],
                    hook_type,
                    invert_hook,
                    config.get('playback_gain', 1.0)
                ):
                    prev_was_on_hook = is_on_hook(config['hook_gpio'], hook_type, invert_hook)
                    continue
                
                # Start recording if still off-hook
                if not is_on_hook(config['hook_gpio'], hook_type, invert_hook) and recording_proc is None:
                    recording_proc = start_recording(config)
                    recording_start_ts = time.time()
                    set_led_color(0, 255, 0)  # Green – recording
            
            # ON-HOOK: User replaced handset
            if not prev_was_on_hook and currently_on_hook:
                logger.info("[ON-HOOK] Handset replaced")
                set_led_color(255, 0, 0)  # Red – on hook (idle)
                if recording_proc:
                    stop_recording(recording_proc)
                    recording_proc = None
                    recording_start_ts = None
            
            # Check max recording duration
            if recording_proc and recording_proc.poll() is None and recording_start_ts:
                elapsed = time.time() - recording_start_ts
                if elapsed >= config['recording_limit']:
                    logger.warning(f"[TIME EXCEEDED] Max recording time {config['recording_limit']}s reached")
                    stop_recording(recording_proc)
                    recording_proc = None
                    recording_start_ts = None
                    set_led_color(255, 100, 0)  # Yellow – playing time-exceeded message

                    # Play time exceeded message (interruptible)
                    play_audio_interruptible(
                        config['time_exceeded'],
                        config['hook_gpio'],
                        config['alsa_hw_mapping'],
                        config['time_exceeded_volume'],
                        config['mixer_control_name'],
                        hook_type,
                        invert_hook,
                        config.get('playback_gain', 1.0)
                    )
            
            prev_was_on_hook = currently_on_hook
            
            # ========== RECORD GREETING BUTTON LOGIC ==========
            
            if has_record_greeting:
                record_greeting_state = GPIO.input(config['record_greeting_gpio'])
                
                # Debounce record greeting button
                record_greeting_bounce_time = config.get('record_greeting_bounce_time', 0.1)
                
                # Detect state change
                if record_greeting_state != prev_record_greeting_state:
                    # State changed - verify it's stable for bounce_time
                    change_time = time.time()
                    stable_state = record_greeting_state
                    
                    # Wait and verify stability
                    while time.time() - change_time < record_greeting_bounce_time:
                        current_check = GPIO.input(config['record_greeting_gpio'])
                        if current_check != stable_state:
                            # State bounced back
                            stable_state = current_check
                            change_time = time.time()
                        time.sleep(0.01)
                    
                    # After debounce period, verify final state
                    final_state = GPIO.input(config['record_greeting_gpio'])
                    if final_state != prev_record_greeting_state:
                        # State change confirmed
                        record_greeting_state = final_state
                    else:
                        # State bounced back to original
                        record_greeting_state = prev_record_greeting_state
                
                # Button pressed (HIGH -> LOW for NC)
                if prev_record_greeting_state == GPIO.HIGH and record_greeting_state == GPIO.LOW:
                    logger.info("\n[RECORD GREETING] Button pressed - recording new greeting")
                    
                    # Play beep to indicate recording start
                    play_audio_interruptible(
                        config['beep'],
                        config['record_greeting_gpio'],  # Use record button as interrupt
                        config['alsa_hw_mapping'],
                        config['beep_volume'],
                        config['mixer_control_name'],
                        config.get('record_greeting_type', 'NC'),
                        False,  # No invert for record greeting
                        config.get('playback_gain', 1.0)
                    )
                    
                    # Start recording greeting
                    if record_greeting_proc is None:
                        record_greeting_proc = start_recording_greeting(config)
                
                # Button released (LOW -> HIGH for NC)
                if prev_record_greeting_state == GPIO.LOW and record_greeting_state == GPIO.HIGH:
                    logger.info("[RECORD GREETING] Button released - saving greeting")
                    if record_greeting_proc:
                        stop_recording(record_greeting_proc, "greeting recording")
                        record_greeting_proc = None
                
                prev_record_greeting_state = record_greeting_state
            
            # ========== SHUTDOWN BUTTON CHECK ==========
            
            if has_shutdown:
                if check_shutdown_button(
                    config['shutdown_gpio'],
                    hold_time=config.get('shutdown_button_hold_time', 4.0)
                ):
                    break  # Shutting down
            
            # Main loop delay
            time.sleep(0.05)
    
    except KeyboardInterrupt:
        logger.info("\n\nExiting...")
    finally:
        stop_recording(recording_proc)
        stop_recording(record_greeting_proc, "greeting recording")
        led_off()
        GPIO.cleanup()
        logger.info("Cleanup complete. Goodbye!")

if __name__ == "__main__":
    main()
