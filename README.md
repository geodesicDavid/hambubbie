# hambubbie — YouTube → podcast feed

Turns the [@hambubbie](https://www.youtube.com/@hambubbie/videos) channel into a
podcast RSS feed. Audio is published as GitHub Release assets; only the feed and
its state file live in git.

```
GitHub Pages  →  feed.xml         (small, versioned, in this repo)
                    │
                    │ <enclosure url=…>
                    ▼
GitHub Releases →  the .mp3 files  (large, outside git, CDN-served)
```

One release per episode, tagged `ep-<youtube_video_id>`. The video ID is the
immutable key everywhere: release tag, `state.json` key, and RSS `<guid>`.

## Setup

```sh
brew install yt-dlp ffmpeg gh     # already installed here
gh auth login
```

Then fill in `config.json`:

- `repo` — `owner/name`. Leave `null` to infer it from the git remote.
- `site_url` — your Pages URL, e.g. `https://<user>.github.io/hambubbie`.
  Used for the feed's `atom:link rel="self"`.
- `feed.image` — podcast artwork URL, square, 1400–3000 px. Apple Podcasts
  rejects feeds without it. Leave `null` to fall back to the channel thumbnail.
- `feed.title` / `description` / `author` — `null` means "use the channel's own".

Enable Pages: repo **Settings → Pages → Deploy from branch → `main` / root**.
The feed is then at `<site_url>/feed.xml`.

## Running

```sh
scripts/sync.py                # fetch anything new, update feed.xml
scripts/sync.py --dry-run      # list what's new, change nothing
scripts/sync.py --limit 3      # process at most 3 (good for the first test)
scripts/sync.py --only VIDEOID # redo one specific video
scripts/sync.py --feed-only    # rebuild feed.xml from state.json
```

The script is incremental and idempotent: it compares the channel listing
against `state.json` and only touches videos it hasn't seen. Running it twice
produces the same feed and re-uploads nothing.

After a run, commit the results:

```sh
git add state.json feed.xml && git commit -m "sync" && git push
```

## Encoding

Mono, 44.1 kHz, LAME VBR `-q:a 5` — roughly 64 kbps for speech. A one-hour
episode lands around 28 MB. Release assets allow up to 2 GB each, so there is no
splitting and no file-size ceiling to worry about.

## State

`state.json` is the single source of truth. `feed.xml` is a pure function of it
and is safe to delete and regenerate. Each episode records:

```json
{
  "video_id": "…",           // immutable key + RSS guid
  "title": "…",
  "published_at": "…",       // captured once at ingest, never recomputed
  "duration": 3600,
  "size_bytes": 28000000,    // must match the file exactly or seeking breaks
  "url": "…",                // the URL GitHub returned, not a constructed one
  "release_tag": "ep-…"
}
```

`published_at` is written on first ingest and never touched again, so `pubDate`
values stay stable across reruns. State is saved after every episode, so an
interrupted run keeps its progress.

## Notes

- Recovering from a bad conversion: `gh release delete ep-<id> --cleanup-tag`,
  remove that key from `state.json`, then `scripts/sync.py --only <id>`.
- This repo must stay public and permanent. Deleting it or making it private
  breaks every enclosure URL at once, which breaks subscribers' back catalogs.
