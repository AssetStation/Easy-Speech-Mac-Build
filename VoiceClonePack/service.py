"""
Easy Speech - Voice Clone Expansion Service
High-Fidelity Offline Voice Cloning Background Daemon (F5-TTS & Faster-Whisper)
"""

import os
import sys
import time
import json
import threading
import subprocess
import hmac
import hashlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# Ensure PyTorch enables CPU fallback for unsupported MPS operators on Apple Silicon
os.environ["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"

# Add executable Scripts directory to PATH for local ffmpeg and tools
_exe_dir = os.path.dirname(sys.executable)
if _exe_dir not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _exe_dir + os.path.pathsep + os.environ.get("PATH", "")

# Safe stream handling for windowless execution (pythonw.exe / detached daemons)
if sys.stdout is None:
    try:
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

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
EXT_ROOT = os.path.dirname(BACKEND_DIR)
MODELS_DIR = os.path.join(EXT_ROOT, "VoiceClonePack", "models")
if not os.path.exists(MODELS_DIR):
    MODELS_DIR = os.path.join(EXT_ROOT, "models")
if os.path.exists(MODELS_DIR):
    os.environ["HF_HOME"] = os.path.abspath(MODELS_DIR)

CLONE_CACHE_DIR = os.path.join(EXT_ROOT, "cloned_profiles", ".cache")
try:
    os.makedirs(CLONE_CACHE_DIR, exist_ok=True)
except Exception:
    import tempfile
    CLONE_CACHE_DIR = os.path.join(tempfile.gettempdir(), "EasySpeech_cache")
    os.makedirs(CLONE_CACHE_DIR, exist_ok=True)

OUTPUT_DIR = os.path.join(EXT_ROOT, "audio_outputs")
try:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
except Exception:
    pass

# Global State
START_TIME = time.time()
LAST_HEARTBEAT_TIME = time.time()
LAST_SYNTHESIS_TIME = 0.0
MODEL_LOCK = threading.RLock()
PARENT_PID = None

# Active Processing State (guards against premature timeouts during long script synthesis)
ACTIVE_TASKS = 0
ACTIVE_TASKS_LOCK = threading.Lock()
_ACTIVE_OP = None

def try_acquire_operation(op_name):
    global _ACTIVE_OP
    with ACTIVE_TASKS_LOCK:
        if _ACTIVE_OP is not None:
            return False, _ACTIVE_OP
        _ACTIVE_OP = op_name
        return True, None

def release_operation(op_name):
    global _ACTIVE_OP
    with ACTIVE_TASKS_LOCK:
        if _ACTIVE_OP == op_name:
            _ACTIVE_OP = None

def start_active_task():
    global ACTIVE_TASKS, LAST_HEARTBEAT_TIME
    with ACTIVE_TASKS_LOCK:
        ACTIVE_TASKS += 1
        LAST_HEARTBEAT_TIME = time.time()

def finish_active_task():
    global ACTIVE_TASKS, LAST_SYNTHESIS_TIME, LAST_HEARTBEAT_TIME
    now = time.time()
    with ACTIVE_TASKS_LOCK:
        ACTIVE_TASKS = max(0, ACTIVE_TASKS - 1)
        LAST_SYNTHESIS_TIME = now
        LAST_HEARTBEAT_TIME = now

def is_currently_processing():
    with ACTIVE_TASKS_LOCK:
        return ACTIVE_TASKS > 0

_f5_instance = None
_whisper_instance = None
_ffmpeg_exe_path = None

def is_process_alive(pid):
    """Checks whether a given process ID is actively running on the host system."""
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, int(pid))
            if not h_process:
                return False
            exit_code = ctypes.c_ulong()
            success = kernel32.GetExitCodeProcess(h_process, ctypes.byref(exit_code))
            kernel32.CloseHandle(h_process)
            STILL_ACTIVE = 259
            return bool(success) and (exit_code.value == STILL_ACTIVE)
        except Exception:
            return False
    else:
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ProcessLookupError):
            return False

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

    # 2. Check explicit standard Mac locations (Apple Silicon & Intel)
    if sys.platform == "darwin":
        mac_paths = [
            "/opt/homebrew/bin/ffmpeg",  # Apple Silicon (M1/M2/M3/M4) Homebrew
            "/usr/local/bin/ffmpeg",      # Intel Mac Homebrew
            "/opt/local/bin/ffmpeg"       # MacPorts
        ]
        for mp in mac_paths:
            if os.path.exists(mp) and os.access(mp, os.X_OK):
                _ffmpeg_exe_path = mp
                return _ffmpeg_exe_path

    # 3. Check system PATH
    try:
        which_cmd = ["where", "ffmpeg"] if sys.platform == "win32" else ["which", "ffmpeg"]
        res = subprocess.run(which_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode == 0:
            lines = [l.strip() for l in res.stdout.strip().splitlines() if l.strip()]
            if lines and os.path.exists(lines[0]):
                _ffmpeg_exe_path = lines[0]
                return _ffmpeg_exe_path
    except Exception:
        pass

    return "ffmpeg"

def get_clean_24k_cache_path(source_path):
    """Returns a deterministic safe cache path inside the internal plugin cache directory."""
    try:
        norm = os.path.normpath(source_path)
        base_name = os.path.splitext(os.path.basename(source_path))[0]
        import re
        safe_base = re.sub(r'[^a-zA-Z0-9_\-]', '_', base_name)[:30].strip('_') or "audio"
        mtime = os.path.getmtime(source_path) if os.path.exists(source_path) else 0
        h = hashlib.md5(f"{norm}:{mtime}".encode("utf-8")).hexdigest()[:10]
        return os.path.join(CLONE_CACHE_DIR, f"{safe_base}_{h}_clean24k.wav")
    except Exception:
        h = hashlib.md5(str(source_path).encode("utf-8")).hexdigest()[:12]
        return os.path.join(CLONE_CACHE_DIR, f"ref_{h}_clean24k.wav")

def get_optref_cache_path(clean_audio_path):
    """Returns safe cache path for trimmed reference audio inside internal plugin cache directory."""
    base_name = os.path.splitext(os.path.basename(clean_audio_path))[0]
    return os.path.join(CLONE_CACHE_DIR, f"{base_name}_optref.wav")

def convert_audio_to_24k_mono_wav(source_path, target_path=None):
    """Converts any audio file to clean 24kHz mono PCM WAV inside the plugin internal cache directory."""
    init_environment()
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source audio file not found: {source_path}")

    if not target_path:
        target_path = get_clean_24k_cache_path(source_path)

    # Reuse cached converted file if it already exists
    if os.path.exists(target_path) and os.path.getsize(target_path) > 1024:
        return target_path

    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    ffmpeg_exe = get_ffmpeg_path()
    cmd = [
        ffmpeg_exe,
        "-y",
        "-i", source_path,
        "-vn",
        "-ar", "24000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        target_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 or not os.path.exists(target_path):
        raise RuntimeError(f"FFmpeg conversion failed: {res.stderr}")

    return target_path

_env_initialized = False
_env_lock = threading.Lock()

def init_environment():
    """Initializes portable ffmpeg, pydub, and CUDA DLL paths in background or on first use."""
    global _env_initialized, _ffmpeg_exe_path
    with _env_lock:
        if _env_initialized:
            return
        try:
            import imageio_ffmpeg
            _ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
            if _ffmpeg_bin and os.path.exists(_ffmpeg_bin):
                _ffmpeg_exe_path = _ffmpeg_bin
                _ff_dir = os.path.dirname(_ffmpeg_bin)
                if _ff_dir not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = _ff_dir + os.path.pathsep + os.environ.get("PATH", "")
                import warnings
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", RuntimeWarning)
                    import pydub
                    pydub.AudioSegment.converter = _ffmpeg_bin
        except Exception:
            pass

        try:
            import torch
            torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
            if torch_lib not in os.environ.get("PATH", ""):
                os.environ["PATH"] = torch_lib + os.path.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory") and os.path.exists(torch_lib):
                os.add_dll_directory(torch_lib)
        except Exception:
            pass

        get_gpu_info()
        _env_initialized = True

_gpu_static_cache = None

def get_gpu_info(allow_block=True):
    global _gpu_static_cache
    if _gpu_static_cache is not None:
        try:
            import torch
            if torch.cuda.is_available():
                free_bytes, total_bytes = torch.cuda.mem_get_info()
                _gpu_static_cache["vram_free_gb"] = round(free_bytes / (1024 ** 3), 2)
                _gpu_static_cache["vram_allocated_gb"] = round(torch.cuda.memory_allocated() / (1024 ** 3), 2)
        except Exception:
            pass
        return _gpu_static_cache

    if not allow_block:
        return {
            "cuda_available": False,
            "is_supported": False,
            "device_name": "Initializing GPU Mode...",
            "vram_total_gb": 0,
            "vram_free_gb": 0,
            "vram_allocated_gb": 0
        }

    try:
        import torch
        if torch.cuda.is_available():
            total_bytes = torch.cuda.get_device_properties(0).total_memory
            total_gb = round(total_bytes / (1024 ** 3), 2)
            try:
                free_bytes, _ = torch.cuda.mem_get_info()
                free_gb = round(free_bytes / (1024 ** 3), 2)
            except Exception:
                free_gb = total_gb
            _gpu_static_cache = {
                "cuda_available": True,
                "is_supported": total_gb >= 3.5,
                "device_name": torch.cuda.get_device_name(0),
                "vram_total_gb": total_gb,
                "vram_free_gb": free_gb,
                "vram_allocated_gb": round(torch.cuda.memory_allocated() / (1024 ** 3), 2)
            }
            return _gpu_static_cache
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            import platform
            dev_name = platform.processor() or "Apple Silicon"
            _gpu_static_cache = {
                "cuda_available": False,
                "mps_available": True,
                "is_supported": True,
                "device_name": f"{dev_name} (Metal GPU)",
                "vram_total_gb": "Unified",
                "vram_free_gb": "Dynamic",
                "vram_allocated_gb": 0
            }
            return _gpu_static_cache
    except Exception:
        pass

    return {
        "cuda_available": False,
        "is_supported": False,
        "device_name": "Initializing GPU Mode...",
        "vram_total_gb": 0,
        "vram_free_gb": 0,
        "vram_allocated_gb": 0
    }

def find_local_f5_paths():
    """Locate bundled F5-TTS checkpoint and Vocos vocoder snapshots locally."""
    ckpt_path = None
    vocoder_path = None

    # Search for F5-TTS checkpoint
    f5_hub = os.path.join(MODELS_DIR, "hub", "models--SWivid--F5-TTS", "snapshots")
    if os.path.exists(f5_hub):
        for snap in os.listdir(f5_hub):
            cand = os.path.join(f5_hub, snap, "F5TTS_v1_Base", "model_1250000.safetensors")
            if os.path.exists(cand):
                ckpt_path = cand
                break
    if not ckpt_path:
        direct_cand = os.path.join(MODELS_DIR, "F5TTS_v1_Base", "model_1250000.safetensors")
        if os.path.exists(direct_cand):
            ckpt_path = direct_cand

    # Search for Vocos vocoder
    vocos_hub = os.path.join(MODELS_DIR, "hub", "models--charactr--vocos-mel-24khz", "snapshots")
    if os.path.exists(vocos_hub):
        for snap in os.listdir(vocos_hub):
            cand = os.path.join(vocos_hub, snap)
            if os.path.exists(os.path.join(cand, "config.yaml")) and os.path.exists(os.path.join(cand, "pytorch_model.bin")):
                vocoder_path = cand
                break
    if not vocoder_path:
        direct_voc = os.path.join(MODELS_DIR, "vocos-mel-24khz")
        if os.path.exists(os.path.join(direct_voc, "config.yaml")):
            vocoder_path = direct_voc

    return ckpt_path, vocoder_path

def get_f5_model():
    global _f5_instance
    init_environment()
    with MODEL_LOCK:
        if _f5_instance is None:
            import torch
            from f5_tts.api import F5TTS
            if torch.cuda.is_available():
                device = "cuda"
            elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
                device = "mps"
            else:
                device = "cpu"
            print(f"[*] Initializing F5-TTS model on {device.upper()}...")
            
            if os.path.exists(MODELS_DIR):
                os.environ["HF_HOME"] = os.path.abspath(MODELS_DIR)

            ckpt_path, vocoder_path = find_local_f5_paths()
            if ckpt_path and vocoder_path:
                # Enforce strict offline execution once bundled snapshots are verified
                os.environ["HF_HUB_OFFLINE"] = "1"
                os.environ["TRANSFORMERS_OFFLINE"] = "1"
                print(f"[+] Using local offline F5 checkpoint: {ckpt_path}")
                print(f"[+] Using local offline Vocos vocoder: {vocoder_path}")
                try:
                    _f5_instance = F5TTS(ckpt_file=ckpt_path, vocoder_local_path=vocoder_path, device=device)
                except Exception as init_err:
                    if device == "mps":
                        print(f"[!] MPS initialization error ({init_err}). Falling back to CPU...")
                        device = "cpu"
                        _f5_instance = F5TTS(ckpt_file=ckpt_path, vocoder_local_path=vocoder_path, device="cpu")
                    else:
                        raise init_err
            else:
                print("[!] Local bundled snapshot paths not found directly. Loading via HF_HOME...")
                try:
                    _f5_instance = F5TTS(device=device)
                except Exception as init_err:
                    if device == "mps":
                        print(f"[!] MPS initialization error ({init_err}). Falling back to CPU...")
                        device = "cpu"
                        try:
                            _f5_instance = F5TTS(device="cpu")
                        except Exception as cpu_err:
                            err_msg = str(cpu_err).lower()
                            if any(k in err_msg for k in ["connection", "offline", "resolution", "getaddrinfo", "timeout", "timed out", "unreachable"]):
                                raise RuntimeError("Neural models not found locally and internet connection is unavailable. Please connect to the internet to download AI model weights.") from cpu_err
                            raise cpu_err
                    else:
                        err_msg = str(init_err).lower()
                        if any(k in err_msg for k in ["connection", "offline", "resolution", "getaddrinfo", "timeout", "timed out", "unreachable"]):
                            raise RuntimeError("Neural models not found locally and internet connection is unavailable. Please connect to the internet to download AI model weights.") from init_err
                        raise init_err

            print("[+] F5-TTS model initialized and warmed up!")
        return _f5_instance

_is_warming_up = False

def warmup_model_async():
    """Proactively warms up F5-TTS model in a non-blocking background thread."""
    global _is_warming_up
    with MODEL_LOCK:
        if _f5_instance is not None or _is_warming_up:
            return
        _is_warming_up = True

    def _worker():
        global _is_warming_up
        try:
            init_environment()
            print("[*] Proactively warming up AI Model (F5-TTS) in background...", flush=True)
            get_f5_model()
            print("[+] AI Model warmup finished successfully!", flush=True)
        except Exception as e:
            print(f"[!] AI Model warmup error: {e}", flush=True)
        finally:
            with MODEL_LOCK:
                _is_warming_up = False

    t = threading.Thread(target=_worker, daemon=True)
    t.start()

def get_whisper_model():
    global _whisper_instance
    init_environment()
    with MODEL_LOCK:
        if _whisper_instance is None:
            import torch
            # Ensure CTranslate2 can locate cublas64_12.dll and cudnn
            torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
            if torch_lib not in os.environ.get("PATH", ""):
                os.environ["PATH"] = torch_lib + os.path.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory") and os.path.exists(torch_lib):
                os.add_dll_directory(torch_lib)

            from faster_whisper import WhisperModel
            if torch.cuda.is_available():
                device = "cuda"
                compute_type = "float16"
            else:
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
            try:
                _whisper_instance = WhisperModel(whisper_model_path, device=device, compute_type=compute_type, download_root=download_root)
            except Exception as wm_err:
                err_msg = str(wm_err).lower()
                if any(k in err_msg for k in ["connection", "offline", "resolution", "getaddrinfo", "timeout", "timed out", "unreachable"]):
                    raise RuntimeError("Whisper speech model not found locally and internet connection is unavailable. Please connect to the internet to download recognition weights.") from wm_err
                raise wm_err
            print("[+] Whisper model ready!")
        return _whisper_instance

def unload_models():
    global _f5_instance, _whisper_instance
    with MODEL_LOCK:
        has_loaded = (_f5_instance is not None or _whisper_instance is not None)
        _f5_instance = None
        _whisper_instance = None
        try:
            import gc
            gc.collect()
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                if hasattr(torch.cuda, "ipc_collect"):
                    torch.cuda.ipc_collect()
            elif hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
                try:
                    torch.mps.empty_cache()
                except Exception:
                    pass
        except Exception:
            pass
        if has_loaded:
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
        global LAST_HEARTBEAT_TIME
        LAST_HEARTBEAT_TIME = time.time()

        raw_path = self.path
        clean_path = raw_path.split("?")[0].rstrip("/")

        if raw_path in ["/", "/health", "/api/status", "/api/ping", "/ping"] or clean_path in ["", "/health", "/api/status", "/api/ping", "/ping"]:
            gpu = get_gpu_info(allow_block=False)
            uptime = round(time.time() - START_TIME, 1)
            idle = round(time.time() - LAST_HEARTBEAT_TIME, 1)
            since_synth = round(time.time() - LAST_SYNTHESIS_TIME, 1) if LAST_SYNTHESIS_TIME > 0 else None
            self._send_json_response(200, {
                "status": "online",
                "service": "Easy Speech Voice Clone Expansion Pack",
                "version": "1.1.0",
                "uptime_seconds": uptime,
                "idle_seconds": idle,
                "seconds_since_synthesis": since_synth,
                "model_loaded": _f5_instance is not None,
                "is_warming_up": _is_warming_up,
                "gpu": gpu
            })
        elif clean_path in ["/api/warmup", "/warmup"]:
            warmup_model_async()
            self._send_json_response(200, {
                "success": True,
                "message": "AI Model warmup initiated.",
                "model_loaded": _f5_instance is not None,
                "is_warming_up": _is_warming_up
            })
        else:
            self._send_json_response(404, {"error": "Not Found"})

    def do_POST(self):
        global LAST_HEARTBEAT_TIME, LAST_SYNTHESIS_TIME, _whisper_instance
        LAST_HEARTBEAT_TIME = time.time()

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        data = {}
        if post_data:
            try:
                data = json.loads(post_data.decode("utf-8-sig"))
            except Exception as e:
                self._send_json_response(400, {"success": False, "error": f"Invalid JSON body: {str(e)}"})
                return

        path = self.path.split("?")[0].rstrip("/")

        # 0. Heartbeat / Ping Endpoint
        if path in ["/api/ping", "/ping"]:
            self._send_json_response(200, {"success": True, "pong": True, "time": time.time()})
            return

        # 0b. Proactive AI Model Warmup Endpoint
        if path in ["/api/warmup", "/warmup"]:
            warmup_model_async()
            self._send_json_response(200, {
                "success": True,
                "message": "AI Model warmup initiated.",
                "model_loaded": _f5_instance is not None,
                "is_warming_up": _is_warming_up
            })
            return

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

        # 2. Reference Audio Transcription Endpoint (Protected by MODEL_LOCK)
        elif path == "/api/transcribe":
            audio_path = data.get("audio_path")
            if not audio_path or not os.path.exists(audio_path):
                self._send_json_response(400, {"success": False, "error": f"Audio file not found: {audio_path}"})
            ok, current_op = try_acquire_operation("transcribe")
            if not ok:
                self._send_json_response(409, {
                    "success": False,
                    "error": f"AI model is currently busy with audio {current_op}. Please wait a moment."
                })
                return

            start_active_task()
            t0 = time.time()
            try:
                # Pre-convert to WAV if non-WAV container or non-standard sample rate/channels
                ext = os.path.splitext(audio_path)[1].lower()
                clean_audio = audio_path
                needs_convert = (ext != ".wav")
                if not needs_convert:
                    try:
                        import soundfile as _sf
                        _info = _sf.info(audio_path)
                        if _info.samplerate != 24000 or _info.channels != 1:
                            needs_convert = True
                    except Exception:
                        needs_convert = True

                if needs_convert:
                    clean_audio = convert_audio_to_24k_mono_wav(audio_path)

                with MODEL_LOCK:
                    wm = get_whisper_model()
                    segments, info = wm.transcribe(clean_audio, language="en")
                    seg_list = [s for s in segments if s.text.strip()]
                    total_dur = getattr(info, "duration", 0)

                    # Guard against F5-TTS 12s ceiling: if reference audio exceeds 9.5s,
                    # F5-TTS truncates audio but leaves ref_text dangling, causing word bleed.
                    # Automatically select the best complete sentence segment within 3.5s - 9.5s.
                    if total_dur > 9.5 and seg_list:
                        selected_segs = []
                        for s in seg_list:
                            if s.end <= 9.5:
                                selected_segs.append(s)
                            elif not selected_segs and s.start < 9.5:
                                selected_segs.append(s)
                                break
                            else:
                                break
                        if selected_segs and selected_segs[-1].end < total_dur:
                            cutoff_sec = min(selected_segs[-1].end + 0.15, total_dur)
                            import soundfile as sf
                            a_data, a_sr = sf.read(clean_audio)
                            trimmed_data = a_data[:int(cutoff_sec * a_sr)]
                            opt_path = get_optref_cache_path(clean_audio)
                            sf.write(opt_path, trimmed_data, a_sr)
                            clean_audio = opt_path
                            text = " ".join([s.text.strip() for s in selected_segs]).strip()
                            total_dur = cutoff_sec
                        else:
                            text = " ".join([seg.text.strip() for seg in seg_list]).strip()
                    else:
                        text = " ".join([seg.text.strip() for seg in seg_list]).strip()

                    # Immediately release Whisper from VRAM to preserve memory for synthesis
                    _whisper_instance = None
                    try:
                        import gc
                        gc.collect()
                        import torch
                        if torch.cuda.is_available():
                            torch.cuda.empty_cache()
                    except Exception:
                        pass
                elapsed = time.time() - t0

                self._send_json_response(200, {
                    "success": True,
                    "text": text,
                    "duration": round(total_dur, 2),
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
            finally:
                release_operation("transcribe")
                finish_active_task()

        # 3. High-Speed Synthesis Endpoint (Protected by MODEL_LOCK with OOM Recovery)
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
            ok, current_op = try_acquire_operation("generate")
            if not ok:
                self._send_json_response(409, {
                    "success": False,
                    "error": f"AI model is currently busy with audio {current_op}. Please wait a moment."
                })
                return

            start_active_task()
            t0 = time.time()
            try:
                import torch
                import soundfile as sf
                import re

                # Ensure reference audio is clean 24kHz mono WAV
                ext = os.path.splitext(ref_audio)[1].lower()
                clean_ref_audio = ref_audio
                needs_convert = (ext != ".wav")
                if not needs_convert:
                    try:
                        _info = sf.info(ref_audio)
                        if _info.samplerate != 24000 or _info.channels != 1:
                            needs_convert = True
                    except Exception:
                        needs_convert = True

                if needs_convert:
                    clean_ref_audio = convert_audio_to_24k_mono_wav(ref_audio)

                # Duration guard: F5-TTS silently chops reference audio > 12s, causing leftover
                # reference text words to bleed into synthesis. If ref audio exceeds 10s, use
                # pre-trimmed _optref.wav or extract the first complete sentence within <= 9.0s.
                try:
                    ref_info = sf.info(clean_ref_audio)
                    if ref_info.duration > 10.0:
                        opt_path = get_optref_cache_path(clean_ref_audio)
                        if os.path.exists(opt_path):
                            clean_ref_audio = opt_path
                        else:
                            sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+', ref_text) if s.strip()]
                            if len(sentences) > 1:
                                with MODEL_LOCK:
                                    wm = get_whisper_model()
                                    segs, _ = wm.transcribe(clean_ref_audio, language="en")
                                    s_list = [s for s in segs if s.text.strip()]
                                    _whisper_instance = None
                                if s_list:
                                    cutoff = min(s_list[0].end + 0.15, 9.0)
                                    a_data, a_sr = sf.read(clean_ref_audio)
                                    trimmed_data = a_data[:int(cutoff * a_sr)]
                                    sf.write(opt_path, trimmed_data, a_sr)
                                    clean_ref_audio = opt_path
                                    ref_text = s_list[0].text.strip()
                except Exception as dur_guard_err:
                    print(f"[!] Reference duration guard note: {dur_guard_err}")

                # Auto-transcribe if reference text was not provided
                if not ref_text:
                    with MODEL_LOCK:
                        wm = get_whisper_model()
                        segs, _ = wm.transcribe(clean_ref_audio)
                        ref_text = " ".join([s.text.strip() for s in segs]).strip()
                        # Release whisper instance immediately
                        _whisper_instance = None
                        try:
                            import gc
                            gc.collect()
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()
                        except Exception:
                            pass

                # Generate output path if not specified
                if not output_path:
                    ts = int(time.time() * 1000)
                    output_path = os.path.join(OUTPUT_DIR, f"clone_gen_{ts}.wav")

                os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

                with MODEL_LOCK:
                    f5 = get_f5_model()
                    try:
                        with torch.inference_mode():
                            samples, sample_rate, _ = f5.infer(
                                ref_file=clean_ref_audio,
                                ref_text=ref_text,
                                gen_text=text,
                                speed=speed,
                                nfe_step=nfe_step,
                                cfg_strength=cfg_strength
                            )
                    except Exception as infer_err:
                        err_str = str(infer_err).lower()
                        is_oom = "out of memory" in err_str or "cuda error: out of memory" in err_str or "mps backend out of memory" in err_str or isinstance(infer_err, MemoryError)
                        if is_oom or (hasattr(torch.cuda, "OutOfMemoryError") and isinstance(infer_err, torch.cuda.OutOfMemoryError)):
                            try:
                                if torch.cuda.is_available():
                                    torch.cuda.empty_cache()
                                elif hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
                                    torch.mps.empty_cache()
                            except Exception:
                                pass
                            self._send_json_response(422, {
                                "success": False,
                                "is_oom": True,
                                "error": "Insufficient free memory (RAM / VRAM). Close background applications or pause Premiere Pro playback and try again."
                            })
                            return
                        raise infer_err

                if samples is None or len(samples) == 0:
                    raise RuntimeError("Voice cloning returned empty audio samples.")

                # Studio Peak Normalization: prevent digital clipping/distortion when saving to PCM WAV
                import numpy as np
                max_amp = float(np.max(np.abs(samples)))
                if max_amp > 0.95:
                    samples = samples * (0.95 / max_amp)

                sf.write(output_path, samples, sample_rate)
                duration = float(len(samples)) / float(sample_rate)
                elapsed = time.time() - t0

                # Release scratch tensors to maintain VRAM head-room for Premiere Pro
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                elif hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
                    try:
                        torch.mps.empty_cache()
                    except Exception:
                        pass

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
            finally:
                release_operation("generate")
                finish_active_task()

        # 4. Unload / VRAM Cleanup Endpoint
        elif path == "/api/unload":
            unload_models()
            self._send_json_response(200, {"success": True, "message": "Models unloaded and VRAM cleared."})

        # 5. Clean Shutdown Endpoint
        elif path == "/api/shutdown":
            self._send_json_response(200, {"success": True, "message": "Voice Clone Daemon shutting down."})
            def _delayed_exit():
                time.sleep(0.3)
                unload_models()
                os._exit(0)
            threading.Thread(target=_delayed_exit, daemon=True).start()

        else:
            self._send_json_response(404, {"error": f"Unknown endpoint: {path}"})

def parent_watchdog_worker():
    """Monitors parent CEP process; if parent closes, crashes or is killed, exit immediately."""
    global PARENT_PID
    if not PARENT_PID:
        return
    consecutive_dead_count = 0
    while True:
        time.sleep(2)
        if not is_process_alive(PARENT_PID):
            consecutive_dead_count += 1
            if consecutive_dead_count >= 3:  # 6s verified death (prevents premature exit during 100% CPU spikes)
                print(f"[*] Parent process {PARENT_PID} terminated. Shutting down Voice Clone daemon to release VRAM...")
                unload_models()
                os._exit(0)
        else:
            consecutive_dead_count = 0

def idle_vram_cleanup_worker():
    """
    1. VRAM Auto-Release: Unloads F5-TTS / Whisper after 90s of post-synthesis idle.
       Guaranteed NEVER to unload or close while a long script is actively synthesizing!
    2. Orphan Process Watchdog: If no parent PID is tracked and heartbeat is lost for 300s,
       cleanly shuts down. If parent PID is tracked, parent_watchdog_worker handles lifecycle.
    """
    while True:
        time.sleep(5)
        now = time.time()

        # NEVER unload or timeout while active synthesis or transcription is processing!
        if is_currently_processing():
            continue

        # 1. Post-Synthesis VRAM Auto-Release (90 seconds idle after work completely finishes)
        if _f5_instance is not None or _whisper_instance is not None:
            if LAST_SYNTHESIS_TIME > 0 and (now - LAST_SYNTHESIS_TIME) > 90:
                print("[*] Post-synthesis idle timeout (90s) reached. Releasing VRAM for Adobe host...")
                unload_models()

        # 2. Heartbeat Orphan Watchdog (only applies if daemon was run without a parent PID)
        if not PARENT_PID:
            if (now - LAST_HEARTBEAT_TIME) > 300:
                print("[*] Heartbeat lost (300s inactivity without parent PID). Shutting down Voice Clone daemon...")
                unload_models()
                os._exit(0)

def main():
    global PARENT_PID
    for i, arg in enumerate(sys.argv):
        if arg == "--parent-pid" and i + 1 < len(sys.argv):
            try:
                PARENT_PID = int(sys.argv[i + 1])
            except ValueError:
                pass

    print("=" * 65)
    print("      Easy Speech - Voice Clone Expansion Service")
    print("=" * 65)
    print(f"[*] Listening on:     http://{HOST}:{PORT}")
    print(f"[*] Health endpoint:  http://{HOST}:{PORT}/health")
    if PARENT_PID:
        print(f"[*] Guarding Parent:  PID {PARENT_PID}")
    print("=" * 65)

    try:
        server = ThreadingHTTPServer((HOST, PORT), VoiceCloneRequestHandler)
    except OSError as e:
        if getattr(e, 'errno', None) in (98, 10048) or "address already in use" in str(e).lower():
            print(f"[*] Voice Clone Service is ALREADY running on http://{HOST}:{PORT}")
            sys.exit(0)
        raise

    threading.Thread(target=init_environment, daemon=True).start()
    threading.Thread(target=idle_vram_cleanup_worker, daemon=True).start()
    if PARENT_PID:
        threading.Thread(target=parent_watchdog_worker, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down Voice Clone Service...")
        server.server_close()
        unload_models()

if __name__ == "__main__":
    main()
