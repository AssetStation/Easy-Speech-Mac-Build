import os
import sys
import multiprocessing

multiprocessing.freeze_support()

# Ensure valid file handles for windowless execution and dual-stream logging
if getattr(sys, 'frozen', False):
    try:
        _dir = os.path.dirname(os.path.abspath(sys.executable))
        _log_path = os.path.join(_dir, "engine.log")
        if os.path.exists(_log_path) and os.path.getsize(_log_path) > 2 * 1024 * 1024:
            try:
                with open(_log_path, "rb") as _rf:
                    _rf.seek(-100 * 1024, os.SEEK_END)
                    _tail = _rf.read()
                with open(_log_path, "wb") as _wf:
                    _wf.write(_tail)
            except Exception: pass
        _log = open(_log_path, "a", encoding="utf-8", buffering=1)

        class _TeeStream:
            def __init__(self, file_stream, console_stream):
                self.file_stream = file_stream
                self.console_stream = console_stream
            def write(self, s):
                try:
                    if self.file_stream: self.file_stream.write(s)
                except Exception: pass
                try:
                    if self.console_stream: self.console_stream.write(s)
                except Exception: pass
            def flush(self):
                try:
                    if self.file_stream: self.file_stream.flush()
                except Exception: pass
                try:
                    if self.console_stream: self.console_stream.flush()
                except Exception: pass

        sys.stdout = _TeeStream(_log, sys.__stdout__)
        sys.stderr = _TeeStream(_log, sys.__stderr__)
    except Exception:
        pass
else:
    if sys.stdout is None:
        try: sys.stdout = open(os.devnull, "w", encoding="utf-8")
        except Exception: pass
    if sys.stderr is None:
        try: sys.stderr = open(os.devnull, "w", encoding="utf-8")
        except Exception: pass

import time
import uuid
import hmac
import hashlib
import numpy as np
import soundfile as sf

def find_base_dir():
    if getattr(sys, 'frozen', False):
        curr = os.path.dirname(os.path.abspath(sys.executable))
    else:
        curr = os.path.dirname(os.path.abspath(__file__))

    for _ in range(5):
        if os.path.exists(os.path.join(curr, "models")):
            return curr
        parent = os.path.dirname(curr)
        if parent == curr:
            break
        curr = parent
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = find_base_dir()

MODELS_DIR = os.path.join(BASE_DIR, "models")
OUTPUT_DIR = os.path.join(BASE_DIR, "audio_outputs")
try:
    os.makedirs(OUTPUT_DIR, exist_ok=True)
except Exception:
    pass

# Global model instances
kokoro_instance = None
piper_voices = {}
f5_instance = None

# Ensure portable ffmpeg in PATH for audio conversion if available
try:
    import imageio_ffmpeg
    _ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
    _ffmpeg_dir = os.path.dirname(_ffmpeg_bin)
    if _ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _ffmpeg_dir + os.path.pathsep + os.environ.get("PATH", "")
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        from pydub import AudioSegment
        AudioSegment.converter = _ffmpeg_bin
except Exception:
    if sys.platform == "darwin":
        for _mp in ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/local/bin/ffmpeg"]:
            if os.path.exists(_mp) and os.access(_mp, os.X_OK):
                _ffmpeg_bin = _mp
                _ffmpeg_dir = os.path.dirname(_mp)
                if _ffmpeg_dir not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = _ffmpeg_dir + os.path.pathsep + os.environ.get("PATH", "")
                break

def get_f5_model():
    global f5_instance
    if f5_instance is None:
        try:
            import torch
            from f5_tts.api import F5TTS
            try:
                # Apply multilingual character-level runtime patches
                sys.path.insert(0, os.path.join(BASE_DIR, "VoiceClonePack"))
                from service import patch_f5_tts_runtime
                patch_f5_tts_runtime()
            except Exception:
                pass
            device = "cuda" if torch.cuda.is_available() else "cpu"
            f5_instance = F5TTS(device=device)
        except ImportError as e:
            raise RuntimeError(
                "Voice Cloning dependencies (PyTorch/F5-TTS) are not installed in the base plugin. "
                "Please launch the Voice Clone Service from the extension settings or VoiceClonePack folder."
            ) from e
    return f5_instance

def get_kokoro():
    global kokoro_instance
    if kokoro_instance is None:
        from kokoro_onnx import Kokoro
        model_path = os.path.join(MODELS_DIR, "kokoro-v1.0.onnx")
        voices_path = os.path.join(MODELS_DIR, "voices-v1.0.bin")
        if not os.path.exists(model_path) or not os.path.exists(voices_path):
            raise FileNotFoundError("Kokoro model files not found in models/ folder.")
        kokoro_instance = Kokoro(model_path, voices_path)
    return kokoro_instance

def get_piper_voice(model_name="en_US-lessac-medium"):
    global piper_voices
    if model_name not in piper_voices:
        from piper import PiperVoice
        model_path = os.path.join(MODELS_DIR, f"{model_name}.onnx")
        config_path = os.path.join(MODELS_DIR, f"{model_name}.onnx.json")
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Piper model {model_name} not found in models/ folder.")
        piper_voices[model_name] = PiperVoice.load(model_path, config_path=config_path)
    return piper_voices[model_name]

# ===========================================================================
# 🛡️ CRYPTOGRAPHIC RUNTIME SECURITY & ENTANGLEMENT ENGINE
# ===========================================================================
PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEFXqgmcQCRgBFT0fyOYPlztPAYvpU
cXpHytneusbAk74qfrmdBIA8UZXb2bKO1yiE7ce5kF2UJk9ErPqzbRHYxQ==
-----END PUBLIC KEY-----"""

def get_system_machine_guid():
    import platform
    import subprocess
    try:
        if platform.system() == 'Windows':
            out = subprocess.check_output(['reg', 'query', r'HKLM\SOFTWARE\Microsoft\Cryptography', '/v', 'MachineGuid'], stderr=subprocess.DEVNULL)
            for line in out.decode().splitlines():
                if 'MachineGuid' in line:
                    parts = line.strip().split()
                    return parts[-1].strip()
        elif platform.system() == 'Darwin':
            out = subprocess.check_output(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'], stderr=subprocess.DEVNULL)
            for line in out.decode().splitlines():
                if 'IOPlatformUUID' in line:
                    parts = line.split('=')
                    if len(parts) > 1:
                        return "".join(c for c in parts[1].strip().strip('"') if c.isalnum())
    except Exception:
        pass
    return ""

def unseal_device_identity():
    import platform
    import hashlib
    import json
    if platform.system() == 'Darwin':
        path = os.path.expanduser('~/Library/Application Support/EasySpeech/.device_identity')
    else:
        appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
        path = os.path.join(appdata, 'EasySpeech', '.device_identity')
    if not os.path.exists(path):
        return ""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = json.load(f)
        guid = get_system_machine_guid()
        if not guid or 'data' not in raw or 'iv' not in raw or 'tag' not in raw:
            return ""
        key = hashlib.pbkdf2_hmac('sha256', guid.encode('utf-8'), b'ES_CACHE_SALT_#8492_PRO', 2000, 32)
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(key)
        ciphertext = bytes.fromhex(raw['data']) + bytes.fromhex(raw['tag'])
        iv = bytes.fromhex(raw['iv'])
        plaintext = aesgcm.decrypt(iv, ciphertext, None)
        return plaintext.decode('utf-8').strip()
    except Exception:
        return ""

def get_hardware_id():
    import platform
    import subprocess
    try:
        if platform.system() == 'Windows':
            out = subprocess.check_output(['powershell', '-NoProfile', '-Command', '(Get-CimInstance Win32_BaseBoard).SerialNumber'], stderr=subprocess.DEVNULL)
            hwid = "".join(c for c in out.decode().strip() if c.isalnum())
            is_zeroes = all(c == '0' for c in hwid) if hwid else False
            is_placeholder = any(hwid.lower().startswith(p) for p in ['none', 'default', 'tobedefined', 'tobefilled', 'baseboard', 'unknown']) if hwid else False
            is_generic = not hwid or len(hwid) < 3 or is_zeroes or is_placeholder
            if is_generic:
                guid = get_system_machine_guid()
                if guid and len(guid) >= 4:
                    hwid = guid
                else:
                    try:
                        cpu_disk = subprocess.check_output(['powershell', '-NoProfile', '-Command', '(Get-CimInstance Win32_Processor).ProcessorId; (Get-CimInstance Win32_DiskDrive | Select-Object -First 1).SerialNumber'], stderr=subprocess.DEVNULL)
                        lines = ["".join(c for c in l if c.isalnum()) for l in cpu_disk.decode().splitlines() if l.strip()]
                        if lines:
                            hwid = hashlib.sha256(":".join(lines).encode('utf-8')).hexdigest()[:32].upper()
                    except Exception:
                        pass
            if not hwid or len(hwid) < 4:
                unsealed = unseal_device_identity()
                if unsealed and len(unsealed) >= 4:
                    hwid = unsealed
                else:
                    hwid = 'WIN_' + (os.environ.get('COMPUTERNAME') or 'DEVICE')
            return hwid
        else:
            out = subprocess.check_output(['/usr/sbin/ioreg', '-rd1', '-c', 'IOPlatformExpertDevice'], stderr=subprocess.DEVNULL)
            hwid = ""
            for line in out.decode().splitlines():
                if 'IOPlatformUUID' in line:
                    parts = line.split('=')
                    if len(parts) > 1:
                        hwid = "".join(c for c in parts[1].strip().strip('"') if c.isalnum())
                        break
            if not hwid or len(hwid) < 4:
                unsealed = unseal_device_identity()
                if unsealed and len(unsealed) >= 4:
                    hwid = unsealed
                else:
                    hwid = 'MAC_' + (os.environ.get('USER') or 'DEVICE')
            return hwid
    except Exception:
        unsealed = unseal_device_identity()
        if unsealed and len(unsealed) >= 4:
            return unsealed
        return 'UNKNOWN_DEVICE'

def load_license_cache(hwid):
    import platform
    import json
    if platform.system() == 'Darwin':
        path = os.path.expanduser('~/Library/Application Support/EasySpeech/license_cache.json')
    else:
        appdata = os.environ.get('APPDATA') or os.path.expanduser('~')
        path = os.path.join(appdata, 'EasySpeech', 'license_cache.json')
    if not os.path.exists(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read().strip()
        if not raw:
            return None
        if raw.startswith('{'):
            try:
                parsed_env = json.loads(raw)
                if 'iv' in parsed_env and 'tag' in parsed_env and 'data' in parsed_env:
                    key = hashlib.pbkdf2_hmac('sha256', hwid.encode('utf-8'), b'ES_CACHE_SALT_#8492_PRO', 10000, 32)
                    try:
                        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                        aesgcm = AESGCM(key)
                        ciphertext = bytes.fromhex(parsed_env['data']) + bytes.fromhex(parsed_env['tag'])
                        iv = bytes.fromhex(parsed_env['iv'])
                        plaintext = aesgcm.decrypt(iv, ciphertext, None)
                        return json.loads(plaintext.decode('utf-8'))
                    except Exception:
                        return None
            except Exception:
                pass
        # Legacy XOR
        text = ""
        for i in range(0, len(raw), 2):
            char_code = int(raw[i:i+2], 16) ^ ord(hwid[(i // 2) % len(hwid)])
            text += chr(char_code)
        return json.loads(text)
    except Exception:
        return None

def get_os_monotonic_watermark():
    import platform
    if platform.system() == 'Windows':
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\EasySpeech")
            val, _ = winreg.QueryValueEx(key, "LastActiveWatermark")
            winreg.CloseKey(key)
            return int(val)
        except Exception:
            return 0
    else:
        try:
            wm_path = os.path.expanduser('~/.easyspeech_wm')
            if os.path.exists(wm_path):
                with open(wm_path, 'r', encoding='utf-8') as f:
                    return int(f.read().strip())
        except Exception:
            return 0
    return 0

def verify_execution_session(token=None, timestamp=None, bucket=None, handshake_json=None):
    import json
    if handshake_json:
        try:
            if os.path.exists(handshake_json):
                with open(handshake_json, 'r', encoding='utf-8') as hf:
                    data = json.load(hf)
            else:
                data = json.loads(handshake_json)
            if isinstance(data, dict):
                token = token or data.get("token")
                timestamp = timestamp or data.get("timestamp")
                bucket = bucket if bucket is not None else data.get("bucket")
        except Exception:
            pass

    if not token or not timestamp:
        return {"valid": False, "error": "Please activate a valid Easy Speech Pro license to synthesize speech."}

    now_ms = time.time() * 1000
    if abs(now_ms - timestamp) > 15 * 60 * 1000:
        return {"valid": False, "error": "Session timed out. Please click Generate Voiceover again."}

    hwid = get_hardware_id()
    cache = load_license_cache(hwid)
    if not cache:
        return {"valid": False, "error": "Please activate a valid Easy Speech Pro license."}

    signed_payload_str = cache.get("signed_payload")
    signature_hex = cache.get("signature")
    if not signed_payload_str or not signature_hex:
        return {"valid": False, "error": "License verification failed: unauthenticated or corrupted session."}

    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        pub_key = serialization.load_pem_public_key(PUBLIC_KEY_PEM.encode('utf-8'))
        sig_bytes = bytes.fromhex(signature_hex)
        if len(sig_bytes) != 64:
            return {"valid": False, "error": "License verification failed: invalid signature format."}
        r = int.from_bytes(sig_bytes[:32], 'big')
        s = int.from_bytes(sig_bytes[32:], 'big')
        der_sig = encode_dss_signature(r, s)
        pub_key.verify(der_sig, signed_payload_str.encode('utf-8'), ec.ECDSA(hashes.SHA256()))

        payload = json.loads(signed_payload_str)
        if payload.get("product") not in ("Easy Speech Pro", "Easy Speech"):
            return {"valid": False, "error": "This license is not valid for Easy Speech Pro."}
        if payload.get("hwid") != hwid:
            return {"valid": False, "error": "This license is already active on another device."}

        # Validate license expiration against signed payload
        exp_str = payload.get("exp")
        if exp_str:
            try:
                from datetime import datetime
                exp_clean = exp_str.replace("Z", "+00:00")
                exp_dt = datetime.fromisoformat(exp_clean)
                exp_ts_ms = exp_dt.timestamp() * 1000
                is_trial = payload.get("type") == "trial"
                grace_ms = 0 if is_trial else 48 * 3600 * 1000
                if now_ms > (exp_ts_ms + grace_ms):
                    return {"valid": False, "error": "License expired. Please renew your Easy Speech Pro license."}
            except Exception:
                return {"valid": False, "error": "License verification failed: invalid expiration date."}
    except Exception:
        return {"valid": False, "error": "License verification failed: cryptographic signature untrusted or tampered."}

    seed = cache.get("entanglement_seed")
    if not seed:
        return {"valid": False, "error": "License session corrupted. Please reactivate your license in settings."}

    current_bucket = int(time.time() // (60 * 15))
    valid = False
    candidates = [current_bucket, current_bucket - 1, current_bucket + 1]
    if bucket is not None and bucket not in candidates:
        return {"valid": False, "error": "Session token expired. Please retry."}

    for b in candidates:
        for prod in ("Easy Speech Pro", "Easy Speech"):
            data = f"{hwid}:{b}:{prod}".encode('utf-8')
            expected_token = hmac.new(seed.encode('utf-8'), data, hashlib.sha256).hexdigest()
            if hmac.compare_digest(expected_token, token):
                valid = True
                break
        if valid:
            break

    if not valid:
        return {"valid": False, "error": "License verification failed. Please reactivate your license in settings."}

    cache_date = cache.get("cache_date")
    last_active = cache.get("last_active_timestamp") or cache_date
    os_watermark = get_os_monotonic_watermark()
    highest_watermark = max(last_active or 0, os_watermark or 0)

    if cache_date and (cache_date > now_ms + 3600000 or now_ms - cache_date < -3600000):
        return {"valid": False, "error": "System clock anomaly detected. Please ensure your clock is set correctly."}
    if highest_watermark > 0 and (highest_watermark - now_ms > 3600000):
        return {"valid": False, "error": "System clock anomaly detected. Please ensure your clock is set correctly."}

    return {"valid": True}

def run_cli():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Easy Speech Pro One-Shot Synthesizer")
    parser.add_argument("--cli", action="store_true", help="Run in CLI one-shot mode")
    parser.add_argument("--text", type=str, default="", help="Text to synthesize")
    parser.add_argument("--text-file", type=str, default=None, help="Path to UTF-8 text file containing script")
    parser.add_argument("--voice", type=str, default="af_heart", help="Voice ID")
    parser.add_argument("--engine", type=str, default="kokoro", choices=["kokoro", "piper", "f5_clone"], help="TTS engine")
    parser.add_argument("--ref-audio", type=str, default=None, help="Reference audio path for voice cloning")
    parser.add_argument("--ref-text", type=str, default=None, help="Spoken transcript of reference audio (optional)")
    parser.add_argument("--speed", type=float, default=1.0, help="Speaking speed")
    parser.add_argument("--lang", type=str, default="en", help="Language code for synthesis")
    parser.add_argument("--output", type=str, default=None, help="Explicit output wav path")
    parser.add_argument("--result-json", type=str, default=None, help="Path to write JSON metadata")
    parser.add_argument("--token", type=str, default=None, help="Execution security token")
    parser.add_argument("--token-ts", type=float, default=None, help="Execution token timestamp")
    parser.add_argument("--token-bucket", type=int, default=None, help="Execution token bucket")
    parser.add_argument("--handshake-json", type=str, default=None, help="JSON string or path of security handshake")

    args, _ = parser.parse_known_args()

    # Cryptographic license token verification
    chk = verify_execution_session(args.token, args.token_ts, args.token_bucket, args.handshake_json)
    if not chk.get("valid"):
        err_res = {"success": False, "error": chk.get("error", "Unauthorized: Valid Easy Speech Pro license required.")}
        if args.result_json:
            try:
                with open(args.result_json, "w", encoding="utf-8") as f:
                    json.dump(err_res, f)
            except Exception: pass
        print(json.dumps(err_res))
        sys.exit(1)

    text = ""
    if args.text_file and os.path.exists(args.text_file):
        try:
            with open(args.text_file, "r", encoding="utf-8") as tf:
                text = tf.read().strip()
        except Exception:
            text = args.text.strip()
    else:
        text = args.text.strip()

    if not text:
        err_res = {"success": False, "error": "Empty text provided."}
        if args.result_json:
            try:
                with open(args.result_json, "w", encoding="utf-8") as f:
                    json.dump(err_res, f)
            except Exception: pass
        print(json.dumps(err_res))
        sys.exit(1)

    t0 = time.time()

    if args.output:
        output_filepath = args.output
        output_filename = os.path.basename(output_filepath)
    else:
        # Default filename format: {model}_{voice}_{input_first_8_char}_{timestamp}.wav
        safe_engine = str(args.engine or "kokoro").lower()
        import re
        raw_v = str(args.voice or "voice")
        clean_v = re.sub(r'^[a-zA-Z]{2}_', '', raw_v)
        clean_v = re.sub(r'^[a-zA-Z]{2}_[a-zA-Z]{2}-', '', clean_v)
        clean_v = re.sub(r'-medium|-low|-high', '', clean_v, flags=re.I)
        clean_v = re.sub(r'[\\/:*?"<>|\'"`~#%&*{}+=;:,!@$^()\[\]]', '', clean_v)
        clean_v = re.sub(r'\s+', '_', clean_v).lower().strip() or "voice"

        clean_snippet = re.sub(r'[\r\n\t\0]', ' ', text)
        clean_snippet = re.sub(r'[\\/:*?"<>|\'"`~#%&*{}+=;:,!@$^()\[\]]', '', clean_snippet)
        clean_snippet = re.sub(r'\s+', ' ', clean_snippet).strip()[:8].strip() or "speech"

        ts = int(time.time() * 1000)
        output_filename = f"{safe_engine}_{clean_v}_{clean_snippet}_{ts}.wav"
        output_filepath = os.path.join(OUTPUT_DIR, output_filename)

    os.makedirs(os.path.dirname(os.path.abspath(output_filepath)), exist_ok=True)

    try:
        if args.engine == "kokoro":
            kokoro = get_kokoro()
            lang = "en-us"
            if args.voice.startswith(("bf_", "bm_")): lang = "en-gb"
            elif args.voice.startswith("ff_"): lang = "fr-fr"
            elif args.voice.startswith(("jf_", "jm_")): lang = "ja"
            elif args.voice.startswith(("zf_", "zm_")): lang = "cmn"
            elif args.voice.startswith(("ef_", "em_")): lang = "es"
            elif args.voice.startswith(("if_", "im_")): lang = "it"
            elif args.voice.startswith(("pf_", "pm_")): lang = "pt-br"
            elif args.voice.startswith(("hf_", "hm_")): lang = "hi"

            samples, sample_rate = kokoro.create(text, voice=args.voice, speed=args.speed, lang=lang)
            if samples is None or len(samples) == 0:
                raise RuntimeError("Kokoro synthesis returned empty audio. Check that text is supported.")
            sf.write(output_filepath, samples, sample_rate)
            duration = float(len(samples)) / float(sample_rate)

        elif args.engine == "piper":
            from piper.config import SynthesisConfig
            voice = get_piper_voice(args.voice)
            length_scale = 1.0 / max(0.2, min(float(args.speed), 3.0))
            syn_cfg = SynthesisConfig(length_scale=length_scale)
            chunks = list(voice.synthesize(text, syn_config=syn_cfg))
            if not chunks:
                raise RuntimeError("Piper synthesis returned no audio.")
            all_audio = np.concatenate([c.audio_float_array for c in chunks])
            if len(all_audio) == 0:
                raise RuntimeError("Piper produced empty audio array.")
            sample_rate = chunks[0].sample_rate
            sf.write(output_filepath, all_audio, sample_rate)
            duration = float(len(all_audio)) / float(sample_rate)

        elif args.engine == "f5_clone":
            ref_audio = args.ref_audio
            if not ref_audio or not os.path.exists(ref_audio):
                raise FileNotFoundError(f"Reference audio not found for voice cloning: {ref_audio}")

            # 1. Attempt to delegate to the persistent high-speed voice clone daemon (Option A)
            handled_by_daemon = False
            try:
                import urllib.request
                import json as _json
                daemon_req = {
                    "text": text,
                    "ref_audio": ref_audio,
                    "ref_text": args.ref_text or "",
                    "speed": args.speed,
                    "lang": getattr(args, "lang", "en") or "en",
                    "output_path": output_filepath
                }
                req = urllib.request.Request(
                    "http://127.0.0.1:8765/api/generate",
                    data=_json.dumps(daemon_req).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                # Generous 300s timeout to allow long paragraphs to complete synthesis smoothly
                with urllib.request.urlopen(req, timeout=300) as daemon_resp:
                    res_obj = _json.loads(daemon_resp.read().decode("utf-8"))
                    if res_obj.get("success"):
                        duration = float(res_obj.get("duration", 0.0))
                        handled_by_daemon = True
            except Exception:
                handled_by_daemon = False

            # 2. If daemon is not running, fallback to direct local inference if dependencies are present
            if not handled_by_daemon:
                f5 = get_f5_model()

                # Ensure clean 24k wav and smart pause-trim if reference audio exceeds 9.2s
                ext = os.path.splitext(ref_audio)[1].lower()
                clean_ref = ref_audio
                cache_dir = os.path.join(BASE_DIR, "cloned_profiles", ".cache")
                os.makedirs(cache_dir, exist_ok=True)

                if ext != ".wav":
                    try:
                        import imageio_ffmpeg, subprocess
                        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
                        clean_ref = os.path.join(cache_dir, os.path.splitext(os.path.basename(ref_audio))[0] + "_24k.wav")
                        subprocess.run([
                            ffmpeg_bin, "-y", "-i", ref_audio,
                            "-t", "25", "-vn",
                            "-af", "highpass=f=75,lowpass=f=11500",
                            "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le",
                            clean_ref
                        ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    except Exception:
                        clean_ref = ref_audio

                try:
                    ref_data, ref_sr = sf.read(clean_ref, dtype="float32")
                    if ref_data.ndim > 1:
                        ref_data = np.mean(ref_data, axis=1)
                    ref_dur = len(ref_data) / float(ref_sr)
                    if ref_dur > 9.0:
                        s_start = int(4.5 * ref_sr)
                        s_end = min(len(ref_data), int(9.2 * ref_sr))
                        p_win = int(0.12 * ref_sr)
                        p_step = int(0.03 * ref_sr)
                        b_cut = int(8.8 * ref_sr)
                        min_e = float("inf")
                        for pos in range(s_start, max(s_start + p_step, s_end - p_win), p_step):
                            seg = ref_data[pos:pos + p_win]
                            e = float(np.mean(np.square(seg)))
                            if e < min_e:
                                min_e = e
                                b_cut = pos + (p_win // 2)
                        ref_data = ref_data[:b_cut]
                        f_out = min(int(0.035 * ref_sr), len(ref_data) // 4)
                        if f_out > 0:
                            ref_data[-f_out:] *= np.linspace(1.0, 0.0, f_out, dtype=np.float32)
                        safe_trimmed = os.path.join(cache_dir, os.path.splitext(os.path.basename(clean_ref))[0] + "_safe9s.wav")
                        sf.write(safe_trimmed, ref_data, ref_sr, subtype="PCM_16")
                        clean_ref = safe_trimmed
                    else:
                        # Check if audio was severed mid-word/speech and trim at previous pause
                        tail_len = min(int(0.12 * ref_sr), len(ref_data))
                        if tail_len > 0 and len(ref_data) >= int(2.8 * ref_sr):
                            t_rms = float(np.sqrt(np.mean(np.square(ref_data[-tail_len:]))))
                            p_peak = float(np.max(np.abs(ref_data)))
                            if p_peak > 0.05 and t_rms > (p_peak * 0.22):
                                b_start = len(ref_data) - int(0.20 * ref_sr)
                                b_limit = max(int(1.5 * ref_sr), len(ref_data) - int(1.8 * ref_sr))
                                p_win = int(0.10 * ref_sr)
                                p_step = int(0.025 * ref_sr)
                                b_pause = None
                                min_pe = float("inf")
                                for pos in range(b_start, b_limit, -p_step):
                                    seg = ref_data[pos - p_win:pos]
                                    e = float(np.mean(np.square(seg)))
                                    if e < min_pe:
                                        min_pe = e
                                        b_pause = pos - (p_win // 2)
                                if b_pause is not None and min_pe < (t_rms ** 2) * 0.40:
                                    ref_data = ref_data[:b_pause]
                                    f_out = min(int(0.035 * ref_sr), len(ref_data) // 4)
                                    if f_out > 0:
                                        ref_data[-f_out:] *= np.linspace(1.0, 0.0, f_out, dtype=np.float32)
                                    safe_cut = os.path.join(cache_dir, os.path.splitext(os.path.basename(clean_ref))[0] + "_cleancut.wav")
                                    sf.write(safe_cut, ref_data, ref_sr, subtype="PCM_16")
                                    clean_ref = safe_cut
                except Exception:
                    pass

                ref_text = re.sub(r'[\-—–\.\,\s]+$', '', (args.ref_text or "").strip()).strip()
                if ref_text and not ref_text.endswith((".", "!", "?", "।", "。")):
                    ref_text += "."
                if not ref_text:
                    try:
                        from faster_whisper import WhisperModel
                        wm = WhisperModel("tiny", device="cpu", compute_type="int8")
                        segs, _ = wm.transcribe(clean_ref)
                        ref_text = " ".join([s.text for s in segs]).strip()
                    except Exception:
                        ref_text = ""

                # Basic typography normalization for clean prosody
                norm_text = re.sub(r'\s*([—–])\s*', ', ', text)
                norm_text = re.sub(r'[""„«»]', '"', norm_text)
                norm_text = re.sub(r'\.{2,}', '...', norm_text)

                import torch
                with torch.inference_mode():
                    samples, sample_rate, _ = f5.infer(
                        ref_file=clean_ref,
                        ref_text=ref_text,
                        gen_text=norm_text,
                        speed=args.speed,
                        nfe_step=32,
                        cfg_strength=2.5
                    )

                if samples is None or len(samples) == 0:
                    raise RuntimeError("Voice cloning inference returned empty audio.")

                # Studio Broadcast Loudness Normalization (-16.5 dBFS active RMS) + Soft-Knee Peak Limiter
                samples = np.asarray(samples, dtype=np.float32)
                samples = samples - float(np.mean(samples))
                abs_s = np.abs(samples)
                active = samples[abs_s > 0.008]
                cur_rms = float(np.sqrt(np.mean(active ** 2))) if len(active) > int(sample_rate * 0.05) else float(np.sqrt(np.mean(samples ** 2)))
                if cur_rms > 1e-5:
                    gain = min(max(0.15 / cur_rms, 0.25), 8.0)
                    samples = samples * gain
                peak = float(np.max(np.abs(samples)))
                if peak > 0.85:
                    over = np.abs(samples) - 0.85
                    compressed = np.where(
                        np.abs(samples) > 0.85,
                        np.sign(samples) * (0.85 + 0.09 * np.tanh(over / 0.09)),
                        samples
                    )
                    samples = compressed.astype(np.float32)
                    max_p = float(np.max(np.abs(samples)))
                    if max_p > 0.94:
                        samples = samples * (0.94 / max_p)

                sf.write(output_filepath, samples, sample_rate, subtype="PCM_16")
                duration = float(len(samples)) / float(sample_rate)

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
        else:
            raise ValueError(f"Unknown engine: {args.engine}")

        elapsed = time.time() - t0
        result = {
            "success": True,
            "filename": output_filename,
            "absolute_path": os.path.abspath(output_filepath).replace("\\", "/"),
            "duration": round(duration, 2),
            "generation_time": round(elapsed, 2),
            "engine": args.engine,
            "voice": args.voice,
            "text": text
        }

        json_path = args.result_json or (os.path.splitext(output_filepath)[0] + ".json")
        try:
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(result, f, indent=2)
        except Exception: pass

        print(json.dumps(result))
        sys.exit(0)
    except Exception as e:
        import traceback
        err_msg = str(e)
        res = {"success": False, "error": err_msg, "traceback": traceback.format_exc()}
        if args.result_json:
            try:
                with open(args.result_json, "w", encoding="utf-8") as f:
                    json.dump(res, f, indent=2)
            except Exception: pass
        print(json.dumps(res))
        sys.exit(1)

if __name__ == "__main__":
    run_cli()
