# 搜剧AI(白嫖者 ai.baipiaozhe.com / souju.ai) OK影视/影视仓专用采集插件
# 形态: type=3 Python 插件, 基于 base.spider, 接口全部为站点自有 JSON API(/v1/)
# 说明: 本插件复刻了站点前端的请求签名(Header: x-ai-movie-timestamp/nonce/signature),
#       若站点更新密钥或 build 版本, 同步修改下方 SIGN_SECRET / BUILD_VERSION 即可。
#
# 【播放恢复修复 2026-11-17】
#   故障根因: 站点 CDN(CloudFront) 对连接做不稳定处理, TLS 握手会间歇性抛
#             SSLError(UNEXPECTED_EOF_WHILE_READING) / 连接被重置; 旧版 fetch()
#             把异常直接吞掉返回 None, 于是一次抖动就导致分类为空、详情/播放链拿不到
#             token, 表现为"无法播放"。同时站点主域名已由 ai.baipiaozhe.com 切到
#             souju.ai(canonical), 两域名后端相同、签名均有效。
#   修复内容: 1) 请求层改为「域名 failover + 连接级重试」, 每次重试重建签名(nonce/ts);
#             2) 主/备域名自动切换(souju.ai <-> ai.baipiaozhe.com);
#             3) 流探测同样加重试容错, 避免单条线路抖动被误判为不可播;
#             4) 成功过的域名被记住并优先使用。
#   注意: 签名 payload 仅由 method+path+ts+nonce 组成, 与域名无关, 故切域名不影响签名。
import sys
import re
import json
import time
import hmac
import hashlib
import secrets
import threading
import urllib.parse
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
requests.packages.urllib3.disable_warnings()

from base.spider import Spider

# ========== 站点请求签名配置(从站点前端 bundle 提取) ==========
SIGN_SECRET = "f39d73aa7a6426203cdee1ef17b31d3b7ea8c23f4c59c62a3a8aa0f39ee5e79d"  # HMAC-SHA256 密钥
CLIENT_NAME = "movie-search-frontend"
CLIENT_VERSION = "1.0.0"
BUILD_VERSION = "aimovie-v2026.09.18.1-50ffdb156742-web"
# ========== 登录凭证(可选, 建议用浏览器 Cookie, 不要把账号密码写进文件) ==========
# SITE_COOKIE: 登录 souju.ai 后 F12 -> Network -> 任一 /v1/ 请求 -> 复制整行 Cookie
# SITE_BEARER: 若站点把登录态放在 Authorization: Bearer xxx 里, 这里只填 xxx 部分
SITE_COOKIE = "ai_movie_session=ums_OzhpapJgpylw5jAbEJ8qQk70gDGef4OWX426YsA-t3E; ai_movie_browser=brw_UeTbgDyqJ14aP0QBnxkbBNhbjB3Ai-qwWPmQiUvj6so"
SITE_BEARER = ""
# 官方线路(1080P-官方S / 高清-官方B 等)只有登录后才下发 resolve://ticket。
# 填了账号密码则 init 时自动登录换 session, 不再依赖手工 Cookie; 留空则只用 SITE_COOKIE。
SITE_ACCOUNT = ""
SITE_PASSWORD = ""
PROTOCOL_VERSION = "2026-07-05.library-v2.playback-v1"

# ========== 播放恢复: 域名与重试配置 ==========
SITE_HOSTS = [
    "https://souju.ai",                # 现用主域名(canonical), 优先
    "https://ai.baipiaozhe.com",       # 旧域名, 仍可用, 作为备份
]
REQUEST_RETRY = 2       # 每个域名的连接级重试次数(3->2: 宁可快失败, 也不要拖 12s)
RETRY_BACKOFF = 0.3     # 重试退避基数(秒)
REQUEST_TIMEOUT = 7     # 单次 API 请求超时(12->7: 壳侧 10s 就会判超时)
API_TIMEOUT_FAST = 4    # 列表/搜索/分集这类"拿不到就先不用"的请求

# ========== 速度相关: 缓存与预算 ==========
# 本版不做任何缓存(播放/详情/分类/搜索都不保留旧数据)
PROBE_BUDGET = 1.2      # 兼容保留(本版不用完整探测)
QUICK_TIMEOUT = 1.2     # 播放前"轻校验"单次超时(只发 1 个 Range 请求)
QUICK_MAX = 2           # 最多校验几条候选, 其余不校验直接顺位
STRICT_LINE = 1         # 1=只播用户点的那条线路: 该源不可用就返回空(壳会提示失败), 绝不自动换源
                        # 0=原行为: 点的那条不通就自动顺位换别的源(会"自己跳到另一个源直接播")
DETAIL_BUDGET = 2.2     # 详情页所有季/集的总预算(秒)

# ========== 线路展示 ==========
MULTI_LINE = 1          # 1 = 把站点多源拆成多条线路(搜剧AI·红牛 / 搜剧AI·速播 ...)
                        # 0 = 原样只显示一条 "搜剧AI"
MAX_LINES = 0           # 线路显示上限: 0 = 不限制(站点给几条列几条)
MAX_LINES_LONG = 0      # 长剧的显示上限: 0 = 同样不限制
LONG_SERIES_EPISODES = 120
PROVIDER_SAMPLE_EPISODES = 2   # 采样几集取线路并集(首集够 2 条就不再多取, 见 _movie_providers)
PROVIDER_BUDGET = 1.2          # 采样并集的总等待上限(并发, 不是每集累加)

# 分类映射: kind -> 中文名称(站内 feed/browse 的 content_kind)
KIND_MAP = {
    "latest": "最新更新",
    "movie": "电影",
    "series": "电视剧",
    "short_drama": "短剧",
    "anime": "动漫",
    "variety": "综艺",
    "documentary": "纪录片",
    "sports": "体育"
}
DEFAULT_KINDS = ["latest", "movie", "series", "short_drama", "anime", "variety", "documentary", "sports"]

# 播放线路筛选: 按实际可用性排序(金鹰等部分源域名不稳定/失效, 已后置);
# 返回前还会做可达性探测, 命中 200 的线路才交付播放器
PREFER_PROVIDER_KEYWORDS = [
    "豪华", "红牛", "速播", "暴风", "新浪", "ikun",
    "量子", "牛牛", "非凡", "无尽", "极速", "最大", "无水印",
    "豆瓣", "360", "猫眼", "金鹰",
]
PROBE_MAX = 3           # 播放前最多探测几条候选(并发请求越少, 越不抢播放器带宽)
PROBE_TIMEOUT = 1.5     # 单次探测超时(秒)
PREFER_SEEKABLE = 1     # 优先可拖动(VOD)的源: 无 ENDLIST 的直播型清单快进会卡死
PREFER_MP4 = 1          # 有 mp4 直链时优先它: Range 定位, 快进几乎瞬时(同 hint 内生效)
SEARCH_POSTER_TIMEOUT = 2  # 搜索结果补海报总等待上限(秒, 5->2)

MAX_EPISODES = 300      # 单剧最多写入的集数
MAX_SEASONS = 8         # 单剧最多拉取的季数


class Spider(Spider):
    # ========== 插件名称 ==========
    def getName(self):
        return "搜剧AI"

    # ========== 初始化 ==========
    def init(self, extend=""):
        super().init(extend)
        self.site_hosts = list(SITE_HOSTS)
        self.site_url = self.site_hosts[0]
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "x-ai-movie-client-name": CLIENT_NAME,
            "x-ai-movie-client-version": CLIENT_VERSION,
            "x-ai-movie-build-version": BUILD_VERSION,
            "x-ai-movie-protocol-version": PROTOCOL_VERSION,
        }
        # 有登录凭证就带上: 官方/专享线路(resolve://ticket)通常要求登录态
        if SITE_COOKIE:
            self.headers["Cookie"] = SITE_COOKIE
        if SITE_BEARER:
            self.headers["Authorization"] = "Bearer " + SITE_BEARER
        # ========== 官方线路前提: 登录态 ==========
        # 未登录时 resolve 只给第三方直链, 官方 resolve://ticket 根本不下发
        if SITE_ACCOUNT and SITE_PASSWORD:
            self._login()
        # 会话保持 + 请求重试(状态码级)
        self.sess = requests.Session()
        for scheme in ("https://", "http://"):
            # 重试只保留我们自己的「域名 failover + 连接重试」, 这里必须 0,
            # 否则一次抖动会被 urllib3 再放大成 (1+2)=3 倍请求
            self.sess.mount(scheme, HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0))
        self.page_size = 20
        # ========== 速度优化: 进程内缓存 ==========
        # 本版不做任何缓存: 分类/搜索/详情/播放每次现取, 不保留任何旧直链
        # 探测单独用一个会话, 复用 TLS 连接, 避免每条线路都重新握手
        self._probe_sess = requests.Session()
        try:
            ad = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
            self._probe_sess.mount("https://", ad)
            self._probe_sess.mount("http://", ad)
        except Exception:
            pass
        self._probe_sess.headers.update({
            "User-Agent": self.headers["User-Agent"],
            "Accept": "*/*",
            "Connection": "keep-alive",
        })
        if self.headers.get("Cookie"):
            self._probe_sess.headers["Cookie"] = self.headers["Cookie"]
        if SITE_BEARER:
            self._probe_sess.headers["Authorization"] = "Bearer " + SITE_BEARER

    def _login(self):
        """账号密码 -> POST /v1/users/password/login, 成功后把新 session 写回 Cookie 头。
        这一步是官方线路(ticket)能下发的前提; 失败则回退到 SITE_COOKIE。"""
        try:
            url = self.site_hosts[0] + "/v1/users/password/login"
            headers = dict(self.headers)
            headers.update(self._sign_headers("POST", url))
            headers["Content-Type"] = "application/json"
            res = requests.post(url, data=json.dumps({"account": SITE_ACCOUNT,
                                                      "password": SITE_PASSWORD}).encode("utf-8"),
                                headers=headers, timeout=REQUEST_TIMEOUT, verify=False)
            if res.status_code not in (200, 201):
                return False
            # 让 requests 的 cookie jar 接管登录态, 再回写成 Cookie 头(其余请求走 _request 的固定头)
            cookie = "; ".join("%s=%s" % (c.name, c.value) for c in res.cookies)
            if cookie:
                self.headers["Cookie"] = cookie
                if hasattr(self, "_probe_sess"):
                    self._probe_sess.headers["Cookie"] = cookie
            self._logged_in = True
            return True
        except Exception:
            return False

    # ========== 站点 API 签名 ==========
    def _sign_headers(self, method, url):
        """复刻前端 HMAC 签名: payload = METHOD\npath[?query]\ntimestamp\nnonce"""
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        if parsed.query:
            path = path + "?" + parsed.query
        ts = str(int(time.time() * 1000))
        nonce = secrets.token_hex(16)
        payload = "{}\n{}\n{}\n{}".format(method.upper(), path, ts, nonce)
        sig = hmac.new(SIGN_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return {
            "x-ai-movie-timestamp": ts,
            "x-ai-movie-nonce": nonce,
            "x-ai-movie-signature": sig,
        }

    # ========== 播放恢复: 带域名 failover + 连接级重试的统一请求 ==========
    def _request(self, method, path, params=None, body=None, timeout=REQUEST_TIMEOUT):
        """遍历 域名×重试, 每次重建签名; 返回 (status_code, response); 全失败返回 (None, None)。
        签名 payload 只含 method+path+ts+nonce, 与 host 无关, 因此切换域名安全。"""
        url_path = path
        if params:
            url_path = path + "?" + urllib.parse.urlencode(params)
        # 成功过的域名排到最前, 减少无谓切换
        hosts = list(self.site_hosts)
        if getattr(self, "site_url", None) in hosts:
            hosts.remove(self.site_url)
            hosts.insert(0, self.site_url)
        # 总时限: 单次 timeout × 1.4, 到点直接放弃, 不让"域名×重试"叠加成几十秒
        deadline = time.time() + timeout * 1.4 + 0.3
        for attempt in range(REQUEST_RETRY):
            for host in hosts:
                left = deadline - time.time()
                if left <= 0.2:
                    return None, None
                # 这一次的 timeout 不超过剩余时限, 避免"到点前又起一个完整超时"
                to = min(timeout, max(0.8, left))
                url = host + url_path
                headers = dict(self.headers)
                headers.update(self._sign_headers(method, url))
                try:
                    if method == "POST":
                        headers["Content-Type"] = "application/json"
                        res = self.sess.post(
                            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                            headers=headers, timeout=to, verify=False)
                    else:
                        res = self.sess.get(url, headers=headers, timeout=to, verify=False)
                    if 200 <= res.status_code < 300:
                        # 记住本次成功的域名, 后续优先
                        self.site_url = host
                        return res.status_code, res
                except Exception:
                    pass
            left = deadline - time.time()
            if left <= 0.05:
                break
            time.sleep(min(RETRY_BACKOFF * (attempt + 1), left))
        return None, None

    # ========== 通用请求方法: 带签名 GET (兼容旧接口) ==========
    def fetch(self, url, timeout=REQUEST_TIMEOUT):
        """入参为完整 URL; 提取 path 后走统一重试请求。"""
        parsed = urllib.parse.urlparse(url)
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        code, res = self._request("GET", path, timeout=timeout)
        return res

    def _api_get(self, path, params=None, timeout=None):
        """请求 JSON API, 返回 dict/list; 失败返回 None"""
        code, res = self._request("GET", path, params=params,
                                  timeout=timeout or REQUEST_TIMEOUT)
        if res is None or code != 200:
            return None
        try:
            return res.json()
        except Exception:
            return None

    def _api_post(self, path, body, timeout=None):
        """带签名 POST JSON, 返回 dict/list; 失败返回 None"""
        code, res = self._request("POST", path, body=body,
                                  timeout=timeout or REQUEST_TIMEOUT)
        if res is None or code not in (200, 201):
            return None
        try:
            return res.json()
        except Exception:
            return None

    def _resolve_line(self, ticket):
        """POST /v1/playback/resolve-line: 把官方 resolve://ticket 解析为官方直链;
        返回 line dict; 失败(官方线路需登录/临时不可用)返回 None"""
        if not ticket:
            return None
        data = self._api_post("/v1/playback/resolve-line", {"ticket": ticket},
                              timeout=6)
        if not data or not isinstance(data, dict):
            return None
        line = data.get("line")
        if not isinstance(line, dict):
            return None
        return line

    # ========== 并发小工具(用守护线程 + 预算, 不阻塞整体) ==========
    def _fan(self, items, fn, budget, gang=6):
        """并发对 items 执行 fn, 最多等 budget 秒; 返回已完成项的 [(item, 结果)]。"""
        items = list(items)
        out = {}
        lock = threading.Lock()

        def run(i):
            try:
                v = fn(items[i])
            except Exception:
                v = None
            with lock:
                out[i] = v

        ths = []
        for i in range(min(len(items), gang * 3)):
            th = threading.Thread(target=run, args=(i,))
            th.daemon = True
            th.start()
            ths.append(th)
        deadline = time.time() + budget
        for th in ths:
            th.join(max(0.02, deadline - time.time()))
        return [(items[i], out[i]) for i in sorted(out)]

    def _probe_quick(self, url, timeout=QUICK_TIMEOUT):
        """站点式的前置校验: 只发 1 个 Range 请求, 200 且是 m3u8/mp4 就算通过。
        目的只是过滤 4xx/域名失效这类硬错, 不等分片、不拉完整清单,
        所以起播等待从原来最长 1.8s 降到一次往返。"""
        if not url:
            return False
        try:
            code, body = self._stream_get(url, 2048, timeout)
            if code not in (200, 206):
                return False
            if b"#EXTM3U" in body or b"#EXT-X-" in body:
                return True
            path = str(url).split("?")[0].lower()
            if path.endswith((".mp4", ".flv", ".mkv", ".mov")):
                return True
            if b"ftyp" in body[:64]:
                return True
            return False
        except Exception:
            return False

    # ========== 工具函数 ==========
    @staticmethod
    def _extract_vod_id(vod_id):
        """从 vod_id(可能是 av_xxx / 绝对地址 / /player/xxx) 提取纯 variant id"""
        s = str(vod_id).strip()
        if not s:
            return ""
        # 若是完整 URL, 取最后一段
        if s.startswith("http"):
            s = s.rstrip("/").rsplit("/", 1)[-1]
        # 去除可能的前缀路由
        for _ in range(2):
            s = re.sub(r"^(/v1/catalog/|/player/|/short-drama/)", "", s)
        return urllib.parse.unquote(s)

    @staticmethod
    def _line_name(lo):
        try:
            return str(lo.get("display_label") or lo.get("label") or lo.get("provider_name") or "")
        except Exception:
            return ""

    @staticmethod
    def _is_media(url):
        """mp4/flv 这类整文件直链: Range 定位, 快进几乎瞬时(比 m3u8 重新拉分片快)。"""
        return str(url).split("?")[0].lower().endswith((".mp4", ".flv", ".mkv", ".mov"))

    def _candidate_urls(self, lines, hint=""):
        """按优先级收集 direct http 候选(去重):
        用户点的线路(hint) > mp4 直链(快进最快) > 偏好关键词 > 其它。"""
        cands, seen = [], set()
        if not lines:
            return []

        def add(lo, strict=False):
            url = lo.get("url") or ""
            kind = str(lo.get("url_kind") or "").lower()
            good = lo.get("resolve_mode") == "direct" and url.startswith("http")
            if good and strict:
                good = kind in ("m3u8", "mp4", "flv")
            if good and url not in seen:
                seen.add(url)
                cands.append((self._line_name(lo), url, kind))

        for kw in PREFER_PROVIDER_KEYWORDS:
            for lo in lines:
                if kw in self._line_name(lo):
                    add(lo, strict=True)
        for lo in lines:
            add(lo)

        def rank(pair):
            name, url, kind = pair
            by_hint = 0 if (hint and hint in name) else 1
            by_media = 0 if (PREFER_MP4 and (kind in ("mp4", "flv") or self._is_media(url))) else 1
            return (by_hint, by_media)
        cands.sort(key=rank)          # sort 稳定: 同分组内保留偏好关键词的顺序
        return [u for _n, u, _k in cands]

    def _first_ticket(lines):
        """取第一个 resolve://ticket 形式的二级解析凭证"""
        for lo in lines or []:
            url = lo.get("url") or ""
            if url.startswith("resolve://"):
                t = url[len("resolve://"):].strip()
                if t:
                    return t
        return ""

    def _stream_get(self, url, range_end, timeout=PROBE_TIMEOUT):
        """裸流探测: 复用会话连接, 只重试 1 次(2 次总计), 失败立刻放行下一条线路。
        原版 3 次 + 每次 sleep 0.25s, 单条线路最坏要 15s 以上, 是"点开转圈"的主因。"""
        for _ in range(2):
            try:
                r = self._probe_sess.get(url, headers={
                    "Range": "bytes=0-%d" % range_end,
                }, timeout=timeout, stream=True, verify=False, allow_redirects=True)
                code = r.status_code
                body = r.raw.read(range_end) if code in (200, 206) else b""
                r.close()
                return code, body
            except Exception:
                continue
        return None, b""

    def _probe_stream(self, url, depth=0, timeout=PROBE_TIMEOUT):
        """兼容占位: 轻校验请用 _probe_quick(不再做完整清单+分片探测)。"""
        return self._probe_quick(url, timeout)

    def _official_candidates(self, lines):
        """收集官方线路(ticket)候选: 线路名含 '官方/专享' 且 url 为 resolve://ticket"""
        out = []
        for lo in lines or []:
            try:
                name = lo.get("display_label") or lo.get("label") or lo.get("provider_name") or ""
            except Exception:
                name = ""
            url = lo.get("url") or ""
            if ("官方" in name or "专享" in name) and url.startswith("resolve://"):
                t = url[len("resolve://"):].strip()
                if t and t not in out:
                    out.append(t)
        return out

    def _resolve_payload(self, token):
        """取 /v1/playback/resolve/<token> 的 line_options。不做缓存, 每次现取。"""
        path = "/v1/playback/resolve/" + urllib.parse.quote(token, safe="")
        for i in range(2):
            st, res = self._request("GET", path, timeout=5)
            if st == 200 and res is not None:
                try:
                    data = res.json()
                except Exception:
                    data = None
                if isinstance(data, dict) and (data.get("line_options") or data.get("current_episode")):
                    return data
                # 偶发空结果(站点抖动): 再取一次, 不把可播的片判死
                if i == 0:
                    time.sleep(0.15)
                    continue
                return {}
            # 400 invalid_public_playback_id: 站点判定该播放标识失效, 重试没有意义
            if st == 400:
                return {}
            if i == 0:
                time.sleep(0.15)
        return {}

    def _providers_of(self, token):
        """单集 token -> 该集可用的 provider 名列表。"""
        data = self._resolve_payload(token)
        names = []
        for lo in (data.get("line_options") or []):
            if not isinstance(lo, dict):
                continue
            nm = self._line_name(lo)
            if not nm and str(lo.get("url") or "").startswith("resolve://"):
                nm = "官方线路"          # 官方线路有时不给 display_label, 不能因此整条丢掉
            if nm and nm not in names:
                names.append(nm)
        return names

    def _movie_providers(self, tokens):
        """跨集取 provider 并集(无缓存): 线路是按集给的, 首集可能只有 A/B、末集才有官方。
        并发取最多 PROVIDER_SAMPLE_EPISODES 集, 总等待 PROVIDER_BUDGET 封顶。"""
        if isinstance(tokens, str):
            tokens = [tokens]
        uniq = []
        for t in tokens:
            if t and t not in uniq:
                uniq.append(t)
        tokens = uniq[:PROVIDER_SAMPLE_EPISODES]
        if not tokens:
            return []
        # 无缓存, 所以要省请求: 首集就有 2 条以上线路时直接用它, 不再多拉别的集
        names = self._providers_of(tokens[0])
        if len(names) < 2 and len(tokens) > 1:
            got = self._fan(tokens[1:], self._providers_of, PROVIDER_BUDGET, gang=len(tokens) - 1)
            for _tk, ns in got:
                for n in (ns or []):
                    if n not in names:
                        names.append(n)
        if names:
            names = sorted(names, key=lambda n: 0 if any(k in n for k in PREFER_PROVIDER_KEYWORDS) else 1)
        return names

    def _resolve_play_url(self, token, hint=""):
        """把剧集 token 解析成可播直链(不做缓存, 每次现取):
        1) 官方 resolve://ticket: 先解析它自己的直链;
        2) 第三方 direct: 首选线路做一次轻校验, 通过就直接交给播放器;
           不通过顺位试下一条(最多 2 条), 都不通过就把首选给播放器,
           和站点前端一致 —— 让用户在界面上手动换线路, 而不是插件里长时间探测。"""
        if not token:
            return ""
        data = self._resolve_payload(token)
        if not data:
            return ""
        lines = data.get("line_options") or []

        # 点的是官方/专享这类 ticket 线路: 先走它自己的解析, 不要被第三方直链抢走
        ticket_failed = False
        if hint:
            for lo in lines:
                if not isinstance(lo, dict):
                    continue
                hn = self._line_name(lo)
                if hint not in hn and not (not hn and hint.startswith("官方")):
                    continue
                u0 = lo.get("url") or ""
                if u0.startswith("resolve://"):
                    ticket_failed = True
                    t = u0[len("resolve://"):].strip()
                    line = self._resolve_line(t)
                    u1 = (line or {}).get("url") or ""
                    # 官方签名直链(bytetos)有时首包很慢, 不再做 Range 校验, 拿到了就直接给播放器
                    if u1.startswith(("http://", "https://")):
                        return u1
                    break
        if ticket_failed and STRICT_LINE:
            return ""                       # 官方不可用: 报错, 不偷偷换第三方

        # 严格模式: 只播用户点的这条线路; 该集没有这条线、或这条线不通, 都返回空
        if STRICT_LINE and hint:
            mine = []
            for lo in lines:
                if not isinstance(lo, dict):
                    continue
                if hint not in self._line_name(lo):
                    continue
                u = lo.get("url") or ""
                if lo.get("resolve_mode") == "direct" and u.startswith("http"):
                    mine.append(u)
            if mine:
                return mine[0]      # 严格模式: 点了就播(不再等校验), 播不了由播放器报错, 绝不换源
            return ""

        urls = self._candidate_urls(lines, hint)
        if urls:
            tried = 0
            for u in urls:
                if tried >= QUICK_MAX:
                    break
                tried += 1
                if self._probe_quick(u):
                    return u
            return urls[0]          # 都校验不过: 先把首选交出去, 别让用户干等

        for ticket in self._official_candidates(lines)[:2]:
            line = self._resolve_line(ticket)
            if not line:
                continue
            url = line.get("url") or ""
            if url.startswith(("http://", "https://")) and self._probe_quick(url):
                return url
        return ""

    def _fetch_episodes(self, var_id, deadline=None):
        """拉一个季的剧集: 首个 48 条 + 按需翻页; 单页用短超时, 失败就停,
        不再像原版那样一页超时(12s×重试)就把整个详情页拖死;
        deadline 到了立刻收手, 避免详情返回后仍有线程在后台翻页浪费请求。"""
        eps = []
        offset = 0
        while offset < MAX_EPISODES:
            if deadline and time.time() > deadline:
                break
            d = self._api_get("/v1/catalog/" + urllib.parse.quote(var_id, safe="") + "/episodes",
                              {"offset": str(offset), "limit": "48"},
                              timeout=API_TIMEOUT_FAST)
            if not d or not isinstance(d, dict):
                break
            batch = [e for e in (d.get("episodes") or []) if isinstance(e, dict)]
            if not batch:
                break
            eps.extend(batch)
            pg = d.get("episode_pagination") or {}
            total = pg.get("total_count") if isinstance(pg, dict) else None
            offset += 48
            try:
                if total is not None and len(eps) >= int(total):
                    break
            except Exception:
                pass
            if len(eps) >= MAX_EPISODES:
                break
        return eps

    # ========== 首页分类接口 ==========
    def homeContent(self, filter):
        cate_list = [{"type_name": KIND_MAP[k], "type_id": k} for k in DEFAULT_KINDS if k in KIND_MAP]
        return {"class": cate_list}

    # ========== 分类列表接口 ==========
    def categoryContent(self, tid, pg, filter, extend):
        pg = int(pg) if str(pg).isdigit() else 1
        tid = str(tid) if tid else "latest"
        params = {"page": str(pg), "limit": str(self.page_size), "intent": "latest_catalog"}
        if tid != "latest":
            params["kind"] = tid
        data = self._api_get("/v1/browse/catalog", params, timeout=API_TIMEOUT_FAST)
        video_list = []
        if data and isinstance(data, dict):
            for card in data.get("cards") or []:
                if not isinstance(card, dict):
                    continue
                # 卡片字段容错: 站点不同分类返回的字段名不完全一致,
                # 原来只认 id/title, 缺一个就整条丢掉 -> 分类里"少了几部"
                vod_id = (card.get("id") or card.get("video_id") or card.get("slug")
                          or (card.get("target") or {}).get("variant_id") or "")
                vod_name = (card.get("title") or card.get("name")
                            or card.get("normalized_title") or card.get("original_title") or "")
                vod_pic = card.get("poster_url") or card.get("poster") or card.get("cover") or ""
                if not vod_id:
                    continue
                if not vod_name:
                    vod_name = "未命名"          # 有 id 就能进详情, 别因为缺标题丢片
                vod_remarks = card.get("remarks") or ""
                if not vod_remarks:
                    year = card.get("year")
                    kind = card.get("content_kind") or ""
                    parts = []
                    if year:
                        parts.append(str(year))
                    if kind and kind in KIND_MAP:
                        parts.append(KIND_MAP[kind])
                    vod_remarks = " ".join(parts)
                video_list.append({
                    "vod_id": vod_id,
                    "vod_name": vod_name.strip(),
                    "vod_pic": vod_pic,
                    "vod_remarks": vod_remarks,
                    "style": {"type": "rect", "ratio": 0.65},  # 站点海报多为竖版 2:3
                })
        # 页数: 服务端给了总数就用总数算, 没给就按"这一页装满没有"判断,
        # 原来无脑 pg+1 会让壳多翻一页空页(每个分类都多一次请求)
        meta_total = 0
        if isinstance(data, dict):
            for k in ("total", "total_count", "count"):
                v = data.get(k)
                if isinstance(v, int) and v > 0:
                    meta_total = v
                    break
        if meta_total:
            pagecount = max(pg, (meta_total + self.page_size - 1) // self.page_size)
        elif len(video_list) >= self.page_size:
            pagecount = pg + 1
        else:
            pagecount = pg
        result = {
            "list": video_list,
            "page": pg,
            "pagecount": pagecount,
            "limit": self.page_size,
            "total": meta_total or len(video_list)
        }
        return result

    # ========== 视频详情接口 ==========
    def detailContent(self, ids):
        vod_id = ids[0] if ids else ""
        vid = self._extract_vod_id(vod_id)
        if not vid:
            return {"list": [{"vod_name": "视频ID为空"}]}


        detail = self._api_get("/v1/catalog/" + urllib.parse.quote(vid, safe=""))
        if not detail or not isinstance(detail, dict):
            return {"list": [{"vod_id": vod_id, "vod_name": "视频详情解析失败"}]}

        # 组装播放地址: 遍历季(variant) -> 剧集 -> token, 命名为 "第X季 第N集$token"
        play_url_parts = []
        titles = []
        variants = [v for v in (detail.get("variants") or []) if isinstance(v, dict)]
        # 无 variants 字段时回退到当前 variant_id
        if not variants and detail.get("variant_id"):
            variants = [{"variant_id": detail.get("variant_id"), "season_label": "正片"}]

        seasons = []
        for vi, var in enumerate(variants[:MAX_SEASONS]):
            var_id = var.get("variant_id") or var.get("id") or vid
            season_label = var.get("season_label") or var.get("title") or ("第%d季" % (vi + 1))
            seasons.append((vi, var_id, season_label))

        # 各季并发拉集, 总预算 DETAIL_BUDGET: 先到的先出, 没跑完的季直接跳过
        # (原版是一条季一条季串行, 5 季 × 多页 = 十几次串行请求, 详情页必然慢)
        dl = time.time() + DETAIL_BUDGET
        got = self._fan(seasons, lambda se: self._fetch_episodes(se[1], dl),
                        DETAIL_BUDGET, gang=min(4, max(1, len(seasons))))
        eps_map = dict((se[1], eps) for se, eps in got)

        for vi, var_id, season_label in seasons:
            episodes = eps_map.get(var_id) or []
            for ep in episodes:
                if len(play_url_parts) >= MAX_EPISODES:
                    break
                token = ep.get("token") or ""
                ep_title = ep.get("title") or ep.get("display_name") or ("第%d集" % (ep.get("number") or (len(play_url_parts) + 1)))
                if not token:
                    continue
                if len(variants) > 1 and season_label != vid and season_label not in ep_title:
                    if "季" in season_label or season_label != detail.get("title", ""):
                        label = "%s %s" % (season_label, ep_title) if season_label not in ep_title else ep_title
                    else:
                        label = ep_title
                else:
                    label = ep_title
                play_url_parts.append("%s$%s" % (label, token))
                titles.append(label)

        vod_name = detail.get("title") or detail.get("normalized_title") or "未知名称"
        vod_pic = detail.get("poster_url") or ""
        vod_remarks = detail.get("remarks") or ""
        vod_content = detail.get("description") or ""
        content_kind = detail.get("content_kind") or ""
        type_name = KIND_MAP.get(content_kind, content_kind)
        year = detail.get("year")
        area = detail.get("area") or ""
        actors = detail.get("actors") or []
        directors = detail.get("directors") or []

        # 线路展示: 原版把站点多源压成一条 "搜剧AI"(多源藏在每集 line_options 里, 播放时才探测选优)。
        # MULTI_LINE=1 时用首集 resolve 出的 provider 列表拆成多条线路, 只多 1 次请求。
   play_from = "搜剧AI"
        play_url = "#".join(play_url_parts) if play_url_parts else ""
        providers = []
        safe = []
        # 长剧少列线路: 4 条 × 200 集 = 800 个剧集项, 界面解析也会变慢
        line_cap = MAX_LINES_LONG if len(play_url_parts) > LONG_SERIES_EPISODES else MAX_LINES
        if not line_cap or line_cap <= 0:
            line_cap = 10 ** 6        # 0 = 去掉上限, 不截断

        # 唯一例外: 一部剧可能有几十条同名/失效线路, 至少保证标题不重复, 数量不再限制
        if MULTI_LINE and play_url_parts:
            try:
                all_toks = [x.split("$", 1)[1] for x in play_url_parts]
                sample = [all_toks[0]] if all_toks else []
                if len(all_toks) > 2:
                    sample.append(all_toks[len(all_toks) // 2])
                if len(all_toks) > 1:
                    sample.append(all_toks[-1])
                raw = self._movie_providers(sample)
                # 官方/专享这类线路常被排到末尾又撞上限而被截掉, 这里单独留位置, 固定放最后
                official = [n for n in raw if any(k in n for k in ("官方", "专享", "独播", "VIP", "尊享"))]
                normal = [n for n in raw if n not in official]
                if official:
                    k = min(len(official), line_cap)
                    # 官方/专享排最前, 普通线路排后面(用户习惯先看官方源)
                    providers = official[:k] + normal[:max(0, line_cap - k)]
                else:
                    providers = normal[:line_cap]
            except Exception:
                providers = []
            if len(providers) > 1:
                safe = []
                for i, pp in enumerate(providers):
                    nm = str(pp).replace("$", "").replace("@@", "").strip() or ("线路%d" % (i + 1))
                    if nm in safe:
                        nm = "%s%d" % (nm, i + 1)
                    safe.append(nm)
                play_from = "$$$".join("搜剧AI·" + pp for pp in safe)
                play_url = "$$$".join("#".join("%s@@%s" % (x, pp) for x in play_url_parts)
                                      for pp in safe)

        detail_info = {
            "vod_id": vid,
            "vod_name": vod_name,
            "vod_pic": vod_pic,
            "vod_remarks": vod_remarks,
            "type_name": type_name,
            "vod_year": str(year) if year else "",
            "vod_area": area,
            "vod_director": "/".join(directors) if directors else "",
            "vod_actor": "/".join(actors) if actors else "",
            "vod_content": vod_content,
            "vod_play_from": play_from,
            "vod_play_url": play_url,
        }
        return {"list": [detail_info]}

    # ========== 搜索接口 ==========
    def searchContent(self, key, quick, pg=1):
        key = (key or "").strip()
        pg = int(pg) if str(pg).isdigit() else 1
        # 搜索结果一次性返回(第 1 页), 避免 OK影视 翻页重复请求导致超时判失败
        if not key or pg > 1:
            return {"list": [], "page": pg, "pagecount": 1, "limit": self.page_size, "total": 0}
        data = self._api_get("/v1/suggest", {"q": key, "limit": "12", "mode": "thread"},
                             timeout=API_TIMEOUT_FAST)
        video_list = []
        if data and isinstance(data, dict):
            for item in data.get("suggestions") or []:
                if not isinstance(item, dict):
                    continue
                target = item.get("target") or {}
                variant_id = target.get("variant_id") or ""
                label = item.get("label") or target.get("title") or ""
                if not variant_id or not label:
                    continue
                # 剔除与关键词完全无关的热推标题, 避免误导搜索结果
                if item.get("type") == "title" and not any(ch in label for ch in key if ch.strip()):
                    continue
                remarks = item.get("subtitle") or ""
                video_list.append({
                    "vod_id": variant_id,
                    "vod_name": label.strip(),
                    "vod_pic": "",
                    "vod_remarks": remarks,
                    "style": {"type": "rect", "ratio": 1.4},
                })
        # 搜索结果的封面图: 并发取详情补海报(单条失败留空, 不影响结果)
        def _fill_poster(item):
            try:
                vid = self._extract_vod_id(item["vod_id"])
                detail = self._api_get("/v1/catalog/" + urllib.parse.quote(vid, safe=""),
                                       timeout=API_TIMEOUT_FAST)
                if detail and isinstance(detail, dict) and detail.get("poster_url"):
                    item["vod_pic"] = detail["poster_url"]
            except Exception:
                pass
        if video_list:
            # 海报只补前 10 条, 最多等 SEARCH_POSTER_TIMEOUT(2s); 补不到就留空,
            # 结果列表该先出来就先出来
            self._fan(video_list[:10], _fill_poster, SEARCH_POSTER_TIMEOUT, gang=8)
        result = {
            "list": video_list,
            "page": 1,
            "pagecount": 1,
            "limit": self.page_size,
            "total": len(video_list)
        }
        return result

    # ========== 播放解析接口 ==========
    def playerContent(self, flag, id, vipFlags):
        token = id
        if "$" in id:
            token = id.split("$", 1)[1]
        # 多线路模式下, 剧集 id 形如 token@@红牛, 线路名也形如 搜剧AI·红牛
        hint = ""
        if "%" in token:
            try:
                token = urllib.parse.unquote(token)
            except Exception:
                pass
        if "@@" in token:
            token, hint = token.rsplit("@@", 1)
        token = token.strip()
        hint = hint.strip()
        if not hint:
            f = str(flag or "")
            if "%" in f:
                try:
                    f = urllib.parse.unquote(f)
                except Exception:
                    pass
            if f.startswith("搜剧AI·"):
                hint = f[len("搜剧AI·"):].strip()
            elif "·" in f:
                hint = f.split("·", 1)[1].strip()
        if not token:
            return {"parse": 0, "url": "", "header": self.headers}
        final_url = self._resolve_play_url(token, hint)
        if not final_url:
            return {"parse": 0, "url": "", "header": self.headers}
        # m3u8 已可直接播放, 无需二次解析; 注意多数源无需 Referer, 避免防盗链拦截不带
        return {"parse": 0, "url": final_url, "header": {"User-Agent": self.headers["User-Agent"]}}