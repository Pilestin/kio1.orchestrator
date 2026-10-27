"""What this computer can run, and which models suit it.

Choosing a local model is the first decision a new user faces and the one
they are least equipped to make: a model too large for the memory crawls or
fails, one too small plans badly. This module looks at the machine — memory,
processor, a graphics card if there is one, whether Ollama is installed and
running — and suggests models from a short list known to work with KIO1.

Only the standard library: it must answer before anything else is set up.
"""

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

_GIB = 1024**3


@dataclass(frozen=True)
class ModelSuggestion:
    """A local model KIO1 works with.

    Attributes:
        name: The Ollama tag.
        size_gb: Download and memory size, roughly.
        min_ram_gb: Memory below which it is too slow or fails, on a CPU.
        role: `planning` for a fast model that follows the plan format,
            `answers` for one that reads results well, `both`.
        note: One line on what it is like to use.
    """

    name: str
    size_gb: float
    min_ram_gb: float
    role: str
    note: str


# Ordered small to large. Sizes are the default quantization's.
SUGGESTIONS: tuple[ModelSuggestion, ...] = (
    ModelSuggestion(
        "ministral-3:3b", 3.0, 8, "planning", "Fast; plans well, answers weakly."
    ),
    ModelSuggestion(
        "qwen3.5:4b", 3.4, 8, "answers", "Slower; reads results carefully."
    ),
    ModelSuggestion(
        "llama3.1:8b", 4.9, 16, "both", "Good at both; wants 16 GB or a GPU."
    ),
    ModelSuggestion("qwen2.5:14b", 9.0, 32, "both", "Strong; needs a GPU or 32 GB."),
)


@dataclass(frozen=True)
class Hardware:
    """What the machine has.

    Attributes:
        system: Operating system name.
        cpus: Logical processors.
        ram_total_gb: Installed memory, or None when it could not be read.
        ram_free_gb: Memory available now, or None.
        gpu: A detected NVIDIA card's name and memory, or empty.
        gpu_memory_gb: That card's memory, or None.
    """

    system: str
    cpus: int
    ram_total_gb: float | None
    ram_free_gb: float | None
    gpu: str = ""
    gpu_memory_gb: float | None = None


def _memory() -> tuple[float | None, float | None]:
    """Return total and available memory in GiB, where the platform says."""
    try:
        if sys.platform == "win32":

            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("load", ctypes.c_ulong),
                    ("total", ctypes.c_ulonglong),
                    ("available", ctypes.c_ulonglong),
                    ("total_page", ctypes.c_ulonglong),
                    ("available_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)

            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
                return status.total / _GIB, status.available / _GIB

            return None, None

        if os.path.exists("/proc/meminfo"):
            fields: dict[str, int] = {}

            with open("/proc/meminfo", encoding="utf-8") as meminfo:
                for line in meminfo:
                    name, _, value = line.partition(":")
                    fields[name] = int(value.split()[0]) * 1024

            total = fields.get("MemTotal")
            available = fields.get("MemAvailable")

            return (
                total / _GIB if total else None,
                available / _GIB if available else None,
            )

        if sys.platform == "darwin":
            total = int(
                subprocess.run(
                    ["sysctl", "-n", "hw.memsize"],
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=3,
                ).stdout.strip()
            )
            return total / _GIB, None
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    return None, None


def _nvidia_gpu() -> tuple[str, float | None]:
    """Return an NVIDIA card's name and memory, when `nvidia-smi` reports one."""
    if shutil.which("nvidia-smi") is None:
        return "", None

    try:
        output = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "", None

    if not output:
        return "", None

    name, _, memory = output.splitlines()[0].partition(",")

    try:
        return name.strip(), float(memory) / 1024
    except ValueError:
        return name.strip(), None


def hardware() -> Hardware:
    """Describe this machine."""
    total, free = _memory()
    gpu, gpu_memory = _nvidia_gpu()

    return Hardware(
        system=f"{platform.system()} {platform.release()}".strip(),
        cpus=os.cpu_count() or 1,
        ram_total_gb=round(total, 1) if total else None,
        ram_free_gb=round(free, 1) if free else None,
        gpu=gpu,
        gpu_memory_gb=round(gpu_memory, 1) if gpu_memory else None,
    )


def _get(url: str, timeout: float = 3) -> Any:
    """Return decoded JSON from a URL, or None when it does not answer."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ollama_status(endpoint: str) -> dict[str, Any]:
    """Report whether Ollama is installed, running, and what it holds.

    Args:
        endpoint: Ollama's base URL.

    Returns:
        `installed` (the program is on this machine), `running` (the server
        answers), `version`, `models` (pulled), and `gpu_in_use` — True when
        a loaded model sits in graphics memory, False when loaded models run
        on the CPU, None when nothing is loaded to tell by.
    """
    base = endpoint.rstrip("/")
    version = _get(f"{base}/api/version")
    tags = _get(f"{base}/api/tags") if version else None
    loaded = _get(f"{base}/api/ps") if version else None

    models = sorted(
        entry["name"]
        for entry in (tags or {}).get("models", [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    )
    running_models = [
        entry for entry in (loaded or {}).get("models", []) if isinstance(entry, dict)
    ]

    return {
        "installed": shutil.which("ollama") is not None or version is not None,
        "running": version is not None,
        "version": (version or {}).get("version"),
        "endpoint": base,
        "models": models,
        "gpu_in_use": (
            any(entry.get("size_vram", 0) > 0 for entry in running_models)
            if running_models
            else None
        ),
    }


def suggest(machine: Hardware, installed: list[str]) -> list[dict[str, Any]]:
    """Return the known models, each marked for how well it suits this machine.

    Args:
        machine: The hardware.
        installed: Models already pulled.

    Returns:
        Every suggestion with `fits` (enough memory), `recommended` (the
        best fit for its role here), and `installed`.
    """
    memory = machine.ram_total_gb or 0
    gpu = machine.gpu_memory_gb or 0
    fits = [
        suggestion
        for suggestion in SUGGESTIONS
        if memory >= suggestion.min_ram_gb or gpu >= suggestion.size_gb * 1.2
    ]
    # Without a GPU, a model above ~5 GB is minutes per answer on a CPU:
    # it fits, but recommending it would be recommending a wait.
    usable = [s for s in fits if gpu or s.size_gb <= 5]
    recommended: set[str] = set()

    planner = next((s for s in usable if s.role in {"planning", "both"}), None)
    answerer = next(
        (s for s in reversed(usable) if s.role in {"answers", "both"}), None
    )

    for choice in (planner, answerer):
        if choice is not None:
            recommended.add(choice.name)

    return [
        {
            **asdict(suggestion),
            "fits": suggestion in fits,
            "recommended": suggestion.name in recommended,
            "installed": suggestion.name in installed,
        }
        for suggestion in SUGGESTIONS
    ]


def needs_setup(
    provider: str, model: str, workspace: str, installed: list[str]
) -> bool:
    """Return whether this installation still lacks what a first run needs.

    Decided here, from the configuration, rather than by each browser: a
    console opened on a new machine, or in a private window, used to offer
    setup again on an installation that was long set up.

    Args:
        provider: The planning provider.
        model: The planning model.
        workspace: The configured repository path.
        installed: The models Ollama has, when it is the provider.

    Returns:
        True when no workspace is set, or the planning model is a local one
        that is not downloaded.
    """
    if not workspace.strip():
        return True

    if provider != "ollama":
        return False

    names = set(installed) | {name.removesuffix(":latest") for name in installed}

    return model not in names
