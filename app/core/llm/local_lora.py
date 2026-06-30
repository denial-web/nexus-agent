"""
Optional local LoRA inference for critic nodes (PEFT adapters on disk).

Adapters are resolved from ``LOCAL_LORA_MODELS_ROOT/<suffix>/`` when the
requested ``model_id`` is ``local-lora:<suffix>``. Keep heavy adapters out of
the main governance process in production by serving merged weights via Ollama
(``ollama:<name>``) instead; this path is for lab/dev closed-loop validation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple] = {}


def models_root() -> Path:
    root = (settings.LOCAL_LORA_MODELS_ROOT or "").strip()
    if not root:
        raise FileNotFoundError(
            "LOCAL_LORA_MODELS_ROOT is not set — cannot load local-lora adapters. "
            "Point it at Doctrine Lab data/models/ or serve via ollama:."
        )
    return Path(root).expanduser().resolve()


def resolve_adapter_dir(model_id: str) -> Path:
    if ":" not in model_id:
        raise ValueError(f"Invalid local-lora model id: {model_id}")
    suffix = model_id.split(":", 1)[1].strip()
    if not suffix:
        raise ValueError(f"Invalid local-lora model id: {model_id}")
    adapter_dir = models_root() / suffix
    if not (adapter_dir / "adapter_config.json").is_file():
        raise FileNotFoundError(f"LoRA adapter not found at {adapter_dir}")
    return adapter_dir


def normalize_adapter_model_id(
    lora_adapter_path: str | None,
    config: dict | None,
) -> str | None:
    """Map registry metadata to a provider ``model_id``."""
    if config:
        mid = str(config.get("model_id") or "").strip()
        if mid:
            return mid
    raw = (lora_adapter_path or "").strip()
    if not raw:
        return None
    if raw.startswith(("local-lora:", "ollama:", "local:", "gpt", "gemini", "deepseek")):
        return raw
    if "/" in raw or raw.startswith("."):
        return f"local-lora:{Path(raw).name}"
    return raw


def _load_base_model_name(adapter_dir: Path) -> str:
    with open(adapter_dir / "adapter_config.json", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg.get("base_model_name_or_path") or "Qwen/Qwen2.5-3B-Instruct"


def _get_device():
    import torch

    if torch.cuda.is_available():
        return "cuda", torch.float16
    if torch.backends.mps.is_available():
        return "mps", torch.float16
    return "cpu", torch.float32


def _load_decode_profile(adapter_dir: Path) -> dict[str, int | float]:
    path = adapter_dir / "decode.json"
    defaults = {"repetition_penalty": 1.05, "no_repeat_ngram_size": 0, "max_new_tokens": 512}
    if not path.is_file():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return defaults
    return {
        "repetition_penalty": float(data.get("repetition_penalty", defaults["repetition_penalty"])),
        "no_repeat_ngram_size": int(data.get("no_repeat_ngram_size", defaults["no_repeat_ngram_size"])),
        "max_new_tokens": int(data.get("max_new_tokens", defaults["max_new_tokens"])),
    }


def _load_model_and_tokenizer(adapter_dir: Path):
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    key = str(adapter_dir.resolve())
    if key in _CACHE:
        return _CACHE[key]

    base_model = _load_base_model_name(adapter_dir)
    device, dtype = _get_device()
    logger.info("Loading local LoRA base=%s adapter=%s device=%s", base_model, adapter_dir, device)

    tokenizer = AutoTokenizer.from_pretrained(base_model, trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    base = AutoModelForCausalLM.from_pretrained(
        base_model,
        torch_dtype=dtype,
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(base, str(adapter_dir))
    model.eval()
    if device in ("cuda", "mps"):
        model = model.to(device)

    _CACHE[key] = (model, tokenizer, device)
    return _CACHE[key]


def generate_local_lora(
    model_id: str,
    prompt: str,
    system_prompt: str | None,
    *,
    max_new_tokens: int | None = None,
) -> tuple[str, str, int]:
    """Generate text with a saved LoRA adapter."""
    import torch

    adapter_dir = resolve_adapter_dir(model_id)
    profile = _load_decode_profile(adapter_dir)
    max_new = max_new_tokens or int(profile["max_new_tokens"])

    model, tokenizer, device = _load_model_and_tokenizer(adapter_dir)
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    prompt_text = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    inputs = tokenizer(prompt_text, return_tensors="pt")
    if device in ("cuda", "mps"):
        inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        output_ids = model.generate(
            **inputs,
            max_new_tokens=max_new,
            do_sample=False,
            pad_token_id=tokenizer.pad_token_id,
            eos_token_id=tokenizer.eos_token_id,
            repetition_penalty=float(profile["repetition_penalty"]),
            no_repeat_ngram_size=int(profile["no_repeat_ngram_size"]),
        )

    new_tokens = output_ids[0][inputs["input_ids"].shape[1] :]
    text = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
    token_count = int(new_tokens.shape[0])
    return text, model_id, token_count


def clear_cache() -> None:
    _CACHE.clear()
