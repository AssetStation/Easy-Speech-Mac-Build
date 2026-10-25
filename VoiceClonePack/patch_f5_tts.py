"""
EasySpeech VoiceClonePack - F5-TTS Core Stability Patcher
Ensures that utils_infer.py in the active environment is permanently patched for:
1. Native character-based duration estimation (prevents duration drift, missing words, and reference audio leakage).
2. Clean boundary spacing between reference audio and generated text across all character sets (Devanagari, Latin, CJK, Cyrillic, Arabic).
3. Character-aware batch weighting for fix_duration allocations.
"""

import os
import sys
import re
import site
import sysconfig
import py_compile

def find_utils_infer_path():
    candidate_dirs = []
    try:
        candidate_dirs.append(sysconfig.get_path('purelib'))
        candidate_dirs.append(sysconfig.get_path('platlib'))
    except Exception:
        pass
    if hasattr(site, 'getsitepackages'):
        try:
            candidate_dirs.extend(site.getsitepackages())
        except Exception:
            pass
    if hasattr(site, 'getusersitepackages'):
        try:
            candidate_dirs.append(site.getusersitepackages())
        except Exception:
            pass
    for p in sys.path:
        if 'site-packages' in p:
            candidate_dirs.append(p)

    for base in candidate_dirs:
        if base and os.path.isdir(base):
            target = os.path.join(base, "f5_tts", "infer", "utils_infer.py")
            if os.path.exists(target):
                return os.path.abspath(target)

    # Fallback to direct import
    try:
        import f5_tts.infer.utils_infer as _ui
        return os.path.abspath(_ui.__file__)
    except Exception:
        pass
    return None

def apply_f5_tts_disk_patch():
    target_path = find_utils_infer_path()
    if not target_path or not os.path.isfile(target_path):
        print("[!] Warning: Could not locate f5_tts/infer/utils_infer.py on disk. Disk patch skipped.")
        return False

    try:
        with open(target_path, "r", encoding="utf-8") as f:
            code = f.read()

        # Check if already fully patched
        if "ref_char_len = max(1, len(ref_text.strip()))" in code and "weights = [max(1, len(c.strip())) for c in gen_text_batches]" in code:
            print(f"[OK] utils_infer.py is already patched: {target_path}")
            return True

        print(f"[*] Patching utils_infer.py at: {target_path}")

        # 1. Replace UTF-8 byte duration calculation in _infer_basic
        old_dur_pattern = r'ref_text_len\s*=\s*len\(ref_text\.encode\("utf-8"\)\)\s*\n\s*gen_text_len\s*=\s*len\(gen_text\.encode\("utf-8"\)\)\s*\n\s*duration\s*=\s*ref_audio_len\s*\+\s*int\(ref_audio_len\s*/\s*ref_text_len\s*\*\s*gen_text_len\s*/\s*local_speed\)'
        new_dur_code = (
            'ref_char_len = max(1, len(ref_text.strip()))\n'
            '            gen_char_len = max(1, len(gen_text.strip()))\n'
            '            duration = ref_audio_len + int(ref_audio_len / ref_char_len * gen_char_len / local_speed)'
        )
        if re.search(old_dur_pattern, code):
            code = re.sub(old_dur_pattern, new_dur_code, code, count=1)
        else:
            code = code.replace(
                'ref_text_len = len(ref_text.encode("utf-8"))\n            gen_text_len = len(gen_text.encode("utf-8"))\n            duration = ref_audio_len + int(ref_audio_len / ref_text_len * gen_text_len / local_speed)',
                new_dur_code
            )

        # 2. Replace single-byte ref_text trailing space check
        old_space_check = r'if\s+len\(ref_text\[-1\]\.encode\("utf-8"\)\)\s*==\s*1:\s*\n\s*ref_text\s*=\s*ref_text\s*\+\s*" "'
        new_space_check = 'if not ref_text.endswith(" "):\n        ref_text = ref_text + " "'
        if re.search(old_space_check, code):
            code = re.sub(old_space_check, new_space_check, code, count=1)
        else:
            code = code.replace(
                'if len(ref_text[-1].encode("utf-8")) == 1:\n        ref_text = ref_text + " "',
                new_space_check
            )

        # 3. Replace text_list joining in _infer_basic
        code = code.replace(
            'text_list = [ref_text + gen_text]',
            'text_list = [(ref_text.strip() + " " + gen_text.strip()).strip()]'
        )

        # 4. Replace weights calculation in infer_batch_process
        code = code.replace(
            'weights = [len(c.encode("utf-8")) for c in gen_text_batches]',
            'weights = [max(1, len(c.strip())) for c in gen_text_batches]'
        )

        # 5. In infer_process: character-based max_chars
        old_max_chars = r'max_chars\s*=\s*int\(len\(ref_text\.encode\("utf-8"\)\)\s*/\s*\(audio\.shape\[-1\]\s*/\s*sr\)\s*\*\s*\(25\s*-\s*audio\.shape\[-1\]\s*/\s*sr\)\s*\*\s*speed\)'
        new_max_chars = (
            'ref_char_count = max(1, len(ref_text.strip()))\n'
            '    ref_dur_sec = max(0.5, audio.shape[-1] / sr)\n'
            '    max_chars = int(ref_char_count / ref_dur_sec * max(4.0, (22 - ref_dur_sec)) * float(speed))'
        )
        if re.search(old_max_chars, code):
            code = re.sub(old_max_chars, new_max_chars, code, count=1)

        # Backup original before writing
        backup_path = target_path + ".orig"
        if not os.path.exists(backup_path):
            try:
                import shutil
                shutil.copy2(target_path, backup_path)
            except Exception:
                pass

        # Write patched code
        with open(target_path, "w", encoding="utf-8") as f:
            f.write(code)

        # Verify syntax compilation
        py_compile.compile(target_path, doraise=True)
        print(f"[SUCCESS] Patched and verified utils_infer.py successfully.")
        return True
    except Exception as e:
        print(f"[!] Error patching utils_infer.py: {e}")
        return False

if __name__ == "__main__":
    success = apply_f5_tts_disk_patch()
    sys.exit(0 if success else 1)
