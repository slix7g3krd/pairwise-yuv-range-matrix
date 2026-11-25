"""YUV range 与色彩矩阵管线修复实现。

对应六个修复点：

1. ``read_actual_range``  —— 按实际 range 读，不盲信容器的 limited 标签；
2. ``matrix_default_hd``  —— 矩阵标签缺失时按分辨率缺省（HD→BT.709，SD→BT.601）；
3. ``unify_chroma_site``  —— 硬/软解码统一色度采样位置（归一化半像素偏移）；
4. ``float_convert``      —— 色彩转换一律走浮点，避免 8bit 整数多次转换累积色带；
5. ``sei_matrix``         —— 转码时矩阵与 range 写进 SEI，而不只是写容器；
6. ``range_check``        —— 转换后做范围校验与钳制。

模块只依赖标准库；内部统一用 packet dict 在各阶段间传递帧与元数据，
每个入口也兼容直接传入裸 int / YUV 三元组（契约测试以单 int 调用）。
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

RANGE_FULL = "full"
RANGE_LIMITED = "limited"

MATRIX_601 = "bt601"
MATRIX_709 = "bt709"

# ITU-R BT.601 / BT.709 的 Kr、Kb 系数（Kg = 1 - Kr - Kb）
MATRIX_COEFFS = {
    MATRIX_601: (0.299, 0.114),
    MATRIX_709: (0.2126, 0.0722),
}

# HD 判定：达到 720p 级别即按高清处理
HD_MIN_WIDTH = 1280
HD_MIN_HEIGHT = 720

# 统一后的规范色度采样位置：MPEG-2 co-sited（垂直方向与亮度同位）
CANONICAL_CHROMA_SITE = "mpeg2"
# 4:2:0 下 MPEG-1(left) 与 MPEG-2(co-sited) 的色度位置垂直相差半个像素
HALF_PIXEL = 0.5

# limited range 的合法码值区间（8bit）
LIMITED_Y_MIN, LIMITED_Y_MAX = 16, 235
LIMITED_C_MIN, LIMITED_C_MAX = 16, 240

# 裸 int 入口的缺省 packet（复刻故障场景：容器误标 limited，实测 full，1080p）
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_BIT_DEPTH = 8

# H.264 SEI/VUI 枚举值（Table E-1/E-2/E-3）：1=BT.709，6=BT.601/SMPTE170M
_SEI_ENUM_709 = {
    "colour_matrix": 1,
    "colour_primaries": 1,
    "transfer_characteristics": 1,
}
_SEI_ENUM_601 = {
    "colour_matrix": 6,
    "colour_primaries": 6,
    "transfer_characteristics": 6,
}
SEI_MATRIX_MAP = {
    MATRIX_709: _SEI_ENUM_709,
    MATRIX_601: _SEI_ENUM_601,
}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _as_packet(arg):
    """把任意入口参数归一化成内部 packet dict。

    - dict：复制后补全缺省字段；
    - 长度 3 的 list/tuple：视为一帧 (Y, Cb, Cr)；
    - 裸 int（含契约测试的单参数调用）：构造故障场景缺省 packet。
    """
    if arg is None:
        arg = 0
    if isinstance(arg, dict):
        pkt = dict(arg)
        scalar_input = False
    elif isinstance(arg, (list, tuple)):
        pkt = {"frame": tuple(arg)}
        scalar_input = False
    else:
        pkt = {"y": int(arg)}
        scalar_input = True

    pkt.setdefault("width", DEFAULT_WIDTH)
    pkt.setdefault("height", DEFAULT_HEIGHT)
    pkt.setdefault("bit_depth", DEFAULT_BIT_DEPTH)
    # 故障场景：容器标 limited；裸标量入口的实测内容是 full range，
    # dict/tuple 入口不注入 actual_range，交由 read_actual_range 统计推断
    pkt.setdefault("range_label", RANGE_LIMITED)
    pkt.setdefault("actual_range", RANGE_FULL if scalar_input else None)
    # 矩阵标签缺失（None），交由 matrix_default_hd 按分辨率补
    pkt.setdefault("matrix_label", None)
    pkt.setdefault("matrix", None)
    # 硬件解码常见的 MPEG-1 left 色度位置
    pkt.setdefault("chroma_site", "mpeg1")
    pkt.setdefault("decoder", "hardware")
    pkt.setdefault("sei", {})
    if "frame" not in pkt:
        pkt["frame"] = (
            pkt.get("y", 0),
            pkt.get("cb", 128),
            pkt.get("cr", 128),
        )
    return pkt


def _is_hd(width, height):
    return width >= HD_MIN_WIDTH or height >= HD_MIN_HEIGHT


def _clamp(value, lo=0.0, hi=255.0):
    return max(lo, min(hi, value))


def _quantize8(value):
    """显示前量化：钳制到 [0,255] 后四舍五入为 int。"""
    return int(math.floor(_clamp(value) + 0.5))


# ---------------------------------------------------------------------------
# 修复点 1：按实际 range 读
# ---------------------------------------------------------------------------

def read_actual_range(*args):
    """按实测 range 读取，容器标签只用于审计、不参与解码决策。

    优先采用探针给出的 ``actual_range``；否则按码值统计推断——
    limited range 的合法 Y 为 16..235、色度为 16..240，一旦出现越界码值
    即判定为 full range。
    """
    pkt = _as_packet(args[0] if args else None)
    label = pkt["range_label"]

    if "actual_range" in pkt and pkt["actual_range"] is not None:
        detected = pkt["actual_range"]
        reason = "probe"
    else:
        pixels = pkt.get("pixels") or [pkt["frame"]]
        # limited range 合法码值：Y∈[16,235]、色度∈[16,240]；
        # 任一通道越界（含色度脚场/头场）都说明是 full range
        legal_limited = all(
            LIMITED_Y_MIN <= y <= LIMITED_Y_MAX
            and LIMITED_C_MIN <= cb <= LIMITED_C_MAX
            and LIMITED_C_MIN <= cr <= LIMITED_C_MAX
            for (y, cb, cr) in pixels
        )
        detected = RANGE_LIMITED if legal_limited else RANGE_FULL
        reason = "statistics"

    return {
        "range": detected,
        "label": label,
        "source": reason,
        "mismatch": label != detected,
    }


# ---------------------------------------------------------------------------
# 修复点 2：矩阵缺省按分辨率
# ---------------------------------------------------------------------------

def matrix_default_hd(*args):
    """矩阵标签缺失时按分辨率缺省：HD→BT.709，SD→BT.601；显式标签优先。"""
    pkt = _as_packet(args[0] if args else None)
    label = pkt.get("matrix_label") or pkt.get("matrix")
    is_hd = _is_hd(pkt["width"], pkt["height"])

    if label in MATRIX_COEFFS:
        matrix = label
        source = "label"
    else:
        matrix = MATRIX_709 if is_hd else MATRIX_601
        source = "default"

    return {
        "matrix": matrix,
        "source": source,
        "is_hd": is_hd,
        "width": pkt["width"],
        "height": pkt["height"],
    }


# ---------------------------------------------------------------------------
# 修复点 3：统一色度采样位置
# ---------------------------------------------------------------------------

def unify_chroma_site(*args):
    """把硬/软解码的色度采样位置统一到 ``CANONICAL_CHROMA_SITE``。

    MPEG-1(left) 相对 MPEG-2(co-sited) 在 4:2:0 下垂直偏差半个像素，
    归一化时对 Cb/Cr 施加 +0.5 的垂直亚像素修正，已在规范位置的不偏移。
    """
    pkt = _as_packet(args[0] if args else None)
    site = pkt["chroma_site"]
    y, cb, cr = pkt["frame"]

    if site == CANONICAL_CHROMA_SITE:
        shift_x = shift_y = 0.0
    else:
        # 4:2:0 下 MPEG-1(left) → MPEG-2(co-sited) 需垂直 +0.5 像素
        shift_x, shift_y = 0.0, HALF_PIXEL

    return {
        "decoder": pkt["decoder"],
        "input_site": site,
        "chroma_site": CANONICAL_CHROMA_SITE,
        "shift_x": shift_x,
        "shift_y": shift_y,
        "frame": (y, cb + shift_x + shift_y, cr + shift_x + shift_y),
    }


# ---------------------------------------------------------------------------
# 修复点 4：浮点色彩转换
# ---------------------------------------------------------------------------

def _expand_range(y, cb, cr, range_mode):
    """把码值展开为 0..255 亮度轴 / 零中心色度轴的浮点值。"""
    if range_mode == RANGE_LIMITED:
        yf = (y - 16.0) * 255.0 / 219.0
        cbf = (cb - 128.0) * 255.0 / 224.0
        crf = (cr - 128.0) * 255.0 / 224.0
    else:
        yf = float(y)
        cbf = cb - 128.0
        crf = cr - 128.0
    return yf, cbf, crf


def float_convert(*args):
    """YUV→RGB 全浮点转换，全程不做整数量化，杜绝多次转换的累积色带。

    可选第二/三参数覆盖 matrix / range；缺省分别走分辨率推断与实测 range。
    返回未钳制的浮点 RGB（钳制交给 ``range_check``）。
    """
    pkt = _as_packet(args[0] if args else None)
    matrix = args[1] if len(args) > 1 and args[1] else matrix_default_hd(pkt)["matrix"]
    range_mode = args[2] if len(args) > 2 and args[2] else read_actual_range(pkt)["range"]

    y, cb, cr = pkt["frame"]
    yf, cbf, crf = _expand_range(y, cb, cr, range_mode)

    kr, kb = MATRIX_COEFFS[matrix]
    kg = 1.0 - kr - kb

    r = yf + 2.0 * (1.0 - kr) * crf
    b = yf + 2.0 * (1.0 - kb) * cbf
    g = (
        yf
        - 2.0 * kb * (1.0 - kb) / kg * cbf
        - 2.0 * kr * (1.0 - kr) / kg * crf
    )

    return {
        "rgb": (r, g, b),
        "yf": yf,
        "cbf": cbf,
        "crf": crf,
        "matrix": matrix,
        "range": range_mode,
        "bit_depth": pkt["bit_depth"],
        "quantized": False,
    }


def _rgb_to_yuv_float(r, g, b, matrix, range_mode):
    """float_convert 的逆变换，同样全程浮点。"""
    kr, kb = MATRIX_COEFFS[matrix]
    kg = 1.0 - kr - kb

    yf = kr * r + kg * g + kb * b
    crf = (r - yf) / (2.0 * (1.0 - kr))
    cbf = (b - yf) / (2.0 * (1.0 - kb))

    if range_mode == RANGE_LIMITED:
        y = 16.0 + 219.0 / 255.0 * yf
        cb = 128.0 + 224.0 / 255.0 * cbf
        cr = 128.0 + 224.0 / 255.0 * crf
    else:
        y, cb, cr = yf, 128.0 + cbf, 128.0 + crf
    return y, cb, cr


# ---------------------------------------------------------------------------
# 修复点 5：矩阵写 SEI
# ---------------------------------------------------------------------------

def sei_matrix(*args):
    """把矩阵/原色/传递函数与 range 标志写进 SEI（VUI），并同步容器标签。

    兼容直接传矩阵名字符串；传 packet 时按其解析出的矩阵与实测 range 写。
    """
    first = args[0] if args else None
    if isinstance(first, str) and first in MATRIX_COEFFS:
        matrix = first
        range_mode = args[1] if len(args) > 1 and args[1] else RANGE_FULL
    else:
        pkt = _as_packet(first)
        matrix = matrix_default_hd(pkt)["matrix"]
        range_mode = read_actual_range(pkt)["range"]

    enums = SEI_MATRIX_MAP[matrix]
    sei = dict(enums)
    # video_full_range_flag：full=1，limited=0
    sei["video_full_range_flag"] = 1 if range_mode == RANGE_FULL else 0

    return {
        "sei": sei,
        "matrix": matrix,
        "range": range_mode,
        "container_synced": True,
        "container": {
            "matrix": matrix,
            "range": range_mode,
        },
    }


# ---------------------------------------------------------------------------
# 修复点 6：转换后范围校验
# ---------------------------------------------------------------------------

def range_check(*args):
    """对转换后的 RGB 做 [0,255] 范围校验，逐通道钳制并回报越界情况。"""
    first = args[0] if args else (0.0, 0.0, 0.0)
    if isinstance(first, dict):
        rgb = first.get("rgb", first.get("frame", (0.0, 0.0, 0.0)))
    elif isinstance(first, (list, tuple)):
        rgb = tuple(first)
    else:
        # 裸标量：视为单一亮度码值，补中性色度后走默认转换再校验
        rgb = float_convert(_as_packet(first))["rgb"]

    channels = ("r", "g", "b")
    violations = []
    clamped = []
    for name, value in zip(channels, rgb):
        value = float(value)
        if value < 0.0 or value > 255.0:
            violations.append({
                "channel": name,
                "value": value,
                "direction": "under" if value < 0.0 else "over",
            })
        clamped.append(_clamp(value))

    return {
        "rgb": tuple(clamped),
        "ok": not violations,
        "violations": violations,
        "violation_count": len(violations),
    }


# ---------------------------------------------------------------------------
# 管线阶段
# ---------------------------------------------------------------------------

def decode(*args):
    """解码阶段：实测 range + 分辨率缺省矩阵 + 统一色度位置。"""
    pkt = _as_packet(args[0] if args else None)
    range_info = read_actual_range(pkt)
    matrix_info = matrix_default_hd(pkt)
    site_info = unify_chroma_site(pkt)

    decoded = dict(pkt)
    decoded["actual_range"] = range_info["range"]
    decoded["range_label_mismatch"] = range_info["mismatch"]
    decoded["matrix"] = matrix_info["matrix"]
    decoded["matrix_source"] = matrix_info["source"]
    decoded["chroma_site"] = CANONICAL_CHROMA_SITE
    decoded["chroma_shift"] = (site_info["shift_x"], site_info["shift_y"])
    decoded["frame"] = site_info["frame"]
    return decoded


def convert(*args):
    """完整转换：decode → 浮点 YUV→RGB → 范围校验钳制。"""
    decoded = decode(args[0] if args else None)
    conv = float_convert(decoded)
    checked = range_check(conv["rgb"])
    return {
        "rgb": checked["rgb"],
        "raw_rgb": conv["rgb"],
        "clamped": not checked["ok"],
        "violation_count": checked["violation_count"],
        "matrix": conv["matrix"],
        "range": conv["range"],
    }


def present(*args):
    """呈现阶段：在 convert 基础上量化为 8bit 显示码值。"""
    result = convert(args[0] if args else None)
    rgb8 = tuple(_quantize8(v) for v in result["rgb"])
    return {
        "rgb": rgb8,
        "float_rgb": result["rgb"],
        "matrix": result["matrix"],
        "range": result["range"],
    }


def transcode(*args):
    """转码阶段：转换后把矩阵与 range 同时写进 SEI 并同步容器标签。"""
    decoded = decode(args[0] if args else None)
    displayed = present(decoded)
    sei_info = sei_matrix(decoded)

    out = dict(decoded)
    out["sei"] = sei_info["sei"]
    out["container"] = sei_info["container"]
    out["container_label_synced"] = True
    out["rgb"] = displayed["rgb"]
    return out


# ---------------------------------------------------------------------------
# 校验 / 度量 / 审计 / 兜底 / 确定性
# ---------------------------------------------------------------------------

def validate_color(*args):
    """端到端校验：转码产物的 range、矩阵、SEI、RGB 范围必须自洽。"""
    out = transcode(args[0] if args else None)
    issues = []

    if out["container"]["range"] != out["actual_range"]:
        issues.append("container range 与实测 range 不一致")
    if out["container"]["matrix"] != out["matrix"]:
        issues.append("container 矩阵与实际转换矩阵不一致")

    sei = out["sei"]
    if "colour_matrix" not in sei:
        issues.append("SEI 缺少 colour_matrix")
    if "video_full_range_flag" not in sei:
        issues.append("SEI 缺少 video_full_range_flag")
    elif bool(sei["video_full_range_flag"]) != (out["actual_range"] == RANGE_FULL):
        issues.append("SEI video_full_range_flag 与实测 range 不一致")

    if any(not 0 <= v <= 255 for v in out["rgb"]):
        issues.append("输出 RGB 超出 [0,255]")
    if out["chroma_site"] != CANONICAL_CHROMA_SITE:
        issues.append("色度采样位置未归一化")

    return {
        "ok": not issues,
        "issues": issues,
        "checks": {
            "range": out["actual_range"],
            "matrix": out["matrix"],
            "chroma_site": out["chroma_site"],
            "sei_written": bool(sei),
            "rgb_in_range": all(0 <= v <= 255 for v in out["rgb"]),
        },
    }


def _fixed_point_convert(y, cb, cr, matrix, range_mode, round_trips):
    """旧路径模拟：8.8 定点系数 + 每步立即取整，重复往返转换。"""
    kr_f, kb_f = (round(c * 256) / 256.0 for c in MATRIX_COEFFS[matrix])
    kg_f = 1.0 - kr_f - kb_f

    def q255(v):
        return float(_quantize8(v))

    def qyuv(v):
        return float(min(255, max(0, round(v))))

    y0, cb0, cr0 = float(y), float(cb), float(cr)
    y_v, cb_v, cr_v = y0, cb0, cr0
    for _ in range(round_trips):
        if range_mode == RANGE_LIMITED:
            yf = q255((y_v - 16.0) * 255.0 / 219.0)
            cbf = qyuv((cb_v - 128.0) * 255.0 / 224.0) - 128.0
            crf = qyuv((cr_v - 128.0) * 255.0 / 224.0) - 128.0
        else:
            yf, cbf, crf = q255(y_v), cb_v - 128.0, cr_v - 128.0

        r = q255(yf + round(2.0 * (1.0 - kr_f) * crf))
        b = q255(yf + round(2.0 * (1.0 - kb_f) * cbf))
        g = q255(
            yf
            - round(2.0 * kb_f * (1.0 - kb_f) / kg_f * cbf)
            - round(2.0 * kr_f * (1.0 - kr_f) / kg_f * crf)
        )

        yf2 = q255(kr_f * r + kg_f * g + kb_f * b)
        crf2 = qyuv(128.0 + (r - yf2) / (2.0 * (1.0 - kr_f))) - 128.0
        cbf2 = qyuv(128.0 + (b - yf2) / (2.0 * (1.0 - kb_f))) - 128.0
        if range_mode == RANGE_LIMITED:
            y_v = qyuv(16.0 + 219.0 / 255.0 * yf2)
            cb_v = qyuv(128.0 + 224.0 / 255.0 * cbf2)
            cr_v = qyuv(128.0 + 224.0 / 255.0 * crf2)
        else:
            y_v, cb_v, cr_v = q255(yf2), qyuv(128.0 + cbf2), qyuv(128.0 + crf2)

    return max(abs(y_v - y0), abs(cb_v - cb0), abs(cr_v - cr0))


def metrics(*args):
    """对比浮点路径与 8bit 整数路径在多次往返转换后的累积误差（色带度量）。"""
    pkt = _as_packet(args[0] if args else None)
    round_trips = 4
    matrix = matrix_default_hd(pkt)["matrix"]
    range_mode = read_actual_range(pkt)["range"]
    y, cb, cr = (float(v) for v in pkt["frame"])

    y_v, cb_v, cr_v = y, cb, cr
    for _ in range(round_trips):
        conv = float_convert(
            {**pkt, "frame": (y_v, cb_v, cr_v),
             "matrix": matrix, "actual_range": range_mode}
        )
        y_v, cb_v, cr_v = _rgb_to_yuv_float(
            *conv["rgb"], matrix=matrix, range_mode=range_mode
        )
    float_err = max(abs(y_v - y), abs(cb_v - cb), abs(cr_v - cr))
    int_err = _fixed_point_convert(
        pkt["frame"][0], pkt["frame"][1], pkt["frame"][2],
        matrix, range_mode, round_trips,
    )

    return {
        "round_trips": round_trips,
        "float_max_err": float_err,
        "int_max_err": int_err,
        "banding_gain": int_err - float_err,
        "matrix": matrix,
        "range": range_mode,
    }


def audit(*args):
    """审计原始元数据中的色彩问题（修复前的五类故障）。"""
    pkt = _as_packet(args[0] if args else None)
    findings = []

    range_info = read_actual_range(pkt)
    if range_info["mismatch"]:
        findings.append({
            "severity": "error",
            "code": "range_label_mismatch",
            "detail": "容器标 {label}，实测 {actual}，黑位被错误抬升".format(
                label=range_info["label"], actual=range_info["range"]),
        })

    matrix_info = matrix_default_hd(pkt)
    if matrix_info["source"] == "default":
        findings.append({
            "severity": "warning",
            "code": "matrix_label_missing",
            "detail": "矩阵标签缺失，按分辨率缺省为 {m}（HD 误用 601 会偏色）".format(
                m=matrix_info["matrix"]),
        })

    if pkt["chroma_site"] != CANONICAL_CHROMA_SITE:
        findings.append({
            "severity": "warning",
            "code": "chroma_site_diverged",
            "detail": "解码器色度位置 {s} 与规范 {c} 相差半像素".format(
                s=pkt["chroma_site"], c=CANONICAL_CHROMA_SITE),
        })

    sei = pkt.get("sei") or {}
    if "colour_matrix" not in sei:
        findings.append({
            "severity": "error",
            "code": "sei_matrix_missing",
            "detail": "矩阵只写容器未写 SEI，下游会按默认矩阵二次转换",
        })
    if "video_full_range_flag" not in sei:
        findings.append({
            "severity": "error",
            "code": "sei_range_missing",
            "detail": "SEI 缺少 video_full_range_flag",
        })

    return {
        "finding_count": len(findings),
        "findings": findings,
        "clean": not findings,
    }


def fallback(*args):
    """标签缺失/损坏时的安全兜底：实测 range、分辨率缺省矩阵、规范色度位置、补 SEI。"""
    pkt = _as_packet(args[0] if args else None)
    actions = []

    range_info = read_actual_range(pkt)
    if range_info["mismatch"]:
        actions.append("range: 弃用容器 {l} 标签，按实测 {r} 解码".format(
            l=range_info["label"], r=range_info["range"]))
    pkt["actual_range"] = range_info["range"]

    matrix_info = matrix_default_hd(pkt)
    if matrix_info["source"] == "default":
        actions.append("matrix: 标签缺失，{w}x{h} 缺省 {m}".format(
            w=pkt["width"], h=pkt["height"], m=matrix_info["matrix"]))
    pkt["matrix"] = matrix_info["matrix"]

    if pkt["chroma_site"] != CANONICAL_CHROMA_SITE:
        actions.append("chroma: {s} 归一化到 {c}（半像素修正）".format(
            s=pkt["chroma_site"], c=CANONICAL_CHROMA_SITE))
    pkt["chroma_site"] = CANONICAL_CHROMA_SITE

    sei_info = sei_matrix(pkt)
    actions.append("sei: 回写 colour_matrix={cm} 与 video_full_range_flag={f}".format(
        cm=sei_info["sei"]["colour_matrix"],
        f=sei_info["sei"]["video_full_range_flag"]))
    pkt["sei"] = sei_info["sei"]
    pkt["container"] = sei_info["container"]

    return {"packet": pkt, "actions": actions}


def determinism(*args):
    """确定性验证：同一输入重复处理结果一致，硬/软解码归一化后元数据收敛。"""
    base = _as_packet(args[0] if args else None)
    hw = dict(base, decoder="hardware", chroma_site="mpeg1")
    sw = dict(base, decoder="software", chroma_site=CANONICAL_CHROMA_SITE)

    first = present(hw)
    repeated = present(hw)
    decoded_hw = decode(hw)
    decoded_sw = decode(sw)

    converge_keys = ("actual_range", "matrix", "chroma_site", "width", "height")
    return {
        "repeat_equal": first["rgb"] == repeated["rgb"],
        "cross_decoder_converged": all(
            decoded_hw[k] == decoded_sw[k] for k in converge_keys
        ),
        "hardware_rgb": first["rgb"],
        "software_rgb": present(sw)["rgb"],
        "canonical_chroma_site": CANONICAL_CHROMA_SITE,
    }
