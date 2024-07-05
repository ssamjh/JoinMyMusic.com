const audioStream = document.getElementById("audio-stream");
const buttonToggle = document.getElementById("button-toggle");
const playingImage = document.getElementById("playing-image");
const playingLink = document.getElementById("playing-link");
const divVolume = document.getElementById("div-volume");
const inputVolume = document.getElementById("input-volume");

function storageSet(key, value) {
  localStorage.setItem(key, value);
}

function storageGet(key) {
  return localStorage.getItem(key);
}

function initHls(source) {
  if (Hls.isSupported()) {
    let hls = new Hls();
    hls.loadSource(source);
    hls.attachMedia(audioStream);
  } else if (audioStream.canPlayType("application/vnd.apple.mpegurl")) {
    audioStream.src = source;
  }
}

function scaleVolume(inputValue) {
  return inputValue <= 50 ? inputValue * 0.2 : 10 + (inputValue - 50) * 0.4;
}

function qualityUpdate() {
  let source;
  let wasPlaying = !audioStream.paused;

  if (Hls.isSupported()) {
    source = "https://hls.sjh.at/spotifynet/stream.m3u8";
    initHls(source);
  } else {
    source = "https://icecast.sjh.at/spotifynet";
    audioStream.setAttribute("src", source);
    console.log("Set source to: " + source);
  }

  if (wasPlaying) {
    audioStream.play();
  }
}

function togglePlay() {
  qualityUpdate();
  audioStream.play();
  buttonToggle.textContent = "Stop";
  buttonToggle.setAttribute("onClick", "javascript: buttonStop();");
}

function buttonStop() {
  audioStream.pause();
  audioStream.currentTime = 0;
  buttonToggle.textContent = "Play";
  buttonToggle.setAttribute("onClick", "javascript: togglePlay();");
}

function volumeSet(val) {
  const scaledVolume = scaleVolume(val);
  audioStream.volume = scaledVolume / 100;
  storageSet("volume", val);
}

function preloadImage(url, callback) {
  let img = new Image();
  img.src = url;
  img.onload = callback;
}

function trackMeta() {
  $.getJSON("./metadata.php?rand=" + Math.random(), function (data) {
    const currentData = data.current;

    if (!currentData || !currentData.song) {
      $(".player-playing-title, .player-playing-artist").text("");
      if (
        playingImage.src !==
        "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="
      ) {
        playingImage.src =
          "data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==";
      }
      playingLink.setAttribute("href", "javascript:void(0)");
      return;
    }

    const createLink = (url, id, name) =>
      `<a href="${url}${id}" target="_blank" style="color: inherit; text-decoration: none;">${name}</a>`;

    // Preload current playing image and then update
    if (playingImage.src !== currentData.cover) {
      preloadImage(currentData.cover, function () {
        playingImage.src = currentData.cover;

        // Update text after image has loaded
        $(".player-playing-title").html(
          createLink(
            "https://open.spotify.com/track/",
            currentData.songid,
            currentData.song
          )
        );
        $(".player-playing-artist").html(
          currentData.artist
            .map((artist) =>
              createLink(
                "https://open.spotify.com/artist/",
                artist.id,
                artist.name
              )
            )
            .join(", ")
        );
        playingLink.setAttribute(
          "href",
          `https://open.spotify.com/album/${currentData.albumid}`
        );
      });
    } else {
      // If image doesn't need updating, still update text
      $(".player-playing-title").html(
        createLink(
          "https://open.spotify.com/track/",
          currentData.songid,
          currentData.song
        )
      );
      $(".player-playing-artist").html(
        currentData.artist
          .map((artist) =>
            createLink(
              "https://open.spotify.com/artist/",
              artist.id,
              artist.name
            )
          )
          .join(", ")
      );
    }
  });
}

// Initialize button state
if (!audioStream.paused) {
  buttonToggle.textContent = "Stop";
  buttonToggle.setAttribute("onClick", "javascript: buttonStop();");
} else {
  buttonToggle.textContent = "Play";
  buttonToggle.setAttribute("onClick", "javascript: togglePlay();");
}

// Start track metadata updates
trackMeta();
setInterval(trackMeta, 5000);
