"""One cell of the wall: a camera's video, its name and state, and a border in the state's colour."""

from __future__ import annotations

from kivy.graphics import Color, Line, Rectangle
from kivy.graphics.texture import Texture
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.image import Image
from kivy.uix.label import Label

from camera_wall.camera_reader import CameraReader

# Same colours as the desktop app
COLORS = {
    "grey": (0.5, 0.5, 0.5, 1),
    "green": (0.196, 0.804, 0.196, 1),
    "orange": (1, 0.647, 0, 1),
    "red": (0.863, 0.078, 0.235, 1),
}
TEXT_COLOR = (0.85, 0.85, 0.85, 1)


class CameraPanel(BoxLayout):
    """A camera's video, scaled into the cell (black bars where the shapes differ), with a
    status line under it.

    It draws nothing by itself: the wall calls :meth:`refresh` on every screen update, which
    shows the reader's newest frame and status if they changed.
    """

    def __init__(
        self, camera_name: str, url: str | None, hardware: bool = False, threads: int = 0,
        aspect: float | None = None, **kwargs,
    ):
        super().__init__(orientation="vertical", spacing=dp(3), padding=dp(4), **kwargs)
        self.camera_name = camera_name
        self.reader = (
            CameraReader(camera_name, url, hardware=hardware, threads=threads, aspect=aspect) if url else None
        )
        self._frame_id = 0
        self._texture: Texture | None = None
        self._shown_status: tuple[str, str] | None = None

        # Rounded border around the whole cell, coloured by state
        with self.canvas.before:
            self._border_color = Color(*COLORS["grey"])
            self._border = Line(width=dp(1.2))
        self.bind(pos=self._layout_border, size=self._layout_border)

        # Video on black. Transparent until the first frame: an Image without a texture is white.
        self.video = Image(fit_mode="contain", color=(1, 1, 1, 0))
        with self.video.canvas.before:
            Color(0, 0, 0, 1)
            self._video_bg = Rectangle()
        self.video.bind(pos=self._layout_video, size=self._layout_video)

        self.label = Label(
            size_hint_y=None,
            height=sp(18),
            font_size=sp(12),
            color=TEXT_COLOR,
            shorten=True,
            shorten_from="right",
            halign="center",
            valign="middle",
        )
        self.label.bind(size=lambda label, size: setattr(label, "text_size", size))

        self.add_widget(self.video)
        self.add_widget(self.label)
        self._show_status(("Connecting…", "grey") if self.reader else ("No camera", "grey"))

    # ── Wall interface ───────────────────────────────────────────────────

    def start(self) -> None:
        if self.reader:
            self.reader.set_output_size(*self.video.size)
            self.reader.start()

    def stop(self) -> None:
        """Ask the reader to stop, without waiting."""
        if self.reader:
            self.reader.stop()

    @property
    def connected(self) -> bool:
        return self._shown_status is not None and self._shown_status[1] == "green"

    def refresh(self) -> None:
        """Show the reader's newest status and frame, if they changed. UI thread only."""
        if not self.reader:
            return
        status = self.reader.status
        if status != self._shown_status:
            self._show_status(status)

        frame_id, frame = self.reader.latest_frame()
        if frame is None or frame_id == self._frame_id:
            return
        self._frame_id = frame_id
        height, width = frame.shape[:2]
        if self._texture is None or self._texture.size != (width, height):
            self._texture = Texture.create(size=(width, height), colorfmt="rgb")
            self._texture.flip_vertical()  # frames are top row first, GL textures bottom first
            self.video.texture = self._texture
            self.video.color = (1, 1, 1, 1)
        # blit_buffer needs a flat buffer; reshape(-1) of a contiguous frame is a view, not a copy
        self._texture.blit_buffer(frame.reshape(-1), colorfmt="rgb", bufferfmt="ubyte")
        self.video.canvas.ask_update()

    # ── Internals ────────────────────────────────────────────────────────

    def _show_status(self, status: tuple[str, str]) -> None:
        text, color = status
        self._shown_status = status
        self.label.text = f"{self.camera_name} — {text}"
        self._border_color.rgba = COLORS.get(color, COLORS["grey"])

    def _layout_border(self, *_args) -> None:
        inset = dp(1)
        self._border.rounded_rectangle = (
            self.x + inset,
            self.y + inset,
            max(0, self.width - 2 * inset),
            max(0, self.height - 2 * inset),
            dp(6),
        )

    def _layout_video(self, *_args) -> None:
        self._video_bg.pos = self.video.pos
        self._video_bg.size = self.video.size
        if self.reader:  # decode at the size the cell shows, not the camera's full size
            self.reader.set_output_size(*self.video.size)
