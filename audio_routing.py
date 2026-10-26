"""
audio_routing.py

Works out which Mixxx Aux inputs an app's audio reaches, so now_playing.py
can tell whether Spotify or YouTube is on air without a fixed Aux setting.
Both stages of the route are read live:

  1. Windows: the playback device(s) the app is playing into right now, from
     its active audio sessions. A per-app device picked in EarTrumpet (or
     Windows' own App volume settings) shows up here.
  2. Voicemeeter: the strip behind that device and the B buses it sends to
     (its lit B1/B2/B3 buttons, ignoring a muted strip or bus).

VOICEMEETER_BUS_AUX then maps each B bus to the Mixxx Aux input it feeds.
"""

import ctypes
import os

import psutil
from comtypes import CLSCTX_ALL, COMError
from pycaw.pycaw import AudioUtilities, IAudioSessionControl2, IAudioSessionManager2

from voicemeeter import A_BUS_COUNT, VOICEMEETER_LAYOUT, find_voicemeeter_dll

_E_RENDER = 0
_DEVICE_STATE_ACTIVE = 1
_SESSION_STATE_ACTIVE = 1

# Windows device names of Voicemeeter's virtual inputs, in virtual-strip
# order. "Voicemeeter In N" feeds hardware strip N.
_VIRTUAL_INPUT_NAMES = ["voicemeeter input", "voicemeeter aux input", "voicemeeter vaio3 input"]
_HARDWARE_INPUT_PREFIX = "voicemeeter in "


def _bus_aux_map():
    """{B bus number: Mixxx Aux number} from VOICEMEETER_BUS_AUX ("1,2,3"
    means B1 -> Aux 1, B2 -> Aux 2, B3 -> Aux 3; 0 = that bus isn't a Mixxx
    input)."""
    mapping = {}
    for bus, aux in enumerate(os.getenv("VOICEMEETER_BUS_AUX", "1,2,3").split(","), start=1):
        aux = aux.strip()
        if aux.isdigit() and 1 <= int(aux) <= 4:  # Mixxx has Aux 1-4
            mapping[bus] = int(aux)
    return mapping


def playback_devices_by_process():
    """{process name (lower case): set of device names it's playing into},
    or None if Windows' audio sessions can't be read."""
    found = {}
    try:
        devices = AudioUtilities.GetDeviceEnumerator().EnumAudioEndpoints(_E_RENDER, _DEVICE_STATE_ACTIVE)
        for i in range(devices.GetCount()):
            device = devices.Item(i)
            # QueryInterface, not ctypes.cast: cast makes a second owner of the
            # same COM reference, and both Release it when garbage collected
            # (an access violation).
            manager = device.Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None).QueryInterface(
                IAudioSessionManager2
            )
            sessions = manager.GetSessionEnumerator()
            device_name = None
            for j in range(sessions.GetCount()):
                session = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
                if session.GetState() != _SESSION_STATE_ACTIVE or not session.GetProcessId():
                    continue
                try:
                    process_name = psutil.Process(session.GetProcessId()).name().lower()
                except psutil.Error:
                    continue
                if device_name is None:
                    device_name = AudioUtilities.CreateDevice(device).FriendlyName
                found.setdefault(process_name, set()).add(device_name)
    except (COMError, OSError):
        return None
    return found


def _short_name(device_name):
    # "Voicemeeter Input (VB-Audio Voicemeeter VAIO)" -> "Voicemeeter Input"
    return device_name.split(" (", 1)[0].strip()


class VoicemeeterRouting:
    """Reads which B buses each Voicemeeter strip sends to."""

    def __init__(self):
        self.dll = None
        self.logged_in = False

    def _connect(self):
        """Voicemeeter type (1/2/3), or None if it can't be reached."""
        if self.dll is None:
            path = find_voicemeeter_dll()
            if not path or not path.is_file():
                return None
            self.dll = ctypes.WinDLL(str(path))
        if not self.logged_in:
            if self.dll.VBVMR_Login() < 0:
                return None
            self.logged_in = True
        vm_type = ctypes.c_long()
        if self.dll.VBVMR_GetVoicemeeterType(ctypes.byref(vm_type)) != 0:
            return None
        return vm_type.value if vm_type.value in VOICEMEETER_LAYOUT else None

    def _get(self, name):
        value = ctypes.c_float()
        if self.dll.VBVMR_GetParameterFloat(name.encode(), ctypes.byref(value)) != 0:
            raise OSError(f"Voicemeeter parameter {name} unreadable")
        return value.value

    def strip_for_device(self, device_name, vm_type):
        hardware, virtual = VOICEMEETER_LAYOUT[vm_type]
        name = _short_name(device_name).lower()
        if name in _VIRTUAL_INPUT_NAMES[:virtual]:
            return hardware + _VIRTUAL_INPUT_NAMES.index(name)
        if name.startswith(_HARDWARE_INPUT_PREFIX):
            number = name[len(_HARDWARE_INPUT_PREFIX):]
            if number.isdigit() and 1 <= int(number) <= hardware:
                return int(number) - 1
        return None

    def route(self, device_names):
        """Route for audio playing into `device_names`:
        {"devices", "buses", "auxes", "note"}, or None if Voicemeeter can't
        be read (the caller then falls back to a fixed Aux)."""
        vm_type = self._connect()
        if vm_type is None:
            return None
        _, virtual = VOICEMEETER_LAYOUT[vm_type]
        bus_aux = _bus_aux_map()
        buses, notes = set(), []
        try:
            self.dll.VBVMR_IsParametersDirty()  # refreshes the values read below
            for device_name in sorted(device_names):
                strip = self.strip_for_device(device_name, vm_type)
                if strip is None:
                    notes.append(f"{_short_name(device_name)} isn't a Voicemeeter input")
                    continue
                if self._get(f"Strip[{strip}].Mute"):
                    notes.append(f"{_short_name(device_name)} is muted in Voicemeeter")
                    continue
                lit = [
                    b for b in range(1, virtual + 1)
                    if self._get(f"Strip[{strip}].B{b}")
                    and not self._get(f"Bus[{A_BUS_COUNT[vm_type] + b - 1}].Mute")
                ]
                if not lit:
                    notes.append(f"{_short_name(device_name)} has no B bus lit in Voicemeeter")
                buses.update(lit)
        except OSError:
            self.close()  # Voicemeeter restarted or closed; log in again next time
            return None
        return {
            "devices": sorted(_short_name(d) for d in device_names),
            "buses": [f"B{b}" for b in sorted(buses)],
            "auxes": sorted({bus_aux[b] for b in buses if b in bus_aux}),
            "note": "; ".join(notes) or None,
        }

    def close(self):
        if self.logged_in:
            self.dll.VBVMR_Logout()
            self.logged_in = False
