import io
import logging
import os
import re
import struct
import subprocess
import sys
import tempfile
import wave
import zipfile
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


@app.route("/api/audio-test/mic-level", methods=["POST"])
def test_mic_level():
    """Record a short mic sample using the current input settings and return its signal level."""
    current_config = load_config()
    hw_mapping = str(current_config.get("alsa_hw_mapping", "default"))
    channels = int(current_config.get("channels") or 1)
    sample_rate = int(current_config.get("sample_rate") or 44100)
    mic_test_gain = _clamp_float(current_config.get("mic_test_gain", 1.0), 1.0, 0.1, 10.0)
    duration = 2

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
    speaker_test_percent = int(speaker_test_volume * 100)
    sample_file = BASE_DIR / "sounds" / "beep.wav"

    if not sample_file.exists():
        return jsonify({"success": False, "message": f"Sample file not found: {sample_file}"}), 404

    subprocess.run(
        ["amixer", "set", mixer_control, f"{speaker_test_percent}%"], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    try:
        result = subprocess.run(
            ["aplay", "-q", "-D", hw_mapping, str(sample_file)],
            capture_output=True, timeout=10, env=_c_locale_env(),
        )
    except FileNotFoundError:
        return jsonify({"success": False, "message": "'aplay' not found. Is ALSA installed?"}), 500
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "message": "Playback timed out."}), 500

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="ignore").strip()
        logger.error(f"aplay failed: {stderr}")
        return jsonify({"success": False, "message": _friendly_alsa_error(stderr, hw_mapping, is_playback=True)}), 500

    return jsonify({
        "success": True,
        "message": f"Playback finished ({speaker_test_percent}% on '{mixer_control}').",
    })


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
