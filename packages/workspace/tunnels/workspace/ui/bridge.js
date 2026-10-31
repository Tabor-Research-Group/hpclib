// Runs before the page's styles (a classic script, so it blocks): takes the console's theme from the address and
// puts the console's own stylesheet under the page's.
//
// The console's Open link ends in #theme=dark|light|system&console=http://127.0.0.1:PORT. The console's dark switch
// lives in the console's origin, which this page can't read, so the link carries it. This page keeps both for the
// next visit (its address loses them, so a copied link doesn't carry another person's console address), sets
// data-theme the way the console's theme.js does, and loads PORT's /user.css (Interface Settings' colours), which a
// stylesheet may do across origins without CORS. Only a loopback address is accepted: a link somebody else made
// must not choose which stylesheet this page loads. A stylesheet that doesn't load changes nothing.
(function () {
  var LOOPBACK = /^http:\/\/(127\.0\.0\.1|localhost|\[::1\]):[0-9]{2,5}$/;
  var params = new URLSearchParams(location.hash.slice(1));
  var theme = params.get("theme"), consoleOrigin = params.get("console");
  try {
    if (theme === "light" || theme === "dark") localStorage.setItem("hpc-theme", theme);
    else if (theme === "system") localStorage.removeItem("hpc-theme");
    if (consoleOrigin && LOOPBACK.test(consoleOrigin)) localStorage.setItem("hpc-console", consoleOrigin);
    else consoleOrigin = localStorage.getItem("hpc-console");
  } catch (e) { /* storage blocked: the system's theme, no console stylesheet */ }
  if (theme || params.get("console")) {
    params.delete("theme");
    params.delete("console");
    var rest = params.toString();
    history.replaceState(null, "", location.pathname + location.search + (rest ? "#" + rest : ""));
  }
  var chosen = null;
  try { chosen = localStorage.getItem("hpc-theme"); } catch (e) { /* the system's */ }
  if (chosen === "light" || chosen === "dark") document.documentElement.setAttribute("data-theme", chosen);
  if (consoleOrigin && LOOPBACK.test(consoleOrigin)) {
    var link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = consoleOrigin + "/user.css";
    link.id = "console-css";
    document.head.append(link);
  }
})();
