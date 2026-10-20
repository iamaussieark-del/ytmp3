#!/usr/bin/env python3
"""
ytmp3 - download YouTube videos (or any direct media link) as MP3 or MP4.
Spotify track links work too - the song is found on YouTube and saved with
the Spotify title/artist/album and cover art.

usage:
    python ytmp3.py <url> [options]

examples:
    python ytmp3.py "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    python ytmp3.py "https://www.youtube.com/watch?v=jNQXAC9IVRw" -f mp4
    python ytmp3.py "https://example.com/song.mp3" -q 320
    python ytmp3.py "https://youtu.be/jNQXAC9IVRw" -o "C:/Users/me/Music"
    python ytmp3.py "https://open.spotify.com/track/18lKp9uRyR2xJZFvg8ZWUC"
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

# URL suffixes that mean "this link points directly at a media file".
MEDIA_EXTENSIONS = {
    # audio
    ".mp3", ".m4a", ".m4b", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac",
    ".wma", ".aiff", ".mid",
    # video
    ".mp4", ".m4v", ".webm", ".mkv", ".mov", ".avi", ".flv", ".wmv", ".mpg",
    ".mpeg", ".ts", ".3gp",
}

EXAMPLES = """\
examples:
  python ytmp3.py "https://www.youtube.com/watch?v=jNQXAC9IVRw"
      download as MP3 (default, 192 kbps) into .\\downloads

  python ytmp3.py "https://www.youtube.com/watch?v=jNQXAC9IVRw" -f mp4
      download as MP4 video

  python ytmp3.py "https://example.com/podcast.mp3" -o "C:/Users/me/Music" -q 320
      direct media link, custom folder and MP3 bitrate

  python ytmp3.py "https://open.spotify.com/track/18lKp9uRyR2xJZFvg8ZWUC"
      Spotify track: the same song is found on YouTube and saved as a
      tagged MP3 (title/artist/album + cover art)
"""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """Add a scheme if the user pasted 'youtube.com/...' without https://."""
    url = url.strip().strip('"').strip("'")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url):
        url = "https://" + url
    return url


def find_ffmpeg() -> str | None:
    """Locate ffmpeg: system PATH first, then the imageio-ffmpeg package."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def find_deno() -> str | None:
    """Locate the Deno JS runtime (yt-dlp uses it to solve YouTube challenges).

    Checked in order: the bundled copy inside a PyInstaller exe, the standard
    per-user install (~/.deno/bin), then whatever is on PATH. Returns None if
    Deno is not available - yt-dlp still works but may warn and miss formats.
    """
    exe = "deno.exe" if sys.platform.startswith("win") else "deno"

    # 1) bundled next to the frozen exe (PyInstaller extracts data to _MEIPASS)
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        candidate = Path(bundle_dir) / exe
        if candidate.exists():
            return str(candidate)

    # 2) standard per-user Deno install
    candidate = Path.home() / ".deno" / "bin" / exe
    if candidate.exists():
        return str(candidate)

    # 3) anything on PATH
    return shutil.which("deno")


def _js_runtime_opts() -> dict:
    """yt-dlp options that point it at Deno, when we can find one."""
    deno = find_deno()
    if deno:
        return {"js_runtimes": {"deno": {"path": deno}}}
    return {}


def is_direct_media_url(url: str) -> bool:
    """True when the URL points straight at a media file (ends in .mp3 etc.)."""
    suffix = Path(urllib.parse.urlparse(url).path).suffix.lower()
    return suffix in MEDIA_EXTENSIONS


def safe_filename(name: str) -> str:
    """Strip characters that Windows does not allow in file names."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "download"


def unique_path(path: Path) -> Path:
    """Return a non-existing path: song.mp3, song (1).mp3, song (2).mp3 ..."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(1, 1000):
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free file name for {path}")


def human_size(num_bytes: float) -> str:
    """1234567 -> '1.2 MB'"""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GB"


# ---------------------------------------------------------------------------
# direct downloads (plain file links)
# ---------------------------------------------------------------------------

def _print_progress(done: int, total: int | None, state: dict) -> None:
    """Draw a simple progress line; throttled so we don't spam the console."""
    if total:
        pct = min(int(done * 100 / total), 100)
        if pct == state.get("pct"):
            return
        state["pct"] = pct
        bar = "#" * (pct // 5)
        line = f"\r  [{bar:<20}] {pct:3d}%  {human_size(done)} / {human_size(total)}"
    else:
        if done - state.get("last", 0) < 1024 * 1024:
            return
        state["last"] = done
        line = f"\r  downloaded {human_size(done)}"
    print(line, end="", flush=True)


def download_direct(url: str, output_dir: Path, on_progress=None) -> Path:
    """Download a URL that points directly at a media file.

    on_progress, if given, is called as on_progress(done_bytes, total_bytes|None)
    so a GUI can draw its own progress bar.
    """
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        # file name: Content-Disposition header first, then the URL path
        name = ""
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^";]+)"?', disposition)
        if match:
            name = match.group(1)
        if not name:
            name = Path(urllib.parse.urlparse(url).path).name
        name = safe_filename(name)
        if "." not in name:
            name = "download"

        dest = unique_path(output_dir / name)
        total_raw = response.headers.get("Content-Length")
        total = int(total_raw) if total_raw and total_raw.isdigit() else None

        print(f"[ytmp3] saving to: {dest}")
        downloaded = 0
        state: dict = {}
        with open(dest, "wb") as out:
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if on_progress is not None:
                    on_progress(downloaded, total)
                else:
                    _print_progress(downloaded, total, state)
        if on_progress is None:
            print()
    return dest


def convert_to_mp3(source: Path, bitrate_kbps: str, ffmpeg: str) -> Path:
    """Convert a downloaded file to MP3 with ffmpeg, then remove the original."""
    target = unique_path(source.with_suffix(".mp3"))
    print(f"[ytmp3] converting {source.name} -> MP3 ({bitrate_kbps} kbps)")
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
         "-vn", "-codec:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k",
         str(target)],
        check=True,
    )
    source.unlink(missing_ok=True)
    return target


# ---------------------------------------------------------------------------
# site downloads (YouTube and 1000+ other sites, via yt-dlp)
# ---------------------------------------------------------------------------

def download_site(url: str, media_format: str, output_dir: Path,
                  bitrate_kbps: str, ffmpeg: str | None,
                  progress_hooks=None, logger=None,
                  output_stem: Path | None = None) -> list[Path]:
    """Download from a video site with yt-dlp; returns the saved file path(s).

    progress_hooks / logger, if given, are passed straight to yt-dlp so a GUI
    can show progress and capture log lines instead of writing to the console.

    output_stem, if given, forces the output file name (without extension)
    instead of the video's own title.
    """
    template = (str(output_stem) if output_stem is not None
                else str(output_dir / "%(title)s"))
    options = {
        "outtmpl": template + ".%(ext)s",
        "noplaylist": True,          # grab a single video even if URL has a list
        "retries": 3,
        "ffmpeg_location": ffmpeg,   # None -> yt-dlp searches PATH itself
        **_js_runtime_opts(),        # point yt-dlp at Deno for YouTube
    }
    if progress_hooks is not None:
        options["progress_hooks"] = progress_hooks
    if logger is not None:
        options["logger"] = logger

    if media_format == "mp3":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": bitrate_kbps,
        }]
    else:  # mp4
        options["format"] = ("bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                             "bestvideo+bestaudio/best")
        options["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)

    # collect the actual output file path(s) yt-dlp produced
    files: list[Path] = []

    def collect(entry: dict) -> None:
        for dl in entry.get("requested_downloads") or []:
            filepath = dl.get("filepath")
            if filepath:
                files.append(Path(filepath))

    if isinstance(info, dict):
        if info.get("_type") == "playlist":
            for entry in info.get("entries") or []:
                if entry:
                    collect(entry)
        else:
            collect(info)
    return files


# ---------------------------------------------------------------------------
# Spotify links (their streams are DRM-protected, so yt-dlp refuses them;
# instead we read the track's public metadata, find the same song on
# YouTube with yt-dlp's search, download it there and tag it with the
# Spotify title/artist/album + cover art)
# ---------------------------------------------------------------------------

SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com"}
SPOTIFY_KINDS = {"track", "episode"}


def is_spotify_url(url: str) -> bool:
    """True for open.spotify.com links (track, album, playlist, ...)."""
    host = urllib.parse.urlparse(url).hostname or ""
    return host.lower() in SPOTIFY_HOSTS


def _http_get(url: str, timeout: int = 30) -> bytes:
    """Tiny GET with a browser-ish User-Agent (Spotify 403s some clients)."""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _parse_meta_tags(page: str) -> dict[str, list[str]]:
    """Collect <meta> tag contents: {'og:title': ['value', ...], ...}."""
    collected: dict[str, list[str]] = {}
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        key = re.search(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag)
        content = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag)
        if key and content:
            collected.setdefault(key.group(1), []).append(content.group(1))
    return collected


def resolve_spotify_track(url: str) -> dict:
    """Look up a Spotify track link and return its metadata.

    No API key needed: the public track page carries meta tags with the title,
    artist(s), album, year and cover art. Only single track / episode links
    are supported (ValueError otherwise).
    """
    path = urllib.parse.urlparse(url).path
    match = re.search(
        r"/(?:intl-[a-zA-Z-]+/)?(track|episode|album|playlist|show|artist)"
        r"/([A-Za-z0-9]+)", path)
    if not match:
        raise ValueError(f"could not parse Spotify link: {url}")
    kind, track_id = match.group(1), match.group(2)
    if kind not in SPOTIFY_KINDS:
        raise ValueError(
            f"this is a Spotify '{kind}' link, but only single track links "
            f"are supported - open it in Spotify, use 'Share > Copy link "
            f"to song' on the individual track, and paste that")

    page = _http_get(f"https://open.spotify.com/{kind}/{track_id}").decode(
        "utf-8", "replace")
    metas = _parse_meta_tags(page)

    title = html.unescape((metas.get("og:title") or [""])[0]).strip()
    if not title:
        page_title = re.search(r"<title[^>]*>(.*?)</title>", page, re.S | re.I)
        if page_title:
            title = html.unescape(page_title.group(1)).split(" - ")[0].strip()
    if not title:
        raise ValueError(f"could not read track info for {url}")

    # artists: 'music:musician_description' repeats once per artist
    artists = [html.unescape(a).strip()
               for a in metas.get("music:musician_description", []) if a.strip()]
    description = html.unescape((metas.get("og:description") or [""])[0])
    # "Artist · Album · Song · Year" (middle dot or bullet as separator)
    segments = [p.strip() for p in re.split(r"[\u00b7\u2022]", description)]
    if not artists and len(segments) > 1:
        artists = [segments[0]]
    artist = ", ".join(a for a in artists if a)

    # album / year: og:description is "Artist · Album · Song · 2018"
    album, year = "", ""
    if len(segments) >= 4 and segments[2] in ("Song", "Episode"):
        album = segments[1]
    year_match = re.search(r"\b(19|20)\d{2}\b", description)
    if year_match:
        year = year_match.group(0)

    # cover art: og:image is 640x640 on the track page
    cover = (metas.get("og:image") or [""])[0]

    # duration is not in the meta tags; the embed page's JSON state has it
    duration = None
    try:
        embed = _http_get(
            f"https://open.spotify.com/embed/{kind}/{track_id}").decode(
                "utf-8", "replace")
        blob = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            embed, re.S)
        if blob:
            entity = (json.loads(blob.group(1)).get("props", {})
                      .get("pageProps", {}).get("state", {})
                      .get("data", {}).get("entity", {}))
            if isinstance(entity.get("duration"), (int, float)):
                duration = round(entity["duration"] / 1000)
    except Exception:
        pass   # duration only improves YouTube match quality; not required

    return {"kind": kind, "title": title, "artist": artist, "album": album,
            "year": year, "cover": cover, "duration": duration}


def search_youtube_track(meta: dict, logger=None) -> dict:
    """Search YouTube for the same song and return the best matching entry.

    Results are scored by title/artist word overlap, audio-style uploads
    ('... - Topic', 'official audio', ...) and how close each duration is
    to the Spotify track.
    """
    query = f"{meta['artist']} {meta['title']}".strip()
    options = {"quiet": True, "extract_flat": True, "skip_download": True,
               **_js_runtime_opts()}
    if logger is not None:
        options["logger"] = logger
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"ytsearch8:{query}", download=False)
    entries = [entry for entry in (info or {}).get("entries") or [] if entry]
    if not entries:
        raise RuntimeError(f"no YouTube results for {query!r}")

    def words_of(text: str) -> list[str]:
        return [w for w in re.split(r"\W+", text.lower()) if len(w) > 2]

    artist_words = words_of(meta["artist"])
    title_words = words_of(meta["title"])
    wanted_duration = meta.get("duration")

    def score(entry: dict) -> float:
        text = " ".join((entry.get("title") or "",
                         entry.get("channel") or "",
                         entry.get("uploader") or "")).lower()
        value = 0.0
        for wanted, weight in ((artist_words, 4.0), (title_words, 4.0)):
            if wanted:
                value += weight * sum(w in text for w in wanted) / len(wanted)
        if any(tag in text for tag in ("topic", "audio", "lyric")):
            value += 1.0
        duration = entry.get("duration")
        if duration and wanted_duration:
            value -= min(4.0, abs(duration - wanted_duration)
                         / max(wanted_duration / 3.0, 1.0))
        return value

    return max(entries, key=score)


def tag_mp3(path: Path, meta: dict, ffmpeg: str, say) -> Path:
    """Embed the Spotify metadata + cover art into a downloaded MP3.

    Remuxes with -c copy (no re-encode); on any failure the untouched
    download is kept and a warning is printed instead.
    """
    # cover art (640x640 jpeg from the Spotify page) -> temp file
    cover_file = None
    if meta.get("cover"):
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg",
                                            prefix="ytmp3_cover_",
                                            delete=False) as temp:
                temp.write(_http_get(meta["cover"]))
                cover_file = temp.name
        except Exception as exc:
            say(f"warning: could not fetch cover art ({exc})")
            cover_file = None

    target = path.with_name(path.stem + ".tagged.mp3")
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-i", str(path)]
    if cover_file:
        command += ["-i", cover_file, "-map", "0:a", "-map", "1:v"]
    else:
        command += ["-map", "0:a"]
    command += ["-c", "copy", "-id3v2_version", "3"]
    for key, value in (("title", meta["title"]), ("artist", meta["artist"]),
                      ("album", meta["album"]), ("date", meta["year"])):
        if value:
            command += ["-metadata", f"{key}={value}"]
    if cover_file:
        command += ["-metadata:s:v", "title=Album cover",
                    "-metadata:s:v", "comment=Cover (front)"]
    command.append(str(target))

    try:
        say("embedding track info and cover art ...")
        subprocess.run(command, check=True)
        target.replace(path)
    except Exception as exc:
        target.unlink(missing_ok=True)
        say(f"warning: could not tag the MP3 ({exc}) - file kept as-is")
    finally:
        if cover_file:
            Path(cover_file).unlink(missing_ok=True)
    return path


def download_spotify(url: str, media_format: str, output_dir: Path,
                     bitrate_kbps: str, ffmpeg: str | None,
                     progress_hooks=None, logger=None,
                     on_message=None) -> list[Path]:
    """Handle a Spotify track link end-to-end; returns the saved file path(s).

    1. resolve the track's metadata (title/artist/album/cover/duration)
    2. find the same song on YouTube
    3. download it there via yt-dlp (MP3 by default, tagged with metadata)
    """
    say = on_message or (lambda text: print(f"[ytmp3] {text}"))

    say(f"spotify link: {url}")
    meta = resolve_spotify_track(url)
    details = ", ".join(x for x in (meta["album"], meta["year"]) if x)
    say(f"track: {' - '.join(x for x in (meta['artist'], meta['title']) if x)}"
        + (f"  ({details})" if details else ""))

    say("searching YouTube for the song ...")
    match = search_youtube_track(meta, logger=logger)
    match_url = (match.get("url") or match.get("webpage_url")
                 or f"https://www.youtube.com/watch?v={match.get('id')}")
    channel = match.get("channel") or match.get("uploader") or ""
    say(f"matched on YouTube: {match.get('title') or match_url}"
        + (f"  [{channel}]" if channel else "") + f"  ({match_url})")

    # fixed output name built from the Spotify metadata (never overwritten)
    base = safe_filename(" - ".join(x for x in (meta["artist"],
                                                 meta["title"]) if x))
    stem = unique_path(output_dir / f"{base}.{media_format}").with_suffix("")

    saved_files = download_site(match_url, media_format, output_dir,
                               bitrate_kbps, ffmpeg,
                               progress_hooks=progress_hooks, logger=logger,
                               output_stem=stem)
    if not saved_files:
        raise RuntimeError("yt-dlp finished but produced no file")

    if media_format == "mp3" and ffmpeg:
        for path in saved_files:
            if path.suffix.lower() == ".mp3":
                tag_mp3(path, meta, ffmpeg, say)
    elif media_format == "mp3":
        say("warning: ffmpeg not found - skipping MP3 tag/cover embedding")
    return saved_files


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ytmp3",
        description="Download YouTube videos, Spotify tracks or direct media "
                    "links as MP3 or MP4.",
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("url",
                        help="YouTube/video URL, a Spotify track link, or a "
                             "direct link to a media file")
    parser.add_argument("-f", "--format", choices=("mp3", "mp4"), default=None,
                        help="output format (default: MP3 for site links, "
                             "original format for direct file links)")
    parser.add_argument("-o", "--output", default="downloads", metavar="DIR",
                        help="output folder (default: ./downloads)")
    parser.add_argument("-q", "--quality", default="192", metavar="KBPS",
                        help="MP3 bitrate in kbps: 128 / 192 / 320 (default: 192)")
    args = parser.parse_args(argv)

    url = normalize_url(args.url)
    direct = is_direct_media_url(url)
    spotify = is_spotify_url(url)

    if yt_dlp is None and not direct:
        print("[ytmp3] error: yt-dlp is not installed. run:  pip install yt-dlp",
              file=sys.stderr)
        return 1

    # smart default: MP3 for site links, keep original for direct file links
    media_format = args.format or ("original" if direct else "mp3")

    output_dir = Path(args.output).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    ffmpeg = find_ffmpeg()
    if media_format == "mp3" and ffmpeg is None:
        print("[ytmp3] warning: ffmpeg not found, MP3 conversion may fail")
        print("[ytmp3]          install ffmpeg or run:  pip install imageio-ffmpeg")

    try:
        if direct:
            print(f"[ytmp3] direct media link: {url}")
            saved = download_direct(url, output_dir)
            if (media_format == "mp3" and saved.suffix.lower() != ".mp3"
                    and ffmpeg):
                saved = convert_to_mp3(saved, args.quality, ffmpeg)
            saved_files = [saved]
        elif spotify:
            # Spotify streams are DRM-protected: resolve the track's info,
            # download the same song from YouTube, tag it like the original
            saved_files = download_spotify(url, media_format, output_dir,
                                           args.quality, ffmpeg)
        else:
            print(f"[ytmp3] downloading with yt-dlp: {url}")
            saved_files = download_site(url, media_format, output_dir,
                                       args.quality, ffmpeg)
            if not saved_files:
                raise RuntimeError("yt-dlp finished but produced no file")

        print()
        for path in saved_files:
            print(f"[ytmp3] saved: {path}  ({human_size(path.stat().st_size)})")
        return 0

    except KeyboardInterrupt:
        print("\n[ytmp3] cancelled", file=sys.stderr)
        return 130
    except Exception as exc:  # CLI boundary: show any failure nicely
        print(f"[ytmp3] error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

