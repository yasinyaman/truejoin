"use strict";
// Carbon's themes are classes, not media queries: g10 in light, g100 in dark,
// following the system and its changes. Runs before the page paints.
(() => {
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  const apply = () => {
    document.documentElement.classList.toggle("cds-theme-zone-g100", dark.matches);
    document.documentElement.classList.toggle("cds-theme-zone-g10", !dark.matches);
  };
  apply();
  dark.addEventListener("change", apply);
})();
