"""
track_notes.py

Short "About this track" notes for the Telos FM website, researched with
Gemini + live Google Search (the same approach as Metadata Doctor's web
search) and kept forever in track_notes.json, so each song costs one lookup
the first time it airs and nothing after that.

Notes are public, so this is deliberately strict:

- Only a grounded answer counts: Gemini must actually have searched (the
  response carries web sources) AND name the same artist and track. When the
  live-search quota runs out there is NO fallback to the model's own memory;
  the track simply has no note until a later play.
- A daily cap (TRACK_NOTES_DAILY_LIMIT, default 90) stops lookups before the
  Gemini quota does.
- Text is cleaned to the station's voice: no em dashes, no markdown or
  citation markers, two or three sentences.

Command line, for reviewing what the site shows:

    python track_notes.py list               every stored note
    python track_notes.py lookup "Artist - Title"   research one now
    python track_notes.py hide   "Artist - Title"   never show this note
    python track_notes.py unhide "Artist - Title"
    python track_notes.py set    "Artist - Title" "Your own text"

API key: GEMINI_API_KEY in .env, or else read from Metadata Doctor's
data/search_config.json (path in METADATA_DOCTOR_CONFIG), so the key does not
have to be copied into a second file.
"""

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_FILE = os.getenv("TRACK_NOTES_FILE", os.path.join(HERE, "track_notes.json"))
DAILY_LIMIT = int(os.getenv("TRACK_NOTES_DAILY_LIMIT", "90"))
GEMINI_MODEL = os.getenv("TRACK_NOTES_MODEL", "gemini-flash-latest")
METADATA_DOCTOR_CONFIG = os.getenv(
    "METADATA_DOCTOR_CONFIG",
    r"W:\Coding Projects\MetaData Doctor\data\search_config.json")
# A track Gemini could not confirm is asked again after this long, in case
# the web has caught up with an obscure release.
RETRY_UNCONFIRMED_DAYS = 30
MAX_NOTE_CHARS = 420

PROMPT = """Using Google Search, find this music track: {title} by {artist}.

Then write 2 or 3 sentences for a radio station's website, telling a listener
what they are hearing: who made it, when and on what release it came out, and
its sound or mood. Warm, factual and specific. Describe what the music is.
Do not use em dashes, markdown, hype words, or phrases like "not just".

After the sentences, output a JSON object in a ```json fenced block with keys
"artist", "title", "confirmed". "artist" and "title" are the performing artist
and the song name as your sources give them. "confirmed" is true only if your
search results clearly describe this exact track by this artist."""


# ------------------------------------------------------------------ titles

# Version and reissue noise that confuses a search and looks untidy on the
# site: " - 2005 Remaster", "(Remastered 2011)", " - Radio Edit".
_SUFFIX_RE = re.compile(
    r"\s*(?:-\s*|[(\[])\s*(?:(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?|"
    r"radio edit|single version|album version|mono|stereo)\s*[)\]]?\s*$",
    re.IGNORECASE)


def clean_title(title):
    """'Sax and Violins - 2005 Remaster' -> 'Sax and Violins'."""
    previous = None
    while previous != title:
        previous, title = title, _SUFFIX_RE.sub("", title).strip()
    return title


def parse_on_air(text):
    """Split the stream's title into (artist, title).

    The metadata poller sends '"Track" by Artist'; Mixxx's own broadcast
    metadata sends 'Artist - Track'. Anything else returns (None, None)."""
    text = (text or "").strip()
    m = re.match(r'^"(.+)" by (.+)$', text)
    if m:
        return m.group(2).strip(), clean_title(m.group(1).strip())
    if " - " in text:
        artist, _, title = text.partition(" - ")
        if artist.strip() and title.strip():
            return artist.strip(), clean_title(title.strip())
    return None, None


def _words(s):
    s = (s or "").lower().replace("&", " and ")
    return {w for w in re.findall(r"[a-z0-9]+", s) if w not in ("and", "the", "feat", "ft")}


def _same(a, b):
    wa, wb = _words(a), _words(b)
    return bool(wa and wb) and (wa <= wb or wb <= wa)


def track_key(artist, title):
    return " ".join(sorted(_words(artist))) + " | " + " ".join(sorted(_words(clean_title(title))))


# ------------------------------------------------------------------ text

def tidy_note(text):
    """Station voice: no em/en dashes, markdown, or citation markers."""
    text = re.sub(r"\[\d+(?:,\s*\d+)*\]", "", text)          # [1], [2, 3]
    text = re.sub(r"[*_#`]+", "", text)                        # markdown
    text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", text)         # em / en dash
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r",\s*([.,;:])", r"\1", text)
    text = re.sub(r"\s+([.,;:!?])", r"\1", text)
    if len(text) > MAX_NOTE_CHARS:   # cut at the last full sentence that fits
        cut = text[:MAX_NOTE_CHARS]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = cut[:end + 1] if end > 80 else cut.rsplit(" ", 1)[0] + "."
    return text


def _split_answer(answer):
    """(prose, facts dict or None) from Gemini's reply."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.S)
    if not m:
        return answer.strip(), None
    try:
        facts = json.loads(m.group(1))
    except ValueError:
        return answer.strip(), None
    prose = (answer[:m.start()] + answer[m.end():]).strip()
    return prose, facts if isinstance(facts, dict) else None


# ------------------------------------------------------------------ Gemini

class QuotaExhausted(Exception):
    pass


def _api_key():
    key = os.getenv("GEMINI_API_KEY")
    if key:
        return key
    try:
        with open(METADATA_DOCTOR_CONFIG, encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key") or None
    except (OSError, ValueError):
        return None


def research(artist, title, timeout=60):
    """Ask Gemini with live search. Returns (status, note, sources):
    status "ok" or "unconfirmed". Raises QuotaExhausted on 429, and other
    errors (network, 5xx) propagate so the caller can retry on a later play."""
    key = _api_key()
    if not key:
        raise RuntimeError("no Gemini API key (set GEMINI_API_KEY in .env)")
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_MODEL}:generateContent")
    body = json.dumps({
        "contents": [{"parts": [{"text": PROMPT.format(artist=artist, title=title)}]}],
        "tools": [{"google_search": {}}],
    }).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "X-goog-api-key": key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise QuotaExhausted("Gemini quota exhausted") from e
        raise

    answer, sources = "", []
    for cand in data.get("candidates", []):
        for chunk in (cand.get("groundingMetadata", {}).get("groundingChunks") or []):
            uri = (chunk.get("web") or {}).get("uri")
            if uri and uri not in sources:
                sources.append(uri)
        answer += "".join(p.get("text", "") for p in (cand.get("content", {}).get("parts") or []))

    prose, facts = _split_answer(answer)
    note = tidy_note(prose)
    confirmed = (
        bool(sources)                                   # it really searched
        and bool(facts) and facts.get("confirmed") is True
        and _same(facts.get("artist"), artist)
        and _same(clean_title(facts.get("title") or ""), title)
        and len(note) >= 40
    )
    return ("ok" if confirmed else "unconfirmed"), note, sources[:5]


# ------------------------------------------------------------------ store

class NoteStore:
    """track_notes.json: {"notes": {key: entry}, "usage": {"date": n}}.

    An entry is {artist, title, status, note, sources, checked_at, hidden,
    custom}. 'custom' (set by hand) always wins over 'note'."""

    def __init__(self, path=STORE_FILE, daily_limit=DAILY_LIMIT, researcher=research):
        self.path = path
        self.daily_limit = daily_limit
        self.researcher = researcher
        self.lock = threading.Lock()
        self.quota_day = None   # set when Gemini answers 429; no more calls that day
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data.setdefault("notes", {})
        data.setdefault("usage", {})
        return data

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, self.path)

    @staticmethod
    def _today():
        return time.strftime("%Y-%m-%d")

    def public_note(self, artist, title):
        """The note to show for this track, or "" — cached data only, no lookup."""
        entry = self.data["notes"].get(track_key(artist, title))
        if not entry or entry.get("hidden"):
            return ""
        if entry.get("custom"):
            return entry["custom"]
        return entry.get("note", "") if entry.get("status") == "ok" else ""

    def needs_research(self, artist, title):
        entry = self.data["notes"].get(track_key(artist, title))
        if entry is None:
            return True
        if entry.get("status") == "unconfirmed" and not entry.get("custom"):
            return time.time() - entry.get("checked_at", 0) > RETRY_UNCONFIRMED_DAYS * 86400
        return False

    def get_note(self, artist, title, log=print):
        """Cached note, researching the track first if it is new. Never
        raises: failures are logged and the track just has no note yet."""
        with self.lock:
            if not self.needs_research(artist, title):
                return self.public_note(artist, title)
            today = self._today()
            used = self.data["usage"].get(today, 0)
            if self.quota_day == today or used >= self.daily_limit:
                log(f"track notes: daily limit reached, skipping {artist} - {title}")
                return ""
            self.data["usage"] = {today: used + 1}   # keep only today's count
            self._save()
        try:
            status, note, sources = self.researcher(artist, title)
        except QuotaExhausted:
            self.quota_day = self._today()
            log("track notes: Gemini quota exhausted, no more lookups today")
            return ""
        except Exception as e:   # network, 5xx: leave uncached, retry next play
            log(f"track notes: lookup failed for {artist} - {title}: {e}")
            return ""
        with self.lock:
            self.data["notes"][track_key(artist, title)] = {
                "artist": artist, "title": title, "status": status, "note": note,
                "sources": sources, "checked_at": time.time(), "hidden": False}
            self._save()
            log(f"track notes: {status} for {artist} - {title}")
            return self.public_note(artist, title)

    # ---- hand edits (CLI)
    def _entry(self, artist, title):
        return self.data["notes"].setdefault(track_key(artist, title), {
            "artist": artist, "title": title, "status": "unconfirmed", "note": "",
            "sources": [], "checked_at": time.time(), "hidden": False})

    def set_hidden(self, artist, title, hidden):
        with self.lock:
            self._entry(artist, title)["hidden"] = hidden
            self._save()

    def set_custom(self, artist, title, text):
        with self.lock:
            self._entry(artist, title)["custom"] = tidy_note(text)
            self._save()


# ------------------------------------------------------------------ CLI

def _parse_arg(text):
    artist, title = parse_on_air(text)
    if not artist:
        sys.exit('Give the track as "Artist - Title".')
    return artist, title


def main(argv):
    store = NoteStore()
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        for entry in sorted(store.data["notes"].values(), key=lambda e: e.get("checked_at", 0)):
            flag = "HIDDEN" if entry.get("hidden") else ("custom" if entry.get("custom") else entry["status"])
            print(f"[{flag}] {entry['artist']} - {entry['title']}")
            text = entry.get("custom") or entry.get("note")
            if text:
                print(f"    {text}")
        print(f"\nlookups today: {store.data['usage'].get(store._today(), 0)}/{store.daily_limit}")
    elif cmd == "lookup" and len(argv) > 2:
        artist, title = _parse_arg(argv[2])
        print(store.get_note(artist, title) or "(no confirmed note)")
    elif cmd in ("hide", "unhide") and len(argv) > 2:
        store.set_hidden(*_parse_arg(argv[2]), cmd == "hide")
        print("ok")
    elif cmd == "set" and len(argv) > 3:
        store.set_custom(*_parse_arg(argv[2]), argv[3])
        print("ok")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
