import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, Response, render_template, request, jsonify, send_file

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
CACHE_DIR = Path(tempfile.gettempdir()) / "talk_transcribe_cache"
CACHE_DIR.mkdir(exist_ok=True)

# Simple job state – single user, one job at a time
_job_lock = threading.Lock()
_job = {
    "running": False,
    "events": [],       # list of (type, data) tuples
    "result": None,     # final transcript text
    "filename": None,   # suggested download filename
}


def _url_hash(url: str) -> str:
    """Stable short hash for a video URL (strips tracking params)."""
    # Try YouTube video id first
    m = re.search(r"(?:v=|youtu\.be/)([\w-]{11})", url)
    if m:
        key = m.group(1)
    else:
        # Instagram reel/post id or generic: strip query params
        m = re.search(r"instagram\.com/(?:reel|p)/([\w-]+)", url)
        key = m.group(1) if m else url.split("?")[0]
    return hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------
def check_dependencies() -> list[str]:
    missing = []
    if not shutil.which("yt-dlp"):
        missing.append("yt-dlp")
    if not shutil.which("ffmpeg"):
        missing.append("ffmpeg")
    return missing


# ---------------------------------------------------------------------------
# Pipeline helpers
# ---------------------------------------------------------------------------
def _emit(msg: str, etype: str = "progress"):
    _job["events"].append((etype, msg))


def download_video(url: str, cache_key: str) -> tuple[Path, Path, str]:
    """Download video+audio via yt-dlp.  Returns (video_path, audio_path, title).
    Skips download if cached."""
    video_dir = CACHE_DIR / cache_key
    video_dir.mkdir(exist_ok=True)
    meta_file = video_dir / "meta.json"

    # Check cache
    if meta_file.exists():
        meta = json.loads(meta_file.read_text())
        vp = Path(meta["video"])
        ap = Path(meta["audio"])
        if vp.exists() and ap.exists():
            _emit("Using cached download.")
            return vp, ap, meta["title"]

    _emit("Downloading video...")

    # Get title first
    title_proc = subprocess.run(
        ["yt-dlp", "--print", "title", "--no-warnings", url],
        capture_output=True, text=True,
    )
    title = title_proc.stdout.strip() or "transcript"

    video_path = video_dir / "video.mp4"
    audio_path = video_dir / "audio.wav"

    # Download best video+audio merged into mp4
    subprocess.run(
        [
            "yt-dlp",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "-o", str(video_path),
            "--no-warnings",
            "--no-playlist",
            url,
        ],
        capture_output=True, text=True,
        check=True,
    )

    # Extract audio as 16kHz mono WAV for Whisper
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video_path),
            "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            str(audio_path),
        ],
        capture_output=True, text=True,
        check=True,
    )

    meta_file.write_text(json.dumps({
        "video": str(video_path),
        "audio": str(audio_path),
        "title": title,
    }))
    _emit("Download complete.")
    return video_path, audio_path, title


def detect_slides(video_path: Path, threshold: float) -> list[float]:
    """Run ffmpeg scene detection.  Returns sorted list of timestamps (seconds).
    Uses Popen to stream stderr so we can emit progress and avoid connection drops."""
    _emit(f"Detecting slide changes (threshold={threshold})...")
    proc = subprocess.Popen(
        [
            "ffmpeg", "-i", str(video_path),
            "-filter:v", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    timestamps: list[float] = []
    last_heartbeat = time.time()
    for line in proc.stderr:
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            timestamps.append(float(m.group(1)))
        # Emit a heartbeat every 30s so the SSE connection stays alive
        now = time.time()
        if now - last_heartbeat > 30:
            _emit(f"Detecting slide changes... {len(timestamps)} transitions so far")
            last_heartbeat = now
    proc.wait()
    timestamps.sort()
    _emit(f"Detecting slide changes... found {len(timestamps)} transitions")
    return timestamps


def transcribe_audio(audio_path: Path, model_name: str, cache_key: str) -> list[dict]:
    """Run Whisper transcription.  Returns list of segments [{start, end, text}].
    Caches result per (url, model)."""
    cache_file = CACHE_DIR / cache_key / f"whisper_{model_name}.json"
    if cache_file.exists():
        _emit("Using cached transcription.")
        return json.loads(cache_file.read_text())

    _emit(f"Transcribing audio with Whisper ({model_name} model — this takes a while)...")

    import whisper  # lazy import so startup stays fast

    model = whisper.load_model(model_name)
    result = model.transcribe(str(audio_path), verbose=False)

    segments = [
        {"start": seg["start"], "end": seg["end"], "text": seg["text"].strip()}
        for seg in result.get("segments", [])
    ]
    cache_file.write_text(json.dumps(segments))
    _emit("Transcription complete.")
    return segments


def merge_transcript(segments: list[dict], slide_times: list[float]) -> str:
    """Interleave slide markers into the transcript at segment boundaries."""
    _emit("Merging transcript with slide markers...")

    def fmt_ts(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    # Build a combined timeline:
    # For each slide change, find the nearest segment boundary (start of a segment)
    # and insert the marker just before that segment.
    # We'll process segments in order and insert markers as we go.

    lines: list[str] = []
    slide_idx = 0

    # Insert an initial slide marker at 00:00:00 if the first scene change
    # doesn't coincide with the very start of the video
    if slide_times and slide_times[0] > 1.0:
        lines.append(f"\n[{fmt_ts(0)}] --- SLIDE CHANGE ---\n")
    elif not slide_times and segments:
        lines.append(f"\n[{fmt_ts(0)}] --- SLIDE CHANGE ---\n")

    for seg in segments:
        # Insert any slide markers whose timestamp falls before this segment's midpoint
        while slide_idx < len(slide_times) and slide_times[slide_idx] <= (seg["start"] + seg["end"]) / 2:
            ts = slide_times[slide_idx]
            lines.append(f"\n[{fmt_ts(ts)}] --- SLIDE CHANGE ---\n")
            slide_idx += 1

        lines.append(seg["text"])

    # Any remaining slide markers at the end
    while slide_idx < len(slide_times):
        ts = slide_times[slide_idx]
        lines.append(f"\n[{fmt_ts(ts)}] --- SLIDE CHANGE ---\n")
        slide_idx += 1

    _emit("Merging transcript with slide markers... Done!")

    # Group text between slide markers into paragraphs
    output = _format_output(lines)
    return output


def _format_output(lines: list[str]) -> str:
    """Combine raw lines into the final output format.
    Groups consecutive text lines into paragraphs under each slide marker."""
    result_parts: list[str] = []
    current_text: list[str] = []

    for line in lines:
        if "--- SLIDE CHANGE ---" in line:
            # Flush accumulated text
            if current_text:
                result_parts.append(" ".join(current_text))
                current_text = []
            result_parts.append(line.strip())
        else:
            stripped = line.strip()
            if stripped:
                current_text.append(stripped)

    # Flush any trailing text
    if current_text:
        result_parts.append(" ".join(current_text))

    return "\n".join(result_parts) + "\n"


# ---------------------------------------------------------------------------
# Processing job
# ---------------------------------------------------------------------------
def _run_job(url: str, threshold: float, model_name: str, detect_slides_flag: bool = True):
    try:
        cache_key = _url_hash(url)

        video_path, audio_path, title = download_video(url, cache_key)

        if detect_slides_flag:
            slide_times = detect_slides(video_path, threshold)
        else:
            slide_times = []
            _emit("Slide detection disabled, skipping.")

        segments = transcribe_audio(audio_path, model_name, cache_key)
        transcript = merge_transcript(segments, slide_times)

        # Clean filename from title
        safe_title = re.sub(r'[^\w\s-]', '', title).strip()[:80]
        safe_title = re.sub(r'\s+', '_', safe_title)

        _job["result"] = transcript
        _job["filename"] = f"{safe_title}_transcript.txt"
        _emit("Done!", "done")

    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "")[:500]
        _emit(f"Error running command: {e.cmd[0]} — {stderr}", "error")
    except Exception as e:
        _emit(f"Error: {e}", "error")
    finally:
        _job["running"] = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    missing = check_dependencies()
    return render_template("index.html", missing_deps=missing)


@app.route("/start", methods=["POST"])
def start():
    if _job["running"]:
        return jsonify({"error": "A job is already running."}), 409

    data = request.get_json()
    url = data.get("url", "").strip()
    threshold = float(data.get("threshold", 0.3))
    model_name = data.get("model", "medium")
    detect_slides_flag = data.get("detect_slides", True)

    if not url:
        return jsonify({"error": "URL is required."}), 400

    # Validate threshold
    threshold = max(0.1, min(0.8, threshold))

    # Validate model
    if model_name not in ("tiny", "small", "medium", "large"):
        model_name = "medium"

    # Reset job state
    _job["running"] = True
    _job["events"] = []
    _job["result"] = None
    _job["filename"] = None

    thread = threading.Thread(target=_run_job, args=(url, threshold, model_name, detect_slides_flag), daemon=True)
    thread.start()

    return jsonify({"ok": True})


@app.route("/events")
def events():
    """SSE endpoint — streams progress events to the frontend."""
    def generate():
        sent = 0
        while True:
            evts = _job["events"]
            while sent < len(evts):
                etype, data = evts[sent]
                yield f"event: {etype}\ndata: {data}\n\n"
                sent += 1
                if etype in ("done", "error"):
                    return
            time.sleep(0.3)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/result")
def result():
    if _job["result"] is None:
        return jsonify({"error": "No result available."}), 404
    return jsonify({"transcript": _job["result"], "filename": _job["filename"]})


@app.route("/download")
def download():
    if _job["result"] is None:
        return "No result available.", 404

    # Write to a temp file and send
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8")
    tmp.write(_job["result"])
    tmp.close()
    return send_file(
        tmp.name,
        as_attachment=True,
        download_name=_job["filename"] or "transcript.txt",
        mimetype="text/plain",
    )


@app.route("/clear-cache", methods=["POST"])
def clear_cache():
    """Clear all cached downloads and transcriptions."""
    if _job["running"]:
        return jsonify({"error": "Cannot clear cache while a job is running."}), 409
    try:
        shutil.rmtree(CACHE_DIR)
        CACHE_DIR.mkdir(exist_ok=True)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5001, threaded=True)
