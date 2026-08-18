"""Configurable QLoRA defaults; no optional training dependency is imported."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class QLoRAConfig:
    base_model: str = "Qwen/Qwen2.5-14B-Instruct"
    base_revision: str | None = None
    load_in_4bit: bool = True
    bnb_4bit_quant_type: str = "nf4"
    bnb_4bit_use_double_quant: bool = True
    bnb_4bit_compute_dtype: str = "bfloat16"
    lora_r: int = 16
    lora_alpha: int = 32
    lora_dropout: float = 0.05
    target_modules: tuple[str, ...] = field(
        default_factory=lambda: (
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        )
    )
    max_production_lora_rank: int = 32

    def __post_init__(self) -> None:
        if self.bnb_4bit_quant_type.lower() != "nf4":
            raise ValueError("QLoRA requires NF4 quantization")
        if not self.load_in_4bit or not self.bnb_4bit_use_double_quant:
            raise ValueError("QLoRA requires 4-bit loading and double quantization")
        if not 0 < self.lora_r <= self.max_production_lora_rank:
            raise ValueError("LoRA rank must be positive and <= production max-lora-rank")
        if self.lora_alpha <= 0 or not 0 <= self.lora_dropout < 1:
            raise ValueError("invalid LoRA alpha or dropout")
        if tuple(self.target_modules) != (
            "q_proj",
            "k_proj",
            "v_proj",
            "o_proj",
            "gate_proj",
            "up_proj",
            "down_proj",
        ):
            raise ValueError("target_modules must cover the configured attention/MLP projections")

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["target_modules"] = list(self.target_modules)
        return result
