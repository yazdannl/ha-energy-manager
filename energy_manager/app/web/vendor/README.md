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

## Inter (variable) 4.1

| | |
| --- | --- |
| Project | Inter — https://rsms.me/inter/ (source: https://github.com/rsms/inter) |
| Version | `InterVariable.woff2` downloaded 2026-10-18 from the project's own font server |
| Licence | SIL Open Font License 1.1 — see `LICENSE.inter.txt` (the upstream `LICENSE.txt`) |
| Source URL | `https://rsms.me/inter/font-files/InterVariable.woff2` |
| Local file | `InterVariable.woff2` |

A single variable font file covers weights 100-900, so the SPA declares one `@font-face` in
`style.css` and never loads a remote stylesheet. `font-display: swap` keeps text visible while it
decodes, and a missing file falls back to the platform UI font declared after it.

```
693b77d4f32ee9b8bfc995589b5fad5e99adf2832738661f5402f9978429a8e3  InterVariable.woff2
```

Size: 352240 bytes. The file is a binary `wOF2`; it is byte-identical to the upstream download
(nothing prepended or edited).

### Refresh procedure

```sh
cd /tmp
curl -fsSLO https://rsms.me/inter/font-files/InterVariable.woff2
sha256sum InterVariable.woff2   # must match the checksum above
curl -fsSL -o LICENSE.inter.txt https://raw.githubusercontent.com/rsms/inter/master/LICENSE.txt
```

Then copy both files into `vendor/` and update this table. The add-on has no runtime network access
for the UI, so the font is served locally like every other asset.

## Bootstrap 5.3.3

| | |
| --- | --- |
| Project | Bootstrap — https://github.com/twbs/bootstrap |
| Version | 5.3.3 (tag `v5.3.3`) |
| Licence | MIT — see `LICENSE.bootstrap.txt` (copied from the upstream `LICENSE` file at that tag) |
| Source URL | `https://raw.githubusercontent.com/twbs/bootstrap/v5.3.3/dist/css/bootstrap.min.css` |
| Local file | `bootstrap.min.css` |

The vendored file is the official `dist/css/bootstrap.min.css` and is byte-identical to the upstream
release: Bootstrap already ships its own licence banner, so nothing was prepended or edited.

```
3c8f27e6009ccfd710a905e6dcf12d0ee3c6f2ac7da05b0572d3e0d12e736fc8  bootstrap.min.css
```

Size: 232803 bytes.

Only the CSS is vendored. Bootstrap's JavaScript is deliberately **not** vendored (SPEC §13.1):
interactions stay in `app.js`. `style.css` loads after this file and maps Bootstrap's `--bs-*`
tokens onto the palette, so the result is this product's design, not Bootstrap's default look.

### Refresh procedure

```sh
cd /tmp
curl -fsSLO https://raw.githubusercontent.com/twbs/bootstrap/v5.3.3/dist/css/bootstrap.min.css
sha256sum bootstrap.min.css   # must match the checksum above
curl -fsSL -o LICENSE.bootstrap.txt https://raw.githubusercontent.com/twbs/bootstrap/v5.3.3/LICENSE
```

Then copy both files into `vendor/` and update this table. Do not switch to a CDN URL: the add-on
has no runtime network access for the UI.
