# -*- coding: utf-8 -*-
"""
凡客TV (m.fktv.me) —— OK影视爬虫插件
====================================
站点：Next.js SSR + 自有加密接口 /ysapi（POST + AES-128-ECB）
依赖：requests（必需）；pycryptodome（可选，缺失时回退纯 python AES）
"""

import sys
import re
import json
import time
import base64
import threading

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

requests.packages.urllib3.disable_warnings()

try:
    from Crypto.Cipher import AES as _PyCryptoAES
except Exception:
    _PyCryptoAES = None

# 核心：继承 OK影视内置 base.spider.Spider 类
from base.spider import Spider as BaseSpider


# ============================================================================
# 常量区
# ============================================================================

HOST = "https://m.fktv.me"

API_KEY = b"9ed1a661a6ab787a"
DEVICE_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
      "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1")

TIMEOUT_CONNECT = 10
TIMEOUT_READ = 20
RETRY_TIMES = 2

TTL_CATEGORY = 300
TTL_FILTER = 86400
TTL_DETAIL = 1800
CACHE_MAX = 512
DOMAIN_TTL = 600

_FALLBACK_DOMAINS = ["m.fktv.me", "fktv.me"]


CATS = [
    {
        "id": "movie", "name": "电影", "cat_id": "6",
        "subs": [
            ("喜剧片", ["299"]), ("动作片", ["308"]), ("爱情片", ["302"]),
            ("剧情片", ["313"]), ("科幻片", ["327"]), ("恐怖片", ["319"]),
            ("战争片", ["337"]), ("犯罪片", ["318"]), ("悬疑片", ["338"]),
            ("惊悚片", ["304"]), ("奇幻片", ["330"]), ("冒险片", ["320"]),
            ("灾难片", ["334"]), ("歌舞片", ["357"]), ("动画电影", ["333"]),
            ("网络电影", ["329"]), ("男男电影", ["297"]), ("百合电影", ["354"]),
            ("经典片", ["361"]),
        ],
    },
    {
        "id": "tv", "name": "电视剧", "cat_id": "5",
        "subs": [
            ("国产剧", ["296"]), ("港剧", ["324"]), ("台剧", ["314"]),
            ("韩剧", ["325"]), ("日剧", ["312"]), ("欧美剧", ["295"]),
            ("泰剧", ["311"]), ("新马剧", ["342"]), ("其他剧", ["310"]),
            ("男男剧", ["345"]), ("百合剧", ["315"]),
        ],
    },
    {
        "id": "comic", "name": "动漫", "cat_id": "7",
        "subs": [
            ("国产动漫", ["326"]), ("日本动漫", ["307"]), ("欧美动漫", ["323"]),
            ("港台动漫", ["362"]), ("韩国动漫", ["369"]), ("其他动漫", ["359"]),
            ("耽美动漫", ["317"]), ("男男动漫", ["343"]), ("百合动漫", ["371"]),
        ],
    },
    {
        "id": "zy", "name": "综艺", "cat_id": "4",
        "subs": [
            ("国产综艺", ["316"]), ("韩国综艺", ["328"]), ("港台综艺", ["336"]),
            ("欧美综艺", ["303"]), ("日本综艺", ["356"]), ("新马泰综艺", ["364"]),
            ("其他综艺", ["365"]), ("耽美综艺", ["370"]), ("电视直播", ["358"]),
        ],
    },
    {
        "id": "short_tv", "name": "短剧", "cat_id": "9",
        "subs": [
            ("逆袭", ["300"]), ("都市", ["322"]), ("重生", ["309"]),
            ("穿越", ["321"]), ("甜宠", ["301"]), ("虐恋", ["331"]),
            ("复仇", ["347"]), ("萌宝", ["344"]), ("年代", ["346"]),
            ("古代", ["348"]), ("乡村", ["353"]), ("强者", ["340"]),
            ("职场", ["341"]), ("架空", ["335"]), ("现代", ["339"]),
            ("民国", ["332"]), ("校园", ["349"]), ("宫廷", ["352"]),
            ("神豪", ["360"]), ("荒岛", ["373"]), ("古装", ["372"]),
        ],
    },
    {
        "id": "jlp", "name": "纪录片", "cat_id": "8",
        "subs": [("人文纪录", ["306"]), ("冒险纪实", ["320"])],
    },
    {
        "id": "js", "name": "电影解说", "cat_id": "11",
        "subs": [("影视解说", ["305"]), ("流行速看", ["298"])],
    },
]

ORDERS = [("new", "最近更新"), ("hot", "热门优先")]

LANGUAGES = [
    ("", "全部语言"), ("1", "国语"), ("2", "粤语"), ("3", "英语"),
    ("4", "韩语"), ("5", "日语"), ("6", "法语"), ("7", "泰语"), ("8", "其他语"),
]

YEARS = ["", "2026", "2025", "2024", "2023", "2022", "2021", "2020",
         "2019", "2018", "2017"]


def _build_filters(cat):
    subs = cat.get("subs") or []
    type_items = [{"n": "全部分类", "v": ""}]
    for name, tag_ids in subs:
        type_items.append({"n": name, "v": ",".join(tag_ids)})

    return [
        {"key": "type", "name": "分类",
         "value": [{"n": it["n"], "v": it["v"]} for it in type_items]},
        {"key": "year", "name": "年份",
         "value": [{"n": (y or "全部年份"), "v": y} for y in YEARS]},
        {"key": "lang", "name": "语言",
         "value": [{"n": n, "v": v} for v, n in LANGUAGES]},
        {"key": "order", "name": "排序",
         "value": [{"n": n, "v": v} for v, n in ORDERS]},
    ]


CAT_INDEX = {c["id"]: c for c in CATS}
# filters 以 type_id 为 key，跟 class 列表一一对应
ALL_FILTERS = {c["id"]: _build_filters(c) for c in CATS}


# ============================================================================
# AES-128-ECB（pycryptodome 优先，缺失时纯 python 回退）
# ============================================================================

_SBOX = []
_INV_SBOX = []
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _init_sbox():
    if _SBOX:
        return
    p = q = 1
    sbox = [0] * 256
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        xformed = (q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6))
                   ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4)))
        sbox[p] = (xformed ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    _SBOX.extend(sbox)
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    _INV_SBOX.extend(inv)


_init_sbox()


def _xtimes(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gf_mul(a, b):
    res = 0
    while b:
        if b & 1:
            res ^= a
        a = _xtimes(a)
        b >>= 1
    return res


def _key_expansion(key):
    nk = len(key) // 4
    nr = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (nr + 1)):
        temp = list(w[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        w.append([w[i - nk][j] ^ temp[j] for j in range(4)])
    return w, nr


def _encrypt_block(block, w, nr):
    s = [block[r + 4 * c] for c in range(4) for r in range(4)]

    def add_round_key(rnd):
        for c in range(4):
            for r in range(4):
                s[c * 4 + r] ^= w[rnd * 4 + c][r]

    add_round_key(0)
    for rnd in range(1, nr + 1):
        s = [_SBOX[b] for b in s]
        ns = list(s)
        for r in range(1, 4):
            row = [s[4 * c + r] for c in range(4)]
            row = row[r:] + row[:r]
            for c in range(4):
                ns[4 * c + r] = row[c]
        s = ns
        if rnd != nr:
            nc = []
            for c in range(4):
                col = s[4 * c:4 * c + 4]
                nc.extend([
                    _gf_mul(col[0], 2) ^ _gf_mul(col[1], 3) ^ col[2] ^ col[3],
                    col[0] ^ _gf_mul(col[1], 2) ^ _gf_mul(col[2], 3) ^ col[3],
                    col[0] ^ col[1] ^ _gf_mul(col[2], 2) ^ _gf_mul(col[3], 3),
                    _gf_mul(col[0], 3) ^ col[1] ^ col[2] ^ _gf_mul(col[3], 2),
                ])
            s = nc
        add_round_key(rnd)
    return bytes(s)


def _decrypt_block(block, w, nr):
    s = [block[r + 4 * c] for c in range(4) for r in range(4)]

    def add_round_key(rnd):
        for c in range(4):
            for r in range(4):
                s[c * 4 + r] ^= w[rnd * 4 + c][r]

    add_round_key(nr)
    for rnd in range(nr - 1, -1, -1):
        ns = list(s)
        for r in range(1, 4):
            row = [s[4 * c + r] for c in range(4)]
            row = row[-r:] + row[:-r]
            for c in range(4):
                ns[4 * c + r] = row[c]
        s = ns
        s = [_INV_SBOX[b] for b in s]
        add_round_key(rnd)
        if rnd != 0:
            nc = []
            for c in range(4):
                col = s[4 * c:4 * c + 4]
                nc.extend([
                    _gf_mul(col[0], 14) ^ _gf_mul(col[1], 11) ^ _gf_mul(col[2], 13) ^ _gf_mul(col[3], 9),
                    _gf_mul(col[0], 9) ^ _gf_mul(col[1], 14) ^ _gf_mul(col[2], 11) ^ _gf_mul(col[3], 13),
                    _gf_mul(col[0], 13) ^ _gf_mul(col[1], 9) ^ _gf_mul(col[2], 14) ^ _gf_mul(col[3], 11),
                    _gf_mul(col[0], 11) ^ _gf_mul(col[1], 13) ^ _gf_mul(col[2], 9) ^ _gf_mul(col[3], 14),
                ])
            s = nc
    return bytes(s)


def _pkcs7_pad(data, bs=16):
    n = bs - len(data) % bs
    return data + bytes([n]) * n


def _pkcs7_unpad(data):
    if not data:
        return data
    n = data[-1]
    if 1 <= n <= 16 and len(data) >= n and data[-n:] == bytes([n]) * n:
        return data[:-n]
    return data


class _PureAesECB(object):
    def __init__(self, key):
        self.w, self.nr = _key_expansion(list(key))

    def encrypt(self, data):
        out = b""
        for i in range(0, len(data), 16):
            out += _encrypt_block(data[i:i + 16], self.w, self.nr)
        return out

    def decrypt(self, data):
        out = b""
        for i in range(0, len(data), 16):
            blk = data[i:i + 16]
            if len(blk) < 16:
                break
            out += _decrypt_block(blk, self.w, self.nr)
        return out


def _aes_cipher():
    if _PyCryptoAES is not None:
        return _PyCryptoAES.new(API_KEY, _PyCryptoAES.MODE_ECB)
    return _PureAesECB(API_KEY)


def aes_encrypt(plain):
    cipher = _aes_cipher()
    return base64.b64encode(cipher.encrypt(_pkcs7_pad(plain.encode("utf-8")))).decode("ascii")


def aes_decrypt(text):
    cipher = _aes_cipher()
    raw = base64.b64decode(text)
    return _pkcs7_unpad(cipher.decrypt(raw)).decode("utf-8", "replace")


# ============================================================================
# 工具
# ============================================================================

def _safe(v, default=""):
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def _host_of(url_or_host):
    s = _safe(url_or_host).strip()
    if not s:
        return ""
    if s.startswith("http"):
        m = re.match(r"https?://([^/]+)", s)
        return m.group(1) if m else ""
    return s.split("/")[0]


def _norm_dict(x):
    """把 dict / JSON 字符串 / None 统一成 dict。"""
    if x is None:
        return {}
    if isinstance(x, dict):
        return x
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return {}
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


class _Cache(object):
    def __init__(self, maxsize=CACHE_MAX):
        self._d = {}
        self._order = []
        self._lock = threading.Lock()
        self._max = maxsize

    def get(self, key):
        with self._lock:
            item = self._d.get(key)
            if not item:
                return None
            ts, val, ttl = item
            if time.time() - ts > ttl:
                self._d.pop(key, None)
                if key in self._order:
                    self._order.remove(key)
                return None
            return val

    def set(self, key, val, ttl):
        with self._lock:
            if key not in self._d and len(self._d) >= self._max:
                for old in self._order[: max(1, self._max // 8)]:
                    self._d.pop(old, None)
                    if old in self._order:
                        self._order.remove(old)
            self._d[key] = (time.time(), val, ttl)
            if key in self._order:
                self._order.remove(key)
            self._order.append(key)


class _DomainPool(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.current = _host_of(HOST) or "m.fktv.me"
        self.pool = list(_FALLBACK_DOMAINS)
        self.fail_at = {}
        self.refresh_at = 0.0

    def pick(self):
        now = time.time()
        with self.lock:
            for h in self.pool:
                if now - self.fail_at.get(h, 0) > DOMAIN_TTL:
                    return h
            return self.pool[0] if self.pool else self.current

    def mark_failed(self, host):
        with self.lock:
            self.fail_at[host] = time.time()
            if host in self.pool:
                self.pool.remove(host)
                self.pool.append(host)

    def set_active(self, host):
        if not host:
            return
        with self.lock:
            self.current = host
            if host in self.pool:
                self.pool.remove(host)
            self.pool.insert(0, host)
            self.fail_at.pop(host, None)

    def merge(self, hosts):
        with self.lock:
            merged = [self.current] + list(hosts) + self.pool + list(_FALLBACK_DOMAINS)
            seen, out = set(), []
            for h in merged:
                if h and h not in seen:
                    seen.add(h)
                    out.append(h)
            self.pool = out

    def snapshot(self):
        with self.lock:
            return list(self.pool)


# ============================================================================
# 数据转换
# ============================================================================

def _pic(item):
    for k in ("img_x_source", "img_y_source", "img_x", "img_y"):
        u = _safe(item.get(k))
        if u.startswith("http") and not u.endswith(".bnc"):
            return u
    return ""


def _remark(item):
    links = item.get("links") or []
    release = _safe(item.get("release_at"))
    score = _safe(item.get("score"))
    note = ""
    if isinstance(links, list) and len(links) > 1:
        note = "更新至%s集" % len(links)
    lang = _safe(item.get("language"))
    area = _safe(item.get("area"))
    tail = "·".join([x for x in (release, area or lang) if x])
    parts = [x for x in (note or tail,
                         ("评分" + score) if score and score != "0.00" else "") if x]
    return " ".join(parts)


def _to_list_item(item):
    vid = _safe(item.get("id") or item.get("video_id"))
    if not vid:
        return None
    return {
        "vod_id": vid,
        "vod_name": _safe(item.get("name"), "未知"),
        "vod_pic": _pic(item),
        "vod_remarks": _remark(item),
        "style": {"type": "rect", "ratio": 0.65},
    }


def _iter_items(node):
    out = []
    if isinstance(node, dict):
        if node.get("id") and node.get("name") and (node.get("img_x") or node.get("canonical_path")):
            out.append(node)
        else:
            for v in node.values():
                out.extend(_iter_items(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_iter_items(v))
    return out


def _dedup(items):
    seen = set()
    res = []
    for it in items:
        if not it:
            continue
        vid = it.get("vod_id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        res.append(it)
    return res


def _build_video_list(raw_list):
    return _dedup([x for x in (_to_list_item(i) for i in raw_list if isinstance(i, dict)) if x])


# ============================================================================
# Spider
# ============================================================================

class Spider(BaseSpider):

    # ---------- 基础配置 ----------
    def getName(self):
        return "凡客TV"

    def init(self, extend=""):
        super().init(extend)

        self.site_url = HOST
        self.page_size = 15
        self.total = 9999
        self.debug = False

        self.headers = {
            "User-Agent": UA,
            "Content-Type": "application/octet-stream",
            "version": "1.0",
            "deviceType": "h5",
            "Referer": HOST + "/",
            "Accept": "*/*",
            "Origin": HOST,
        }

        self.sess = requests.Session()
        try:
            adapter = HTTPAdapter(
                pool_connections=8, pool_maxsize=16,
                max_retries=Retry(total=2, backoff_factor=0.5,
                                  status_forcelist=[500, 502, 503, 504]))
            self.sess.mount("https://", adapter)
            self.sess.mount("http://", adapter)
        except Exception as exc:
            self._log("mount adapter failed:", exc)
        self.sess.headers.update(self.headers)

        self.cache = _Cache()
        self.domains = _DomainPool()

    def _log(self, *args):
        if getattr(self, "debug", False):
            try:
                print("[凡客TV]", *args, file=sys.stderr)
            except Exception:
                pass

    # ---------- GET 兜底 ----------
    def fetch(self, url, timeout=10):
        try:
            return self.sess.get(url, headers=self.headers, timeout=timeout, verify=False)
        except Exception as exc:
            self._log("GET 失败:", url, exc)
            return None

    # ========== 底层 API（单次调用，不轮换域名） ==========
    def _api_raw_ex(self, endpoint, data=None, host=None, retry=0):
        host = host or self.domains.current
        url = "https://" + host + "/ysapi/" + endpoint
        payload = {
            "deviceId": DEVICE_ID,
            "token": "",
            "domain": host,
            "referer": "https://" + host + "/",
            "user_agent": UA,
            "shareCode": "",
            "channel": "",
            "ip": "",
            "data": data or {},
        }
        body = aes_encrypt(json.dumps(payload, ensure_ascii=False))
        headers = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}

        last_kind = "empty"
        for attempt in range(retry + 1):
            try:
                resp = self.sess.post(
                    url, data=body.encode("utf-8"), headers=headers,
                    timeout=(TIMEOUT_CONNECT, TIMEOUT_READ), verify=False)
                status = resp.status_code
                text = (resp.text or "").strip()

                if status >= 500 or status == 0:
                    last_kind = "network"
                    continue
                if status != 200:
                    last_kind = "http"
                    self._log(endpoint, "status", status)
                    continue
                if not text:
                    last_kind = "empty"
                    continue
                if text[0] not in "{[":
                    text = aes_decrypt(text)
                obj = json.loads(text)
                return (obj if isinstance(obj, dict) else {"data": obj}), "ok"
            except requests.exceptions.RequestException as exc:
                last_kind = "network"
                self._log(endpoint, "net err on", host, ":", exc)
                time.sleep(0.3 * (attempt + 1))
            except Exception as exc:
                last_kind = "parse"
                self._log(endpoint, "parse err:", exc)
                time.sleep(0.3 * (attempt + 1))
        return None, last_kind

    def _api_raw(self, endpoint, data=None, host=None, retry=0):
        obj, _ = self._api_raw_ex(endpoint, data, host=host, retry=retry)
        return obj if obj is not None else {}

    # ========== API 主入口（缓存 + 域名轮换） ==========
    def api(self, endpoint, data=None, retry=RETRY_TIMES):
        cache_key = "api:" + endpoint + ":" + json.dumps(data or {}, sort_keys=True, ensure_ascii=False)
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        tried = set()
        last_err = None
        pool = self.domains.snapshot()

        for _ in range(max(len(pool), 1)):
            host = self.domains.pick()
            if host in tried:
                break
            tried.add(host)

            obj, err_kind = self._api_raw_ex(endpoint, data, host=host, retry=retry)
            if obj is not None:
                self.domains.set_active(host)
                if endpoint == "movie/detail":
                    ttl = TTL_DETAIL
                elif endpoint in ("movie/filter", "movie/tags", "system/info"):
                    ttl = TTL_FILTER
                else:
                    ttl = TTL_CATEGORY
                self.cache.set(cache_key, obj, ttl)
                if endpoint != "system/info":
                    self._maybe_refresh_pool()
                return obj

            last_err = err_kind
            if err_kind == "network":
                self.domains.mark_failed(host)
                self._log(endpoint, "network fail on", host, "-> rotate domain")
                continue
            break

        self._log(endpoint, "FAILED:", last_err)
        return {}

    # ---------- 域名池刷新 ----------
    def _refresh_domain_pool(self):
        try:
            info = self._api_raw("system/info", {}, host=self.domains.current)
            d = info.get("data") or {}
            found = []

            h = _host_of(d.get("main_url"))
            if h:
                found.append(h)

            cd = d.get("channel_domains") or []
            if isinstance(cd, list):
                for it in cd:
                    if isinstance(it, str):
                        hh = _host_of(it)
                    else:
                        it = it or {}
                        hh = _host_of(it.get("url") or it.get("domain"))
                    if hh:
                        found.append(hh)

            self.domains.merge(found)
        except Exception as exc:
            self._log("refresh domain pool failed:", exc)

    def _maybe_refresh_pool(self):
        now = time.time()
        if now - self.domains.refresh_at > DOMAIN_TTL:
            self.domains.refresh_at = now
            self._refresh_domain_pool()

    # ========== 首页分类接口 ==========
    def homeContent(self, filter):
        """
        首页分类：只返回 OK影视 解析器认识的 type_name / type_id，
        filters 单独 try 保护，绝不因 filters 出错导致 class 变空。
        """
        try:
            classes = []
            for c in CATS:
                if not c.get("id") or not c.get("name"):
                    continue
                classes.append({
                    "type_name": c["name"],
                    "type_id": c["id"],
                })
            if not classes:
                classes = [{"type_name": "电影", "type_id": "movie"}]
        except Exception as exc:
            self._log("homeContent class build error:", exc)
            classes = [{"type_name": "电影", "type_id": "movie"}]

        result = {"class": classes}

        # filters 单独构建，出错也不影响 class
        try:
            if ALL_FILTERS:
                result["filters"] = ALL_FILTERS
        except Exception as exc:
            self._log("homeContent filters error:", exc)

        return result

    # ========== 首页推荐（可选） ==========
    def homeVideoContent(self):
        try:
            vids = []
            for cat in CATS[:4]:
                data = self.api("movie/channel", {"code": cat["id"]})
                blocks = (data.get("data") or {}).get("block") or []
                for blk in blocks[:3]:
                    vids.extend(_build_video_list(_iter_items(blk.get("items"))))
                if len(vids) > 60:
                    break
            return {"list": _dedup(vids)[:60]}
        except Exception as exc:
            self._log("homeVideoContent error:", exc)
            return {"list": []}

    # ========== 分类列表接口 ==========
    def categoryContent(self, tid, pg, filter, extend):
        try:
            pg = int(pg) if str(pg).isdigit() else 1
            cat = CAT_INDEX.get(tid) or CATS[0]
            ext = _norm_dict(extend)

            params = {
                "cat_id": cat["cat_id"],
                "page": pg,
                "order": _safe(ext.get("order"), "new"),
            }

            tag = _safe(ext.get("type"))
            if tag:
                params["tag_id"] = tag

            year = _safe(ext.get("year"))
            if year:
                params["year"] = year

            lang = _safe(ext.get("lang"))
            if lang:
                params["language"] = lang

            data = self.api("movie/search", params)
            d = data.get("data") or {}
            vids = _build_video_list(d.get("data") or [])

            # 第 1 页空数据时回退频道板块，保证首屏不空白
            if not vids and pg == 1:
                ch = self.api("movie/channel", {"code": cat["id"]})
                blocks = (ch.get("data") or {}).get("block") or []
                for blk in blocks:
                    f = blk.get("filter")
                    try:
                        fj = json.loads(f) if isinstance(f, str) else (f or {})
                    except Exception:
                        fj = {}
                    if tag and fj.get("tag_id") and fj.get("tag_id") != tag:
                        continue
                    vids.extend(_build_video_list(_iter_items(blk.get("items"))))
                vids = _dedup(vids)

            try:
                pagecount = int(d.get("last_page") or 1)
            except Exception:
                pagecount = 1

            try:
                total = int(d.get("total") or len(vids))
            except Exception:
                total = len(vids)

            return {
                "list": vids,
                "page": pg,
                "pagecount": max(pagecount, 1),
                "limit": self.page_size,
                "total": total,
            }
        except Exception as exc:
            self._log("categoryContent error:", exc)
            return {
                "list": [], "page": 1, "pagecount": 1,
                "limit": self.page_size, "total": 0,
            }

    # ========== 视频详情接口 ==========
    def detailContent(self, ids):
        try:
            vod_id = _safe(ids if isinstance(ids, str) else (ids or [""])[0]).split("@")[0]
            if not vod_id:
                return {"list": [{"vod_name": "视频ID为空"}]}

            data = self.api("movie/detail", {"id": vod_id})
            d = data.get("data") or {}
            if not d:
                return {"list": [{"vod_id": vod_id, "vod_name": "视频详情解析失败"}]}

            links = d.get("links") or []
            play_links = d.get("play_links") or []

            # 线路名取自 playback_v2.video_lines
            pb2 = d.get("playback_v2") or {}
            line_names = [_safe(l.get("name")) for l in (pb2.get("video_lines") or [])
                          if (l.get("h264") or {}).get("url")]
            if not line_names:
                line_names = [_safe(pl.get("name"), "线路%d" % (i + 1))
                              for i, pl in enumerate(play_links)]
            if not line_names:
                line_names = ["国内线路"]

            episodes = []
            if isinstance(links, list) and links:
                for lk in links:
                    episodes.append((_safe(lk.get("name"), "正片"), _safe(lk.get("id"))))
            else:
                episodes.append((_safe(d.get("name"), "正片"), ""))

            def _ep_key(pair):
                nums = re.findall(r"\d+", pair[0])
                return int(nums[-1]) if nums else 10 ** 9

            if any(re.search(r"\d+", n) for n, _ in episodes):
                episodes = sorted(episodes, key=_ep_key)

            playlists = []
            for _ in range(len(line_names)):
                playlists.append("#".join(["%s$%s@%s" % (n, vod_id, lid) for n, lid in episodes]))

            vod_play_from = "$$$".join(line_names) or "凡客·官方"
            vod_play_url = "$$$".join(playlists) if playlists else "正片$%s@" % vod_id

            tags = d.get("tags") or []
            tag_names = " ".join([_safe(t.get("name")) for t in tags if isinstance(t, dict)])

            detail = {
                "vod_id": vod_id,
                "vod_name": _safe(d.get("name"), "未知"),
                "vod_pic": _pic(d),
                "type_name": _safe(d.get("categories") or d.get("child_title")),
                "vod_year": _safe(d.get("release_at")),
                "vod_area": _safe(d.get("area")),
                "vod_lang": _safe(d.get("language")),
                "director": _safe(d.get("director")),
                "actor": _safe(d.get("actor")),
                "vod_remarks": _remark(d),
                "vod_content": re.sub(r"\s+", " ", _safe(d.get("description"))).strip(),
                "vod_tag": tag_names,
                "vod_play_from": vod_play_from,
                "vod_play_url": vod_play_url,
            }
            return {"list": [detail]}
        except Exception as exc:
            self._log("detailContent error:", exc)
            return {"list": [{"vod_name": "详情异常"}]}

    # ========== 搜索接口 ==========
    def searchContent(self, key, quick, pg="1"):
        try:
            kw = _safe(key).strip()
            pg = int(pg) if str(pg).isdigit() else 1
            if not kw:
                return {"list": [], "page": pg, "pagecount": 1,
                        "limit": self.page_size, "total": 0}

            data = self.api("movie/search", {"keywords": kw, "page": pg, "order": "new"})
            d = data.get("data") or {}
            vids = _build_video_list(d.get("data") or [])

            # 整词无结果时去掉常见助词再宽松匹配一次
            if not vids and len(kw) > 2:
                loose = re.sub(r"[的了呢吗啊吧与和及]", "", kw)
                if loose and loose != kw:
                    alt = self.api("movie/search", {"keywords": loose, "page": pg, "order": "new"})
                    ad = alt.get("data") or {}
                    vids = _build_video_list(ad.get("data") or [])
                    if vids:
                        d = ad

            try:
                pagecount = int(d.get("last_page") or (1 if vids else 0))
            except Exception:
                pagecount = 1

            try:
                total = int(d.get("total") or len(vids))
            except Exception:
                total = len(vids)

            return {
                "list": vids,
                "page": pg,
                "pagecount": max(pagecount, 1),
                "limit": self.page_size,
                "total": total,
            }
        except Exception as exc:
            self._log("searchContent error:", exc)
            return {"list": [], "page": 1, "pagecount": 1,
                    "limit": self.page_size, "total": 0}

    # ========== 播放解析接口 ==========
    def playerContent(self, flag, id, vipFlags):
        try:
            raw = _safe(id)
            if "$" in raw:
                raw = raw.rsplit("$", 1)[-1]

            vid, _, link_id = raw.partition("@")
            if not vid:
                return {"parse": 0, "url": "", "header": self.headers, "msg": "参数错误"}

            params = {"id": vid}
            if link_id:
                params["link_id"] = link_id

            data = self.api("movie/detail", params)
            d = data.get("data") or {}
            if not d:
                return {"parse": 0, "url": "", "header": self.headers, "msg": "获取播放信息失败"}

            headers = {
                "User-Agent": UA,
                "Referer": "https://" + self.domains.current + "/",
            }

            line_idx = self._line_index(d, flag)
            url = self._pick_site(d, line_idx) or self._pick_site(d, 0)
            if not url:
                return {"parse": 0, "url": "", "header": headers, "msg": "暂无可用播放地址"}

            # 站内直链无需二次解析
            return {"parse": 0, "url": url, "header": headers}
        except Exception as exc:
            self._log("playerContent error:", exc)
            return {"parse": 0, "url": "", "header": self.headers, "msg": "播放异常"}

    @staticmethod
    def _line_index(d, flag):
        """播放器传入的线路名(flag) -> play_links 下标。"""
        f = _safe(flag)

        names = [_safe(pl.get("name")) for pl in (d.get("play_links") or [])]
        if f and f in names:
            return names.index(f)

        pb2 = d.get("playback_v2") or {}
        vnames = [_safe(l.get("name")) for l in (pb2.get("video_lines") or [])]
        if f and f in vnames:
            return vnames.index(f)

        return 0

    @staticmethod
    def _pick_site(d, line_idx=0):
        """站内直链：按 line_idx 选第 N 条 play_link；越界回落首条可用。"""
        site_lines = [pl for pl in (d.get("play_links") or [])
                      if _safe(pl.get("m3u8_url")).startswith("http")]
        if not site_lines:
            return ""
        pl = site_lines[line_idx] if 0 <= line_idx < len(site_lines) else site_lines[0]
        return _safe(pl.get("m3u8_url"))