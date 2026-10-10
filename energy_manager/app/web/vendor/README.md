# Vendored browser assets

These files are the **only** third-party code in this repository (SPEC R3). They are served
locally by the add-on; the SPA never loads anything from the network at runtime.

## uPlot 1.6.32

| | |
| --- | --- |
| Project | uPlot (μPlot) — https://github.com/leeoniya/uPlot |
| Version | 1.6.32 (tag `1.6.32`, released 2025-03-14) |
| Licence | MIT — see `LICENSE.uPlot.txt` (copied from the upstream `LICENSE` file at that tag) |
| Source URLs | `https://raw.githubusercontent.com/leeoniya/uPlot/1.6.32/dist/uPlot.iife.min.js` and `.../dist/uPlot.min.css` |
| Global export | `uPlot` (IIFE build) |

Upstream SHA-256 checksums (the bytes actually downloaded):

```
19c8d4c6ad88929a79f4ae49d6f7161566dfd0ba3d15cc495e974f787eb78f1f  uPlot.iife.min.js
df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04  uPlot.min.css
```

Local files: `uPlot.min.js` and `uPlot.min.css`. **The only local modification is a prepended
comment**: the upstream `dist/uPlot.iife.js` licence banner was prepended to the minified JS, and a
one-line `/*! uPlot v1.6.32 | MIT License | ... */` banner to the CSS, because the minified builds
ship without the MIT notice. Re-download and diff against the checksums above to verify.

### Refresh procedure

```sh
cd /tmp
curl -fsSLO https://raw.githubusercontent.com/leeoniya/uPlot/1.6.32/dist/uPlot.iife.min.js
curl -fsSLO https://raw.githubusercontent.com/leeoniya/uPlot/1.6.32/dist/uPlot.min.css
sha256sum uPlot.iife.min.js uPlot.min.css   # must match the table above
```

Then prepend the licence banners and update this table. Do not switch to a CDN URL: the add-on has
no runtime network access for the UI.
