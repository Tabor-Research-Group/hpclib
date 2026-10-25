// Loaded before the stylesheets apply, so the page opens in the theme the menu's switch chose (this browser's
// choice; without one, the system's). theme() in app.js changes it.
(function () {
  try {
    var t = localStorage.getItem("hpc-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage blocked: the system's theme */ }
})();
