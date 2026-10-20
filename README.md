# ytmp3

A small tool that downloads **YouTube videos** (and most other video sites),
**Spotify tracks**, or any direct audio/video file link and saves them as
**MP3** (audio) or **MP4** (video).

It comes in three flavours:

- **`ytmp3.exe`** - a standalone Windows app with a GUI. Nothing to install
  (Python, yt-dlp and ffmpeg are all bundled inside it). Just double-click.
- **`ytmp3_gui.py`** - the same GUI, run from a Python install.
- **`ytmp3.py`** - the original command-line version.

## Standalone app (easiest)

Grab **`dist\ytmp3.exe`** and double-click it. Paste a link, choose MP3 or MP4,
pick a folder, and hit **Download**. ffmpeg is built in, so MP3 conversion and
MP4 merging work with nothing else installed.

The exe is ~93 MB because it carries its own copy of Python, yt-dlp, ffmpeg
(~31 MB) and the Deno JS runtime (~40 MB compressed) so it runs on a machine
with none of them installed. Deno is what yt-dlp uses to solve YouTube's
JavaScript challenges - bundling it keeps YouTube downloads working without the
"No supported JavaScript runtime" warning and the missing formats that come
with it.

### Building the exe yourself

Run **`build_exe.bat`** (or the equivalent command below). On the first run it
downloads Deno into `vendor\deno.exe`; after that it reuses it. YouTube changes
over time, so a frozen yt-dlp eventually stops working - when that happens, just
rebuild to pick up the latest yt-dlp:

```bat
build_exe.bat
```

The result lands in `dist\ytmp3.exe`.

## Command-line version

### Setup

Requires Python 3.9+ ([python.org](https://www.python.org)), then:

```
pip install -r requirements.txt
```

`requirements.txt` installs:

| package           | why                                                          |
| ----------------- | ------------------------------------------------------------ |
| `yt-dlp`          | downloads from YouTube and 1000+ other sites                  |
| `imageio-ffmpeg`  | bundled ffmpeg binary used for MP3 conversion / MP4 merging  |

If you already have **ffmpeg** installed and on your PATH, the bundled one is
skipped automatically.

### Usage

```
python ytmp3.py <url> [options]
```

| option        | meaning                                                        |
| ------------- | -------------------------------------------------------------- |
| `-f mp3\|mp4` | output format (default: MP3 for site links, original format for direct file links) |
| `-o DIR`      | output folder (default: `./downloads`)                         |
| `-q KBPS`     | MP3 bitrate: `128` / `192` / `320` (default: `192`)            |

#### Examples

```bat
:: download a YouTube video as MP3 (default, 192 kbps)
python ytmp3.py "https://www.youtube.com/watch?v=jNQXAC9IVRw"

:: download a YouTube video as MP4
python ytmp3.py "https://www.youtube.com/watch?v=jNQXAC9IVRw" -f mp4

:: direct file links work too - saved in their original format
python ytmp3.py "https://example.com/some-song.mp3"

:: convert a direct video link to MP3 at 320 kbps into your Music folder
python ytmp3.py "https://example.com/clip.mp4" -f mp3 -q 320 -o "C:\Users\me\Music"

:: download a Spotify track - the song is found on YouTube and saved as a
:: tagged MP3 (title/artist/album + cover art from Spotify)
python ytmp3.py "https://open.spotify.com/track/18lKp9uRyR2xJZFvg8ZWUC"
```

## Notes

- Files go into `./downloads` unless `-o` is given. Existing files are never
  overwritten - a ` (1)`, ` (2)` ... suffix is added instead.
- Direct media links (URLs ending in `.mp3`, `.mp4`, `.wav`, ...) are saved
  as-is unless you explicitly pass `-f mp3`, in which case they are converted
  to MP3 with ffmpeg.
- Anything else (YouTube, Vimeo, X/Twitter, SoundCloud, ...) goes through
  yt-dlp, so every
  [site supported by yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)
  works too.
- **Spotify track links** work too: Spotify's own streams are DRM-protected
  (that's why yt-dlp refuses them), so the track is looked up via Spotify's
  public page metadata, the same song is found on YouTube and downloaded
  there, and the MP3 is tagged with the Spotify title/artist/album and
  cover art. Only single track links (`open.spotify.com/track/...`) are
  supported.
- Paste URLs in quotes to avoid `&` being interpreted by your shell.
- Please only download content you have the right to download. :)
