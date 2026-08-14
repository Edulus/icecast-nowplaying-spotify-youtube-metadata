// Runs on youtube.com / music.youtube.com pages. The tab title alone
// (read by background.js via chrome.tabs.query) is often just the video
// title -- e.g. "Die Young" -- with the artist/uploader only present as the
// channel name elsewhere on the page. This reads that channel name from the
// DOM so background.js can report "Channel - Title" instead.
//
// Selectors are duplicated across a few known layouts (regular watch page,
// YouTube Music player bar) since YouTube's DOM structure isn't stable
// across surfaces or redesigns -- first match wins, and a miss just falls
// back to the bare title upstream.
function getChannelName() {
  const selectors = [
    "ytd-video-owner-renderer ytd-channel-name a",
    "ytd-video-owner-renderer ytd-channel-name yt-formatted-string",
    "#owner ytd-channel-name a",
    ".ytmusic-player-bar .byline a",
    ".ytmusic-player-bar yt-formatted-string.byline",
  ];

  for (const selector of selectors) {
    const el = document.querySelector(selector);
    const text = el && el.textContent && el.textContent.trim();
    if (text) return text;
  }
  return null;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "GET_CHANNEL_NAME") {
    sendResponse({ channelName: getChannelName() });
  }
});
