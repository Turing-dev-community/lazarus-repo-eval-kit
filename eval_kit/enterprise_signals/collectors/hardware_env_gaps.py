"""Stage E11: Hardware / environment gaps collector (Programmatic, per-PR)."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from eval_kit.enterprise_signals.base import PRCollector, PRContext

_PATTERNS: List[re.Pattern] = [
    re.compile(r, re.IGNORECASE)
    for r in [
        # GPU / CUDA
        r"\.cu$",
        r"\.cuh$",
        r"\bcuda\b",
        r"\bcuDNN\b",
        r"\btorch\.cuda\b",
        r"\btf\.device\b",
        r"\bGPU\b",
        r"\bnvidia\b",
        r"\bcublas\b",
        r"\bcufft\b",
        r"\bcurand\b",
        r"\bROCm\b",
        r"\bOpenCL\b",
        r"\bMetal\b",
        r"\bMPS\b",  # Apple Metal Performance Shaders
        # Hardware-specific file extensions / directories
        r"/hardware/",
        r"/firmware/",
        r"/drivers?/",
        r"\.ino$",  # Arduino
        r"\.vhd$",  # VHDL
        r"\.v$",  # Verilog
        r"\.sv$",  # SystemVerilog
        r"\.bit$",  # FPGA bitstream
        r"\.xdc$",  # Xilinx constraints
        r"\.qsf$",  # Quartus settings
        # IoT / embedded
        r"\bRaspberryPi\b",
        r"\bArduino\b",
        r"\bESP32\b",
        r"\bSTM32\b",
        r"\bFPGA\b",
        r"\bembedded\b",
        r"\bbaremetal\b",
        r"\bRTOS\b",
        r"\bFreeRTOS\b",
        r"\bZephyr\b",
        # Docker / container environment markers
        r"Dockerfile",
        r"docker-compose",
        r"\.devcontainer",
        r"devcontainer\.json",
        # CI environment variables indicating hardware
        r"CUDA_VISIBLE_DEVICES",
        r"GPU_DEVICE_ORDINAL",
        r"NVIDIA_VISIBLE_DEVICES",
    ]
]


class HardwareEnvGapsCollector(PRCollector):
    name = "hardware_environment_gaps"
    requires_diff = False

    def collect(self, pr: PRContext) -> Dict[str, Any]:
        matched: List[str] = []
        seen: set = set()
        for path in pr.changed_files:
            for pat in _PATTERNS:
                if pat.search(path) and path not in seen:
                    seen.add(path)
                    matched.append(path)
                    break
        return {
            "has_hardware_environment_gaps": bool(matched),
            "matched_files": matched,
        }
