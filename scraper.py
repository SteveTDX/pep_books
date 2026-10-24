# -*- coding: utf-8 -*-
"""书目抓取模块（Selenium）。

访问 https://jc.pep.com.cn/ ，按以下流程抓取书目清单：

1. 打开首页；
2. 激活（点击）text 为“义务教育（五四学制）”的 li 元素；
3. 读取 ul.el-pager 分页器显示的页数；
4. 逐页遍历 div.page_pc_btm_book_body，取其第一个 class="imga" 的 <a>
   子元素的 title（书名）与 href（阅读地址前段）。

返回 dict：{书名: href}，按页面出现顺序排列。
"""

import time

from selenium.webdriver.common.by import By

from browser import create_driver

BASE_URL = "https://jc.pep.com.cn/"
CATEGORY_TEXT = "义务教育（五四学制）"

BOOK_LINK_SEL = "div.page_pc_btm_book_body a.imga"


def _page_numbers(driver):
    """返回 [(页码, li元素), ...]，只保留纯数字页码。"""
    result = []
    for li in driver.find_elements(By.CSS_SELECTOR, "ul.el-pager li"):
        text = (li.text or "").strip()
        if text.isdigit():
            result.append((int(text), li))
    return result


def _total_pages(driver) -> int:
    nums = [n for n, _ in _page_numbers(driver)]
    return max(nums) if nums else 1


def _active_page(driver):
    """当前激活页码（el-pager 的 li.active）；无法识别时返回 None。"""
    try:
        li = driver.find_element(By.CSS_SELECTOR, "ul.el-pager li.active")
        text = (li.text or "").strip()
        return int(text) if text.isdigit() else None
    except Exception:
        return None


def _first_title(driver):
    links = driver.find_elements(By.CSS_SELECTOR, BOOK_LINK_SEL)
    return (links[0].get_attribute("title") or "").strip() if links else None


def _collect_page(driver, books: dict):
    """收集当前页书目到 books（{书名: href}）。"""
    for card in driver.find_elements(By.CSS_SELECTOR,
                                     "div.page_pc_btm_book_body"):
        links = card.find_elements(By.CSS_SELECTOR, "a.imga")
        if not links:
            continue
        a = links[0]  # 第一个 class="imga" 的 <a> 子元素
        title = (a.get_attribute("title") or "").strip()
        href = (a.get_attribute("href") or "").strip()
        if title and href:
            books.setdefault(title, href)


def _goto_page(driver, target: int, timeout_s: float = 20) -> bool:
    """翻到第 target 页，成功返回 True。

    优先点击页码数字 li；页面折叠时点击“下一页”按钮（button.btn-next）。
    以“激活页码变为 target”或“列表首本书名变化”作为翻页成功依据。
    """
    if _active_page(driver) == target:
        return True

    before_title = _first_title(driver)

    clicked = False
    for n, li in _page_numbers(driver):
        if n == target:
            try:
                driver.execute_script("arguments[0].click();", li)
                clicked = True
                break
            except Exception:
                pass
    if not clicked:
        try:
            btn = driver.find_element(By.CSS_SELECTOR, "button.btn-next")
            if btn.get_attribute("disabled") is not None:
                return False
            driver.execute_script("arguments[0].click();", btn)
        except Exception:
            return False

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _active_page(driver) == target:
            time.sleep(0.5)  # 等剩余条目渲染完成
            return True
        after = _first_title(driver)
        if after not in (None, before_title) and _active_page(driver) is None:
            time.sleep(0.5)
            return True
        time.sleep(0.3)
    return False


def fetch_book_list(headless: bool = True, log=print) -> dict:
    """抓取“义务教育（五四学制）”全部书目，返回 {书名: href}。"""
    books: dict = {}
    driver = create_driver(headless=headless, window=(1366, 1000), log=log)
    try:
        log(f"[scraper] 打开 {BASE_URL}")
        driver.get(BASE_URL)

        # 等待并激活分类 li（精确匹配文本）
        target, deadline = None, time.time() + 20
        while time.time() < deadline:
            for li in driver.find_elements(By.TAG_NAME, "li"):
                if (li.text or "").strip() == CATEGORY_TEXT:
                    target = li
                    break
            if target is not None:
                break
            time.sleep(0.5)
        if target is None:
            raise RuntimeError(f"未找到分类 li：{CATEGORY_TEXT}")
        log(f"[scraper] 激活分类：{CATEGORY_TEXT}")
        driver.execute_script("arguments[0].click();", target)

        # 等待书目网格加载
        deadline = time.time() + 20
        while time.time() < deadline:
            if driver.find_elements(By.CSS_SELECTOR, BOOK_LINK_SEL):
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("书目网格未加载")
        time.sleep(1.2)  # 等渲染稳定

        total = _total_pages(driver)
        log(f"[scraper] 分页器共 {total} 页")

        _collect_page(driver, books)
        log(f"[scraper] 第 1/{total} 页，累计 {len(books)} 本")

        for pg in range(2, total + 1):
            if not _goto_page(driver, pg):
                log(f"[scraper] 翻到第 {pg} 页失败，提前结束")
                break
            _collect_page(driver, books)
            log(f"[scraper] 第 {pg}/{total} 页，累计 {len(books)} 本")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
    return books


if __name__ == "__main__":
    import json
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    data = fetch_book_list()
    print(json.dumps(data, ensure_ascii=False, indent=2))
