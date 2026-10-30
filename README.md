# Happy Birthday MOTHERv3 — community guestbook

A static page (GitHub Pages, no build step in the browser) that gathers the
community's birthday messages and the collaborative Magma board.

Everything flows through one command:

```bash
python tools/build.py
```

Pillow (`pip install Pillow`) is optional but recommended (resizes/optimises
images, builds Magma tiles). `inbox/` holds raw inputs and is **not** committed;
only the generated `data/` and `img/` output is.

## Common tasks

- **Add messages** — scroll the whole Discord channel, save the page, drop the
  `.html` in `inbox/discord-html/`, run the build. Media is downloaded at build
  time, so **run it soon after saving** (the image links expire). Merged by
  message id, so re-captures never duplicate.
- **Hide / pin / correct a message** — edit `data/overrides.json`
  (`{ "<id>": { "hidden": true } }`, or `pinned` / `lang` / `author` / `content`).
- **Add a message by hand** — add an entry to `data/manual.json`.
- **Update the Magma board** — replace `img/magma/source.png` (or `.jpg`),
  run the build. Add `--tiles` if it's larger than ~8000px.
- **Side decorations** — drop images in `inbox/decor/`; positions live in
  `data/decor.json`. Run the build.

Build options: `--dry-run`, `--clean`, `--tiles`, `--force`.

## Test locally

`fetch` needs a server (not `file://`):

```bash
python -m http.server 8000   # then open http://localhost:8000
```

## Deploy

Push to GitHub, then **Settings → Pages → Deploy from a branch → `main` / root**.
Served at `https://<user>.github.io/<repo>/`. To update: build, commit, push.
