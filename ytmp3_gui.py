#!/usr/bin/env python3
"""
ytmp3 GUI - a small desktop front-end for ytmp3.py.

Paste a YouTube (or other video-site) link, a Spotify track link, or a
direct audio/video file link, pick MP3 or MP4, and download. Built on tkinter
so it ships with Python and packages cleanly into a single Windows .exe with
PyInstaller.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import ytmp3


# ---------------------------------------------------------------------------
# bridge between the worker thread and the yt-dlp callbacks
# ---------------------------------------------------------------------------

class _YtdlpLogger:
    """Forwards yt-dlp log lines into the GUI's message queue."""

    def __init__(self, emit):
        self._emit = emit

    def debug(self, msg):
        # yt-dlp sends normal output through debug(); skip its noisy prefixes
        if msg and not msg.startswith("[debug]"):
            self._emit(msg)

    def info(self, msg):
        self._emit(msg)

    def warning(self, msg):
        self._emit(f"warning: {msg}")

    def error(self, msg):
        self._emit(f"error: {msg}")


class DownloadWorker(threading.Thread):
    """Runs one download in the background and reports back through a queue."""

    def __init__(self, url, media_format, output_dir, quality, msg_queue):
        super().__init__(daemon=True)
        self.url = url
        self.media_format = media_format
        self.output_dir = output_dir
        self.quality = quality
        self.q = msg_queue

    # helpers that push typed events onto the queue -----------------------
    def _log(self, text):
        self.q.put(("log", text))

    def _progress(self, fraction):
        self.q.put(("progress", fraction))

    def _status(self, text):
        self.q.put(("status", text))

    # yt-dlp progress hook ------------------------------------------------
    def _yt_hook(self, d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            if total:
                self._progress(done / total)
            pct = d.get("_percent_str", "").strip()
            speed = (d.get("_speed_str") or "").strip()
            self._status(f"downloading {pct}  {speed}".strip())
        elif status == "finished":
            self._progress(1.0)
            self._status("processing / converting ...")

    def _direct_progress(self, done, total):
        if total:
            self._progress(done / total)
            self._status(f"downloading {ytmp3.human_size(done)} / "
                         f"{ytmp3.human_size(total)}")
        else:
            self._status(f"downloading {ytmp3.human_size(done)}")

    # thread body ---------------------------------------------------------
    def run(self):
        try:
            url = ytmp3.normalize_url(self.url)
            direct = ytmp3.is_direct_media_url(url)
            spotify = ytmp3.is_spotify_url(url)

            if ytmp3.yt_dlp is None and not direct:
                raise RuntimeError("yt-dlp is not available in this build.")

            output_dir = Path(self.output_dir).expanduser().resolve()
            output_dir.mkdir(parents=True, exist_ok=True)

            ffmpeg = ytmp3.find_ffmpeg()
            if self.media_format == "mp3" and ffmpeg is None:
                self._log("warning: ffmpeg not found - MP3 conversion may fail")

            if direct:
                self._log(f"direct media link: {url}")
                saved = ytmp3.download_direct(
                    url, output_dir, on_progress=self._direct_progress)
                if (self.media_format == "mp3"
                        and saved.suffix.lower() != ".mp3" and ffmpeg):
                    self._status("converting to MP3 ...")
                    saved = ytmp3.convert_to_mp3(saved, self.quality, ffmpeg)
                saved_files = [saved]
            elif spotify:
                # Spotify streams are DRM-protected: resolve the track's
                # info, download the same song from YouTube, tag it instead
                saved_files = ytmp3.download_spotify(
                    url, self.media_format, output_dir, self.quality, ffmpeg,
                    progress_hooks=[self._yt_hook],
                    logger=_YtdlpLogger(self._log),
                    on_message=lambda text: (self._log(text),
                                             self._status(text)))
            else:
                self._log(f"downloading with yt-dlp: {url}")
                saved_files = ytmp3.download_site(
                    url, self.media_format, output_dir, self.quality, ffmpeg,
                    progress_hooks=[self._yt_hook],
                    logger=_YtdlpLogger(self._log))
                if not saved_files:
                    raise RuntimeError("yt-dlp finished but produced no file")

            for path in saved_files:
                size = ytmp3.human_size(path.stat().st_size)
                self._log(f"saved: {path}  ({size})")

            self.q.put(("done", [str(p) for p in saved_files]))

        except Exception as exc:  # worker boundary: report any failure to UI
            self.q.put(("error", f"{type(exc).__name__}: {exc}"))


# ---------------------------------------------------------------------------
# the window
# ---------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ytmp3 - YouTube / Spotify / media downloader")
        self.geometry("640x520")
        self.minsize(560, 460)

        self.q: queue.Queue = queue.Queue()
        self.worker: DownloadWorker | None = None
        self.last_files: list[str] = []

        default_out = str((Path.cwd() / "downloads").resolve())
        self.var_url = tk.StringVar()
        self.var_format = tk.StringVar(value="mp3")
        self.var_quality = tk.StringVar(value="192")
        self.var_output = tk.StringVar(value=default_out)
        self.var_status = tk.StringVar(value="ready")

        self._build_ui()
        self.after(100, self._drain_queue)

    # layout --------------------------------------------------------------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}
        self.columnconfigure(0, weight=1)

        frm = ttk.Frame(self)
        frm.grid(row=0, column=0, sticky="ew", **pad)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Link").grid(row=0, column=0, sticky="w")
        url_entry = ttk.Entry(frm, textvariable=self.var_url)
        url_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=(8, 0))
        url_entry.focus()

        # format + quality row
        opts = ttk.Frame(self)
        opts.grid(row=1, column=0, sticky="ew", **pad)

        ttk.Label(opts, text="Format").pack(side="left")
        ttk.Radiobutton(opts, text="MP3 (audio)", value="mp3",
                        variable=self.var_format,
                        command=self._sync_quality).pack(side="left", padx=(8, 0))
        ttk.Radiobutton(opts, text="MP4 (video)", value="mp4",
                        variable=self.var_format,
                        command=self._sync_quality).pack(side="left", padx=(4, 0))

        ttk.Label(opts, text="MP3 quality").pack(side="left", padx=(20, 4))
        self.quality_box = ttk.Combobox(
            opts, textvariable=self.var_quality, width=6, state="readonly",
            values=("128", "192", "320"))
        self.quality_box.pack(side="left")

        # output folder row
        out = ttk.Frame(self)
        out.grid(row=2, column=0, sticky="ew", **pad)
        out.columnconfigure(1, weight=1)
        ttk.Label(out, text="Save to").grid(row=0, column=0, sticky="w")
        ttk.Entry(out, textvariable=self.var_output).grid(
            row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(out, text="Browse...", command=self._choose_folder).grid(
            row=0, column=2)

        # action buttons
        actions = ttk.Frame(self)
        actions.grid(row=3, column=0, sticky="ew", **pad)
        self.btn_download = ttk.Button(
            actions, text="Download", command=self._start_download)
        self.btn_download.pack(side="left")
        self.btn_open = ttk.Button(
            actions, text="Open folder", command=self._open_output)
        self.btn_open.pack(side="left", padx=(8, 0))

        # progress bar
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=1.0)
        self.progress.grid(row=4, column=0, sticky="ew", **pad)

        ttk.Label(self, textvariable=self.var_status, foreground="#555").grid(
            row=5, column=0, sticky="w", padx=10)

        # log output
        self.rowconfigure(6, weight=1)
        log_frame = ttk.Frame(self)
        log_frame.grid(row=6, column=0, sticky="nsew", **pad)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=10, wrap="word", state="disabled",
                           font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        self._sync_quality()

    # small ui actions ----------------------------------------------------
    def _sync_quality(self):
        state = "readonly" if self.var_format.get() == "mp3" else "disabled"
        self.quality_box.configure(state=state)

    def _choose_folder(self):
        chosen = filedialog.askdirectory(
            initialdir=self.var_output.get() or str(Path.cwd()))
        if chosen:
            self.var_output.set(chosen)

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_output(self):
        folder = self.var_output.get().strip()
        if not folder or not Path(folder).exists():
            messagebox.showinfo("ytmp3", "That folder does not exist yet.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.run(["open", folder])
            else:
                subprocess.run(["xdg-open", folder])
        except Exception as exc:
            messagebox.showerror("ytmp3", f"Could not open folder:\n{exc}")

    # download lifecycle --------------------------------------------------
    def _start_download(self):
        if self.worker and self.worker.is_alive():
            return
        url = self.var_url.get().strip()
        if not url:
            messagebox.showwarning("ytmp3", "Please paste a link first.")
            return

        self.last_files = []
        self.progress.configure(value=0)
        self.var_status.set("starting ...")
        self.btn_download.configure(state="disabled", text="Downloading...")
        self._append_log("-" * 60)

        self.worker = DownloadWorker(
            url=url,
            media_format=self.var_format.get(),
            output_dir=self.var_output.get().strip() or "downloads",
            quality=self.var_quality.get(),
            msg_queue=self.q,
        )
        self.worker.start()

    def _finish(self, success, message=""):
        self.btn_download.configure(state="normal", text="Download")
        if success:
            self.progress.configure(value=1.0)
            self.var_status.set("done")
        else:
            self.var_status.set("failed")
            if message:
                messagebox.showerror("ytmp3", message)

    # poll the worker queue on the UI thread ------------------------------
    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "status":
                    self.var_status.set(payload)
                elif kind == "progress":
                    self.progress.configure(value=max(0.0, min(1.0, payload)))
                elif kind == "done":
                    self.last_files = payload
                    self._finish(True)
                elif kind == "error":
                    self._append_log(f"error: {payload}")
                    self._finish(False, payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
