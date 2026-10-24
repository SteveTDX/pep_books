# -*- coding: utf-8 -*-
"""下载模块（Selenium）：在真实阅读器里逐页翻页下载页面 jpg。

流程（每本书一个浏览器，共用持久化配置目录，串行下载）：
1. 先打开 jc 首页，再通过页面内 <a> 点击跳转到书本地址（携带
   Referer；直接访问 book.pep.com.cn 会被 WAF 重定向回首页），
   站点自动跳到 <href>mobile/index.html 阅读器；
2. 读取 #currentPageIndexTextField 的 value（形如 "1/70"）得到
   总页数 maxPageCount；
3. 循环：用 canvas 从阅读器已加载的 DOM 图片提取当前页内容存盘
   （img → drawImage → toDataURL，同源画布、零额外网络请求）；
4. 点击“下一页”控件（图标 div.flip_button_right）翻页并等待页码
   前进，点击间隔带随机抖动（CLICK_MIN_S ~ CLICK_MAX_S 秒）；
5. 重复直到 maxPageCount 页全部保存到 jpg/<书名>/NNN.jpg
   （已存在的有效页自动跳过，可断点续传）。

反爬处理（阿里云 WAF，实测要点）：
- 直接访问 book.pep.com.cn 会被重定向回 jc 首页 → 先到首页再点击跳转；
- 对图片地址发起 fetch/xhr 会被拦截返回验证页 → 只用 canvas 提取
  阅读器自身已加载的图片，不产生任何额外图片请求；
- 若仍触发滑块验证（Page Verification），自动在同一持久化配置上
  重开“有头”窗口由用户人工滑动一次后继续；持久化配置
  （chrome_profile/）会记住验证信任状态。
"""

import base64
import random
import re
import time
from pathlib import Path
from urllib.parse import urljoin

from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from browser import create_driver

JC_URL = "https://jc.pep.com.cn/"
PROFILE_DIR = Path(__file__).resolve().parent / "chrome_profile"

CLICK_MIN_S = 1.2      # 翻页点击间隔下限（秒）
CLICK_MAX_S = 2.8      # 翻页点击间隔上限（秒），实际取随机值抖动
PAGE_WAIT_S = 8        # 点击翻页后等待页码变化的最长时间
VERIFY_TIMEOUT_S = 300  # 等待用户完成滑块验证的最长时间（秒）

_INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')

_verify_listeners = []       # [fn(active: bool)]，供界面提示使用


def on_verify_event(fn):
    """注册滑块验证开始/结束回调（active=True 表示正等待用户验证）。"""
    _verify_listeners.append(fn)


def _notify_verify(active: bool):
    for fn in _verify_listeners:
        try:
            fn(active)
        except Exception:
            pass


# ---------------- 阅读器操作 ----------------

def sanitize(name: str) -> str:
    """去掉 Windows 文件名非法字符。"""
    name = _INVALID_CHARS.sub("_", name).strip().rstrip(".")
    return name or "未命名"


def normalize_base(href: str) -> str:
    """把 href 规范化为以 / 结尾的绝对地址（地址前段）。"""
    base = urljoin(JC_URL, href)
    return base if base.endswith("/") else base + "/"


def _is_challenge(driver) -> bool:
    """当前页是否为 WAF“滑块验证”页。"""
    try:
        if "page verification" in (driver.title or "").lower():
            return True
        if driver.find_elements(By.ID, "currentPageIndexTextField"):
            return False
        body = (driver.find_element(By.TAG_NAME, "body").text or "").lower()
        return "slide to verify" in body or "滑动" in body
    except Exception:
        return False


def _wait_input(driver, timeout_s: float) -> bool:
    """等待页码输入框出现（浏览器被关闭时立即返回 False）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if driver.find_elements(By.ID, "currentPageIndexTextField"):
                return True
        except Exception:
            return False
        time.sleep(0.5)
    return False


def _input_value(driver) -> str:
    """#currentPageIndexTextField 的 value 属性（形如 "1/70"）。"""
    try:
        els = driver.find_elements(By.ID, "currentPageIndexTextField")
        return (els[0].get_attribute("value") or "") if els else ""
    except Exception:
        return ""


def _current_page(driver) -> int:
    """当前页码（跨页显示时取左页码）。"""
    m = re.search(r"\d+", _input_value(driver).split("/")[0])
    return int(m.group()) if m else 0


def _read_total(driver, mobile: str) -> int:
    for _ in range(60):  # 最多等 30 秒让 JS 填入 value
        v = _input_value(driver)
        if "/" in v:
            m = re.search(r"(\d+)\s*$", v.split("/")[-1])
            if m:
                return int(m.group(1))
        time.sleep(0.5)
    raise ValueError(f"无法从 {mobile} 解析总页数")


_ANCHOR_JS = """
    const a = document.createElement('a');
    a.href = arguments[0]; a.target = '_self';
    document.body.appendChild(a); a.click();
"""


def _goto_reader(driver, base: str, mobile: str):
    """先打开 jc 首页，再通过 <a> 点击跳到书本页面（携带 Referer）。

    直接访问 book.pep.com.cn 会被 WAF 重定向回 jc 首页；
    从首页点击跳转（等价于用户点“在线阅读”）则正常放行。
    先点书本根地址（站点会自动跳到 mobile/index.html），
    若未出现阅读器再直接点击 mobile 地址。
    """
    driver.get(JC_URL)
    for _ in range(20):
        if (driver.title or "").strip():
            break
        time.sleep(0.5)
    time.sleep(1.0)
    driver.execute_script(_ANCHOR_JS, base)
    if not _wait_input(driver, 8):
        driver.execute_script(_ANCHOR_JS, mobile)


def _open_reader(base: str, mobile: str, headless: bool, log=print):
    """打开阅读器；遇到滑块验证则换有头窗口由用户滑动后继续。

    返回已通过验证、处于阅读器页面的驱动。
    """
    driver = create_driver(headless=headless, user_data_dir=PROFILE_DIR,
                           log=log)
    try:
        _goto_reader(driver, base, mobile)
        if _wait_input(driver, 15):
            return driver
        if not _is_challenge(driver):
            raise RuntimeError("阅读页加载超时，未出现页码输入框")

        # 无头会话被拦：换有头窗口（同一持久化配置）请用户滑一次
        log(">>> 检测到滑块验证：切换到可见浏览器窗口，请手动完成验证 "
            f"（{VERIFY_TIMEOUT_S // 60} 分钟内）……")
        try:
            driver.quit()
        except Exception:
            pass
        _notify_verify(True)
        driver = create_driver(headless=False, window=(1280, 860),
                               user_data_dir=PROFILE_DIR, log=log)
        try:
            _goto_reader(driver, base, mobile)
            if not _wait_input(driver, VERIFY_TIMEOUT_S):
                raise RuntimeError("滑块验证未完成（超时或窗口被关闭）")
        finally:
            _notify_verify(False)
        log(">>> 验证通过，用当前窗口继续翻页下载")
        return driver
    except Exception:
        try:
            driver.quit()
        except Exception:
            pass
        raise


_EXTRACT_JS = """const cb = arguments[arguments.length - 1];
(async () => {
    const out = [];
    const imgs = [...document.querySelectorAll('img')].filter(
        e => e.src.includes('/files/mobile/'));
    // 等图片加载完成（最多约 3 秒）
    for (let t = 0; t < 30; t++) {
        if (imgs.every(e => e.complete)) break;
        await new Promise(r => setTimeout(r, 100));
    }
    for (const e of imgs) {
        if (!e.complete || !e.naturalWidth) continue;
        try {
            const c = document.createElement('canvas');
            c.width = e.naturalWidth; c.height = e.naturalHeight;
            c.getContext('2d').drawImage(e, 0, 0);
            out.push({u: e.src, d: c.toDataURL('image/jpeg', 0.95)});
        } catch (err) { /* 单张失败忽略 */ }
    }
    cb(out);
})();"""


def _extract_images(driver) -> dict:
    """从阅读器已加载的 DOM 图片直接提取像素 → {页码: dataURL}。

    用 canvas.drawImage + toDataURL（同源画布），不产生任何额外网络
    请求——实测对图片地址发起 fetch 会被 WAF 拦截返回验证页，而阅读器
    自身的图片加载是放行的，因此只提取它已加载的内容。
    """
    try:
        rows = driver.execute_async_script(_EXTRACT_JS) or []
    except Exception:
        return {}
    result = {}
    for row in rows or []:
        m = re.search(r"/files/mobile/(\d+)\.jpg", row.get("u") or "")
        d = row.get("d") or ""
        if m and d.startswith("data:image"):
            result[int(m.group(1))] = d
    return result


# 点击“下一页”图标（实测：div.flip_button_right 内的 img[src*=icon/next]）
_NEXT_ICON_JS = """return (() => {
    const img = document.querySelector('img[src*="icon/next"]');
    if (!img) return false;
    const target = img.closest('a,[onclick],div[class],span[class]')
                   || img.parentElement || img;
    target.click();
    return true;
})();"""

# 其他备选的“下一页”控件选择器（按序尝试）
_NEXT_SELECTORS = [
    "#nextBtn", "#btn_next", "#btnNext", "#pageNext", "#next",
    ".nextBtn", ".btn-next", ".next-btn", ".flip_button_right",
    "[class*='next' i]",
]


def _flip_next(driver, cur: int, state: dict, log=print) -> bool:
    """点击“下一页”并等待页码前进，成功返回 True。"""

    def _advanced(timeout_s: float) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if _current_page(driver) > cur:
                return True
            time.sleep(0.3)
        return False

    def _run_js(js: str, *args) -> bool:
        try:
            driver.execute_script(js, *args)
            return _advanced(PAGE_WAIT_S)
        except Exception:
            return False

    # 策略 1：翻页图标（实测有效）
    if _run_js(_NEXT_ICON_JS):
        return True

    # 策略 2：常见“下一页”控件选择器
    order = ([_NEXT_SELECTORS[state["idx"]]]
             + _NEXT_SELECTORS[:state["idx"]]
             + _NEXT_SELECTORS[state["idx"] + 1:])
    for sel in order:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if not els or not els[0].is_displayed():
                continue
            driver.execute_script("arguments[0].click();", els[0])
            if _advanced(PAGE_WAIT_S):
                state["idx"] = _NEXT_SELECTORS.index(sel)
                return True
        except Exception:
            continue

    # 策略 3：键盘右方向键
    try:
        ActionChains(driver).send_keys(Keys.ARROW_RIGHT).perform()
        if _advanced(PAGE_WAIT_S):
            return True
    except Exception:
        pass

    # 策略 4：点击书页区域右侧（elementFromPoint 取点击目标，避免遮挡）
    try:
        driver.execute_script(
            "var e = document.elementFromPoint(arguments[0], arguments[1]);"
            " if (e) { e.click(); }",
            int(1366 * 0.85), int(900 * 0.5))
        if _advanced(PAGE_WAIT_S):
            return True
    except Exception:
        pass

    log(f"[downloader] 翻页失败（当前第 {cur} 页）")
    return False


def _write_data_url(data_url: str, dest: Path):
    """把 dataURL（JPEG）解码写入文件，校验 JPEG 魔数。"""
    raw = base64.b64decode(data_url.split(",", 1)[1])
    if not raw.startswith(b"\xff\xd8"):
        raise RuntimeError("提取到的不是有效 JPEG 图片")
    dest.write_bytes(raw)


def _is_valid_jpg(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            return f.read(2) == b"\xff\xd8"
    except OSError:
        return False


# ---------------- 对外入口 ----------------

def download_book(title: str, href: str, jpg_root: Path,
                  on_progress=None, headless: bool = True,
                  log=print) -> Path:
    """打开阅读器逐页翻页下载整本书的 jpg，返回图片目录。

    on_progress(已获取页数, 总页数) 每存下一页回调一次；
    已存在且非空的页文件自动跳过（断点续传）。
    """
    mobile = normalize_base(href) + "mobile/index.html"
    base = normalize_base(href)
    folder = jpg_root / sanitize(title)
    folder.mkdir(parents=True, exist_ok=True)

    driver = _open_reader(base, mobile, headless, log=log)
    try:
        total = _read_total(driver, mobile)
        log(f"[downloader] 《{title}》共 {total} 页，开始翻页下载")

        # 清理无效残留（如被拦截时误存的网页），再统计已有页（断点续传）
        for f in folder.glob("*.jpg"):
            if f.stem.isdigit() and not _is_valid_jpg(f):
                f.unlink()
        have = {int(f.stem) for f in folder.glob("*.jpg")
                if f.stem.isdigit() and _is_valid_jpg(f)}
        if on_progress:
            on_progress(len(have), total)

        errors: list = []

        def _bank():
            """把阅读器当前已加载的页面图片全部存盘。"""
            for n, data_url in sorted(_extract_images(driver).items()):
                if n in have:
                    continue
                try:
                    _write_data_url(data_url, folder / f"{n:03d}.jpg")
                except Exception as exc:
                    errors.append(f"第{n}页：{exc}")
                    continue
                have.add(n)
                if on_progress:
                    on_progress(len(have), total)

        flip_state = {"idx": 0}
        stall = 0
        while len(have) < total:
            _bank()
            if len(have) >= total:
                break

            cur = _current_page(driver)
            if cur >= total:      # 已到最后一页，稍等后把余图收完
                time.sleep(1.5)
                _bank()
                break

            if not _flip_next(driver, cur, flip_state, log=log):
                stall += 1
                if stall >= 3:
                    raise RuntimeError(
                        f"无法翻页（停在第 {cur} 页，已获取 "
                        f"{len(have)}/{total} 页）")
                continue
            stall = 0
            # 控制点击速度：随机抖动间隔
            time.sleep(random.uniform(CLICK_MIN_S, CLICK_MAX_S))

        if len(have) < total or errors:
            msg = f"仅获取 {len(have)}/{total} 页"
            if errors:
                msg += "；" + "；".join(errors[:2])
                msg += "……" if len(errors) > 2 else ""
            raise RuntimeError(msg)
        return folder
    finally:
        try:
            driver.quit()
        except Exception:
            pass
