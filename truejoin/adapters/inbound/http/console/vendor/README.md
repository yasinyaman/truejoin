# Vendored for the console

Served from `/ui/vendor/` so the console needs no CDN (air-gapped installs, and
a CSP of `'self'`). Do not edit these files; regenerate them.

| Directory | What | Version | Licence | Source |
|---|---|---|---|---|
| `carbon/` | Carbon Design System web components (ES module bundles and their chunks) | 2.66.0 | Apache-2.0 (`carbon/LICENSE`) | `https://1.www.s81c.com/common/carbon/web-components/version/v2.66.0/` |
| `carbon/themes.css` | Carbon theme tokens (`cds-theme-zone-g10`, `-g100`, ...) | v2 latest at vendoring | Apache-2.0 | `.../web-components/tag/v2/latest/themes.css` |
| `plex/` | IBM Plex Sans Regular, SemiBold; IBM Plex Mono Regular | sans 1.1.0, mono 2.5.0 | SIL OFL 1.1 (`plex/LICENSE.txt`) | npm `@ibm/plex-sans`, `@ibm/plex-mono` |

To change the components or the version: `python scripts/vendor_carbon.py <entries...>`
(see the script), then check every console tab.
