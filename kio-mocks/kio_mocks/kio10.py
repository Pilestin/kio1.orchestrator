"""KIO10 — energy efficiency, and model-driven TinyML for edge devices.

D2.6 §4.1.10: a TinyML project from the user's intent (FR-KIO10-01), the
model checked against an edge device's memory, energy and timing limits and
adjusted (FR-KIO10-02), a device assigned with fallback (FR-KIO10-03),
firmware generated and deployed (FR-KIO10-04/05). In UC-UC1-03 it "evaluates
energy impact of alternative fixes".

- `energy_efficiency` measures, rather than guesses: the tests (or the entry
  point) run a few times, and energy is CPU time × the CPU's power
  (`KIO_MOCKS_CPU_WATTS`, 15 W by default). Where the system does not report
  child CPU time (Windows), wall time stands in and the method says so. A
  fixed copy from KIO7 (`sandbox`) is measured the same way and compared.
- `tinyml` has no device to flash, so it is a **simulation** with the real
  device limits: it reads a model's size from a `.tflite`/`.onnx` file or the
  arguments, checks it against a catalog of boards, falls back to one that
  fits, and estimates latency and energy per inference.
"""

from __future__ import annotations

import os
import statistics
from pathlib import Path
from typing import Any

from kio_mocks.errors import KioError
from kio_mocks.protocol import Capability, Kio, Request
from kio_mocks.pytest_runner import run_tests
from kio_mocks.workspace import Workspace, python

CPU_WATTS = float(os.environ.get("KIO_MOCKS_CPU_WATTS", "15"))
# Enough to smooth out a cold start; fewer where speed matters more (tests).
RUNS = int(os.environ.get("KIO_MOCKS_ENERGY_RUNS", "3"))

# Public figures for common boards: flash and RAM in KB, throughput in
# millions of multiply-accumulates per second, active power in milliwatts.
DEVICES = [
    {"name": "nRF52840", "flash_kb": 1024, "ram_kb": 256, "mmacs": 30, "active_mw": 15},
    {"name": "STM32L4", "flash_kb": 1024, "ram_kb": 320, "mmacs": 40, "active_mw": 30},
    {
        "name": "ESP32-S3",
        "flash_kb": 8192,
        "ram_kb": 512,
        "mmacs": 240,
        "active_mw": 240,
    },
    {
        "name": "Raspberry Pi Pico",
        "flash_kb": 2048,
        "ram_kb": 264,
        "mmacs": 50,
        "active_mw": 90,
    },
]


def _measure(workspace: Workspace) -> dict[str, Any]:
    """Run the workload a few times; seconds, CPU seconds, joules."""
    walls, cpus = [], []
    for _ in range(RUNS):
        if workspace.entry_point:
            completed = workspace.run([python(), workspace.entry_point])
            walls.append(completed.seconds)
            cpus.append(completed.cpu_seconds)
        else:
            run = run_tests(workspace)
            walls.append(run.seconds)
            cpus.append(run.cpu_seconds)

    measured_cpu = statistics.mean(cpus)
    seconds = statistics.mean(walls)
    cpu = measured_cpu if measured_cpu > 0 else seconds

    return {
        "workload": workspace.entry_point or "test suite",
        "runs": RUNS,
        "seconds": round(seconds, 3),
        "cpu_seconds": round(cpu, 3),
        "joules": round(cpu * CPU_WATTS, 2),
        "method": (
            f"cpu_seconds × {CPU_WATTS:g} W"
            if measured_cpu > 0
            else f"wall seconds × {CPU_WATTS:g} W (child CPU time not reported here)"
        ),
    }


def energy_efficiency(request: Request) -> dict[str, Any]:
    baseline = _measure(request.workspace)
    result: dict[str, Any] = {"baseline": baseline}

    sandbox = request.find_upstream("sandbox")
    if isinstance(sandbox, str) and Path(sandbox).is_dir():
        alternative = _measure(
            Workspace(root=Path(sandbox), entry_point=request.workspace.entry_point)
        )
        change = (
            (alternative["joules"] - baseline["joules"]) / baseline["joules"]
            if baseline["joules"]
            else 0.0
        )
        result["alternative"] = alternative
        result["change"] = round(change, 3)
        verdict = "defect" if change > 0.2 else "clean"
        summary = (
            f"The fix costs {alternative['joules']} J against {baseline['joules']} J before "
            f"({change:+.0%}) over {RUNS} runs of the {baseline['workload']}."
        )
    else:
        verdict = "clean"
        summary = f"The {baseline['workload']} takes {baseline['seconds']} s and about {baseline['joules']} J per run ({baseline['method']})."

    return {"summary": summary, "verdict": verdict, **result}


def _model_size_kb(request: Request) -> tuple[float, str]:
    if request.arguments.get("model_kb"):
        return float(request.arguments["model_kb"]), "arguments"
    root = request.workspace.root
    if root is not None and root.is_dir():
        for path in sorted(root.rglob("*")):
            if path.suffix in (".tflite", ".onnx", ".h5") and path.is_file():
                return round(path.stat().st_size / 1024, 1), request.workspace.relative(
                    path
                )
    raise KioError(
        "No model to place: add a .tflite/.onnx file to the workspace, or give model_kb (and ram_kb, macs)."
    )


def tinyml(request: Request) -> dict[str, Any]:
    size_kb, source = _model_size_kb(request)
    # A TinyML arena is typically a fraction of the model it runs.
    ram_kb = float(request.arguments.get("ram_kb") or size_kb * 0.25)
    macs = float(request.arguments.get("macs") or size_kb * 1000)
    wanted = str(request.arguments.get("device") or "")
    deadline_ms = float(request.arguments.get("deadline_ms") or 100)

    def assess(device: dict[str, Any]) -> dict[str, Any]:
        latency_ms = macs / (device["mmacs"] * 1000)
        return {
            "device": device["name"],
            "fits_flash": size_kb <= device["flash_kb"] * 0.8,
            "fits_ram": ram_kb <= device["ram_kb"] * 0.8,
            "latency_ms": round(latency_ms, 2),
            "meets_deadline": latency_ms <= deadline_ms,
            "energy_mj_per_inference": round(
                device["active_mw"] * latency_ms / 1000, 3
            ),
        }

    assessments = [assess(device) for device in DEVICES]
    usable = [
        item
        for item in assessments
        if item["fits_flash"] and item["fits_ram"] and item["meets_deadline"]
    ]
    chosen = next((item for item in assessments if item["device"] == wanted), None)
    fallback = None

    if chosen is None or chosen not in usable:
        fallback = (
            min(usable, key=lambda item: item["energy_mj_per_inference"])
            if usable
            else None
        )

    target = fallback or chosen
    summary = (
        f"A {size_kb:g} KB model ({source}) "
        + (f"does not fit {wanted}; " if wanted and fallback else "")
        + (
            f"runs on {target['device']} in {target['latency_ms']} ms at {target['energy_mj_per_inference']} mJ per inference."
            if target and target in usable
            else "fits none of the catalog's boards within the limits; quantise or prune it."
        )
    )

    return {
        "summary": summary + " (simulated: no device attached)",
        "verdict": "clean" if target and target in usable else "defect",
        "simulated": True,
        "model_kb": size_kb,
        "assessments": assessments,
        "device": target["device"] if target else None,
        "fallback_from": wanted if fallback and wanted else None,
        "firmware": (
            f"// Generated for {target['device']} (simulated)\n#include \"model_data.h\"\n"
            "void setup() { tflite_init(model_data); }\nvoid loop() { tflite_invoke(); sleep_until_next_sample(); }\n"
            if target
            else ""
        ),
    }


KIO = Kio(
    kio_id="KIO10",
    title="Energy-efficiency with Model-Driven TinyML and LLMs",
    description=(
        "TinyML and energy-efficiency analysis: measures the energy a workload or a fix "
        "costs, and places a model on an edge device within its memory, latency and "
        "energy limits, falling back to one that fits (simulated)."
    ),
    capabilities=[
        Capability(
            "energy_efficiency",
            "Measure a workload's energy, and compare a fix.",
            energy_efficiency,
        ),
        Capability(
            "tinyml",
            "Place a model on an edge device within its limits (simulated).",
            tinyml,
        ),
    ],
)
