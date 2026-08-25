from __future__ import annotations

import json
import time
from collections import defaultdict
from functools import lru_cache
from typing import Any, NamedTuple

from .models import CaptureResult, NetworkRequest


class SeleniumApi(NamedTuple):
    """The Selenium names browser capture needs, resolved on first use."""

    webdriver: Any
    Options: Any
    By: Any
    EC: Any
    WebDriverWait: Any


@lru_cache(maxsize=1)
def selenium_api() -> SeleniumApi:
    """Import Selenium lazily so the package is usable without a browser stack.

    Only browser capture needs Selenium. Importing it at module scope would make
    the CLI, the resolver chain, and the server all require Chrome to be
    installed before they could even start.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise RuntimeError(
            "Browser capture needs Selenium, which is not installed. "
            "Install it with: pip install selenium"
        ) from exc
    return SeleniumApi(webdriver, Options, By, EC, WebDriverWait)


def _build_driver(headless: bool):
    api = selenium_api()
    options = api.Options()
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--window-size=1400,900")
    if headless:
        options.add_argument("--headless=new")
    driver = api.webdriver.Chrome(options=options)
    driver.execute_cdp_cmd("Network.enable", {})
    return driver


def _normalize_headers(raw_headers: dict) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in raw_headers.items():
        normalized[str(key)] = str(value)
    return normalized


def _try_play_in_current_context(driver) -> None:
    try:
        driver.execute_script(
            """
            const video = document.querySelector('video');
            if (video) {
              video.muted = true;
              video.play().catch(() => null);
            }
            """
        )
    except Exception:
        pass

    try:
        driver.execute_script(
            """
            const selectors = [
              '.vjs-big-play-button',
              '.jw-icon-playback',
              '.jw-display-icon-container',
              '.plyr__control--overlaid',
              '[aria-label*="play" i]',
              'button[title*="play" i]',
              'button.play',
              '.play'
            ];
            for (const s of selectors) {
              const el = document.querySelector(s);
              if (el) { el.click(); break; }
            }
            """
        )
    except Exception:
        pass


def capture_page(
    url: str,
    wait_seconds: int = 15,
    headless: bool = True,
    try_play: bool = True,
) -> CaptureResult:
    driver = _build_driver(headless=headless)

    try:
        driver.get(url)

        # Wait for body so we can attempt interactions safely.
        api = selenium_api()
        api.WebDriverWait(driver, 20).until(
            api.EC.presence_of_element_located((api.By.TAG_NAME, "body"))
        )

        if try_play:
            _try_play_in_current_context(driver)

            # Try interactions inside iframes as many players are embedded.
            try:
                frames = driver.find_elements(api.By.TAG_NAME, "iframe")
                for frame in frames:
                    try:
                        driver.switch_to.frame(frame)
                        _try_play_in_current_context(driver)
                    except Exception:
                        pass
                    finally:
                        driver.switch_to.default_content()
            except Exception:
                pass

        time.sleep(wait_seconds)

        raw_logs = driver.get_log("performance")
        request_map: dict[str, NetworkRequest] = {}
        response_map: dict[str, dict] = defaultdict(dict)

        for entry in raw_logs:
            msg = json.loads(entry["message"]).get("message", {})
            method = msg.get("method")
            params = msg.get("params", {})

            if method == "Network.requestWillBeSent":
                request_id = params.get("requestId")
                req = params.get("request", {})
                req_url = req.get("url")
                if not request_id or not req_url:
                    continue
                request_map[request_id] = NetworkRequest(
                    url=req_url,
                    method=req.get("method", "GET"),
                    headers=_normalize_headers(req.get("headers", {})),
                    resource_type=params.get("type"),
                )

            if method == "Network.responseReceived":
                request_id = params.get("requestId")
                response = params.get("response", {})
                if not request_id:
                    continue
                response_map[request_id] = {
                    "status": response.get("status"),
                    "headers": _normalize_headers(response.get("headers", {})),
                }

        requests: list[NetworkRequest] = []
        for request_id, req in request_map.items():
            resp = response_map.get(request_id, {})
            req.status = resp.get("status")
            req.response_headers = resp.get("headers", {})
            requests.append(req)

        cookies = {cookie["name"]: cookie["value"] for cookie in driver.get_cookies()}
        # Include cross-domain cookies observed in this browser session.
        # Some media CDNs require cookies not scoped to the top page domain.
        try:
            all_cookies = driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])
            for cookie in all_cookies:
                name = str(cookie.get("name") or "").strip()
                value = str(cookie.get("value") or "")
                if name:
                    cookies[name] = value
        except Exception:
            pass
        user_agent = driver.execute_script("return navigator.userAgent")
        title = driver.title or ""

        return CaptureResult(
            page_url=url,
            final_url=driver.current_url,
            title=title,
            user_agent=user_agent,
            cookies=cookies,
            requests=requests,
        )
    finally:
        driver.quit()
