# -*- coding: utf-8 -*-
"""
云朵|4K · TVBox Spider (Python 实现)

本文件实现的是这条配置, 其 ext 已内置为默认值 —— 不传 ext 即可直接运行:

    {"key":  "云朵",
     "name": "云朵|4K",
     "type": 3,
     "ext":  {"host":        "https://323433ssdfd.top",
              "pkg":         "com.sunshine.tv",
              "sk":          "SK-thanks",
              "finger":      "SF-C3B2B41F6EFFFF9869176CF68F6790E8F07506FC88632C94B4F5F0430D5498CA",
              "ver":         "7",
              "updateId":    "01a00b72-a18d-77ac-9921-9dbe96573441",
              "deviceBrand": "vivo",
              "deviceModel": "V2309A"}}

协议要点(按服务端既有规则实现):
  1. 请求签名  x-sign = SHA256("finger={finger}&id={pkg}&nonce={nonce}&sk={sk}&time={ms}&v={ver}").upper()
  2. 响应加密  AES-128-GCM: 响应体 = IV(12字节) + 密文 + GCM标签(16字节)
              密钥由响应头 mcg821-a 给出的索引, 从 _KEY_POOL 中选取
  3. 设备ID    首次运行随机生成 16 位小写 HEX 并持久化, 之后固定复用

换站/换渠道: 用 ext 覆盖 host / pkg / sk / finger / ver / updateId /
             deviceBrand / deviceModel 中的任意项即可, 未覆盖的沿用默认值。

依赖: cryptography 或 pycryptodome (二选一即可)
"""

import hashlib
import json
import os
import random
import re
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error

# ---------------------------------------------------------------- 默认配置

DEFAULT_EXT = {
    "host": "https://323433ssdfd.top",
    "pkg": "com.sunshine.tv",
    "sk": "SK-thanks",
    "finger": "SF-C3B2B41F6EFFFF9869176CF68F6790E8F07506FC88632C94B4F5F0430D5498CA",
    "ver": "7",
    "updateId": "01a00b72-a18d-77ac-9921-9dbe96573441",
    "deviceBrand": "vivo",
    "deviceModel": "V2309A",
}

# 响应解密密钥池(服务端通过响应头 mcg821-a 指定索引)
_KEY_POOL = [
    "A7mQ9vL2pX4rZ8tN", "b3Tn6Yq8Kp2Vx5Ls", "R5cH2wN9eM7qP4vD", "x8Lk1Zp6Cw3Nq9Ty",
    "M4vS7rQ2bT9hX6kE", "p9Dq3Lx8Vn5Cz2Ra", "K2wF6tM8yQ4sH7Np", "z6Pj9Rb3Lc8Vx1Tm",
    "N8qC4yL7pS2dK5Wa", "t3Vx9Mn6Qp4Rs8Yk", "C7hL2qT5vN9xB3Wp", "y4Rk8Pz1Md6Lq3Vs",
    "L9pX2cQ7tV4nR8Hy", "q5Nw8Zr3Kp6Mt2Va", "V2cT9yL5Rq8Nw4Ks", "s8Kp4Xn7Cw2Vq9Md",
    "D6rM1tY8pL3zQ5Vx", "w9Qv5Ck2Nr8Ty4Lp", "P3xL7mR9qV2cN6Ty", "h2Zq8Vn4Kp7Sx5Mc",
    "T8mC3yQ6rL9pV2Nw", "n5Rk2Pz8Xq4Vt7Ls", "Q4vN9cL3Mp6Yx2Rk", "k7Tq1Wv5Zn8Pc4Ms",
    "X6pL3rV8Cq2Ny9Kt", "m2Qz7Kp4Vx9Ts5Rc", "Y9cV5nL2Rq8Pw3Kx", "r4Mp8Tq1Zc6Vn9Ly",
    "B7xQ2vK9pR5Lm3Ts", "u8Lr4Cq7Nw2Vp5Yz",
]

_HEX_UPPER = "0123456789ABCDEF"
_HEX_LOWER = "0123456789abcdef"

_UA_API = "okhttp/4.12.0"
_UA_PLAYER = "com.sunshine.tv/1.2.0 (Linux;Android 15) AndroidXMedia3/1.4.1"
_ACCEPT = "application/json"

# 需要第三方解析的站点(命中则 parse=1 交给 TVBox 解析)
_NEED_PARSE = re.compile(r"(www\.iqiyi|v\.qq|v\.youku|www\.mgtv|www\.bilibili)\.com")

# 实测稳定性较差的线路排到末尾(可按需调整为空元组保持服务端原顺序)
_LINE_DEPRIORITIZE = ("NBY", "qsvip")

_DEVICE_ID_FILE = os.path.join(tempfile.gettempdir(), ".yunduo_device_id")


# ---------------------------------------------------------------- AES-GCM

def _load_aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        def dec(key: bytes, iv: bytes, ct: bytes, tag: bytes) -> bytes:
            return AESGCM(key).decrypt(iv, ct + tag, None)
        return dec
    except Exception:
        pass
    try:
        from Crypto.Cipher import AES
        def dec(key: bytes, iv: bytes, ct: bytes, tag: bytes) -> bytes:
            c = AES.new(key, AES.MODE_GCM, nonce=iv)
            return c.decrypt_and_verify(ct, tag)
        return dec
    except Exception:
        pass
    raise RuntimeError("需要 cryptography 或 pycryptodome: pip install cryptography")


_AESGCM_DECRYPT = _load_aesgcm()


# ---------------------------------------------------------------- Spider

class Spider(object):

    def getName(self):
        return "云朵|4K"

    # ---------------- 初始化 ----------------

    def init(self, extend=""):
        cfg = dict(DEFAULT_EXT)
        try:
            txt = str(extend or "").strip()
            if txt.startswith("{"):
                ext = json.loads(txt)
                if isinstance(ext, dict):
                    for k, v in ext.items():
                        if v:
                            cfg[k] = str(v)
        except Exception:
            pass
        self.host = str(cfg["host"]).rstrip("/")
        self.pkg = cfg["pkg"]
        self.sk = cfg["sk"]
        self.finger = cfg["finger"]
        self.ver = str(cfg["ver"])
        self.update_id = cfg["updateId"]
        self.brand = cfg["deviceBrand"]
        self.model = cfg["deviceModel"]
        self.device_id = self._load_device_id()
        # 分类缓存: type_id -> type_name
        self._cats = {}
        self._timeout = 20

    @staticmethod
    def _load_device_id():
        """设备ID: 首次随机生成并持久化, 之后固定复用"""
        try:
            if os.path.exists(_DEVICE_ID_FILE):
                v = open(_DEVICE_ID_FILE, "r").read().strip()
                if len(v) == 16:
                    return v
        except Exception:
            pass
        v = "".join(random.choice(_HEX_LOWER) for _ in range(16))
        try:
            with open(_DEVICE_ID_FILE, "w") as f:
                f.write(v)
        except Exception:
            pass
        return v

    # ---------------- 传输层 ----------------

    def _headers(self):
        t = str(int(time.time() * 1000))
        nonce = "".join(random.choice(_HEX_UPPER) for _ in range(16))
        raw = "finger={}&id={}&nonce={}&sk={}&time={}&v={}".format(
            self.finger, self.pkg, nonce, self.sk, t, self.ver)
        sign = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
        return {
            "User-Agent": _UA_API,
            "Accept": _ACCEPT,
            "x-aid": self.pkg,
            "x-ave": self.ver,
            "x-avn": "1.6.0",
            "x-time": t,
            "x-nonc": nonce,
            "x-sign": sign,
            "x-device-id": self.device_id,
            "x-device-brand": self.brand,
            "x-device-model": self.model,
            "x-platform": "android",
            "x-update-id": self.update_id,
        }

    def _raw(self, path):
        """返回 (http_code, headers, body_bytes)"""
        try:
            req = urllib.request.Request(self.host + path, headers=self._headers())
            resp = urllib.request.urlopen(req, timeout=self._timeout)
            return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.headers, e.read()
            except Exception:
                return e.code, {}, b""
        except Exception as e:
            print("fetch error: {}".format(e))
            return 0, {}, b""

    def _decrypt(self, headers, body):
        """AES-128-GCM 解密; 无密钥索引时按明文处理"""
        if not body:
            return ""
        try:
            kidx = int(headers.get("mcg821-a", "-1"))
        except Exception:
            kidx = -1
        if kidx < 0 or kidx >= len(_KEY_POOL) or len(body) <= 28:
            return body.decode("utf-8", errors="replace")
        try:
            key = _KEY_POOL[kidx].encode("utf-8")
            iv, ct, tag = body[:12], body[12:-16], body[-16:]
            return _AESGCM_DECRYPT(key, iv, ct, tag).decode("utf-8", errors="replace")
        except Exception as e:
            print("decrypt error: {}".format(e))
            return ""

    def _api(self, path):
        code, headers, body = self._raw(path)
        if not body:
            return {}
        txt = self._decrypt(headers, body)
        try:
            return json.loads(txt)
        except Exception:
            return {}

    # ---------------- 工具 ----------------

    @staticmethod
    def _enc(s):
        return urllib.parse.quote(str(s), safe="!~*'()")

    @staticmethod
    def _card(v):
        return {
            "vod_id": str(v.get("vod_id") or ""),
            "vod_name": str(v.get("vod_name") or ""),
            "vod_pic": (v.get("vod_pic") or "").replace("\\/", "/"),
            "vod_remarks": str(v.get("vod_remarks") or ""),
        }

    def _cards(self, lst):
        seen, out = set(), []
        for v in lst or []:
            if not isinstance(v, dict):
                continue
            vid = str(v.get("vod_id") or "")
            if not vid or vid in seen:
                continue
            seen.add(vid)
            out.append(self._card(v))
        return out

    def _load_cats(self):
        """从首页拉取分类(type_id -> type_name)"""
        if self._cats:
            return self._cats
        j = self._api("/api.php/app/index/home")
        for c in ((j.get("data") or {}).get("categories") or []):
            tid = str(c.get("type_id") or "")
            name = str(c.get("type_name") or "")
            if tid and name:
                self._cats[tid] = name
        return self._cats

    # ---------------- 契约方法 ----------------

    def homeContent(self, filter):
        result = {"class": [], "list": []}
        j = self._api("/api.php/app/index/home")
        data = j.get("data") or {}

        classes = []
        for c in (data.get("categories") or []):
            tid = str(c.get("type_id") or "")
            name = str(c.get("type_name") or "")
            if tid and name:
                classes.append({"type_id": tid, "type_name": name})
                self._cats[tid] = name
        if not classes:
            classes = [{"type_id": "1", "type_name": "电影"},
                       {"type_id": "2", "type_name": "剧集"},
                       {"type_id": "3", "type_name": "动漫"},
                       {"type_id": "4", "type_name": "综艺"}]
        result["class"] = classes

        items = []
        for key in ("appCarouselVideos", "recommend", "webCarouselVideos"):
            items.extend(data.get(key) or [])
        for c in (data.get("categories") or []):
            items.extend(c.get("videos") or [])
        result["list"] = self._cards(items)
        return result

    def homeVideoContent(self):
        items = []
        j = self._api("/api.php/app/index/home")
        data = j.get("data") or {}
        for key in ("appCarouselVideos", "recommend", "webCarouselVideos"):
            items.extend(data.get(key) or [])
        if not items:
            for c in (data.get("categories") or []):
                items.extend(c.get("videos") or [])
        return {"list": self._cards(items)}

    def categoryContent(self, tid, pg, filter, extend):
        page = int(pg) if str(pg).isdigit() and int(pg) > 0 else 1
        cats = self._load_cats()
        name = cats.get(str(tid), "")
        if not name:  # 也允许直接传中文分类名
            if str(tid) in cats.values():
                name = str(tid)
            elif cats:
                name = list(cats.values())[0]
        if not name:
            name = "电影"

        url = "/api.php/app/filter/vod?type_name={}&page={}&sort=hits".format(
            self._enc(name), page)
        if isinstance(extend, dict):
            for k, v in extend.items():
                if v and v != "全部":
                    url += "&{}={}".format(self._enc(k), self._enc(v))
        j = self._api(url)
        lst = j.get("data") if isinstance(j.get("data"), list) else []
        return {"list": self._cards(lst), "page": page,
                "pagecount": 9999 if lst else page, "limit": 24,
                "total": 999999 if lst else 0}

    def detailContent(self, ids):
        result = {"list": []}
        vid = str(ids[0]).split(",")[0].strip()
        j = self._api("/api.php/app/vod/get_detail?vod_id=" + self._enc(vid))
        data = j.get("data")
        if not isinstance(data, list) or not data:
            return result
        v = data[0]

        flags = [f for f in (v.get("vod_play_from") or "").split("$$$") if f]
        urls = (v.get("vod_play_url") or "").split("$$$")
        pairs = [(f, urls[i] if i < len(urls) else "") for i, f in enumerate(flags)]
        if not pairs and v.get("vod_play_url"):
            pairs = [("线路1", v["vod_play_url"])]

        # 稳定性较差的线路排到末尾
        pairs = ([p for p in pairs if p[0] not in _LINE_DEPRIORITIZE] +
                 [p for p in pairs if p[0] in _LINE_DEPRIORITIZE])

        froms, plays = [], []
        for f, u in pairs:
            froms.append(f.replace("$", "＄").replace("#", "＃"))
            plays.append(u or "")

        content = re.sub(r"<[^>]+>", "", v.get("vod_content") or "").strip()
        result["list"] = [{
            "vod_id": vid,
            "vod_name": str(v.get("vod_name") or vid),
            "vod_pic": (v.get("vod_pic") or "").replace("\\/", "/"),
            "vod_year": str(v.get("vod_year") or ""),
            "vod_area": str(v.get("vod_area") or ""),
            "vod_director": str(v.get("vod_director") or ""),
            "vod_actor": str(v.get("vod_actor") or ""),
            "vod_content": content,
            "vod_remarks": str(v.get("vod_remarks") or ""),
            "vod_play_from": "$$$".join(froms),
            "vod_play_url": "$$$".join(plays),
        }]
        return result

    def searchContent(self, key, quick, pg="1"):
        result = {"list": []}
        kw = str(key or "").strip()
        if not kw:
            return result
        page = pg if str(pg).isdigit() and int(pg) > 0 else "1"
        j = self._api("/api.php/app/search/index?wd={}&page={}&limit=15".format(
            self._enc(kw), page))
        data = j.get("data")
        lst = data if isinstance(data, list) else (
            data.get("videos", []) if isinstance(data, dict) else [])
        result["list"] = self._cards(lst)
        return result

    def playerContent(self, flag, pid, vipFlags):
        result = {"parse": 0, "url": "", "header": {
            "User-Agent": _UA_PLAYER, "Referer": self.host + "/"}}
        ep = (pid or "").strip()
        if not ep or not flag:
            return result

        url = ""
        for i in range(2):  # 同一线路重试一次, 救间歇性失效
            url = self._decode(ep, flag)
            if url:
                break
            time.sleep(0.6)

        if not url:
            return result
        result["url"] = url
        if _NEED_PARSE.search(url):
            result["parse"] = 1
        return result

    def _decode(self, ep, flag):
        path = "/api.php/app/decode/url/?url={}&vodFrom={}&_t={}".format(
            self._enc(ep), self._enc(flag), int(time.time() * 1000))
        j = self._api(path)
        u = j.get("data")
        if isinstance(u, str) and u.startswith("http"):
            return u
        if j.get("msg"):
            print("decode[{}] failed: {}".format(flag, j.get("msg")))
        return ""

    # ---------------- 其它 ----------------

    def isVideoFormat(self, url):
        t = str(url or "").lower()
        return any(x in t for x in (".m3u8", ".mp4", ".flv", ".ts", ".mkv", ".webm", ".m4s"))

    def manualVideoCheck(self):
        return False

    def destroy(self):
        return None

    def localProxy(self, params):
        return None

    def proxy(self, params):
        return self.localProxy(params)


# ---------------------------------------------------------------- 自测

def _self_test():
    sp = Spider()
    sp.init("")
    print("getName      :", sp.getName())
    print("host         :", sp.host)
    print("device_id    :", sp.device_id)

    print("\n[1] homeContent")
    h = sp.homeContent(True)
    print("    分类:", [c["type_name"] for c in h.get("class", [])])
    print("    条目:", len(h.get("list", [])))
    for v in h.get("list", [])[:3]:
        print("      ", v["vod_id"], v["vod_name"], v["vod_remarks"])

    print("\n[2] categoryContent")
    if h.get("class"):
        tid = h["class"][0]["type_id"]
        c = sp.categoryContent(tid, "1", True, {})
        print("    type_id={} -> {} 条".format(tid, len(c.get("list", []))))

    print("\n[3] searchContent")
    target = None
    for kw in ("庆余年", "繁花"):
        s = sp.searchContent(kw, False, "1")
        print("    '{}' -> {} 条".format(kw, len(s.get("list", []))))
        if not target and s.get("list"):
            target = s["list"][0]
    if not target and h.get("list"):
        target = h["list"][0]

    print("\n[4] detailContent")
    d = sp.detailContent([str(target["vod_id"])])
    if not d.get("list"):
        print("    详情为空")
        return
    v = d["list"][0]
    flags = [f for f in (v["vod_play_from"] or "").split("$$$") if f]
    urls = (v["vod_play_url"] or "").split("$$$")
    print("    片名:", v["vod_name"], "| 线路:", flags)
    ep = urls[0].split("#")[0].split("$")[-1]
    print("    首集ID:", ep[:40])

    print("\n[5] playerContent")
    p = sp.playerContent(flags[0], ep, [])
    if p.get("url"):
        print("    ✅ 播放地址:", p["url"][:100])
        print("    parse:", p.get("parse"), "| UA:", p["header"]["User-Agent"][:40])
    else:
        print("    ❌ 未拿到播放地址")


if __name__ == "__main__":
    _self_test()