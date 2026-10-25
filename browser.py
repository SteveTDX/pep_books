# -*- coding: utf-8 -*-
"""统一的 Selenium 浏览器创建（隐藏自动化特征 + 可选持久化配置）。

阿里云 WAF 会识别自动化浏览器（navigator.webdriver、enable-automation
等特征），导致人工滑动滑块也被判失败（页面报 error: xxxx），因此所有
浏览器统一做“去自动化”处理：
- --disable-blink-features=AutomationControlled
- excludeSwitches=enable-automation / useAutomationExtension=False
- CDP 注入脚本覆写 navigator.webdriver
- 显式设置真实 Chrome UA（headless 模式默认 UA 含 HeadlessChrome）

user_data_dir：指定持久化用户配置目录。WAF 验证通过后的信任 Cookie
保存在配置里，跨书本、跨程序运行持续有效，避免反复弹滑块验证。
注意：同一配置目录同时只能被一个浏览器进程使用（下载因此串行）。
"""

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_STEALTH_JS = ("Object.defineProperty(navigator, 'webdriver', "
               "{get: () => undefined});")


def _apply_stealth(opts, headless: bool, window, user_data_dir=None):
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--window-size={window[0]},{window[1]}")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument(f"--user-agent={UA}")
    opts.add_argument("--log-level=3")
    if user_data_dir:
        opts.add_argument(f"--user-data-dir={user_data_dir}")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)


def _log(msg: str):
    print(msg, flush=True)


def create_driver(headless: bool = True, window=(1366, 900), log=_log,
                  user_data_dir=None):
    """创建带隐身处理的浏览器驱动：优先 Chrome，失败回退 Edge。

    Selenium Manager 会自动下载匹配的浏览器驱动。
    指定了 user_data_dir 时不回退 Edge（两者的配置目录不通用），
    由调用方决定是否重置配置后重试。
    """
    from selenium import webdriver

    chrome_exc = None
    try:
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        opts = ChromeOptions()
        _apply_stealth(opts, headless, window, user_data_dir)
        driver = webdriver.Chrome(options=opts)
    except Exception as exc:
        chrome_exc = exc
        if user_data_dir:
            raise RuntimeError(
                f"Chrome 启动失败（配置目录可能被残留浏览器进程占用或损坏）："
                f"{str(exc).splitlines()[0]}") from exc
        log(f"[browser] Chrome 启动失败（{str(exc).splitlines()[0]}），回退 Edge")
        from selenium.webdriver.edge.options import Options as EdgeOptions
        opts = EdgeOptions()
        _apply_stealth(opts, headless, window, user_data_dir)
        driver = webdriver.Edge(options=opts)

    driver.set_page_load_timeout(60)
    driver.set_script_timeout(120)   # execute_async_script 提取图片用
    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument", {"source": _STEALTH_JS})
    except Exception:
        pass
    return driver
