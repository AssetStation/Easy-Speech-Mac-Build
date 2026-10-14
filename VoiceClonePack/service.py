"""
Easy Speech - Voice Clone RTX Expansion Service
High-Fidelity Offline Voice Cloning Background Daemon (F5-TTS & Faster-Whisper)
"""

import os
import sys
import time
import json
import threading
import subprocess
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# Safe stream handling for windowless execution (pythonw.exe / detached daemons)
if sys.stdout is None:
    try:
        if sys.platform == "darwin":
            log_dir = os.path.join(os.path.expanduser("~"), "Library", "Logs", "EasySpeech")
        else:
            log_dir = os.path.join(os.environ.get('LOCALAPPDATA', os.path.dirname(os.path.abspath(__file__))), 'EasySpeech', 'logs')
        os.makedirs(log_dir, exist_ok=True)
        sys.stdout = open(os.path.join(log_dir, 'voice_clone_service.log'), 'a', encoding='utf-8', buffering=1)
    except Exception:
        sys.stdout = open(os.devnull, 'w', encoding='utf-8')

if sys.stderr is None:
    sys.stderr = sys.stdout

if hasattr(sys.stdout, 'reconfigure') and callable(getattr(sys.stdout, 'reconfigure', None)):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

if hasattr(sys.stderr, 'reconfigure') and callable(getattr(sys.stderr, 'reconfigure', None)):
    try:
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

HOST = "127.0.0.1"
PORT = 8765

PACK_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(PACK_DIR, "models")
if os.path.exists(MODELS_DIR):
    os.environ["HF_HOME"] = os.path.abspath(MODELS_DIR)

# Global State
START_TIME = time.time()
LAST_REQUEST_TIME = time.time()
MODEL_LOCK = threading.Lock()

_f5_instance = None
_whisper_instance = None
_ffmpeg_exe_path = None

def get_ffmpeg_path():
    global _ffmpeg_exe_path
    if _ffmpeg_exe_path and os.path.exists(_ffmpeg_exe_path):
        return _ffmpeg_exe_path

    # 1. Try imageio_ffmpeg
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            _ffmpeg_exe_path = exe
            ff_dir = os.path.dirname(exe)
            if ff_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = ff_dir + os.path.pathsep + os.environ.get("PATH", "")
            return _ffmpeg_exe_path
    except Exception:
        pass

    # 2. Check shutil.which (Standard Library, cross-platform)
    try:
        import shutil
        w = shutil.which("ffmpeg")
        if w and os.path.exists(w):
            _ffmpeg_exe_path = w
            return _ffmpeg_exe_path
    except Exception:
        pass

    # 3. macOS Homebrew and standard system binary paths
    if sys.platform == "darwin":
        for mac_path in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]:
            if os.path.exists(mac_path):
                _ffmpeg_exe_path = mac_path
                return _ffmpeg_exe_path

    # 4. Windows where fallback
    if sys.platform.startswith("win"):
        try:
            res = subprocess.run(["where", "ffmpeg"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0:
                lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
                if lines and os.path.exists(lines[0]):
                    _ffmpeg_exe_path = lines[0]
                    return _ffmpeg_exe_path
        except Exception:
            pass

    return "ffmpeg"

def convert_audio_to_24k_mono_wav(source_path, target_path=None):
    """Converts any audio file (m4a, mp3, ogg, wav) to clean 24kHz mono PCM WAV."""
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source audio file not found: {source_path}")

    if not target_path:
        base, _ = os.path.splitext(source_path)
        target_path = base + "_clean24k.wav"

    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    ffmpeg_exe = get_ffmpeg_path()
    cmd = [
        ffmpeg_exe,
        "-y",
        "-i", source_path,
        "-ar", "24000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        target_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 or not os.path.exists(target_path):
        raise RuntimeError(f"FFmpeg conversion failed: {res.stderr}")

    return target_path

def get_best_device():
    """Detects best available compute device: CUDA -> Apple Silicon MPS -> CPU."""
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"

def empty_device_cache():
    """Clears GPU cache across NVIDIA CUDA and Apple Silicon MPS."""
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        elif hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
            torch.mps.empty_cache()
    except Exception:
        pass

_gpu_info_cache = None

def get_gpu_info():
    global _gpu_info_cache
    if _gpu_info_cache is not None:
        return _gpu_info_cache
    try:
        import torch
        if torch.cuda.is_available():
            free_bytes, total_bytes = torch.cuda.mem_get_info()
            _gpu_info_cache = {
                "cuda_available": True,
                "mps_available": False,
                "device_name": torch.cuda.get_device_name(0),
                "vram_total_gb": round(total_bytes / (1024 ** 3), 2),
                "vram_free_gb": round(free_bytes / (1024 ** 3), 2),
                "vram_allocated_gb": round(torch.cuda.memory_allocated() / (1024 ** 3), 2)
            }
            return _gpu_info_cache
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device_name = "Apple Silicon (Metal MPS GPU)"
            try:
                if sys.platform == "darwin":
                    res = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    if res.returncode == 0 and res.stdout.strip():
                        device_name = res.stdout.strip() + " (Metal GPU)"
            except Exception:
                pass
            _gpu_info_cache = {
                "cuda_available": False,
                "mps_available": True,
                "device_name": device_name,
                "vram_total_gb": "Unified",
                "vram_free_gb": "Unified",
                "vram_allocated_gb": 0
            }
            return _gpu_info_cache
    except Exception:
        pass
    _gpu_info_cache = {
        "cuda_available": False,
        "mps_available": False,
        "device_name": "CPU Mode",
        "vram_total_gb": 0,
        "vram_free_gb": 0,
        "vram_allocated_gb": 0
    }
    return _gpu_info_cache

def get_f5_model():
    global _f5_instance
    with MODEL_LOCK:
        if _f5_instance is None:
            import torch
            from f5_tts.api import F5TTS
            device = get_best_device()
            print(f"[*] Initializing F5-TTS model on {device.upper()}...")
            if os.path.exists(MODELS_DIR):
                os.environ["HF_HOME"] = os.path.abspath(MODELS_DIR)
                os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                # Fast offline load from local models folder
                _f5_instance = F5TTS(device=device)
            except Exception as e:
                print(f"[!] Local F5-TTS load on {device} warning: {e}. Retrying with offline mode unset...")
                os.environ.pop("HF_HUB_OFFLINE", None)
                try:
                    _f5_instance = F5TTS(device=device)
                except Exception as e2:
                    if device == "mps":
                        print(f"[!] MPS initialization note: {e2}. Falling back to CPU...")
                        _f5_instance = F5TTS(device="cpu")
                    else:
                        raise e2
            print("[+] F5-TTS model initialized and warmed up!")
        return _f5_instance

def get_whisper_model():
    global _whisper_instance
    with MODEL_LOCK:
        if _whisper_instance is None:
            import torch
            from faster_whisper import WhisperModel
            if torch.cuda.is_available():
                device = "cuda"
                compute_type = "float16"
            else:
                # CPU mode on Windows and macOS (ARM NEON optimized)
                device = "cpu"
                compute_type = "int8"
            
            # Use local bundled faster-whisper model snapshot if available
            whisper_model_path = "tiny"
            local_hub = os.path.join(MODELS_DIR, "hub", "models--Systran--faster-whisper-tiny", "snapshots")
            if os.path.exists(local_hub):
                try:
                    snaps = [d for d in os.listdir(local_hub) if os.path.isdir(os.path.join(local_hub, d))]
                    if snaps:
                        whisper_model_path = os.path.join(local_hub, snaps[0])
                except Exception:
                    pass

            print(f"[*] Initializing WhisperModel ('{whisper_model_path}') on {device.upper()} ({compute_type})...")
            download_root = os.path.join(MODELS_DIR, "hub") if os.path.exists(MODELS_DIR) else None
            _whisper_instance = WhisperModel(whisper_model_path, device=device, compute_type=compute_type, download_root=download_root)
            print("[+] Whisper model ready!")
        return _whisper_instance

def unload_models():
    global _f5_instance, _whisper_instance
    with MODEL_LOCK:
        _f5_instance = None
        _whisper_instance = None
        empty_device_cache()
        print("[*] VRAM emptied and models unloaded.")

class VoiceCloneRequestHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json_response(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._send_cors_headers()
        self.end_headers()
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        global LAST_REQUEST_TIME
        LAST_REQUEST_TIME = time.time()

        if self.path in ["/", "/health", "/api/status"]:
            gpu = get_gpu_info()
            uptime = round(time.time() - START_TIME, 1)
            idle = round(time.time() - LAST_REQUEST_TIME, 1)
            self._send_json_response(200, {
                "status": "online",
                "service": "Easy Speech Voice Clone Expansion Pack",
                "version": "1.0.0",
                "uptime_seconds": uptime,
                "idle_seconds": idle,
                "model_loaded": _f5_instance is not None,
                "gpu": gpu
            })
        else:
            self._send_json_response(404, {"error": "Not Found"})

    def do_POST(self):
        global LAST_REQUEST_TIME
        LAST_REQUEST_TIME = time.time()

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        data = {}
        if post_data:
            try:
                data = json.loads(post_data.decode("utf-8"))
            except Exception as e:
                self._send_json_response(400, {"success": False, "error": f"Invalid JSON body: {str(e)}"})
                return

        path = self.path.rstrip("/")

        # 1. Audio Conversion Endpoint
        if path == "/api/convert":
            source_path = data.get("source_path")
            target_path = data.get("target_path")
            if not source_path:
                self._send_json_response(400, {"success": False, "error": "Missing source_path parameter"})
                return
            try:
                out_path = convert_audio_to_24k_mono_wav(source_path, target_path)
                self._send_json_response(200, {
                    "success": True,
                    "target_path": out_path.replace("\\", "/")
                })
            except Exception as e:
                self._send_json_response(500, {"success": False, "error": str(e)})

        # 2. Reference Audio Transcription Endpoint
        elif path == "/api/transcribe":
            audio_path = data.get("audio_path")
            if not audio_path or not os.path.exists(audio_path):
                self._send_json_response(400, {"success": False, "error": f"Audio file not found: {audio_path}"})
                return

            t0 = time.time()
            try:
                # Pre-convert to WAV if non-WAV container
                ext = os.path.splitext(audio_path)[1].lower()
                clean_audio = audio_path
                if ext != ".wav":
                    clean_audio = convert_audio_to_24k_mono_wav(audio_path)

                wm = get_whisper_model()
                segments, info = wm.transcribe(clean_audio, language="en")
                text = " ".join([seg.text.strip() for seg in segments if seg.text.strip()]).strip()
                elapsed = time.time() - t0

                self._send_json_response(200, {
                    "success": True,
                    "text": text,
                    "duration": round(info.duration, 2) if hasattr(info, "duration") else 0,
                    "transcribe_time": round(elapsed, 2),
                    "clean_audio_path": clean_audio.replace("\\", "/")
                })
            except Exception as e:
                import traceback
                self._send_json_response(500, {
                    "success": False,
                    "error": str(e),
                    "traceback": traceback.format_exc()
                })

        # 3. High-Speed Synthesis Endpoint
        elif path == "/api/generate":
            text = (data.get("text") or "").strip()
            ref_audio = data.get("ref_audio")
            ref_text = (data.get("ref_text") or "").strip()
            speed = float(data.get("speed", 1.0))
            output_path = data.get("output_path")
            nfe_step = int(data.get("nfe_step", 32))  # 32 steps: optimal fidelity / speed balance
            cfg_strength = float(data.get("cfg_strength", 2.5))

            if not text:
                self._send_json_response(400, {"success": False, "error": "Empty text provided for synthesis."})
                return
            if not ref_audio or not os.path.exists(ref_audio):
                self._send_json_response(400, {"success": False, "error": f"Reference audio not found: {ref_audio}"})
                return

            t0 = time.time()
            try:
                import torch
                import soundfile as sf

                # Ensure reference audio is 24kHz mono WAV
                ext = os.path.splitext(ref_audio)[1].lower()
                clean_ref_audio = ref_audio
                if ext != ".wav":
                    clean_ref_audio = convert_audio_to_24k_mono_wav(ref_audio)

                # Auto-transcribe if reference text was not provided
                if not ref_text:
                    wm = get_whisper_model()
                    segs, _ = wm.transcribe(clean_ref_audio)
                    ref_text = " ".join([s.text.strip() for s in segs]).strip()

                # Generate output path if not specified
                if not output_path:
                    ts = int(time.time() * 1000)
                    out_dir = os.path.dirname(os.path.abspath(clean_ref_audio))
                    output_path = os.path.join(out_dir, f"clone_gen_{ts}.wav")

                os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

                f5 = get_f5_model()

                with torch.inference_mode():
                    samples, sample_rate, _ = f5.infer(
                        ref_file=clean_ref_audio,
                        ref_text=ref_text,
                        gen_text=text,
                        speed=speed,
                        nfe_step=nfe_step,
                        cfg_strength=cfg_strength
                    )

                if samples is None or len(samples) == 0:
                    raise RuntimeError("Voice cloning returned empty audio samples.")

                sf.write(output_path, samples, sample_rate)
                duration = float(len(samples)) / float(sample_rate)
                elapsed = time.time() - t0

                # Release scratch tensors to maintain VRAM head-room for Premiere Pro / After Effects
                empty_device_cache()

                self._send_json_response(200, {
                    "success": True,
                    "filename": os.path.basename(output_path),
                    "absolute_path": os.path.abspath(output_path).replace("\\", "/"),
                    "duration": round(duration, 2),
                    "generation_time": round(elapsed, 2),
                    "ref_text": ref_text,
                    "engine": "f5_clone"
                })

            except Exception as e:
                import traceback
                self._send_json_response(500, {
                    "success": False,
                    "error": str(e),
                    "traceback": traceback.format_exc()
                })

        # 4. Unload / VRAM Cleanup Endpoint
        elif path == "/api/unload":
            unload_models()
            self._send_json_response(200, {"success": True, "message": "Models unloaded and VRAM cleared."})

        else:
            self._send_json_response(404, {"error": f"Unknown endpoint: {path}"})

def idle_vram_cleanup_worker():
    """Background monitor that clears GPU cache if inactive for 10 minutes."""
    while True:
        time.sleep(30)
        idle = time.time() - LAST_REQUEST_TIME
        if idle > 600 and _f5_instance is not None:
            empty_device_cache()

def main():
    print("=" * 65)
    print("      Easy Speech - Voice Clone Expansion Service")
    print("=" * 65)
    gpu = get_gpu_info()
    print(f"[*] Target Device:    {gpu['device_name']}")
    if gpu['cuda_available']:
        print(f"[*] Free VRAM:        {gpu['vram_free_gb']} GB / {gpu['vram_total_gb']} GB")
    elif gpu.get('mps_available'):
        print(f"[*] Metal Hardware:   Apple Silicon Unified Memory")
    else:
        print("[!] Note: CUDA/MPS not detected. Voice cloning running on CPU.")
    print(f"[*] Listening on:     http://{HOST}:{PORT}")
    print(f"[*] Health endpoint:  http://{HOST}:{PORT}/health")
    print("=" * 65)

    threading.Thread(target=idle_vram_cleanup_worker, daemon=True).start()

    server = ThreadingHTTPServer((HOST, PORT), VoiceCloneRequestHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down Voice Clone Service...")
        server.server_close()
        unload_models()

if __name__ == "__main__":
    main()
