# Easy Speech — Voice Clone Expansion Pack

High-fidelity offline AI voice cloning powered by **F5-TTS Diffusion** and **Faster-Whisper**.
Compatible with **macOS** (Apple Silicon M1/M2/M3/M4 & Intel) and **Windows** (NVIDIA RTX & CPU).

---

## System Requirements

### macOS
- **Operating System:** macOS 12 (Monterey), 13 (Ventura), 14 (Sonoma), 15 (Sequoia) or newer
- **Chip:** Apple Silicon (M1/M2/M3/M4) recommended for native Metal (MPS) GPU acceleration, or Intel x64
- **Host Application:** Adobe Premiere Pro / After Effects (with Easy Speech CEP Extension)
- **Python:** Python 3.9 – 3.12 (Installable via `brew install python@3.11` or python.org)

### Windows
- **Operating System:** Windows 10 / 11 (64-bit)
- **GPU:** NVIDIA GeForce RTX Series (RTX 2060, 3050, 3060, 4060 or higher) with 4+ GB VRAM recommended
- **Host Application:** Adobe Premiere Pro / After Effects (with Easy Speech CEP Extension)
- **Python:** Python 3.10 or 3.11 (64-bit)

---

## Quick Start Guide

### macOS Setup

#### Step 1: Run Installer (One-Time Setup)
Open Terminal, navigate to the `VoiceClonePack` folder, and run:
```bash
chmod +x install_service.sh start_service.sh
./install_service.sh
```
This automatically sets up a dedicated macOS virtual environment (`.venv`), installs PyTorch with Apple Metal (MPS) support, and downloads the required voice cloning dependencies.

#### Step 2: Running the Service
- **Automatic:** Easy Speech will automatically launch the service in the background whenever you clone or generate a cloned voice!
- **Manual (Optional):** You can also launch it manually in a Terminal window:
  ```bash
  ./start_service.sh
  ```

---

### Windows Setup

#### Step 1: Run Installer (One-Time Setup)
Double-click `install_service.bat` to set up the dedicated Python virtual environment and install PyTorch with CUDA acceleration.

#### Step 2: Start the Service
Double-click `start_service.bat`. Keep the terminal window open while editing in Premiere Pro / After Effects.

---

## Clone & Generate in Easy Speech
1. Open the **Easy Speech** panel in Premiere Pro or After Effects.
2. In the Voice selector, click **🎙️ Cloned** → **➕ Clone New Voice**.
3. Enter a voice profile name and drop a 5–10 second clean audio clip (WAV, MP3, or M4A).
4. Review the auto-transcribed reference text (or click **✨ Auto-Transcribe**).
5. Click **Save & Add Voice**.
6. Type any text in your script and click **Generate Speech** — audio will generate and drop straight onto your timeline!

---

## Architecture & Benefits
- **Zero Host Bloat:** The base Easy Speech plugin installer stays compact (<500 MB).
- **Native Metal & CUDA Acceleration:** Uses Apple Silicon Metal (MPS) on Mac and CUDA on NVIDIA Windows systems.
- **Warm Inference:** Keeps the neural diffusion model warm in memory to eliminate cold-start wait times.
- **Automatic VRAM / Memory Release:** Automatically frees unused PyTorch cache after each generation to preserve playback performance in Premiere Pro and After Effects.
