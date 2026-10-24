# -*- coding: utf-8 -*-
"""Flask 入口：抓取书目 → 本地 Web 界面 → 后台下载 jpg → 合成 PDF。

运行：python app.py [--port 5000] [--force-scrape] [--visible]
然后浏览器打开 http://127.0.0.1:5000
"""

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from downloader import download_book, on_verify_event, sanitize
from pdf_maker import folder_to_pdf
from scraper import CATEGORY_TEXT, fetch_book_list

BASE_DIR = Path(__file__).resolve().parent
JPG_DIR = BASE_DIR / "jpg"      # 每本书一个子文件夹，存放页面 jpg
PDF_DIR = BASE_DIR / "pdf"      # 存放合成后的 PDF

DOWNLOAD_WORKERS = 1            # 串行下载：共享持久化浏览器配置，降低触发反爬
PDF_WORKERS = 2                 # 同时合成 PDF 的书本数
VISIBLE = False                 # --visible 时显示下载用的浏览器窗口

app = Flask(__name__)

# 书目字典：{书名: href}
BOOKS: dict = {}

# 每本书的实时进度：{书名: {title, phase, done, total, error}}
_progress: dict = {}
_lock = threading.Lock()
_state = {"running": False, "verify": False}


def _set(title: str, **kw):
    with _lock:
        _progress.setdefault(title, {})
        _progress[title].update(kw)
        _progress[title]["title"] = title


def _get_phase(title: str) -> str:
    with _lock:
        return _progress.get(title, {}).get("phase")


def _task_download(title: str):
    try:
        _set(title, phase="download", done=0, total=0, error=None)
        download_book(
            title, BOOKS[title], JPG_DIR,
            on_progress=lambda d, t: _set(title, phase="download",
                                          done=d, total=t),
            headless=not VISIBLE,
        )
        _set(title, phase="downloaded")
    except Exception as exc:
        _set(title, phase="error", error=str(exc))


def _task_pdf(title: str):
    folder = JPG_DIR / sanitize(title)
    try:
        _set(title, phase="pdf", done=0, error=None)
        folder_to_pdf(
            folder, PDF_DIR / f"{sanitize(title)}.pdf",
            on_progress=lambda d, t: _set(title, phase="pdf", done=d, total=t),
        )
        _set(title, phase="done")
    except Exception as exc:
        _set(title, phase="error", error=str(exc))


def _run_job(titles):
    """后台任务：并行翻页下载全部 jpg → 全部下载完成后逐本合成 PDF。

    每本书由独立浏览器打开阅读器，读取总页数后逐页点击“下一页”，
    随机抖动间隔控制点击速度，逐页保存 jpg。
    """
    try:
        with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as pool:
            for title in titles:
                pool.submit(_task_download, title)

        # 全部 jpg 下载完成后，逐本合成 PDF（界面绿条）
        pdf_titles = [t for t in titles if _get_phase(t) == "downloaded"]
        if pdf_titles:
            with ThreadPoolExecutor(max_workers=PDF_WORKERS) as pool:
                for title in pdf_titles:
                    pool.submit(_task_pdf, title)
    except Exception as exc:
        print(f"[download] 任务异常：{exc}")
    finally:
        _state["running"] = False
        print("[download] 全部任务结束")


@app.get("/")
def index():
    items = [{"title": t, "href": h} for t, h in BOOKS.items()]
    return render_template("index.html", books=items, category=CATEGORY_TEXT)


@app.post("/api/download")
def api_download():
    data = request.get_json(silent=True) or {}
    titles = [t for t in data.get("titles", []) if t in BOOKS]
    if not titles:
        return jsonify({"error": "未选择有效书目"}), 400

    with _lock:
        if _state["running"]:
            return jsonify({"error": "已有下载任务在进行中，请稍候"}), 409
        _state["running"] = True
        for t in titles:
            _progress[t] = {"title": t, "phase": "wait",
                            "done": 0, "total": 0, "error": None}

    threading.Thread(target=_run_job, args=(titles,), daemon=True).start()
    return jsonify({"started": True, "count": len(titles)})


@app.get("/api/progress")
def api_progress():
    with _lock:
        return jsonify({"running": _state["running"],
                        "verify": _state["verify"],
                        "items": list(_progress.values())})


def load_books(cache: Path, force: bool, headless: bool) -> dict:
    if cache.exists() and not force:
        return json.loads(cache.read_text(encoding="utf-8"))
    print(f"[app] 正在抓取“{CATEGORY_TEXT}”书目（首次运行约需 1 分钟）……")
    books = fetch_book_list(headless=headless)
    if books:
        cache.write_text(json.dumps(books, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    return books


def main():
    parser = argparse.ArgumentParser(description="人教数字教材下载器")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--cache", default=str(BASE_DIR / "books.json"),
                        help="书目缓存文件路径")
    parser.add_argument("--force-scrape", action="store_true",
                        help="忽略缓存，重新抓取书目")
    parser.add_argument("--visible", action="store_true",
                        help="显示抓取/解析用的浏览器窗口（调试用）")
    args = parser.parse_args()

    # 滑块验证开始/结束 → 前端横幅提示
    on_verify_event(lambda active: _state.update(verify=active))

    global BOOKS, VISIBLE
    VISIBLE = args.visible
    BOOKS = load_books(Path(args.cache), args.force_scrape, not args.visible)
    if not BOOKS:
        print("[app] 未抓到任何书目，请检查网络后重试（或加 --force-scrape）")
        return
    print(f"[app] 共 {len(BOOKS)} 本书目")
    print(f"[app] 请用浏览器打开 http://{args.host}:{args.port}")

    JPG_DIR.mkdir(exist_ok=True)
    PDF_DIR.mkdir(exist_ok=True)
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
