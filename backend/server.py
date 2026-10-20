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

def run_cli():
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Easy Speech One-Shot Synthesizer")
    parser.add_argument("--cli", action="store_true", help="Run in CLI one-shot mode")
    parser.add_argument("--text", type=str, default="", help="Text to synthesize")
    parser.add_argument("--text-file", type=str, default=None, help="Path to UTF-8 text file containing script")
    parser.add_argument("--voice", type=str, default="af_heart", help="Voice ID")
    parser.add_argument("--engine", type=str, default="kokoro", choices=["kokoro", "piper", "f5_clone"], help="TTS engine")
    parser.add_argument("--ref-audio", type=str, default=None, help="Reference audio path for voice cloning")
    parser.add_argument("--ref-text", type=str, default=None, help="Spoken transcript of reference audio (optional)")
    parser.add_argument("--speed", type=float, default=1.0, help="Speaking speed")
    parser.add_argument("--output", type=str, default=None, help="Explicit output wav path")
    parser.add_argument("--result-json", type=str, default=None, help="Path to write JSON metadata")

    args, _ = parser.parse_known_args()

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

                # Ensure clean 24k wav if non-wav
                ext = os.path.splitext(ref_audio)[1].lower()
                clean_ref = ref_audio
                if ext != ".wav":
                    try:
                        import imageio_ffmpeg, subprocess
                        ffmpeg_bin = imageio_ffmpeg.get_ffmpeg_exe()
                        cache_dir = os.path.join(BASE_DIR, "cloned_profiles", ".cache")
                        os.makedirs(cache_dir, exist_ok=True)
                        clean_ref = os.path.join(cache_dir, os.path.splitext(os.path.basename(ref_audio))[0] + "_24k.wav")
                        subprocess.run([ffmpeg_bin, "-y", "-i", ref_audio, "-vn", "-ar", "24000", "-ac", "1", clean_ref], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    except Exception:
                        clean_ref = ref_audio

                ref_text = (args.ref_text or "").strip()
                if not ref_text:
                    try:
                        from faster_whisper import WhisperModel
                        wm = WhisperModel("tiny", device="cpu", compute_type="int8")
                        segs, _ = wm.transcribe(clean_ref)
                        ref_text = " ".join([s.text for s in segs]).strip()
                    except Exception:
                        ref_text = ""

                import torch
                with torch.inference_mode():
                    samples, sample_rate, _ = f5.infer(
                        ref_file=clean_ref,
                        ref_text=ref_text,
                        gen_text=text,
                        speed=args.speed,
                        nfe_step=32,
                        cfg_strength=2.5
                    )

                if samples is None or len(samples) == 0:
                    raise RuntimeError("Voice cloning inference returned empty audio.")

                # Studio Peak Normalization: prevent digital clipping/distortion
                max_amp = float(np.max(np.abs(samples)))
                if max_amp > 0.95:
                    samples = samples * (0.95 / max_amp)

                sf.write(output_filepath, samples, sample_rate)
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
