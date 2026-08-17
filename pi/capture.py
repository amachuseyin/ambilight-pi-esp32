#!/usr/bin/env python3
"""
Ambilight camera capture -> WebSocket streamer.

This is the active capture script for this repo. It supports:
- PiCamera2 or OpenCV capture
- perspective correction from calibrated TV corners
- deterministic LED perimeter ordering
- RGB output by default for browser / LED consumers
- optional preview frame streaming for calibration pages
"""

import argparse
import asyncio
import base64
import contextlib
import colorsys
import json
import os
from pathlib import Path
import struct
import time

import cv2
import numpy as np
import websockets


# --- DEFAULT CONFIGURATION ---
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 960
DEFAULT_FPS = 30.0

# TV layout: Left (47) + Top (71) + Right (47) + Bottom (71) = 236 zones.
DEFAULT_ZONES_TOP = 71
DEFAULT_ZONES_BOTTOM = 71
DEFAULT_ZONES_LEFT = 47
DEFAULT_ZONES_RIGHT = 47

DEFAULT_CROP_TOP = 0
DEFAULT_CROP_BOTTOM = 0
DEFAULT_CROP_LEFT = 0
DEFAULT_CROP_RIGHT = 0

DEFAULT_SERVER_URI = "ws://localhost:8765"
DEFAULT_SAMPLE_DEPTH = 0.06
DEFAULT_SAMPLE_INSET = 0.0
DEFAULT_RED_GAIN = 0.94
DEFAULT_GREEN_GAIN = 1.10
DEFAULT_BLUE_GAIN = 0.78
DEFAULT_SATURATION = 1.12
DEFAULT_COLOR_MATRIX = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

# Older working scripts in this repo used this physical strip order:
# bottom-left start -> left side upward -> top L->R -> right downward -> bottom R->L.
# capture7 had drifted to top/right/bottom/left, which shifts the whole output
# by roughly one side (~47 LEDs on this layout).
DEFAULT_LED_ORDER = "left-top-right-bottom"


def parse_corners(s):
    try:
        vals = [float(x.strip()) for x in s.split(",")]
        if len(vals) != 8:
            raise ValueError
        return np.array(
            [
                [vals[0], vals[1]],
                [vals[2], vals[3]],
                [vals[4], vals[5]],
                [vals[6], vals[7]],
            ],
            dtype=np.float32,
        )
    except Exception as exc:
        raise argparse.ArgumentTypeError(
            "Corners must be 8 comma-separated numbers: "
            "tl_x,tl_y,tr_x,tr_y,br_x,br_y,bl_x,bl_y"
        ) from exc


def parse_color_matrix(s):
    try:
        if isinstance(s, (list, tuple)):
            vals = [float(x) for x in s]
        else:
            vals = [float(x.strip()) for x in s.replace(",", " ").split()]
        if len(vals) != 9:
            raise ValueError
        return tuple(vals)
    except Exception as exc:
        raise argparse.ArgumentTypeError(
            "color matrix must be 9 numbers, row-major: rr rg rb gr gg gb br bg bb"
        ) from exc


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def clamp_u8(value):
    return int(max(0, min(255, round(value))))


CHANNEL_ORDERS = ("rgb", "rbg", "grb", "gbr", "brg", "bgr")


def apply_saturation(r, g, b, saturation):
    if saturation == 1.0:
        return r, g, b

    # Preserve perceived brightness while moving channels away from gray.
    luma = (0.2126 * r) + (0.7152 * g) + (0.0722 * b)
    r = luma + (r - luma) * saturation
    g = luma + (g - luma) * saturation
    b = luma + (b - luma) * saturation
    return r, g, b


def apply_color_matrix(r, g, b, color_matrix):
    m = color_matrix
    return (
        (m[0] * r) + (m[1] * g) + (m[2] * b),
        (m[3] * r) + (m[4] * g) + (m[5] * b),
        (m[6] * r) + (m[7] * g) + (m[8] * b),
    )


def avg_raw_rgb(frame):
    if frame is None or frame.size == 0:
        return (0.0, 0.0, 0.0)
    h, w = frame.shape[:2]
    y0, y1 = int(h * 0.20), int(h * 0.80)
    x0, x1 = int(w * 0.20), int(w * 0.80)
    roi = frame[y0:y1, x0:x1]
    b, g, r = cv2.mean(roi)[:3]
    return (float(r), float(g), float(b))


def avg_rect_rgb(frame, rect):
    if frame is None or frame.size == 0:
        return (0.0, 0.0, 0.0)
    h, w = frame.shape[:2]
    x = max(0.0, min(1.0, float(rect.get("x", 0.0))))
    y = max(0.0, min(1.0, float(rect.get("y", 0.0))))
    rw = max(0.0, min(1.0, float(rect.get("w", 0.0))))
    rh = max(0.0, min(1.0, float(rect.get("h", 0.0))))
    x0 = int(x * w)
    y0 = int(y * h)
    x1 = int(min(1.0, x + rw) * w)
    y1 = int(min(1.0, y + rh) * h)
    if x1 <= x0 or y1 <= y0:
        return (0.0, 0.0, 0.0)
    roi = frame[y0:y1, x0:x1]
    b, g, r = cv2.mean(roi)[:3]
    return (float(r), float(g), float(b))


def avg_color_bgr(roi, black_level, red_gain, green_gain, blue_gain, saturation, color_matrix):
    if roi is None or roi.size == 0:
        return (0, 0, 0)

    b, g, r = cv2.mean(roi)[:3]

    r, g, b = apply_color_matrix(r, g, b, color_matrix)
    r = min(255.0, r * red_gain)
    g = min(255.0, g * green_gain)
    b = min(255.0, b * blue_gain)
    r, g, b = apply_saturation(r, g, b, saturation)

    # Use brightest channel after correction. This avoids gray LCD bleed keeping
    # LEDs on when the screen area is effectively black.
    if max(r, g, b) < black_level:
        return (0, 0, 0)

    return (clamp_u8(b), clamp_u8(g), clamp_u8(r))


def edge_rects(width, height, top_z, bottom_z, left_z, right_z, sample_depth, sample_inset):
    depth_v = max(1, int(height * sample_depth))
    depth_h = max(1, int(width * sample_depth))
    inset_v = max(0, int(height * sample_inset))
    inset_h = max(0, int(width * sample_inset))

    rects = {
        "left": [],
        "top": [],
        "right": [],
        "bottom": [],
    }

    # LEFT edge, bottom -> top.
    seg_h = height / left_z if left_z else 0
    for i in range(left_z):
        y1 = int(height - i * seg_h)
        y0 = int(height - (i + 1) * seg_h)
        x0 = min(width - 1, inset_h)
        x1 = min(width, x0 + depth_h)
        rects["left"].append((x0, max(0, y0), max(x0 + 1, x1), max(y0 + 1, y1)))

    # TOP edge, left -> right.
    seg_w = width / top_z if top_z else 0
    for i in range(top_z):
        x0 = int(i * seg_w)
        x1 = int((i + 1) * seg_w)
        y0 = min(height - 1, inset_v)
        y1 = min(height, y0 + depth_v)
        rects["top"].append((x0, y0, max(x0 + 1, x1), max(y0 + 1, y1)))

    # RIGHT edge, top -> bottom.
    seg_h = height / right_z if right_z else 0
    for i in range(right_z):
        y0 = int(i * seg_h)
        y1 = int((i + 1) * seg_h)
        x2 = max(1, width - inset_h)
        x1 = max(0, x2 - depth_h)
        rects["right"].append((x1, y0, x2, max(y0 + 1, y1)))

    # BOTTOM edge, right -> left.
    seg_w = width / bottom_z if bottom_z else 0
    for i in range(bottom_z):
        x1 = int(width - i * seg_w)
        x0 = int(width - (i + 1) * seg_w)
        y2 = max(1, height - inset_v)
        y1 = max(0, y2 - depth_v)
        rects["bottom"].append((max(0, x0), y1, max(x0 + 1, x1), y2))

    return rects


def ordered_zone_rects(width, height, top_z, bottom_z, left_z, right_z, sample_depth, sample_inset, led_order):
    rects = edge_rects(width, height, top_z, bottom_z, left_z, right_z, sample_depth, sample_inset)
    zones = []
    for edge in led_order.split("-"):
        zones.extend(rects[edge])
    return zones


def color_from_bgr(bgr, color_order):
    b, g, r = bgr
    channels = {"r": r, "g": g, "b": b}
    return [channels[name] for name in color_order]


def apply_led_offset(colors, offset):
    if not colors or offset == 0:
        return colors
    shift = offset % len(colors)
    return colors[shift:] + colors[:shift]


def smooth_led_colors(args, colors):
    if not args.smoothing_enabled:
        args._smoothed_colors = None
        return colors
    current = np.asarray(colors, dtype=np.float32)
    previous = getattr(args, "_smoothed_colors", None)
    if previous is None or previous.shape != current.shape:
        args._smoothed_colors = current
        return colors

    attack = max(0.0, min(1.0, float(args.smoothing_attack)))
    decay = max(0.0, min(1.0, float(args.smoothing_decay)))
    threshold = max(0.0, float(args.smoothing_threshold))
    delta = np.max(np.abs(current - previous), axis=1, keepdims=True)
    alpha = np.where(delta >= threshold, attack, decay).astype(np.float32)
    smoothed = (previous * (1.0 - alpha)) + (current * alpha)
    args._smoothed_colors = smoothed
    return np.clip(np.rint(smoothed), 0, 255).astype(np.uint8).tolist()


def encode_binary_frame(colors):
    # Binary frame format:
    #   bytes 0..3: "AMB1"
    #   bytes 4..5: LED count, little-endian uint16
    #   bytes 6..N: 3 bytes per LED, same channel order as JSON colors.
    payload = bytearray()
    payload.extend(b"AMB1")
    payload.extend(struct.pack("<H", len(colors)))
    for color in colors:
        payload.extend(clamp_u8(channel) for channel in color[:3])
    return bytes(payload)


def mock_colors(n_total, t, color_order):
    colors = []
    for i in range(n_total):
        hue = ((i / max(1, n_total)) + t * 0.08) % 1.0
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        colors.append(color_from_bgr((int(b * 255), int(g * 255), int(r * 255)), color_order))
    return colors


def make_mock_frame(colors, top, bottom, left, right, color_order, width=320, height=180):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    rects = ordered_zone_rects(
        width,
        height,
        top,
        bottom,
        left,
        right,
        DEFAULT_SAMPLE_DEPTH,
        DEFAULT_SAMPLE_INSET,
        DEFAULT_LED_ORDER,
    )
    for color, (x1, y1, x2, y2) in zip(colors, rects):
        ordered = dict(zip(color_order, color))
        r = ordered["r"]
        g = ordered["g"]
        b = ordered["b"]
        image[y1:y2, x1:x2] = (b, g, r)
    return image


def draw_preview(frame, zones):
    preview = frame.copy()
    for idx, (x1, y1, x2, y2) in enumerate(zones):
        cv2.rectangle(preview, (x1, y1), (x2 - 1, y2 - 1), (0, 255, 0), 1)
        if idx % 10 == 0:
            cv2.putText(
                preview,
                str(idx),
                (max(0, x1 + 2), max(12, y1 + 12)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.35,
                (0, 255, 255),
                1,
                cv2.LINE_AA,
            )
    return preview


def encode_frame_jpeg(frame_bgr, quality):
    h, w = frame_bgr.shape[:2]
    max_width = 960
    if w > max_width:
        scale = max_width / float(w)
        frame_bgr = cv2.resize(
            frame_bgr,
            (max_width, max(1, int(h * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, encoded = cv2.imencode(
        ".jpg", frame_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
    )
    if not ok:
        return None
    return base64.b64encode(encoded.tobytes()).decode("ascii")


def rotate_frame(frame, angle_degrees):
    h, w = frame.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle_degrees, 1.0)
    return cv2.warpAffine(frame, matrix, (w, h))


def crop_frame(frame, crop_top, crop_bottom, crop_left, crop_right):
    h, w = frame.shape[:2]
    y0, y1 = crop_top, h - crop_bottom
    x0, x1 = crop_left, w - crop_right
    if y1 <= y0 or x1 <= x0:
        raise ValueError("Crop values leave nothing left of the frame.")
    return frame[y0:y1, x0:x1]


def crop_black_bars(frame, threshold, margin, args=None):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    rows = np.where(gray.max(axis=1) > threshold)[0]
    if rows.size == 0:
        return frame
    h, w = frame.shape[:2]
    y0, y1 = int(rows[0]), int(rows[-1]) + 1
    pad_y = int((y1 - y0) * margin)
    y0 = max(0, y0 - pad_y)
    y1 = min(h, y1 + pad_y)

    # Letterbox detection should not chase dark scene content. If the active
    # area is implausibly small, keep the previous crop or the full frame.
    if y1 <= y0 or (y1 - y0) < int(h * 0.55):
        return frame

    if args is not None:
        previous = getattr(args, "_blackbar_crop_y", None)
        if previous is None:
            smoothed = (float(y0), float(y1))
        else:
            alpha = 0.12
            prev_y0, prev_y1 = previous
            smoothed = (
                (prev_y0 * (1.0 - alpha)) + (y0 * alpha),
                (prev_y1 * (1.0 - alpha)) + (y1 * alpha),
            )
        args._blackbar_crop_y = smoothed
        y0, y1 = int(round(smoothed[0])), int(round(smoothed[1]))

    return frame[y0:y1, 0:w]


def perspective_matrix(corners, output_width, output_height):
    dst = np.array(
        [
            [0, 0],
            [output_width - 1, 0],
            [output_width - 1, output_height - 1],
            [0, output_height - 1],
        ],
        dtype=np.float32,
    )
    return cv2.getPerspectiveTransform(corners, dst)


def open_camera(args):
    if args.backend == "picamera2":
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            raise RuntimeError("picamera2 python module not found.") from exc

        picam2 = None
        try:
            picam2 = Picamera2()
            frame_us = int(1_000_000 / args.fps)
            config = picam2.create_video_configuration(
                main={"size": (args.width, args.height), "format": "BGR888"},
                buffer_count=2,
                queue=False,
                controls={"FrameDurationLimits": (frame_us, frame_us)},
            )
            picam2.configure(config)
            picam2.start()
            if args.exposure != 0.0:
                try:
                    picam2.set_controls({"ExposureValue": args.exposure})
                except Exception as exc:
                    print(f"Warning: could not apply ExposureValue={args.exposure}: {exc}")
            return picam2
        except Exception:
            if picam2 is not None:
                with contextlib.suppress(Exception):
                    picam2.stop()
                with contextlib.suppress(Exception):
                    picam2.close()
            raise

    cap = cv2.VideoCapture(args.camera_index)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open OpenCV camera index {args.camera_index}.")
    return cap


def read_frame(args, camera):
    if args.backend == "picamera2":
        frame = camera.capture_array()
        if frame is None:
            return None
        # We request BGR888 above. If a different platform returns RGB data,
        # pass --picamera-color-space rgb to convert it back to internal BGR.
        if args.picamera_color_space == "rgb":
            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        return frame

    ok, frame = camera.read()
    return frame if ok else None


def close_camera(args, camera):
    if camera is None:
        return
    if args.backend == "picamera2":
        with contextlib.suppress(Exception):
            camera.stop()
        with contextlib.suppress(Exception):
            camera.close()
    else:
        camera.release()


def process_frame(frame, args, matrix):
    if args.rotate != 0.0:
        frame = rotate_frame(frame, args.rotate)

    if matrix is not None:
        processed = cv2.warpPerspective(frame, matrix, (args.persp_width, args.persp_height))
        if args.blackbar_detect:
            processed = crop_black_bars(processed, args.blackbar_threshold, args.blackbar_margin, args)
            processed = cv2.resize(processed, (args.persp_width, args.persp_height), interpolation=cv2.INTER_AREA)
        return processed

    processed = crop_frame(
        frame,
        args.crop_top,
        args.crop_bottom,
        args.crop_left,
        args.crop_right,
    )
    out_h, out_w = processed.shape[:2]
    if args.blackbar_detect:
        processed = crop_black_bars(processed, args.blackbar_threshold, args.blackbar_margin, args)
        processed = cv2.resize(processed, (out_w, out_h), interpolation=cv2.INTER_AREA)
    return processed


CALIBRATION_FIELDS = {
    "red_gain": float,
    "green_gain": float,
    "blue_gain": float,
    "saturation": float,
    "black_level": float,
    "led_offset": int,
    "color_order": str,
    "color_matrix": parse_color_matrix,
    "blackbar_detect": parse_bool,
    "blackbar_threshold": float,
    "blackbar_margin": float,
    "smoothing_enabled": parse_bool,
    "smoothing_attack": float,
    "smoothing_decay": float,
    "smoothing_threshold": float,
}


def calibration_config(args):
    return {
        "type": "config",
        "top": args.zones_top,
        "bottom": args.zones_bottom,
        "left": args.zones_left,
        "right": args.zones_right,
        "total": args.zones_top + args.zones_bottom + args.zones_left + args.zones_right,
        "led_order": args.led_order,
        "led_offset": args.led_offset,
        "color_order": args.color_order,
        "red_gain": args.red_gain,
        "green_gain": args.green_gain,
        "blue_gain": args.blue_gain,
        "saturation": args.saturation,
        "black_level": args.black_level,
        "color_matrix": list(args.color_matrix),
        "auto_samples": args.auto_samples,
        "sample_depth": args.sample_depth,
        "sample_inset": args.sample_inset,
        "blackbar_detect": args.blackbar_detect,
        "blackbar_threshold": args.blackbar_threshold,
        "blackbar_margin": args.blackbar_margin,
        "smoothing_enabled": args.smoothing_enabled,
        "smoothing_attack": args.smoothing_attack,
        "smoothing_decay": args.smoothing_decay,
        "smoothing_threshold": args.smoothing_threshold,
    }


def calibration_flags(args):
    return (
        f"--color-order {args.color_order} "
        f"--red-gain {args.red_gain:.3f} "
        f"--green-gain {args.green_gain:.3f} "
        f"--blue-gain {args.blue_gain:.3f} "
        f"--saturation {args.saturation:.3f} "
        f"--color-matrix \"{' '.join(f'{v:.6f}' for v in args.color_matrix)}\" "
        f"--black-level {args.black_level:.1f} "
        f"--led-offset {args.led_offset}"
    )


def apply_calibration_update(args, data):
    changed = []
    values = data.get("values", data)
    for field, converter in CALIBRATION_FIELDS.items():
        if field not in values:
            continue
        try:
            value = converter(values[field])
        except (TypeError, ValueError):
            continue
        if field == "color_order" and value not in CHANNEL_ORDERS:
            continue
        if field in {"red_gain", "green_gain", "blue_gain", "saturation", "black_level", "smoothing_attack", "smoothing_decay", "smoothing_threshold"} and value < 0:
            continue
        if field in {"smoothing_attack", "smoothing_decay"} and value > 1:
            continue
        setattr(args, field, value)
        changed.append(field)
    return changed


COLOR_TARGETS = {
    "red": (255.0, 0.0, 0.0),
    "green": (0.0, 255.0, 0.0),
    "blue": (0.0, 0.0, 255.0),
    "white": (255.0, 255.0, 255.0),
    "gray": (128.0, 128.0, 128.0),
    "yellow": (255.0, 255.0, 0.0),
    "cyan": (0.0, 255.0, 255.0),
    "magenta": (255.0, 0.0, 255.0),
    "orange": (255.0, 128.0, 0.0),
    "pink": (255.0, 64.0, 192.0),
    "warm_white": (255.0, 217.0, 170.0),
}


def solve_color_matrix(samples):
    measured = []
    target = []
    for name in sorted(samples):
        sample = samples.get(name)
        if not sample:
            continue
        measured.append(sample["measured"])
        target.append(sample["target"])
    if len(measured) < 3:
        return None

    measured_arr = np.array(measured, dtype=np.float64)
    target_arr = np.array(target, dtype=np.float64)
    solved, _, _, _ = np.linalg.lstsq(measured_arr, target_arr, rcond=None)
    matrix = solved.T.reshape(-1)
    matrix = np.clip(matrix, -3.0, 3.0)
    return tuple(float(v) for v in matrix)


def record_auto_sample(args, name, measured_rgb):
    target = COLOR_TARGETS.get(name)
    if target is None:
        return None
    args.auto_samples[name] = {
        "measured": [float(v) for v in measured_rgb],
        "target": [float(v) for v in target],
    }
    solved = solve_color_matrix(args.auto_samples)
    if solved is not None:
        args.color_matrix = solved
    return solved


def record_patch_sample(args, name, target_rgb, measured_rgb):
    key = f"{name}_{len(args.auto_samples) + 1}"
    args.auto_samples[key] = {
        "label": name,
        "measured": [float(v) for v in measured_rgb],
        "target": [float(v) for v in target_rgb],
    }
    solved = solve_color_matrix(args.auto_samples)
    if solved is not None:
        args.color_matrix = solved
    return key, solved


async def receive_calibration_updates(websocket, args):
    async for message in websocket:
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            continue
        msg_type = data.get("type")
        if msg_type == "calibration":
            changed = apply_calibration_update(args, data)
            if changed:
                print(f"Calibration updated ({', '.join(changed)}): {calibration_flags(args)}")
        elif msg_type == "command" and data.get("command") == "auto_sample":
            name = str(data.get("target", "")).lower()
            if name in COLOR_TARGETS:
                args.pending_auto_sample = name
        elif msg_type == "command" and data.get("command") == "patch_sample":
            rect = data.get("rect")
            target = data.get("target_rgb")
            label = str(data.get("label", "patch")).lower()
            if isinstance(rect, dict) and isinstance(target, list) and len(target) == 3:
                args.pending_patch_sample = {
                    "rect": rect,
                    "target_rgb": [float(v) for v in target],
                    "label": label,
                }


async def run(args):
    n_total = args.zones_top + args.zones_bottom + args.zones_left + args.zones_right
    print(
        f"Zones: top={args.zones_top} bottom={args.zones_bottom} "
        f"left={args.zones_left} right={args.zones_right} total={n_total}"
    )
    print(f"LED order: {args.led_order}; color order: {args.color_order}")

    zone_width = args.persp_width if args.corners is not None else args.width - args.crop_left - args.crop_right
    zone_height = args.persp_height if args.corners is not None else args.height - args.crop_top - args.crop_bottom
    if zone_width <= 0 or zone_height <= 0:
        raise ValueError("Crop/perspective output dimensions must be positive.")

    zones = ordered_zone_rects(
        zone_width,
        zone_height,
        args.zones_top,
        args.zones_bottom,
        args.zones_left,
        args.zones_right,
        args.sample_depth,
        args.sample_inset,
        args.led_order,
    )
    matrix = (
        perspective_matrix(args.corners, args.persp_width, args.persp_height)
        if args.corners is not None
        else None
    )

    camera = None
    receiver_task = None
    if not args.mock:
        camera = open_camera(args)
        print(f"Camera opened via {args.backend} backend.")

    print(f"Connecting to {args.server} ...")
    try:
        async with websockets.connect(args.server) as websocket:
            await websocket.send(json.dumps({"role": "producer"}))
            await websocket.send(json.dumps(calibration_config(args)))
            print("Connected. Streaming...")
            receiver_task = asyncio.create_task(receive_calibration_updates(websocket, args))

            sent_since_report = 0
            last_fps_report = time.time()
            loop_count = 0
            t0 = time.time()

            while True:
                loop_start = time.time()

                if args.mock:
                    colors = mock_colors(n_total, time.time() - t0, args.color_order)
                    processed = make_mock_frame(
                        colors,
                        args.zones_top,
                        args.zones_bottom,
                        args.zones_left,
                        args.zones_right,
                        args.color_order,
                    )
                else:
                    raw = read_frame(args, camera)
                    if raw is None:
                        print("Warning: empty camera frame, skipping.")
                        await asyncio.sleep(0.01)
                        continue
                    processed = process_frame(raw, args, matrix)

                    if args.pending_auto_sample:
                        sample_name = args.pending_auto_sample
                        args.pending_auto_sample = None
                        measured = avg_raw_rgb(processed)
                        solved = record_auto_sample(args, sample_name, measured)
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "sample_result",
                                    "target": sample_name,
                                    "measured": list(measured),
                                    "solved": solved is not None,
                                    "color_matrix": list(args.color_matrix),
                                    "flags": calibration_flags(args),
                                }
                            )
                        )
                        await websocket.send(json.dumps(calibration_config(args)))
                        print(
                            f"Auto sample {sample_name}: measured RGB "
                            f"{measured[0]:.1f},{measured[1]:.1f},{measured[2]:.1f}; "
                            f"solved={solved is not None}"
                        )

                    if args.pending_patch_sample:
                        patch = args.pending_patch_sample
                        args.pending_patch_sample = None
                        measured = avg_rect_rgb(processed, patch["rect"])
                        key, solved = record_patch_sample(
                            args,
                            patch["label"],
                            patch["target_rgb"],
                            measured,
                        )
                        await websocket.send(
                            json.dumps(
                                {
                                    "type": "sample_result",
                                    "target": key,
                                    "label": patch["label"],
                                    "measured": list(measured),
                                    "target_rgb": patch["target_rgb"],
                                    "solved": solved is not None,
                                    "color_matrix": list(args.color_matrix),
                                    "flags": calibration_flags(args),
                                }
                            )
                        )
                        await websocket.send(json.dumps(calibration_config(args)))
                        print(
                            f"Patch sample {key}: measured RGB "
                            f"{measured[0]:.1f},{measured[1]:.1f},{measured[2]:.1f}; "
                            f"target RGB {patch['target_rgb']}; solved={solved is not None}"
                        )

                    colors = []
                    for x1, y1, x2, y2 in zones:
                        bgr = avg_color_bgr(
                            processed[y1:y2, x1:x2],
                            args.black_level,
                            args.red_gain,
                            args.green_gain,
                            args.blue_gain,
                            args.saturation,
                            args.color_matrix,
                        )
                        colors.append(color_from_bgr(bgr, args.color_order))

                colors = apply_led_offset(colors, args.led_offset)
                colors = smooth_led_colors(args, colors)
                payload = {"type": "frame", "colors": colors}

                if args.send_frame and loop_count % args.frame_every == 0:
                    frame_to_send = draw_preview(processed, zones) if args.preview else processed
                    encoded = encode_frame_jpeg(frame_to_send, args.frame_quality)
                    if encoded:
                        # Keep both names for compatibility with the two server/page versions
                        # shown in your terminal history.
                        payload["image"] = encoded
                        payload["frame"] = encoded

                if args.binary_output and not args.send_frame:
                    await websocket.send(encode_binary_frame(colors))
                else:
                    await websocket.send(json.dumps(payload))

                loop_count += 1
                sent_since_report += 1
                now = time.time()
                if now - last_fps_report >= 2.0:
                    print(f"Streaming at ~{sent_since_report / (now - last_fps_report):.1f} fps")
                    await websocket.send(json.dumps(calibration_config(args)))
                    sent_since_report = 0
                    last_fps_report = now

                elapsed = time.time() - loop_start
                target_frame_time = 1.0 / args.fps
                await asyncio.sleep(max(0.0, target_frame_time - elapsed))
    except (ConnectionRefusedError, OSError) as exc:
        print(f"Connection failed: {exc}")
        print("Start server.py first, or pass --server ws://<server-ip>:8765.")
    finally:
        if receiver_task is not None:
            receiver_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver_task
        close_camera(args, camera)
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass


def parse_args():
    p = argparse.ArgumentParser(description="Ambilight camera capture -> WebSocket streamer")
    p.add_argument("--config", type=str, default=None, help="Load settings from JSON config file")
    p.add_argument("--mock", action="store_true", help="Generate fake rainbow data, no camera needed")
    p.add_argument("--preview", action="store_true", help="Send JPEG frames with zone boxes over WebSocket")
    p.add_argument("--backend", choices=["opencv", "picamera2"], default="picamera2")
    p.add_argument("--camera-index", type=int, default=0, help="OpenCV camera index (ignored for picamera2)")
    p.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    p.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    p.add_argument("--fps", type=float, default=DEFAULT_FPS)

    p.add_argument("--zones-top", type=int, default=DEFAULT_ZONES_TOP)
    p.add_argument("--zones-bottom", type=int, default=DEFAULT_ZONES_BOTTOM)
    p.add_argument("--zones-left", type=int, default=DEFAULT_ZONES_LEFT)
    p.add_argument("--zones-right", type=int, default=DEFAULT_ZONES_RIGHT)

    p.add_argument("--crop-top", type=int, default=DEFAULT_CROP_TOP)
    p.add_argument("--crop-bottom", type=int, default=DEFAULT_CROP_BOTTOM)
    p.add_argument("--crop-left", type=int, default=DEFAULT_CROP_LEFT)
    p.add_argument("--crop-right", type=int, default=DEFAULT_CROP_RIGHT)

    p.add_argument("--server", type=str, default=DEFAULT_SERVER_URI, help="WebSocket relay server URI")
    p.add_argument("--exposure", type=float, default=-3.0)
    p.add_argument("--rotate", type=float, default=0.0)
    p.add_argument("--corners", type=parse_corners, default=None)
    p.add_argument("--persp-width", type=int, default=640)
    p.add_argument("--persp-height", type=int, default=360)

    p.add_argument("--sample-depth", type=float, default=DEFAULT_SAMPLE_DEPTH)
    p.add_argument(
        "--sample-inset",
        type=float,
        default=DEFAULT_SAMPLE_INSET,
        help="Move edge sampling inward by this fraction of width/height to avoid black bezels/borders.",
    )
    p.add_argument("--black-level", type=float, default=35.0)
    p.add_argument(
        "--led-order",
        choices=[
            "left-top-right-bottom",
            "top-right-bottom-left",
            "right-bottom-left-top",
            "bottom-left-top-right",
        ],
        default=DEFAULT_LED_ORDER,
        help="Physical strip order. Default matches older working scripts: start bottom-left.",
    )
    p.add_argument(
        "--color-order",
        choices=CHANNEL_ORDERS,
        default="rgb",
        help="Payload channel order. Try grb first if RGB screen colors are channel-swapped on WS281x LEDs.",
    )
    p.add_argument("--red-gain", type=float, default=DEFAULT_RED_GAIN)
    p.add_argument("--green-gain", type=float, default=DEFAULT_GREEN_GAIN)
    p.add_argument("--blue-gain", type=float, default=DEFAULT_BLUE_GAIN)
    p.add_argument("--saturation", type=float, default=DEFAULT_SATURATION)
    p.add_argument("--color-matrix", type=parse_color_matrix, default=DEFAULT_COLOR_MATRIX)
    p.add_argument(
        "--led-offset",
        type=int,
        default=0,
        help="Rotate the final color list by N LEDs for fine physical start-point alignment.",
    )
    p.add_argument(
        "--picamera-color-space",
        choices=["bgr", "rgb"],
        default="bgr",
        help="Color space returned by Picamera2. Default bgr because this script requests BGR888.",
    )

    p.add_argument("--send-frame", dest="send_frame", action="store_true", default=True)
    p.add_argument("--no-frame", dest="send_frame", action="store_false")
    p.add_argument("--frame-quality", type=int, default=45)
    p.add_argument("--frame-every", type=int, default=5)
    p.add_argument("--blackbar-detect", action="store_true", default=False)
    p.add_argument("--blackbar-threshold", type=float, default=22.0)
    p.add_argument("--blackbar-margin", type=float, default=0.02)
    p.add_argument("--smoothing-enabled", action="store_true", default=True)
    p.add_argument("--no-smoothing", dest="smoothing_enabled", action="store_false")
    p.add_argument("--smoothing-attack", type=float, default=0.75)
    p.add_argument("--smoothing-decay", type=float, default=0.28)
    p.add_argument("--smoothing-threshold", type=float, default=18.0)
    p.add_argument("--reconnect-delay", type=float, default=3.0)
    p.add_argument(
        "--binary-output",
        action="store_true",
        default=False,
        help="Send compact binary LED frames instead of JSON. Disable preview/send-frame when using this.",
    )

    args = p.parse_args()
    if args.config:
        load_config_into_args(args, args.config)
    if args.fps <= 0:
        p.error("--fps must be > 0")
    if args.frame_every <= 0:
        p.error("--frame-every must be > 0")
    if not (0.0 < args.sample_depth <= 0.5):
        p.error("--sample-depth must be > 0 and <= 0.5")
    if not (0.0 <= args.sample_inset <= 0.25):
        p.error("--sample-inset must be >= 0 and <= 0.25")
    if args.red_gain < 0 or args.green_gain < 0 or args.blue_gain < 0:
        p.error("--red-gain, --green-gain, and --blue-gain must be >= 0")
    if args.saturation < 0:
        p.error("--saturation must be >= 0")
    if not (0.0 <= args.smoothing_attack <= 1.0):
        p.error("--smoothing-attack must be between 0 and 1")
    if not (0.0 <= args.smoothing_decay <= 1.0):
        p.error("--smoothing-decay must be between 0 and 1")
    args.auto_samples = {}
    args.pending_auto_sample = None
    args.pending_patch_sample = None
    args._smoothed_colors = None
    return args


def load_config_into_args(args, config_path):
    path = Path(config_path)
    data = json.loads(path.read_text())
    field_map = {
        "persp_width": "persp_width",
        "persp_height": "persp_height",
        "zones_top": "zones_top",
        "zones_bottom": "zones_bottom",
        "zones_left": "zones_left",
        "zones_right": "zones_right",
        "crop_top": "crop_top",
        "crop_bottom": "crop_bottom",
        "crop_left": "crop_left",
        "crop_right": "crop_right",
        "send_frame": "send_frame",
        "frame_quality": "frame_quality",
        "frame_every": "frame_every",
        "black_level": "black_level",
        "led_order": "led_order",
        "led_offset": "led_offset",
        "picamera_color_space": "picamera_color_space",
        "color_order": "color_order",
        "red_gain": "red_gain",
        "green_gain": "green_gain",
        "blue_gain": "blue_gain",
        "saturation": "saturation",
        "sample_depth": "sample_depth",
        "sample_inset": "sample_inset",
        "blackbar_detect": "blackbar_detect",
        "blackbar_threshold": "blackbar_threshold",
        "blackbar_margin": "blackbar_margin",
        "smoothing_enabled": "smoothing_enabled",
        "smoothing_attack": "smoothing_attack",
        "smoothing_decay": "smoothing_decay",
        "smoothing_threshold": "smoothing_threshold",
        "reconnect_delay": "reconnect_delay",
        "binary_output": "binary_output",
    }
    for key, attr in field_map.items():
        if key in data:
            setattr(args, attr, data[key])
    for key in ("backend", "server", "width", "height", "fps", "exposure", "rotate"):
        if key in data:
            setattr(args, key, data[key])
    if "corners" in data and data["corners"] is not None:
        args.corners = parse_corners(",".join(str(v) for v in data["corners"]))
    if "color_matrix" in data:
        args.color_matrix = parse_color_matrix(data["color_matrix"])


if __name__ == "__main__":
    args = parse_args()
    while True:
        try:
            asyncio.run(run(args))
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as exc:
            print(f"Capture crashed: {exc}")
            if "Camera __init__ sequence did not complete" in str(exc):
                print("Fatal camera init failure; exiting process so systemd releases camera resources.")
                os._exit(1)
        if args.mock:
            break
        print(f"Restarting capture in {args.reconnect_delay:.1f}s ...")
        time.sleep(args.reconnect_delay)
