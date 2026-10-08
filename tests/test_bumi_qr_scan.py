"""Real JPEG/QR decoding plus cache, deduplication and lifecycle contracts."""
import importlib.util
import ast
import __future__
from pathlib import Path
import threading
import sys
import time
from types import SimpleNamespace
from unittest import mock

import cv2
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bumi_qr_scan", ROOT / "noetix/bumi/qr_scan.py")
qr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qr)


def qr_image(text):
    matrix = cv2.QRCodeEncoder_create().encode(text)
    return cv2.resize(matrix, (290, 290), interpolation=cv2.INTER_NEAREST)


def jpeg(image):
    ok, encoded = cv2.imencode(".jpg", image)
    assert ok
    return encoded.tobytes()


@pytest.mark.parametrize("text", ["BUMI-001", "展品编号：001", "https://example.com/item/001"])
def test_real_qr_content_and_original_pixel_corners(text):
    image = qr_image(text)
    found = qr.QrDecoder().decode(jpeg(image))
    assert [c["text"] for c in found] == [text]
    assert len(found[0]["corners_px"]) == 4
    assert found[0]["image_width"] == 290
    assert all(0 <= v < 290 for p in found[0]["corners_px"] for v in p)


def test_two_codes_in_one_image():
    image = np.full((390, 740), 255, dtype=np.uint8)
    image[50:340, 30:320] = qr_image("ITEM-A")
    image[50:340, 420:710] = qr_image("ITEM-B")
    assert {c["text"] for c in qr.QrDecoder().decode(jpeg(image))} == {"ITEM-A", "ITEM-B"}


def test_blank_image_has_no_qr():
    assert qr.QrDecoder().decode(jpeg(np.full((480, 640), 255, dtype=np.uint8))) == []


@pytest.mark.parametrize("data", [b"", b"not a jpeg", b"x" * 5_000_001])
def test_invalid_images_are_errors_not_no_qr(data):
    with pytest.raises(ValueError):
        qr.QrDecoder().decode(data)


def test_dedup_continuous_presence_and_reappearance():
    tracker = qr.QrTracker(3)
    code = {"text": "A"}
    assert len(tracker.update([code], 0)) == 1
    for now in [1, 2, 4, 6, 8]:
        assert tracker.update([code], now) == []
    assert tracker.update([], 9) == []
    assert tracker.update([code], 12)[0]["sequence"] == 2


def test_identical_contents_in_two_physical_codes_emit_one_event():
    tracker = qr.QrTracker(3)
    assert len(tracker.update([{"text": "A"}, {"text": "A"}], 0)) == 1


def test_history_is_bounded():
    tracker = qr.QrTracker(300)
    tracker.update([{"text": str(i)} for i in range(1000)], 0)
    assert len(tracker.last_seen) == 256


def plugin(decoder=None, camera=None):
    return qr.QrScanPlugin({}, "test_bumi", mock.Mock(), camera, decoder or qr.QrDecoder())


def frame(data, received=10, sequence=1):
    return {"data": data, "received_monotonic": received, "frame_sequence": sequence,
            "received_at": "2026-10-08T00:00:00+00:00"}


def test_stale_frame_never_reaches_decoder():
    decoder = mock.Mock()
    result = plugin(decoder).process_frame(frame(b"jpeg"), now=14)
    assert result["status"] == "stale" and result["codes"] == []
    decoder.decode.assert_not_called()


def test_decode_error_then_recovery():
    p = plugin()
    assert p.process_frame(frame(b"bad"), now=10)["status"] == "decode_error"
    result = p.process_frame(frame(jpeg(qr_image("RECOVERED")), sequence=2), now=11)
    assert result["status"] == "detected" and result["new_events"][0]["text"] == "RECOVERED"


def test_read_cannot_return_stale_codes_as_fresh():
    p = plugin()
    p._set_result(p.process_frame(frame(jpeg(qr_image("A"))), now=10))
    p._result_set_monotonic -= 4
    result = p.dispatch("read", {})
    assert result["status"] == "stale" and result["codes"] == [] and result["new_events"] == []


@pytest.mark.parametrize("cfg", [{"scan_hz": 0}, {"scan_hz": float("nan")},
                                 {"stale_after_s": -1}, {"rearm_after_s": float("inf")}])
def test_invalid_configuration(cfg):
    with pytest.raises(ValueError):
        qr.QrScanPlugin(cfg, "bumi", mock.Mock(), None)


def test_camera_disabled_and_wrong_input_are_explicit():
    p = plugin()
    assert p.dispatch("start", {})["status"] == "unavailable"
    assert p.dispatch("config", {"input_topic": "/another/camera"})["state"] == "error"


class Cache:
    def __init__(self):
        self.current = frame(jpeg(qr_image("CACHED")), time.monotonic())

    def wait_for_color_frame(self, after_sequence=None, timeout_s=0.2):
        if after_sequence is None:
            return self.current, 1
        time.sleep(min(timeout_s, 0.01))
        return None, 1


def test_worker_reuses_cache_and_can_restart_without_old_codes():
    p = plugin(camera=Cache())
    published = []

    def create_output():
        p._node = mock.Mock()
        p._publisher = SimpleNamespace(publish=lambda msg: published.append(msg.data))
        p._message_type = SimpleNamespace

    p._create_output = create_output
    try:
        p.start()
        first_thread = p._thread
        p.start()
        assert p._thread is first_thread
        deadline = time.monotonic() + 3
        while p.dispatch("read", {})["status"] != "detected" and time.monotonic() < deadline:
            time.sleep(0.02)
        assert p.dispatch("read", {})["codes"][0]["text"] == "CACHED"
        first_session = p.dispatch("info", {})["session_id"]
        p.stop()
        assert not first_thread.is_alive()
        assert p.dispatch("read", {})["codes"] == []
        p.start()
        assert p.dispatch("info", {})["session_id"] != first_session
    finally:
        p.stop()
    assert published


def test_frame_expiring_during_decode_emits_no_event():
    p = plugin(decoder=mock.Mock())
    p.decoder.decode.return_value = [{"text": "OLD"}]
    with mock.patch.object(qr.time, "monotonic", side_effect=[10, 14]):
        result = p.process_frame(frame(b"jpeg"))
    assert result["status"] == "stale" and result["new_events"] == []
    assert p._tracker.sequence == 0


def test_actual_bundle_registers_qr_card_and_routes_read():
    # Run the actual bundle class with SDK/ROS producers replaced, not a copy
    # of its registration code. Only the QR plugin is enabled in this fixture.
    tree = ast.parse((ROOT / "noetix/bumi/main.py").read_text())
    bundle_class = next(n for n in tree.body if isinstance(n, ast.ClassDef)
                        and n.name == "BumiDeviceBundle")
    namespace = {}
    exec(compile(ast.Module(body=[bundle_class], type_ignores=[]), "main.py", "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    with mock.patch.dict(sys.modules, {"qr_scan": qr}):
        bundle = namespace["BumiDeviceBundle"](
            {"plugins": {"qr_scan": {"enabled": True}}}, "bumi", mock.Mock(), None, None)
    assert [t["name"] for t in bundle.get_all_tools()] == ["qr_scan"]
    assert bundle.dispatch("qr_scan", {"action": "read"})["status"] == "idle"
    bundle.start_all()
    assert bundle.dispatch("qr_scan", {"action": "info"})["status"] == "unavailable"
    bundle.stop_all()
