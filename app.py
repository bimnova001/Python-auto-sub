import sys
import os
import tempfile
import subprocess
import threading
from pathlib import Path

# Workaround: on Windows ctypes.util.find_library('c') can return None -> causes ctypes.CDLL(None) in some whisper builds.
import ctypes.util
_orig_find_library = ctypes.util.find_library
def _patched_find_library(name):
    res = _orig_find_library(name)
    if res is None and name == "c":
        return "msvcrt"  # use the MS C runtime on Windows
    return res
ctypes.util.find_library = _patched_find_library

# robust whisper import: try common package names and verify load_model exists
def import_whisper_module():
    try:
        import whisper as _w
    except Exception:
        _w = None
    if _w and hasattr(_w, "load_model"):
        return _w
    try:
        import openai_whisper as _w2
    except Exception:
        _w2 = None
    if _w2 and hasattr(_w2, "load_model"):
        return _w2
    return None

whisper = import_whisper_module()

# moviepy import
try:
    from moviepy.editor import VideoFileClip
except Exception:
    VideoFileClip = None

from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QComboBox, QFileDialog, QSpinBox, QLineEdit, QProgressBar, QMessageBox
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtCore import Qt, QUrl, Signal

def ensure_ffmpeg_available():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        return True
    except Exception:
        return False

def write_srt(segments, out_path):
    with open(out_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            start = seg["start"]
            end = seg["end"]
            text = seg["text"].strip()
            def fmt(s):
                ms = int((s - int(s)) * 1000)
                hh = int(s // 3600)
                mm = int((s % 3600) // 60)
                ss = int(s % 60)
                return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"
            if text:
                f.write(f"{i}\n{fmt(start)} --> {fmt(end)}\n{text}\n\n")

def burn_subs_with_ffmpeg(in_video, srt_file, out_video, font_size=24, font_color="FFFFFF", position="bottom"):
    # font_color: RRGGBB -> ASS uses BGR + &H
    bgr = f"{font_color[4:6]}{font_color[2:4]}{font_color[0:2]}"
    alignment = "2" if position == "bottom" else "8"
    force = f"Fontsize={font_size},PrimaryColour=&H00{bgr}&,Alignment={alignment}"

    # แปลง path ให้ FFmpeg-friendly
    srt_file_escaped = srt_file.replace("\\", "/").replace(":", "\\:").replace("'", r"\'")
    in_video_escaped = in_video.replace("\\", "/")
    out_video_escaped = out_video.replace("\\", "/")

    cmd = [
        "ffmpeg", "-y", "-i", in_video_escaped, "-vf",
        f"subtitles='{srt_file_escaped}':force_style='{force}'",
        "-c:a", "copy", out_video_escaped
    ]
    subprocess.run(cmd, check=True)


class AutoSubApp(QWidget):
    status_changed = Signal(str)
    progress_changed = Signal(int)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Auto Sub Editor - Prototype")
        self.resize(900, 600)
        self.video_path = None
        self.output_path = str(Path.cwd() / "output_hardsub.mp4")

        # UI
        layout = QVBoxLayout()

        # File selection
        h1 = QHBoxLayout()
        self.lbl_file = QLabel("No video selected")
        btn_open = QPushButton("Open Video")
        btn_open.clicked.connect(self.open_video)
        h1.addWidget(self.lbl_file)
        h1.addWidget(btn_open)
        layout.addLayout(h1)

        # language + model
        h2 = QHBoxLayout()
        h2.addWidget(QLabel("Language (e.g. 'th' or 'en'):"))
        self.lang_edit = QLineEdit("th")
        h2.addWidget(self.lang_edit)
        h2.addWidget(QLabel("Whisper model:"))
        self.model_combo = QComboBox()
        self.model_combo.addItems(["small", "medium", "base"])  # pick defaults; user may change
        h2.addWidget(self.model_combo)
        layout.addLayout(h2)

        # style controls
        h3 = QHBoxLayout()
        h3.addWidget(QLabel("Font size:"))
        self.font_size = QSpinBox()
        self.font_size.setRange(10, 200)
        self.font_size.setValue(28)
        h3.addWidget(self.font_size)
        h3.addWidget(QLabel("Font color (RRGGBB):"))
        self.color_edit = QLineEdit("FFFFFF")
        h3.addWidget(self.color_edit)
        h3.addWidget(QLabel("Position:"))
        self.pos_combo = QComboBox()
        self.pos_combo.addItems(["bottom", "top"])
        h3.addWidget(self.pos_combo)
        layout.addLayout(h3)

        # actions
        h4 = QHBoxLayout()
        self.btn_start = QPushButton("Start Auto-Sub")
        self.btn_start.clicked.connect(self.start_pipeline)
        self.btn_preview = QPushButton("Preview Output")
        self.btn_preview.clicked.connect(self.preview_output)
        self.btn_export = QPushButton("Export (Save As...)")
        self.btn_export.clicked.connect(self.export_as)
        h4.addWidget(self.btn_start)
        h4.addWidget(self.btn_preview)
        h4.addWidget(self.btn_export)
        layout.addLayout(h4)

        # progress / status
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.status = QLabel("Ready")
        # connect signals to UI updates (safe from worker threads)
        self.status_changed.connect(self.status.setText)
        self.progress_changed.connect(self.progress.setValue)
        layout.addWidget(self.progress)
        layout.addWidget(self.status)

        # Embedded video preview (simple)
        self.video_widget = QVideoWidget()
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setVideoOutput(self.video_widget)
        self.player.setAudioOutput(self.audio_output)
        layout.addWidget(self.video_widget, stretch=1)

        self.setLayout(layout)

    def open_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select video", str(Path.home()), "Video Files (*.mp4 *.mov *.mkv *.avi)")
        if path:
            self.video_path = path
            self.lbl_file.setText(os.path.basename(path))
            self.status.setText("Video selected")

    def preview_output(self):
        if not os.path.exists(self.output_path):
            QMessageBox.warning(self, "No output", "Output video not found. Run Auto-Sub first.")
            return
        url = QUrl.fromLocalFile(os.path.abspath(self.output_path))
        self.player.setSource(url)
        self.player.play()
        self.status.setText("Playing output")

    def export_as(self):
        if not os.path.exists(self.output_path):
            QMessageBox.warning(self, "No output", "Output video not found. Run Auto-Sub first.")
            return
        dst, _ = QFileDialog.getSaveFileName(self, "Export video as", str(Path.home() / "output_hardsub.mp4"), "MP4 Files (*.mp4)")
        if dst:
            try:
                import shutil
                shutil.copy2(self.output_path, dst)
                QMessageBox.information(self, "Saved", f"Saved to {dst}")
            except Exception as e:
                QMessageBox.critical(self, "Error", str(e))

    def start_pipeline(self):
        if not self.video_path:
            QMessageBox.warning(self, "No video", "Please select a video first.")
            return
        if whisper is None:
            QMessageBox.critical(self, "Missing dependency", "whisper or moviepy not available. Install requirements.")
            return
        if not ensure_ffmpeg_available():
            QMessageBox.critical(self, "ffmpeg missing", "ffmpeg not found in PATH.")
            return

        self.btn_start.setEnabled(False)
        self.status.setText("Starting...")
        thread = threading.Thread(target=self._pipeline_worker, daemon=True)
        thread.start()

    def _pipeline_worker(self):
        try:
            tmpdir = tempfile.mkdtemp(prefix="autosub_")
            audio_path = os.path.join(tmpdir, "audio.wav")
            self._set_status("Extracting audio...")
            self._set_progress(5)
            # extract audio
            clip = VideoFileClip(self.video_path)
            clip.audio.write_audiofile(audio_path, logger=None)

            # load whisper model
            model_name = self.model_combo.currentText()
            self._set_status(f"Loading model '{model_name}'...")
            self._set_progress(15)

            if whisper is None:
                raise RuntimeError(
                    "Whisper library not available or wrong package installed. "
                    "Install the official package with:\n"
                    "  pip uninstall whisper\n"
                    "  pip install -U openai-whisper"
                )

            model = whisper.load_model(model_name)

            lang = self.lang_edit.text().strip() or None
            self._set_status("Transcribing (this may take a while)...")
            self._set_progress(30)
            # transcribe with timestamps
            result = model.transcribe(audio_path, language=lang, task="transcribe")
            segments = result.get("segments", [])
            self._set_progress(70)

            # write srt
            srt_path = os.path.join(tmpdir, "out.srt")
            self._set_status("Writing subtitle file...")
            write_srt(segments, srt_path)
            self._set_progress(80)

            # burn subs
            self._set_status("Burning subtitles into video (ffmpeg)...")
            font_size = int(self.font_size.value())
            color = self.color_edit.text().strip() or "FFFFFF"
            position = self.pos_combo.currentText()
            out_video = os.path.abspath(self.output_path)
            burn_subs_with_ffmpeg(self.video_path, srt_path, out_video, font_size=font_size, font_color=color, position=position)
            self._set_progress(100)
            self._set_status(f"Done. Output: {out_video}")
        except Exception as e:
            self._set_status("Error: " + str(e))
        finally:
            self.btn_start.setEnabled(True)

    def _set_status(self, text):
        # emit signal so UI updates happen on the main thread
        try:
            self.status_changed.emit(text)
        except Exception:
            # fallback (shouldn't be needed)
            self.status.setText(text)

    def _set_progress(self, v):
        try:
            self.progress_changed.emit(int(v))
        except Exception:
            self.progress.setValue(int(v))

if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = AutoSubApp()
    w.show()
    sys.exit(app.exec())