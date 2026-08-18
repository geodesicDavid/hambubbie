#!/usr/bin/env python3
"""
Sync a YouTube channel into a podcast feed.

For each video not yet in state.json:
  1. download bestaudio with yt-dlp
  2. transcode to mono VBR mp3 with ffmpeg
  3. publish it as a GitHub Release tagged ep-<video_id>
  4. record the returned asset URL in state.json

Then regenerate feed.xml from state.json alone. The feed is always a pure
function of the state file; nothing is ever read back out of the feed.

Usage:
    scripts/sync.py                 # incremental: fetch anything new
    scripts/sync.py --dry-run       # show what would happen, touch nothing
    scripts/sync.py --limit 3       # process at most 3 new videos
    scripts/sync.py --only VIDEOID  # process one specific video
    scripts/sync.py --feed-only     # just rebuild feed.xml from state.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"
FEED_PATH = ROOT / "feed.xml"

# yt-dlp flags derived from config["youtube"]; populated once in main() and
# applied to every yt-dlp invocation (listing, metadata, download) so auth is
# consistent across all three.
YT_EXTRA: list[str] = []

ITUNES_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"
ATOM_NS = "http://www.w3.org/2005/Atom"


# ---------------------------------------------------------------- utilities


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> "typing.NoReturn":  # noqa: F821
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, capturing output, raising on failure."""
    return subprocess.run(cmd, check=True, text=True, capture_output=True, **kw)


def require_tools() -> None:
    missing = [t for t in ("yt-dlp", "ffmpeg", "ffprobe", "gh") if not shutil.which(t)]
    if missing:
        die(f"missing required tools: {', '.join(missing)}")


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open() as f:
        return json.load(f)


def save_json(path: Path, data) -> None:
    """Write atomically so an interrupted run can't corrupt state.json."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)


def slugify(title: str, video_id: str) -> str:
    """Filename for the release asset.

    GitHub rewrites spaces and some punctuation in uploaded asset names, so we
    normalize up front and then trust the URL the API hands back rather than
    reconstructing it from this name.
    """
    s = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip()
    s = re.sub(r"[\s_-]+", "-", s).strip("-").lower()
    s = s[:80].strip("-") or "episode"
    return f"{s}-{video_id}.mp3"


def build_yt_extra(config: dict) -> list[str]:
    """Translate config["youtube"] into yt-dlp arguments.

    YouTube regularly starts rejecting unauthenticated media fetches with a 403
    even when metadata extraction succeeds. Cookies from a logged-in browser are
    the durable fix; extractor_args is the escape hatch for pinning player
    clients when YouTube changes behaviour again.
    """
    yt = config.get("youtube") or {}
    args: list[str] = []

    browser = yt.get("cookies_from_browser")
    cookie_file = yt.get("cookies_file")
    if browser and cookie_file:
        die("set only one of youtube.cookies_from_browser or youtube.cookies_file")
    if browser:
        args += ["--cookies-from-browser", browser]
    elif cookie_file:
        path = Path(cookie_file).expanduser()
        if not path.exists():
            die(f"cookies_file not found: {path}")
        args += ["--cookies", str(path)]

    for ea in yt.get("extractor_args") or []:
        args += ["--extractor-args", ea]

    if yt.get("sleep_requests"):
        args += ["--sleep-requests", str(yt["sleep_requests"])]

    return args


# ------------------------------------------------------------------ youtube


def list_channel_videos(channel_url: str) -> tuple[dict, list[dict]]:
    """Return (channel_info, entries) using a flat listing.

    A flat listing is cheap (one request per page, no per-video extraction) and
    returns the whole back catalog, unlike YouTube's own RSS feed which is
    capped at the 15 most recent uploads.
    """
    log(f"listing videos from {channel_url} ...")
    proc = run([
        "yt-dlp",
        "--flat-playlist",
        "--dump-single-json",
        "--ignore-errors",
        *YT_EXTRA,
        channel_url,
    ])
    data = json.loads(proc.stdout)
    entries = [e for e in (data.get("entries") or []) if e and e.get("id")]
    return data, entries


def fetch_metadata(video_id: str) -> dict:
    """Full per-video metadata, needed for the real upload timestamp."""
    proc = run([
        "yt-dlp",
        "--dump-single-json",
        "--no-download",
        *YT_EXTRA,
        f"https://www.youtube.com/watch?v={video_id}",
    ])
    return json.loads(proc.stdout)


def published_at(meta: dict) -> str:
    """Resolve an upload time to a stable ISO-8601 UTC string.

    Preference order: release_timestamp, timestamp, then upload_date pinned to
    12:00 UTC. Recorded once at ingest and never recomputed, so pubDates stay
    identical across reruns even if YouTube changes what it reports.
    """
    for key in ("release_timestamp", "timestamp"):
        ts = meta.get(key)
        if ts:
            return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).isoformat()

    ymd = meta.get("upload_date")
    if ymd:
        d = dt.datetime.strptime(ymd, "%Y%m%d").replace(
            hour=12, tzinfo=dt.timezone.utc
        )
        return d.isoformat()

    return dt.datetime.now(dt.timezone.utc).isoformat()


# ----------------------------------------------------------------- encoding


def download_audio(video_id: str, workdir: Path) -> Path:
    log(f"  downloading audio ...")
    out_tmpl = str(workdir / "source.%(ext)s")
    run([
        "yt-dlp",
        "-f", "bestaudio/best",
        "--no-playlist",
        "-o", out_tmpl,
        *YT_EXTRA,
        f"https://www.youtube.com/watch?v={video_id}",
    ])
    files = list(workdir.glob("source.*"))
    if not files:
        raise RuntimeError("yt-dlp produced no output file")
    return files[0]


def transcode(src: Path, dest: Path, meta: dict, enc: dict, channel: str) -> None:
    """Transcode to mono VBR mp3.

    libmp3lame -q:a 5 averages ~130 kbps in stereo, so at one channel it lands
    around 64 kbps -- the usual target for spoken-word audio.
    """
    log(f"  encoding to mono VBR mp3 (q={enc['vbr_quality']}) ...")
    year = (meta.get("upload_date") or "")[:4]
    run([
        "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
        "-i", str(src),
        "-vn",
        "-map_metadata", "-1",
        "-ac", str(enc["channels"]),
        "-ar", str(enc["sample_rate"]),
        "-c:a", "libmp3lame",
        "-q:a", str(enc["vbr_quality"]),
        "-metadata", f"title={meta.get('title', '')}",
        "-metadata", f"artist={channel}",
        "-metadata", f"album={channel}",
        "-metadata", f"date={year}",
        "-metadata", f"comment=https://www.youtube.com/watch?v={meta.get('id', '')}",
        "-id3v2_version", "3",
        str(dest),
    ])


def probe_duration(path: Path) -> int:
    proc = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return int(float(proc.stdout.strip()))


# ------------------------------------------------------------------- github


def resolve_repo(config: dict) -> str:
    if config.get("repo"):
        return config["repo"]
    try:
        proc = run(["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"])
        return proc.stdout.strip()
    except subprocess.CalledProcessError:
        die("could not determine the repo; set \"repo\": \"owner/name\" in config.json")


def release_exists(repo: str, tag: str) -> bool:
    proc = subprocess.run(
        ["gh", "release", "view", tag, "--repo", repo, "--json", "tagName"],
        text=True, capture_output=True,
    )
    return proc.returncode == 0


def publish_release(repo: str, tag: str, title: str, notes: str, asset: Path) -> str:
    """Create the release (or add to it if it already exists) and return the
    canonical download URL GitHub assigned to the asset."""
    if release_exists(repo, tag):
        log(f"  release {tag} exists, uploading asset ...")
        run(["gh", "release", "upload", tag, str(asset), "--repo", repo, "--clobber"])
    else:
        log(f"  creating release {tag} ...")
        run([
            "gh", "release", "create", tag, str(asset),
            "--repo", repo,
            "--title", title,
            "--notes", notes,
        ])

    proc = run(["gh", "release", "view", tag, "--repo", repo, "--json", "assets"])
    assets = json.loads(proc.stdout).get("assets", [])
    for a in assets:
        if a.get("name") == asset.name:
            return a["url"]
    if assets:
        return assets[0]["url"]
    raise RuntimeError(f"no assets found on release {tag} after upload")


# --------------------------------------------------------------------- feed


def rfc2822(iso: str) -> str:
    return format_datetime(dt.datetime.fromisoformat(iso))


def hhmmss(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:d}:{s:02d}"


def build_feed(config: dict, state: dict) -> str:
    feed_cfg = config["feed"]
    channel_meta = state.get("channel", {})

    title = feed_cfg.get("title") or channel_meta.get("title") or "Podcast"
    desc = feed_cfg.get("description") or channel_meta.get("description") or title
    author = feed_cfg.get("author") or channel_meta.get("title") or title
    image = feed_cfg.get("image") or channel_meta.get("thumbnail") or ""
    link = channel_meta.get("url") or config.get("channel_url", "")
    site = (config.get("site_url") or "").rstrip("/")
    self_url = f"{site}/feed.xml" if site else ""

    episodes = sorted(
        state.get("episodes", {}).values(),
        key=lambda e: e["published_at"],
        reverse=True,
    )

    out: list[str] = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        f'<rss version="2.0" xmlns:itunes="{ITUNES_NS}" xmlns:atom="{ATOM_NS}">'
    )
    out.append("  <channel>")
    out.append(f"    <title>{escape(title)}</title>")
    out.append(f"    <link>{escape(link)}</link>")
    out.append(f"    <description>{escape(desc)}</description>")
    out.append(f"    <language>{escape(feed_cfg.get('language', 'en-us'))}</language>")
    out.append(f"    <lastBuildDate>{format_datetime(dt.datetime.now(dt.timezone.utc))}</lastBuildDate>")
    if self_url:
        out.append(
            f'    <atom:link href="{escape(self_url)}" rel="self" type="application/rss+xml"/>'
        )
    out.append(f"    <itunes:author>{escape(author)}</itunes:author>")
    out.append(f"    <itunes:summary>{escape(desc)}</itunes:summary>")
    out.append(f"    <itunes:explicit>{escape(feed_cfg.get('explicit', 'no'))}</itunes:explicit>")
    out.append("    <itunes:type>episodic</itunes:type>")
    if image:
        out.append(f'    <itunes:image href="{escape(image)}"/>')
    if feed_cfg.get("category"):
        out.append(f'    <itunes:category text="{escape(feed_cfg["category"])}"/>')
    if feed_cfg.get("email"):
        out.append("    <itunes:owner>")
        out.append(f"      <itunes:name>{escape(author)}</itunes:name>")
        out.append(f"      <itunes:email>{escape(feed_cfg['email'])}</itunes:email>")
        out.append("    </itunes:owner>")

    for ep in episodes:
        out.append("    <item>")
        out.append(f"      <title>{escape(ep['title'])}</title>")
        out.append(f"      <link>https://www.youtube.com/watch?v={ep['video_id']}</link>")
        out.append(f"      <guid isPermaLink=\"false\">{escape(ep['video_id'])}</guid>")
        out.append(f"      <pubDate>{rfc2822(ep['published_at'])}</pubDate>")
        if ep.get("description"):
            out.append(f"      <description>{escape(ep['description'])}</description>")
        out.append(
            f'      <enclosure url="{escape(ep["url"])}" '
            f'length="{ep["size_bytes"]}" type="audio/mpeg"/>'
        )
        out.append(f"      <itunes:duration>{hhmmss(ep['duration'])}</itunes:duration>")
        out.append(f"      <itunes:explicit>{escape(feed_cfg.get('explicit', 'no'))}</itunes:explicit>")
        out.append("    </item>")

    out.append("  </channel>")
    out.append("</rss>")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------- main


def process_video(video_id: str, repo: str, config: dict, channel_name: str) -> dict:
    meta = fetch_metadata(video_id)
    title = meta.get("title") or video_id
    log(f"* {video_id}  {title}")

    with tempfile.TemporaryDirectory() as td:
        workdir = Path(td)
        src = download_audio(video_id, workdir)
        mp3 = workdir / slugify(title, video_id)
        transcode(src, mp3, meta, config["encode"], channel_name)

        size = mp3.stat().st_size
        duration = probe_duration(mp3)
        log(f"  {size / 1_000_000:.1f} MB, {hhmmss(duration)}")

        notes = f"Source: https://www.youtube.com/watch?v={video_id}"
        url = publish_release(repo, f"ep-{video_id}", title, notes, mp3)

    desc = (meta.get("description") or "").strip()
    return {
        "video_id": video_id,
        "title": title,
        "description": desc[:4000],
        "published_at": published_at(meta),
        "duration": duration,
        "size_bytes": size,
        "url": url,
        "release_tag": f"ep-{video_id}",
        "ingested_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--limit", type=int, help="process at most N new videos")
    ap.add_argument("--only", metavar="VIDEO_ID", help="process one specific video")
    ap.add_argument("--feed-only", action="store_true", help="rebuild feed.xml from state.json")
    args = ap.parse_args()

    require_tools()
    config = load_json(CONFIG_PATH, None)
    if config is None:
        die(f"{CONFIG_PATH} not found")
    state = load_json(STATE_PATH, {"channel": {}, "episodes": {}})
    state.setdefault("channel", {})
    state.setdefault("episodes", {})

    global YT_EXTRA
    YT_EXTRA = build_yt_extra(config)

    if args.feed_only:
        FEED_PATH.write_text(build_feed(config, state))
        log(f"wrote {FEED_PATH} ({len(state['episodes'])} episodes)")
        return

    repo = resolve_repo(config)
    log(f"repo: {repo}")

    if args.only:
        todo = [args.only]
        channel_name = state["channel"].get("title", "")
    else:
        info, entries = list_channel_videos(config["channel_url"])
        state["channel"] = {
            "title": info.get("channel") or info.get("title") or "",
            "description": (info.get("description") or "").strip()[:4000],
            "url": info.get("channel_url") or config["channel_url"],
            "channel_id": info.get("channel_id") or "",
            "thumbnail": state["channel"].get("thumbnail") or "",
        }
        channel_name = state["channel"]["title"]
        known = set(state["episodes"])
        todo = [e["id"] for e in entries if e["id"] not in known]
        log(f"{len(entries)} videos on channel, {len(known)} already synced, {len(todo)} new")

    if args.limit:
        todo = todo[: args.limit]

    if args.dry_run:
        for vid in todo:
            log(f"  would process {vid}")
        log(f"dry run: {len(todo)} video(s) would be processed")
        return

    failures: list[tuple[str, str]] = []
    for vid in todo:
        try:
            state["episodes"][vid] = process_video(vid, repo, config, channel_name)
            # Persist after each episode so an interrupted run keeps its progress
            # and never re-uploads what already succeeded.
            save_json(STATE_PATH, state)
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or "").strip().splitlines()[-1:] or ["failed"]
            log(f"  SKIPPED {vid}: {msg[0]}")
            failures.append((vid, msg[0]))
        except Exception as e:  # noqa: BLE001
            log(f"  SKIPPED {vid}: {e}")
            failures.append((vid, str(e)))

    save_json(STATE_PATH, state)
    FEED_PATH.write_text(build_feed(config, state))

    log("")
    log(f"done. {len(state['episodes'])} episode(s) in feed.xml")
    if failures:
        log(f"{len(failures)} video(s) skipped:")
        for vid, msg in failures:
            log(f"  {vid}: {msg}")
        sys.exit(1)


if __name__ == "__main__":
    main()
