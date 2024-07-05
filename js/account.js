function checkLoginStatus() {
  fetch("account.php?action=check_login", {
    method: "GET",
    credentials: "include",
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.loggedIn) {
        updateLoginStatus(true, data.username);
        showForm(songSearchArea);
      } else {
        updateLoginStatus(false);
        showForm(loginForm);
      }
    })
    .catch((error) => {
      console.error("Error:", error);
    });
}

function updateLoginStatus(isLoggedIn, username = "") {
  const modalTitle = document.querySelector("#songRequestModalLabel");
  const logoutButton = document.getElementById("logoutButton");
  if (isLoggedIn) {
    modalTitle.textContent = `Request a Song (Logged in as ${username})`;
    logoutButton.style.display = "inline-block";
  } else {
    modalTitle.textContent = "Request a Song";
    logoutButton.style.display = "none";
  }
}

function logout() {
  const formData = new FormData();
  formData.append('action', 'logout');

  fetch("account.php", {
    method: "POST",
    body: formData,
    credentials: "include",
  })
    .then((response) => response.json())
    .then((data) => {
      if (data.success) {
        updateLoginStatus(false);
        showForm(loginForm);
        displayModalMessage("Logged out successfully");
      } else {
        displayModalMessage("Logout failed: " + data.message, true);
      }
    })
    .catch((error) => {
      console.error("Error:", error);
      displayModalMessage("An error occurred while logging out", true);
    });
}

function showForm(formToShow) {
  [loginForm, registerForm, resetRequestForm, songSearchArea].forEach(
    (form) => {
      form.style.display = "none";
    }
  );
  formToShow.style.display = "block";
}

function displayModalMessage(message, isError = false) {
  const messageDiv = document.createElement("div");
  messageDiv.className = `alert ${
    isError ? "alert-danger" : "alert-success"
  } mt-3`;
  messageDiv.textContent = message;

  // Remove any existing message
  const existingMessage = document.querySelector(".modal-body > .alert");
  if (existingMessage) {
    existingMessage.remove();
  }

  // Insert the new message at the top of the modal body
  const modalBody = document.querySelector(".modal-body");
  modalBody.insertBefore(messageDiv, modalBody.firstChild);

  // Automatically remove the message after 5 seconds
  setTimeout(() => {
    messageDiv.remove();
  }, 5000);
}

document.addEventListener("DOMContentLoaded", function () {
  console.log("DOM fully loaded and parsed");

  const loginForm = document.getElementById("loginForm");
  const registerForm = document.getElementById("registerForm");
  const resetRequestForm = document.getElementById("resetRequestForm");
  const songSearchArea = document.getElementById("songSearchArea");

  const showLogin = document.getElementById("showLogin");
  const showRegister = document.getElementById("showRegister");
  const showResetRequest = document.getElementById("showResetRequest");
  const showLoginFromReset = document.getElementById("showLoginFromReset");

  showLogin.addEventListener("click", (e) => {
    e.preventDefault();
    showForm(loginForm);
  });
  showRegister.addEventListener("click", (e) => {
    e.preventDefault();
    showForm(registerForm);
  });
  showResetRequest.addEventListener("click", (e) => {
    e.preventDefault();
    showForm(resetRequestForm);
  });
  showLoginFromReset.addEventListener("click", (e) => {
    e.preventDefault();
    showForm(loginForm);
  });

  // Call this when the modal is opened
  $("#songRequestModal").on("show.bs.modal", function () {
    checkLoginStatus();
  });

  // Handle form submissions
  document
    .getElementById("loginFormSubmit")
    .addEventListener("submit", function (e) {
      e.preventDefault();
      const formData = new FormData(this);
      formData.append("action", "login");

      // Add remember me value to form data
      const rememberMe = document.getElementById("rememberMe").checked;
      formData.append("remember_me", rememberMe);

      fetch("account.php", {
        method: "POST",
        body: formData,
        credentials: "include",
      })
        .then((response) => response.json())
        .then((data) => {
          if (data.success) {
            showForm(songSearchArea);
            updateLoginStatus(true, data.username);
            displayModalMessage("Login successful");
          } else {
            displayModalMessage(data.message, true);
          }
        })
        .catch((error) => {
          console.error("Error:", error);
          displayModalMessage(
            "An error occurred while logging in. Please try again.",
            true
          );
        });
    });

  document
    .getElementById("registerFormSubmit")
    .addEventListener("submit", function (e) {
      e.preventDefault();
      const formData = new FormData(this);
      formData.append("action", "register");

      fetch("account.php", {
        method: "POST",
        body: formData,
      })
        .then((response) => response.json())
        .then((data) => {
          if (data.success) {
            displayModalMessage("Account created successfully. Please log in.");
            showForm(loginForm);
          } else {
            displayModalMessage(data.message, true);
          }
        });
    });

  document
    .getElementById("resetRequestFormSubmit")
    .addEventListener("submit", function (e) {
      e.preventDefault();
      const formData = new FormData(this);
      formData.append("action", "reset_password_request");

      fetch("account.php", {
        method: "POST",
        body: formData,
      })
        .then((response) => response.json())
        .then((data) => {
          displayModalMessage(data.message, !data.success);
          if (data.success) {
            showForm(loginForm);
          }
        });
    });
});

// Call this function when the page loads
document.addEventListener("DOMContentLoaded", checkLoginStatus);
