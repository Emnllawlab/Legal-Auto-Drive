"""OCR 适配层：把扫描件/图片转成"文本行 + 坐标框"，供脱敏定位。

引擎可插拔，统一接口 run(image) -> [(text,(x0,y0,x1,y1)), ...]（按阅读序）：
- RapidOCREngine：PP-OCR 模型的 ONNX 版，纯本地离线、轻；默认。
- PaddleOCREngine：官方 PaddleOCR，可调更大的 server 模型，精度略高但重。
二者同款 PP-OCR 模型，精度差异主要来自模型档位（mobile vs server），非框架。

另提供把管线的字符级 span 映射回像素框的工具：按字符在行内的比例插值出子区域。
"""
import os


class RapidOCREngine:
    """本地 RapidOCR 适配器。run() 返回 [(text, (x0,y0,x1,y1)), ...]，按阅读序。"""

    def __init__(self):
        from rapidocr_onnxruntime import RapidOCR
        self._ocr = RapidOCR()

    def run(self, image):
        out, _ = self._ocr(image)
        lines = []
        for box, txt, _score in (out or []):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            lines.append((txt, (min(xs), min(ys), max(xs), max(ys))))
        return lines


class PaddleOCREngine:
    """官方 PaddleOCR 适配器（需 pip install paddleocr paddlepaddle）。

    兼容 PaddleOCR 2.x 与 3.x（两版 API 不同）。首次使用会联网下载模型。
    lang="ch" 中文；ocr_kwargs 透传给 PaddleOCR（如调模型档位）。
    """

    def __init__(self, lang="ch", **ocr_kwargs):
        # 跳过 Paddle v3 的连通性预检（LibreSSL 下常误判"无可用模型源"），
        # 并把模型源指向 ModelScope（国内可达）。模型下载一次后存 ~/.paddlex，
        # 之后纯本地离线可用，不再需要网络。可用 env 覆盖。
        os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
        os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "modelscope")
        import paddleocr
        from paddleocr import PaddleOCR
        major = int(getattr(paddleocr, "__version__", "2").split(".")[0])
        self._v3 = major >= 3
        if self._v3:
            params = dict(lang=lang, use_doc_orientation_classify=False,
                          use_doc_unwarping=False, use_textline_orientation=True)
        else:
            params = dict(lang=lang, use_angle_cls=True, show_log=False)
        params.update(ocr_kwargs)
        self._ocr = PaddleOCR(**params)

    @staticmethod
    def _bbox(poly):
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        return (float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)))

    def run(self, image):
        import numpy as np
        img = image[:, :, ::-1] if isinstance(image, np.ndarray) else image  # RGB->BGR
        lines = []
        if self._v3:                                  # 3.x：predict() + 结构化结果
            for r in (self._ocr.predict(img) or []):
                texts = r.get("rec_texts", [])
                polys = r.get("rec_polys")
                if polys is None:
                    polys = r.get("dt_polys", [])
                for txt, poly in zip(texts, polys):
                    lines.append((txt, self._bbox(np.asarray(poly).tolist())))
            return lines
        page = (self._ocr.ocr(img, cls=True) or [None])[0] or []  # 2.x
        for box, (txt, _score) in page:
            lines.append((txt, self._bbox(box)))
        return lines


def make_ocr(name=None):
    """OCR 引擎工厂。name 取 'rapid'|'paddle'；默认 paddle（精度更高，中英混排扫描件更稳），
    可用环境变量 DESENSITIZE_OCR=rapid 全局切回轻量版。Paddle 不可用时自动回退 Rapid，
    保证 OCR 永不整体失效。归类/脱敏/问答共用此工厂，统一走同一 OCR。"""
    name = (name or os.environ.get("DESENSITIZE_OCR", "paddle")).lower()
    if name == "rapid":
        return RapidOCREngine()
    try:
        return PaddleOCREngine()
    except Exception:
        return RapidOCREngine()


def lines_to_text(lines):
    """拼成整页文本（行间用换行分隔），并记录每行的字符区间与坐标框。"""
    parts, ranges, pos = [], [], 0
    for txt, bbox in lines:
        ranges.append((pos, pos + len(txt), bbox))
        parts.append(txt)
        pos += len(txt) + 1                 # +1 为换行符
    return "\n".join(parts), ranges


def span_to_boxes(start, end, ranges):
    """把整页文本上的字符 span 映射成像素子框（可能跨多行）。

    行内按字符比例插值 x，得到该 span 在这一行覆盖的横向子区间——
    比"整行涂黑"精准得多。
    """
    boxes = []
    for ls, le, (x0, y0, x1, y1) in ranges:
        if end <= ls or start >= le:
            continue
        n = max(1, le - ls)
        a = (max(start, ls) - ls) / n
        b = (min(end, le) - ls) / n
        sx0 = x0 + (x1 - x0) * a
        sx1 = x0 + (x1 - x0) * b
        if sx1 - sx0 < 1:                    # 退化保护
            sx1 = sx0 + (x1 - x0) / n
        boxes.append((sx0, y0, sx1, y1))
    return boxes
