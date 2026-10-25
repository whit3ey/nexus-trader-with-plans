"""Mac hardware detection and local Ollama model selection.

Run `python -m bot.hardware` to print the report. Parsing functions are pure so
they can be tested with sample system_profiler output.
"""
from __future__ import annotations

import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class Hardware:
    chip: str = "unknown"
    memory_gb: float = 0.0
    cpu_cores: Optional[int] = None
    cpu_detail: str = ""
    gpu_cores: Optional[int] = None
    macos: str = "unknown"
    architecture: str = "unknown"
    apple_silicon: bool = False
    model_name: str = ""


def _run(cmd: List[str], timeout: int = 20) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def parse_hardware_profile(text: str) -> Dict[str, object]:
    out: Dict[str, object] = {}
    m = re.search(r"^\s*Chip:\s*(.+)$", text, re.M)
    if m:
        out["chip"] = m.group(1).strip()
    m = re.search(r"^\s*Model Name:\s*(.+)$", text, re.M)
    if m:
        out["model_name"] = m.group(1).strip()
    m = re.search(r"^\s*Memory:\s*([\d.]+)\s*(GB|TB|MB)", text, re.M)
    if m:
        v = float(m.group(1))
        out["memory_gb"] = v * {"GB": 1, "TB": 1024, "MB": 1 / 1024}[m.group(2)]
    m = re.search(r"Total Number of Cores:\s*(\d+)(?:\s*\(([^)]*)\))?", text)
    if m:
        out["cpu_cores"] = int(m.group(1))
        out["cpu_detail"] = (m.group(2) or "").strip()
    return out


def parse_gpu_cores(text: str) -> Optional[int]:
    m = re.search(r"Total Number of Cores:\s*(\d+)", text)
    return int(m.group(1)) if m else None


def detect_hardware() -> Hardware:
    hw = Hardware(architecture=platform.machine(), macos=platform.mac_ver()[0] or platform.platform())
    if sys.platform != "darwin":
        hw.chip = platform.processor() or "non-macOS"
        return hw
    info = parse_hardware_profile(_run(["system_profiler", "SPHardwareDataType"]))
    hw.chip = str(info.get("chip", "unknown"))
    hw.memory_gb = float(info.get("memory_gb", 0.0))
    hw.cpu_cores = info.get("cpu_cores")  # type: ignore[assignment]
    hw.cpu_detail = str(info.get("cpu_detail", ""))
    hw.model_name = str(info.get("model_name", ""))
    hw.gpu_cores = parse_gpu_cores(_run(["system_profiler", "SPDisplaysDataType"]))
    if hw.memory_gb == 0:
        raw = _run(["sysctl", "-n", "hw.memsize"]).strip()
        if raw.isdigit():
            hw.memory_gb = int(raw) / 1024 ** 3
    hw.apple_silicon = hw.architecture == "arm64" and "Apple" in hw.chip
    return hw


# (ollama tag, approx download GB). Ordered best -> smallest within each tier.
MODEL_TIERS = [
    (48, [("qwen3:30b", 19.0), ("qwen3:14b", 9.3)]),
    (24, [("qwen3:30b", 19.0), ("qwen3:14b", 9.3)]),
    (12, [("qwen3:14b", 9.3), ("qwen3:8b", 5.2)]),
    (0, [("qwen3:4b", 2.6), ("qwen3:1.7b", 1.4)]),
]
KNOWN_SIZES = {"qwen3:1.7b": 1.4, "qwen3:4b": 2.6, "qwen3:8b": 5.2, "qwen3:14b": 9.3,
               "qwen3:30b": 19.0, "qwen3:32b": 20.0}
MAX_MODEL_FRACTION = 0.65   # never pick a model larger than this share of unified memory


@dataclass
class ModelChoice:
    model: str
    size_gb: Optional[float]
    reason: str
    needs_pull: bool = True


def select_model(memory_gb: float, installed: Optional[List[str]] = None,
                 override: Optional[str] = None) -> ModelChoice:
    """Pick a model from RAM. `override` ('auto' or '' = no override) is always honoured."""
    installed = installed or []

    def has(tag: str) -> bool:
        return any(i == tag or i.split(":latest")[0] == tag or i == tag + ":latest" for i in installed)

    if override and override.lower() != "auto":
        return ModelChoice(override, KNOWN_SIZES.get(override), "model set manually in .env / config.yaml",
                           needs_pull=not has(override))
    if memory_gb <= 0:
        return ModelChoice("qwen3:4b", 2.6, "memory could not be detected - choosing the safest small model",
                           needs_pull=not has("qwen3:4b"))
    for floor, options in MODEL_TIERS:
        if memory_gb >= floor:
            for tag, size in options:
                if size <= memory_gb * MAX_MODEL_FRACTION:
                    why = (f"{memory_gb:.0f} GB unified memory; {tag} (~{size:.1f} GB) leaves headroom for macOS, "
                           f"the bot and the dashboard")
                    if memory_gb >= 48 and tag == "qwen3:30b":
                        why += "; mixture-of-experts model, fast on Apple Silicon (qwen3:32b is a slower dense alternative)"
                    return ModelChoice(tag, size, why, needs_pull=not has(tag))
    return ModelChoice("qwen3:1.7b", 1.4, "very little memory - smallest practical model", needs_pull=not has("qwen3:1.7b"))


def format_report(hw: Hardware, choice: ModelChoice) -> str:
    gpu = f"{hw.gpu_cores} cores" if hw.gpu_cores else "unknown"
    cpu = f"{hw.cpu_cores} cores" + (f" ({hw.cpu_detail})" if hw.cpu_detail else "") if hw.cpu_cores else "unknown"
    size = f"~{choice.size_gb:.1f} GB download" if choice.size_gb else "unknown"
    return "\n".join([
        "Detected hardware:",
        f"Chip: {hw.chip}" + ("" if hw.apple_silicon else "   (NOT Apple Silicon - performance will differ)"),
        f"Memory: {hw.memory_gb:.0f} GB unified memory",
        f"CPU: {cpu}",
        f"GPU: {gpu}",
        f"macOS: {hw.macos}  Architecture: {hw.architecture}",
        f"Selected AI model: {choice.model}",
        f"Estimated model size: {size}",
        f"Reason for selection: {choice.reason}",
    ])


def main() -> int:
    import os
    hw = detect_hardware()
    choice = select_model(hw.memory_gb, override=os.environ.get("OLLAMA_MODEL"))
    print(format_report(hw, choice))
    if len(sys.argv) > 1 and sys.argv[1] == "--model-only":
        pass
    return 0 if hw.apple_silicon or sys.platform != "darwin" else 1


if __name__ == "__main__":
    sys.exit(main())


def resolve_model(configured: str, root, memory_gb: Optional[float] = None) -> str:
    """Model to use: explicit config/env > model chosen by setup.sh > auto-selection from RAM."""
    from pathlib import Path
    if configured and configured.lower() != "auto":
        return configured
    saved = Path(root) / "data" / "selected_model.txt"
    if saved.exists() and saved.read_text().strip():
        return saved.read_text().strip()
    gb = memory_gb if memory_gb is not None else detect_hardware().memory_gb
    return select_model(gb).model
