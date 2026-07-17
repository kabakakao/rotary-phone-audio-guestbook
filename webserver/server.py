import io
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
import zipfile
import shutil
from datetime import datetime
from io import BytesIO
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from ruamel.yaml import YAML

# Set up logging and app configuration
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Get absolute paths for reliable file locations regardless of where the app is started
WEBSERVER_DIR = Path(__file__).parent.absolute()
BASE_DIR = WEBSERVER_DIR.parent
STATIC_DIR = WEBSERVER_DIR / "static"

# Log critical paths for debugging
logger.info(f"Current working directory: {os.getcwd()}")
logger.info(f"Webserver directory: {WEBSERVER_DIR}")
logger.info(f"Base directory: {BASE_DIR}")
logger.info(f"Static directory: {STATIC_DIR}")

# Create Flask app with absolute path to static folder
app = Flask(__name__,
           static_url_path="/static",
           static_folder=str(STATIC_DIR))
app.secret_key = "supersecretkey"  # Needed for flashing messages

# Define other important paths. The AGB_CONFIG_PATH / AGB_UPLOAD_FOLDER env
# overrides let an off-device harness (test/test_server.py) point the app at an
# isolated config and uploads dir. The device never sets these, so its behavior
# is unchanged.
config_path = Path(os.environ.get("AGB_CONFIG_PATH", str(BASE_DIR / "config.yaml")))
upload_folder = Path(os.environ.get("AGB_UPLOAD_FOLDER", str(BASE_DIR / "uploads")))
upload_folder.mkdir(parents=True, exist_ok=True)

logger.info(f"Config path: {config_path}")
logger.info(f"Upload folder: {upload_folder}")

# Initialize ruamel.yaml
yaml = YAML()

# Attempt to grab recording path from the configuration file
try:
    with config_path.open("r") as f:
        config = yaml.load(f)
        logger.info(f"Config loaded successfully from {config_path}")
except FileNotFoundError as e:
    logger.error(f"Configuration file not found: {e}")
    sys.exit(1)
except Exception as e:
    logger.error(f"Error loading configuration: {e}")
    sys.exit(1)

# Ensure recordings_path is an absolute path
recordings_path_str = config.get("recordings_path", "recordings")
recordings_path = Path(recordings_path_str)
if not recordings_path.is_absolute():
    recordings_path = BASE_DIR / recordings_path_str
    logger.info(f"Converted relative recordings path to absolute: {recordings_path}")

# Verify recordings directory exists and is accessible
if not recordings_path.exists():
    logger.warning(f"Recordings directory does not exist: {recordings_path}")
    try:
        recordings_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created recordings directory: {recordings_path}")
    except Exception as e:
        logger.error(f"Failed to create recordings directory: {e}")
        sys.exit(1)
elif not recordings_path.is_dir():
    logger.error(f"Recordings path exists but is not a directory: {recordings_path}")
    sys.exit(1)
else:
    logger.info(f"Recordings directory verified: {recordings_path}")

# File types that arecord/aplay can handle natively.
NATIVE_FILE_TYPES = {"wav", "raw", "au", "voc"}
# File types that require ffmpeg for encoding/decoding.
FFMPEG_FILE_TYPES = {"mp3", "ogg"}

# Library-mode virtual hook runtime state (webserver process only).
library_recording_proc = None
library_recording_file = None
library_recording_started_at = None
# "idle" → "playing" (greeting/beep) → "recording" → back to "idle"
library_phase = "idle"
_library_abort_event: threading.Event | None = None
_library_hook_thread: threading.Thread | None = None
_library_play_proc: subprocess.Popen | None = None  # current playback process (for abort)

# Greeting recording overlay runtime state (webserver process only).
greeting_record_proc = None
greeting_record_temp_file = None
greeting_record_started_at = None

def normalize_path(path):
    """Normalize and convert paths to Unix format."""
    return str(path.as_posix())


@app.context_processor
def inject_title():
    """Inject the UI title into all templates."""
    current_config = load_config()
    ui_config = current_config.get('ui') or {}
    return {'title': ui_config.get('title', 'Audio Guestbook')}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/<filename>", methods=["GET"])
def download_file(filename):
    """Download a file dynamically from the recordings folder."""
    return send_from_directory(recordings_path, filename, as_attachment=True)


@app.route("/delete/<filename>", methods=["POST"])
def delete_file(filename):
    """Delete a specific recording."""
    file_path = recordings_path / filename
    try:
        file_path.unlink()
        return jsonify({"success": True, "message": f"{filename} has been deleted."})
    except Exception as e:
        return jsonify(
            {"success": False, "message": f"Error deleting file: {str(e)}"}
        ), 500


@app.route("/api/recordings")
def get_recordings():
    """API route to get a list of all recordings."""
    try:
        # List directory contents if it exists
        if recordings_path.exists() and recordings_path.is_dir():
            all_items = list(sorted(recordings_path.iterdir(), key=lambda f:f.stat().st_mtime, reverse=True))
            logger.info(f"Directory contains {len(all_items)} items")

            # List all items with their types
            for item in all_items:
                logger.info(f"  - {item.name} ({'file' if item.is_file() else 'dir'})")

            files = [f.name for f in all_items if f.is_file()]
            logger.info(f"Found {len(files)} files: {files}")
            return jsonify(files)
        else:
            logger.error(f"Recordings path is not a valid directory: {recordings_path}")
            return jsonify({"error": "Recordings directory not found"}), 404

    except Exception as e:
        logger.error(f"Error accessing recordings directory: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/config", methods=["GET", "POST"])
def edit_config():
    """Handle GET and POST requests to edit the configuration."""
    if request.method == "POST":
        logger.info("Form data received:")
        for key, value in request.form.items():
            logger.info(f"  {key}: {value}")
        try:
            # Handle file uploads
            for field in ["greeting", "beep", "time_exceeded"]:
                if f"{field}_file" in request.files:
                    file = request.files[f"{field}_file"]
                    if file.filename:
                        file_path = upload_folder / file.filename
                        file.save(file_path)
                        # Store path relative to BASE_DIR for portability
                        config[field] = normalize_path(file_path.relative_to(BASE_DIR))

            update_config(request.form)

            with config_path.open("w") as f:
                yaml.dump(config, f)

            # Restart the audioGuestBook service to apply changes
            try:
                subprocess.run(["sudo", "systemctl", "restart", "audioGuestBook.service"], check=True)
                logger.info("Successfully restarted audioGuestBook service")
                flash("Configuration updated and service restarted successfully!", "success")
            except subprocess.CalledProcessError as e:
                logger.error(f"Failed to restart audioGuestBook service: {e}")
                flash("Configuration updated but failed to restart service. Please restart manually.", "warning")

            return redirect(url_for("edit_config"))
        except Exception as e:
            logger.error(f"Error updating configuration: {e}")
            flash(f"Error updating configuration: {str(e)}", "error")
            # Continue with current configuration but show error

    current_config = load_config()
    current_ui_config = current_config.get('ui') or {}

    return render_template("config.html", config=current_config, ui_config=current_ui_config)


@app.route("/recordings/<filename>")
def serve_recording(filename):
    """Serve a specific recording with proper streaming and range support."""
    file_path = recordings_path / filename

    # Verify file exists
    if not file_path.exists():
        logger.error(f"Recording file not found: {file_path}")
        return jsonify({"error": "File not found"}), 404

    # Get file size for range requests
    file_size = file_path.stat().st_size

    # Pick mimetype based on file extension so the browser's audio player
    # decodes wav/mp3/ogg files correctly.
    mimetype = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
    }.get(file_path.suffix.lower(), "audio/wav")

    # Parse Range header
    range_header = request.headers.get('Range', None)

    if range_header:
        # Parse the range header
        byte1, byte2 = 0, None
        match = re.search(r'(\d+)-(\d*)', range_header)
        groups = match.groups()

        if groups[0]:
            byte1 = int(groups[0])
        if groups[1]:
            byte2 = int(groups[1])

        if byte2 is None:
            byte2 = file_size - 1

        length = byte2 - byte1 + 1

        # Create the response with the proper headers for range request
        resp = Response(
            generate_file_chunks(str(file_path), byte1, byte2),
            status=206,
            mimetype=mimetype,
            direct_passthrough=True
        )

        resp.headers.add('Content-Range', f'bytes {byte1}-{byte2}/{file_size}')
        resp.headers.add('Accept-Ranges', 'bytes')
        resp.headers.add('Content-Length', str(length))
        return resp

    # If no range header, serve the whole file
    resp = Response(
        generate_file_chunks(str(file_path), 0, file_size - 1),
        mimetype=mimetype
    )
    resp.headers.add('Accept-Ranges', 'bytes')
    resp.headers.add('Content-Length', str(file_size))
    return resp


def generate_file_chunks(file_path, byte1=0, byte2=None):
    """Generator to stream file in chunks with range support."""
    with open(file_path, 'rb') as f:
        f.seek(byte1)
        while True:
            buffer_size = 8192
            if byte2:
                buffer_size = min(buffer_size, byte2 - f.tell() + 1)
                if buffer_size <= 0:
                    break
            chunk = f.read(buffer_size)
            if not chunk:
                break
            yield chunk


@app.route("/download-all")
def download_all():
    """Download all recordings as a zip file."""
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, "w") as zf:
        audio_files = [f for f in recordings_path.iterdir() if f.is_file() and f.suffix.lower() in (".wav", ".mp3", ".ogg")]

        # Log the files being added to the zip
        logger.info(f"Adding {len(audio_files)} files to zip")

        for file_path in audio_files:
            # Use absolute path for reading
            abs_path = str(file_path.absolute())
            logger.info(f"Adding file: {abs_path}")

            # Verify file exists and is readable
            if os.path.exists(abs_path) and os.access(abs_path, os.R_OK):
                # Add to zip with just the filename as the internal path
                zf.write(abs_path, arcname=file_path.name)
            else:
                logger.error(f"Cannot access file: {abs_path}")

    memory_file.seek(0)

    logger.info(f"Zip file size: {memory_file.getbuffer().nbytes} bytes")

    return send_file(
        memory_file,
        mimetype="application/zip",
        as_attachment=True,
        download_name="recordings.zip",
    )


@app.route("/download-selected", methods=["POST"])
def download_selected():
    """Download selected recordings as a zip file."""
    selected_files = request.form.getlist("files[]")
    logger.info(f"Selected files for download: {selected_files}")

    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, "w") as zf:
        for filename in selected_files:
            file_path = recordings_path / filename
            if file_path.exists() and os.access(str(file_path), os.R_OK):
                logger.info(f"Adding to zip: {file_path}")
                zf.write(str(file_path), filename)
            else:
                logger.error(f"Cannot access file: {file_path}")

    memory_file.seek(0)
    logger.info(f"Zip file size: {memory_file.getbuffer().nbytes} bytes")

    return send_file(
        memory_file,
        mimetype="application/zip",
        download_name="selected_recordings.zip",
        as_attachment=True,
    )


@app.route("/rename/<old_filename>", methods=["POST"])
def rename_recording(old_filename):
    """Rename a recording."""
    new_filename = request.json["newFilename"]
    old_path = recordings_path / old_filename
    new_path = recordings_path / new_filename

    if old_path.exists():
        os.rename(str(old_path), str(new_path))
        return jsonify(success=True)
    else:
        return jsonify(success=False), 404


@app.route("/reboot", methods=["POST"])
def reboot():
    """Reboot the system."""
    try:
        os.system("sudo reboot now")
        return jsonify({"success": True, "message": "System is rebooting..."})
    except Exception as e:
        logger.error(f"Failed to reboot: {e}")
        return jsonify(
            {"success": False, "message": "Failed to reboot the system!"}
        ), 500


@app.route("/shutdown", methods=["POST"])
def shutdown():
    """Shut down the system."""
    try:
        os.system("sudo shutdown now")
        return jsonify({"success": True, "message": "System is shutting down..."})
    except Exception as e:
        logger.error(f"Failed to shut down: {e}")
        return jsonify(
            {"success": False, "message": "Failed to shut down the system!"}
        ), 500


def load_config():
    # Load the current configuration
    try:
        with config_path.open("r") as f:
            return yaml.load(f)
    except FileNotFoundError as e:
        logger.error(f"Configuration file not found: {e}")
        return {}

def update_config(form_data):
    """Update the YAML configuration with form data."""
    for key, value in form_data.items():
        # Skip CSRF token if it exists
        if key == 'csrf_token':
            continue

        # Handle nested config fields with underscore notation (ui_title -> ui.title)
        if key.startswith('ui_'):
            nested_key = key[3:]
            if 'ui' not in config:
                config['ui'] = {}
            config['ui'][nested_key] = value
            logger.info(f"Updated 'ui.{nested_key}' to: {value}")
            continue

        # Check if key exists in config
        if key not in config and key != 'invert_hook':
            logger.warning(f"Form field '{key}' not found in config, skipping")
            continue

        # Log the conversion attempt
        logger.info(f"Updating '{key}': {config.get(key, 'Not set')} (type: {type(config.get(key, '')).__name__}) → '{value}'")

        try:
            # Convert value based on the type in config or for new boolean fields
            if key == 'invert_hook' or isinstance(config.get(key), bool):
                # Convert string to boolean
                new_value = (value.lower() == "true")
                logger.info(f"Converting to boolean: {value} → {new_value}")
                config[key] = new_value
            elif isinstance(config.get(key), int):
                config[key] = int(value)
            elif isinstance(config.get(key), float):
                config[key] = float(value)
            else:
                config[key] = value

            # Verify the conversion worked
            logger.info(f"Updated '{key}' to: {config[key]} (type: {type(config[key]).__name__})")

        except (ValueError, TypeError) as e:
            logger.error(f"Failed to update '{key}': {e}")

@app.route("/api/system-status")
def system_status():
    """Return basic system information for the dashboard."""
    try:
        import psutil

        cpu_usage = psutil.cpu_percent()
        memory_usage = psutil.virtual_memory().percent
        disk_usage = psutil.disk_usage("/").percent
        recording_count = len([f for f in recordings_path.iterdir() if f.is_file()])

        return jsonify(
            {
                "success": True,
                "cpu": cpu_usage,
                "memory": memory_usage,
                "disk": disk_usage,
                "recordings": recording_count,
            }
        )
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        return jsonify({"success": False, "message": str(e)}), 500


def _c_locale_env():
    """Env with LC_ALL/LANG forced to C so ALSA tool output stays in English and parseable,
    regardless of the system's configured locale (e.g. a German Raspberry Pi OS install)."""
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _list_capture_devices():
    """Return a list of 'plughw:CARD=<id>,DEV=<n>' strings for cards that support capture."""
    try:
        result = subprocess.run(["arecord", "-l"], capture_output=True, timeout=5, env=_c_locale_env())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    devices = []
    # Typical line: "card 1: Device [USB Audio Device], device 0: USB Audio [USB Audio]"
    for match in re.finditer(
        r"card\s+\d+:\s+(?P<card>\S+)\s+\[.*?\],\s*device\s+(?P<dev>\d+):", result.stdout.decode(errors="ignore")
    ):
        devices.append(f"plughw:CARD={match.group('card')},DEV={match.group('dev')}")
    return devices


def _friendly_alsa_error(stderr, hw_mapping, is_playback=False):
    """Translate common ALSA error output into an actionable message for the user."""
    action = "Playback" if is_playback else "Recording"
    if "capture slave is not defined" in stderr or ("asym" in stderr and "capture" in stderr):
        suggestions = _list_capture_devices()
        hint = (
            f" Your system does have a capture-capable device though: try setting 'ALSA Hardware Mapping' to "
            f"'{suggestions[0]}' in the Audio settings above."
            if suggestions
            else " No capture-capable sound card was detected at all (run 'arecord -l' on the Pi to check)."
        )
        return (
            f"The ALSA device '{hw_mapping}' is set up for playback only and has no microphone input configured "
            f"(this is common with the Raspberry Pi's built-in headphone jack, or a stale /etc/asound.conf). "
            f"Plug in a USB sound card/microphone and reboot, or set 'alsa_hw_mapping' to point directly at your "
            f"capture device (see 'arecord -l' on the Pi)." + hint
        )
    if "playback slave is not defined" in stderr:
        return (
            f"The ALSA device '{hw_mapping}' is set up for capture only and has no speaker output configured. "
            f"Set 'alsa_hw_mapping' to point directly at your playback device (see 'aplay -l' on the Pi)."
        )
    return f"{action} failed: {stderr or 'unknown error'}"


def _clamp_float(value, default, minimum, maximum):
    """Parse numeric input and clamp it to a safe range."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _set_amixer_percent(mixer_control, percent, capture=False):
    """Best-effort mixer level update for playback or capture controls."""
    level = max(0, min(int(percent), 100))
    cmd = ["amixer", "set", mixer_control, f"{level}%"]
    if capture:
        subprocess.run(cmd + ["cap"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _resolve_config_audio_path(path_value):
    """Resolve configured audio paths against BASE_DIR when relative."""
    path = Path(str(path_value))
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _play_audio_once(file_path, hw_mapping, mixer_control, level, gain):
    """Play one file once via ALSA with optional software gain."""
    if not file_path.exists():
        return False, f"Audio file not found: {file_path}"

    output_level = _clamp_float(level, 1.0, 0.0, 1.0)
    gain = _clamp_float(gain, 1.0, 0.1, 4.0)
    _set_amixer_percent(mixer_control, int(output_level * 100), capture=False)

    ext = file_path.suffix.lower()
    use_ffmpeg = ext in (".mp3", ".ogg") or abs(gain - 1.0) > 1e-6

    if use_ffmpeg:
        cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(file_path)]
        if abs(gain - 1.0) > 1e-6:
            cmd.extend(["-filter:a", f"volume={gain}"])
        cmd.extend(["-f", "alsa", hw_mapping])
    else:
        cmd = ["aplay", "-q", "-D", hw_mapping, str(file_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=20, env=_c_locale_env())
    except FileNotFoundError:
        tool = cmd[0]
        return False, f"'{tool}' not found. Is ALSA/ffmpeg installed?"
    except subprocess.TimeoutExpired:
        return False, "Playback timed out."

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="ignore").strip()
        logger.error(f"audio playback failed: {stderr}")
        return False, _friendly_alsa_error(stderr, hw_mapping, is_playback=True)

    return True, "ok"


def _build_record_command(out_file, current_config):
    """Build recording command (arecord or ffmpeg) based on extension and gain."""
    ext = Path(out_file).suffix.lower().lstrip(".")
    recording_gain = _clamp_float(current_config.get("recording_gain", 1.0), 1.0, 0.1, 4.0)
    requires_ffmpeg = ext in FFMPEG_FILE_TYPES or abs(recording_gain - 1.0) > 1e-6

    if requires_ffmpeg:
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "alsa",
            "-ar", str(int(current_config.get("sample_rate") or 44100)),
            "-ac", str(int(current_config.get("channels") or 1)),
            "-i", str(current_config.get("alsa_hw_mapping", "default")),
        ]
        if abs(recording_gain - 1.0) > 1e-6:
            cmd.extend(["-filter:a", f"volume={recording_gain}"])
        cmd.append(str(out_file))
        return cmd, requires_ffmpeg, ext

    arecord_type = ext if ext in NATIVE_FILE_TYPES else "wav"
    cmd = [
        "arecord", "-q",
        "-f", str(current_config.get("format", "cd")),
        "-t", arecord_type,
        "-D", str(current_config.get("alsa_hw_mapping", "default")),
        "-r", str(int(current_config.get("sample_rate") or 44100)),
        "-c", str(int(current_config.get("channels") or 1)),
        str(out_file),
    ]
    return cmd, requires_ffmpeg, ext


def _library_hook_state_payload():
    """Return current library-mode virtual hook state."""
    global library_recording_proc, library_recording_file, library_recording_started_at, library_phase

    if library_recording_proc and library_recording_proc.poll() is not None:
        # Process ended unexpectedly/outside hook-down.
        library_recording_proc = None
        library_recording_file = None
        library_recording_started_at = None
        if library_phase == "recording":
            library_phase = "idle"

    if library_phase == "playing":
        return {
            "off_hook": True,
            "recording": False,
            "phase": "playing",
            "filename": None,
            "elapsed_seconds": 0,
        }

    if library_recording_proc:
        elapsed = 0
        if library_recording_started_at:
            elapsed = max(0, int(time.time() - library_recording_started_at))
        return {
            "off_hook": True,
            "recording": True,
            "phase": "recording",
            "filename": Path(library_recording_file).name if library_recording_file else None,
            "elapsed_seconds": elapsed,
        }

    return {
        "off_hook": False,
        "recording": False,
        "phase": "idle",
        "filename": None,
        "elapsed_seconds": 0,
    }


def _greeting_record_state_payload():
    """Return current greeting-record overlay state."""
    global greeting_record_proc, greeting_record_temp_file, greeting_record_started_at

    if greeting_record_proc and greeting_record_proc.poll() is not None:
        greeting_record_proc = None
        greeting_record_started_at = None

    temp_exists = bool(greeting_record_temp_file and Path(greeting_record_temp_file).exists())
    elapsed = 0
    if greeting_record_proc and greeting_record_started_at:
        elapsed = max(0, int(time.time() - greeting_record_started_at))

    return {
        "recording": bool(greeting_record_proc and greeting_record_proc.poll() is None),
        "has_recording": temp_exists,
        "temp_filename": Path(greeting_record_temp_file).name if temp_exists else None,
        "elapsed_seconds": elapsed,
    }


@app.route("/api/greeting-record/status", methods=["GET"])
def greeting_record_status():
    """Return current greeting-record session state."""
    return jsonify({"success": True, **_greeting_record_state_payload()})


@app.route("/api/greeting-record/preview", methods=["GET"])
def greeting_record_preview():
    """Serve the temporary recorded greeting for in-overlay playback preview."""
    state = _greeting_record_state_payload()
    if not state["has_recording"] or not greeting_record_temp_file:
        return jsonify({"success": False, "message": "No recorded greeting available for preview."}), 404

    temp_path = Path(greeting_record_temp_file)
    mimetype = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
    }.get(temp_path.suffix.lower(), "audio/wav")

    return send_file(str(temp_path), mimetype=mimetype)


@app.route("/api/greeting-record/start", methods=["POST"])
def greeting_record_start():
    """Start recording a temporary greeting file for later apply/discard."""
    global greeting_record_proc, greeting_record_temp_file, greeting_record_started_at

    if greeting_record_proc and greeting_record_proc.poll() is None:
        return jsonify({
            "success": False,
            "message": "Greeting recording already in progress.",
            **_greeting_record_state_payload(),
        }), 409

    current_config = load_config()
    target_greeting = _resolve_config_audio_path(current_config.get("greeting", "sounds/greeting.wav"))
    ext = target_greeting.suffix.lower().lstrip(".") or "wav"

    tmp_dir = upload_folder / ".greeting-temp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = tmp_dir / f"greeting_temp.{ext}"

    if temp_path.exists():
        temp_path.unlink()

    capture_control = str(current_config.get("capture_mixer_control_name", "Capture"))
    capture_volume = _clamp_float(current_config.get("capture_volume", 1.0), 1.0, 0.0, 1.0)
    _set_amixer_percent(capture_control, int(capture_volume * 100), capture=True)

    cmd, requires_ffmpeg, cmd_ext = _build_record_command(temp_path, current_config)
    try:
        greeting_record_proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        if requires_ffmpeg and cmd_ext not in FFMPEG_FILE_TYPES:
            fallback_cmd = [
                "arecord", "-q",
                "-f", str(current_config.get("format", "cd")),
                "-t", cmd_ext if cmd_ext in NATIVE_FILE_TYPES else "wav",
                "-D", str(current_config.get("alsa_hw_mapping", "default")),
                "-r", str(int(current_config.get("sample_rate") or 44100)),
                "-c", str(int(current_config.get("channels") or 1)),
                str(temp_path),
            ]
            try:
                greeting_record_proc = subprocess.Popen(
                    fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except FileNotFoundError:
                return jsonify({"success": False, "message": "Recording tools not found (arecord/ffmpeg)."}), 500
        else:
            return jsonify({"success": False, "message": "Recording tool not found. Install ffmpeg for this setup."}), 500

    greeting_record_temp_file = str(temp_path)
    greeting_record_started_at = time.time()

    return jsonify({
        "success": True,
        "message": "Greeting recording started.",
        **_greeting_record_state_payload(),
    })


@app.route("/api/greeting-record/stop", methods=["POST"])
def greeting_record_stop():
    """Stop active greeting recording and keep temp file for apply/discard."""
    global greeting_record_proc, greeting_record_started_at

    if not greeting_record_proc or greeting_record_proc.poll() is not None:
        greeting_record_proc = None
        greeting_record_started_at = None
        state = _greeting_record_state_payload()
        if state["has_recording"]:
            return jsonify({"success": True, "message": "Recording already stopped.", **state})
        return jsonify({"success": False, "message": "No active greeting recording.", **state}), 400

    greeting_record_proc.terminate()
    try:
        greeting_record_proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        greeting_record_proc.kill()

    greeting_record_proc = None
    greeting_record_started_at = None

    state = _greeting_record_state_payload()
    if not state["has_recording"]:
        return jsonify({"success": False, "message": "Recording stopped but no temp file was created.", **state}), 500

    return jsonify({"success": True, "message": "Greeting recording stopped.", **state})


@app.route("/api/greeting-record/use", methods=["POST"])
def greeting_record_use():
    """Apply the recorded temp greeting by replacing configured greeting file."""
    global greeting_record_temp_file

    if greeting_record_proc and greeting_record_proc.poll() is None:
        return jsonify({"success": False, "message": "Stop recording before using it.", **_greeting_record_state_payload()}), 409

    if not greeting_record_temp_file or not Path(greeting_record_temp_file).exists():
        return jsonify({"success": False, "message": "No recorded greeting available to use.", **_greeting_record_state_payload()}), 400

    current_config = load_config()
    target_greeting = _resolve_config_audio_path(current_config.get("greeting", "sounds/greeting.wav"))
    target_greeting.parent.mkdir(parents=True, exist_ok=True)

    temp_path = Path(greeting_record_temp_file)
    try:
        if target_greeting.exists():
            target_greeting.unlink()
        shutil.move(str(temp_path), str(target_greeting))
    except Exception as e:
        logger.error(f"Failed applying recorded greeting: {e}")
        return jsonify({"success": False, "message": f"Could not apply greeting: {e}"}), 500

    greeting_record_temp_file = None
    return jsonify({
        "success": True,
        "message": f"New greeting applied: {target_greeting.name}",
        **_greeting_record_state_payload(),
    })


@app.route("/api/greeting-record/discard", methods=["POST"])
def greeting_record_discard():
    """Discard active/temporary greeting recording from overlay workflow."""
    global greeting_record_proc, greeting_record_temp_file, greeting_record_started_at

    if greeting_record_proc and greeting_record_proc.poll() is None:
        greeting_record_proc.terminate()
        try:
            greeting_record_proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            greeting_record_proc.kill()

    greeting_record_proc = None
    greeting_record_started_at = None

    if greeting_record_temp_file:
        temp_path = Path(greeting_record_temp_file)
        if temp_path.exists():
            temp_path.unlink()
    greeting_record_temp_file = None

    return jsonify({"success": True, "message": "Temporary greeting recording discarded.", **_greeting_record_state_payload()})


@app.route("/api/audio-test/mic-level", methods=["POST"])
def test_mic_level():
    """Record a short mic sample using the current input settings and return its signal level."""
    current_config = load_config()
    hw_mapping = str(current_config.get("alsa_hw_mapping", "default"))
    capture_mixer_control = str(current_config.get("capture_mixer_control_name", "Capture"))
    capture_volume = _clamp_float(current_config.get("capture_volume", 1.0), 1.0, 0.0, 1.0)
    channels = int(current_config.get("channels") or 1)
    sample_rate = int(current_config.get("sample_rate") or 44100)
    mic_test_gain = _clamp_float(current_config.get("mic_test_gain", 1.0), 1.0, 0.1, 10.0)
    duration = 2

    _set_amixer_percent(capture_mixer_control, int(capture_volume * 100), capture=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "mic_test.wav"
        cmd = [
            "arecord", "-q",
            "-D", hw_mapping,
            "-f", "S16_LE",
            "-c", str(channels),
            "-r", str(sample_rate),
            "-d", str(duration),
            str(tmp_path),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=duration + 5, env=_c_locale_env())
        except FileNotFoundError:
            return jsonify({"success": False, "message": "'arecord' not found. Is ALSA installed?"}), 500
        except subprocess.TimeoutExpired:
            return jsonify({"success": False, "message": "Recording timed out."}), 500

        if result.returncode != 0:
            stderr = result.stderr.decode(errors="ignore").strip()
            logger.error(f"arecord failed: {stderr}")
            return jsonify({"success": False, "message": _friendly_alsa_error(stderr, hw_mapping)}), 500

        try:
            with wave.open(str(tmp_path), "rb") as wf:
                frames = wf.readframes(wf.getnframes())
                sampwidth = wf.getsampwidth()
        except Exception as e:
            logger.error(f"Could not read mic test recording: {e}")
            return jsonify({"success": False, "message": f"Could not read recorded audio: {e}"}), 500

    if not frames or sampwidth != 2:
        return jsonify({"success": False, "message": "No audio captured. Check your microphone wiring."}), 500

    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    peak = max(abs(s) for s in samples)
    rms = (sum(s * s for s in samples) / len(samples)) ** 0.5
    max_possible = 32767.0
    boosted_peak = min(peak * mic_test_gain, max_possible)
    boosted_rms = min(rms * mic_test_gain, max_possible)

    return jsonify({
        "success": True,
        "peak_percent": round((boosted_peak / max_possible) * 100, 1),
        "rms_percent": round((boosted_rms / max_possible) * 100, 1),
        "raw_peak_percent": round(min(peak / max_possible, 1.0) * 100, 1),
        "raw_rms_percent": round(min(rms / max_possible, 1.0) * 100, 1),
        "mic_test_gain": mic_test_gain,
    })


@app.route("/api/audio-test/play-sample", methods=["POST"])
def test_play_sample():
    """Play a bundled sample sound through the configured output device to test speaker wiring."""
    current_config = load_config()
    hw_mapping = str(current_config.get("alsa_hw_mapping", "default"))
    mixer_control = str(current_config.get("mixer_control_name", "Speaker"))
    speaker_test_volume = _clamp_float(current_config.get("speaker_test_volume", 1.0), 1.0, 0.0, 1.0)
    speaker_test_gain = _clamp_float(current_config.get("speaker_test_gain", 1.0), 1.0, 0.1, 4.0)
    speaker_test_percent = int(speaker_test_volume * 100)
    greeting_file = _resolve_config_audio_path(current_config.get("greeting", "sounds/greeting.wav"))

    success, message = _play_audio_once(
        greeting_file,
        hw_mapping,
        mixer_control,
        speaker_test_volume,
        speaker_test_gain,
    )
    if not success:
        status = 404 if "not found" in message.lower() else 500
        return jsonify({"success": False, "message": message}), status

    return jsonify({
        "success": True,
        "message": f"Greeting playback finished ({speaker_test_percent}% on '{mixer_control}', gain x{speaker_test_gain}).",
    })


def _sleep_abortable(seconds: float, abort_event: threading.Event, step: float = 0.05) -> bool:
    """Sleep for *seconds* in small steps; return False immediately if abort_event is set."""
    end = time.time() + seconds
    while time.time() < end:
        if abort_event.is_set():
            return False
        time.sleep(min(step, end - time.time()))
    return True


def _library_simulate_sequence(config: dict, abort_event: threading.Event) -> None:
    """Background thread: play greeting + beep, then start the recording.

    Modifies the library_* globals once the recording is running.
    Checks abort_event frequently so simulate_hook_down can interrupt playback.
    """
    global library_recording_proc, library_recording_file, library_recording_started_at
    global library_phase, _library_play_proc

    hw_mapping    = str(config.get("alsa_hw_mapping", "default"))
    mixer_control = str(config.get("mixer_control_name", "Speaker"))
    playback_gain = _clamp_float(config.get("playback_gain", 1.0), 1.0, 0.1, 4.0)

    greeting_file   = _resolve_config_audio_path(config.get("greeting", "sounds/greeting.wav"))
    beep_file       = _resolve_config_audio_path(config.get("beep",     "sounds/beep.wav"))
    greeting_volume = _clamp_float(config.get("greeting_volume", 1.0), 1.0, 0.0, 1.0)
    beep_volume     = _clamp_float(config.get("beep_volume",     1.0), 1.0, 0.0, 1.0)
    greeting_delay  = _clamp_float(config.get("greeting_start_delay", 0.0), 0.0, 0.0, 30.0)
    beep_delay      = _clamp_float(config.get("beep_start_delay",     0.0), 0.0, 0.0, 30.0)

    def play_abortable(file_path: Path, volume: float) -> bool:
        """Start playback via Popen; poll until done or aborted. Returns True on completion."""
        global _library_play_proc
        if abort_event.is_set() or not file_path.exists():
            return not abort_event.is_set()

        _set_amixer_percent(mixer_control, int(volume * 100), capture=False)
        ext        = file_path.suffix.lower()
        use_ffmpeg = ext in (".mp3", ".ogg") or abs(playback_gain - 1.0) > 1e-6

        if use_ffmpeg:
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(file_path)]
            if abs(playback_gain - 1.0) > 1e-6:
                cmd.extend(["-filter:a", f"volume={playback_gain}"])
            cmd.extend(["-f", "alsa", hw_mapping])
        else:
            cmd = ["aplay", "-q", "-D", hw_mapping, str(file_path)]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            logger.warning(f"Playback tool not found: {cmd[0]}")
            return True  # skip silently

        _library_play_proc = proc
        try:
            while proc.poll() is None:
                if abort_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return False
                time.sleep(0.05)
        finally:
            _library_play_proc = None

        return not abort_event.is_set()

    try:
        # --- greeting start delay ---
        if greeting_delay > 0 and not _sleep_abortable(greeting_delay, abort_event):
            return

        # --- greeting ---
        if not play_abortable(greeting_file, greeting_volume):
            return

        # --- beep start delay ---
        if beep_delay > 0 and not _sleep_abortable(beep_delay, abort_event):
            return

        # --- beep ---
        if not play_abortable(beep_file, beep_volume):
            return

        if abort_event.is_set():
            return

        # --- start recording ---
        file_type = str(config.get("file_type", "wav"))
        timestamp = datetime.now().isoformat().replace(":", "-")
        out_file  = recordings_path / f"{timestamp}.{file_type}"

        capture_control = str(config.get("capture_mixer_control_name", "Capture"))
        capture_volume  = _clamp_float(config.get("capture_volume", 1.0), 1.0, 0.0, 1.0)
        _set_amixer_percent(capture_control, int(capture_volume * 100), capture=True)

        cmd, requires_ffmpeg, ext = _build_record_command(out_file, config)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            if requires_ffmpeg and ext not in FFMPEG_FILE_TYPES:
                fallback_cmd = [
                    "arecord", "-q",
                    "-f", str(config.get("format", "cd")),
                    "-t", ext if ext in NATIVE_FILE_TYPES else "wav",
                    "-D", str(config.get("alsa_hw_mapping", "default")),
                    "-r", str(int(config.get("sample_rate") or 44100)),
                    "-c", str(int(config.get("channels") or 1)),
                    str(out_file),
                ]
                try:
                    proc = subprocess.Popen(
                        fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                except FileNotFoundError:
                    logger.error("Recording tools not found (arecord/ffmpeg) in simulation.")
                    return
            else:
                logger.error("Recording tool not found in simulation.")
                return

        library_recording_proc    = proc
        library_recording_file    = str(out_file)
        library_recording_started_at = time.time()
        library_phase = "recording"
        logger.info(f"Simulation recording started: {out_file.name}")

    except Exception:
        logger.exception("Unexpected error in simulation sequence")
    finally:
        # If we exit before reaching "recording", mark as idle
        if library_phase == "playing":
            library_phase = "idle"


@app.route("/api/library-mode/simulate-hook-up", methods=["POST"])
def simulate_hook_up():
    """Simulate handset lift: start background playback of greeting+beep, then record.

    Returns immediately so the browser is not blocked during the greeting.
    Use /api/library-mode/hook-status to poll for phase transitions.
    """
    global library_phase, _library_abort_event, _library_hook_thread

    if library_phase != "idle":
        return jsonify({
            "success": False,
            "message": "Already off-hook: recording is already running.",
            **_library_hook_state_payload(),
        }), 409

    current_config = load_config()
    hw_mapping = str(current_config.get("alsa_hw_mapping", "default"))
    mixer_control = str(current_config.get("mixer_control_name", "Speaker"))
    playback_gain = _clamp_float(current_config.get("playback_gain", 1.0), 1.0, 0.1, 4.0)

    abort_event = threading.Event()
    _library_abort_event = abort_event
    _library_hook_thread = threading.Thread(
        target=_library_simulate_sequence,
        args=(current_config, abort_event),
        daemon=True,
        name="library-simulate",
    )
    library_phase = "playing"
    _library_hook_thread.start()

    return jsonify({
        "success": True,
        "message": "Simulated hook-up: greeting/beep playing — recording will start automatically.",
        **_library_hook_state_payload(),
    })


@app.route("/api/library-mode/simulate-hook-down", methods=["POST"])
def simulate_hook_down():
    """Simulate handset down: abort playback (if still playing) or stop an active recording."""
    global library_recording_proc, library_recording_file, library_recording_started_at
    global library_phase, _library_abort_event, _library_hook_thread

    # --- abort ongoing playback (greeting/beep phase) ---
    if library_phase == "playing" and _library_abort_event is not None:
        _library_abort_event.set()
        if _library_hook_thread is not None:
            _library_hook_thread.join(timeout=3)
        library_phase = "idle"
        _library_abort_event = None
        _library_hook_thread = None
        return jsonify({
            "success": True,
            "message": "Simulated hook-down: playback aborted.",
            **_library_hook_state_payload(),
        })

    if not library_recording_proc or library_recording_proc.poll() is not None:
        library_recording_proc = None
        library_recording_file = None
        library_recording_started_at = None
        library_phase = "idle"
        _library_abort_event = None
        _library_hook_thread = None
        return jsonify({
            "success": True,
            "message": "Already on-hook: no active simulation recording.",
            **_library_hook_state_payload(),
        })

    finished_file = Path(library_recording_file).name if library_recording_file else None
    duration = 0
    if library_recording_started_at:
        duration = max(0, int(time.time() - library_recording_started_at))

    library_recording_proc.terminate()
    try:
        library_recording_proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        library_recording_proc.kill()

    library_recording_proc = None
    library_recording_file = None
    library_recording_started_at = None
    library_phase = "idle"
    _library_abort_event = None
    _library_hook_thread = None

    return jsonify({
        "success": True,
        "message": f"Simulated hook-down: recording saved ({finished_file}, {duration}s).",
        **_library_hook_state_payload(),
    })


@app.route("/api/library-mode/hook-status", methods=["GET"])
def library_hook_status():
    """Return current virtual hook/recording state for the library page."""
    return jsonify({"success": True, **_library_hook_state_payload()})


@app.route("/delete-recordings", methods=["POST"])
def delete_recordings():
    """Delete multiple recordings in bulk."""
    try:
        data = request.get_json()
        if not data or 'ids' not in data:
            return jsonify({"success": False, "message": "No recordings specified for deletion"}), 400

        deleted_files = []
        failed_files = []

        for filename in data['ids']:
            file_path = recordings_path / filename
            try:
                if file_path.exists():
                    file_path.unlink()
                    deleted_files.append(filename)
                    logger.info(f"Successfully deleted: {filename}")
                else:
                    failed_files.append(filename)
                    logger.warning(f"File not found: {filename}")
            except Exception as e:
                failed_files.append(filename)
                logger.error(f"Error deleting {filename}: {str(e)}")

        if failed_files:
            message = f"Deleted {len(deleted_files)} files, failed to delete {len(failed_files)}"
            return jsonify({
                "success": False,
                "message": message,
                "deleted": deleted_files,
                "failed": failed_files
            }), 207  # Multi-status response

        return jsonify({
            "success": True,
            "message": f"Successfully deleted {len(deleted_files)} recordings",
            "deleted": deleted_files
        })

    except Exception as e:
        logger.error(f"Error in bulk deletion: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"Server error during bulk deletion: {str(e)}"
        }), 500

if __name__ == "__main__":
    # Print summary of configuration for debugging
    logger.info("=== Starting Audio Guestbook Server ===")
    logger.info(f"Static files location: {STATIC_DIR}")
    logger.info(f"Recordings location: {recordings_path}")
    logger.info("=====================================")
