# Tripper

**TR**anscript r**IPPER** — a local web app that downloads a YouTube or Instagram Reel video, transcribes the audio using OpenAI Whisper, optionally detects slide transitions, and produces a clean timestamped transcript.

## What it does

1. **Downloads** the video via yt-dlp
2. **Detects slide changes** using ffmpeg scene detection (optional, with adjustable sensitivity)
3. **Transcribes** the audio using OpenAI Whisper (runs locally — nothing is sent to the cloud)
4. **Merges** slide change markers into the transcript at the correct timestamps
5. **Outputs** a downloadable `.txt` file with timestamped slide markers

## Prerequisites

You need these installed and available on your PATH:

- **Python 3.9+**
- **ffmpeg** — `brew install ffmpeg`
- **yt-dlp** — `brew install yt-dlp`

On Linux, use your package manager instead of Homebrew.

## Setup

```bash
git clone <this-repo-url>
cd ytslideripper

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
source venv/bin/activate
python app.py
```

Open **http://localhost:5001** in your browser.

## Usage

1. Paste a YouTube or Instagram Reel URL
2. Toggle slide detection on/off as needed
3. Adjust sensitivity (0.3 is a good default for talks with slides)
4. Pick a Whisper model (medium is recommended for English talks)
5. Hit **RIP IT**

Progress streams live to the browser. When it's done, you can view the transcript, copy it to clipboard, or download it as a `.txt` file.

## Caching

Downloaded videos and Whisper transcriptions are cached in `/tmp/talk_transcribe_cache/`. If you re-run with a different sensitivity threshold, only the scene detection and merge steps re-run — the download and transcription are skipped. This makes tuning the threshold fast.

## Whisper model guide

| Model | Speed (45-min talk) | Accuracy | Best for |
|-------|-------------------|----------|----------|
| tiny | ~30 sec | Low | Quick previews |
| small | ~1-2 min | Good | Faster machines, clear audio |
| medium | ~2-5 min | Very good | Most English talks (recommended) |
| large | ~10-20 min | Best | Accented speech, noisy audio, non-English |

All models run locally on your CPU/GPU. Nothing is sent to the cloud.
