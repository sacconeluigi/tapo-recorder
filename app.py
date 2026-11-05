import os
import sys
import json
import time
import threading
import subprocess
import urllib.parse
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_file, Response, abort
import imageio_ffmpeg

app = Flask(__name__, template_folder=".")

BASE_DIR = Path(__file__).resolve().parent
MEDIA_DIR = BASE_DIR / "videos"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = BASE_DIR / "settings.json"

recorder_state = {
    "is_recording": False,
    "start_time": None,
    "current_file": None,
    "rec_type": "video_audio",
    "process": None,
    "last_error": None,
}
state_lock = threading.Lock()


def load_settings():
    default = {
        "mode": "outside",
        "ip_home": "192.168.1.18",
        "ip_outside": "79.20.231.70",
        "port": "554",
        "username": "luigi1",
        "password": "Luigi2026",
        "quality": "Alta (HD - stream1)",
        "audio_mode": "video_audio",  # video_audio, video_only, audio_only
        "split_mode": "5m",           # none, 5m, 10m, 15m, 30m, 1h, 2h
        "duration_limit": "0",       # 0 = illimitato, 10800 = 3h, 21600 = 6h, 43200 = 12h
        "gdrive_url": "",            # URL Webhook Google Drive per salvataggio automatico
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


def get_active_host(settings):
    mode = settings.get("mode", "outside")
    if mode == "inside":
        return settings.get("ip_home", "192.168.1.18").strip()
    return settings.get("ip_outside", "79.20.231.70").strip()


def cleanup_disk_space():
    """Mantiene la cartella videos sotto i 350MB eliminando automaticamente i file più vecchi."""
    try:
        files = [f for f in MEDIA_DIR.glob("*.*") if f.is_file() and f.suffix.lower() in [".mp4", ".m4a", ".jpg"]]
        total_size = sum(f.stat().st_size for f in files)
        max_bytes = 350 * 1024 * 1024  # 350 MB limite di sicurezza
        if total_size > max_bytes:
            files.sort(key=lambda x: x.stat().st_mtime)
            while total_size > max_bytes and files:
                oldest = files.pop(0)
                sz = oldest.stat().st_size
                oldest.unlink(missing_ok=True)
                total_size -= sz
    except Exception as e:
        print("Errore cleanup:", e)


def upload_to_gdrive(file_path):
    """Carica un file video o audio direttamente su Google Drive via Webhook Apps Script."""
    settings = load_settings()
    url = settings.get("gdrive_url", "").strip()
    if not url:
        return False
    try:
        import base64
        import urllib.request
        import urllib.parse
        p = Path(file_path)
        if not p.exists():
            return False

        file_size = p.stat().st_size
        # Se il file supera i 40MB, Google Apps Script rifiuterebbe il payload (>50MB base64)
        if file_size > 40 * 1024 * 1024:
            print(f"File {p.name} troppo grande per Webhook Apps Script ({file_size / (1024*1024):.1f} MB > 40 MB).")
            return False

        with open(p, "rb") as f:
            content = f.read()
        b64 = base64.b64encode(content).decode("utf-8")
        post_data = urllib.parse.urlencode({
            "filename": p.name,
            "mimetype": "video/mp4" if p.suffix == ".mp4" else ("audio/mp4" if p.suffix == ".m4a" else "image/jpeg"),
            "data": b64
        }).encode("utf-8")
        req = urllib.request.Request(url, data=post_data, headers={"User-Agent": "TapoCloudRecorder"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            res_text = resp.read().decode("utf-8")
            if "OK" in res_text:
                cleanup_disk_space()
                return True
    except Exception as e:
        print("Errore upload Google Drive:", e)
    return False


uploaded_gdrive_files = set()


def start_ffmpeg_process(settings):
    """Avvia il processo FFmpeg con gestione socket TCP e spezzettamento automatico."""
    host = get_active_host(settings)
    port = settings.get("port", "554").strip()
    user = settings.get("username", "luigi1").strip()
    pwd = settings.get("password", "Luigi2026")
    if not host or not user or not pwd:
        raise ValueError("Parametri telecamera mancanti.")

    stream = "stream1" if "stream1" in settings.get("quality", "") else "stream2"
    safe_user = urllib.parse.quote(user, safe="")
    safe_pwd = urllib.parse.quote(pwd, safe="")
    rtsp_url = f"rtsp://{safe_user}:{safe_pwd}@{host}:{port}/{stream}"

    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    audio_mode = settings.get("audio_mode", "video_audio")
    split_mode = settings.get("split_mode", "5m")
    dur_limit = int(settings.get("duration_limit", "0") or "0")

    cmd = [
        ffmpeg_exe,
        "-rtsp_transport", "tcp",
        "-i", rtsp_url
    ]

    if dur_limit > 0:
        cmd.extend(["-t", str(dur_limit)])

    if audio_mode == "audio_only":
        ext = "m4a"
        output_pattern = str(MEDIA_DIR / f"Tapo_Audio_{timestamp}.{ext}")
        cmd.extend(["-vn", "-c:a", "aac", output_pattern])
    elif split_mode in ["5m", "10m", "15m", "30m", "1h", "2h"]:
        seg_map = {
            "5m": 300,
            "10m": 600,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200
        }
        seg_time = seg_map.get(split_mode, 300)
        output_pattern = str(MEDIA_DIR / "Tapo_%Y-%m-%d_%H-%M-%S.mp4")
        if audio_mode == "video_only":
            cmd.extend(["-an", "-c:v", "copy"])
        else:
            cmd.extend(["-c:v", "copy", "-c:a", "aac"])

        cmd.extend([
            "-movflags", "+faststart",
            "-f", "segment",
            "-segment_time", str(seg_time),
            "-reset_timestamps", "1",
            "-strftime", "1",
            output_pattern,
        ])
    else:
        output_pattern = str(MEDIA_DIR / f"Tapo_{timestamp}.mp4")
        if audio_mode == "video_only":
            cmd.extend(["-an", "-c:v", "copy"])
        else:
            cmd.extend(["-c:v", "copy", "-c:a", "aac"])
        cmd.extend(["-movflags", "+faststart", output_pattern])

    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    if p.poll() is not None:
        err = p.stderr.read().decode("utf-8", errors="ignore") if p.stderr else ""
        raise RuntimeError(f"FFmpeg terminato subito: {err[-250:]}")

    return p, output_pattern


def recorder_watchdog():
    """Watchdog continuo:
    1. Ping Keep-Alive ogni 3 minuti per evitare lo sleep di Render quando chiudi l'app
    2. Rileva i singoli spezzoni completati e li invia subito a Google Drive
    3. Riconnette la registrazione se la rete o telecamera ha una caduta temporanea
    """
    last_ping = 0
    file_stat_cache = {}

    while True:
        time.sleep(10)
        now = time.time()

        with state_lock:
            is_rec = recorder_state.get("is_recording", False)
            proc = recorder_state.get("process")

        # 1. Keep-Alive per Render (ogni 3 minuti mentre si registra per non far dormire il server)
        if is_rec and (now - last_ping > 180):
            last_ping = now
            try:
                req = urllib.request.Request(
                    "https://tapo-recorder.onrender.com/api/status",
                    headers={"User-Agent": "TapoKeepAlive/1.0"}
                )
                with urllib.request.urlopen(req, timeout=10) as _:
                    pass
            except Exception:
                pass

        # 2. Invio automatico spezzoni a Google Drive mentre la registrazione continua
        try:
            settings = load_settings()
            if settings.get("gdrive_url", "").strip():
                all_files = sorted(MEDIA_DIR.glob("*.*"), key=lambda x: x.stat().st_mtime)
                for f in all_files:
                    if not (f.is_file() and f.suffix.lower() in [".mp4", ".m4a"]):
                        continue
                    fname = f.name
                    if fname in uploaded_gdrive_files:
                        continue

                    cur_sz = f.stat().st_size
                    prev_sz, prev_t = file_stat_cache.get(fname, (0, 0))

                    # Se il file ha smesso di crescere da almeno 20s ed è > 50KB, lo spezzone è completato!
                    if cur_sz == prev_sz and cur_sz > 50000 and (now - prev_t >= 20):
                        ok = upload_to_gdrive(f)
                        if ok:
                            uploaded_gdrive_files.add(fname)
                    else:
                        file_stat_cache[fname] = (cur_sz, now)
        except Exception:
            pass

        # 3. Watchdog: se ffmpeg cade durante la registrazione attiva, riavvia
        if is_rec and proc and proc.poll() is not None:
            time.sleep(3)
            with state_lock:
                if recorder_state.get("is_recording", False):
                    try:
                        settings = load_settings()
                        new_p, new_pat = start_ffmpeg_process(settings)
                        recorder_state["process"] = new_p
                        recorder_state["current_file"] = new_pat
                    except Exception as e:
                        print("Watchdog error:", e)


# Avvia il watchdog in background all'avvio
threading.Thread(target=recorder_watchdog, daemon=True).start()


@app.route("/")
def index():
    settings = load_settings()
    return render_template("index.html", settings=settings)


@app.route("/manifest.json")
def manifest():
    return send_file(BASE_DIR / "manifest.json", mimetype="application/manifest+json")


@app.route("/icon.png")
def icon():
    return send_file(BASE_DIR / "icon.png", mimetype="image/png")


@app.route("/api/status")
def get_status():
    with state_lock:
        is_rec = recorder_state["is_recording"]
        start_t = recorder_state["start_time"]
        elapsed = int(time.time() - start_t) if is_rec and start_t else 0
        cur_file = recorder_state["current_file"]
        rec_type = recorder_state["rec_type"]
        last_err = recorder_state["last_error"]

    mins, secs = divmod(elapsed, 60)
    hrs, mins = divmod(mins, 60)
    timer_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"

    return jsonify({
        "is_recording": is_rec,
        "elapsed_seconds": elapsed,
        "timer_formatted": timer_str,
        "current_file": Path(cur_file).name if (is_rec and cur_file) else None,
        "rec_type": rec_type,
        "last_error": last_err,
    })


@app.route("/api/settings", methods=["GET", "POST"])
def handle_settings():
    if request.method == "POST":
        data = request.get_json() or {}
        cur = load_settings()
        for k in ["mode", "ip_home", "ip_outside", "username", "password", "quality", "port", "audio_mode", "split_mode", "duration_limit", "gdrive_url"]:
            if k in data:
                cur[k] = data[k]
        save_settings(cur)
        return jsonify({"success": True, "message": "Impostazioni salvate con successo."})
    return jsonify(load_settings())


@app.route("/api/start", methods=["POST"])
def start_recording():
    settings = load_settings()
    req_data = request.get_json() or {}
    
    for k in ["mode", "ip_home", "ip_outside", "username", "password", "quality", "port", "audio_mode", "split_mode", "duration_limit", "gdrive_url"]:
        if k in req_data:
            settings[k] = req_data[k]
    save_settings(settings)
    cleanup_disk_space()

    with state_lock:
        if recorder_state["is_recording"]:
            return jsonify({"success": False, "error": "Registrazione già attiva."}), 400

        try:
            p, output_pattern = start_ffmpeg_process(settings)
            recorder_state["is_recording"] = True
            recorder_state["start_time"] = time.time()
            recorder_state["current_file"] = output_pattern
            recorder_state["rec_type"] = settings.get("audio_mode", "video_audio")
            recorder_state["process"] = p
            recorder_state["last_error"] = None
        except Exception as e:
            return jsonify({"success": False, "error": f"Errore avvio: {str(e)}"}), 500

    audio_mode = settings.get("audio_mode", "video_audio")
    desc_map = {
        "video_audio": "Video Full HD + Audio microfono",
        "video_only": "Solo Video (Muto)",
        "audio_only": "Solo Audio (Registratore vocale)",
    }
    return jsonify({
        "success": True,
        "message": f"Registrazione avviata in modalità: {desc_map.get(audio_mode, audio_mode)}!",
        "file": Path(output_pattern).name,
    })


@app.route("/api/stop", methods=["POST"])
def stop_recording():
    saved_file = None

    with state_lock:
        if not recorder_state["is_recording"] or not recorder_state["process"]:
            return jsonify({"success": False, "error": "Nessuna registrazione in corso."}), 400

        p = recorder_state["process"]
        saved_file = recorder_state["current_file"]
        recorder_state["is_recording"] = False
        recorder_state["current_file"] = None
        recorder_state["start_time"] = None
        recorder_state["process"] = None

        try:
            p.communicate(b"q", timeout=6)
        except Exception:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                p.kill()

    def auto_upload_latest():
        time.sleep(1)
        for f in sorted(MEDIA_DIR.glob("*.*"), key=lambda x: x.stat().st_mtime, reverse=True)[:3]:
            if f.is_file() and f.suffix.lower() in [".mp4", ".m4a", ".jpg"]:
                upload_to_gdrive(f)

    threading.Thread(target=auto_upload_latest, daemon=True).start()

    return jsonify({
        "success": True,
        "message": "Registrazione salvata con successo!",
        "file": Path(saved_file).name if saved_file else None,
    })


@app.route("/api/upload_drive/<filename>", methods=["POST"])
def api_upload_drive(filename):
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        return jsonify({"success": False, "error": "File non trovato sul server."}), 404
    ok = upload_to_gdrive(file_path)
    if ok:
        return jsonify({"success": True, "message": f"File {filename} caricato con successo nel tuo Google Drive!"})
    return jsonify({"success": False, "error": "Impossibile caricare su Google Drive. Verifica il link nelle impostazioni."}), 500


@app.route("/api/snapshot")
def take_snapshot():
    """Scatta una foto istantanea JPEG dalla telecamera senza disturbare la registrazione su stream1."""
    settings = load_settings()
    host = get_active_host(settings)
    port = settings.get("port", "554").strip()
    user = settings.get("username", "luigi1").strip()
    pwd = settings.get("password", "Luigi2026")

    safe_user = urllib.parse.quote(user, safe="")
    safe_pwd = urllib.parse.quote(pwd, safe="")
    # Usiamo stream2 per lo scatto: NON interferisce con stream1 usato dalla registrazione continua!
    rtsp_url = f"rtsp://{safe_user}:{safe_pwd}@{host}:{port}/stream2"
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    timestamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    snap_path = MEDIA_DIR / f"Foto_Tapo_{timestamp}.jpg"

    cmd = [
        ffmpeg_exe,
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-frames:v", "1",
        "-q:v", "2",
        "-y",
        str(snap_path)
    ]

    try:
        subprocess.run(cmd, capture_output=True, timeout=8)
        if snap_path.exists():
            return send_file(snap_path, mimetype="image/jpeg", as_attachment=True, download_name=snap_path.name)
        return jsonify({"error": "Impossibile scattare foto"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/live_stream")
def live_stream():
    """Genera un flusso video in diretta MJPEG compatibile con iPhone, Android e PC."""
    settings = load_settings()
    host = get_active_host(settings)
    port = settings.get("port", "554").strip()
    user = settings.get("username", "luigi1").strip()
    pwd = settings.get("password", "Luigi2026")

    safe_user = urllib.parse.quote(user, safe="")
    safe_pwd = urllib.parse.quote(pwd, safe="")
    rtsp_url = f"rtsp://{safe_user}:{safe_pwd}@{host}:{port}/stream2"
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    cmd = [
        ffmpeg_exe,
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-f", "image2pipe",
        "-vf", "scale=640:-1",
        "-r", "6",
        "-q:v", "6",
        "-vcodec", "mjpeg",
        "-"
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=10**6
        )
    except Exception:
        abort(500)

    def generate():
        try:
            buffer = b""
            frames_sent = 0
            max_frames = 5 * 60 * 6  # 5 minuti massimo a 6fps per non bloccare connessioni infinite
            while frames_sent < max_frames:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buffer += chunk
                a = buffer.find(b"\xff\xd8")
                b = buffer.find(b"\xff\xd9")
                if a != -1 and b != -1 and b > a:
                    jpg = buffer[a:b+2]
                    buffer = buffer[b+2:]
                    frames_sent += 1
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                proc.kill()

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/videos")
def list_videos():
    files = []
    for f in MEDIA_DIR.glob("*.*"):
        if f.is_file() and f.suffix.lower() in [".mp4", ".m4a", ".jpg"]:
            stat = f.stat()
            size_mb = round(stat.st_size / (1024 * 1024), 2)
            mtime_str = time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(stat.st_mtime))
            files.append({
                "name": f.name,
                "size_mb": size_mb,
                "date": mtime_str,
                "mtime": stat.st_mtime,
                "type": "audio" if f.suffix == ".m4a" else ("photo" if f.suffix == ".jpg" else "video"),
            })
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify({"videos": files})


@app.route("/api/download/<filename>")
@app.route("/api/video/<filename>")
def stream_or_download_video(filename):
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        abort(404)
    mimetype = "video/mp4"
    if filename.endswith(".m4a"):
        mimetype = "audio/mp4"
    elif filename.endswith(".jpg"):
        mimetype = "image/jpeg"

    # Se la richiesta è su /api/download/ o include ?dl=1, forza l'allegato per iOS Safari
    is_download = request.path.startswith("/api/download") or request.args.get("dl") == "1"
    return send_file(
        file_path,
        mimetype=mimetype,
        as_attachment=is_download,
        download_name=filename
    )


def delete_from_gdrive(filename):
    """Elimina il file anche da Google Drive tramite il Webhook Apps Script."""
    settings = load_settings()
    url = settings.get("gdrive_url", "").strip()
    if not url:
        return False
    try:
        import urllib.request
        import urllib.parse
        post_data = urllib.parse.urlencode({
            "action": "delete",
            "filename": filename
        }).encode("utf-8")
        req = urllib.request.Request(url, data=post_data, headers={"User-Agent": "TapoCloudRecorder"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            res_text = resp.read().decode("utf-8")
            if "DELETED" in res_text or "OK" in res_text:
                return True
    except Exception as e:
        print(f"Errore cancellazione Google Drive per {filename}:", e)
    return False


@app.route("/api/delete_video/<filename>", methods=["POST"])
def delete_video(filename):
    file_path = MEDIA_DIR / filename
    local_deleted = False
    if file_path.exists():
        try:
            file_path.unlink()
            local_deleted = True
        except Exception as e:
            print("Errore eliminazione locale:", e)

    # Rimuovi dalla cache upload per sicurezza
    uploaded_gdrive_files.discard(filename)

    # Elimina anche da Google Drive
    gdrive_deleted = delete_from_gdrive(filename)

    if local_deleted or gdrive_deleted:
        msg = f"File {filename} eliminato con successo!"
        if gdrive_deleted:
            msg += " (Rimosso anche da Google Drive)"
        return jsonify({"success": True, "message": msg, "gdrive_deleted": gdrive_deleted})

    return jsonify({"success": False, "error": "File non trovato."}), 404


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
