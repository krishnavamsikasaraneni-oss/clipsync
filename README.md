# ClipSync

Copy on one Windows PC, paste on another, over the same Wi-Fi. Free, no server, no account, no API keys.

- **ClipSync Sender** (PC 1) - tray app that sends whatever text you copy.
- **ClipSync Receiver** (PC 2) - in **Paste mode** it puts clips on the clipboard. In **Type mode** it types them letter by letter into a web app window you pick, and never pastes. It keeps the last 10 clips. Also has an offline English mic (faster-whisper) and a typing box that types into any window at an adjustable words-per-minute speed.
- **Ctrl+Alt+Z** pauses or resumes sync from either PC. The app warns you if another program already owns the shortcut.

How to install and use it: [SETUP.md](SETUP.md).

## Getting the .exe files

**GitHub Actions (recommended):** Push this folder to a GitHub repo. Every push to `main` builds both apps on a free Windows runner. It runs the tests, bundles the speech model, checks each .exe starts, and uploads `ClipSyncSender-PC1` and `ClipSyncReceiver-PC2`. Get them from the repo's **Actions** tab > latest run > **Artifacts**. Push a tag like `v1.0` to also get them as a Release.

**On your own PC:** Install Python 3.12, then double-click `build.bat`. The output goes in `dist\`.

## How it works

| Piece | What it does |
|---|---|
| `clipsync/link.py` | Receiver runs a WebSocket server and announces itself with mDNS (`_clipsync._tcp`). Sender finds it, connects, reconnects forever with backoff, and remembers the last working address as a fallback. Pause state syncs both ways (newest change wins). |
| `clipsync/crypto.py` | Pairing uses SPAKE2 with the 6-digit code, so the code never crosses the network and can't be brute-forced offline. The code changes after 3 wrong tries. Each connection derives a fresh AES-256-GCM key from the paired key, with counters against replay. |
| `clipsync/winsys.py` | Global hotkey (`RegisterHotKey`, which detects conflicts), Start with Windows (HKCU Run key), and tracking the last window you used. |
| `clipsync/typer.py` | Types into the focused window with `pynput`. 60 / (wpm x 5) seconds per character, with human-like jitter at 150 wpm and below. A guard checks the locked window is still in front before every character. If it isn't, typing stops and the remaining text is queued until you click back in. |
| `clipsync/voice.py` | Mic capture with `sounddevice`, English transcription with faster-whisper `base.en` (int8, CPU), with voice activity detection. |

Clips from password managers are skipped (`ExcludeClipboardContentFromMonitorProcessing`, `CanIncludeInClipboardHistory=0`).

## Developing

```
pip install -r requirements.txt pytest
python receiver_app.py      # PC 2
python sender_app.py        # PC 1
pytest
```
