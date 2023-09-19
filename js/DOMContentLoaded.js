window.addEventListener("DOMContentLoaded", () => {
  if (navigator.userAgent.toLowerCase().match(/mobile/i)) {
    console.log("Detected mobile, leaving volume bar hidden");
    audioStream.volume = 1.0;
  } else {
    console.log("Detected desktop, unhiding volume bar");
    divVolume.style.display = "block";

    let volumeStored = storageGet("volume");
    if (volumeStored) {
      inputVolume.value = volumeStored;
      volumeSet(volumeStored);
    } else {
      inputVolume.value = 50;
      volumeSet(inputVolume.value);
    }
  }
});
