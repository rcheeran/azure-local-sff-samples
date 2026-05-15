#!/usr/bin/env python3
"""Install missing Python packages required by cobotpoc.py."""

import importlib.util
import os
import platform
import pwd
import re
import subprocess
import shutil
import sys
import tempfile
from ctypes.util import find_library


# Map import module names to pip package specifiers.
REQUIRED = [
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("torch", "torch"),
    ("cv2", "opencv-python"),
    ("sounddevice", "sounddevice"),
    ("soundfile", "soundfile"),
    ("transformers", "transformers"),
    ("nemo", "nemo_toolkit[asr]"),
]


def is_installed(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def has_nvidia_gpu() -> bool:
    gpu_info_path = "/proc/driver/nvidia/gpus"
    if os.path.isdir(gpu_info_path) and os.listdir(gpu_info_path):
        return True

    if shutil.which("lspci"):
        result = subprocess.run(
            ["lspci"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "nvidia" in result.stdout.lower()

    return False


def has_nvidia_driver() -> bool:
    return os.path.exists("/proc/driver/nvidia/version")


def read_char_device_major(device_name: str) -> int | None:
    try:
        with open("/proc/devices", "r", encoding="utf-8") as handle:
            for line in handle:
                parts = line.strip().split()
                if len(parts) == 2 and parts[1] == device_name and parts[0].isdigit():
                    return int(parts[0])
    except OSError:
        return None

    return None


def list_nvidia_gpu_minors() -> list[int]:
    gpu_info_root = "/proc/driver/nvidia/gpus"
    if not os.path.isdir(gpu_info_root):
        return []

    minors: list[int] = []
    for entry in os.listdir(gpu_info_root):
        info_path = os.path.join(gpu_info_root, entry, "information")
        try:
            with open(info_path, "r", encoding="utf-8") as handle:
                info_text = handle.read()
        except OSError:
            continue

        match = re.search(r"Device Minor:\s*(\d+)", info_text)
        if match:
            minors.append(int(match.group(1)))

    return sorted(set(minors))


def ensure_char_device(path: str, major: int, minor: int) -> None:
    if os.path.exists(path):
        return

    cmd = ["mknod", "-m", "666", path, "c", str(major), str(minor)]
    if os.geteuid() != 0:
        cmd.insert(0, "sudo")
    subprocess.check_call(cmd)


def ensure_nvidia_gpu_ready() -> None:
    if platform.system().lower() != "linux":
        print("Skipping NVIDIA GPU readiness check: only implemented for Linux.")
        return

    if not has_nvidia_gpu():
        print("No NVIDIA GPU detected.")
        return

    if not has_nvidia_driver():
        raise RuntimeError(
            "NVIDIA GPU detected, but the NVIDIA driver is not loaded. Install or start the NVIDIA driver, then rerun this script."
        )

    if os.path.exists("/dev/nvidia0") and os.path.exists("/dev/nvidiactl"):
        print("NVIDIA device nodes are already present.")
        return

    print("NVIDIA GPU detected; ensuring device nodes are present...")

    if shutil.which("nvidia-modprobe"):
        modprobe_cmd = ["nvidia-modprobe", "-u", "-c=0"]
        if os.geteuid() != 0:
            modprobe_cmd.insert(0, "sudo")
        subprocess.run(modprobe_cmd, check=False)

    if os.path.exists("/dev/nvidia0") and os.path.exists("/dev/nvidiactl"):
        print("NVIDIA device nodes created successfully.")
        return

    nvidia_major = read_char_device_major("nvidia")
    if nvidia_major is None:
        raise RuntimeError(
            "NVIDIA GPU detected, but the NVIDIA character device major could not be determined from /proc/devices."
        )

    gpu_minors = list_nvidia_gpu_minors()
    if not gpu_minors:
        raise RuntimeError(
            "NVIDIA GPU detected, but no GPU minors were found under /proc/driver/nvidia/gpus."
        )

    for minor in gpu_minors:
        ensure_char_device(f"/dev/nvidia{minor}", nvidia_major, minor)
    ensure_char_device("/dev/nvidiactl", nvidia_major, 255)

    if os.path.exists("/dev/nvidia0") and os.path.exists("/dev/nvidiactl"):
        print("NVIDIA device nodes created successfully.")
        return

    raise RuntimeError(
        "NVIDIA GPU detected, but device node creation did not succeed."
    )


def ensure_video_group_access() -> None:
    if platform.system().lower() != "linux":
        print("Skipping video group access check: only implemented for Linux.")
        return

    video_devices = [path for path in ("/dev/video0", "/dev/video1") if os.path.exists(path)]
    if not video_devices:
        print("No /dev/video* devices found; skipping video group access check.")
        return

    if os.geteuid() == 0:
        print("Running as root; video group access check is not required.")
        return

    username = pwd.getpwuid(os.getuid()).pw_name
    group_result = subprocess.run(
        ["id", "-nG", username],
        capture_output=True,
        text=True,
        check=True,
    )
    group_names = set(group_result.stdout.split())
    if "video" in group_names:
        print(f"User '{username}' is already in the video group.")
        return

    if not shutil.which("usermod"):
        raise RuntimeError(
            "The current user is not in the video group, but 'usermod' is unavailable. Add the user to the video group manually, then rerun this script."
        )

    print(f"Adding user '{username}' to the video group for camera access...")
    subprocess.check_call(["sudo", "usermod", "-aG", "video", username])
    print(f"User '{username}' added to the video group.")

    # Activate the video group in the current process so a re-login is not required.
    import grp
    video_gid = grp.getgrnam("video").gr_gid
    current_groups = os.getgroups()
    if video_gid not in current_groups:
        try:
            os.setgroups(current_groups + [video_gid])
            print("Video group activated in the current process.")
        except PermissionError:
            print("Could not activate video group in current process. Start a new login session before using the camera.")


def ensure_sound_drivers() -> None:
    """Ensure kernel sound modules are installed and loaded so ALSA can detect USB audio devices."""
    if platform.system().lower() != "linux":
        print("Skipping sound driver check: only implemented for Linux.")
        return

    # Check if ALSA already sees sound cards.
    if os.path.exists("/proc/asound/cards"):
        print("Sound drivers are already loaded.")
        return

    # Try loading snd-usb-audio in case the module exists but isn't loaded.
    subprocess.run(["sudo", "modprobe", "snd-usb-audio"], check=False)
    if os.path.exists("/proc/asound/cards"):
        print("Sound driver loaded successfully via modprobe.")
        return

    # The module isn't available; try to install the kernel sound drivers package.
    kernel_version = platform.release()
    print(f"Sound driver module not found; attempting to install kernel-drivers-sound for {kernel_version}...")

    sound_pkg_installed = False
    if shutil.which("dnf"):
        result = subprocess.run(
            ["sudo", "dnf", "install", "-y", f"kernel-drivers-sound-{kernel_version}"],
            check=False,
        )
        sound_pkg_installed = result.returncode == 0
    elif shutil.which("apt-get"):
        subprocess.run(["sudo", "apt-get", "update"], check=False)
        result = subprocess.run(
            ["sudo", "apt-get", "install", "-y", "linux-modules-extra-" + kernel_version],
            check=False,
        )
        sound_pkg_installed = result.returncode == 0

    if not sound_pkg_installed:
        raise RuntimeError(
            "Could not install kernel sound drivers. Install the sound driver package for your kernel manually, then rerun this script."
        )

    # Load the newly installed module.
    subprocess.check_call(["sudo", "modprobe", "snd-usb-audio"])

    if os.path.exists("/proc/asound/cards"):
        print("Kernel sound drivers installed and loaded successfully.")
    else:
        raise RuntimeError(
            "Kernel sound drivers were installed but ALSA still does not detect any sound cards. Check that a USB audio device is connected."
        )


def ensure_audio_group_access() -> None:
    """Ensure the current user is in the 'audio' group for access to /dev/snd/* devices."""
    if platform.system().lower() != "linux":
        print("Skipping audio group access check: only implemented for Linux.")
        return

    if not os.path.exists("/dev/snd"):
        print("No /dev/snd directory found; skipping audio group access check.")
        return

    if os.geteuid() == 0:
        print("Running as root; audio group access check is not required.")
        return

    username = pwd.getpwuid(os.getuid()).pw_name
    group_result = subprocess.run(
        ["id", "-nG", username],
        capture_output=True,
        text=True,
        check=True,
    )
    group_names = set(group_result.stdout.split())
    if "audio" in group_names:
        print(f"User '{username}' is already in the audio group.")
        return

    if not shutil.which("usermod"):
        raise RuntimeError(
            "The current user is not in the audio group, but 'usermod' is unavailable. Add the user to the audio group manually, then rerun this script."
        )

    print(f"Adding user '{username}' to the audio group for microphone access...")
    subprocess.check_call(["sudo", "usermod", "-aG", "audio", username])
    print(f"User '{username}' added to the audio group.")

    # Activate the audio group in the current process so a re-login is not required.
    import grp
    audio_gid = grp.getgrnam("audio").gr_gid
    current_groups = os.getgroups()
    if audio_gid not in current_groups:
        try:
            os.setgroups(current_groups + [audio_gid])
            print("Audio group activated in the current process.")
        except PermissionError:
            print("Could not activate audio group in current process. Start a new login session before using the microphone.")


def ensure_portaudio() -> None:
    if platform.system().lower() != "linux":
        print("Skipping PortAudio auto-install: only implemented for Linux.")
        return

    if find_library("portaudio"):
        print("PortAudio is already available.")
        return

    installers = [
        ("dnf", ["sudo", "dnf", "install", "-y", "portaudio", "portaudio-devel"]),
        ("apt-get", ["sudo", "apt-get", "update"], ["sudo", "apt-get", "install", "-y", "libportaudio2", "portaudio19-dev"]),
        ("yum", ["sudo", "yum", "install", "-y", "portaudio", "portaudio-devel"]),
        ("pacman", ["sudo", "pacman", "-S", "--noconfirm", "portaudio"]),
        ("apk", ["sudo", "apk", "add", "portaudio", "portaudio-dev"]),
    ]

    for entry in installers:
        manager = entry[0]
        if not shutil.which(manager):
            continue

        print(f"PortAudio not found; installing via {manager}...")
        try:
            if manager == "apt-get":
                subprocess.check_call(entry[1])
                subprocess.check_call(entry[2])
            else:
                subprocess.check_call(entry[1])
        except subprocess.CalledProcessError as exc:
            print(f"PortAudio install via {manager} failed: {exc}. Trying source build fallback...")
            continue

        if find_library("portaudio"):
            print("PortAudio installed successfully.")
            return

    build_portaudio_from_source()


def install_system_packages(packages: list[str]) -> bool:
    if shutil.which("dnf"):
        cmd = ["sudo", "dnf", "install", "-y", *packages]
    elif shutil.which("apt-get"):
        subprocess.check_call(["sudo", "apt-get", "update"])
        cmd = ["sudo", "apt-get", "install", "-y", *packages]
    elif shutil.which("yum"):
        cmd = ["sudo", "yum", "install", "-y", *packages]
    elif shutil.which("apk"):
        cmd = ["sudo", "apk", "add", *packages]
    elif shutil.which("pacman"):
        cmd = ["sudo", "pacman", "-S", "--noconfirm", *packages]
    else:
        return False

    subprocess.check_call(cmd)
    return True


def ensure_build_tools() -> None:
    if shutil.which("gcc") and shutil.which("make"):
        if shutil.which("file") and os.path.exists("/usr/include/linux/limits.h") and (not shutil.which("dnf") or os.path.exists("/usr/lib/crt1.o")):
            return

    print("Build tools are missing; installing compiler toolchain...")
    for candidate in (["gcc", "make", "glibc-devel", "kernel-headers", "file"], ["build-essential"], ["build-base"], ["gcc", "make", "file"]):
        try:
            if install_system_packages(candidate):
                break
        except subprocess.CalledProcessError:
            continue

    if not (shutil.which("gcc") and shutil.which("make") and shutil.which("file") and os.path.exists("/usr/include/linux/limits.h")):
        raise RuntimeError(
            "Could not install build tools required for PortAudio. Install gcc, make, file, and Linux kernel headers manually, then rerun this script."
        )


def build_portaudio_from_source() -> None:
    if find_library("portaudio"):
        return

    print("Falling back to building PortAudio from source...")
    ensure_build_tools()

    # Optional Linux audio headers improve build success for PortAudio.
    for packages in (["alsa-lib-devel"], ["libasound2-dev"]):
        try:
            if install_system_packages(list(packages)):
                break
        except subprocess.CalledProcessError:
            continue

    source_url = "https://files.portaudio.com/archives/pa_stable_v190700_20210406.tgz"
    with tempfile.TemporaryDirectory(prefix="portaudio-build-") as tmpdir:
        archive_path = os.path.join(tmpdir, "portaudio.tgz")
        source_dir = os.path.join(tmpdir, "portaudio-src")

        download_cmd = None
        if shutil.which("curl"):
            download_cmd = ["curl", "-L", source_url, "-o", archive_path]
        elif shutil.which("wget"):
            download_cmd = ["wget", source_url, "-O", archive_path]
        else:
            raise RuntimeError(
                "Neither curl nor wget is available to download PortAudio source. Install one of them and rerun this script."
            )

        subprocess.check_call(download_cmd)
        os.makedirs(source_dir, exist_ok=True)
        subprocess.check_call(["tar", "-xzf", archive_path, "-C", source_dir, "--strip-components=1"])

        configure_cmd = ["./configure"]
        if os.geteuid() != 0:
            configure_cmd.append("--prefix=/usr/local")

        configure_env = os.environ.copy()
        configure_env.setdefault("CPP", "gcc -E")
        subprocess.check_call(configure_cmd, cwd=source_dir, env=configure_env)
        subprocess.check_call(["make", "-j2"], cwd=source_dir)
        install_cmd = ["make", "install"]
        if os.geteuid() != 0:
            install_cmd.insert(0, "sudo")
        subprocess.check_call(install_cmd, cwd=source_dir)

    ldconfig_cmd = ["ldconfig"]
    if os.geteuid() != 0:
        ldconfig_cmd.insert(0, "sudo")
    subprocess.check_call(ldconfig_cmd)

    if not find_library("portaudio"):
        raise RuntimeError(
            "PortAudio source build completed, but the library was still not detected after running ldconfig. Check that /usr/local/lib is in the dynamic linker path, then rerun this script."
        )

    print("PortAudio installed successfully from source.")


def ensure_pip() -> None:
    try:
        import pip  # noqa: F401
        return
    except Exception:
        pass

    # Try Python's built-in pip bootstrap first.
    try:
        import ensurepip

        print("pip is missing; attempting to bootstrap with ensurepip...")
        ensurepip.bootstrap(upgrade=True)
    except Exception:
        pass


def install(packages: list[str]) -> None:
    ensure_pip()
    if not is_installed("pip"):
        raise RuntimeError(
            "pip is not available. Install it first (for example: sudo dnf install -y python3-pip), then rerun this script."
        )

    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", *packages]
    print("Installing:", ", ".join(packages))
    subprocess.check_call(cmd)


def main() -> int:
    ensure_nvidia_gpu_ready()
    ensure_video_group_access()
    ensure_sound_drivers()
    ensure_audio_group_access()
    ensure_portaudio()

    missing = [pip_name for module_name, pip_name in REQUIRED if not is_installed(module_name)]

    if not missing:
        print("All required packages are already installed.")
        return 0

    print("Missing packages detected:", ", ".join(missing))
    install(missing)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
