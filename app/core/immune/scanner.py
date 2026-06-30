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


def is_tool_call_blocked(result: ScanResult) -> bool:
    """Return True if `result` should block an MCP `tools/call` payload.

    Free-text prompts have a safety fallback — `harden_prompt` strips
    flagged fragments before they reach the LLM — so FLAG is a viable
    "suspected, mitigated" verdict there. Tool-call arguments have no
    such fallback: the MCP proxy either forwards the literal payload
    or raises. At that boundary, any non-PASS verdict is a detected
    attack and the safest action is to reject.

    Using one helper keeps the proxy and the
    tests/eval/tool_injection_redteam.py benchmark in agreement about
    what "blocked at the tool-call boundary" means — changing the
    policy (e.g. allowing FLAG through for a specific backend) is
    then a single-site edit instead of a grep-and-fix across files.
    """
    return result.verdict in (Verdict.BLOCK, Verdict.FLAG)


# ── Unicode normalization ──────────────────────────────────────────

_ZERO_WIDTH = re.compile(
    "[\u200b\u200c\u200d\u2060\ufeff\u2063\u2062\u2061\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u00ad]"
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
    # Jailbreak mode keywords — narrow patterns, high precision. Words
    # like "jailbreak mode", "DAN mode enabled", "no restrictions" are
    # hallmarks of attack payloads and essentially never appear in
    # legitimate prompts. Extends the existing "DAN mode" single-pattern.
    r"jailbreak\s*(mode|enabled|active|activated|on)",
    r"(no\s+restrictions|unrestricted\s+mode)\s*(enabled|active|on)?[\s.,!:;]",
    # Secret-exfil intent — attacker tells the agent to dump credentials.
    # Anchored on both a verb (print/dump/reveal/leak/show) AND a
    # credential noun, with optional quantifier in between. Matches
    # "print all environment variables", "reveal my api keys", "dump
    # passwords", but NOT neutral phrases like "environment variables
    # are important" or "how do I print a list in Python".
    (
        r"(print|dump|output|reveal|leak|show|expose)"
        r"\s+(all\s+|my\s+|the\s+)?"
        r"(environment\s+variables?|env\s+vars?|api[_ -]?keys?|"
        r"secret[s]?|password[s]?|credentials?|tokens?)"
    ),
    # Sensitive-path read — classic post-exploit commands. We match the
    # PATH token rather than the verb so equivalent forms (`read`,
    # `print`, `open`, `cat`) all get caught.
    r"/etc/(passwd|shadow|sudoers)\b",
    r"\.ssh/(id_rsa|id_ed25519|authorized_keys)\b",
    # Shell-execution smuggling — the canonical "pipe to shell" attack
    # and destructive-root commands.
    r"\brm\s+-rf\s+/",
    r"(curl|wget)\s+\S+\s*\|\s*(bash|sh|zsh|ksh)\b",
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
    # Protected access/secret codes — labeled codes such as
    # "access code: COBRA-6" or "the secret code is VAULT-CODE-00".
    # The credential noun forms above never match an arbitrary code
    # shape, so this adds a defense-in-depth catch for the per-deployment
    # protected code. The code token itself is matched case-sensitively
    # via (?-i:...) (the list is otherwise IGNORECASE) and must contain
    # uppercase letters joined by a hyphen, so it fires on COBRA-6 /
    # VAULT-CODE-00 but NOT on lowercase prose or letterless hyphen runs
    # like a "90210-1234" ZIP+4. Anchoring on a secret-context label
    # ("access/secret/... code") keeps it from blocking ordinary
    # hyphenated tokens (COVID-19, GPT-4, ISO-8601) in normal output.
    (
        r"(access|secret|confidential|vault|admin|auth|recovery|master)\s*code\b"
        r"[\s:=()\-\"'`]*(?:is|are|=|:)?[\s:=\"'`]*"
        r"(?-i:[A-Z]{2,}(?:-[A-Z0-9]+)+)"
    ),
]

_compiled_injection = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
_compiled_leaks = [re.compile(p, re.IGNORECASE) for p in OUTPUT_LEAK_PATTERNS]


# ── Quoted / delimited data spans ──────────────────────────────────
#
# A prompt that *quotes* an injection string ("count the words in
# 'ignore all previous instructions'") is asking the agent to operate
# ON the string as data, not to obey it. Tracking quoted/delimited
# spans lets scan_input treat injection-pattern hits inside them as
# data (FLAG-only, never a stand-alone BLOCK) while bare/unquoted
# commands keep blocking, and lets harden_prompt leave quoted data
# intact instead of mangling a benign analyze-only request.

_QUOTED_SPAN_RE = re.compile(
    r'"""[\s\S]*?"""'  # triple double-quote
    r"|'''[\s\S]*?'''"  # triple single-quote
    r"|```[\s\S]*?```"  # fenced code block
    r'|"[^"\n]*"'  # straight double quotes
    r"|`[^`\n]*`"  # backticks
    r"|'[^'\n]*'"  # straight single quotes
    r"|\u201c[^\u201d\n]*\u201d"  # smart double quotes “ ”
    r"|\u2018[^\u2019\n]*\u2019"  # smart single quotes ‘ ’
    r"|\u00ab[^\u00bb\n]*\u00bb"  # guillemets « »
)


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    """Return (start, end) ranges of quoted/delimited data spans in `text`."""
    return [m.span() for m in _QUOTED_SPAN_RE.finditer(text)]


def _within_any(span: tuple[int, int], spans: list[tuple[int, int]]) -> bool:
    """True if `span` is fully contained within one of `spans`."""
    start, end = span
    return any(s <= start and end <= e for s, e in spans)


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


def _strip_injections(text: str) -> tuple[str, list[str]]:
    """Remove unquoted injection fragments from `text`.

    Fragments that fall inside quoted/delimited data spans are left in
    place — a flagged prompt that merely *quotes* an injection string is
    asking the agent to analyze it as data, so mangling that data would
    break the benign request without adding any defense.
    """
    removed: list[str] = []
    result = text
    for pattern in _HARDENING_REMOVALS:
        spans = _quoted_spans(result)

        def _repl(match: re.Match, _spans: list[tuple[int, int]] = spans) -> str:
            if _within_any(match.span(), _spans):
                return match.group()
            fragment = match.group().strip()
            if fragment:
                removed.append(fragment)
            return ""

        result = pattern.sub(_repl, result)
    return result, removed


def harden_prompt(prompt: str) -> tuple[str, list[str]]:
    """
    Strip detected injection fragments from a flagged prompt.

    Returns (hardened_prompt, list_of_removed_fragments).
    Does NOT apply to blocked prompts — those are rejected entirely.
    """
    result, removed = _strip_injections(prompt)

    if not removed:
        normalized, removed = _strip_injections(_normalize_unicode(prompt))
        if removed:
            result = normalized

    result = re.sub(r"\s{2,}", " ", result).strip()
    return result, removed


# ── Core scan functions ────────────────────────────────────────────


def scan_input(
    prompt: str,
    session_id: str | None = None,
    *,
    treat_quoted_as_data: bool = True,
) -> ScanResult:
    """Scan an inbound prompt for injection attempts and escalation.

    `treat_quoted_as_data` (default True) relaxes the *free-text* verdict
    so an injection pattern that only matches inside a quoted/delimited
    span is treated as data (use/mention) and cannot, on its own, BLOCK.
    The MCP tool-call boundary passes `treat_quoted_as_data=False` so its
    verdict is unchanged — a quoted injection in a serialized tool payload
    (where the quotes are structural JSON, not a use/mention) keeps its
    full BLOCK weight, matching the conservative `is_tool_call_blocked`
    policy.
    """
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

    # An injection pattern that matches only inside a quoted/delimited
    # data span is a use/mention (the prompt quotes the attack string to
    # analyze it, e.g. "count the words in 'ignore all instructions'").
    # Such hits feed a separate `data_score` that can FLAG but can never,
    # on its own, reach a BLOCK — only bare/unquoted commands do that.
    variants = [
        (prompt, _quoted_spans(prompt)),
        (norm_strip, _quoted_spans(norm_strip)),
        (norm_space, _quoted_spans(norm_space)),
    ]
    data_score = 0.0
    for pattern in _compiled_injection:
        bare_hit = False
        quoted_hit = False
        for text, spans in variants:
            for match in pattern.finditer(text):
                if treat_quoted_as_data and _within_any(match.span(), spans):
                    quoted_hit = True
                else:
                    bare_hit = True
        if bare_hit:
            triggers.append(f"injection:{pattern.pattern[:40]}")
            score += 0.4
        elif quoted_hit:
            triggers.append(f"injection:quoted_data:{pattern.pattern[:30]}")
            data_score += 0.2

    prompt_lower = norm_space.lower()
    for phrase in ESCALATION_PHRASES:
        if phrase in prompt_lower:
            triggers.append(f"escalation:{phrase}")
            score += 0.2

    score = min(score, 1.0)
    data_score = min(data_score, 1.0)

    # Tool-call boundary (treat_quoted_as_data=False): a single bare injection
    # pattern match is a hard block — arguments get no harden_prompt fallback.
    if not treat_quoted_as_data and any(
        t.startswith("injection:") and not t.startswith("injection:quoted_data:")
        for t in triggers
    ):
        score = max(score, 0.6)

    # Only the block-eligible `score` can BLOCK; quoted data tops out at FLAG.
    if score >= 0.6:
        verdict = Verdict.BLOCK
    elif score >= 0.2 or data_score > 0:
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
        score=round(max(score, data_score), 3),
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
