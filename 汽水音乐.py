# -*- coding: utf-8 -*-
"""
================================================================================
 汽水音乐 · 纯 Python Spider（CatVod / 影视仓 / TVBox / FongMi 通用）
================================================================================
 后端    : https://api.qishui.com/luna/pc/*        （PC 客户端接口，免签名可读）
 播放源  : https://beta-luna.douyin.com/luna/h5/seo_track  → vod-luna GetPlayInfo
          → 直链 m4a（实测 HTTP 206 / audio/mp4 可放）
 依赖    : 仅标准库（urllib / json / uuid / hashlib），requests 可选

 已实测链路
 ----------------------------------------------------------------------------
  1. 目录      POST /luna/pc/feed/playlist-square   -> 14 个分类 + 歌单卡片
  2. 歌单      GET  /luna/pc/playlist/detail        -> playlist + media_resources
  3. 推荐单曲  POST /luna/pc/feed/song-tab          -> 单曲流（items）
  4. 我的歌单  GET  /luna/pc/me/playlist            -> 需填 cookie
  5. 艺人/专辑  GET  /luna/pc/artists|albums/{id}    -> 热门单曲 / 专辑曲目
  6. 播放      GET  beta-luna.douyin.com/luna/h5/seo_track?track_id=..
               -> track_player.url_player_info（带签名的 VOD 地址）
               -> Action=GetPlayInfo  -> PlayInfoList（medium/higher/highest…）
               -> 取最大码率 main_play_url（带时效，播放时实时解析，不缓存）
  7. 搜索      GET  /luna/pc/search/{all,track,playlist,album,artist}
               服务端对「无 App 签名头」的请求回 HTTP 200 + 空 body（新反爬），
               本源默认用「歌单广场 + 推荐流」本地关键词兜底；
               若在 extend 里给出 helios/medusa（从官方客户端抓到的签名头）会优先走官方搜索。

 配置（extend，JSON 或 k=v,k=v）
 ----------------------------------------------------------------------------
  {"cookie":"<汽水登录 Cookie>",      // 填了才能读“我的歌单”，也更容易拿到高码率
   "fp":"<19 位设备号>",             // 可选，默认内置
   "helios":"<x-helios>","medusa":"<x-medusa>",  // 可选，搜索签名头
   "onlyFree":1,                    // 1 = 只返回非会员曲目（默认 0，全部返回）
   "timeout":20}

 说明
 ----------------------------------------------------------------------------
 · 播放地址 30~60 分钟内有效，本 Spider 在 playerContent 里实时换取，不落盘。
 · 会员曲目在无登录态时通常只给 3 档（medium/higher/highest）；登录 Cookie 后
   官方 seo 接口可能给到 lossless/hi_res，是否给整曲取决于账号权益。
 · 不支持 App 的“每日推荐/发现页”（/luna/pc/discover 需签名，返回空）。
================================================================================
"""
import json
import time
import uuid
import hashlib
import urllib.parse
import urllib.request

try:
    import requests
except Exception:
    requests = None

try:
    from base.spider import Spider as BaseSpider
except Exception:
    class BaseSpider(object):
        def init(self, extend=""): pass
        def getName(self): return ""
        def isVideoFormat(self, url): return False
        def manualVideoCheck(self): return False
        def destroy(self): pass
        def localProxy(self, param): return None


PC_UA = "LunaPC/3.5.2(359450208)"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")

# 歌单广场的固定分类（首次请求也会从接口刷新）
DEFAULT_CATS = [
    (0, "每日推荐"), (93, "官方"), (8, "华语"), (14, "流行"), (15, "摇滚"),
    (16, "电子"), (18, "民谣"), (19, "R&B"), (20, "国风"), (38, "说唱"),
    (9, "欧美"), (40, "学习"), (45, "睡前"), (69, "治愈"),
]


class Spider(BaseSpider):
    CONFIG = {
        "aid": "386088",
        "app_name": "luna_pc",
        "device_platform": "windows",
        "version_code": "359450208",
        "version_name": "3.5.2",
        "channel": "official",
        "fp": "7394826150937468215",
        "cookie": "",
        "helios": "",
        "medusa": "",
        "timeout": 20,
        "onlyFree": 0,
    }

    def __init__(self):
        super().__init__()
        self.cfg = dict(self.CONFIG)
        self.cats = list(DEFAULT_CATS)
        self._cache = {}
        self._my_id = ""

    def getName(self):
        return "汽水音乐"

    # ------------------------------------------------------------ 初始化
    def init(self, extend=""):
        self.cfg = dict(self.CONFIG)
        if isinstance(extend, dict):
            self.cfg.update(extend)
        elif isinstance(extend, str) and extend.strip():
            txt = extend.strip()
            try:
                self.cfg.update(json.loads(txt))
            except Exception:
                for kv in txt.split(","):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        self.cfg[k.strip()] = v.strip()
        ck = str(self.cfg.get("cookie") or "").strip()
        if ck and "=" not in ck:
            # 允许只填 sessionid
            ck = "sessionid=" + ck
        self.cookie = ck
        self._only_free = int(self.cfg.get("onlyFree") or 0)

    # ------------------------------------------------------------ HTTP
    def _common(self):
        return {
            "aid": str(self.cfg.get("aid")),
            "app_name": str(self.cfg.get("app_name")),
            "device_platform": str(self.cfg.get("device_platform")),
            "version_code": str(self.cfg.get("version_code")),
            "version_name": str(self.cfg.get("version_name")),
            "channel": str(self.cfg.get("channel")),
            "fp": str(self.cfg.get("fp")),
        }

    def _headers(self, extra=None):
        h = {
            "User-Agent": PC_UA,
            "Accept": "application/json",
            "x-luna-background-type": "foreground",
            "x-luna-is-background-req": "0",
            "x-luna-is-local-user": "1" if self.cookie else "0",
        }
        if self.cookie:
            h["Cookie"] = self.cookie
        if extra:
            h.update(extra)
        return h

    def _raw(self, url, headers=None, body=None, method="GET"):
        t = int(self.cfg.get("timeout") or 20)
        data = None
        h = dict(headers or {})
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            h["Content-Type"] = "application/json; charset=utf-8"
        if requests:
            r = requests.request(method, url, data=data, headers=h, timeout=t)
            return r.status_code, r.content
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            r = urllib.request.urlopen(req, timeout=t)
            return r.getcode(), r.read()
        except Exception as e:
            code = getattr(getattr(e, "code", None), "real", None) or getattr(e, "code", 0) or 0
            try:
                return int(code), e.read()
            except Exception:
                return int(code) if code else 0, b""

    def _api(self, path, params=None, body=None, method="GET", extra_headers=None):
        q = self._common()
        if params:
            q.update({k: v for k, v in params.items() if v is not None})
        url = "https://api.qishui.com" + path + "?" + urllib.parse.urlencode(q)
        code, raw = self._raw(url, self._headers(extra_headers), body, method)
        if code != 200 or not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {}

    def _seo_track(self, track_id):
        """SEO 单曲接口：免签名返回元数据 + 播放器签名地址。"""
        url = ("https://beta-luna.douyin.com/luna/h5/seo_track?"
               + urllib.parse.urlencode({"track_id": str(track_id), "device_platform": "web"}))
        code, raw = self._raw(url, {"User-Agent": WEB_UA, "Accept": "application/json"}, None, "GET")
        if code != 200 or not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return {}

    def _play_info(self, player_url):
        """VOD GetPlayInfo：把 track_player.url_player_info 换成真实音频列表。"""
        code, raw = self._raw(player_url, {"User-Agent": WEB_UA}, None, "GET")
        if code != 200 or not raw:
            return []
        try:
            jo = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            return []
        return (((jo.get("Result") or {}).get("Data") or {}).get("PlayInfoList") or [])

    # ------------------------------------------------------------ 工具
    @staticmethod
    def _img(node, size="960:960"):
        node = node or {}
        urls = node.get("urls") or []
        uri = str(node.get("uri") or "").strip()
        tpl = str(node.get("template_prefix") or "").strip()
        if not urls:
            return ""
        base = str(urls[0] or "")
        if not uri:
            return base
        # uri 里可能带中文，必须 quote，否则 urllib 会报 ascii 错
        full = base.rstrip("/") + "/" + urllib.parse.quote(uri.lstrip("/"))
        if tpl:
            return full + "~" + tpl + "-resize:" + size + ".png"
        return full

    @staticmethod
    def _artists(track):
        return " / ".join([str(a.get("name") or "") for a in (track.get("artists") or []) if a.get("name")])

    @staticmethod
    def _dur(ms):
        try:
            return int(ms) // 1000
        except Exception:
            return 0

    @staticmethod
    def _clean(s):
        return str(s or "").replace("#", " ").replace("$", " ").replace("$$$", " ").strip()

    def _is_vip(self, track):
        lab = (track or {}).get("label_info") or {}
        if lab.get("only_vip_playable") or lab.get("only_vip_download"):
            return True
        if lab.get("quality_only_vip_can_play"):
            return True
        for v in (lab.get("quality_map") or {}).values():
            for k in ("play_detail", "download_detail"):
                if (v.get(k) or {}).get("need_vip"):
                    return True
        return False

    def _track_item(self, t, remark_prefix=""):
        t = t or {}
        al = t.get("album") or {}
        remark = self._artists(t)
        if remark_prefix:
            remark = remark_prefix + remark
        if self._is_vip(t):
            remark += " · VIP"
        return {
            "vod_id": "tr:" + str(t.get("id") or ""),
            "vod_name": str(t.get("name") or ""),
            "vod_pic": self._img(al.get("url_cover"), "375:375"),
            "vod_remarks": remark,
            "vod_year": "",
            "vod_area": "",
            "vod_class": "单曲",
            "vod_actor": self._artists(t),
            "vod_director": str(al.get("name") or ""),
            "vod_content": "专辑：%s\n时长：%ss" % (str(al.get("name") or ""), self._dur(t.get("duration"))),
        }

    def _playlist_item(self, p):
        p = p or {}
        cnt = p.get("count_tracks") or (p.get("resource_cnt") or {}).get("track_cnt") or 0
        owner = (p.get("owner") or {}).get("nickname") or (p.get("owner") or {}).get("public_name") or ""
        remark = "歌单 · %s 首" % cnt
        if owner:
            remark += " · " + str(owner)
        return {
            "vod_id": "pl:" + str(p.get("id") or ""),
            "vod_name": str(p.get("title") or p.get("public_title") or ""),
            "vod_pic": self._img(p.get("url_cover"), "600:600"),
            "vod_remarks": remark,
            "vod_year": "",
            "vod_area": "",
            "vod_class": "歌单",
            "vod_actor": str(owner),
            "vod_director": "",
            "vod_content": str(p.get("desc") or ""),
        }

    def _album_item(self, a):
        a = a or {}
        return {
            "vod_id": "al:" + str(a.get("id") or ""),
            "vod_name": str(a.get("name") or ""),
            "vod_pic": self._img(a.get("url_cover"), "600:600"),
            "vod_remarks": "专辑 · %s 首" % (a.get("count_tracks") or 0),
            "vod_class": "专辑",
            "vod_actor": " / ".join([x.get("name") or "" for x in (a.get("artists") or [])]),
            "vod_content": str(a.get("intro") or ""),
        }

    def _artist_item(self, a):
        a = a or {}
        return {
            "vod_id": "ar:" + str(a.get("id") or ""),
            "vod_name": str(a.get("name") or ""),
            "vod_pic": self._img(a.get("url_avatar"), "600:600"),
            "vod_remarks": "音乐人 · %s 首" % (a.get("count_tracks") or 0),
            "vod_class": "音乐人",
            "vod_actor": str(a.get("simple_display_name") or a.get("name") or ""),
            "vod_content": str(((a.get("artist_profile") or {}).get("description") or "")),
        }

    # ------------------------------------------------------------ 歌单广场 / 分类
    def _square(self, category_id=0, count=30, cursor=""):
        body = {"includes": ["playlist_square_include_category",
                             self._my_id or "guest"],
                "category_id": int(category_id),
                "count": int(count)}
        if cursor:
            body["cursor"] = str(cursor)
        return self._api("/luna/pc/feed/playlist-square", None, body, "POST")

    def _refresh_cats(self, resp):
        cats = (resp or {}).get("categories") or []
        got = [(int(c.get("id") or 0), str(c.get("name") or "")) for c in cats if c.get("name") is not None]
        if got:
            self.cats = got

    def _feed(self, page=1, count=20):
        body = {
            "is_first_request": page <= 1,
            "count": int(count),
            "total_request_count": (page - 1) * int(count),
            "is_did_first_request": page > 1,
            "feed_mode": "",
            "full_media": True,
        }
        return self._api("/luna/pc/feed/song-tab", None, body, "POST")

    # ------------------------------------------------------------ 首页
    def homeContent(self, filter=False):
        r = self._square(0, 30)
        self._refresh_cats(r)
        classes = [{"type_id": "feed", "type_name": "推荐单曲"},
                   {"type_id": "square", "type_name": "热门歌单"}]
        if self.cookie:
            classes.append({"type_id": "mine", "type_name": "我的歌单"})
        for cid, name in self.cats:
            classes.append({"type_id": "tag:%d" % cid, "type_name": name})
        lst = [self._playlist_item((it.get("entity") or {}).get("playlist"))
               for it in (r.get("items") or [])
               if ((it.get("entity") or {}).get("playlist") or {}).get("id")]
        return {"class": classes, "filters": {}, "list": lst}

    def homeVideoContent(self):
        return {"list": self.homeContent(False).get("list", [])}

    # ------------------------------------------------------------ 分类
    def categoryContent(self, tid, pg, filter, extend):
        try:
            pg = int(pg or 1)
        except Exception:
            pg = 1
        tid = str(tid or "square")
        lst = []

        if tid == "feed":
            r = self._feed(pg, 20)
            for it in r.get("items") or []:
                t = (((it.get("entity") or {}).get("track_wrapper") or {}).get("track")) or {}
                if not t.get("id"):
                    t = ((it.get("entity") or {}).get("track")) or {}
                if t.get("id"):
                    lst.append(self._track_item(t))
            return {"list": lst, "page": pg, "pagecount": 9999, "limit": 20, "total": 999999}

        if tid == "mine":
            d = self._api("/luna/pc/me/playlist")
            for p in d.get("playlists") or []:
                if p.get("id"):
                    lst.append(self._playlist_item(p))
            return {"list": lst, "page": 1, "pagecount": 1, "limit": len(lst), "total": len(lst)}

        if tid.startswith("tag:"):
            cid = int(tid.split(":", 1)[1])
        elif tid == "square":
            cid = 0
        else:
            cid = 0

        r = self._square(cid, 30, str((pg - 1) * 30) if pg > 1 else "")
        self._refresh_cats(r)
        for it in r.get("items") or []:
            p = (it.get("entity") or {}).get("playlist") or {}
            if p.get("id"):
                lst.append(self._playlist_item(p))
        return {"list": lst, "page": pg, "pagecount": 9999, "limit": 30, "total": 999999}

    # ------------------------------------------------------------ 详情
    def detailContent(self, ids):
        if not ids:
            return {"list": []}
        vid = str(ids[0])
        if ":" in vid:
            kind, oid = vid.split(":", 1)
        else:
            kind, oid = "pl", vid

        if kind == "tr":
            return {"list": [self._track_detail(oid)]}
        if kind == "ar":
            return {"list": [self._artist_detail(oid)]}
        if kind == "al":
            return {"list": [self._album_detail(oid)]}
        return {"list": [self._playlist_detail(oid)]}

    def _playlist_detail(self, pid):
        tracks, seen, cursor = [], set(), ""
        info = {}
        for _ in range(5):
            d = self._api("/luna/pc/playlist/detail",
                          {"playlist_id": pid, "cursor": cursor, "count": 100})
            if not d:
                break
            info = info or (d.get("playlist") or {})
            for it in d.get("media_resources") or []:
                if it.get("type") != "track":
                    continue
                t = (((it.get("entity") or {}).get("track_wrapper") or {}).get("track")) or {}
                if not t.get("id") or t["id"] in seen:
                    continue
                seen.add(t["id"])
                tracks.append(t)
            nxt = str(d.get("next_cursor") or "")
            if not nxt or nxt == cursor:
                break
            cursor = nxt

        eps = []
        for t in tracks:
            if self._only_free and self._is_vip(t):
                continue
            name = self._clean("%s - %s" % (t.get("name") or "", self._artists(t)))
            eps.append("%s$tr:%s" % (name, t.get("id")))

        if not eps and tracks:
            eps = ["%s$tr:%s" % (self._clean(t.get("name") or ""), t.get("id")) for t in tracks]

        return {
            "vod_id": "pl:" + pid,
            "vod_name": str(info.get("title") or info.get("public_title") or "汽水歌单"),
            "vod_pic": self._img(info.get("url_cover"), "600:600"),
            "type_name": "歌单",
            "vod_remarks": "共 %s 首" % (len(tracks) or info.get("count_tracks") or 0),
            "vod_content": str(info.get("desc") or ""),
            "vod_actor": str(((info.get("owner") or {}).get("nickname")) or ""),
            "vod_play_from": "汽水音乐",
            "vod_play_url": "#".join(eps),
        }

    def _track_detail(self, tid):
        d = self._seo_track(tid)
        t = (((d.get("seo_track") or {}).get("track")) or {})
        eps = ["%s$tr:%s" % (self._clean(t.get("name") or ""), tid)]
        for rt in (d.get("related_tracks") or [])[:20]:
            rt = rt or {}
            if rt.get("id") and rt.get("id") != tid:
                eps.append("%s$tr:%s" % (self._clean(rt.get("name") or ""), rt.get("id")))
        return {
            "vod_id": "tr:" + tid,
            "vod_name": str(t.get("name") or ""),
            "vod_pic": self._img((t.get("album") or {}).get("url_cover"), "600:600"),
            "type_name": "单曲",
            "vod_remarks": self._artists(t),
            "vod_content": str(((d.get("lyric") or {}).get("content") or ""))[:3000],
            "vod_play_from": "汽水音乐",
            "vod_play_url": "#".join(eps),
        }

    def _artist_detail(self, aid):
        d = self._api("/luna/pc/artists/%s" % aid)
        info = d.get("artist_info") or {}
        eps = []
        for t in d.get("hot_tracks") or []:
            if t.get("id"):
                eps.append("%s$tr:%s" % (self._clean(t.get("name") or ""), t.get("id")))
        return {
            "vod_id": "ar:" + aid,
            "vod_name": str(info.get("name") or ""),
            "vod_pic": self._img(info.get("url_avatar"), "600:600"),
            "type_name": "音乐人",
            "vod_remarks": "热门单曲 %s 首" % len(eps),
            "vod_content": str(((info.get("artist_profile") or {}).get("description") or "")),
            "vod_play_from": "汽水音乐",
            "vod_play_url": "#".join(eps),
        }

    def _album_detail(self, aid):
        d = self._api("/luna/pc/albums/%s" % aid)
        info = d.get("album_info") or {}
        eps = []
        for t in d.get("tracks") or []:
            if t.get("id"):
                eps.append("%s$tr:%s" % (self._clean(t.get("name") or ""), t.get("id")))
        return {
            "vod_id": "al:" + aid,
            "vod_name": str(info.get("name") or ""),
            "vod_pic": self._img(info.get("url_cover"), "600:600"),
            "type_name": "专辑",
            "vod_remarks": "共 %s 首" % (len(eps) or info.get("count_tracks") or 0),
            "vod_content": str(info.get("intro") or " ".join(info.get("pclines") or [])),
            "vod_play_from": "汽水音乐",
            "vod_play_url": "#".join(eps),
        }

    # ------------------------------------------------------------ 搜索
    def _search_api(self, key, stype="all", count=20):
        sid = str(uuid.uuid4())
        params = {
            "q": str(key), "cursor": "0", "count": str(count),
            "search_id": sid, "search_method": "input",
            "debug_params": "", "from_search_id": "", "search_scene": "",
        }
        extra = {}
        if str(self.cfg.get("helios") or "").strip():
            extra["x-helios"] = str(self.cfg.get("helios")).strip()
        if str(self.cfg.get("medusa") or "").strip():
            extra["x-medusa"] = str(self.cfg.get("medusa")).strip()
        return self._api("/luna/pc/search/" + stype, params, None, "GET", extra or None)

    def _search_local(self, key, limit=60):
        """无签名时的兜底：在歌单广场（各分类）和推荐流里按关键词过滤。"""
        k = str(key or "").strip().lower()
        if not k:
            return []
        out, seen = [], set()
        cache = self._cache.get("pool")
        now = time.time()
        if cache and now - cache[0] < 300:
            pool = cache[1]
        else:
            pool = []
            for cid, _name in self.cats:
                r = self._square(cid, 30)
                for it in r.get("items") or []:
                    p = (it.get("entity") or {}).get("playlist") or {}
                    if p.get("id"):
                        pool.append(("pl", p))
            r = self._feed(1, 20)
            for it in r.get("items") or []:
                t = (((it.get("entity") or {}).get("track_wrapper") or {}).get("track")) or {}
                if t.get("id"):
                    pool.append(("tr", t))
            self._cache["pool"] = (now, pool)

        for kind, obj in pool:
            title = str(obj.get("title") or obj.get("public_title") or obj.get("name") or "")
            desc = str(obj.get("desc") or "")
            if k not in title.lower() and k not in desc.lower():
                continue
            key2 = kind + ":" + str(obj.get("id"))
            if key2 in seen:
                continue
            seen.add(key2)
            out.append(self._playlist_item(obj) if kind == "pl" else self._track_item(obj))
            if len(out) >= limit:
                break
        return out

    def searchContent(self, key, quick, pg="1"):
        d = self._search_api(key, "all")
        groups = d.get("result_groups") or []
        lst = []
        for g in groups:
            for it in g.get("data") or []:
                e = it.get("entity") or {}
                if e.get("track") and e["track"].get("id"):
                    lst.append(self._track_item(e["track"]))
                elif e.get("playlist") and e["playlist"].get("id"):
                    lst.append(self._playlist_item(e["playlist"]))
                elif e.get("album") and e["album"].get("id"):
                    lst.append(self._album_item(e["album"]))
                elif e.get("artist") and e["artist"].get("id"):
                    lst.append(self._artist_item(e["artist"]))
        if not lst:
            # 单曲搜索单独再试一次（all 有时只给歌单）
            d2 = self._search_api(key, "track")
            for g in d2.get("result_groups") or []:
                for it in g.get("data") or []:
                    t = (it.get("entity") or {}).get("track") or {}
                    if t.get("id"):
                        lst.append(self._track_item(t))
        if not lst:
            lst = self._search_local(key)
        return {"list": lst, "page": 1, "pagecount": 1, "limit": len(lst), "total": len(lst)}

    # ------------------------------------------------------------ 播放
    def _resolve_track(self, tid):
        """track_id -> 真实音频直链 + 请求头。"""
        d = self._seo_track(tid)
        tp = d.get("track_player") or {}
        purl = str(tp.get("url_player_info") or "")
        if not purl:
            return "", {}, "seo_track 无 url_player_info"
        list_ = self._play_info(purl)
        if not list_:
            return "", {}, "GetPlayInfo 无 PlayInfoList"
        # 优先级：码率 > 体积；同码率取更大
        def rank(x):
            return (float(x.get("Bitrate") or 0), int(x.get("Size") or 0))
        best = max(list_, key=rank)
        url = str(best.get("MainPlayUrl") or best.get("BackupPlayUrl") or "")
        if not url:
            return "", {}, "PlayInfoList 无 MainPlayUrl"
        auth = str(best.get("PlayAuth") or "")
        if auth:
            # 有的播放器会要求 auth 参数；直链本身已带签名，这里只在缺失时才补
            if "auth=" not in url:
                url += ("&" if "?" in url else "?") + "auth=" + urllib.parse.quote(auth)
        hdr = {"User-Agent": WEB_UA, "Referer": "https://www.qishui.com/"}
        return url, hdr, ""

    def playerContent(self, flag, id, vipFlags=None):
        hdr = {"User-Agent": WEB_UA, "Referer": "https://www.qishui.com/"}
        pid = str(id or "")
        if "$" in pid:
            pid = pid.split("$", 1)[1]
        if ":" in pid:
            pid = pid.split(":", 1)[1]
        if not pid:
            return {"parse": 0, "url": "", "header": hdr, "msg": "缺少歌曲 id"}
        url, h2, err = self._resolve_track(pid)
        if not url:
            return {"parse": 0, "url": "", "header": hdr, "msg": err or "解析失败"}
        return {"parse": 0, "url": url, "header": h2 or hdr,
                "format": "mp4", "msg": "汽水音乐"}


if __name__ == "__main__":
    s = Spider()
    s.init("")
    print("name:", s.getName())
    h = s.homeContent(False)
    print("分类:", [(c["type_id"], c["type_name"]) for c in h["class"]])
    print("首页:", len(h["list"]), (h["list"][0]["vod_name"] if h["list"] else ""))
    c = s.categoryContent("tag:15", 1, False, {})
    print("摇滚歌单:", len(c["list"]), (c["list"][0]["vod_name"] if c["list"] else ""))
    f = s.categoryContent("feed", 1, False, {})
    print("推荐单曲:", len(f["list"]), (f["list"][0]["vod_name"] if f["list"] else ""))
    pid = (c["list"] or [{}])[0].get("vod_id", "")
    if pid:
        d = s.detailContent([pid])["list"][0]
        print("歌单详情:", d["vod_name"], "曲目:", len(str(d["vod_play_url"]).split("#")))
        first = str(d["vod_play_url"]).split("#")[0]
        p = s.playerContent("", first)
        print("播放:", str(p.get("url"))[:120], "err:", p.get("msg"))
    print("搜索:", len(s.searchContent("摇滚", False)["list"]), "条")
