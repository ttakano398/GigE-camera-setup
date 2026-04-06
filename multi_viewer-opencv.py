from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

GST_AVAILABLE = False
GST_IMPORT_ERROR = None

try:
    import gi

    gi.require_version("Gst", "1.0")

    from gi.repository import Gst

    GST_AVAILABLE = True
except Exception as exc:  # pragma: no cover - runtime environment dependent
    Gst = None
    GST_IMPORT_ERROR = exc


logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)


WINDOW_NAME = "GigE Multi Viewer"
SCRIPT_DIR = Path(__file__).resolve().parent

MODE_MAX_WIDTH = 2592
MODE_MAX_HEIGHT = 1944
MODE_MAX_FPS = 22

WINDOW_COLUMNS = 2
PREVIEW_WINDOW_WIDTH = 960
PREVIEW_WINDOW_HEIGHT = 720
WINDOW_GAP = 40

OPEN_DELAY_SEC = 0.25
GST_STATE_TIMEOUT_SEC = 8.0
READ_TIMEOUT_MS = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preview four TIS GigE cameras in max mode using Gst appsink + OpenCV."
    )
    parser.add_argument(
        "--serial",
        "--serials",
        nargs="+",
        required=True,
        metavar="SERIAL",
        help="Exactly four camera serial numbers in display order.",
    )
    args = parser.parse_args()

    if len(args.serial) != 4:
        parser.error("--serial requires exactly 4 values.")

    return args


def make_window_name(index: int, serial: str) -> str:
    return f"{WINDOW_NAME} [{index + 1}] {serial}"


def summarize_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:120]


def make_placeholder(serial: str, status: str, detail: str = "") -> np.ndarray:
    frame = np.zeros((PREVIEW_WINDOW_HEIGHT, PREVIEW_WINDOW_WIDTH, 3), dtype=np.uint8)
    lines = [
        (f"SERIAL: {serial}", (0, 255, 255), 1.05),
        (status, (0, 0, 255), 1.0),
    ]

    if detail:
        lines.append((detail[:90], (200, 200, 200), 0.75))

    y = 60
    for text, color, scale in lines:
        cv2.putText(
            frame,
            text,
            (30, y),
            cv2.FONT_HERSHEY_SIMPLEX,
            scale,
            color,
            2,
            cv2.LINE_AA,
        )
        y += 60

    return frame


def annotate_frame(frame: np.ndarray, serial: str) -> np.ndarray:
    annotated = frame.copy()
    height, width = annotated.shape[:2]

    cv2.putText(
        annotated,
        f"SERIAL: {serial}",
        (20, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        1.0,
        (0, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        annotated,
        f"MAX {width}x{height}",
        (20, 80),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.9,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return annotated


@dataclass
class CameraStream:
    serial: str
    index: int
    window_name: str = field(init=False)
    backend_label: Optional[str] = None
    pipeline_description: Optional[str] = None
    pipeline: Optional[Gst.Pipeline] = None
    appsink: Optional[Gst.Element] = None
    bus: Optional[Gst.Bus] = None
    last_frame: np.ndarray = field(init=False)
    error_message: str = ""

    def __post_init__(self) -> None:
        self.window_name = make_window_name(self.index, self.serial)
        self.last_frame = make_placeholder(self.serial, "INITIALIZING")

    def _sink_name(self) -> str:
        return f"sink_{self.index}"

    def pipeline_candidates(self) -> list[tuple[str, str]]:
        sink_name = self._sink_name()
        caps = (
            f"video/x-bayer,format=grbg,width={MODE_MAX_WIDTH},"
            f"height={MODE_MAX_HEIGHT},framerate={MODE_MAX_FPS}/1"
        )
        sink = (
            f'appsink name={sink_name} sync=false drop=true max-buffers=1 emit-signals=false'
        )

        return [
            (
                "tcamsrc/aravis",
                (
                    f'tcamsrc serial="{self.serial}" type=aravis ! '
                    f"{caps} ! "
                    "bayer2rgb ! videoconvert ! video/x-raw,format=BGR ! "
                    f"{sink}"
                ),
            ),
            (
                "tcammainsrc",
                (
                    f'tcammainsrc serial="{self.serial}" ! '
                    f"{caps} ! "
                    "bayer2rgb ! videoconvert ! video/x-raw,format=BGR ! "
                    f"{sink}"
                ),
            ),
        ]

    def _wait_for_playing(self) -> None:
        if self.pipeline is None:
            raise RuntimeError("pipeline is not initialized")

        result, current, pending = self.pipeline.get_state(
            int(GST_STATE_TIMEOUT_SEC * Gst.SECOND)
        )
        if result == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("failed to reach PLAYING state")

        if current != Gst.State.PLAYING and pending != Gst.State.PLAYING:
            raise RuntimeError(
                f"unexpected state current={current.value_nick} pending={pending.value_nick}"
            )

    def _drain_bus(self) -> None:
        if self.bus is None:
            return

        mask = Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.WARNING
        while True:
            message = self.bus.timed_pop_filtered(0, mask)
            if message is None:
                break

            if message.type == Gst.MessageType.WARNING:
                err, debug = message.parse_warning()
                logging.warning("[%s] %s | debug=%s", self.serial, err, debug)
                continue

            if message.type == Gst.MessageType.EOS:
                raise RuntimeError("pipeline reached EOS unexpectedly")

            if message.type == Gst.MessageType.ERROR:
                err, debug = message.parse_error()
                raise RuntimeError(f"{err} | debug={debug}")

    def stop(self) -> None:
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.appsink = None
            self.bus = None

    def start(self) -> bool:
        self.stop()
        errors = []

        for backend_label, description in self.pipeline_candidates():
            self.backend_label = backend_label
            self.pipeline_description = description
            logging.info("[%s] Opening pipeline via %s", self.serial, backend_label)
            logging.info("[%s] %s", self.serial, description)

            try:
                pipeline = Gst.parse_launch(description)
                appsink = pipeline.get_by_name(self._sink_name())
                if appsink is None:
                    raise RuntimeError("appsink was not created")

                self.pipeline = pipeline
                self.appsink = appsink
                self.bus = pipeline.get_bus()

                if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError("failed to set pipeline to PLAYING")

                self._wait_for_playing()
                self._drain_bus()
                self.error_message = ""
                return True
            except Exception as exc:
                error_text = f"{backend_label}: {summarize_error(exc)}"
                logging.error("[%s] %s", self.serial, error_text)
                errors.append(error_text)
                self.stop()

        self.error_message = " | ".join(errors)
        self.last_frame = make_placeholder(
            self.serial, "OPEN FAILED", summarize_error(RuntimeError(self.error_message))
        )
        return False

    def read_frame(self, timeout_ms: int = READ_TIMEOUT_MS) -> Optional[np.ndarray]:
        if self.appsink is None:
            return None

        try:
            self._drain_bus()
            sample = self.appsink.emit("try-pull-sample", timeout_ms * 1_000_000)
            if sample is None:
                return None

            buffer = sample.get_buffer()
            caps = sample.get_caps()
            if buffer is None or caps is None:
                return None

            structure = caps.get_structure(0)
            width = int(structure.get_value("width"))
            height = int(structure.get_value("height"))
            channels = 3

            ok, map_info = buffer.map(Gst.MapFlags.READ)
            if not ok:
                raise RuntimeError("failed to map GStreamer buffer")

            try:
                frame = np.frombuffer(map_info.data, dtype=np.uint8)
                expected = width * height * channels
                if frame.size < expected:
                    raise RuntimeError(
                        f"unexpected frame size expected={expected} actual={frame.size}"
                    )
                return frame[:expected].reshape((height, width, channels)).copy()
            finally:
                buffer.unmap(map_info)
        except Exception as exc:
            self.error_message = summarize_error(exc)
            logging.error("[%s] stream error: %s", self.serial, self.error_message)
            self.last_frame = make_placeholder(self.serial, "STREAM ERROR", self.error_message)
            self.stop()
            return None


@dataclass
class FallbackCamera:
    serial: str
    index: int
    window_name: str = field(init=False)
    backend_label: Optional[str] = None
    pipeline_description: Optional[str] = None
    cap: Optional[cv2.VideoCapture] = None
    last_frame: np.ndarray = field(init=False)
    error_message: str = ""

    def __post_init__(self) -> None:
        self.window_name = make_window_name(self.index, self.serial)
        self.last_frame = make_placeholder(self.serial, "INITIALIZING")

    def pipeline_candidates(self) -> list[tuple[str, str]]:
        caps = (
            f"video/x-bayer,format=grbg,width={MODE_MAX_WIDTH},"
            f"height={MODE_MAX_HEIGHT},framerate={MODE_MAX_FPS}/1"
        )

        return [
            (
                "tcamsrc/aravis",
                (
                    f'tcamsrc serial="{self.serial}" type=aravis ! '
                    f"{caps} ! "
                    "bayer2rgb ! videoconvert ! video/x-raw,format=BGR ! "
                    "appsink sync=false drop=true max-buffers=1"
                ),
            ),
            (
                "tcammainsrc",
                (
                    f'tcammainsrc serial="{self.serial}" ! '
                    f"{caps} ! "
                    "bayer2rgb ! videoconvert ! video/x-raw,format=BGR ! "
                    "appsink sync=false drop=true max-buffers=1"
                ),
            ),
        ]

    def stop(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def start(self) -> bool:
        self.stop()
        errors = []

        for backend_label, description in self.pipeline_candidates():
            self.backend_label = backend_label
            self.pipeline_description = description
            logging.info("[%s] Opening pipeline via %s", self.serial, backend_label)
            logging.info("[%s] %s", self.serial, description)

            cap = cv2.VideoCapture(description, cv2.CAP_GSTREAMER)
            if cap.isOpened():
                self.cap = cap
                self.error_message = ""
                return True

            cap.release()
            error_text = f"{backend_label}: failed to open pipeline"
            logging.error("[%s] %s", self.serial, error_text)
            errors.append(error_text)

        self.error_message = " | ".join(errors)
        self.last_frame = make_placeholder(
            self.serial, "OPEN FAILED", summarize_error(RuntimeError(self.error_message))
        )
        return False

    def read_frame(self) -> Optional[np.ndarray]:
        if self.cap is None:
            return None

        ok, frame = self.cap.read()
        if not ok:
            return None
        return frame


def create_windows(cameras: list[CameraStream]) -> None:
    for camera in cameras:
        cv2.namedWindow(camera.window_name, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(camera.window_name, PREVIEW_WINDOW_WIDTH, PREVIEW_WINDOW_HEIGHT)

        row, col = divmod(camera.index, WINDOW_COLUMNS)
        cv2.moveWindow(
            camera.window_name,
            col * (PREVIEW_WINDOW_WIDTH + WINDOW_GAP),
            row * (PREVIEW_WINDOW_HEIGHT + WINDOW_GAP),
        )


def run_gst_viewer(serials: list[str]) -> None:
    Gst.init(None)
    cameras = [CameraStream(serial=serial, index=index) for index, serial in enumerate(serials)]

    print("script_dir:", SCRIPT_DIR)

    opened_count = 0
    for camera in cameras:
        opened = camera.start()
        print(f"serial={camera.serial}")
        print("pipeline:", camera.pipeline_description)
        print("opened:", opened)
        if not opened and camera.error_message:
            print("error:", camera.error_message)
        if opened:
            opened_count += 1
        time.sleep(OPEN_DELAY_SEC)

    if opened_count == 0:
        raise RuntimeError("failed to open any cameras via GStreamer")

    create_windows(cameras)

    try:
        while True:
            for camera in cameras:
                frame = camera.read_frame()
                if frame is not None:
                    camera.last_frame = annotate_frame(frame, camera.serial)

                cv2.imshow(camera.window_name, camera.last_frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
    finally:
        for camera in cameras:
            camera.stop()
        cv2.destroyAllWindows()


def run_opencv_fallback_viewer(serials: list[str]) -> None:
    print("script_dir:", SCRIPT_DIR)
    print("gst_backend: unavailable")
    print("gst_error:", GST_IMPORT_ERROR)

    cameras = [FallbackCamera(serial=serial, index=index) for index, serial in enumerate(serials)]
    opened_count = 0

    for camera in cameras:
        opened = camera.start()
        print(f"serial={camera.serial}")
        print("pipeline:", camera.pipeline_description)
        print("opened:", opened)
        if not opened and camera.error_message:
            print("error:", camera.error_message)
        if opened:
            opened_count += 1
        time.sleep(OPEN_DELAY_SEC)

    if opened_count == 0:
        raise RuntimeError("failed to open any cameras via OpenCV/GStreamer")

    create_windows(cameras)

    try:
        while True:
            for camera in cameras:
                frame = camera.read_frame()
                if frame is not None:
                    camera.last_frame = annotate_frame(frame, camera.serial)

                cv2.imshow(camera.window_name, camera.last_frame)

            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break
    finally:
        for camera in cameras:
            camera.stop()
        cv2.destroyAllWindows()


def main() -> None:
    args = parse_args()
    serials = args.serial

    if GST_AVAILABLE:
        run_gst_viewer(serials)
        return

    run_opencv_fallback_viewer(serials)


if __name__ == "__main__":
    main()
