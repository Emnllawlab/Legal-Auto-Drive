"""印章视觉检测：定位红色公章/私章区域，供涂改。

中国公文印章绝大多数是红色圆形/椭圆章。这里用经典 CV（HSV 红色掩膜 +
形态学 + 轮廓筛选），纯本地离线、无需模型、轻量——覆盖主流场景。
黑色/特殊印章、套红等疑难样本留给后续视觉模型（YOLO / Qwen-VL）。

输入 RGB ndarray，输出印章 bbox 列表 [(x0,y0,x1,y1), ...]（像素坐标）。
需要 opencv-python + numpy（延迟导入）。
"""


def detect_seals(rgb, min_area_ratio=0.0008, min_area_abs=1500,
                 aspect_lo=0.45, aspect_hi=2.2, pad=4):
    """检测红色印章区域。

    参数都偏保守，宁可多框（涂掉一点背景）也别漏章——漏章=机构信息泄露。
    """
    import cv2
    import numpy as np

    h, w = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    # 红色在 HSV 里跨 0/180 两段
    m1 = cv2.inRange(hsv, (0, 60, 50), (12, 255, 255))
    m2 = cv2.inRange(hsv, (160, 60, 50), (180, 255, 255))
    mask = cv2.bitwise_or(m1, m2)

    # 印章是"圆环+文字"的镂空结构，闭运算把它连成一个整块
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)
    mask = cv2.dilate(mask, k, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_area = max(min_area_abs, min_area_ratio * h * w)

    boxes = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        x, y, bw, bh = cv2.boundingRect(c)
        aspect = bw / bh if bh else 0
        if not (aspect_lo <= aspect <= aspect_hi):     # 章近方/近圆，滤掉红色长条/下划线
            continue
        red_frac = cv2.countNonZero(mask[y:y + bh, x:x + bw]) / (bw * bh)
        if red_frac < 0.25:                             # 框内红色占比太低，多半是误检
            continue
        boxes.append((max(0, x - pad), max(0, y - pad),
                      min(w, x + bw + pad), min(h, y + bh + pad)))
    return _merge_overlaps(boxes)


def _merge_overlaps(boxes):
    """合并重叠/相邻的框，避免同一印章被拆成几块。"""
    boxes = sorted(boxes, key=lambda b: -(b[2] - b[0]) * (b[3] - b[1]))
    merged = []
    for b in boxes:
        placed = False
        for i, m in enumerate(merged):
            if not (b[2] < m[0] or b[0] > m[2] or b[3] < m[1] or b[1] > m[3]):
                merged[i] = (min(b[0], m[0]), min(b[1], m[1]),
                             max(b[2], m[2]), max(b[3], m[3]))
                placed = True
                break
        if not placed:
            merged.append(b)
    return merged
