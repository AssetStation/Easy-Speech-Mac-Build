# Easy Speech — RTX Voice Clone Expansion Pack

High-fidelity offline AI voice cloning powered by **F5-TTS Diffusion** and **Faster-Whisper**.

## System Requirements
- **Operating System:** Windows 10 / 11 (64-bit)
- **GPU:** NVIDIA GeForce RTX Series (RTX 2060, 3050, 3060, 4060 or higher) with at least 4 GB VRAM
- **Host Application:** Adobe Premiere Pro / After Effects (with Easy Speech CEP Extension)
- **Python:** Python 3.10 or 3.11 (64-bit)

## Quick Start Guide

### Windows Setup
- **Automatic:** Open Easy Speech in Premiere Pro / After Effects → switch to Voice Clone → click **"Install Voice Clone Pack"**.
- **Manual (Optional):** Double-click `install_service.bat`, then double-click `start_service.bat`.

### macOS Setup
- **Automatic:** Open Easy Speech in Premiere Pro / After Effects → switch to Voice Clone → click **"Install Voice Clone Pack"**.
- **Manual (Optional):** Double-click `Install_Voice_Clone.command` in Finder (or run `./install_service.sh` in Terminal).

Keep the service running (or let Easy Speech auto-spawn it in the background) while using Voice Cloning in your video timeline.

### Step 3: Clone & Generate in Easy Speech
1. Open the **Easy Speech** panel in Premiere Pro.
2. Under the voice selection menu, click **🎙️ Cloned** → **➕ Clone New Voice**.
3. Enter a voice profile name and drop a 5–10 second clean audio clip (WAV, MP3, or M4A).
4. Review the auto-transcribed reference text.
5. Click **Save & Add Voice**.
6. Type any text and click **Generate Speech** — audio will generate in 5–10 seconds and drop right onto your timeline!

## Architecture & Benefits
- **Zero Host Bloat:** The base Easy Speech plugin installer stays compact (<500 MB) and virus-free.
- **Warm Inference:** Keeps the neural diffusion model warm in VRAM, eliminating the 150-second cold-start wait.
- **Automatic VRAM Release:** Automatically frees unused PyTorch cache after each generation to protect Premiere Pro's Mercury Playback Engine.
