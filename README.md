# icecast-nowplaying-spotify-youtube-metadata

Push live now-playing metadata from Spotify and YouTube on Windows to any
Icecast-compatible stream.

Captures what's currently playing from the Spotify desktop app and from
YouTube / YouTube Music in Chrome, and pushes a clean "Artist - Track" string
to an Icecast 2 server so it shows up on the stream's player / now-playing
display.

**Windows only.** Detection relies on Win32 window enumeration for Spotify
and a Chrome extension for YouTube.

> **Status:** this is a generalized, config-driven version of a system that
> runs live against a real Icecast stream. The detection logic, auth model
> and title-cleaning rules all come from that working setup, but this
> repository's exact files have not been re-run end to end against a live
> server. Smoke-test it against your own stream before relying on it.

## How it works

Four pieces:

1. **`now_playing.py`** -- runs continuously. Finds the current Spotify track
   via a Win32-specific trick (see Gotchas below) and also runs a small local
   HTTP server on `127.0.0.1:8765` that the Chrome extension posts YouTube
   tab titles to. If a YouTube update has arrived in the last 90 seconds, it
   wins; otherwise Spotify's title is used. The result is written to
   `now_playing.txt`.

2. **`chrome-extension/`** -- a Manifest V3 extension that finds whichever
   YouTube/YouTube Music tab is actually making sound and POSTs its title to
   the local server above.

3. **`nowplaying_poller.py`** -- watches `now_playing.txt`, strips known
   YouTube tag noise (see `STRIP_PATTERNS`), and pushes changes to your
   Icecast server's metadata endpoint.

4. **`start_pipeline.bat`** -- launches both Python scripts in their own
   console windows after checking Python is on PATH and `.env` exists.

```
Spotify (hidden window) --\
                            +--> now_playing.py --> now_playing.txt --> nowplaying_poller.py --> Icecast
Chrome extension (audible tab, HTTP POST) --/
```

## Setup

1. Install Python 3.9+ and make sure it's on your PATH.
2. `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and fill in your Icecast details (see
   below). **If creating `.env` in Notepad, use "Save as type: All Files"**
   or it will save as `.env.txt` and be silently ignored.
4. Load the extension: open `chrome://extensions`, enable Developer Mode,
   "Load unpacked", select the `chrome-extension/` folder.
5. Run `start_pipeline.bat` (or run `now_playing.py` and
   `nowplaying_poller.py` yourself in two terminals).

## Icecast metadata endpoint and auth

This uses the standard Icecast 2 metadata update mechanism:

```
GET /admin/metadata?mode=updinfo&mount=<mount>&charset=UTF-8&song=<title>
```

sent with **preemptive** HTTP Basic Auth -- the `Authorization: Basic ...`
header is set on the first request rather than waiting for a `401` challenge.
This matters: Python's `urllib.request.HTTPBasicAuthHandler` is *reactive* and
only sends credentials after the server issues a `WWW-Authenticate` challenge.
Not every Icecast host issues one reliably, and when it doesn't, the
credentials are never sent and the failure looks exactly like a wrong
username/password.

Both the username and the password/token are
separate config values in `.env` (`ICECAST_USERNAME`, `ICECAST_TOKEN`) so
this works regardless of which auth model your host uses:

- **Vanilla Icecast**: typically the `admin` or `source` account and its
  password.
- **Hosted/managed Icecast providers**: some use a username plus an
  API token in place of a password. **Many shared hosting plans disable the
  `/admin/metadata` endpoint entirely** -- confirm with your host that
  external `updinfo` calls are allowed before assuming this is broken.

If you get "authentication required" / 401, it almost always means the
username or secret doesn't match what your specific host expects -- it's a
credentials problem, not a bug in this script.

## Configuration (`.env`)

| Variable          | Meaning                                                        |
|--------------------|-----------------------------------------------------------------|
| `ICECAST_SCHEME`   | `http` or `https` (default `http`)                              |
| `ICECAST_HOST`     | Server hostname                                                 |
| `ICECAST_PORT`     | Server port                                                     |
| `ICECAST_MOUNT`    | Exact mount string, including leading `/`                       |
| `ICECAST_USERNAME` | Username for `/admin/metadata`                                  |
| `ICECAST_TOKEN`    | Password or API token for `/admin/metadata`                     |
| `NOWPLAYING_FILE`  | Local file used to pass titles between the two scripts          |
| `POLL_INTERVAL`    | Seconds between checks                                          |

Nothing is hardcoded -- all of the above come from `.env` via
`python-dotenv`. `.env` is gitignored; only `.env.example` (blank
placeholders) is committed.

## Hard-won notes / gotchas

- **Auth varies by host.** "authentication required" almost always means the
  wrong username or secret for your specific host's auth model -- see above.
- **Basic Auth must be preemptive.** Sending the `Authorization` header only
  after a `401` challenge (which is what urllib's `HTTPBasicAuthHandler` does)
  fails against hosts that don't issue a challenge -- and it fails *looking
  like bad credentials*, which sends you hunting in the wrong place. Set the
  header on the first request. This was the single highest-value fix here.
- **Chrome tab titles carry more than the track.** A YouTube tab title is
  `"Artist - Track (Official Video) - YouTube"`, and Chrome prepends an unread
  count like `"(2) "` when notifications are pending. Both the trailing
  ` - YouTube` / ` - YouTube Music` (hyphen **or** en/em dash) and the leading
  `(N)` must be stripped, or every track goes on air with ` - YouTube` glued
  to the end. The suffix strip is anchored to end-of-string so a title like
  `"Youtube Boys - My YouTube Song"` survives intact.
- **Mount case-sensitivity + font ambiguity.** If you get "mount not found"
  or "Source does not exist" and you're *sure* the mount is right, it
  probably isn't: lowercase `l`, capital `I`, and `i` look identical in many
  dashboard fonts. Verify the exact mount string from your server's own
  `/admin/publicstats.json` (not the dashboard UI) before assuming anything
  else is wrong.
- **Spotify's now-playing window is not "visible."** The window whose title
  is "Artist - Track" does not show up under `EnumWindows` +
  `IsWindowVisible()` -- that returns nothing. The only reliable approach is
  to enumerate *every* top-level window regardless of visibility, resolve
  each one's owning process via `GetWindowThreadProcessId`, and keep the ones
  owned by `spotify.exe`.
- **MV3 service worker lifecycle.** Chrome suspends MV3 background service
  workers after ~30 seconds of inactivity, which kills any `setInterval` /
  `setTimeout` state. The extension uses `chrome.alarms` (1-minute minimum
  period) as a heartbeat, plus `chrome.tabs.onUpdated` /
  `chrome.tabs.onActivated` listeners for near-immediate responsiveness in
  between heartbeats.
- **CORS preflight.** The extension POSTs JSON to `localhost`, which Chrome
  treats as cross-origin and precedes with an `OPTIONS` preflight request.
  `now_playing.py`'s HTTP server explicitly handles `OPTIONS` (in addition to
  `POST`) and returns `Access-Control-Allow-*` headers, or the browser
  silently blocks the real POST and no YouTube data ever arrives.
- **Audible tab, not focused tab.** The extension uses
  `chrome.tabs.query({ audible: true })` filtered to YouTube URLs -- not the
  active/focused tab. These are frequently different tabs (e.g. music
  playing in a background tab while you work elsewhere); using the focused
  tab is a tempting but wrong approach that silently reports nothing or the
  wrong track.
- **Tag-stripping philosophy.** `clean_title()` only strips explicit
  bracketed/parenthesized forms like `(Official Audio)`, `[Official Video]`,
  `(Edit)`, `(Visualizer)` -- listed in the editable `STRIP_PATTERNS` list.
  It deliberately does **not** do bare-word matching, which would mangle real
  titles like "Video Games" or "Radio Edit". `(Remix)` and `(feat. ...)` are
  intentionally preserved since they're meaningful title content. Note the
  list carries **both** `(Visualiser)` (British) and `(Visualizer)`
  (American) -- the British spelling does appear in real release titles.
- **WASAPI/SMTC were tried and abandoned.** Windows' audio session APIs and
  System Media Transport Controls were tried first for both Spotify and
  Chrome and returned empty results for Chrome/YouTube. Window/tab titles
  turned out to be the only reliable source across both apps.
- **Known limitations:**
  - **Spotify's window title persists while paused** -- it keeps showing
    "Artist - Track" rather than reverting to "Spotify". There is no pause
    signal in the window title, so a paused Spotify can hold a stale track on
    the stream until something else starts playing.
  - Spotify's title during **ads** varies by client version. The bare app
    name is filtered out, and `SPOTIFY_IGNORE_TITLES` in `now_playing.py`
    carries a small best-effort ignore list, but this was **not** verified
    against live ad playback.
  - Up to ~90 seconds of staleness is possible if a YouTube pause event is
    missed and the next heartbeat hasn't fired yet.
  - If Spotify and YouTube are both playing at once, YouTube wins
    (intentional; change `YOUTUBE_FRESH_SECONDS` / the priority logic in
    `now_playing.py` if you'd rather have Spotify win).
  - Windows only -- the Spotify detection is Win32-specific.

## Files

- `now_playing.py` -- track detection + local HTTP server
- `nowplaying_poller.py` -- pushes to Icecast, cleans titles, logs
- `chrome-extension/manifest.json`, `chrome-extension/background.js` --
  YouTube audible-tab reporter
- `start_pipeline.bat` -- Windows launcher
- `.env.example` -- config template (no real values)
- `requirements.txt` -- pywin32, psutil, python-dotenv

## License

MIT -- see `LICENSE`.
