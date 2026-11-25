"""图片脱敏：OCR 定位 -> 识别管线 -> 在像素上涂改 + 叠加化名。

适用于 JPG/PNG（聊天截图、财务凭证拍照、扫描页另存的图）。
mode="alias" 白底+化名；mode="blackout" 涂黑。
需要 pillow + rapidocr_onnxruntime（延迟导入）。
"""
import glob

from .pipeline import analyze_text, spans_from_dicts
from .pseudonymize import plan, Registry
from .ocr import RapidOCREngine, lines_to_text, span_to_boxes

# macOS 自带中文字体；找不到则退默认字体（化名可能不渲染中文）
_FONT_CANDIDATES = (
    glob.glob("/System/Library/Fonts/STHeiti Light.ttc")
    + glob.glob("/System/Library/Fonts/PingFang.ttc")
    + glob.glob("/System/Library/Fonts/Hiragino Sans GB.ttc")
    + glob.glob("/usr/share/fonts/**/*.ttc", recursive=True)
)
_font_cache = {}


def _cjk_font(size):
    size = int(size)
    if size not in _font_cache:
        from PIL import ImageFont
        try:
            _font_cache[size] = ImageFont.truetype(_FONT_CANDIDATES[0], size)
        except (IndexError, OSError):
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


def _draw_alias(draw, box, alias):
    x0, y0, x1, y1 = box
    size = max(8, int((y1 - y0) * 0.8))
    draw.text((x0, y0), alias, fill=(0, 0, 0), font=_cjk_font(size))


def desensitize_image(in_path, out_path, reg: Registry,
                      ner=None, engine=None, mode="alias", detect_seals=True,
                      lang="both", cats=None, allow=None):
    """脱敏一张图片，写到 out_path。复用 reg 保证跨文档化名一致。返回涂改区域数。

    detect_seals=True 时额外检测并覆盖红色印章。
    """
    import numpy as np
    from PIL import Image, ImageDraw

    img = Image.open(in_path).convert("RGB")
    arr0 = np.array(img)                        # 原始像素（印章检测用，先于涂改）
    draw = ImageDraw.Draw(img)
    fill = (0, 0, 0) if mode == "blackout" else (255, 255, 255)
    count = 0

    engine = engine or RapidOCREngine()
    full_text, ranges = lines_to_text(engine.run(in_path))
    if full_text.strip():
        detected = analyze_text(full_text, reg, ner=ner, fallback=ner is None,
                                lang=lang, cats=cats, allow=allow, register=True)
        for rep in plan(full_text, spans_from_dicts(full_text, detected), reg):
            for i, box in enumerate(span_to_boxes(rep["start"], rep["end"], ranges)):
                draw.rectangle(box, fill=fill)
                if mode == "alias" and i == 0:      # 只在首个子框叠化名
                    _draw_alias(draw, box, rep["alias"])
                count += 1

    if detect_seals:
        from .seal_detect import detect_seals as _detect
        for box in _detect(arr0):
            draw.rectangle(box, fill=fill)
            if mode == "alias":
                _draw_alias(draw, box, "〔印章〕")
            count += 1

    img.save(out_path)
    return count
