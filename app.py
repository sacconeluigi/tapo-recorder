import os
import sys
import json
import time
import threading
import subprocess
import urllib.parse
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_file, abort
import imageio_ffmpeg

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
VIDEO_DIR = BASE_DIR / "videos"
VIDEO_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = BASE_DIR / "settings.json"

recorder_state = {
    "is_recording": False,
    "start_time": None,
    "current_file": None,
    "process": None,
    "last_error": None,
}
state_lock = threading.Lock()


def load_settings():
    default = {
        "host": "79.20.231.70",
        "port": "554",
        "username": "luigi1",
        "password": "Luigi2026",
        "quality": "Alta (HD - stream1)",
    }
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                default.update(data)
        except Exception:
            pass
    return default


def save_settings(data):
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return True
    except Exception as e:
        return False


@app.route("/")
def index():
    settings = load_settings()
    return render_template("index.html", settings=settings)


@app.route("/api/status")
def get_status():
    with state_lock:
        is_rec = recorder_state["is_recording"]
        start_t = recorder_state["start_time"]
        elapsed = int(time.time() - start_t) if is_rec and start_t else 0
        cur_file = recorder_state["current_file"]
        last_err = recorder_state["last_error"]

    mins, secs = divmod(elapsed, 60)
    hrs, mins = divmod(mins, 60)
    timer_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"

    return jsonify({
        "is_recording": is_rec,
        "elapsed_seconds": elapsed,
        "timer_formatted": timer_str,
        "current_file": Path(cur_file).name if cur_file else None,
        "last_error": last_err,
    })


@app.route("/api/start", methods=["POST"])
def start_recording():
    settings = load_settings()
    req_data = request.get_json() or {}
    for k in ["host", "port", "username", "password", "quality"]:
        if k in req_data:
            settings[k] = req_data[k]
    save_settings(settings)

    with state_lock:
        if recorder_state["is_recording"]:
            return jsonify({"success": False, "error": "Registrazione già attiva."}), 400

        host = settings.get("host", "").strip()
        port = settings.get("port", "554").strip()
        user = settings.get("username", "").strip()
        pwd = settings.get("password", "")

        if not host or not user or not pwd:
            return jsonify({"success": False, "error": "Compila Host, Nome Utente e Password."}), 400

        stream = "stream1" if "stream1" in settings.get("quality", "") else "stream2"
        safe_user = urllib.parse.quote(user, safe="")
        safe_pwd = urllib.parse.quote(pwd, safe="")
        rtsp_url = f"rtsp://{safe_user}:{safe_pwd}@{host}:{port}/{stream}"

        timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
        output_file = VIDEO_DIR / f"Tapo_{timestamp}.mp4"
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        cmd = [
            ffmpeg_exe,
            "-rtsp_transport",
            "tcp",
            "-i",
            rtsp_url,
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output_file),
        ]

        try:
            p = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            recorder_state["is_recording"] = True
            recorder_state["start_time"] = time.time()
            recorder_state["current_file"] = str(output_file)
            recorder_state["process"] = p
            recorder_state["last_error"] = None
        except Exception as e:
            return jsonify({"success": False, "error": f"Errore avvio: {str(e)}"}), 500

    return jsonify({"success": True, "message": "Registrazione avviata!", "file": output_file.name})


@app.route("/api/stop", methods=["POST"])
def stop_recording():
    saved_file = None

    with state_lock:
        if not recorder_state["is_recording"] or not recorder_state["process"]:
            return jsonify({"success": False, "error": "Nessuna registrazione in corso."}), 400

        p = recorder_state["process"]
        saved_file = recorder_state["current_file"]
        recorder_state["is_recording"] = False
        recorder_state["process"] = None

        try:
            p.communicate(b"q", timeout=6)
        except Exception:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                p.kill()

    return jsonify({
        "success": True,
        "message": "Registrazione salvata con successo!",
        "file": Path(saved_file).name if saved_file else None,
    })


@app.route("/api/videos")
def list_videos():
    videos = []
    for f in VIDEO_DIR.glob("*.mp4"):
        if f.is_file():
            stat = f.stat()
            size_mb = round(stat.st_size / (1024 * 1024), 2)
            mtime_str = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(stat.st_mtime))
            videos.append({
                "name": f.name,
                "size_mb": size_mb,
                "date": mtime_str,
                "mtime": stat.st_mtime,
            })
    videos.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"videos": videos})


@app.route("/api/video/<filename>")
def stream_video(filename):
    file_path = VIDEO_DIR / filename
    if not file_path.exists():
        abort(404)
    return send_file(file_path, mimetype="video/mp4")


@app.route("/api/delete_video/<filename>", methods=["POST"])
def delete_video(filename):
    file_path = VIDEO_DIR / filename
    if file_path.exists():
        try:
            file_path.unlink()
            return jsonify({"success": True, "message": f"File {filename} eliminato."})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": False, "error": "File non trovato."}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
