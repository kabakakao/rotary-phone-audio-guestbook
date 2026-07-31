#!/usr/bin/env python3
import logging
import RPi.GPIO as GPIO
import subprocess
import time
import yaml
from datetime import datetime
from pathlib import Path
import os
import sys

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
recording_capture_file = None
recording_output_file = None
recording_file_type = "wav"
record_greeting_proc = None

SUPPORTED_OUTPUT_FILE_TYPES = {"wav", "mp3", "ogg"}
MISCONFIGURED_ARECORD_FORMATS = {"wav", "wave", "mp3", "ogg"}

def set_volume(volume_pct, mixer_control):
    """Set system volume using amixer."""
    vol = max(0, min(int(volume_pct * 100), 100))
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

def play_wav_interruptible(file_path, pin_hook, hw_mapping, volume, mixer_control, hook_type, invert_hook):
    """
    Play a WAV file with aplay, checking GPIO during playback.
    Returns True if played to completion, False if interrupted by on-hook.
    """
    if not Path(file_path).exists():
        logger.error(f"Missing audio file: {file_path}")
        return False
    
    logger.info(f"Playing: {Path(file_path).name}")
    set_volume(volume, mixer_control)
    
    proc = subprocess.Popen(
        ["aplay", "-q", "-D", hw_mapping, str(file_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
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

def resolve_arecord_sample_format(config):
    """Return a safe arecord sample format and guard against common misconfiguration."""
    sample_format = str(config.get('format', 'cd')).strip()
    if sample_format.lower() in MISCONFIGURED_ARECORD_FORMATS:
        logger.warning(
            "Unsupported arecord sample format '%s'. Using 'cd' (PCM) instead.",
            sample_format
        )
        return "cd"
    return sample_format

def resolve_output_file_type(config):
    """Normalize and validate output type for guest recordings."""
    output_type = str(config.get('file_type', 'wav')).strip().lower()
    if output_type not in SUPPORTED_OUTPUT_FILE_TYPES:
        logger.warning(
            "Unsupported output file type '%s'. Falling back to 'wav'.",
            output_type
        )
        return "wav"
    return output_type

def transcode_recording_if_needed(capture_file, output_file, output_type):
    """Transcode recorded WAV to the configured format after recording has stopped."""
    if output_type == "wav":
        return

    if not capture_file or not Path(capture_file).exists():
        logger.error("Capture file missing, cannot transcode: %s", capture_file)
        return

    ffmpeg_args = {
        "mp3": ["-codec:a", "libmp3lame", "-q:a", "2"],
        "ogg": ["-codec:a", "libvorbis", "-q:a", "5"],
    }

    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(capture_file),
        *ffmpeg_args[output_type],
        str(output_file),
    ]

    logger.info("Transcoding recording to %s: %s", output_type, Path(output_file).name)
    try:
        subprocess.run(cmd, check=True)
        Path(capture_file).unlink(missing_ok=True)
    except FileNotFoundError:
        logger.error("ffmpeg is not installed; keeping WAV recording: %s", capture_file)
    except subprocess.CalledProcessError as e:
        logger.error("Failed to transcode recording to %s: %s", output_type, e)

def start_recording(config):
    """Start arecord process for guest recording."""
    timestamp = datetime.now().isoformat().replace(':','-')
    recordings_path = Path(config['recordings_path'])
    recordings_path.mkdir(parents=True, exist_ok=True)

    output_type = resolve_output_file_type(config)
    capture_file = recordings_path / f"{timestamp}.wav"
    output_file = recordings_path / f"{timestamp}.{output_type}"
    arecord_sample_format = resolve_arecord_sample_format(config)

    if output_type == "wav":
        logger.info(f"Recording to: {output_file.name}")
    else:
        logger.info(
            "Recording to temporary WAV (%s), then converting to: %s",
            capture_file.name,
            output_file.name
        )
    
    proc = subprocess.Popen([
        "arecord", "-q",
        "-f", arecord_sample_format,
        "-t", "wav",
        "-D", config['alsa_hw_mapping'],
        "-r", str(config['sample_rate']),
        "-c", str(config['channels']),
        str(capture_file)
    ])
    return proc, capture_file, output_file, output_type

def start_recording_greeting(config):
    """Start arecord process for recording greeting message."""
    greeting_path = Path(config['greeting'])
    greeting_path.parent.mkdir(exist_ok=True)
    arecord_sample_format = resolve_arecord_sample_format(config)
    
    logger.info(f"Recording greeting to: {greeting_path.name}")
    
    proc = subprocess.Popen([
        "arecord", "-q",
        "-f", arecord_sample_format,
        "-t", "wav",
        "-D", config['alsa_hw_mapping'],
        "-r", str(config['sample_rate']),
        "-c", str(config['channels']),
        str(greeting_path)
    ])
    return proc

def stop_recording(proc, name="recording"):
    """Stop an arecord process if running."""
    if proc and proc.poll() is None:
        logger.info(f"Stopping {name}")
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

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
    global recording_proc, recording_start_ts, recording_capture_file
    global recording_output_file, recording_file_type, record_greeting_proc
    
    # Load configuration
    config_path = Path(__file__).parent / "../config.yaml"
    config = load_config(config_path)
    
    logger.info(f"Loaded configuration from: {config_path}")
    
    # Setup GPIO
    GPIO.setmode(GPIO.BCM)
    
    # Hook GPIO (handset)
    GPIO.setup(config['hook_gpio'], GPIO.IN, pull_up_down=GPIO.PUD_UP)
    
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
                if not play_wav_interruptible(
                    config['greeting'],
                    config['hook_gpio'],
                    config['alsa_hw_mapping'],
                    config['greeting_volume'],
                    config['mixer_control_name'],
                    hook_type,
                    invert_hook
                ):
                    prev_was_on_hook = is_on_hook(config['hook_gpio'], hook_type, invert_hook)
                    continue
                
                # Beep delay
                beep_delay = config.get('beep_start_delay', 0)
                if beep_delay > 0:
                    time.sleep(beep_delay)
                
                # Play beep (interruptible)
                if not play_wav_interruptible(
                    config['beep'],
                    config['hook_gpio'],
                    config['alsa_hw_mapping'],
                    config['beep_volume'],
                    config['mixer_control_name'],
                    hook_type,
                    invert_hook
                ):
                    prev_was_on_hook = is_on_hook(config['hook_gpio'], hook_type, invert_hook)
                    continue
                
                # Start recording if still off-hook
                if not is_on_hook(config['hook_gpio'], hook_type, invert_hook) and recording_proc is None:
                    recording_proc, recording_capture_file, recording_output_file, recording_file_type = start_recording(config)
                    recording_start_ts = time.time()
            
            # ON-HOOK: User replaced handset
            if not prev_was_on_hook and currently_on_hook:
                logger.info("[ON-HOOK] Handset replaced")
                if recording_proc:
                    stop_recording(recording_proc)
                    transcode_recording_if_needed(recording_capture_file, recording_output_file, recording_file_type)
                    recording_proc = None
                    recording_start_ts = None
                    recording_capture_file = None
                    recording_output_file = None
                    recording_file_type = "wav"
            
            # Check max recording duration
            if recording_proc and recording_proc.poll() is None and recording_start_ts:
                elapsed = time.time() - recording_start_ts
                if elapsed >= config['recording_limit']:
                    logger.warning(f"[TIME EXCEEDED] Max recording time {config['recording_limit']}s reached")
                    stop_recording(recording_proc)
                    transcode_recording_if_needed(recording_capture_file, recording_output_file, recording_file_type)
                    recording_proc = None
                    recording_start_ts = None
                    recording_capture_file = None
                    recording_output_file = None
                    recording_file_type = "wav"
                    
                    # Play time exceeded message (interruptible)
                    play_wav_interruptible(
                        config['time_exceeded'],
                        config['hook_gpio'],
                        config['alsa_hw_mapping'],
                        config['time_exceeded_volume'],
                        config['mixer_control_name'],
                        hook_type,
                        invert_hook
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
                    play_wav_interruptible(
                        config['beep'],
                        config['record_greeting_gpio'],  # Use record button as interrupt
                        config['alsa_hw_mapping'],
                        config['beep_volume'],
                        config['mixer_control_name'],
                        config.get('record_greeting_type', 'NC'),
                        False  # No invert for record greeting
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
        transcode_recording_if_needed(recording_capture_file, recording_output_file, recording_file_type)
        stop_recording(record_greeting_proc, "greeting recording")
        GPIO.cleanup()
        logger.info("Cleanup complete. Goodbye!")

if __name__ == "__main__":
    main()
