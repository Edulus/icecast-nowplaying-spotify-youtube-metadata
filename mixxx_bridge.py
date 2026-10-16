"""
mixxx_bridge.py

Receives Mixxx mixer state from the "TelosFM Bridge" controller mapping
(mixxx-bridge/TelosFM-Bridge.js) over a loopMIDI port, so now_playing.py can
tell whether an Aux channel is actually feeding the broadcast. Mixxx has no
other external API for this.

Uses the Windows multimedia MIDI API (winmm) directly, so there is no extra
dependency. The mapping resends everything every 500 ms; if nothing arrives
for STALE_SECONDS the bridge counts as disconnected and the port is reopened.
"""

import ctypes
import os
import threading
import time
from ctypes import wintypes

PORT_NAME = os.getenv("MIXXX_BRIDGE_MIDI_PORT", "TelosFM Bridge")
STALE_SECONDS = 3.0
REOPEN_SECONDS = 5.0

# CC numbers -- keep in sync with mixxx-bridge/TelosFM-Bridge.js.
CC_AUX_MAIN_MIX = 1
CC_AUX_VOLUME = 11
CC_AUX_ORIENTATION = 21
CC_DECK_PLAY = 31
CC_DECK_VOLUME = 41
CC_DECK_ORIENTATION = 51
CC_CROSSFADER = 60
CC_HEARTBEAT = 127

ORIENTATION_LEFT, ORIENTATION_CENTER, ORIENTATION_RIGHT = 0, 1, 2

_MIM_DATA = 0x3C3
_CALLBACK_FUNCTION = 0x30000
_winmm = ctypes.WinDLL("winmm")


class _MIDIINCAPSW(ctypes.Structure):
    _fields_ = [
        ("wMid", wintypes.WORD),
        ("wPid", wintypes.WORD),
        ("vDriverVersion", wintypes.UINT),
        ("szPname", wintypes.WCHAR * 32),
        ("dwSupport", wintypes.DWORD),
    ]


_MidiInProc = ctypes.WINFUNCTYPE(
    None, wintypes.HANDLE, wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t
)

_winmm.midiInOpen.argtypes = [
    ctypes.POINTER(wintypes.HANDLE), wintypes.UINT, ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD
]
_winmm.midiInStart.argtypes = [wintypes.HANDLE]
_winmm.midiInStop.argtypes = [wintypes.HANDLE]
_winmm.midiInClose.argtypes = [wintypes.HANDLE]
_winmm.midiInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(_MIDIINCAPSW), wintypes.UINT]


def _find_input_device(name):
    caps = _MIDIINCAPSW()
    for device_id in range(_winmm.midiInGetNumDevs()):
        if _winmm.midiInGetDevCapsW(device_id, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            if name.lower() in caps.szPname.lower():
                return device_id
    return None


class MixxxBridge:
    def __init__(self, port_name=PORT_NAME):
        self.port_name = port_name
        self._values = {}
        self._last_message = 0.0
        self._handle = None
        self._lock = threading.Lock()
        # Must stay referenced for as long as the port is open.
        self._callback = _MidiInProc(self._on_midi)

    def start(self):
        threading.Thread(target=self._keep_open, daemon=True).start()

    # --- MIDI plumbing ---------------------------------------------------

    def _on_midi(self, handle, message, instance, param1, param2):
        if message != _MIM_DATA:
            return
        status = param1 & 0xFF
        if status != 0xB0:  # Control Change, channel 1
            return
        number = (param1 >> 8) & 0x7F
        value = (param1 >> 16) & 0x7F
        with self._lock:
            self._values[number] = value
            self._last_message = time.time()

    def _open(self):
        device_id = _find_input_device(self.port_name)
        if device_id is None:
            return False
        handle = wintypes.HANDLE()
        callback_ptr = ctypes.cast(self._callback, ctypes.c_void_p).value
        if _winmm.midiInOpen(ctypes.byref(handle), device_id, callback_ptr, 0, _CALLBACK_FUNCTION) != 0:
            return False
        _winmm.midiInStart(handle)
        self._handle = handle
        return True

    def _close(self):
        if self._handle:
            _winmm.midiInStop(self._handle)
            _winmm.midiInClose(self._handle)
            self._handle = None

    def _keep_open(self):
        opened_at = 0.0
        while True:
            if self._handle is None:
                if self._open():
                    opened_at = time.time()
            elif not self.connected() and time.time() - opened_at > REOPEN_SECONDS:
                # Port open but silent: loopMIDI may have been restarted
                # (stale handle) or Mixxx closed. Reopen to recover either way.
                self._close()
                continue
            time.sleep(1)

    # --- State -----------------------------------------------------------

    def connected(self):
        with self._lock:
            return time.time() - self._last_message < STALE_SECONDS

    def port_found(self):
        return _find_input_device(self.port_name) is not None

    def _get(self, number, default=0):
        with self._lock:
            return self._values.get(number, default)

    def _crossfader(self):
        """-1.0 (full left) .. 1.0 (full right)."""
        return self._get(CC_CROSSFADER, 64) / 127 * 2 - 1

    def _audible_through_crossfader(self, orientation):
        crossfader = self._crossfader()
        if orientation == ORIENTATION_LEFT:
            return crossfader < 0.98
        if orientation == ORIENTATION_RIGHT:
            return crossfader > -0.98
        return True

    def aux(self, number):
        """State of Aux 1-4, including whether it is feeding the main mix."""
        play = self._get(CC_AUX_MAIN_MIX + number - 1) >= 64
        volume = self._get(CC_AUX_VOLUME + number - 1) / 127
        orientation = self._get(CC_AUX_ORIENTATION + number - 1, ORIENTATION_CENTER)
        if not play:
            reason = "PLAY is off"
        elif volume <= 0.01:
            reason = "volume fader is down"
        elif not self._audible_through_crossfader(orientation):
            reason = "crossfader is turned away from it"
        else:
            reason = None
        return {"play": play, "volume": round(volume, 2), "live": reason is None, "reason": reason}

    def deck(self, number):
        playing = self._get(CC_DECK_PLAY + number - 1) >= 64
        volume = self._get(CC_DECK_VOLUME + number - 1) / 127
        orientation = self._get(CC_DECK_ORIENTATION + number - 1, ORIENTATION_CENTER)
        live = playing and volume > 0.01 and self._audible_through_crossfader(orientation)
        return {"playing": playing, "volume": round(volume, 2), "live": live}
