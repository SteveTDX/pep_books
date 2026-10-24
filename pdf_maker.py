# -*- coding: utf-8 -*-
"""PDF 合成模块：把同一本书的 jpg 按页码顺序合并为 PDF。

优先使用 img2pdf（JPEG 无损嵌入、不重新编码、速度快、体积小）；
未安装 img2pdf 时回退到 Pillow（逐张转 RGB 后合并）。
两种途径都支持 on_progress(当前张数, 总张数) 进度回调。
"""

import re
from pathlib import Path


def _page_key(path: Path):
    """按页码数字升序排列（001.jpg、002.jpg ……），非数字名排最后。"""
    m = re.fullmatch(r"\d+", path.stem)
    return (0, int(path.stem)) if m else (1, path.name)


def list_jpgs(folder: Path):
    return sorted(folder.glob("*.jpg"), key=_page_key)


class _ProgressList(list):
    """list 子类：img2pdf 逐个取出图片时触发进度回调。"""

    def __init__(self, items, cb):
        super().__init__(items)
        self._cb = cb

    def __iter__(self):
        total = len(self)
        for i, item in enumerate(super().__iter__(), 1):
            self._cb(i, total)
            yield item


def folder_to_pdf(folder: Path, pdf_path: Path, on_progress=None) -> None:
    """把 folder 下的 jpg 按顺序合并写入 pdf_path。"""
    cb = on_progress or (lambda *_: None)
    files = list_jpgs(folder)
    if not files:
        raise FileNotFoundError(f"{folder} 中没有 jpg 文件")

    # 校验均为有效 JPEG，避免坏文件（如被拦截时误存的网页）导致合成失败
    bad = []
    for f in files:
        try:
            with f.open("rb") as fp:
                if fp.read(2) != b"\xff\xd8":
                    bad.append(f.name)
        except OSError:
            bad.append(f.name)
    if bad:
        raise RuntimeError(
            f"{len(bad)} 个 jpg 文件无效（{', '.join(bad[:3])}…），"
            "请删除后重新下载该书")

    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import img2pdf

        with open(pdf_path, "wb") as fp:
            fp.write(img2pdf.convert(_ProgressList([str(f) for f in files], cb)))
    except ImportError:
        from PIL import Image

        images = []
        try:
            for i, path in enumerate(files, 1):
                im = Image.open(path)
                images.append(im.convert("RGB"))
                im.close()
                cb(i, len(files))
            images[0].save(str(pdf_path), "PDF", save_all=True,
                           append_images=images[1:], quality=95)
        finally:
            for im in images:
                im.close()

    cb(len(files), len(files))
