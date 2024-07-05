document.addEventListener("DOMContentLoaded", function () {
  const songSearchInput = document.getElementById("songSearchInput");
  const songSearchButton = document.getElementById("songSearchButton");
  const searchResults = document.getElementById("searchResults");

  if (songSearchButton) {
    songSearchButton.addEventListener("click", function () {
      console.log("Search button clicked");
      searchSongs();
    });
  } else {
    console.error("songSearchButton not found");
  }

  function searchSongs() {
    const query = songSearchInput.value.trim();
    if (query.length < 2) {
      searchResults.innerHTML = "";
      return;
    }

    const formData = new FormData();
    formData.append("action", "search");
    formData.append("query", query);

    fetch("requests.php", {
      method: "POST",
      body: formData,
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.error) {
          displayMessage(data.error, true);
        } else {
          displaySearchResults(data);
        }
      })
      .catch((error) => {
        console.error("Error:", error);
        displayMessage("An error occurred while searching.", true);
      });
  }

  function displaySearchResults(data) {
    searchResults.innerHTML = "";
    if (data.error) {
      displayMessage(data.error, true);
      return;
    }

    const tracks = data.results.tracks.hits;

    if (tracks.length === 0) {
      displayMessage("No songs found.");
      return;
    }

    tracks.forEach((track) => {
      const resultItem = document.createElement("a");
      resultItem.href = "#";
      resultItem.className = "song-result-item";
      resultItem.innerHTML = `
          <img src="${track.image}" alt="${
        track.name
      }" class="song-result-image">
          <div class="song-result-info">
            <div class="song-result-title">${track.name}</div>
            <div class="song-result-artist">${track.artists
              .map((artist) => artist.name)
              .join(", ")}</div>
          </div>
        `;
      resultItem.addEventListener("click", (e) => {
        e.preventDefault();
        requestSong(track.uri);
      });
      searchResults.appendChild(resultItem);
    });
  }

  function requestSong(uri) {
    const formData = new FormData();
    formData.append("action", "addToQueue");
    formData.append("uri", uri);

    fetch("requests.php", {
      method: "POST",
      body: formData,
    })
      .then((response) => response.json())
      .then((data) => {
        if (data.success) {
          displayMessage(data.message);
          setTimeout(() => $("#songRequestModal").modal("hide"), 2000);
        } else if (data.error) {
          displayMessage(data.error, true);
        } else {
          displayMessage("Failed to send song request.", true);
        }
      })
      .catch((error) => {
        console.error("Error:", error);
        displayMessage(
          "An error occurred while sending the song request.",
          true
        );
      });
  }

  function displayMessage(message, isError = false) {
    const messageDiv = document.createElement("div");
    messageDiv.className = `alert ${
      isError ? "alert-danger" : "alert-success"
    } mt-3`;
    messageDiv.textContent = message;
    searchResults.innerHTML = "";
    searchResults.appendChild(messageDiv);
  }
});
