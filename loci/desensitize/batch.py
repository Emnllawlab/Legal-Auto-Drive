"""整目录批量脱敏：把一个案件文件夹镜像成"脱敏版案件空间"。

use_md=True（默认）：先转 Markdown 再脱敏，输出 .md 文件。
  优势：全文干净文本，NER 上下文更大，效果更好；
  支持格式：.docx / .doc / .pdf（含扫描件）/ 图片。

use_md=False：原格式原位改写（保留排版，但 NER 上下文受限）。
  支持格式：.pdf / .docx / 图片。
"""
import json
from pathlib import Path

from .pseudonymize import Registry, vault_save

_IMAGE = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}
_SUPPORTED_ORIG = {".pdf", ".docx"} | _IMAGE        # use_md=False 时
_SUPPORTED_MD   = {".pdf", ".docx", ".doc"} | _IMAGE  # use_md=True 时
_SKIP_NAMES = {".DS_Store", ".gitkeep"}


def desensitize_tree(src_root, dst_root, reg=None, ner=None, ocr_engine=None,
                     mode="alias", detect_seals=True, lang="zh",
                     vault_password=None, progress=None, use_md=True, cats=None, allow=None):
    """镜像脱敏整棵目录树。

    use_md=True（默认）：先转 Markdown 再脱敏，输出与源文件同路径但扩展名改为 .md。
    progress(i, total, rel_path, result) 每个文件回调一次，
    result 为命中数(int) / "skip" / "error"。返回报告 dict。
    """
    src_root, dst_root = Path(src_root), Path(dst_root)
    reg = reg or Registry(lang=lang)
    _ocr = [ocr_engine]

    def get_ocr():
        if _ocr[0] is None:
            from .ocr import make_ocr
            _ocr[0] = make_ocr()
        return _ocr[0]

    supported = _SUPPORTED_MD if use_md else _SUPPORTED_ORIG
    files = sorted(p for p in src_root.rglob("*") if p.is_file())
    report = {"total": len(files), "processed": 0, "hits": 0,
              "skipped": [], "errors": []}

    for i, src in enumerate(files, 1):
        rel = src.relative_to(src_root)
        ext = src.suffix.lower()
        if src.name in _SKIP_NAMES or ext not in supported:
            report["skipped"].append(str(rel))
            if progress:
                progress(i, len(files), str(rel), "skip")
            continue

        try:
            if use_md:
                from .pipeline import desensitize_md_file
                dst = dst_root / rel.with_suffix(".md")
                dst.parent.mkdir(parents=True, exist_ok=True)
                n = desensitize_md_file(src, dst, reg, ner=ner,
                                        ocr_engine=get_ocr(), lang=lang, cats=cats, allow=allow)
            else:
                from .docx_io import desensitize_docx
                from .pdf_io import desensitize_pdf
                from .image_io import desensitize_image
                dst = dst_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if ext == ".pdf":
                    n = desensitize_pdf(str(src), str(dst), reg, ner=ner, mode=mode,
                                        ocr_engine=get_ocr(), detect_seals=detect_seals)
                elif ext == ".docx":
                    n = desensitize_docx(str(src), str(dst), reg, ner=ner)
                else:
                    n = desensitize_image(str(src), str(dst), reg, ner=ner,
                                          engine=get_ocr(), mode=mode,
                                          detect_seals=detect_seals)
            report["processed"] += 1
            report["hits"] += n
            if progress:
                progress(i, len(files), str(rel), n)
        except Exception as e:
            report["errors"].append({"file": str(rel), "error": f"{type(e).__name__}: {e}"})
            if progress:
                progress(i, len(files), str(rel), "error")

    report["entities"] = len(reg.export())
    dst_root.mkdir(parents=True, exist_ok=True)
    if vault_password:
        vault_save(str(dst_root / "_脱敏映射表.vault"), reg.export(),
                   password=vault_password)
    (dst_root / "_脱敏报告.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def desensitize_files(file_list, dst_root, reg=None, ner=None, ocr_engine=None,
                      lang="zh", vault_password=None, progress=None, cats=None,
                      should_cancel=None, used_names=None, allow=None):
    """把任意勾选的多个文件脱敏成一个扁平 Markdown 包（无视原目录结构）。

    与 desensitize_tree 的区别：源是一份文件路径列表，而不是一棵目录树；
    输出全部平铺到 dst_root 下（同名自动加序号前缀避免覆盖）。
    所有文件共用同一个 Registry，保证整包化名一致（张三→甲 全包统一）。

    progress(i, total, name, result) 每个文件回调一次，
    result 为命中数(int) / "skip" / "error"。返回报告 dict。
    should_cancel(): 可选，返回 True 则中途停止，已处理结果保留（report["cancelled"]=True）。
    used_names: 可选，传入已占用的输出名集合（重试时复用，避免与首轮重名冲突）。
    """
    dst_root = Path(dst_root)
    reg = reg or Registry(lang=lang)
    _ocr = [ocr_engine]

    def get_ocr():
        if _ocr[0] is None:
            from .ocr import make_ocr
            _ocr[0] = make_ocr()
        return _ocr[0]

    paths = [Path(p) for p in file_list]
    report = {"total": len(paths), "processed": 0, "hits": 0,
              "skipped": [], "errors": [], "outputs": [], "cancelled": False}
    if used_names is None:
        used_names = set()

    from .pipeline import desensitize_md_file
    dst_root.mkdir(parents=True, exist_ok=True)

    for i, src in enumerate(paths, 1):
        if should_cancel and should_cancel():
            report["cancelled"] = True
            break
        ext = src.suffix.lower()
        if src.name in _SKIP_NAMES or ext not in _SUPPORTED_MD or not src.is_file():
            report["skipped"].append(src.name)
            if progress:
                progress(i, len(paths), src.name, "skip")
            continue

        # 扁平命名：同名加序号前缀，避免不同目录的同名文件互相覆盖
        out_name = f"{src.stem}.md"
        if out_name in used_names:
            out_name = f"{i:02d}_{src.stem}.md"
        used_names.add(out_name)
        dst = dst_root / out_name

        try:
            n = desensitize_md_file(src, dst, reg, ner=ner,
                                    ocr_engine=get_ocr(), lang=lang, cats=cats, allow=allow)
            report["processed"] += 1
            report["hits"] += n
            report["outputs"].append(out_name)
            if progress:
                progress(i, len(paths), src.name, n)
        except Exception as e:
            report["errors"].append({"file": src.name, "error": f"{type(e).__name__}: {e}"})
            if progress:
                progress(i, len(paths), src.name, "error")

    report["entities"] = len(reg.export())
    if vault_password:
        vault_save(str(dst_root / "_脱敏映射表.vault"), reg.export(),
                   password=vault_password)
    (dst_root / "_脱敏报告.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
