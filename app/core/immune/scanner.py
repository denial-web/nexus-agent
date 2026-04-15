"""
Agent-Immune gateway — input and output scanning.

Provides multi-language prompt-injection detection, session-based
escalation tracking, prompt hardening, and output scanning.
This is the first and last checkpoint in the zero-trust pipeline.
"""

import hashlib
import logging
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class Verdict(StrEnum):
    PASS = "pass"
    BLOCK = "block"
    FLAG = "flag"


@dataclass
class ScanResult:
    verdict: Verdict
    score: float
    triggers: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)


# ── Unicode normalization ──────────────────────────────────────────

_ZERO_WIDTH = re.compile(
    "[\u200b\u200c\u200d\u2060\ufeff\u2063\u2062\u2061"
    "\u202a\u202b\u202c\u202d\u202e"
    "\u2066\u2067\u2068\u2069"
    "\u00ad]"
)

_COMBINING_MARKS = re.compile(r"[\u0300-\u036f]")

_CONFUSABLE_MAP: dict[int, str] = {
    0x0430: "a",  # Cyrillic а
    0x0435: "e",  # Cyrillic е
    0x043E: "o",  # Cyrillic о
    0x0440: "p",  # Cyrillic р
    0x0441: "c",  # Cyrillic с
    0x0443: "y",  # Cyrillic у
    0x0445: "x",  # Cyrillic х
    0x0456: "i",  # Cyrillic і
    0x0410: "A",  # Cyrillic А
    0x0412: "B",  # Cyrillic В
    0x0415: "E",  # Cyrillic Е
    0x041D: "H",  # Cyrillic Н
    0x041E: "O",  # Cyrillic О
    0x0420: "P",  # Cyrillic Р
    0x0421: "C",  # Cyrillic С
    0x0422: "T",  # Cyrillic Т
    0x0425: "X",  # Cyrillic Х
    0x2170: "i",  # Roman numeral ⅰ
    0x2171: "ii",
    0x217A: "xi",
}


def _normalize_unicode(text: str, *, replace_zw_with_space: bool = False) -> str:
    """Normalize Unicode tricks that attempt to bypass regex-based detection."""
    text = _ZERO_WIDTH.sub(" " if replace_zw_with_space else "", text)
    text = unicodedata.normalize("NFD", text)
    text = _COMBINING_MARKS.sub("", text)
    text = unicodedata.normalize("NFKC", text)
    chars: list[str] = []
    for ch in text:
        replacement = _CONFUSABLE_MAP.get(ord(ch))
        if replacement:
            chars.append(replacement)
        else:
            chars.append(ch)
    return "".join(chars)


# ── Multi-language injection patterns ──────────────────────────────

INJECTION_PATTERNS = [
    # English
    r"ignore\s+(all\s+)?(previous|prior|above)\s+(instructions|rules|prompts)",
    r"you\s+are\s+now\s+(DAN|evil|jailbroken|unrestricted)",
    r"system\s*:\s*you\s+are",
    r"pretend\s+(you\s+are|to\s+be)\s+a",
    r"do\s+not\s+follow\s+(your|the)\s+(rules|guidelines)",
    r"reveal\s+(your|the)\s+(system\s+)?prompt",
    r"override\s+(safety|content)\s+(filters?|policies)",
    r"\bDAN\s+mode\b",
    r"act\s+as\s+if\s+(you\s+have\s+)?no\s+(restrictions|rules|limits)",
    # Spanish
    r"ignora\s+(todas?\s+)?(las\s+)?instrucciones\s+(anteriores|previas)",
    r"olvida\s+(tus|las)\s+(reglas|instrucciones)",
    # French
    r"ignore[rz]?\s+(toutes?\s+)?(les\s+)?instructions\s+(pr[eé]c[eé]dentes|ant[eé]rieures)",
    r"oublie[rz]?\s+(tes|les)\s+(r[eè]gles|instructions)",
    # German
    r"ignorier(e|en)?\s+(alle\s+)?(vorherigen|bisherigen)\s+(Anweisungen|Regeln)",
    r"vergiss\s+(deine|die)\s+(Regeln|Anweisungen)",
    # Portuguese
    r"ignor[ea]\s+(todas?\s+)?(as\s+)?instru[cç][oõ]es\s+(anteriores|pr[eé]vias)",
    # Chinese (simplified)
    r"忽略.{0,4}(之前|以前|先前).{0,4}(指令|指示|规则|提示)",
    r"无视.{0,4}(安全|内容).{0,4}(规则|限制|过滤)",
    # Japanese
    r"(以前|前|これまで)の(指示|ルール|命令)を(無視|忘れ)",
    r"制限(なし|を解除|を無視)",
    # Korean
    r"(이전|앞선|위의)\s*(지시|규칙|명령).{0,4}(무시|잊어)",
    # Russian
    r"игнорир(уй|овать)\s+(все\s+)?(предыдущие|прежние)\s+(инструкции|правила)",
    r"забудь\s+(свои|все)\s+(правила|инструкции)",
    # Arabic
    r"تجاهل.{0,6}(التعليمات|القواعد|الأوامر)\s*(السابقة|القديمة)",
    # Hindi
    r"(पिछले|पहले).{0,6}(निर्देश|नियम).{0,6}(अनदेखा|भूल\s*जाओ)",
]

ESCALATION_PHRASES = [
    "this is urgent",
    "do it now",
    "you must comply",
    "i am your creator",
    "admin override",
    "sudo mode",
    "bypass safety",
]

OUTPUT_LEAK_PATTERNS = [
    r"(sk|pk)[-_][a-zA-Z0-9_]{20,}",
    r"(api[_-]?key|secret|password|token)\s*[:=]\s*[\"'][^\s\"']{6,}",
    r"(api[_-]?key|secret|password|token)\s*[:=]\s*(?=[^\s\"']*\d)[a-zA-Z0-9/+=_-]{8,}",
    r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----",
]

_compiled_injection = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
_compiled_leaks = [re.compile(p, re.IGNORECASE) for p in OUTPUT_LEAK_PATTERNS]


# ── Semantic Memory Bank ───────────────────────────────────────────


class MemoryBank:
    """
    Stores known attack signatures and matches new prompts against them.

    Uses token-set overlap (Jaccard similarity) for fast fuzzy matching
    without requiring an embedding model.
    """

    _MAX_SIGNATURES = 10_000

    def __init__(self, similarity_threshold: float = 0.6):
        self._signatures: list[tuple[str, set[str]]] = []
        self._threshold = similarity_threshold
        self._lock = threading.Lock()

    def add_signature(self, text: str) -> None:
        tokens = self._tokenize(text)
        if tokens:
            with self._lock:
                if len(self._signatures) >= self._MAX_SIGNATURES:
                    self._signatures = self._signatures[self._MAX_SIGNATURES // 5 :]
                self._signatures.append((text, tokens))

    def match(self, text: str) -> str | None:
        tokens = self._tokenize(text)
        if not tokens:
            return None
        with self._lock:
            for sig_text, sig_tokens in self._signatures:
                intersection = tokens & sig_tokens
                union = tokens | sig_tokens
                if union and len(intersection) / len(union) >= self._threshold:
                    return sig_text
        return None

    def clear(self) -> None:
        with self._lock:
            self._signatures.clear()

    @property
    def size(self) -> int:
        return len(self._signatures)

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        return set(re.findall(r"\w+", text.lower()))


_memory_bank = MemoryBank()


def get_memory_bank() -> MemoryBank:
    return _memory_bank


# ── Escalation Tracker ─────────────────────────────────────────────


class EscalationTracker:
    """
    Tracks per-session escalation across multiple turns.

    Each flagged prompt in a session increments the session's escalation
    score. When the cumulative score crosses the threshold, subsequent
    prompts in that session are automatically blocked.
    """

    _MAX_SESSIONS = 50_000
    _CLEANUP_MULTIPLIER = 3.0

    def __init__(self, threshold: float = 1.0, decay_seconds: float = 300.0):
        self._sessions: dict[str, tuple[float, float]] = {}
        self._threshold = threshold
        self._decay_seconds = decay_seconds
        self._lock = threading.Lock()
        self._cleanup_counter = 0

    def record(self, session_id: str, score: float) -> None:
        with self._lock:
            current, last_time = self._sessions.get(session_id, (0.0, 0.0))
            now = time.time()
            if last_time and self._decay_seconds > 0:
                elapsed = now - last_time
                decay = elapsed / self._decay_seconds
                current = max(0.0, current - decay)
            self._sessions[session_id] = (current + score, now)
            self._cleanup_counter += 1
            if self._cleanup_counter >= 1000:
                self._cleanup_counter = 0
                self._evict_stale(now)
            from app.metrics import ACTIVE_SESSIONS

            ACTIVE_SESSIONS.set(len(self._sessions))

    def is_escalated(self, session_id: str) -> bool:
        with self._lock:
            current, last_time = self._sessions.get(session_id, (0.0, 0.0))
            if last_time and self._decay_seconds > 0:
                elapsed = time.time() - last_time
                decay = elapsed / self._decay_seconds
                current = max(0.0, current - decay)
            return current >= self._threshold

    def get_score(self, session_id: str) -> float:
        with self._lock:
            current, last_time = self._sessions.get(session_id, (0.0, 0.0))
            if last_time and self._decay_seconds > 0:
                elapsed = time.time() - last_time
                decay = elapsed / self._decay_seconds
                current = max(0.0, current - decay)
            return current

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def _evict_stale(self, now: float) -> None:
        """Remove sessions whose score has fully decayed. Caller must hold _lock."""
        cutoff = self._decay_seconds * self._CLEANUP_MULTIPLIER
        stale = [sid for sid, (_, ts) in self._sessions.items() if (now - ts) > cutoff]
        for sid in stale:
            del self._sessions[sid]
        if len(self._sessions) > self._MAX_SESSIONS:
            by_age = sorted(self._sessions.items(), key=lambda kv: kv[1][1])
            for sid, _ in by_age[: len(self._sessions) - self._MAX_SESSIONS]:
                del self._sessions[sid]


_escalation_tracker = EscalationTracker()


def get_escalation_tracker() -> EscalationTracker:
    return _escalation_tracker


# ── Prompt Hardener ────────────────────────────────────────────────

_HARDENING_REMOVALS = [re.compile(p + r"[.\s]*", re.IGNORECASE) for p in INJECTION_PATTERNS]


def harden_prompt(prompt: str) -> tuple[str, list[str]]:
    """
    Strip detected injection fragments from a flagged prompt.

    Returns (hardened_prompt, list_of_removed_fragments).
    Does NOT apply to blocked prompts — those are rejected entirely.
    """
    removed: list[str] = []
    result = prompt
    for pattern in _HARDENING_REMOVALS:
        for match in pattern.finditer(result):
            removed.append(match.group().strip())
        result = pattern.sub("", result)

    if not removed:
        normalized = _normalize_unicode(prompt)
        for pattern in _HARDENING_REMOVALS:
            for match in pattern.finditer(normalized):
                removed.append(match.group().strip())
            normalized = pattern.sub("", normalized)
        if removed:
            result = normalized

    result = re.sub(r"\s{2,}", " ", result).strip()
    return result, removed


# ── Core scan functions ────────────────────────────────────────────


def scan_input(
    prompt: str,
    session_id: str | None = None,
) -> ScanResult:
    """Scan an inbound prompt for injection attempts and escalation."""
    triggers: list[str] = []
    score = 0.0

    if session_id and _escalation_tracker.is_escalated(session_id):
        return ScanResult(
            verdict=Verdict.BLOCK,
            score=1.0,
            triggers=["escalation:session_threshold_exceeded"],
            details={
                "prompt_length": len(prompt),
                "prompt_hash": _hash(prompt),
                "session_escalation": _escalation_tracker.get_score(session_id),
            },
        )

    norm_strip = _normalize_unicode(prompt)
    norm_space = _normalize_unicode(prompt, replace_zw_with_space=True)

    sig_match = _memory_bank.match(prompt)
    if not sig_match:
        sig_match = _memory_bank.match(norm_strip)
    if not sig_match:
        sig_match = _memory_bank.match(norm_space)
    if sig_match:
        triggers.append("memory_bank:known_attack_signature")
        score += 0.5

    for pattern in _compiled_injection:
        if (
            pattern.search(prompt)
            or pattern.search(norm_strip)
            or pattern.search(norm_space)
        ):
            triggers.append(f"injection:{pattern.pattern[:40]}")
            score += 0.4

    prompt_lower = norm_space.lower()
    for phrase in ESCALATION_PHRASES:
        if phrase in prompt_lower:
            triggers.append(f"escalation:{phrase}")
            score += 0.2

    score = min(score, 1.0)

    if score >= 0.6:
        verdict = Verdict.BLOCK
    elif score >= 0.2:
        verdict = Verdict.FLAG
    else:
        verdict = Verdict.PASS

    if session_id and score > 0:
        _escalation_tracker.record(session_id, score)

    if verdict == Verdict.BLOCK and triggers:
        for t in triggers:
            if t.startswith("injection:"):
                _memory_bank.add_signature(prompt)
                break

    return ScanResult(
        verdict=verdict,
        score=round(score, 3),
        triggers=triggers,
        details={
            "prompt_length": len(prompt),
            "prompt_hash": _hash(prompt),
            "session_escalation": (_escalation_tracker.get_score(session_id) if session_id else None),
        },
    )


def scan_output(response: str) -> ScanResult:
    """Scan model output for leaked secrets or forbidden content."""
    triggers: list[str] = []
    score = 0.0

    for pattern in _compiled_leaks:
        if pattern.search(response):
            triggers.append(f"leak:{pattern.pattern[:40]}")
            score += 0.5

    score = min(score, 1.0)

    if score >= 0.5:
        verdict = Verdict.BLOCK
    elif score > 0.0:
        verdict = Verdict.FLAG
    else:
        verdict = Verdict.PASS

    return ScanResult(
        verdict=verdict,
        score=round(score, 3),
        triggers=triggers,
        details={"response_length": len(response), "response_hash": _hash(response)},
    )


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
