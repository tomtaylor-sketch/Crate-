# Crate Brain — hosted app + APK build guide

This folder is a complete, installable Progressive Web App. Hosting it gives you
a URL you can open on any phone and "add to home screen", and lets you generate a
real Android APK with no Android Studio.

## What's in this folder

- `index.html` — the app (your upgraded version: colour-coded keys, scroll memory, Now Playing flow, deep links, sorting)
- `manifest.json` — app identity (name, icons, colours) so it installs properly
- `sw.js` — service worker, makes the app open and run fully offline after first load
- `icon-192.png`, `icon-512.png`, `icon-512-maskable.png` — app icons
- `crate.json` — **you add this** (your library export). Or load it in-app each time.

## Step 1 — add your data

Copy your `crate.json` (from `python export_to_app.py --inline`) into this folder
next to `index.html`. If you'd rather not host your library publicly, skip this and
just load `crate.json` from your phone via the in-app CHOOSE FILE button as you do now —
the app works either way.

To make the app auto-load a hosted `crate.json`, it already tries IndexedDB first;
to wire automatic fetch of a hosted file, say the word and I'll add ~5 lines.

## Step 2 — host it (free)

### Option A — GitHub Pages
1. Make a free GitHub account if you don't have one
2. Create a new repository, e.g. `crate`
3. Upload everything in this folder (drag-drop in the GitHub web UI works)
4. Repo → Settings → Pages → Source: "Deploy from a branch" → branch `main`, folder `/ (root)` → Save
5. Wait ~1 minute. Your app is live at `https://YOURNAME.github.io/crate/`

### Option B — Netlify Drop (even simpler, no account steps)
1. Go to https://app.netlify.com/drop
2. Drag this whole folder onto the page
3. It gives you a live URL instantly

Open the URL on your phone in Chrome. You can already "add to home screen" here —
that alone gives you and your mate the app with an icon, working offline.

## Step 3 — turn it into an APK (PWABuilder)

1. Go to https://www.pwabuilder.com
2. Paste your hosted URL, hit Start
3. It analyses the PWA (manifest, service worker, icons — all already set up here).
   You want green ticks; minor warnings are fine.
4. Click **Package for stores** → **Android**
5. Choose the signing option (PWABuilder can generate a new signing key for you —
   keep the .keystore file and passwords it gives you somewhere safe; you need them
   to publish updates)
6. Download the generated `.zip` — it contains `app-release-signed.apk`

## Step 4 — install / share

- Email or message the `.apk` to yourself and your mate
- On the phone: open it, allow "install from unknown sources" if prompted, install
- It appears as a real app with the Crate icon, opens standalone, works offline

## Updating later

When you enrich the library or I ship app changes:
- Re-export `crate.json`, re-upload to your host (or just reload the file in-app)
- For app code changes: re-upload `index.html`, and bump `CACHE_VERSION` in `sw.js`
  (e.g. `crate-v2`) so phones pick up the new version instead of the cached old one
- For a new APK: re-run PWABuilder with the **same signing key** from step 3

## Notes

- The deep-link buttons (YouTube / SoundCloud / Bandcamp) open the phone browser — expected.
- `file://` opening won't register the service worker; that's why hosting matters for true offline. Once installed from a hosted URL, offline works.
- Everything here is just static files — no server, no backend, nothing to maintain.
