import os
import sys
import shutil
import subprocess
import platform

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT_PATH = os.path.join(ROOT_DIR, "backend", "server.py")
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform.startswith("win")

if IS_MAC:
    mac_arch = "arm64" if platform.machine().lower() in ["arm64", "aarch64"] else "x64"
    ENGINE_DIR = os.path.join(ROOT_DIR, "bin", "mac", f"engine_{mac_arch}")
    BIN_NAME = "engine"
elif IS_WIN:
    ENGINE_DIR = os.path.join(ROOT_DIR, "bin", "win", "engine")
    BIN_NAME = "engine.exe"
else:
    ENGINE_DIR = os.path.join(ROOT_DIR, "bin", "linux", "engine")
    BIN_NAME = "engine"

BUILD_DIR = os.path.join(ROOT_DIR, "_build_tmp")
DIST_DIR = os.path.join(ROOT_DIR, "_dist_tmp")
SPEC_PATH = os.path.join(ROOT_DIR, "engine.spec")

def clean_temp_dirs():
    for p in [BUILD_DIR, DIST_DIR]:
        if os.path.exists(p):
            shutil.rmtree(p, ignore_errors=True)
    if os.path.exists(SPEC_PATH):
        try:
            os.remove(SPEC_PATH)
        except Exception:
            pass

def build_standalone():
    print("=" * 60)
    print("Easy Speech - Standalone Engine Compiler")
    print(f"Target OS: {'macOS' if IS_MAC else ('Windows' if IS_WIN else 'Linux')}")
    if IS_MAC:
        print(f"Architecture: {platform.machine()} -> {mac_arch}")
    print(f"Engine Output Directory: {ENGINE_DIR}")
    print("=" * 60)

    clean_temp_dirs()

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name", "engine",
        "--workpath", BUILD_DIR,
        "--distpath", DIST_DIR,
        "--specpath", ROOT_DIR,
        "--collect-all", "kokoro_onnx",
        "--collect-all", "espeakng_loader",
        "--collect-all", "phonemizer",
        "--collect-all", "piper",
        "--collect-all", "soundfile",
        "--collect-all", "onnxruntime",
        "--copy-metadata", "kokoro-onnx",
        "--copy-metadata", "piper-tts",
        "--hidden-import", "kokoro_onnx",
        "--hidden-import", "piper",
        "--hidden-import", "soundfile",
        "--hidden-import", "numpy",
        SCRIPT_PATH
    ]

    print(f"[*] Running PyInstaller compilation...")
    res = subprocess.run(cmd)
    if res.returncode != 0:
        print(f"[!] Compilation failed with return code {res.returncode}")
        sys.exit(res.returncode)

    built_engine_dir = os.path.join(DIST_DIR, "engine")
    if not os.path.exists(built_engine_dir):
        print(f"[!] Expected output folder not found at: {built_engine_dir}")
        sys.exit(1)

    # Prepare destination directory
    if os.path.exists(ENGINE_DIR):
        print(f"[*] Purging previous engine folder at {ENGINE_DIR}...")
        shutil.rmtree(ENGINE_DIR, ignore_errors=True)
    os.makedirs(os.path.dirname(ENGINE_DIR), exist_ok=True)

    print(f"[*] Moving compiled engine to target directory: {ENGINE_DIR}...")
    shutil.move(built_engine_dir, ENGINE_DIR)

    target_binary = os.path.join(ENGINE_DIR, BIN_NAME)
    if not os.path.exists(target_binary):
        print(f"[!] Built binary not found at {target_binary}")
        sys.exit(1)

    # Post-build adjustments for macOS
    if IS_MAC:
        print("[*] Setting executable permissions and codesigning for macOS...")
        # 1. Grant execute permissions
        try:
            subprocess.run(["chmod", "-R", "+x", ENGINE_DIR], check=True)
            print("[✓] Granted execute permissions to engine directory.")
        except Exception as e:
            print(f"[!] Warning granting permissions: {e}")

        # 2. Ad-hoc codesign binary and internal dynamic libraries
        try:
            subprocess.run(["codesign", "--force", "--deep", "--sign", "-", target_binary], check=True)
            print(f"[✓] Ad-hoc signed {target_binary}")
        except Exception as e:
            print(f"[!] Warning ad-hoc signing: {e}")

    # Clean up temp build dirs
    clean_temp_dirs()

    print("\n" + "=" * 60)
    print(f"[✓] Successfully built Easy Speech standalone engine at:\n    {ENGINE_DIR}")
    print("=" * 60)

if __name__ == "__main__":
    build_standalone()
