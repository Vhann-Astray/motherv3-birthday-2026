# Happy Birthday MOTHER — community guestbook

A static page (GitHub Pages, no framework, no build step in the browser) that
gathers the community's birthday messages and the collaborative Magma board.

- **Messages** come from Discord exports (DiscordChatExporter) plus a few
  hand-written ones, turned into `data/messages.json` by one script.
- **The Magma board** is a single big image you retouch yourself; the script
  produces a light version for the page and a full-res one for the zoom viewer.

You add messages, images and new Magma versions over time. Everything flows
through **one command**:

```bash
python tools/build.py
```

---

## Add messages (3 steps)

Primary workflow — a **saved HTML page** of the Discord channel:

1. Open the channel in the Discord web client. **Scroll from the very top all
   the way to the bottom slowly** so every message is rendered (Discord only
   keeps ~50 messages in the page at once — anything you don't scroll past is
   not captured). Then save the page: `Ctrl+S` → *Webpage, Complete*, **or**
   DevTools → right-click the messages list (`<ol data-list-id="chat-messages">`)
   → *Copy* → *Copy outerHTML* into a `.html` file. Put the file in
   `inbox/discord-html/`.
2. Run:
   ```bash
   python tools/build.py
   ```
3. Commit & push:
   ```bash
   git add -A && git commit -m "Add new messages" && git push
   ```

To **update** later, just save a fresh page and drop it in `inbox/discord-html/`
(a new file, or overwrite the old one). The script keys off Discord message ids,
so re-captures merge cleanly and never create duplicates. You can keep several
HTML files side by side; overlapping ones are deduplicated.

> **Capture media while it's fresh.** The HTML only contains temporary,
> signed links (`...?ex=...&hm=...`) that expire after a few hours. `build.py`
> downloads every avatar / image / **video** / custom emoji at build time, so
> **run it soon after saving the page**, not days later.

**Videos & YouTube.** Uploaded videos are downloaded (into `img/videos/`) and
shown with a player; **YouTube links** in a message become an embedded player.
Note that videos are committed to the repo as-is (not re-encoded): GitHub caps
individual files at 100 MB and warns past ~1 GB total, so if someone posts a
huge clip, trim/compress it before building.

### Alternative: DiscordChatExporter (more reliable for the whole channel)

If scrolling a big channel by hand is painful, export it with
**DiscordChatExporter**, format **JSON**, with the **`--media`** option (images
downloaded next to the JSON in a `*_Files/` folder). Drop the `.json` **and**
its `*_Files/` folder into `inbox/discord/`, then run `build.py`. Both sources
can be used together and are merged by message id.

> `inbox/` is **not** committed to git (it holds raw community data and the repo
> is public). You build locally; only the cleaned `data/` and `img/` output is
> pushed.

---

## Add a message by hand

Edit `data/manual.json` (a list). Each entry uses the output format:

```json
{
  "id": "manual-002",
  "author": "A nickname",
  "avatar": null,
  "date": "2026-10-30T12:00:00+00:00",
  "lang": "FR",
  "content": "Your text. **Bold**, *italic*, `code` and > quotes work.",
  "images": ["inbox/manual/my-image.png"],
  "pinned": false
}
```

- Give it a unique `id` starting with `manual-`.
- Put any images in `inbox/manual/` and reference them as shown; the script
  copies and optimises them.
- `lang` can be `"FR"`, `"EN"` or `null`. Omit it to let the script guess.

Then run `python tools/build.py` and push.

## Hide, correct or pin a message

Edit `data/overrides.json`, keyed by the message id (the Discord id, or the
`manual-xxx` id). All fields are optional:

```json
{
  "123456789012345678": { "pinned": true },
  "223456789012345678": { "hidden": true },
  "323456789012345678": { "lang": "FR", "author": "Better name",
                          "content": "A corrected version of the text." }
}
```

- `hidden: true` removes the message from the page.
- `pinned: true` keeps it at the top of the wall, in every sort mode.
- `lang` / `author` / `content` override what was parsed.

The script **never rewrites** `overrides.json` or `manual.json`.

To find a message id: in Discord enable *Developer Mode* (Settings → Advanced),
right-click the message → *Copy Message ID*.

---

## Side decorations

Drop images into `inbox/decor/` to scatter decorations down the left and right
edges of the page (they gently float and parallax as you scroll). The filename
decides the side and the order:

- `left-*` → left edge, `right-*` → right edge (anything else alternates).
- Alphabetical order places them top → bottom (`left-01`, `left-02`, …).

Use **transparent PNGs** (or GIFs — animation is kept) for the best look. Run
`python tools/build.py`; they're optimised into `img/decor/` and shown
automatically. They are hidden on narrow screens (< 1100px) so they never
cover the messages on mobile.

## Update the Magma board

1. Replace `img/magma/source.png` with your new retouched export (any size).
2. Run `python tools/build.py`. The script regenerates:
   - `img/magma/magma-web.webp` — ~2000px wide, shown on the card,
   - `img/magma/magma-full.webp` — full resolution for the zoom viewer.
3. Commit & push.

If the source is **very large** (roughly > 8000px on a side), build tiled
zoom for smoother panning:

```bash
python tools/build.py --tiles
```

This writes `img/magma/tiles/` and the page automatically switches the viewer
to [OpenSeadragon](https://openseadragon.github.io/) (loaded from a CDN). The
plain built-in zoom/pan viewer is used otherwise — no dependency.

---

## Test locally

`fetch` does not work over `file://`, so open the page through a local server:

```bash
python -m http.server 8000
# then visit http://localhost:8000
```

If `data/messages.json` is missing, the page falls back to
`data/messages.sample.json` so you always see something.

---

## Command reference

```bash
python tools/build.py            # normal build
python tools/build.py --dry-run  # show what would change, write nothing
python tools/build.py --clean    # also delete generated media no longer referenced
python tools/build.py --tiles    # also generate Deep Zoom tiles for the Magma board
python tools/build.py --force    # reprocess all media, ignoring the cache
```

The run ends with a summary, e.g.:

```
+5 new messages, 2 new images, 1 hidden, 1 pinned, Magma unchanged, total 87
```

**Pillow** (`pip install Pillow`) is optional but recommended: it resizes images
to 1600px, converts to WebP, optimises the Magma board, and builds tiles.
Without it the script still runs and copies images untouched (with a warning).

---

## Configuration

Front-end settings live at the top of [`assets/app.js`](assets/app.js) in the
`CONFIG` object: the live Magma board URL, the hero intro text, and the footer
line. The big title and FR/EN badges are in the markup/CSS.

---

## Deploy on GitHub Pages

1. Push the repo to GitHub.
2. Repo **Settings → Pages → Source: Deploy from a branch**, pick your branch
   and the `/ (root)` folder.
3. The page is served at `https://<user>.github.io/<repo>/`.

### Optional: auto-build Action (disabled by default)

`.github/workflows/build.yml` can rebuild the data automatically. As shipped it
only runs when triggered by hand (Actions tab → *Build site data* → *Run
workflow*). To run it on every relevant push, uncomment the `push:` block in
that file. Because `inbox/` is gitignored, the Action only rebuilds from
versioned inputs (`data/manual.json`, `data/overrides.json`,
`img/magma/source.png`); full Discord rebuilds stay local.

---

## Project layout

```
index.html                 the page
assets/style.css           styles
assets/app.js              logic + CONFIG
data/messages.json         GENERATED — never edit by hand
data/manual.json           hand-written messages
data/overrides.json        hide / pin / correct, by id
data/version.json          GENERATED — cache-busting hashes
data/messages.sample.json  fallback sample data
inbox/discord-html/        drop saved Discord web pages here (gitignored)
inbox/discord/             DiscordChatExporter exports, if you use them (gitignored)
inbox/manual/              images for hand-written messages (gitignored)
img/magma/source.png       your retouched Magma board
img/magma/, img/avatars/, img/attachments/, img/emoji/   GENERATED
tools/build.py             the one command
```
