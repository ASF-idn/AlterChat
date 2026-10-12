"""
AlterChat - data_manager.py
===========================

Everything that touches the disk or transforms chat data lives here:

* Path resolution that works both from source and from a PyInstaller one-file
  build (persistent data is stored next to the executable, never in the
  temporary ``_MEIPASS`` extraction folder).
* Atomic JSON persistence for ``settings.json`` and ``contacts.json``.
* A robust WhatsApp ``.txt`` / ``.zip`` export parser (Indonesian + English,
  Android + iOS formats) that separates the two (or more) senders.
* A persona builder that analyses ONLY the target friend's messages and turns
  them into a ``base_system_prompt`` with style notes and few-shot examples.
* A lightweight, dependency-free, file-based RAG (keyword / IDF scoring over
  ``riwayat_[id].txt`` and the imported chat).
* Final prompt assembly (system prompt + RAG memories + last N turns).

The module has no GUI dependencies so it can be unit-tested in isolation.
"""

from __future__ import annotations

import difflib
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import unicodedata
import uuid
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # Pillow is only needed for avatar processing.
    from PIL import Image, ImageOps
except ImportError:  # pragma: no cover - handled gracefully at runtime
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

logger = logging.getLogger("alterchat.data")

APP_NAME = "AlterChat"
APP_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------


def _resolve_app_dir() -> Path:
    """Return the folder that contains the executable (frozen) or this file."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _resolve_bundle_dir() -> Path:
    """Return the folder holding bundled read-only resources.

    For a PyInstaller one-file build this is the temporary ``_MEIPASS`` folder;
    otherwise it is the same as the application folder.
    """
    return Path(getattr(sys, "_MEIPASS", _resolve_app_dir()))


def _is_writable_dir(directory: Path) -> bool:
    """Check whether we can create files inside ``directory``."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write_test_{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _resolve_data_dir(app_dir: Path) -> Path:
    """Prefer ``<app>/data``; fall back to a per-user folder if read-only.

    The fallback matters when the .exe is placed in a protected location such
    as ``C:\\Program Files``.
    """
    preferred = app_dir / "data"
    if _is_writable_dir(preferred):
        return preferred
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    fallback_root = Path(base) if base else Path.home() / ".local" / "share"
    fallback = fallback_root / APP_NAME / "data"
    logger.warning("App folder is read-only, using %s for data.", fallback)
    return fallback


APP_DIR: Path = _resolve_app_dir()
BUNDLE_DIR: Path = _resolve_bundle_dir()
DATA_DIR: Path = _resolve_data_dir(APP_DIR)
AVATAR_DIR: Path = DATA_DIR / "avatars"
HISTORY_DIR: Path = DATA_DIR / "history"
CONTACTS_FILE: Path = DATA_DIR / "contacts.json"
SETTINGS_FILE: Path = DATA_DIR / "settings.json"
LOG_FILE: Path = DATA_DIR / "alterchat.log"
MODELS_DIR: Path = APP_DIR / "models"

DEFAULT_MODEL_FILENAME = "qwen2.5-0.5b-instruct-q4_k_m.gguf"

DEFAULT_SETTINGS: dict[str, Any] = {
    "has_accepted_tc": False,
    "accepted_tc_at": "",
    "language": "en",          # "en" | "id"
    "theme": "System",         # "System" | "Dark" | "Light"
    "model_path": "",          # empty = auto-detect inside ./models
    "n_ctx": 4096,             # context window (0.5B KV cache @4096 ~ 50 MB)
    "n_threads": 0,            # 0 = auto (about half of the logical cores)
    "max_tokens": 64,
    "temperature": 0.45,
}

SUPPORTED_LANGUAGES = ("en", "id")
SUPPORTED_THEMES = ("System", "Dark", "Light")

# Tunables ------------------------------------------------------------------
MAX_EXPORT_BYTES = 64 * 1024 * 1024   # refuse absurdly large exports (64 MB)
MAX_IMPORT_TURNS = 20_000             # turns kept in import_[id].txt for RAG
MIN_FRIEND_MESSAGES = 5               # minimum to build a meaningful persona
HISTORY_CONTEXT_TURNS = 4             # "last 4 messages" of the session
RAG_TOP_K = 4                         # 3-5 relevant past exchanges
SNIPPET_MAX_CHARS = 220               # truncate long memories
AVATAR_SIZE_PX = 512

_ID_RE = re.compile(r"^[a-f0-9]{6,32}$")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ChatMessage:
    """A single cleaned message from a WhatsApp export."""

    timestamp: datetime | None
    sender: str
    text: str


@dataclass
class ParsedChat:
    """Result of parsing a WhatsApp export."""

    messages: list[ChatMessage] = field(default_factory=list)
    sender_counts: Counter = field(default_factory=Counter)
    total_lines: int = 0
    system_messages: int = 0
    skipped_messages: int = 0   # media placeholders, deleted messages, ...

    @property
    def senders(self) -> list[str]:
        """Senders sorted by number of messages (descending)."""
        return [name for name, _ in self.sender_counts.most_common()]


@dataclass
class Turn:
    """Consecutive messages from the same side merged into one turn."""

    role: str               # "user" (the app user) | "assistant" (the friend)
    text: str
    timestamp: datetime | None = None


@dataclass
class HistoryEntry:
    """A line from ``riwayat_[id].txt`` or ``import_[id].txt``."""

    timestamp: datetime | None
    role: str               # "user" | "assistant"
    text: str


@dataclass
class PersonaResult:
    """Everything generated from an import, used to create a contact."""

    system_prompt: str
    stats: dict[str, Any]
    few_shots: list[tuple[str, str]]
    turns: list[Turn]
    user_sender: str


# ---------------------------------------------------------------------------
# WhatsApp parser
# ---------------------------------------------------------------------------

# Invisible direction marks that WhatsApp sprinkles into exports.
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
# Exotic spaces (narrow no-break space before AM/PM, NBSP, figure space).
_ODD_SPACE_RE = re.compile("[\u00a0\u2007\u202f]")

_DATE = r"(?P<d>\d{1,2})[/.\-](?P<m>\d{1,2})[/.\-](?P<y>\d{2,4})"
_TIME = (
    r"(?P<H>\d{1,2})[.:](?P<M>\d{2})(?:[.:](?P<S>\d{2}))?"
    r"(?:\s?(?P<ampm>[AaPp]\.?\s?[Mm]\.?))?"
)
# Android: "dd/mm/yy hh.mm - Sender: Message" (comma after date is optional).
_ANDROID_RE = re.compile(rf"^\s*{_DATE},?\s+{_TIME}\s+[-\u2013\u2014]\s+(?P<body>.*)$")
# iOS: "[dd/mm/yy hh.mm.ss] Sender: Message"
_IOS_RE = re.compile(rf"^\s*\[{_DATE},?\s+{_TIME}\]\s*(?P<body>.*)$")
# "Sender: Message" - sender is the shortest prefix before ": ".
_SENDER_RE = re.compile(r"^(?P<sender>[^:]{1,80}?):(?:\s(?P<text>.*)|$)", re.DOTALL)

# Whole-message patterns that mean "drop this message entirely".
_MESSAGE_NOISE_RE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(polling|poll)\s*:",                       # polls (multi-line)
        r"^(lokasi|location)\s*:\s*https?://",        # shared locations
        r"^(lokasi terkini|live location)",
    )
]

# Line-level noise. A line that fully matches one of these is removed; if a
# message ends up empty it is skipped altogether.
_LINE_NOISE_RE = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"<?\s*media\s+(tidak\s+disertakan|omitted)\s*>?",
        r"(gambar|foto|video|audio|stiker|sticker|gif|dokumen|document|image|photo|"
        r"kontak|contact card|pesan suara|voice message|audio message)\s+"
        r"(tidak\s+disertakan|dihilangkan|dihapus|omitted)",
        r"<\s*(lampiran|attached)\s*:.*>",
        r".+\.(jpe?g|png|webp|gif|mp4|3gp|mov|opus|ogg|mp3|m4a|aac|pdf|docx?|xlsx?|"
        r"pptx?|vcf|zip|rar|apk|txt)\s*\((file\s+terlampir|file\s+attached)\)",
        r"(pesan ini (telah )?dihapus|anda (telah )?menghapus pesan ini|"
        r"this message was deleted|you deleted this message)\.?",
        r"null",
        r"(pesan sekali lihat|view once message)",
        r"(panggilan (suara|video) tak terjawab|missed (voice|video) call).*",
        r"(menunggu pesan ini|waiting for this message).*",
        r".*(pesan dan panggilan terenkripsi|messages and calls are end-to-end encrypted).*",
        r"(pesan sementara|disappearing messages)\b.*",
    )
]

# "<Pesan ini diedit>" / "<This message was edited>" suffix.
_EDITED_RE = re.compile(r"\s*<\s*(pesan ini diedit|this message was edited)\s*>\s*$", re.IGNORECASE)


def _clean_line(line: str) -> str:
    """Remove invisible characters and normalise odd spaces."""
    line = _INVISIBLE_RE.sub("", line)
    line = _ODD_SPACE_RE.sub(" ", line)
    return line.rstrip("\r\n")


def _build_timestamp(match: re.Match[str]) -> datetime | None:
    """Convert regex groups into a ``datetime`` (day-first, Indonesian style)."""
    try:
        day, month, year = int(match["d"]), int(match["m"]), int(match["y"])
        if year < 100:
            year += 2000
        # Fallback for US-style exports (mm/dd/yy) when day-first is impossible.
        if month > 12 >= day:
            day, month = month, day
        hour, minute = int(match["H"]), int(match["M"])
        second = int(match["S"] or 0)
        ampm = (match["ampm"] or "").lower().replace(".", "").replace(" ", "")
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return datetime(year, month, day, hour, minute, second)
    except (TypeError, ValueError):
        return None


def _looks_like_system_sender(sender: str) -> bool:
    """Heuristic guard against system messages that happen to contain ':'."""
    sender = sender.strip()
    if not sender or len(sender) > 60:
        return True
    if sender.count(" ") > 6:
        return True
    return any(ch in sender for ch in ('"', "\u201c", "\u201d"))


def _clean_message_text(text: str) -> str:
    """Strip media placeholders, deleted markers, edited suffixes, etc."""
    text = _EDITED_RE.sub("", text).strip()
    if not text:
        return ""
    if any(p.search(text) for p in _MESSAGE_NOISE_RE):
        return ""
    kept = [
        line for line in text.split("\n")
        if line.strip() and not any(p.fullmatch(line.strip()) for p in _LINE_NOISE_RE)
    ]
    return "\n".join(kept).strip()


def parse_whatsapp_text(raw_text: str) -> ParsedChat:
    """Parse the raw text of a WhatsApp export.

    Supported line formats (all with Indonesian ``hh.mm`` or ``hh:mm`` times)::

        12/01/23 14.05 - Budi: halo
        12/01/2023, 14.05 - Budi: halo
        1/12/23, 9:05 PM - Budi: halo
        [12/01/23 14.05.33] Budi: halo

    Lines that do not start with a timestamp are continuation lines of a
    multi-line message. Timestamped lines without a ``Sender:`` part are
    system messages and are discarded.
    """
    result = ParsedChat()
    # Each raw item: [timestamp, sender or None (system), list of text lines]
    raw_messages: list[list[Any]] = []

    for raw_line in raw_text.splitlines():
        result.total_lines += 1
        line = _clean_line(raw_line)
        match = _ANDROID_RE.match(line) or _IOS_RE.match(line)
        if match:
            body = match["body"].strip()
            timestamp = _build_timestamp(match)
            sender_match = _SENDER_RE.match(body)
            if sender_match and not _looks_like_system_sender(sender_match["sender"]):
                raw_messages.append(
                    [timestamp, sender_match["sender"].strip(), [sender_match["text"] or ""]]
                )
            else:
                raw_messages.append([timestamp, None, [body]])
                result.system_messages += 1
        elif raw_messages:
            # Continuation of the previous (multi-line) message.
            raw_messages[-1][2].append(line)
        # Text before the first timestamp is ignored.

    for timestamp, sender, parts in raw_messages:
        if sender is None:
            continue
        text = _clean_message_text("\n".join(parts))
        if not text:
            result.skipped_messages += 1
            continue
        result.messages.append(ChatMessage(timestamp, sender, text))
        result.sender_counts[sender] += 1

    return result


def _decode_bytes(data: bytes) -> str:
    """Decode export bytes trying the encodings WhatsApp actually uses."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


def read_export_text(path: str | Path) -> str:
    """Read a WhatsApp export from a ``.txt`` file or an iOS/Android ``.zip``."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    if path.stat().st_size > MAX_EXPORT_BYTES:
        raise ValueError("The export file is too large (limit is 64 MB).")

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            txt_members = [n for n in archive.namelist() if n.lower().endswith(".txt")]
            if not txt_members:
                raise ValueError("The .zip archive does not contain a chat .txt file.")
            # Prefer iOS "_chat.txt" or Android "WhatsApp Chat with ..." files.
            txt_members.sort(key=lambda n: (
                not n.lower().endswith("_chat.txt"),
                "whatsapp" not in n.lower(),
                len(n),
            ))
            info = archive.getinfo(txt_members[0])
            if info.file_size > MAX_EXPORT_BYTES:
                raise ValueError("The chat inside the archive is too large (limit is 64 MB).")
            data = archive.read(info)
    else:
        data = path.read_bytes()
    return _decode_bytes(data)


def parse_whatsapp_export(path: str | Path) -> ParsedChat:
    """Read and parse an export file. Raises ``ValueError``/``OSError``."""
    return parse_whatsapp_text(read_export_text(path))


def _normalize_name(name: str) -> str:
    """Casefold, strip emojis/punctuation and collapse whitespace."""
    name = unicodedata.normalize("NFKC", name).casefold()
    name = re.sub(r"[^\w\s]", " ", name)
    return " ".join(name.split())


def match_sender(contact_name: str, senders: Sequence[str]) -> str | None:
    """Find the sender that best matches the contact name typed by the user.

    Strategy: exact match -> substring match -> token overlap -> fuzzy ratio.
    Returns ``None`` when nothing is reasonably similar.
    """
    target = _normalize_name(contact_name)
    if not target or not senders:
        return None
    normalized = {s: _normalize_name(s) for s in senders}

    for sender, norm in normalized.items():
        if norm == target:
            return sender

    contains = [s for s, n in normalized.items() if n and (target in n or n in target)]
    if contains:
        return min(contains, key=lambda s: abs(len(normalized[s]) - len(target)))

    target_tokens = set(target.split())
    best_sender, best_score = None, 0.0
    for sender, norm in normalized.items():
        tokens = set(norm.split())
        if not tokens:
            continue
        jaccard = len(tokens & target_tokens) / len(tokens | target_tokens)
        fuzzy = difflib.SequenceMatcher(None, norm, target).ratio()
        score = max(jaccard, fuzzy * 0.9)
        if score > best_score:
            best_sender, best_score = sender, score
    return best_sender if best_score >= 0.5 else None


# ---------------------------------------------------------------------------
# Tokenisation helpers (shared by the persona builder and the RAG)
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_TOKEN_ELONGATION_RE = re.compile(r"(\w)\1{2,}")
_TEXT_ELONGATION_RE = re.compile(r"([^\W\d_])\1{2,}")
_LAUGH_RE = re.compile(
    r"\b(?:[wk]*wk[wk]*|(?:ha|he|hi|hu|xi){2,}h?|a?wok(?:wok)+\w*|lo+l|lmf?ao+|rofl)\b",
    re.IGNORECASE,
)
_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001F5FF\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF\U0001F900-\U0001F9FF\U0001FA70-\U0001FAFF"
    "\u2600-\u26FF\u2700-\u27BF]"
)

# Pure function words: never "signature" slang.
_FUNCTION_WORDS = frozenset(
    """
    yang dan di ke dari ini itu untuk dengan ada pada dalam akan atau karena adalah
    saya kami kita mereka anda oleh sebagai bahwa para pun telah agar
    the a an and or to of in on at is are was were be been it this that for with
    i you he she we they my your me his her our their so but if do does did not
    have has had as by from will would can could
    """.split()
)

# Broader stop list for retrieval (slang particles carry style, not topic).
_RAG_STOPWORDS = _FUNCTION_WORDS | frozenset(
    """
    aku kamu gue gua gw lo lu loe elu dia ya yaa iya iyaa yg ga gak ngga nggak enggak
    engga tidak tak bukan sih dong deh kok nih tuh aja saja juga udah udh sudah belum
    blm lagi lg mau apa gimana gmn kenapa knp kapan dimana mana siapa kalo kalau klo
    tapi tp jadi jd sama sm emang memang bisa bgt banget kan lah kah yah oh eh ah hm
    hmm mah wah nah ok oke okay okey yes no yeah just like what how why when where
    who im its dont thats ini itu sini situ gitu gini begitu begini terus trus
    """.split()
)

_ID_MARKERS = frozenset(
    "yang dan aku kamu gak ga nggak udah banget aja sih dong deh kok gue gw lu lo "
    "nih tuh juga lagi mau bisa itu ini ya iya kalo kalau sama tapi emang".split()
)
_EN_MARKERS = frozenset(
    "the and you is are what that this it to of for just like yeah so but not have "
    "do my me was with".split()
)


def tokenize_words(text: str) -> list[str]:
    """Lowercase word tokens with URLs removed and elongations collapsed."""
    text = _URL_RE.sub(" ", text.lower())
    return [_TOKEN_ELONGATION_RE.sub(r"\1", tok) for tok in _WORD_RE.findall(text)]


def extract_keywords(text: str) -> list[str]:
    """Topic keywords used by the RAG scorer."""
    keywords: list[str] = []
    for tok in tokenize_words(text):
        if len(tok) < 2 or tok.isdigit() or tok in _RAG_STOPWORDS:
            continue
        if _LAUGH_RE.fullmatch(tok):
            continue
        # Light Indonesian normalisation: "rumahnya" -> "rumah".
        if tok.endswith("nya") and len(tok) - 3 >= 4:
            tok = tok[:-3]
        keywords.append(tok)
    return keywords


# ---------------------------------------------------------------------------
# Persona builder (uses ONLY the target friend's messages for style)
# ---------------------------------------------------------------------------


def build_turns(messages: Iterable[ChatMessage], friend_sender: str) -> list[Turn]:
    """Merge consecutive messages from the same side into turns.

    The friend becomes ``assistant``; everybody else (the app user, or other
    members in a group chat) becomes ``user``.
    """
    turns: list[Turn] = []
    for msg in messages:
        role = "assistant" if msg.sender == friend_sender else "user"
        if turns and turns[-1].role == role:
            turns[-1].text += "\n" + msg.text
        else:
            turns.append(Turn(role=role, text=msg.text, timestamp=msg.timestamp))
    return turns


def _detect_language(tokens: Sequence[str]) -> str:
    """Return ``id``, ``en`` or ``mixed`` from marker-word frequencies."""
    id_hits = sum(1 for t in tokens if t in _ID_MARKERS)
    en_hits = sum(1 for t in tokens if t in _EN_MARKERS)
    if id_hits == 0 and en_hits == 0:
        return "id"
    ratio = id_hits / (id_hits + en_hits)
    if ratio >= 0.75:
        return "id"
    if ratio <= 0.25:
        return "en"
    return "mixed"


def _signature_words(friend_msgs: Sequence[str], other_msgs: Sequence[str], limit: int = 12) -> list[str]:
    """Words the friend uses a lot *and* more than the other side (log-odds)."""
    friend_counts = Counter(t for m in friend_msgs for t in tokenize_words(m))
    other_counts = Counter(t for m in other_msgs for t in tokenize_words(m))
    total_f = sum(friend_counts.values()) or 1
    total_o = sum(other_counts.values()) or 1

    def eligible(word: str) -> bool:
        return (
            len(word) >= 2
            and not word.isdigit()
            and word not in _FUNCTION_WORDS
            and not _LAUGH_RE.fullmatch(word)
        )

    scored: list[tuple[float, str]] = []
    for word, count in friend_counts.items():
        if count < 3 or not eligible(word):
            continue
        p_friend = (count + 0.5) / (total_f + 1)
        p_other = (other_counts.get(word, 0) + 0.5) / (total_o + 1)
        score = math.sqrt(count) * math.log(p_friend / p_other)
        if score > 0:
            scored.append((score, word))
    scored.sort(reverse=True)
    result = [w for _, w in scored[:limit]]

    # Too few distinctive words (small chat)? Fill with plain frequency.
    if len(result) < limit // 2:
        for word, _ in friend_counts.most_common():
            if len(result) >= limit:
                break
            if eligible(word) and word not in result:
                result.append(word)
    return result


def analyze_style(friend_msgs: Sequence[str], other_msgs: Sequence[str]) -> dict[str, Any]:
    """Compute texting-style statistics for the friend."""
    n = max(len(friend_msgs), 1)
    word_counts = [len(m.split()) for m in friend_msgs] or [0]
    with_letters = [m for m in friend_msgs if any(c.isalpha() for c in m)]
    lowercase = sum(1 for m in with_letters if m == m.lower())
    emoji_counter: Counter = Counter()
    laugh_counter: Counter = Counter()
    emoji_msgs = 0
    for msg in friend_msgs:
        emojis = _EMOJI_RE.findall(msg)
        if emojis:
            emoji_msgs += 1
            emoji_counter.update(emojis)
        laugh_counter.update(m.lower()[:10] for m in _LAUGH_RE.findall(msg))

    all_tokens = [t for m in friend_msgs for t in tokenize_words(m)]
    return {
        "avg_words": round(sum(word_counts) / len(word_counts), 1),
        "lowercase_ratio": round(lowercase / max(len(with_letters), 1), 2),
        "elongation_ratio": round(sum(1 for m in friend_msgs if _TEXT_ELONGATION_RE.search(m)) / n, 2),
        "emoji_ratio": round(emoji_msgs / n, 2),
        "top_emojis": [e for e, _ in emoji_counter.most_common(5)],
        "top_laugh": laugh_counter.most_common(1)[0][0] if laugh_counter else "",
        "question_ratio": round(sum(1 for m in friend_msgs if "?" in m) / n, 2),
        "exclaim_ratio": round(sum(1 for m in friend_msgs if "!" in m) / n, 2),
        "end_punct_ratio": round(sum(1 for m in friend_msgs if m.rstrip()[-1:] in ".!?") / n, 2),
        "language": _detect_language(all_tokens),
        "signature_words": _signature_words(friend_msgs, other_msgs),
    }


def _single_line(text: str, limit: int) -> str:
    """Flatten newlines to ' / ' and truncate (keeps prompts compact)."""
    text = " / ".join(part.strip() for part in text.split("\n") if part.strip())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def _spread_pick(items: list[Any], count: int) -> list[Any]:
    """Pick ``count`` items evenly spread over the list (keeps diversity)."""
    if len(items) <= count:
        return items
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def _select_sample_messages(friend_msgs: Sequence[str], signature: Sequence[str], limit: int = 8) -> list[str]:
    """Representative real messages, preferring ones with signature words."""
    sig = set(signature)
    seen: set[str] = set()
    candidates: list[tuple[int, int, str]] = []
    for idx, msg in enumerate(friend_msgs):
        flat = _single_line(msg, 160)
        key = flat.lower()
        if key in seen or not 8 <= len(flat) <= 160 or _URL_RE.search(flat):
            continue
        seen.add(key)
        score = sum(1 for t in tokenize_words(flat) if t in sig)
        candidates.append((score, idx, flat))
    best = sorted(candidates, key=lambda c: (-c[0], -c[1]))[: limit * 4]
    best.sort(key=lambda c: c[1])  # chronological, then spread
    return [c[2] for c in _spread_pick(best, limit)]


def _select_few_shots(turns: Sequence[Turn], signature: Sequence[str], limit: int = 4) -> list[tuple[str, str]]:
    """Pick short (user turn -> friend reply) pairs as dialogue examples."""
    sig = set(signature)
    candidates: list[tuple[int, int, str, str]] = []
    for i in range(len(turns) - 1):
        user_turn, friend_turn = turns[i], turns[i + 1]
        if user_turn.role != "user" or friend_turn.role != "assistant":
            continue
        u_text, a_text = _single_line(user_turn.text, 140), _single_line(friend_turn.text, 180)
        if not (2 <= len(u_text) <= 140 and 2 <= len(a_text) <= 180):
            continue
        if _URL_RE.search(u_text) or _URL_RE.search(a_text):
            continue
        score = sum(1 for t in tokenize_words(a_text) if t in sig)
        score += 1 if 8 <= len(a_text) <= 120 else 0
        candidates.append((score, i, u_text, a_text))
    best = sorted(candidates, key=lambda c: (-c[0], -c[1]))[: limit * 4]
    best.sort(key=lambda c: c[1])
    return [(c[2], c[3]) for c in _spread_pick(best, limit)]


def compose_system_prompt(
    name: str,
    stats: dict[str, Any],
    samples: Sequence[str],
    few_shots: Sequence[tuple[str, str]],
) -> str:
    """Turn style statistics into the ``base_system_prompt``.

    Instructions are written in English (small instruction-tuned models follow
    English instructions best) while the model is told to reply in the
    friend's own language and slang.
    """
    avg = stats.get("avg_words", 8)
    if avg <= 4:
        length_desc = "very short, often just a few words"
    elif avg <= 9:
        length_desc = "short and casual"
    elif avg <= 18:
        length_desc = "medium length"
    else:
        length_desc = "fairly long"

    lower = stats.get("lowercase_ratio", 0)
    caps_desc = (
        "mostly all lowercase, rarely uses capital letters" if lower >= 0.7
        else "mixed capitalization" if lower >= 0.35
        else "normal capitalization"
    )
    punct_desc = (
        "rarely ends messages with punctuation"
        if stats.get("end_punct_ratio", 0) < 0.2 else "often uses punctuation"
    )
    language_desc = {
        "id": "casual, informal Indonesian (bahasa gaul), exactly like the examples",
        "en": "casual English, exactly like the examples",
        "mixed": "a casual mix of Indonesian and English, exactly like the examples",
    }.get(stats.get("language", "id"), "the same language as the examples")

    lines = [
        f"You are {name}. You are chatting on WhatsApp with a close friend.",
        f"You are NOT an AI assistant. Never say you are an AI, a bot or a language model. "
        f"Stay in character as {name} at all times.",
        "",
        f"## {name}'s texting style",
        f"- Language: {language_desc}.",
        f"- Messages are {length_desc} (about {avg:.0f} words).",
        f"- Writing: {caps_desc}; {punct_desc}.",
    ]
    if stats.get("elongation_ratio", 0) >= 0.08:
        lines.append("- Often stretches words for emphasis (e.g. 'bangettt', 'iyaaa').")
    if stats.get("top_laugh"):
        lines.append(f"- Laughs by typing '{stats['top_laugh']}'.")
    if stats.get("emoji_ratio", 0) >= 0.05 and stats.get("top_emojis"):
        lines.append(f"- Uses emojis like {' '.join(stats['top_emojis'])}.")
    else:
        lines.append("- Rarely uses emojis.")
    if stats.get("signature_words"):
        lines.append(f"- Typical words and slang: {', '.join(stats['signature_words'])}.")

    if samples:
        lines += ["", f"## Real messages {name} has sent"]
        lines += [f'- "{s}"' for s in samples]

    if few_shots:
        lines += ["", "## Example conversation"]
        for user_text, friend_text in few_shots:
            lines += [f"Friend: {user_text}", f"{name}: {friend_text}"]

    lines += [
        "",
        "## Rules",
        f"- Reply only as {name}, with one short, natural chat message in {name}'s style and language.",
        f'- Do not start with "{name}:" and never write the friend\'s side of the conversation.',
        "- Be casual and human. Do not sound like a formal or overly helpful assistant.",
    ]
    return "\n".join(lines)


def default_system_prompt(name: str) -> str:
    """Fallback prompt for contacts without an imported persona."""
    return compose_system_prompt(name, {"avg_words": 8, "language": "id"}, [], [])


def build_persona(contact_name: str, friend_sender: str, parsed: ParsedChat) -> PersonaResult:
    """Build the persona for ``friend_sender`` from a parsed chat."""
    friend_msgs = [m.text for m in parsed.messages if m.sender == friend_sender]
    other_msgs = [m.text for m in parsed.messages if m.sender != friend_sender]
    others = Counter({s: c for s, c in parsed.sender_counts.items() if s != friend_sender})
    user_sender = others.most_common(1)[0][0] if others else ""

    turns = build_turns(parsed.messages, friend_sender)
    stats = analyze_style(friend_msgs, other_msgs)
    friend_turns = sum(1 for t in turns if t.role == "assistant")
    stats.update(
        friend_messages=len(friend_msgs),
        total_messages=len(parsed.messages),
        avg_burst=round(len(friend_msgs) / max(friend_turns, 1), 2),
        participants=len(parsed.sender_counts),
    )
    samples = _select_sample_messages(friend_msgs, stats["signature_words"])
    few_shots = _select_few_shots(turns, stats["signature_words"])
    prompt = compose_system_prompt(contact_name, stats, samples, few_shots)
    return PersonaResult(prompt, stats, few_shots, turns, user_sender)


# ---------------------------------------------------------------------------
# History file format
# ---------------------------------------------------------------------------


def _format_history_line(timestamp: datetime | None, role: str, text: str) -> str:
    """``ISO-timestamp<TAB>role<TAB>"json-escaped text"`` (one line per message)."""
    ts = timestamp.isoformat(timespec="seconds") if timestamp else ""
    return f"{ts}\t{role}\t{json.dumps(text, ensure_ascii=False)}\n"


def _parse_history_line(line: str) -> HistoryEntry | None:
    """Inverse of :func:`_format_history_line`. Returns ``None`` if invalid."""
    parts = line.rstrip("\n").split("\t", 2)
    if len(parts) != 3 or parts[1] not in ("user", "assistant"):
        return None
    ts_raw, role, text_raw = parts
    try:
        text = json.loads(text_raw)
        if not isinstance(text, str):
            text = str(text)
    except json.JSONDecodeError:
        text = text_raw
    try:
        timestamp = datetime.fromisoformat(ts_raw) if ts_raw else None
    except ValueError:
        timestamp = None
    return HistoryEntry(timestamp, role, text)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


def clean_model_reply(text: str, persona_name: str) -> str:
    """Remove artefacts small models tend to produce (role prefixes, etc.)."""
    for token in ("<|im_end|>", "<|endoftext|>", "<|im_start|>"):
        text = text.replace(token, "")
    text = text.strip()
    prefix = re.compile(rf"^\s*(?:{re.escape(persona_name)}|assistant)\s*:\s*", re.IGNORECASE)
    text = prefix.sub("", text)
    # Cut if the model started writing the other side of the conversation.
    cut = re.search(r"\n\s*(?:Friend|User|Teman|You|Me)\s*:", text, re.IGNORECASE)
    if cut:
        text = text[: cut.start()]
    return text.strip()


# ---------------------------------------------------------------------------
# DataManager
# ---------------------------------------------------------------------------


class DataManager:
    """Thread-safe facade over all persistent data used by the app."""

    def __init__(self) -> None:
        self.app_dir = APP_DIR
        self.data_dir = DATA_DIR
        self.avatar_dir = AVATAR_DIR
        self.history_dir = HISTORY_DIR
        self.contacts_file = CONTACTS_FILE
        self.settings_file = SETTINGS_FILE
        self.log_file = LOG_FILE
        self.models_dir = MODELS_DIR
        # RLock: public methods may call each other while holding the lock.
        self._lock = threading.RLock()
        # path -> ((mtime_ns, size), entries, keyword sets)
        self._index_cache: dict[str, tuple[tuple[int, int], list[HistoryEntry], list[frozenset[str]]]] = {}

    # ----------------------------------------------------------------- setup
    def ensure_data_structure(self) -> None:
        """Create ``/data`` (and sub-folders) and default JSON files if missing.

        When running as a PyInstaller build, the bundled ``data`` template is
        used to seed the persistent folder. Personal data is never copied from
        the bundle: contacts always start empty and the EULA must be accepted.
        """
        for directory in (self.data_dir, self.avatar_dir, self.history_dir):
            directory.mkdir(parents=True, exist_ok=True)
        try:
            self.models_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # read-only app folder: models can still be picked in Settings

        bundled_settings = BUNDLE_DIR / "data" / "settings.json"
        if not self.settings_file.exists():
            seed = dict(DEFAULT_SETTINGS)
            if bundled_settings.is_file() and bundled_settings.resolve() != self.settings_file.resolve():
                seed.update(self._read_json(bundled_settings, {}) or {})
                seed["has_accepted_tc"] = False
                seed["accepted_tc_at"] = ""
            self._write_json(self.settings_file, self._sanitize_settings(seed))
        if not self.contacts_file.exists():
            self._write_json(self.contacts_file, [])

    # ------------------------------------------------------------- JSON utils
    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        """Read JSON; on corruption keep a ``.corrupt`` backup and return default."""
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            logger.error("Could not read %s (%s). Using defaults.", path, exc)
            try:
                shutil.copy2(path, path.with_suffix(path.suffix + ".corrupt"))
            except OSError:
                pass
            return default

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        """Atomic write: temp file + fsync + ``os.replace``."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    # --------------------------------------------------------------- settings
    @staticmethod
    def _sanitize_settings(raw: dict[str, Any]) -> dict[str, Any]:
        """Merge with defaults and coerce invalid values."""
        settings = dict(DEFAULT_SETTINGS)
        if isinstance(raw, dict):
            settings.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
        if settings["language"] not in SUPPORTED_LANGUAGES:
            settings["language"] = "en"
        if settings["theme"] not in SUPPORTED_THEMES:
            settings["theme"] = "System"
        settings["has_accepted_tc"] = bool(settings["has_accepted_tc"])
        for key, low, high in (("n_ctx", 512, 32768), ("n_threads", 0, 256), ("max_tokens", 16, 2048)):
            try:
                settings[key] = min(max(int(settings[key]), low), high)
            except (TypeError, ValueError):
                settings[key] = DEFAULT_SETTINGS[key]
        try:
            settings["temperature"] = min(max(float(settings["temperature"]), 0.0), 2.0)
        except (TypeError, ValueError):
            settings["temperature"] = DEFAULT_SETTINGS["temperature"]
        settings["model_path"] = str(settings.get("model_path") or "")
        return settings

    def load_settings(self) -> dict[str, Any]:
        with self._lock:
            return self._sanitize_settings(self._read_json(self.settings_file, {}))

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            clean = self._sanitize_settings(settings)
            self._write_json(self.settings_file, clean)
            return clean

    def update_settings(self, **changes: Any) -> dict[str, Any]:
        """Read-modify-write a subset of settings and return the result."""
        with self._lock:
            settings = self.load_settings()
            settings.update(changes)
            return self.save_settings(settings)

    # --------------------------------------------------------------- contacts
    def _read_contacts(self) -> list[dict[str, Any]]:
        data = self._read_json(self.contacts_file, [])
        if not isinstance(data, list):
            return []
        return [c for c in data if isinstance(c, dict) and _ID_RE.match(str(c.get("id", "")))]

    def load_contacts(self) -> list[dict[str, Any]]:
        """All contacts, most recently active first (like WhatsApp)."""
        with self._lock:
            contacts = self._read_contacts()
        contacts.sort(key=lambda c: c.get("last_activity") or c.get("created_at") or "", reverse=True)
        return contacts

    def get_contact(self, contact_id: str) -> dict[str, Any] | None:
        with self._lock:
            return next((dict(c) for c in self._read_contacts() if c["id"] == contact_id), None)

    def create_contact(
        self,
        name: str,
        friend_sender: str,
        parsed: ParsedChat,
        avatar_source: str | Path | None = None,
    ) -> dict[str, Any]:
        """Build a persona from ``parsed`` and persist a new contact.

        Raises ``ValueError`` for invalid input and ``OSError`` for disk errors.
        Partially written files are cleaned up on failure.
        """
        name = name.strip()
        if not name:
            raise ValueError("Contact name is empty.")
        if friend_sender not in parsed.sender_counts:
            raise ValueError("The selected sender does not exist in this chat.")

        persona = build_persona(name, friend_sender, parsed)
        if persona.stats["friend_messages"] < MIN_FRIEND_MESSAGES:
            raise ValueError(
                f"Only {persona.stats['friend_messages']} messages from {friend_sender} "
                f"were found (minimum {MIN_FRIEND_MESSAGES})."
            )

        contact_id = uuid.uuid4().hex[:12]
        created_files: list[Path] = []
        try:
            avatar_rel = ""
            if avatar_source:
                try:
                    avatar_rel = self._save_avatar(avatar_source, contact_id)
                    created_files.append(self.data_dir / avatar_rel)
                except Exception as exc:  # avatar problems must not block the import
                    logger.warning("Avatar could not be saved: %s", exc)

            import_file = self.import_path(contact_id)
            self._write_import_file(import_file, persona.turns)
            created_files.append(import_file)

            history_file = self.history_path(contact_id)
            history_file.touch(exist_ok=True)
            created_files.append(history_file)

            now = datetime.now().isoformat(timespec="seconds")
            contact = {
                "id": contact_id,
                "name": name,
                "avatar_path": avatar_rel,
                "system_prompt": persona.system_prompt,
                "friend_sender": friend_sender,
                "user_sender": persona.user_sender,
                "few_shots": persona.few_shots,
                "stats": persona.stats,
                "created_at": now,
                "last_activity": "",
                "last_message": "",
            }
            with self._lock:
                contacts = self._read_contacts()
                contacts.append(contact)
                self._write_json(self.contacts_file, contacts)
            logger.info("Created contact %s (%s) with %d friend messages.",
                        name, contact_id, persona.stats["friend_messages"])
            return dict(contact)
        except Exception:
            for path in created_files:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def delete_contact(self, contact_id: str) -> None:
        """Remove a contact together with its avatar and history files."""
        with self._lock:
            contacts = self._read_contacts()
            target = next((c for c in contacts if c["id"] == contact_id), None)
            self._write_json(self.contacts_file, [c for c in contacts if c["id"] != contact_id])
            paths = [self.history_path(contact_id), self.import_path(contact_id)]
            if target and target.get("avatar_path"):
                paths.append(self.data_dir / target["avatar_path"])
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Could not delete %s: %s", path, exc)
                self._index_cache.pop(str(path), None)

    def clear_all_data(self) -> None:
        """Delete every contact, avatar and history file (settings are kept)."""
        with self._lock:
            for directory in (self.avatar_dir, self.history_dir):
                if not directory.is_dir():
                    continue
                for item in directory.iterdir():
                    if item.name == ".gitkeep":
                        continue
                    try:
                        if item.is_dir():
                            shutil.rmtree(item)
                        else:
                            item.unlink()
                    except OSError as exc:
                        logger.warning("Could not delete %s: %s", item, exc)
            self._write_json(self.contacts_file, [])
            self._index_cache.clear()
        logger.info("All user data cleared.")

    def _touch_contact(self, contact_id: str, preview: str, timestamp: datetime) -> None:
        """Update ``last_message`` / ``last_activity`` for the sidebar."""
        with self._lock:
            contacts = self._read_contacts()
            for contact in contacts:
                if contact["id"] == contact_id:
                    contact["last_message"] = _truncate(" ".join(preview.split()), 80)
                    contact["last_activity"] = timestamp.isoformat(timespec="seconds")
                    break
            else:
                return
            self._write_json(self.contacts_file, contacts)

    # ---------------------------------------------------------------- avatars
    def _save_avatar(self, source: str | Path, contact_id: str) -> str:
        """Square-crop, resize and store an avatar as PNG. Returns relative path."""
        if Image is None or ImageOps is None:
            raise RuntimeError("Pillow is not installed.")
        target = self.avatar_dir / f"{contact_id}.png"
        with Image.open(source) as img:
            img = ImageOps.exif_transpose(img).convert("RGBA")
            img = ImageOps.fit(img, (AVATAR_SIZE_PX, AVATAR_SIZE_PX), Image.Resampling.LANCZOS)
            img.save(target, format="PNG", optimize=True)
        return target.relative_to(self.data_dir).as_posix()

    def avatar_abs_path(self, contact: dict[str, Any] | None) -> Path | None:
        """Absolute avatar path, or ``None`` if not set / missing on disk."""
        if not contact or not contact.get("avatar_path"):
            return None
        path = (self.data_dir / contact["avatar_path"]).resolve()
        try:
            path.relative_to(self.data_dir.resolve())  # block path traversal
        except ValueError:
            return None
        return path if path.is_file() else None

    # ---------------------------------------------------------------- history
    def history_path(self, contact_id: str) -> Path:
        if not _ID_RE.match(contact_id):
            raise ValueError(f"Invalid contact id: {contact_id!r}")
        return self.history_dir / f"riwayat_{contact_id}.txt"

    def import_path(self, contact_id: str) -> Path:
        if not _ID_RE.match(contact_id):
            raise ValueError(f"Invalid contact id: {contact_id!r}")
        return self.history_dir / f"import_{contact_id}.txt"

    @staticmethod
    def _write_import_file(path: Path, turns: Sequence[Turn]) -> None:
        """Store the imported chat (last N turns) in the history format for RAG."""
        with open(path, "w", encoding="utf-8") as fh:
            for turn in turns[-MAX_IMPORT_TURNS:]:
                fh.write(_format_history_line(turn.timestamp, turn.role, turn.text))

    def append_history(self, contact_id: str, role: str, text: str) -> datetime:
        """Append one message to ``riwayat_[id].txt`` immediately."""
        if role not in ("user", "assistant"):
            raise ValueError(f"Invalid role: {role!r}")
        timestamp = datetime.now()
        line = _format_history_line(timestamp, role, text)
        with self._lock:
            with open(self.history_path(contact_id), "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
            self._touch_contact(contact_id, text, timestamp)
        return timestamp

    @staticmethod
    def _read_entries(path: Path) -> list[HistoryEntry]:
        entries: list[HistoryEntry] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    entry = _parse_history_line(line)
                    if entry is not None:
                        entries.append(entry)
        except FileNotFoundError:
            pass
        return entries

    def load_history(self, contact_id: str, limit: int | None = None) -> list[HistoryEntry]:
        """Messages exchanged inside AlterChat (oldest first)."""
        with self._lock:
            entries = self._read_entries(self.history_path(contact_id))
        return entries[-limit:] if limit else entries

    def clear_history(self, contact_id: str) -> None:
        """Empty ``riwayat_[id].txt`` (the imported chat is kept)."""
        with self._lock:
            path = self.history_path(contact_id)
            path.write_text("", encoding="utf-8")
            self._index_cache.pop(str(path), None)
            contacts = self._read_contacts()
            for contact in contacts:
                if contact["id"] == contact_id:
                    contact["last_message"] = ""
                    contact["last_activity"] = ""
            self._write_json(self.contacts_file, contacts)

    # -------------------------------------------------------------------- RAG
    def _load_indexed(self, path: Path) -> tuple[list[HistoryEntry], list[frozenset[str]]]:
        """Entries + keyword sets for a history file (cached by mtime/size)."""
        try:
            stat = path.stat()
        except FileNotFoundError:
            return [], []
        signature = (stat.st_mtime_ns, stat.st_size)
        key = str(path)
        with self._lock:
            cached = self._index_cache.get(key)
            if cached and cached[0] == signature:
                return cached[1], cached[2]
            entries = self._read_entries(path)
        keywords = [frozenset(extract_keywords(e.text)) for e in entries]
        with self._lock:
            self._index_cache[key] = (signature, entries, keywords)
        return entries, keywords

    @staticmethod
    def _rank_exchanges(
        sources: Sequence[tuple[list[HistoryEntry], list[frozenset[str]]]],
        query: set[str],
        top_k: int,
    ) -> list[list[HistoryEntry]]:
        """Score 2-message windows by IDF-weighted keyword overlap.

        * ``df`` is computed across every entry of every source so rare words
          (names, places, topics) weigh more than common ones.
        * A window that is a real exchange (user -> friend) gets a 20% bonus.
        * Selected windows never overlap; output keeps chronological order.
        """
        total = sum(len(entries) for entries, _ in sources)
        if total == 0:
            return []
        df = Counter()
        for _, kw_sets in sources:
            for kws in kw_sets:
                for word in query & kws:
                    df[word] += 1
        if not df:
            return []
        idf = {w: math.log((total + 1) / (df[w] + 1)) + 1.0 for w in df}

        scored: list[tuple[float, int, int]] = []   # (score, source index, start)
        for s_idx, (entries, kw_sets) in enumerate(sources):
            for i in range(len(entries)):
                j = min(i + 1, len(entries) - 1)
                overlap = query & (kw_sets[i] | kw_sets[j])
                if not overlap:
                    continue
                score = sum(idf.get(w, 0.0) for w in overlap)
                if entries[i].role == "user" and entries[j].role == "assistant":
                    score *= 1.2
                scored.append((score, s_idx, i))
        scored.sort(key=lambda item: (-item[0], -item[2]))

        used: set[tuple[int, int]] = set()
        chosen: list[tuple[int, int]] = []
        for _, s_idx, i in scored:
            entries = sources[s_idx][0]
            j = min(i + 1, len(entries) - 1)
            if (s_idx, i) in used or (s_idx, j) in used:
                continue
            used.update({(s_idx, i), (s_idx, j)})
            chosen.append((s_idx, i))
            if len(chosen) >= top_k:
                break
        chosen.sort()
        windows = []
        for s_idx, i in chosen:
            entries = sources[s_idx][0]
            j = min(i + 1, len(entries) - 1)
            windows.append(entries[i: j + 1])
        return windows

    def retrieve_context(
        self,
        contact: dict[str, Any],
        query: str,
        *,
        exclude_tail: int = 0,
        top_k: int = RAG_TOP_K,
    ) -> list[str]:
        """Return the top past exchanges relevant to ``query`` as text snippets.

        Searches ``riwayat_[id].txt`` (minus the last ``exclude_tail`` lines,
        which are already part of the live context) and the imported chat.
        """
        query_words = set(extract_keywords(query))
        if not query_words:
            return []
        contact_id = contact["id"]
        hist_entries, hist_kw = self._load_indexed(self.history_path(contact_id))
        if exclude_tail > 0:
            hist_entries, hist_kw = hist_entries[:-exclude_tail], hist_kw[:-exclude_tail]
        sources = [self._load_indexed(self.import_path(contact_id)), (hist_entries, hist_kw)]

        name = contact.get("name", "Friend")
        snippets = []
        for window in self._rank_exchanges(sources, query_words, top_k):
            lines = [
                f"{name if e.role == 'assistant' else 'Friend'}: "
                f"{_truncate(_single_line(e.text, SNIPPET_MAX_CHARS), SNIPPET_MAX_CHARS)}"
                for e in window
            ]
            snippets.append("\n".join(lines))
        return snippets

    def build_prompt_messages(
        self,
        contact: dict[str, Any],
        user_text: str,
        *,
        history_turns: int = HISTORY_CONTEXT_TURNS,
        rag_top_k: int = RAG_TOP_K,
    ) -> list[dict[str, str]]:
        """Assemble the final chat-completion messages.

        ``system`` = base_system_prompt + retrieved memories, followed by the
        last ``history_turns`` messages and finally the new user message.
        Expects ``user_text`` to have already been appended to the history.
        """
        history = self.load_history(contact["id"])
        if history and history[-1].role == "user" and history[-1].text == user_text:
            history = history[:-1]
        recent = history[-history_turns:] if history_turns > 0 else []

        snippets = self.retrieve_context(
            contact, user_text, exclude_tail=len(recent) + 1, top_k=rag_top_k
        )
        system = contact.get("system_prompt") or default_system_prompt(contact.get("name", "Friend"))
        if snippets:
            system += (
                "\n\n## Memories from past chats (use only if relevant)\n"
                + "\n\n".join(snippets)
            )

        messages = [{"role": "system", "content": system}]
        messages += [{"role": e.role, "content": e.text} for e in recent]
        messages.append({"role": "user", "content": user_text})
        return messages

    # ------------------------------------------------------------------ model
    def find_model_path(self, configured: str = "") -> Path | None:
        """Locate the GGUF model: configured path -> default name -> any *.gguf."""
        candidates: list[Path] = []
        if configured:
            path = Path(configured)
            candidates.append(path if path.is_absolute() else self.app_dir / path)
        search_dirs = [self.models_dir, BUNDLE_DIR / "models", self.app_dir]
        candidates += [d / DEFAULT_MODEL_FILENAME for d in search_dirs]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        for directory in search_dirs[:2]:
            if directory.is_dir():
                ggufs = sorted(directory.glob("*.gguf"))
                if ggufs:
                    return ggufs[0]
        return None

