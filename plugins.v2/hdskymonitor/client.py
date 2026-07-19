"""MoviePilot 内部 API 客户端。"""
import json
import sys
import urllib.parse
import urllib.request
from app.core.config import settings


def api_get(path, params=None):
    """调用 MoviePilot GET API。"""
    url = f"http://127.0.0.1:{getattr(settings, 'PORT', 3001)}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url)
    req.add_header("X-API-KEY", settings.API_TOKEN or "")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def api_post(path, body=None):
    """调用 MoviePilot POST API。"""
    url = f"http://127.0.0.1:{getattr(settings, 'PORT', 3001)}{path}"
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("X-API-KEY", settings.API_TOKEN or "")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())
