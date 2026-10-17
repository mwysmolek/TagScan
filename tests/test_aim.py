"""Aim box geometry and the stability judgement, no Qt."""
import numpy as np

from tagscan.app.aim import Box, Stability, box_rect, crop_box


def test_box_rect_shape_and_clamping():
    b = box_rect(1920, 1440)
    assert (b.w, b.h) == (1152, 501) and b.x == 384 and b.y == round((1440 - 501) / 2)
    assert box_rect(1920, 1440, zoom=2.0).w == 576
    assert box_rect(1920, 1440, zoom=3.0, aspect=1.0).h == 384
    # a centre near the edge keeps the box inside the frame
    b = box_rect(1920, 1440, zoom=2.0, center=(0.02, 0.98))
    assert b.x == 0 and b.y + b.h == 1440
    # tiny frames never produce a zero-size box
    b = box_rect(100, 60, zoom=3.0)
    assert b.w >= 64 and b.h >= 32 and b.x >= 0 and b.y >= 0 and b.x + b.w <= 100 and b.y + b.h <= 60
    assert len(b.quad()) == 4 and b.as_dict() == {"x": b.x, "y": b.y, "w": b.w, "h": b.h}


def test_crop_box_with_margin_and_origin():
    frame = np.arange(200 * 300 * 3, dtype=np.uint8).reshape(200, 300, 3)
    box = Box(100, 50, 100, 40)
    crop, (x0, y0) = crop_box(frame, box, margin=0.1)
    assert (x0, y0) == (90, 46) and crop.shape == (48, 120, 3)
    assert np.array_equal(crop, frame[46:94, 90:210]) and crop.flags["C_CONTIGUOUS"]
    crop, (x0, y0) = crop_box(frame, Box(0, 0, 300, 200), margin=0.2)
    assert (x0, y0) == (0, 0) and crop.shape == frame.shape


def _scene(seed, shift=0):
    rng = np.random.default_rng(seed)
    img = np.full((120, 260, 3), 200, np.uint8)
    img[30:90, 40 + shift:220 + shift] = 20                      # a dark label
    img[45:55, 60 + shift:200 + shift] = 240                     # a bright text line
    noise = rng.integers(0, 4, img.shape, np.uint8)
    return np.clip(img.astype(int) + noise, 0, 255).astype(np.uint8)


def test_stability_still_content_sharp_changed():
    st = Stability(still_frames=2)
    s1 = st.update(_scene(1))
    assert not s1.still and s1.content and s1.sharp and s1.changed
    st.update(_scene(2))
    s3 = st.update(_scene(3))
    assert s3.still and s3.ready
    st.mark_read()
    s4 = st.update(_scene(4))
    assert s4.still and not s4.changed                           # same scene as at the read
    s5 = st.update(_scene(5, shift=40))                          # the label moved
    assert not s5.still and s5.changed
    # a blank wall: content False, never ready
    blank = Stability()
    for _ in range(4):
        b = blank.update(np.full((120, 260, 3), 180, np.uint8))
    assert b.still and not b.content and not b.ready
    # a blurred scene is not sharp
    import cv2
    blur = Stability()
    for _ in range(3):
        r = blur.update(cv2.GaussianBlur(_scene(7), (31, 31), 0))
    assert r.still and r.content and not r.sharp
    assert Stability().update(np.zeros((0, 0, 3), np.uint8)).ready is False
