// Reports the currently audible YouTube / YouTube Music tab's title to the
// local now_playing.py HTTP server, so it can be merged with Spotify
// detection and pushed to Icecast.
//
// Why "audible" and not "active"/"focused": the focused tab and the tab
// that's actually making sound are frequently different (background music
// tab while you work in another tab). chrome.tabs.query({ audible: true })
// is the correct signal here -- using the focused tab is a known-wrong
// approach that reports silence or the wrong track.
//
// Why alarms instead of setInterval: MV3 service workers are suspended after
// ~30s of inactivity, which kills any setInterval/setTimeout state.Chrome's
// alarms API survives worker suspension (minimum period is 1 minute), so it
// is used as a heartbeat/safety net. The onUpdated/onActivated listeners
// give near-immediate responsiveness on top of that heartbeat.

const SERVER_URL = "http://127.0.0.1:8765/";
const YOUTUBE_URL_PATTERN = /^https:\/\/(www\.)?(music\.)?youtube\.com\//;
const HEARTBEAT_ALARM = "now-playing-heartbeat";

async function reportNowPlaying() {
  let tabs;
  try {
    tabs = await chrome.tabs.query({ audible: true });
  } catch (err) {
    return;
  }

  const ytTab = tabs.find((t) => t.url && YOUTUBE_URL_PATTERN.test(t.url));

  let payload = { idle: true };
  if (ytTab && ytTab.title) {
    payload = { title: ytTab.title };
    try {
      // Ask the content script in that tab for the channel name so we can
      // report "Channel - Title" instead of just the (often artist-less)
      // video title. This fails harmlessly if the content script hasn't
      // loaded yet (e.g. tab just opened) -- we fall back to the bare title.
      const response = await chrome.tabs.sendMessage(ytTab.id, { type: "GET_CHANNEL_NAME" });
      if (response && response.channelName) {
        payload.channel = response.channelName;
      } else {
        console.debug("Now Playing Reporter: content script responded but found no channel name", response);
      }
    } catch (err) {
      // No content script listening in that tab -- fine, use bare title.
      console.debug("Now Playing Reporter: could not reach content script", err.message);
    }
  }

  try {
    await fetch(SERVER_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    // now_playing.py isn't running -- nothing to do, just try again later.
  }
}

chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
  // "audible" catches play/pause; "title" catches same-tab track changes
  // (e.g. YouTube autoplaying the next video) where audible never toggles.
  if (
    Object.prototype.hasOwnProperty.call(changeInfo, "audible") ||
    Object.prototype.hasOwnProperty.call(changeInfo, "title")
  ) {
    reportNowPlaying();
  }
});

chrome.tabs.onActivated.addListener(() => {
  reportNowPlaying();
});

chrome.alarms.create(HEARTBEAT_ALARM, { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === HEARTBEAT_ALARM) {
    reportNowPlaying();
  }
});

// Fire once on install/browser start so state isn't stale until the next
// event or heartbeat tick.
reportNowPlaying();
