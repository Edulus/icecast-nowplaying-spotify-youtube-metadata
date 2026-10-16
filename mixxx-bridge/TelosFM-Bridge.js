// TelosFM Bridge -- Mixxx controller script.
//
// Mixxx has no external API for mixer state, so this mapping sends it out as
// MIDI Control Change messages on channel 1 of a loopMIDI port, where
// now_playing.py reads them (see mixxx_bridge.py). Everything is resent every
// SEND_INTERVAL_MS, which doubles as a heartbeat: if the messages stop, the
// Python side knows the bridge is gone.
//
// CC numbers (keep in sync with mixxx_bridge.py):
//   1-4    Aux 1-4 PLAY button (main_mix)    0 = off, 127 = on
//   11-14  Aux 1-4 volume fader               0-127
//   21-24  Aux 1-4 crossfader orientation     0 = left, 1 = center, 2 = right
//   31-34  Deck 1-4 playing                   0 / 127
//   41-44  Deck 1-4 volume fader              0-127
//   51-54  Deck 1-4 crossfader orientation    0 / 1 / 2
//   60     Crossfader                         0 = full left, 127 = full right
//   127    Heartbeat                          always 127

var TelosFMBridge = {};

TelosFMBridge.SEND_INTERVAL_MS = 500;
TelosFMBridge.CC = 0xB0; // Control Change, MIDI channel 1

TelosFMBridge.toCC = function (fraction) {
    return Math.max(0, Math.min(127, Math.round(fraction * 127)));
};

TelosFMBridge.send = function (number, value) {
    midi.sendShortMsg(TelosFMBridge.CC, number, value);
};

TelosFMBridge.sendAll = function () {
    var i;
    for (i = 1; i <= 4; i++) {
        var aux = "[Auxiliary" + i + "]";
        TelosFMBridge.send(i, engine.getValue(aux, "main_mix") ? 127 : 0);
        TelosFMBridge.send(10 + i, TelosFMBridge.toCC(engine.getValue(aux, "volume")));
        TelosFMBridge.send(20 + i, engine.getValue(aux, "orientation"));

        var deck = "[Channel" + i + "]";
        TelosFMBridge.send(30 + i, engine.getValue(deck, "play") ? 127 : 0);
        TelosFMBridge.send(40 + i, TelosFMBridge.toCC(engine.getValue(deck, "volume")));
        TelosFMBridge.send(50 + i, engine.getValue(deck, "orientation"));
    }
    // crossfader runs -1 (left) .. 1 (right).
    TelosFMBridge.send(60, TelosFMBridge.toCC((engine.getValue("[Master]", "crossfader") + 1) / 2));
    TelosFMBridge.send(127, 127);
};

// Puts the station on air once Mixxx has had time to load the library. These
// set the controls to on rather than toggling them, so running it again is
// harmless, and it needs no keyboard focus (station_watchdog.ps1 used to send
// Ctrl+L and Shift+F12, which got lost when another window kept focus).
TelosFMBridge.GO_LIVE_DELAY_MS = 30000;

TelosFMBridge.goLive = function () {
    TelosFMBridge.goLiveTimer = 0;
    engine.setValue("[Shoutcast]", "enabled", 1);
    engine.setValue("[AutoDJ]", "enabled", 1);
};

TelosFMBridge.init = function (id, debugging) {
    TelosFMBridge.sendAll();
    TelosFMBridge.timer = engine.beginTimer(TelosFMBridge.SEND_INTERVAL_MS, TelosFMBridge.sendAll);
    TelosFMBridge.goLiveTimer = engine.beginTimer(TelosFMBridge.GO_LIVE_DELAY_MS, TelosFMBridge.goLive, true);
};

TelosFMBridge.shutdown = function () {
    if (TelosFMBridge.timer) {
        engine.stopTimer(TelosFMBridge.timer);
        TelosFMBridge.timer = 0;
    }
    if (TelosFMBridge.goLiveTimer) {
        engine.stopTimer(TelosFMBridge.goLiveTimer);
        TelosFMBridge.goLiveTimer = 0;
    }
};
