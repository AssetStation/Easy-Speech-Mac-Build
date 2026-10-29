"""
Easy Speech Pro - Voice Clone Expansion Service
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
import errno
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
        if sys.platform == "win32":
            base_log = os.environ.get('LOCALAPPDATA', os.path.dirname(os.path.abspath(__file__)))
            log_dir = os.path.join(base_log, 'EasySpeech', 'logs')
        else:
            base_log = os.path.join(os.environ.get('HOME', os.path.dirname(os.path.abspath(__file__))), '.easyspeech')
            log_dir = os.path.join(base_log, 'logs')
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
_current_loaded_lang = None
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
        h = hashlib.md5(f"v3:{norm}:{mtime}".encode("utf-8")).hexdigest()[:10]
        return os.path.join(CLONE_CACHE_DIR, f"{safe_base}_{h}_clean24k.wav")
    except Exception:
        h = hashlib.md5(f"v3:{source_path}".encode("utf-8")).hexdigest()[:12]
        return os.path.join(CLONE_CACHE_DIR, f"ref_{h}_clean24k.wav")

def get_optref_cache_path(clean_audio_path):
    """Returns safe cache path for trimmed reference audio inside internal plugin cache directory."""
    base_name = os.path.splitext(os.path.basename(clean_audio_path))[0]
    return os.path.join(CLONE_CACHE_DIR, f"{base_name}_optref.wav")

def validate_safe_audio_output_path(candidate_path, default_dir=None):
    """
    Security Guard: Ensures output/target paths written by the daemon are strictly .wav files
    inside authorized plugin or user audio directories, preventing path traversal or arbitrary file overwrite.
    """
    if not default_dir:
        default_dir = OUTPUT_DIR
    if not candidate_path:
        ts = int(time.time() * 1000)
        return os.path.join(default_dir, f"clone_gen_{ts}.wav")

    norm_cand = os.path.abspath(os.path.normpath(str(candidate_path)))
    if not norm_cand.lower().endswith(".wav"):
        raise ValueError("Invalid output file extension. Only .wav audio files are permitted.")

    import tempfile
    allowed_roots = [
        os.path.abspath(EXT_ROOT),
        os.path.abspath(OUTPUT_DIR),
        os.path.abspath(CLONE_CACHE_DIR),
        os.path.abspath(tempfile.gettempdir())
    ]
    for env_var in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME"):
        val = os.environ.get(env_var)
        if val:
            allowed_roots.append(os.path.abspath(os.path.join(val, "EasySpeech")))
            allowed_roots.append(os.path.abspath(os.path.join(val, "Adobe", "CEP", "extensions", "EasySpeech")))
            allowed_roots.append(os.path.abspath(os.path.join(val, "Adobe", "CEP", "extensions", "com.easyspeech.pro")))
            allowed_roots.append(os.path.abspath(os.path.join(val, "Library", "Application Support", "EasySpeech")))
            allowed_roots.append(os.path.abspath(os.path.join(val, "Library", "Application Support", "Adobe", "CEP", "extensions", "com.easyspeech.pro")))

    is_allowed = False
    norm_cand_case = os.path.normcase(norm_cand)
    for root in allowed_roots:
        root_case = os.path.normcase(root)
        if norm_cand_case.startswith(root_case + os.sep) or norm_cand_case == root_case:
            is_allowed = True
            break

    if not is_allowed:
        raise ValueError("Output path is outside authorized Easy Speech Pro directories.")

    return norm_cand

def condition_reference_wav(wav_path, trim_over_9_5s=True):
    """
    Studio Reference Audio Conditioner:
    1. Removes DC offset.
    2. Validates minimum duration (>= 1.2s) and non-silent signal (SNR / RMS check).
    3. Trims dead leading/trailing silence (< -45 dBFS).
    4. If duration > 9.5s, smart-trims at the cleanest natural pause between 4.5s and 9.2s.
    5. Applies 25ms fade-in and 35ms fade-out + 160ms trailing silence pad to prevent first-word clipping.
    6. Normalizes active speech RMS to -18.5 dBFS (0.119) with soft-knee limiting at -1.0 dBFS (0.891).
    """
    import numpy as np
    import soundfile as sf

    data, sr = sf.read(wav_path, dtype="float32")
    if data.ndim > 1:
        data = np.mean(data, axis=1)

    if len(data) < int(1.2 * sr):
        raise ValueError("Reference audio is too short (minimum 1.5 seconds of clear speech required).")

    # 1. Remove DC offset
    data = data - float(np.mean(data))

    peak_raw = float(np.max(np.abs(data)))
    rms_raw = float(np.sqrt(np.mean(np.square(data))))
    if peak_raw < 1e-3 or rms_raw < 2e-4:
        raise ValueError("Reference audio appears to be silent or muted. Please select a clear voice clip.")

    # 2. Trim leading and trailing dead silence (< -45 dBFS relative to peak)
    win_len = max(1, int(0.02 * sr))  # 20ms frame
    thresh = max(10 ** (-45.0 / 20.0), peak_raw * 0.015)
    num_frames = len(data) // win_len
    if num_frames > 4:
        frames = data[:num_frames * win_len].reshape(num_frames, win_len)
        frame_rms = np.sqrt(np.mean(np.square(frames), axis=1))
        active_idx = np.where(frame_rms > thresh)[0]
        if len(active_idx) > 0:
            lead_pad = int(0.05 * sr)
            tail_pad = int(0.06 * sr)
            start_sample = max(0, int(active_idx[0] * win_len) - lead_pad)
            end_sample = min(len(data), int((active_idx[-1] + 1) * win_len) + tail_pad)
            if (end_sample - start_sample) >= int(1.2 * sr):
                data = data[start_sample:end_sample]

    # 3. Smart pause-boundary trimmer if reference audio exceeds 9.0s
    if trim_over_9_5s and len(data) > int(9.0 * sr):
        search_start = int(4.5 * sr)
        search_end = min(len(data), int(9.2 * sr))
        pause_win = int(0.12 * sr)  # 120ms pause window
        step = int(0.03 * sr)       # 30ms step
        best_cut = int(8.8 * sr)
        min_energy = float("inf")
        for pos in range(search_start, max(search_start + step, search_end - pause_win), step):
            seg = data[pos:pos + pause_win]
            e = float(np.mean(np.square(seg)))
            # Slightly prefer pauses between 6.0s and 9.0s for rich voice timbre
            weight = 0.85 if pos >= int(6.0 * sr) else 1.0
            if e * weight < min_energy:
                min_energy = e * weight
                best_cut = pos + (pause_win // 2)
        data = data[:best_cut]

    # 3b. Abrupt Mid-Speech Cut Guard:
    # If the user's clip was cut mid-word or mid-syllable, the trailing samples will have high
    # active vocal energy without natural acoustic decay. In F5-TTS, this severed phoneme causes
    # the diffusion reverse process to click, pop, or hallucinate the severed syllable into generated speech.
    # We inspect the trailing 120ms; if severed, we scan backwards up to 1.8s for the nearest natural
    # inter-word acoustic valley and cleanly trim there.
    tail_eval_len = min(int(0.12 * sr), len(data))
    if tail_eval_len > 0:
        tail_rms = float(np.sqrt(np.mean(np.square(data[-tail_eval_len:]))))
        if peak_raw > 0.05 and tail_rms > (peak_raw * 0.22) and len(data) >= int(2.8 * sr):
            back_start = len(data) - int(0.20 * sr)
            back_limit = max(int(1.5 * sr), len(data) - int(1.8 * sr))
            p_win = int(0.10 * sr)   # 100ms pause window
            p_step = int(0.025 * sr) # 25ms step
            best_pause = None
            min_p_e = float("inf")
            for pos in range(back_start, back_limit, -p_step):
                seg = data[pos - p_win:pos]
                e = float(np.mean(np.square(seg)))
                if e < min_p_e:
                    min_p_e = e
                    best_pause = pos - (p_win // 2)
            # If an inter-word gap significantly quieter than the severed voice tail is found:
            if best_pause is not None and min_p_e < (tail_rms ** 2) * 0.40:
                data = data[:best_pause]

    # 4. Smooth speech-edge fades (25ms fade-in, 35ms fade-out) to eliminate mid-vowel cut clicks
    fade_in_len = min(int(0.025 * sr), len(data) // 4)
    fade_out_len = min(int(0.035 * sr), len(data) // 4)
    if fade_in_len > 0:
        data[:fade_in_len] *= np.linspace(0.0, 1.0, fade_in_len, dtype=np.float32)
    if fade_out_len > 0:
        data[-fade_out_len:] *= np.linspace(1.0, 0.0, fade_out_len, dtype=np.float32)

    # 5. Normalize active speech RMS to -18.5 dBFS (0.119) so F5-TTS never attenuates output
    frame_sz = max(1, int(0.05 * sr))
    n_f = len(data) // frame_sz
    if n_f > 0:
        f_mat = data[:n_f * frame_sz].reshape(n_f, frame_sz)
        f_rms = np.sqrt(np.mean(np.square(f_mat), axis=1))
        speech_frames = f_rms[f_rms > 0.008]
        active_rms = float(np.sqrt(np.mean(np.square(speech_frames)))) if len(speech_frames) > 0 else float(np.sqrt(np.mean(np.square(data))))
    else:
        active_rms = float(np.sqrt(np.mean(np.square(data))))

    target_ref_rms = 0.12  # ~ -18.4 dBFS (above F5-TTS 0.1 floor)
    if active_rms > 1e-5:
        gain = min(10.0, max(0.25, target_ref_rms / active_rms))
        data = data * gain

    # Soft-knee true peak limiter at 0.891 (-1.0 dBFS)
    knee = 0.72
    limit = 0.891
    abs_d = np.abs(data)
    over = abs_d > knee
    if np.any(over):
        headroom = limit - knee
        sign = np.sign(data[over])
        data[over] = sign * (knee + headroom * np.tanh((abs_d[over] - knee) / headroom))

    # 6. Append 50ms of clean trailing silence (matches F5-TTS conditioning window without swallowing first generated word)
    tail_silence = np.zeros(int(0.05 * sr), dtype=np.float32)
    data = np.concatenate([data, tail_silence])

    sf.write(wav_path, data, sr, subtype="PCM_16")
    return wav_path, round(float(len(data)) / float(sr), 2)

def master_synthesized_audio(samples, sample_rate):
    """
    Studio Broadcast Post-Processing Chain for Synthesized Voiceovers:
    1. Removes DC offset.
    2. Trims excessive leading/trailing dead silence (< -48 dBFS) with micro-fades (40ms lead, 110ms tail).
    3. Bidirectional Loudness Normalization to -16.5 dBFS active speech RMS (0.149).
    4. Transparent soft-knee true-peak limiter at -0.5 dBFS (0.94) for NLE timeline readiness.
    """
    import numpy as np

    wav = np.asarray(samples, dtype=np.float32).squeeze()
    if wav.ndim == 0 or len(wav) == 0:
        return wav

    # 1. Remove DC offset
    wav = wav - float(np.mean(wav))

    # 2. Trim dead silence edges while preserving natural breath lead/tail
    win_len = max(1, int(0.02 * sample_rate))
    peak_init = float(np.max(np.abs(wav)))
    if peak_init > 1e-5 and len(wav) > win_len * 6:
        thresh = max(10 ** (-48.0 / 20.0), peak_init * 0.01)
        n_frames = len(wav) // win_len
        frames = wav[:n_frames * win_len].reshape(n_frames, win_len)
        f_rms = np.sqrt(np.mean(np.square(frames), axis=1))
        active = np.where(f_rms > thresh)[0]
        if len(active) > 0:
            lead = int(0.04 * sample_rate)
            tail = int(0.11 * sample_rate)
            s_idx = max(0, int(active[0] * win_len) - lead)
            e_idx = min(len(wav), int((active[-1] + 1) * win_len) + tail)
            if e_idx > s_idx:
                wav = wav[s_idx:e_idx]

    # 3. Bidirectional Active-Speech RMS Normalization (-16.5 dBFS target)
    frame_sz = max(1, int(0.05 * sample_rate))
    n_f = len(wav) // frame_sz
    if n_f > 0:
        f_mat = wav[:n_f * frame_sz].reshape(n_f, frame_sz)
        f_rms = np.sqrt(np.mean(np.square(f_mat), axis=1))
        noise_gate = max(0.004, float(np.max(f_rms)) * 0.08)
        speech_frames = f_rms[f_rms > noise_gate]
        active_rms = float(np.sqrt(np.mean(np.square(speech_frames)))) if len(speech_frames) > 0 else float(np.sqrt(np.mean(np.square(wav))))
    else:
        active_rms = float(np.sqrt(np.mean(np.square(wav))))

    target_studio_rms = 0.149  # -16.5 dBFS broadcast voiceover level
    if active_rms > 1e-5:
        gain = min(12.0, max(0.2, target_studio_rms / active_rms))
        wav = wav * gain

    # 4. Transparent Soft-Knee Peak Limiter (knee at 0.75, ceiling at 0.94 / -0.5 dBFS)
    knee = 0.75
    ceiling = 0.94
    abs_w = np.abs(wav)
    over = abs_w > knee
    if np.any(over):
        headroom = ceiling - knee
        sign = np.sign(wav[over])
        wav[over] = sign * (knee + headroom * np.tanh((abs_w[over] - knee) / headroom))

    # 5. Micro-fades (12ms) at start and end to prevent NLE timeline clicks
    fade_len = min(int(0.012 * sample_rate), len(wav) // 4)
    if fade_len > 0:
        wav[:fade_len] *= np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
        wav[-fade_len:] *= np.linspace(1.0, 0.0, fade_len, dtype=np.float32)

    return wav

def convert_audio_to_24k_mono_wav(source_path, target_path=None):
    """Converts any audio/video file (capped at 25s) to studio-conditioned 24kHz mono PCM WAV."""
    init_environment()
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Source audio file not found: {source_path}")

    if target_path:
        target_path = validate_safe_audio_output_path(target_path, default_dir=CLONE_CACHE_DIR)
    else:
        target_path = get_clean_24k_cache_path(source_path)

    # Reuse cached converted file if it already exists
    if os.path.exists(target_path) and os.path.getsize(target_path) > 1024:
        return target_path

    os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)

    ffmpeg_exe = get_ffmpeg_path()
    # Cap reference decoding at 25s (-t 25) and apply 75Hz highpass to remove mic rumble/DC plosives
    cmd = [
        ffmpeg_exe,
        "-y",
        "-t", "25",
        "-i", source_path,
        "-vn",
        "-af", "highpass=f=75,lowpass=f=11500",
        "-ar", "24000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        target_path
    ]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0 or not os.path.exists(target_path):
        # Fallback without -af filter if minimal ffmpeg build lacks filtergraph
        cmd_fallback = [
            ffmpeg_exe,
            "-y",
            "-t", "25",
            "-i", source_path,
            "-vn",
            "-ar", "24000",
            "-ac", "1",
            "-c:a", "pcm_s16le",
            target_path
        ]
        res = subprocess.run(cmd_fallback, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if res.returncode != 0 or not os.path.exists(target_path):
            raise RuntimeError(f"FFmpeg conversion failed: {res.stderr}")

    try:
        # Do NOT trim >9.5s audio blindly here without trimming ref_text; synchronized trimming is handled in /api/transcribe & /api/generate
        condition_reference_wav(target_path, trim_over_9_5s=False)
    except ValueError as ve:
        if os.path.exists(target_path):
            try: os.remove(target_path)
            except Exception: pass
        raise ve
    except Exception as cond_err:
        print(f"[!] Reference conditioning note: {cond_err}")

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

LANGUAGES_DIR = os.path.join(MODELS_DIR, "languages")
try:
    os.makedirs(LANGUAGES_DIR, exist_ok=True)
except Exception:
    pass

SUPPORTED_LANGUAGES = {
    "en": {
        "name": "English / Chinese",
        "flag": "🇺🇸",
        "model_arch": "F5TTS_v1_Base",
        "repo_id": "SWivid/F5-TTS",
        "subfolder": "F5TTS_v1_Base",
        "model_file": "model_1250000.safetensors",
        "vocab_file": "vocab.txt",
        "size_mb": 1286
    },
    "es": {
        "name": "Spanish (Español)",
        "flag": "🇪🇸",
        "model_arch": "F5TTS_Base",
        "repo_id": "jpgallegoar/F5-Spanish",
        "subfolder": "",
        "model_file": "model_1250000.safetensors",
        "vocab_file": "vocab.txt",
        "size_mb": 1286
    },
    "fr": {
        "name": "French (Français)",
        "flag": "🇫🇷",
        "model_arch": "F5TTS_Base",
        "repo_id": "RASPIAUDIO/F5-French-MixedSpeakers-reduced",
        "subfolder": "",
        "model_file": "model_last_reduced.pt",
        "vocab_file": "vocab.txt",
        "size_mb": 1286
    },
    "de": {
        "name": "German (Deutsch)",
        "flag": "🇩🇪",
        "model_arch": "F5TTS_Base",
        "repo_id": "hvoss-techfak/F5-TTS-German",
        "subfolder": "",
        "model_file": "model_f5tts_german.safetensors",
        "vocab_file": "vocab.txt",
        "size_mb": 1286
    },
    "hi": {
        "name": "Hindi (हिन्दी)",
        "flag": "🇮🇳",
        "model_arch": "F5TTS_Small",
        "repo_id": "SPRINGLab/F5-Hindi-24KHz",
        "subfolder": "",
        "model_file": "model_2500000.safetensors",
        "vocab_file": "vocab.txt",
        "size_mb": 603
    },
    "it": {
        "name": "Italian (Italiano)",
        "flag": "🇮🇹",
        "model_arch": "F5TTS_Base",
        "repo_id": "alien79/F5-TTS-italian",
        "subfolder": "",
        "model_file": "model_159600.safetensors",
        "vocab_file": "vocab.txt",
        "size_mb": 1286
    },
    "ja": {
        "name": "Japanese (日本語)",
        "flag": "🇯🇵",
        "model_arch": "F5TTS_Base",
        "repo_id": "Jmica/F5TTS",
        "subfolder": "",
        "model_file": "model_1108224.pt",
        "vocab_file": "vocab.txt",
        "size_mb": 5146
    },
    "ru": {
        "name": "Russian (Русский)",
        "flag": "🇷🇺",
        "model_arch": "F5TTS_Base",
        "repo_id": "hotstone228/F5-TTS-Russian",
        "subfolder": "",
        "model_file": "model_last.safetensors",
        "vocab_file": "vocab.txt",
        "size_mb": 1286
    },
    "ar": {
        "name": "Arabic (العربية)",
        "flag": "🇸🇦",
        "model_arch": "F5TTS_Small",
        "repo_id": "silma-ai/silma-tts",
        "subfolder": "",
        "model_file": "model.pt",
        "vocab_file": "vocab.txt",
        "size_mb": 2483
    }
}

_download_lock = threading.Lock()
_download_state = {
    "status": "idle",
    "lang": None,
    "progress": 0,
    "downloaded_mb": 0.0,
    "total_mb": 0.0,
    "error": None
}

def find_language_pack_paths(lang_code):
    """
    Returns (ckpt_path, vocab_path, model_arch) for given language code.
    """
    meta = SUPPORTED_LANGUAGES.get(lang_code, {})
    model_arch = meta.get("model_arch", "F5TTS_Base")
    
    # 1. Check explicit models/languages/<lang>/
    lang_dir = os.path.join(LANGUAGES_DIR, lang_code)
    if os.path.exists(lang_dir):
        cand_ckpts = []
        cand_vocabs = []
        for f in os.listdir(lang_dir):
            low = f.lower()
            fp = os.path.join(lang_dir, f)
            if os.path.isfile(fp):
                file_size = 0
                try:
                    file_size = os.path.getsize(fp)
                except Exception:
                    pass
                # Strict validation: model checkpoint must be at least 50 MB and not a .part/.tmp file
                if (low.endswith(".safetensors") or low.endswith(".pt") or low.endswith(".bin")) and not low.endswith(".part") and not low.endswith(".tmp"):
                    if file_size > 50 * 1024 * 1024:
                        cand_ckpts.append(fp)
                elif low.endswith(".txt") and "vocab" in low:
                    if file_size > 10:
                        cand_vocabs.append(fp)
                elif low.endswith(".txt") and not cand_vocabs:
                    if file_size > 10:
                        cand_vocabs.append(fp)
        
        if cand_ckpts:
            chosen_ckpt = cand_ckpts[0]
            chosen_vocab = cand_vocabs[0] if cand_vocabs else ""
            return chosen_ckpt, chosen_vocab, model_arch

    # 2. If 'en', fallback to bundled snapshots in models/hub/
    if lang_code == "en":
        ckpt, _ = find_local_f5_paths()
        if ckpt:
            return ckpt, "", "F5TTS_v1_Base"

    return None, None, model_arch

from tqdm.auto import tqdm

class HubProgressTracker(tqdm):
    def __init__(self, *args, **kwargs):
        try:
            kwargs['file'] = open(os.devnull, 'w')
        except Exception:
            pass
        super().__init__(*args, **kwargs)
        if self.total and self.total > 0:
            total_mb = round(self.total / (1024 * 1024), 1)
            with _download_lock:
                _download_state["total_mb"] = total_mb

    def update(self, n=1):
        super().update(n)
        global _download_state
        if self.total and self.total > 0:
            pct = round((self.n / self.total) * 100, 1)
            mb = round(self.n / (1024 * 1024), 1)
            total_mb = round(self.total / (1024 * 1024), 1)
            with _download_lock:
                _download_state["progress"] = pct
                _download_state["downloaded_mb"] = mb
                _download_state["total_mb"] = total_mb

def download_language_pack_worker(lang_code):
    global _download_state
    meta = SUPPORTED_LANGUAGES.get(lang_code)
    if not meta:
        with _download_lock:
            _download_state = {"status": "error", "lang": lang_code, "progress": 0, "downloaded_mb": 0.0, "total_mb": 0.0, "error": f"Unsupported language: {lang_code}"}
        return

    target_dir = os.path.join(LANGUAGES_DIR, lang_code)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as dir_err:
        with _download_lock:
            _download_state = {"status": "error", "lang": lang_code, "progress": 0, "downloaded_mb": 0.0, "total_mb": float(meta.get("size_mb", 1200)), "error": f"Cannot create folder: {dir_err}"}
        return

    try:
        import urllib.request
        from huggingface_hub import hf_hub_url
        repo_id = meta["repo_id"]
        subfolder = meta.get("subfolder") or None
        model_filename = meta["model_file"]
        vocab_filename = meta.get("vocab_file", "vocab.txt")
        total_sz = float(meta.get("size_mb", 1200))

        with _download_lock:
            _download_state = {
                "status": "downloading",
                "lang": lang_code,
                "progress": 0,
                "downloaded_mb": 0.0,
                "total_mb": total_sz,
                "error": None
            }

        # 1. Download vocab file
        vocab_dest = os.path.join(target_dir, vocab_filename)
        vocab_url = hf_hub_url(repo_id=repo_id, filename=vocab_filename, subfolder=subfolder)
        print(f"[*] Downloading '{lang_code}' vocab file from {vocab_url}...")
        try:
            req_v = urllib.request.Request(vocab_url, headers={"User-Agent": "EasySpeech-VoiceClone/1.0"})
            with urllib.request.urlopen(req_v, timeout=25) as r_v:
                with open(vocab_dest + ".tmp", "wb") as f_v:
                    f_v.write(r_v.read())
            # Integrity check: Ensure vocab file is not empty or corrupted (< 10 bytes)
            if not os.path.exists(vocab_dest + ".tmp") or os.path.getsize(vocab_dest + ".tmp") < 10:
                raise IOError(f"Downloaded vocab file for '{lang_code}' is incomplete or empty.")
            if os.path.exists(vocab_dest):
                try: os.remove(vocab_dest)
                except Exception: pass
            os.replace(vocab_dest + ".tmp", vocab_dest)
        except Exception as v_err:
            if os.path.exists(vocab_dest + ".tmp"):
                try: os.remove(vocab_dest + ".tmp")
                except Exception: pass
            print(f"[!] Direct vocab download note: {v_err}, trying hf_hub_download...")
            from huggingface_hub import hf_hub_download
            hf_hub_download(repo_id=repo_id, filename=vocab_filename, subfolder=subfolder, local_dir=target_dir)

        # 2. Download model file with streaming real-time chunks
        model_dest = os.path.join(target_dir, model_filename)
        model_url = hf_hub_url(repo_id=repo_id, filename=model_filename, subfolder=subfolder)
        print(f"[*] Streaming '{lang_code}' model file from {model_url}...")

        stream_success = False
        tmp_model_dest = model_dest + ".part"
        try:
            req_m = urllib.request.Request(model_url, headers={"User-Agent": "EasySpeech-VoiceClone/1.0"})
            with urllib.request.urlopen(req_m, timeout=30) as r_m:
                content_len = int(r_m.headers.get("Content-Length") or (total_sz * 1024 * 1024))
                real_total_mb = round(content_len / (1024 * 1024), 1)
                with _download_lock:
                    _download_state["total_mb"] = real_total_mb

                downloaded_bytes = 0
                chunk_sz = 512 * 1024  # 512 KB per chunk for high responsiveness
                last_ui_update = 0
                with open(tmp_model_dest, "wb") as f_m:
                    while True:
                        chunk = r_m.read(chunk_sz)
                        if not chunk:
                            break
                        f_m.write(chunk)
                        downloaded_bytes += len(chunk)

                        now = time.time()
                        if now - last_ui_update >= 0.1 or downloaded_bytes >= content_len:
                            last_ui_update = now
                            pct = round((downloaded_bytes / max(1, content_len)) * 100, 1)
                            mb = round(downloaded_bytes / (1024 * 1024), 1)
                            with _download_lock:
                                _download_state["progress"] = pct
                                _download_state["downloaded_mb"] = mb
                                _download_state["total_mb"] = real_total_mb

                # Network Safety Verification Guard:
                # Ensure the entire stream was actually received before renaming .part to final model file.
                # Protects against silent CDN connection dropouts without socket exceptions.
                min_expected_bytes = int(content_len * 0.98) if content_len > 0 else int(total_sz * 1024 * 1024 * 0.8)
                if downloaded_bytes < min_expected_bytes or not os.path.exists(tmp_model_dest) or os.path.getsize(tmp_model_dest) < min_expected_bytes:
                    raise IOError(f"Incomplete download: received {downloaded_bytes} bytes (expected ~{content_len} bytes)")

                if os.path.exists(model_dest):
                    try: os.remove(model_dest)
                    except Exception: pass
                os.replace(tmp_model_dest, model_dest)
                stream_success = True
        except Exception as str_err:
            print(f"[!] Direct streaming failed: {str_err}, falling back to hf_hub_download...")
            if os.path.exists(tmp_model_dest):
                try: os.remove(tmp_model_dest)
                except Exception: pass

        if not stream_success:
            from huggingface_hub import hf_hub_download
            hf_hub_download(
                repo_id=repo_id,
                filename=model_filename,
                subfolder=subfolder,
                local_dir=target_dir,
                tqdm_class=HubProgressTracker
            )
            # Post-fallback integrity verification
            min_model_bytes = int(total_sz * 1024 * 1024 * 0.7)
            if not os.path.exists(model_dest) or os.path.getsize(model_dest) < min_model_bytes:
                raise IOError(f"Fallback download for '{model_filename}' did not produce a complete model checkpoint.")

        # 3. Mark completed
        with _download_lock:
            final_mb = _download_state.get("total_mb") or total_sz
            _download_state = {
                "status": "completed",
                "lang": lang_code,
                "progress": 100,
                "downloaded_mb": final_mb,
                "total_mb": final_mb,
                "error": None
            }
        print(f"[+] Language pack '{lang_code}' downloaded successfully into {target_dir}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        with _download_lock:
            _download_state = {
                "status": "error",
                "lang": lang_code,
                "progress": 0,
                "downloaded_mb": 0.0,
                "total_mb": float(meta.get("size_mb", 1200)),
                "error": str(e)
            }

_f5_runtime_patched = False

def patch_f5_tts_runtime():
    """
    Upgrades F5-TTS internal inference functions in-memory for production studio quality:
    1. Sentence chunker supports Hindi Purna Viram (।, ॥) and Arabic punctuation (؟, ،, ۔).
    2. Prevents Japanese (ja) Kanji from being corrupted into Mandarin Chinese Pinyin.
    3. Pads reference audio tail with 150ms silence (instead of 50ms) to prevent first-word clipping,
       respects Hindi । sentence endings, and stores temp WAVs inside CLONE_CACHE_DIR instead of leaking in %TEMP%.
    """
    global _f5_runtime_patched
    if _f5_runtime_patched:
        return
    try:
        try:
            from patch_f5_tts import apply_f5_tts_disk_patch
            apply_f5_tts_disk_patch()
        except Exception:
            pass
        import re
        import hashlib
        import numpy as np
        import torch
        import torchaudio
        from concurrent.futures import ThreadPoolExecutor
        import f5_tts.infer.utils_infer as _ui
        import f5_tts.model.utils as _mu
        import f5_tts.api as _api

        _orig_convert_pinyin = _mu.convert_char_to_pinyin

        def _patched_convert_char_to_pinyin(text_list, polyphone=True):
            # For all non-English languages (es, fr, de, hi, it, ja, ru, ar), or when text does not contain Chinese characters,
            # tokenize directly into character sequences without running rjieba/pinyin (which injects unwanted spaces into accented and non-Latin words).
            if _current_loaded_lang != "en" or _current_loaded_lang == "ja" or not any(re.search(r"[\u3100-\u9fff]", str(t)) for t in text_list):
                custom_trans = str.maketrans({";": ",", "“": '"', "”": '"', "‘": "'", "’": "'", "—": ",", "–": ","})
                return [list(str(t).translate(custom_trans)) for t in text_list]
            return _orig_convert_pinyin(text_list, polyphone=polyphone)

        _mu.convert_char_to_pinyin = _patched_convert_char_to_pinyin
        _ui.convert_char_to_pinyin = _patched_convert_char_to_pinyin

        _orig_load_checkpoint = getattr(_ui, "load_checkpoint", None)

        def _patched_load_checkpoint(model, ckpt_path, device: str, dtype=None, use_ema=True):
            if dtype is None:
                dtype = (
                    torch.float16
                    if "cuda" in device
                    and torch.cuda.get_device_properties(device).major >= 7
                    and not torch.cuda.get_device_name().endswith("[ZLUDA]")
                    else torch.float32
                )
            model = model.to(dtype)

            ckpt_type = ckpt_path.split(".")[-1].lower()
            if ckpt_type == "safetensors":
                from safetensors.torch import load_file
                load_dev = "cpu" if str(device).startswith("mps") else device
                try:
                    checkpoint = load_file(ckpt_path, device=load_dev)
                except Exception:
                    checkpoint = load_file(ckpt_path, device="cpu")
            else:
                map_dev = "cpu" if str(device).startswith("mps") else device
                try:
                    checkpoint = torch.load(ckpt_path, map_location=map_dev, weights_only=True)
                except Exception:
                    checkpoint = torch.load(ckpt_path, map_location="cpu", weights_only=False)

            if use_ema:
                if ckpt_type == "safetensors":
                    raw_dict = checkpoint
                elif isinstance(checkpoint, dict):
                    if "ema_model_state_dict" in checkpoint:
                        raw_dict = checkpoint["ema_model_state_dict"]
                    elif "model_state_dict" in checkpoint:
                        raw_dict = checkpoint["model_state_dict"]
                    elif "state_dict" in checkpoint:
                        raw_dict = checkpoint["state_dict"]
                    else:
                        raw_dict = checkpoint
                else:
                    raw_dict = checkpoint

                model_state_dict = {
                    k.replace("ema_model.", ""): v
                    for k, v in raw_dict.items()
                    if k not in ["initted", "step"]
                }

                # patch for backward compatibility
                for key in ["mel_spec.mel_stft.mel_scale.fb", "mel_spec.mel_stft.spectrogram.window"]:
                    if key in model_state_dict:
                        del model_state_dict[key]

                model.load_state_dict(model_state_dict)
            else:
                if ckpt_type == "safetensors":
                    checkpoint = {"model_state_dict": checkpoint}
                elif isinstance(checkpoint, dict) and "model_state_dict" not in checkpoint:
                    checkpoint = {"model_state_dict": checkpoint}
                model.load_state_dict(checkpoint["model_state_dict"])

            del checkpoint
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            return model.to(device)

        _ui.load_checkpoint = _patched_load_checkpoint

        def _patched_chunk_text(text, max_chars=135):
            # Universal character-aware sentence chunker:
            # 1. Cap max_chars at 180 characters per batch (optimal for F5-TTS ~12-15s audio generation).
            # 2. Counts native characters (NOT UTF-8 bytes) so multi-byte scripts (Devanagari, Cyrillic, Arabic)
            #    are never fragmented mid-sentence at commas.
            eff_max = min(int(max_chars), 180)

            # 1. Split on sentence-ending punctuation first (. ! ? । ॥ ؟ ۔)
            raw_sentences = [s.strip() for s in re.split(r"(?<=[.!?।॥؟۔])\s+|(?<=[。！？।॥])", text) if s and s.strip()]
            sentences = []
            for rs in raw_sentences:
                # Only split on pause/comma if an individual sentence exceeds eff_max characters
                if len(rs) > eff_max:
                    sub_clauses = [sc.strip() for sc in re.split(r"(?<=[,;،؛:，；：])\s*", rs) if sc and sc.strip()]
                    sentences.extend(sub_clauses if len(sub_clauses) > 1 else [rs])
                else:
                    sentences.append(rs)

            chunks = []
            current_chunk = ""
            is_cjk = (_current_loaded_lang == "ja" or bool(re.search(r"[\u3040-\u30FF\u4E00-\u9FFF]", text)))
            sep = "" if is_cjk else " "

            for sentence in sentences:
                if not sentence:
                    continue
                candidate = (current_chunk + sep + sentence).strip() if current_chunk else sentence
                if current_chunk and len(candidate) > eff_max:
                    chunks.append(current_chunk.strip())
                    current_chunk = sentence
                else:
                    current_chunk = candidate
            if current_chunk:
                chunks.append(current_chunk.strip())

            # Merge tiny trailing orphan chunk (< 20 characters) into previous chunk to prevent hallucination
            if len(chunks) > 1 and len(chunks[-1]) < 20:
                merged = (chunks[-2] + sep + chunks[-1]).strip()
                if len(merged) <= int(eff_max * 1.35):
                    chunks[-2] = merged
                    chunks.pop()

            return chunks

        _ui.chunk_text = _patched_chunk_text

        def _patched_infer_process(
            ref_audio,
            ref_text,
            gen_text,
            model_obj,
            vocoder,
            mel_spec_type=_ui.mel_spec_type,
            show_info=print,
            progress=_ui.tqdm,
            target_rms=_ui.target_rms,
            cross_fade_duration=_ui.cross_fade_duration,
            nfe_step=_ui.nfe_step,
            cfg_strength=_ui.cfg_strength,
            sway_sampling_coef=_ui.sway_sampling_coef,
            speed=_ui.speed,
            fix_duration=_ui.fix_duration,
            device=_ui.device,
        ):
            audio, sr = _ui.torchaudio.load(ref_audio)
            # Universal character-aware max_chars calculation (prevents multi-byte script fragmentation)
            ref_clean = ref_text.strip()
            ref_char_count = max(1, len(ref_clean))
            ref_dur_sec = max(0.5, audio.shape[-1] / sr)
            max_chars = int(ref_char_count / ref_dur_sec * max(4.0, (22 - ref_dur_sec)) * float(speed))
            gen_text_batches = _ui.chunk_text(gen_text, max_chars=max_chars)
            for i, gen_text_i in enumerate(gen_text_batches):
                print(f"gen_text {i}", gen_text_i)
            print("\n")

            show_info(f"Generating audio in {len(gen_text_batches)} batches...")
            if not gen_text_batches:
                show_info("No text batches to generate.")
                return None, _ui.target_sample_rate, None

            return next(
                _ui.infer_batch_process(
                    (audio, sr),
                    ref_text,
                    gen_text_batches,
                    model_obj,
                    vocoder,
                    mel_spec_type=mel_spec_type,
                    progress=progress,
                    target_rms=target_rms,
                    cross_fade_duration=cross_fade_duration,
                    nfe_step=nfe_step,
                    cfg_strength=cfg_strength,
                    sway_sampling_coef=sway_sampling_coef,
                    speed=speed,
                    fix_duration=fix_duration,
                    device=device,
                )
            )

        _ui.infer_process = _patched_infer_process
        _api.infer_process = _patched_infer_process

        def _patched_infer_batch_process(
            ref_audio,
            ref_text,
            gen_text_batches,
            model_obj,
            vocoder,
            mel_spec_type="vocos",
            progress=_ui.tqdm,
            target_rms=0.1,
            cross_fade_duration=0.15,
            nfe_step=32,
            cfg_strength=2.0,
            sway_sampling_coef=-1,
            speed=1,
            fix_duration=None,
            device=None,
            streaming=False,
            chunk_size=2048,
        ):
            audio, sr = ref_audio
            if audio.shape[0] > 1:
                audio = torch.mean(audio, dim=0, keepdim=True)

            rms = torch.sqrt(torch.mean(torch.square(audio)))
            if rms < target_rms:
                audio = audio * target_rms / rms
            if sr != _ui.target_sample_rate:
                resampler = torchaudio.transforms.Resample(sr, _ui.target_sample_rate)
                audio = resampler(audio)
            audio = audio.to(device)

            generated_waves = []
            spectrograms = []

            # Ensure clean boundary space regardless of unicode character set
            ref_text_clean = ref_text.strip()
            if not ref_text.endswith(" "):
                ref_text = ref_text + " "

            # Allocate fix_duration using character lengths (not UTF-8 bytes)
            if fix_duration is not None and len(gen_text_batches) > 1:
                ref_audio_len_frames = audio.shape[-1] // _ui.hop_length
                ref_sec = ref_audio_len_frames * _ui.hop_length / _ui.target_sample_rate
                target_total = fix_duration - ref_sec
                weights = [max(1, len(c.strip())) for c in gen_text_batches]
                total_w = sum(weights)
                allocatable = target_total + cross_fade_duration * (len(gen_text_batches) - 1)
                fix_durations = [ref_sec + allocatable * w / total_w for w in weights]
            else:
                fix_durations = [fix_duration] * len(gen_text_batches)

            def _infer_basic(gen_text, fix_dur):
                local_speed = speed
                gen_text_clean = gen_text.strip()
                if len(gen_text_clean) < 3:
                    local_speed = 0.3

                # Prepare the text cleanly joined with single space
                text_list = [(ref_text_clean + " " + gen_text_clean).strip()]
                final_text_list = _ui.convert_char_to_pinyin(text_list)

                ref_audio_len = audio.shape[-1] // _ui.hop_length
                if fix_dur is not None:
                    duration = int(fix_dur * _ui.target_sample_rate / _ui.hop_length)
                else:
                    # Universal character-aware duration calculation (prevents duration drift, missing words, reference leakage)
                    ref_char_len = max(1, len(ref_text_clean))
                    gen_char_len = max(1, len(gen_text_clean))
                    duration = ref_audio_len + int(ref_audio_len / ref_char_len * gen_char_len / local_speed)

                # inference
                with torch.inference_mode():
                    generated, _ = model_obj.sample(
                        cond=audio,
                        text=final_text_list,
                        duration=duration,
                        steps=nfe_step,
                        cfg_strength=cfg_strength,
                        sway_sampling_coef=sway_sampling_coef,
                    )
                    del _

                    generated = generated.to(torch.float32)  # generated mel spectrogram
                    generated = generated[:, ref_audio_len:, :]
                    generated = generated.permute(0, 2, 1)
                    if mel_spec_type == "vocos":
                        generated_wave = vocoder.decode(generated)
                    elif mel_spec_type == "bigvgan":
                        generated_wave = vocoder(generated)
                    if rms < target_rms:
                        generated_wave = generated_wave * rms / target_rms

                    # wav -> numpy
                    generated_wave = generated_wave.squeeze().cpu().numpy()

                return generated_wave, generated

            def infer_single_process(gen_text, fix_dur):
                generated_wave, generated = _infer_basic(gen_text, fix_dur)
                generated_cpu = generated[0].cpu().numpy()
                del generated
                return generated_wave, generated_cpu

            def infer_single_process_streaming(gen_text, fix_dur):
                generated_wave, generated = _infer_basic(gen_text, fix_dur)
                del generated
                for j in range(0, len(generated_wave), chunk_size):
                    yield generated_wave[j : j + chunk_size], _ui.target_sample_rate

            if streaming:
                batches_iter = progress.tqdm(gen_text_batches) if progress is not None else gen_text_batches
                for gen_text, fix_dur in zip(batches_iter, fix_durations):
                    for chunk in infer_single_process_streaming(gen_text, fix_dur):
                        yield chunk
            else:
                with ThreadPoolExecutor() as executor:
                    futures = [executor.submit(infer_single_process, gt, fd) for gt, fd in zip(gen_text_batches, fix_durations)]
                    for future in (progress.tqdm(futures) if progress is not None else futures):
                        result = future.result()
                        if result:
                            generated_wave, generated_mel_spec = result
                            generated_waves.append(generated_wave)
                            spectrograms.append(generated_mel_spec)

                if generated_waves:
                    if cross_fade_duration <= 0:
                        final_wave = np.concatenate(generated_waves)
                    else:
                        final_wave = generated_waves[0]
                        for i in range(1, len(generated_waves)):
                            prev_wave = final_wave
                            next_wave = generated_waves[i]

                            cross_fade_samples = int(cross_fade_duration * _ui.target_sample_rate)
                            cross_fade_samples = min(cross_fade_samples, len(prev_wave), len(next_wave))

                            if cross_fade_samples <= 0:
                                final_wave = np.concatenate([prev_wave, next_wave])
                                continue

                            prev_overlap = prev_wave[-cross_fade_samples:]
                            next_overlap = next_wave[:cross_fade_samples]

                            fade_out = np.linspace(1, 0, cross_fade_samples)
                            fade_in = np.linspace(0, 1, cross_fade_samples)

                            cross_faded_overlap = prev_overlap * fade_out + next_overlap * fade_in

                            new_wave = np.concatenate(
                                [prev_wave[:-cross_fade_samples], cross_faded_overlap, next_wave[cross_fade_samples:]]
                            )
                            final_wave = new_wave

                    combined_spectrogram = np.concatenate(spectrograms, axis=1) if spectrograms else None
                    yield final_wave, _ui.target_sample_rate, combined_spectrogram
                else:
                    yield None, _ui.target_sample_rate, None

        _ui.infer_batch_process = _patched_infer_batch_process
        if hasattr(_api, "infer_batch_process"):
            _api.infer_batch_process = _patched_infer_batch_process

        def _patched_preprocess_ref_audio_text(ref_audio_orig, ref_text, show_info=print):
            show_info("Converting audio...")
            with open(ref_audio_orig, "rb") as audio_file:
                audio_data = audio_file.read()
                audio_hash = hashlib.md5(audio_data).hexdigest()

            if audio_hash in _ui._ref_audio_cache and os.path.exists(_ui._ref_audio_cache[audio_hash]):
                ref_audio = _ui._ref_audio_cache[audio_hash]
            else:
                os.makedirs(CLONE_CACHE_DIR, exist_ok=True)
                temp_path = os.path.join(CLONE_CACHE_DIR, f"f5ref_{audio_hash[:12]}.wav")
                aseg = _ui.AudioSegment.from_file(ref_audio_orig)

                if len(aseg) > 11500:
                    non_silent_segs = _ui.silence.split_on_silence(
                        aseg, min_silence_len=600, silence_thresh=-45, keep_silence=600, seek_step=10
                    )
                    non_silent_wave = _ui.AudioSegment.silent(duration=0)
                    for non_silent_seg in non_silent_segs:
                        if len(non_silent_wave) > 5000 and len(non_silent_wave + non_silent_seg) > 10500:
                            break
                        non_silent_wave += non_silent_seg
                    if len(non_silent_wave) >= 2500:
                        aseg = non_silent_wave
                    if len(aseg) > 10500:
                        aseg = aseg[:10500]

                # Trim silence edges, apply 25ms fade-out on voice tail, and append 50ms silence pad
                # (keeps exact F5-TTS frame alignment so first generated word is never cut at ref_audio_len).
                aseg = _ui.remove_silence_edges(aseg, silence_threshold=-42)
                if len(aseg) > 100:
                    aseg = aseg.fade_out(25)
                aseg = aseg + _ui.AudioSegment.silent(duration=50)
                aseg.export(temp_path, format="wav")
                ref_audio = temp_path
                _ui._ref_audio_cache[audio_hash] = ref_audio

            if not ref_text.strip():
                if audio_hash in _ui._ref_text_cache:
                    ref_text = _ui._ref_text_cache[audio_hash]
                else:
                    ref_text = _ui.transcribe(ref_audio)
                    _ui._ref_text_cache[audio_hash] = ref_text

            ref_text = ref_text.strip()
            # Clean up trailing dashes or unfinished syllable marks (e.g. "amaz--", "so-")
            import re
            ref_text = re.sub(r'[\-—–\.\,\s]+$', '', ref_text).strip()
            if _current_loaded_lang == "hi":
                if not ref_text.endswith(("।", ".", "?", "!")):
                    ref_text += "। "
                else:
                    ref_text += " "
            elif _current_loaded_lang == "ja":
                if not ref_text.endswith(("。", "？", "！", ".", "?", "!")):
                    ref_text += "。"
            else:
                if not ref_text.endswith((". ", "。", "? ", "! ", "؟ ", "۔ ")):
                    if ref_text.endswith((".", "?", "!", "؟", "۔")):
                        ref_text += " "
                    else:
                        ref_text += ". "

            return ref_audio, ref_text

        _ui.preprocess_ref_audio_text = _patched_preprocess_ref_audio_text
        _api.preprocess_ref_audio_text = _patched_preprocess_ref_audio_text
        _f5_runtime_patched = True
        print("[+] Applied F5-TTS studio runtime patches (multilingual chunking, 150ms boundary pad, Kanji guard).")
    except Exception as patch_err:
        print(f"[!] Note: F5-TTS runtime patch skipped: {patch_err}")

_EN_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
            "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
            "seventeen", "eighteen", "nineteen"]
_EN_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]

def _int_to_en_words(n):
    if n < 0:
        return "minus " + _int_to_en_words(-n)
    if n < 20:
        return _EN_ONES[n]
    if n < 100:
        return _EN_TENS[n // 10] + ("" if n % 10 == 0 else " " + _EN_ONES[n % 10])
    if n < 1000:
        rem = n % 100
        return _EN_ONES[n // 100] + " hundred" + ("" if rem == 0 else " " + _int_to_en_words(rem))
    if n < 1000000:
        rem = n % 1000
        return _int_to_en_words(n // 1000) + " thousand" + ("" if rem == 0 else " " + _int_to_en_words(rem))
    if n < 1000000000:
        rem = n % 1000000
        return _int_to_en_words(n // 1000000) + " million" + ("" if rem == 0 else " " + _int_to_en_words(rem))
    return " ".join(_EN_ONES[int(d)] for d in str(n))

_HI_NUM_WORDS = {
    0: "शून्य", 1: "एक", 2: "दो", 3: "तीन", 4: "चार", 5: "पाँच", 6: "छह", 7: "सात", 8: "आठ", 9: "नौ",
    10: "दस", 11: "ग्यारह", 12: "बारह", 13: "तेरह", 14: "चौदह", 15: "पंद्रह", 16: "सोलह", 17: "सत्रह", 18: "अठारह", 19: "उन्नीस",
    20: "बीस", 21: "इक्कीस", 22: "बाईस", 23: "तेईस", 24: "चौबीस", 25: "पच्चीस", 26: "छब्बीस", 27: "सत्ताईस", 28: "अट्ठाईस", 29: "उनतीस",
    30: "तीस", 35: "पैंतीस", 40: "चालीस", 45: "पैंतालीस", 50: "पचास", 60: "साठ", 70: "सत्तर", 75: "पचहत्तर", 80: "अस्सी", 90: "नब्बे", 100: "सौ", 1000: "हज़ार"
}

def _int_to_hi_words(n):
    if n in _HI_NUM_WORDS:
        if n == 100:
            return "एक सौ"
        if n == 1000:
            return "एक हज़ार"
        return _HI_NUM_WORDS[n]
    if 20 < n < 100:
        tens = (n // 10) * 10
        ones = n % 10
        if tens in _HI_NUM_WORDS and ones in _HI_NUM_WORDS:
            return _HI_NUM_WORDS[tens] + " " + _HI_NUM_WORDS[ones]
    if 100 <= n < 1000:
        rem = n % 100
        return _int_to_hi_words(n // 100) + " सौ" + ("" if rem == 0 else " " + _int_to_hi_words(rem))
    if 1000 <= n < 100000:
        rem = n % 1000
        return _int_to_hi_words(n // 1000) + " हज़ार" + ("" if rem == 0 else " " + _int_to_hi_words(rem))
    if 100000 <= n < 10000000:
        rem = n % 100000
        return _int_to_hi_words(n // 100000) + " लाख" + ("" if rem == 0 else " " + _int_to_hi_words(rem))
    return " ".join(_HI_NUM_WORDS.get(int(d), d) for d in str(n))

def validate_script_for_language(text, lang="en"):
    """
    Safety Guard against cross-script vocabulary mismatch (where unknown characters map to index 0 / space
    and cause F5-TTS to synthesize several seconds of unintelligible babble).
    Returns (is_valid, error_message).
    """
    import re
    clean = (text or "").strip()
    if not clean:
        return False, "Empty text provided for synthesis."

    lang = (lang or "en").strip().lower()
    devanagari = len(re.findall(r'[\u0900-\u097F]', clean))
    cyrillic = len(re.findall(r'[\u0400-\u04FF]', clean))
    arabic = len(re.findall(r'[\u0600-\u06FF]', clean))
    japanese = len(re.findall(r'[\u3040-\u30FF\u4E00-\u9FFF]', clean))
    latin = len(re.findall(r'[A-Za-z\u00C0-\u024F]', clean))

    if lang == "hi":
        if devanagari == 0 and latin > 0:
            return False, "Hindi voice clones require Devanagari script (e.g. नमस्ते). Click '✨ AI Polish' to automatically convert Hinglish to Hindi!"
        if cyrillic > 2 or arabic > 2:
            return False, "Selected voice is a Hindi voice model and cannot read Cyrillic or Arabic script."
    elif lang == "ru":
        if cyrillic == 0 and (latin > 0 or devanagari > 0 or arabic > 0):
            return False, "Russian voice clones require Cyrillic script (e.g. Привет). Click '✨ AI Polish' to convert your text!"
    elif lang == "ar":
        if arabic == 0 and (latin > 0 or devanagari > 0 or cyrillic > 0):
            return False, "Arabic voice clones require Arabic script (e.g. مرحبا). Click '✨ AI Polish' to convert your text!"
    elif lang == "ja":
        if japanese == 0 and (latin > 0 or devanagari > 0 or cyrillic > 0 or arabic > 0):
            return False, "Japanese voice clones require Japanese Kanji/Kana script (e.g. こんにちは). Click '✨ AI Polish' to convert your text!"
    elif lang in ("en", "es", "fr", "de", "it"):
        lang_label = SUPPORTED_LANGUAGES.get(lang, {}).get("name", lang.upper())
        if devanagari > 0 and latin < devanagari:
            return False, f"Selected voice uses the {lang_label} model and cannot pronounce Hindi Devanagari script. Please select a Hindi voice clone or enter {lang_label} text."
        if cyrillic > 0 and latin < cyrillic:
            return False, f"Selected voice uses the {lang_label} model and cannot pronounce Cyrillic script. Please select a Russian voice clone or enter {lang_label} text."
        if arabic > 0 and latin < arabic:
            return False, f"Selected voice uses the {lang_label} model and cannot pronounce Arabic script. Please select an Arabic voice clone or enter {lang_label} text."

    return True, None

def normalize_tts_text(text, lang="en"):
    """
    Studio Pronunciation & Prosody Normalizer for F5-TTS:
    - Converts OOV typography (—, –, …, curly quotes, emojis) into clean in-vocabulary pauses.
    - Expands URLs/domains (www., .com, .io, motionkitae), currencies ($100), percentages (100%),
      units (24kHz, 4GB, 1.0x), acronyms (AE, GPU, AI), and numbers into natural spoken words.
    """
    import re
    if not text:
        return ""

    lang = (lang or "en").strip().lower()
    s = str(text).strip()

    # 1. Strip emojis and markdown formatting symbols (*, #, `, ~)
    s = re.sub(r'[\U00010000-\U0010ffff]', '', s)
    s = re.sub(r'[*#`~_]+', ' ', s)

    # 2. Typography normalization (em-dash / en-dash -> natural comma pause)
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    s = s.replace("…", "...")
    s = re.sub(r'\s*[—–]\s*', ', ', s)
    s = re.sub(r'\s+-\s+', ', ', s)

    if lang == "en":
        # 3. URL & domain pronunciation expansion
        s = re.sub(r'https?://', '', s, flags=re.I)
        s = re.sub(r'\bwww\.', 'w w w dot ', s, flags=re.I)
        s = re.sub(r'\bmotionkitae\b', 'motionkit A E', s, flags=re.I)
        s = re.sub(r'\bmotionkit\s+ae\b', 'motionkit A E', s, flags=re.I)
        s = re.sub(r'\.com\b', ' dot com', s, flags=re.I)
        s = re.sub(r'\.org\b', ' dot org', s, flags=re.I)
        s = re.sub(r'\.net\b', ' dot net', s, flags=re.I)
        s = re.sub(r'\.io\b', ' dot I O', s, flags=re.I)
        s = re.sub(r'\.ai\b', ' dot A I', s, flags=re.I)

        # 4. Technical units, multipliers, currencies & symbols
        s = re.sub(r'\$(\d[\d,]*(?:\.\d+)?)', r'\1 dollars', s)
        s = re.sub(r'(\d+)\s*%', r'\1 percent', s)
        s = re.sub(r'(\d+)\s*\+', r'\1 plus', s)
        s = re.sub(r'\b(\d+)\.0x\b', r'\1 times', s, flags=re.I)
        s = re.sub(r'\b(\d+)\.(\d+)x\b', r'\1 point \2 times', s, flags=re.I)
        s = re.sub(r'\b(\d+)x\b', r'\1 times', s, flags=re.I)
        s = re.sub(r'\b(\d+)\s*khz\b', r'\1 kilohertz', s, flags=re.I)
        s = re.sub(r'\b(\d+)\s*gb\b', r'\1 gigabytes', s, flags=re.I)
        s = re.sub(r'\b(\d+)\s*mb\b', r'\1 megabytes', s, flags=re.I)
        s = re.sub(r'\b(\d+)\s*fps\b', r'\1 F P S', s, flags=re.I)

        # Remove commas inside numbers (e.g. 30,000 -> 30000) and expand decimals (99.5 -> 99 point 5)
        s = re.sub(r'(?<=\d),(?=\d{3}\b)', '', s)
        s = re.sub(r'\b(\d+)\.(\d+)\b', r'\1 point \2', s)

        # Expand standalone integers (up to 6 digits) into spoken English words
        def _en_num_repl(m):
            val_str = m.group(0)
            try:
                val = int(val_str)
                if 0 <= val <= 999999:
                    return _int_to_en_words(val)
            except Exception:
                pass
            return val_str

        s = re.sub(r'\b\d{1,6}\b', _en_num_repl, s)

        # Expand common video-editing / tech uppercase acronyms so F5-TTS spells them clearly
        acronyms = {
            "AE": "A E", "PR": "P R", "AI": "A I", "GPU": "G P U", "VRAM": "V RAM",
            "CPU": "C P U", "FPS": "F P S", "HD": "H D", "UI": "U I", "UX": "U X",
            "PC": "P C", "API": "A P I", "TTS": "T T S", "CEP": "C E P", "NLE": "N L E"
        }
        for acr, expanded in acronyms.items():
            s = re.sub(r'\b' + acr + r'\b', expanded, s)

    elif lang == "hi":
        # Convert Devanagari digits (०-९) to ASCII digits first for unified number expansion
        dev_digits = str.maketrans("०१२३४५६७८९", "0123456789")
        s = s.translate(dev_digits)
        s = re.sub(r'(?<=\d),(?=\d{3}\b)', '', s)
        s = re.sub(r'(\d+)\s*%', r'\1 प्रतिशत', s)
        s = re.sub(r'(\d+)\s*\+', r'\1 प्लस', s)
        s = re.sub(r'\b(\d+)\.(\d+)\b', r'\1 दशमलव \2', s)

        def _hi_num_repl(m):
            try:
                val = int(m.group(0))
                if 0 <= val <= 9999999:
                    return _int_to_hi_words(val)
            except Exception:
                pass
            return m.group(0)

        s = re.sub(r'\b\d{1,7}\b', _hi_num_repl, s)

        # Transliterate common uppercase video-editing / tech acronyms into Devanagari phonetics
        hi_acronyms = {
            "AI": "ए आई", "GPU": "जी पी यू", "CPU": "सी पी यू", "PC": "पी सी",
            "FPS": "एफ पी एस", "RAM": "रैम", "VRAM": "वी रैम", "UI": "यू आई", "UX": "यू एक्स",
            "AE": "ए ई", "PR": "पी आर", "HD": "एच डी", "4K": "फोर के", "API": "ए पी आई"
        }
        for acr, hi_exp in hi_acronyms.items():
            s = re.sub(r'\b' + re.escape(acr) + r'\b', hi_exp, s, flags=re.I)

        # Ensure sentence-ending ASCII periods and ellipses in Hindi text use natural pauses
        s = re.sub(r'\.{2,}', ', ', s)
        s = re.sub(r'(?<!\.)\.(?!\.)(\s+|$)', r'। ', s)
        # Phonetic disambiguation for common Hinglish loanwords ("अनदर" -> "अनादर" so it is not read as "अंदर")
        s = s.replace("अनदर", "अनादर")

    elif lang in ("es", "fr", "de", "it"):
        # Normalize uppercase accented characters missing from the 2,545-token Emilia vocab.txt
        # so sentence-initial accented words in FR/DE/IT never map to index 0 (space)
        accent_fallbacks = {
            "Œ": "œ", "Ë": "ë", "Ù": "ù", "Û": "û", "Ÿ": "ÿ",
            "ẞ": "ss", "Ì": "ì", "Ò": "ò", "«": '"', "»": '"', "„": '"'
        }
        for k_ch, v_ch in accent_fallbacks.items():
            s = s.replace(k_ch, v_ch)
        s = re.sub(r'(?<=\d)[.,](?=\d{3}\b)', '', s)
        if lang == "es":
            s = re.sub(r'(\d+)\s*%', r'\1 por ciento', s)
            s = re.sub(r'(\d+)\s*\+', r'\1 más', s)
        elif lang == "fr":
            s = re.sub(r'(\d+)\s*%', r'\1 pour cent', s)
            s = re.sub(r'(\d+)\s*\+', r'\1 plus', s)
        elif lang == "de":
            s = re.sub(r'(\d+)\s*%', r'\1 Prozent', s)
            s = re.sub(r'(\d+)\s*\+', r'\1 plus', s)
        elif lang == "it":
            s = re.sub(r'(\d+)\s*%', r'\1 percento', s)
            s = re.sub(r'(\d+)\s*\+', r'\1 più', s)

    elif lang == "ru":
        s = s.replace("«", '"').replace("»", '"').replace("„", '"')
        s = re.sub(r'(?<=\d)\s*(?=\d{3}\b)', '', s)
        s = re.sub(r'(\d+)\s*%', r'\1 процентов', s)
        s = re.sub(r'(\d+)\s*\+', r'\1 плюс', s)

    elif lang == "ar":
        # Strip decorative Arabic Tatweel/Kashida (\u0640) and normalize Eastern Arabic numerals (٠-٩)
        s = s.replace("\u0640", "")
        ar_digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
        s = s.translate(ar_digits)
        s = re.sub(r'(\d+)\s*[%٪]', r'\1 بالمئة', s)

    elif lang == "ja":
        # Normalize full-width wave dash and punctuation into standard Japanese pauses
        s = s.replace("〜", "、").replace("～", "、").replace("…", "、")

    # Clean up redundant spaces and duplicate punctuation
    s = re.sub(r',\s*,+', ',', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s

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

def get_f5_model(lang="en"):
    global _f5_instance, _current_loaded_lang
    init_environment()
    if not lang:
        lang = "en"
    lang = lang.strip().lower()

    with MODEL_LOCK:
        if _f5_instance is not None and _current_loaded_lang == lang:
            return _f5_instance

        if _f5_instance is not None and _current_loaded_lang != lang:
            print(f"[*] Switching F5-TTS model from '{_current_loaded_lang}' to '{lang}'...")
            _f5_instance = None
            _current_loaded_lang = None
            try:
                import gc
                gc.collect()
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                elif hasattr(torch, "mps") and hasattr(torch.mps, "empty_cache"):
                    torch.mps.empty_cache()
            except Exception:
                pass

        import torch
        from f5_tts.api import F5TTS
        patch_f5_tts_runtime()
        if torch.cuda.is_available():
            device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            device = "mps"
        else:
            device = "cpu"

        if os.path.exists(MODELS_DIR):
            os.environ["HF_HOME"] = os.path.abspath(MODELS_DIR)

        ckpt_path, vocab_path, model_arch = find_language_pack_paths(lang)
        _, vocoder_path = find_local_f5_paths()

        if not ckpt_path:
            if lang == "en":
                ckpt_path, vocoder_path = find_local_f5_paths()
                model_arch = "F5TTS_v1_Base"
                vocab_path = ""
            else:
                meta = SUPPORTED_LANGUAGES.get(lang, {})
                lang_display = meta.get("name", lang)
                raise RuntimeError(
                    f"Language pack '{lang_display}' ({lang}) is not installed. "
                    "Please download it from Easy Speech Pro Settings -> Language Packs."
                )

        if not vocoder_path:
            _, vocoder_path = find_local_f5_paths()

        print(f"[*] Initializing F5-TTS ({model_arch}) for '{lang}' on {device.upper()}...")
        if ckpt_path:
            print(f"[+] Using checkpoint: {ckpt_path}")
        if vocab_path:
            print(f"[+] Using vocab: {vocab_path}")
        if vocoder_path:
            print(f"[+] Using vocoder: {vocoder_path}")

        try:
            _f5_instance = F5TTS(
                model=model_arch,
                ckpt_file=ckpt_path or "",
                vocab_file=vocab_path or "",
                vocoder_local_path=vocoder_path,
                device=device
            )
        except Exception as init_err:
            if device == "mps":
                print(f"[!] MPS initialization error ({init_err}). Falling back to CPU...")
                device = "cpu"
                _f5_instance = F5TTS(
                    model=model_arch,
                    ckpt_file=ckpt_path or "",
                    vocab_file=vocab_path or "",
                    vocoder_local_path=vocoder_path,
                    device="cpu"
                )
            else:
                raise init_err

        _current_loaded_lang = lang
        print(f"[+] F5-TTS language '{lang}' initialized and ready!")
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
    global _f5_instance, _whisper_instance, _current_loaded_lang
    with MODEL_LOCK:
        has_loaded = (_f5_instance is not None or _whisper_instance is not None)
        _f5_instance = None
        _current_loaded_lang = None
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
    def _is_authorized_origin(self):
        """Blocks cross-site requests from external web pages while allowing CEP (file://, null) and localhost."""
        origin = (self.headers.get("Origin") or "").strip().lower()
        if not origin or origin in ("null", "file://"):
            return True
        if origin.startswith(("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")):
            return True
        return False

    def _send_cors_headers(self):
        origin = (self.headers.get("Origin") or "").strip()
        if origin and self._is_authorized_origin():
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "null")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-ES-Token, X-ES-Token-Ts, X-ES-Token-Bucket")

    def _verify_client_authorized(self, data):
        """Free distribution edition: Local client requests are automatically authorized."""
        return True, None

    def _send_json_response(self, code, payload):
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self._send_cors_headers()
            self.end_headers()
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass

    def do_OPTIONS(self):
        if not self._is_authorized_origin():
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        if not self._is_authorized_origin():
            self._send_json_response(403, {"success": False, "error": "Forbidden origin"})
            return

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
                "service": "Easy Speech Pro Voice Clone Expansion Pack",
                "version": "1.0.0",
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
        elif clean_path in ["/api/languages", "/languages"]:
            lang_list = []
            for code, meta in SUPPORTED_LANGUAGES.items():
                ckpt, vocab, _ = find_language_pack_paths(code)
                is_installed = bool(ckpt)
                size_on_disk_mb = 0
                if is_installed and os.path.exists(ckpt):
                    try:
                        size_on_disk_mb = round(os.path.getsize(ckpt) / (1024 * 1024), 1)
                    except Exception:
                        size_on_disk_mb = meta.get("size_mb", 1200)
                lang_list.append({
                    "code": code,
                    "name": meta["name"],
                    "flag": meta["flag"],
                    "installed": is_installed,
                    "active": (_current_loaded_lang == code),
                    "size_mb": meta.get("size_mb", 1200),
                    "size_on_disk_mb": size_on_disk_mb,
                    "can_delete": (code != "en" and is_installed)
                })
            self._send_json_response(200, {
                "success": True,
                "languages": lang_list,
                "current_language": _current_loaded_lang or "en"
            })
        elif clean_path in ["/api/languages/progress", "/languages/progress"]:
            with _download_lock:
                st = dict(_download_state)
            self._send_json_response(200, {"success": True, "download": st})
        else:
            self._send_json_response(404, {"error": "Not Found"})

    def do_POST(self):
        if not self._is_authorized_origin():
            self._send_json_response(403, {"success": False, "error": "Forbidden origin"})
            return

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

        # Protect synthesis, transcription, and audio processing endpoints with cryptographic license check
        if path in ["/api/generate", "/api/transcribe", "/api/convert"]:
            auth_ok, auth_err = self._verify_client_authorized(data)
            if not auth_ok:
                self._send_json_response(401, {"success": False, "error": auth_err or "Unauthorized: Valid Easy Speech Pro license required."})
                return

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

        # 1. Audio Conversion & Reference Pre-Conditioning Endpoint
        if path == "/api/convert":
            source_path = data.get("source_path") or data.get("audio_path")
            target_path = data.get("target_path")
            trim_new = bool(data.get("trim_for_new_profile", False))
            if not source_path:
                self._send_json_response(400, {"success": False, "error": "Missing source_path parameter"})
                return
            try:
                out_path = convert_audio_to_24k_mono_wav(source_path, target_path)
                orig_dur = 0.0
                try:
                    import soundfile as _sf
                    orig_dur = round(float(_sf.info(out_path).duration), 2)
                except Exception:
                    pass
                is_trimmed = False
                if trim_new and orig_dur > 9.0:
                    try:
                        import soundfile as _sf
                        import shutil
                        newprof_path = os.path.splitext(out_path)[0] + "_newprof.wav"
                        shutil.copy2(out_path, newprof_path)
                        _, trimmed_dur = condition_reference_wav(newprof_path, trim_over_9_5s=True)
                        out_path = newprof_path
                        is_trimmed = bool(trimmed_dur < orig_dur)
                    except Exception:
                        pass
                dur = orig_dur
                try:
                    import soundfile as _sf
                    dur = round(float(_sf.info(out_path).duration), 2)
                except Exception:
                    pass
                clean_str = out_path.replace("\\", "/")
                self._send_json_response(200, {
                    "success": True,
                    "target_path": clean_str,
                    "clean_audio_path": clean_str,
                    "duration": dur,
                    "orig_duration": orig_dur,
                    "is_trimmed": is_trimmed
                })
            except ValueError as ve:
                self._send_json_response(400, {"success": False, "error": str(ve)})
            except Exception as e:
                self._send_json_response(500, {"success": False, "error": str(e)})

        # 2. Reference Audio Transcription Endpoint (Protected by MODEL_LOCK)
        elif path == "/api/transcribe":
            audio_path = data.get("audio_path")
            if not audio_path or not os.path.exists(audio_path):
                self._send_json_response(400, {"success": False, "error": f"Audio file not found: {audio_path}"})
                return
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
                # Always convert & condition reference audio into internal 24kHz cache
                clean_audio = convert_audio_to_24k_mono_wav(audio_path)

                with MODEL_LOCK:
                    wm = get_whisper_model()
                    req_lang = (data.get("lang") or "en").strip().lower()
                    whisper_lang = req_lang if req_lang in ["en", "es", "fr", "de", "hi", "it", "ja", "ru", "ar", "zh", "fi", "lv"] else None
                    transcribe_kwargs = {
                        "language": whisper_lang,
                        "task": "transcribe",
                        "condition_on_previous_text": False
                    }
                    if whisper_lang == "hi":
                        transcribe_kwargs["initial_prompt"] = "यह हिंदी बातचीत है।"
                    segments, info = wm.transcribe(clean_audio, **transcribe_kwargs)
                    seg_list = [s for s in segments if s.text.strip()]
                    total_dur = getattr(info, "duration", 0)

                    # Guard against F5-TTS 12s ceiling: if reference audio exceeds 9.5s,
                    # select complete sentence segments within <= 9.2s and save both audio and transcript.
                    if total_dur > 9.5 and seg_list:
                        selected_segs = []
                        for s in seg_list:
                            if s.end <= 9.2:
                                selected_segs.append(s)
                            elif not selected_segs and s.start < 9.2:
                                selected_segs.append(s)
                                break
                            else:
                                break
                        if selected_segs and selected_segs[-1].end < total_dur:
                            cutoff_sec = min(selected_segs[-1].end + 0.16, total_dur)
                            import soundfile as sf
                            import numpy as np
                            a_data, a_sr = sf.read(clean_audio, dtype="float32")
                            trimmed_data = a_data[:int(cutoff_sec * a_sr)]
                            fade_out_len = min(int(0.035 * a_sr), len(trimmed_data) // 4)
                            if fade_out_len > 0:
                                trimmed_data[-fade_out_len:] *= np.linspace(1.0, 0.0, fade_out_len, dtype=np.float32)
                            trimmed_data = np.concatenate([trimmed_data, np.zeros(int(0.16 * a_sr), dtype=np.float32)])
                            opt_path = get_optref_cache_path(clean_audio)
                            sf.write(opt_path, trimmed_data, a_sr, subtype="PCM_16")
                            clean_audio = opt_path
                            text = " ".join([s.text.strip() for s in selected_segs]).strip()
                            try:
                                with open(opt_path + ".txt", "w", encoding="utf-8") as tf:
                                    tf.write(text)
                            except Exception:
                                pass
                            total_dur = round(float(len(trimmed_data)) / float(a_sr), 2)
                        else:
                            text = " ".join([seg.text.strip() for seg in seg_list]).strip()
                    else:
                        text = " ".join([seg.text.strip() for seg in seg_list]).strip()

                    import re
                    # Remove hallucinated foreign script artifacts if language is Hindi
                    if req_lang == "hi":
                        text = re.sub(r'[\u0600-\u06FF\u0400-\u04FF\u0E00-\u0E7F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF\u1100-\u11FF]', '', text)
                        text = re.sub(r'\s+', ' ', text).strip()
                    # Automatically normalize typography, accents, and punctuation for target language
                    text = normalize_tts_text(text, lang=req_lang)

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
            except ValueError as ve:
                self._send_json_response(400, {"success": False, "error": str(ve)})
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
            raw_text = (data.get("text") or "").strip()
            ref_audio = data.get("ref_audio")
            ref_text = (data.get("ref_text") or "").strip()
            speed = float(data.get("speed", 1.0))
            output_path = data.get("output_path")
            nfe_step = int(data.get("nfe_step", 32))  # 32 steps: optimal studio fidelity / speed balance
            lang = (data.get("lang") or "en").strip().lower()
            # Adaptive CFG strength: 2.0 prevents over-guidance/metallic harshness on non-English & short scripts
            default_cfg = 2.0 if (lang != "en" or len(raw_text) < 25) else 2.2
            cfg_strength = float(data.get("cfg_strength", default_cfg))

            if not raw_text:
                self._send_json_response(400, {"success": False, "error": "Empty text provided for synthesis."})
                return
            if not ref_audio or not os.path.exists(ref_audio):
                self._send_json_response(400, {"success": False, "error": f"Reference audio not found: {ref_audio}"})
                return

            # Validate script alphabet against target language model vocabulary
            is_valid_script, script_err = validate_script_for_language(raw_text, lang=lang)
            if not is_valid_script:
                self._send_json_response(400, {"success": False, "error": script_err})
                return

            # Normalize text for studio pronunciation (numbers, URLs, acronyms, em-dashes, punctuation)
            text = normalize_tts_text(raw_text, lang=lang)
            if not text:
                self._send_json_response(400, {"success": False, "error": "Text contains no pronounceable words."})
                return

            if ref_text:
                ref_text = normalize_tts_text(ref_text, lang=lang)

            try:
                output_path = validate_safe_audio_output_path(output_path, default_dir=OUTPUT_DIR)
            except ValueError as path_ve:
                self._send_json_response(400, {"success": False, "error": str(path_ve)})
                return

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
                import numpy as np
                import re

                # Always ensure reference audio is conditioned 24kHz mono WAV (with studio RMS & tail pad)
                if os.path.abspath(os.path.dirname(ref_audio)) == os.path.abspath(CLONE_CACHE_DIR) and os.path.basename(ref_audio).endswith(("_clean24k.wav", "_optref.wav")):
                    clean_ref_audio = ref_audio
                else:
                    clean_ref_audio = convert_audio_to_24k_mono_wav(ref_audio)

                # Duration & Transcript Synchronization Guard:
                # If reference audio was > 9.8s (or if an _optref.wav + .txt pair exists), ensure both
                # audio AND ref_text match the trimmed segment so leftover words never bleed into synthesis!
                try:
                    opt_path = get_optref_cache_path(clean_ref_audio)
                    opt_txt_path = opt_path + ".txt"
                    ref_info = sf.info(clean_ref_audio)

                    if os.path.exists(opt_path) and os.path.exists(opt_txt_path):
                        clean_ref_audio = opt_path
                        if not ref_text:
                            try:
                                cached_rt = open(opt_txt_path, "r", encoding="utf-8").read().strip()
                                if cached_rt:
                                    ref_text = cached_rt
                            except Exception:
                                pass
                    elif ref_info.duration > 9.2:
                        with MODEL_LOCK:
                            wm = get_whisper_model()
                            whisper_lang = lang if lang in ["en", "es", "fr", "de", "hi", "it", "ja", "ru", "ar", "zh", "fi", "lv"] else None
                            segs, _ = wm.transcribe(clean_ref_audio, language=whisper_lang, condition_on_previous_text=False)
                            s_list = [s for s in segs if s.text.strip()]
                            _whisper_instance = None
                        if s_list:
                            selected_segs = []
                            for s in s_list:
                                if s.end <= 9.2:
                                    selected_segs.append(s)
                                elif not selected_segs and s.start < 9.2:
                                    selected_segs.append(s)
                                    break
                                else:
                                    break
                            if not selected_segs:
                                selected_segs = [s_list[0]]
                            cutoff = min(selected_segs[-1].end + 0.16, 9.2, ref_info.duration)
                            a_data, a_sr = sf.read(clean_ref_audio, dtype="float32")
                            trimmed_data = a_data[:int(cutoff * a_sr)]
                            fade_out_len = min(int(0.035 * a_sr), len(trimmed_data) // 4)
                            if fade_out_len > 0:
                                trimmed_data[-fade_out_len:] *= np.linspace(1.0, 0.0, fade_out_len, dtype=np.float32)
                            trimmed_data = np.concatenate([trimmed_data, np.zeros(int(0.16 * a_sr), dtype=np.float32)])
                            sf.write(opt_path, trimmed_data, a_sr, subtype="PCM_16")
                            clean_ref_audio = opt_path
                            trimmed_rt = " ".join([s.text.strip() for s in selected_segs]).strip()
                            if lang == "hi":
                                trimmed_rt = re.sub(r'[\u0600-\u06FF\u0400-\u04FF\u0E00-\u0E7F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF\u1100-\u11FF]', '', trimmed_rt)
                                trimmed_rt = re.sub(r'\s+', ' ', trimmed_rt).strip()
                            trimmed_rt = normalize_tts_text(trimmed_rt, lang=lang)
                            if trimmed_rt:
                                ref_text = trimmed_rt
                                try:
                                    with open(opt_txt_path, "w", encoding="utf-8") as tf:
                                        tf.write(ref_text)
                                except Exception:
                                    pass
                except Exception as dur_guard_err:
                    print(f"[!] Reference duration guard note: {dur_guard_err}")

                # Auto-transcribe if reference text was not provided
                if not ref_text:
                    with MODEL_LOCK:
                        wm = get_whisper_model()
                        whisper_lang = lang if lang in ["en", "es", "fr", "de", "hi", "it", "ja", "ru", "ar", "zh", "fi", "lv"] else None
                        transcribe_kwargs = {
                            "language": whisper_lang,
                            "condition_on_previous_text": False
                        }
                        segs, _ = wm.transcribe(clean_ref_audio, **transcribe_kwargs)
                        ref_text = " ".join([s.text.strip() for s in segs]).strip()
                        if lang == "hi":
                            ref_text = re.sub(r'[\u0600-\u06FF\u0400-\u04FF\u0E00-\u0E7F\u4E00-\u9FFF\u3040-\u30FF\uAC00-\uD7AF\u1100-\u11FF]', '', ref_text)
                            ref_text = re.sub(r'\s+', ' ', ref_text).strip()
                        ref_text = normalize_tts_text(ref_text, lang=lang)
                        # Release whisper instance immediately
                        _whisper_instance = None
                        try:
                            import gc
                            gc.collect()
                            if torch.cuda.is_available():
                                torch.cuda.empty_cache()
                        except Exception:
                            pass

                os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

                with MODEL_LOCK:
                    f5 = get_f5_model(lang=lang)
                    # Native speaking speed without artificial scaling offsets (prevents boundary drift & word leakage)
                    effective_speed = float(speed)
                    try:
                        with torch.inference_mode():
                            samples, sample_rate, _ = f5.infer(
                                ref_file=clean_ref_audio,
                                ref_text=ref_text,
                                gen_text=text,
                                speed=effective_speed,
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

                # Studio Broadcast Mastering: DC offset removal, silence edge trim, bidirectional -16.5 dBFS RMS
                # normalization, and transparent soft-knee true-peak limiting (-0.5 dBFS ceiling)
                samples = master_synthesized_audio(samples, sample_rate)

                sf.write(output_path, samples, sample_rate, subtype="PCM_16")
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
                    "normalized_text": text,
                    "engine": "f5_clone",
                    "lang": lang
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

        # 4. Language Pack Download Endpoint
        elif path == "/api/languages/download":
            target_lang = (data.get("lang") or "").strip().lower()
            if not target_lang or target_lang not in SUPPORTED_LANGUAGES:
                self._send_json_response(400, {"success": False, "error": f"Invalid or unsupported language: {target_lang}"})
                return
            with _download_lock:
                if _download_state.get("status") == "downloading":
                    self._send_json_response(409, {
                        "success": False,
                        "error": f"Another download is already in progress ({_download_state.get('lang')}). Please wait for it to finish."
                    })
                    return
            threading.Thread(target=download_language_pack_worker, args=(target_lang,), daemon=True).start()
            self._send_json_response(200, {
                "success": True,
                "message": f"Download started for '{target_lang}'.",
                "lang": target_lang
            })

        # 5. Language Pack Delete Endpoint
        elif path == "/api/languages/delete":
            target_lang = (data.get("lang") or "").strip().lower()
            if not target_lang or target_lang == "en":
                self._send_json_response(400, {"success": False, "error": "Cannot delete default English language pack."})
                return
            with MODEL_LOCK:
                if _current_loaded_lang == target_lang:
                    unload_models()
            target_dir = os.path.join(LANGUAGES_DIR, target_lang)
            if os.path.exists(target_dir):
                import shutil
                shutil.rmtree(target_dir, ignore_errors=True)
                self._send_json_response(200, {"success": True, "message": f"Language pack '{target_lang}' deleted."})
            else:
                self._send_json_response(200, {"success": True, "message": "Language pack was not installed."})

        # 6. Unload / VRAM Cleanup Endpoint
        elif path == "/api/unload":
            unload_models()
            self._send_json_response(200, {"success": True, "message": "Models unloaded and VRAM cleared."})

        # 7. Clean Shutdown Endpoint
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
    print("      Easy Speech Pro - Voice Clone Expansion Service")
    print("=" * 65)
    print(f"[*] Listening on:     http://{HOST}:{PORT}")
    print(f"[*] Health endpoint:  http://{HOST}:{PORT}/health")
    if PARENT_PID:
        print(f"[*] Guarding Parent:  PID {PARENT_PID}")
    print("=" * 65)

    try:
        ThreadingHTTPServer.allow_reuse_address = True
        server = ThreadingHTTPServer((HOST, PORT), VoiceCloneRequestHandler)
    except OSError as e:
        in_use_errnos = (getattr(errno, 'EADDRINUSE', 48), 48, 98, 10048)
        if getattr(e, 'errno', None) in in_use_errnos or "address already in use" in str(e).lower():
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
