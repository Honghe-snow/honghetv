#!/usr/bin/python
# -*- coding: utf-8 -*-
"""
@header({
  searchable: 1,
  filterable: 1,
  quickSearch: 1,
  title: '剧岛4K',
  lang: 'hipy'
})
"""
import base64
import hashlib
import html
import json
import random
import re
import sys

try:
    import threading
except Exception:
    threading = None

try:
    import concurrent.futures
except Exception:
    concurrent.futures = None

import requests
from base.spider import Spider

sys.path.append('..')

# ---------- 诊断跟踪 ----------
_TRACE = []


def _trace(msg):
    try:
        _TRACE.append(str(msg)[:300])
    except Exception:
        pass


try:
    from Crypto.Cipher import AES as _CAES
except Exception:
    _CAES = None

# ---------- 纯 Python AES-128 (CFB, segment_size=128) 兜底 ----------
# 当运行环境没有 PyCryptodome(Crypto) 时，原脚本会把 AES 设成 None，
# 导致所有接口返回的数据无法解密 -> 表现为"没有数据"。
# 这里内置一份与 PyCryptodome 行为完全一致的纯 Python 实现，确保任何
# hipy 运行环境都能正常加解密。优先使用 Crypto（更快），缺失时自动降级。
_SBOX = [
    0x63,0x7c,0x77,0x7b,0xf2,0x6b,0x6f,0xc5,0x30,0x01,0x67,0x2b,0xfe,0xd7,0xab,0x76,
    0xca,0x82,0xc9,0x7d,0xfa,0x59,0x47,0xf0,0xad,0xd4,0xa2,0xaf,0x9c,0xa4,0x72,0xc0,
    0xb7,0xfd,0x93,0x26,0x36,0x3f,0xf7,0xcc,0x34,0xa5,0xe5,0xf1,0x71,0xd8,0x31,0x15,
    0x04,0xc7,0x23,0xc3,0x18,0x96,0x05,0x9a,0x07,0x12,0x80,0xe2,0xeb,0x27,0xb2,0x75,
    0x09,0x83,0x2c,0x1a,0x1b,0x6e,0x5a,0xa0,0x52,0x3b,0xd6,0xb3,0x29,0xe3,0x2f,0x84,
    0x53,0xd1,0x00,0xed,0x20,0xfc,0xb1,0x5b,0x6a,0xcb,0xbe,0x39,0x4a,0x4c,0x58,0xcf,
    0xd0,0xef,0xaa,0xfb,0x43,0x4d,0x33,0x85,0x45,0xf9,0x02,0x7f,0x50,0x3c,0x9f,0xa8,
    0x51,0xa3,0x40,0x8f,0x92,0x9d,0x38,0xf5,0xbc,0xb6,0xda,0x21,0x10,0xff,0xf3,0xd2,
    0xcd,0x0c,0x13,0xec,0x5f,0x97,0x44,0x17,0xc4,0xa7,0x7e,0x3d,0x64,0x5d,0x19,0x73,
    0x60,0x81,0x4f,0xdc,0x22,0x2a,0x90,0x88,0x46,0xee,0xb8,0x14,0xde,0x5e,0x0b,0xdb,
    0xe0,0x32,0x3a,0x0a,0x49,0x06,0x24,0x5c,0xc2,0xd3,0xac,0x62,0x91,0x95,0xe4,0x79,
    0xe7,0xc8,0x37,0x6d,0x8d,0xd5,0x4e,0xa9,0x6c,0x56,0xf4,0xea,0x65,0x7a,0xae,0x08,
    0xba,0x78,0x25,0x2e,0x1c,0xa6,0xb4,0xc6,0xe8,0xdd,0x74,0x1f,0x4b,0xbd,0x8b,0x8a,
    0x70,0x3e,0xb5,0x66,0x48,0x03,0xf6,0x0e,0x61,0x35,0x57,0xb9,0x86,0xc1,0x1d,0x9e,
    0xe1,0xf8,0x98,0x11,0x69,0xd9,0x8e,0x94,0x9b,0x1e,0x87,0xe9,0xce,0x55,0x28,0xdf,
    0x8c,0xa1,0x89,0x0d,0xbf,0xe6,0x42,0x68,0x41,0x99,0x2d,0x0f,0xb0,0x54,0xbb,0x16]


def _xtime(a):
    a <<= 1
    if a & 0x100:
        a ^= 0x11b
    return a & 0xff


def _gmul(a, b):
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        b >>= 1
        a = _xtime(a)
    return p & 0xff


_RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1b,0x36]


def _keyexp(key):
    Nk = len(key) // 4  # AES-128
    w = [list(key[4*i:4*i+4]) for i in range(Nk)]
    for i in range(Nk, 4*10 + 4):
        temp = list(w[i-1])
        if i % Nk == 0:
            temp = temp[1:] + temp[:1]          # RotWord
            temp = [_SBOX[b] for b in temp]      # SubWord
            temp[0] ^= _RCON[i // Nk - 1]
        elif Nk > 6 and i % Nk == 4:
            temp = [_SBOX[b] for b in temp]
        w.append([w[i-Nk][j] ^ temp[j] for j in range(4)])
    return w


def _addroundkey(state, w, r):
    for c in range(4):
        wk = w[r*4+c]
        for r2 in range(4):
            state[r2][c] ^= wk[r2]


def _subbytes(state):
    for r in range(4):
        for c in range(4):
            state[r][c] = _SBOX[state[r][c]]


def _shiftrows(state):
    state[1] = state[1][1:] + state[1][:1]
    state[2] = state[2][2:] + state[2][:2]
    state[3] = state[3][3:] + state[3][:3]


def _mixcolumns(state):
    for c in range(4):
        a = [state[r][c] for r in range(4)]
        state[0][c] = _gmul(a[0],2) ^ _gmul(a[1],3) ^ a[2] ^ a[3]
        state[1][c] = a[0] ^ _gmul(a[1],2) ^ _gmul(a[2],3) ^ a[3]
        state[2][c] = a[0] ^ a[1] ^ _gmul(a[2],2) ^ _gmul(a[3],3)
        state[3][c] = _gmul(a[0],3) ^ a[1] ^ a[2] ^ _gmul(a[3],2)


def _aes128_ecb_encrypt_block(key, block):
    # 列主序：block[c*4+r] -> state[r][c]
    state = [[block[c*4+r] for c in range(4)] for r in range(4)]
    w = _keyexp(key)
    _addroundkey(state, w, 0)
    for rnd in range(1, 10):
        _subbytes(state)
        _shiftrows(state)
        _mixcolumns(state)
        _addroundkey(state, w, rnd)
    _subbytes(state)
    _shiftrows(state)
    _addroundkey(state, w, 10)
    out = bytearray(16)
    for r in range(4):
        for c in range(4):
            out[c*4+r] = state[r][c]
    return bytes(out)


def _aes_cfb(key, iv, data, decrypt):
    """AES-128-CFB, segment_size=128（整块反馈）。解密/加密均只需 E_K。"""
    if _CAES is not None:
        c = _CAES.new(key, _CAES.MODE_CFB, iv=iv, segment_size=128)
        return c.decrypt(data) if decrypt else c.encrypt(data)
    # 纯 Python 兜底
    out = bytearray()
    prev = iv
    n = len(data)
    i = 0
    while i < n:
        seg = data[i:i+16]
        ek = _aes128_ecb_encrypt_block(key, prev)
        seglen = len(seg)
        x = bytes(seg[j] ^ ek[j] for j in range(seglen))
        out.extend(x)
        prev = seg if decrypt else x
        i += 16
    return bytes(out)


if threading is not None:
    _IMG_POOL_LOCK = threading.Lock()
    _IMG_TL = threading.local()
else:
    _IMG_POOL_LOCK = None
    _IMG_TL = None

_IMG_POOL = None


def _img_pool():
    if concurrent.futures is None or threading is None:
        return None
    global _IMG_POOL
    if _IMG_POOL is None:
        with _IMG_POOL_LOCK:
            if _IMG_POOL is None:
                _IMG_POOL = concurrent.futures.ThreadPoolExecutor(
                    max_workers=8, thread_name_prefix='jd-pic')
    return _IMG_POOL


def _img_session(proxy):
    if threading is None:
        s = requests.Session()
        s.trust_env = False
        s.headers.update({'User-Agent': 'okhttp/3.12.0'})
        if proxy:
            s.proxies = {'http': proxy, 'https': proxy}
        return s
    s = getattr(_IMG_TL, 's', None)
    if s is None:
        s = requests.Session()
        s.trust_env = False
        s.headers.update({'User-Agent': 'okhttp/3.12.0'})
        _IMG_TL.s = s
    if proxy:
        s.proxies = {'http': proxy, 'https': proxy}
    return s


class Spider(Spider):

    DEFAULT_HOST = 'https://api.xhsdns.cn'
    DNS_NAME = 'dns.dyttdns.com'
    APPID = '__UNI__CPA0011'
    PLATFORM = '1101'
    VERSION = '1.0.0'
    VERSION_CODE = '1'
    AES_KEY = b'UeFk58Si151OmxqH'
    UA = 'okhttp/3.12.0'
    TIMEOUT = 8

    FALLBACK_CLASS = [('1', '电影'), ('2', '电视剧'), ('3', '综艺'), ('4', '动漫'), ('5', '短剧')]

    IMG_TIMEOUT = 5
    IMG_DEADLINE = 5.5
    IMG_BUDGET = 3481600
    IMG_MAX_ONE = 1024 * 1024
    IMG_CACHE_TTL = 21600
    B64_MAGIC = (('/9j/', 'image/jpeg'), ('UklG', 'image/webp'),
                 ('iVBOR', 'image/png'), ('R0lG', 'image/gif'))
    BIN_MAGIC = (b'\xff\xd8\xff', b'RIFF', b'\x89PNG\r\n\x1a\n', b'GIF8')

    def init(self, extend=''):
        self._mem = {}
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'User-Agent': self.UA})
        cfg = {}
        if isinstance(extend, dict):
            cfg = extend
        elif isinstance(extend, str) and extend.strip():
            try:
                obj = json.loads(extend)
                if isinstance(obj, dict):
                    cfg = obj
            except Exception:
                cfg = {}
        self.host = str(cfg.get('base') or cfg.get('host') or self.DEFAULT_HOST).rstrip('/')
        proxy = cfg.get('proxy')
        self.proxy = str(proxy or '')
        self.session.proxies = {'http': proxy, 'https': proxy} if proxy else {}
        _trace('init 完成 host=%s proxy=%s' % (self.host, self.proxy or '无'))
        _trace('运行环境: Crypto=%s threading=%s futures=%s requests=%s' % (
            _CAES is not None, threading is not None,
            concurrent.futures is not None, requests.__version__))

    # ---------- 安全缓存：运行环境没有 getCache/setCache 时降级为内存缓存 ----------
    def _cget(self, key):
        try:
            return self._cget(key)
        except Exception as e:
            _trace('getCache 不可用(%s)，用内存缓存' % e)
            return self._mem.get(key)

    def _cset(self, key, val, ttl=0):
        try:
            self._cset(key, val, ttl)
        except Exception:
            try:
                self._mem[key] = val
            except Exception:
                pass

    def getName(self):
        return '剧岛4K'

    def isVideoFormat(self, url):
        return bool(re.search(r'\.(m3u8|mp4|flv|ts)(\?|#|$)', str(url or ''), re.I))

    def manualVideoCheck(self):
        return False

    def destroy(self):
        try:
            self.session.close()
        except Exception:
            pass
        global _IMG_POOL
        try:
            if _IMG_POOL_LOCK is not None:
                with _IMG_POOL_LOCK:
                    if _IMG_POOL is not None:
                        _IMG_POOL.shutdown(wait=False)
                        _IMG_POOL = None
        except Exception:
            pass

    def localProxy(self, param):
        return None

    def _enc(self, text):
        iv = bytes(random.randrange(256) for _ in range(16))
        ct = _aes_cfb(self.AES_KEY, iv, text.encode('utf-8'), False)
        hx = ct.hex().upper()
        return hx[:16] + iv.hex().upper() + hx[16:]

    def _dec(self, s):
        if len(s) < 48:
            iv_hex, body = s[len(s) - 32:], s[:len(s) - 32]
        else:
            iv_hex, body = s[16:48], s[0:16] + s[48:]
        raw = _aes_cfb(self.AES_KEY, bytes.fromhex(iv_hex), bytes.fromhex(body), True)
        return raw.decode('utf-8', 'replace')

    def _base(self):
        cached = self._cget('jd_base')
        if cached:
            return cached
        found = self._discover_host()
        base = found or self.host
        _trace('base 解析: 发现=%s 使用=%s' % (found or '无', base))
        self._cset('jd_base', base, 3600)
        return base

    def _discover_host(self):
        for doh in ('https://dns.alidns.com/resolve?name=%s&type=TXT' % self.DNS_NAME,
                    'https://doh.pub/dns-query?name=%s&type=TXT' % self.DNS_NAME):
            try:
                r = self.session.get(doh, headers={'accept': 'application/dns-json'}, timeout=self.TIMEOUT)
                for ans in (r.json().get('Answer') or []):
                    if ans.get('type') != 16:
                        continue
                    for cand in str(ans.get('data') or '').strip('"').split(','):
                        cand = cand.strip()
                        if not cand:
                            continue
                        url = cand if cand.startswith('http') else 'https://' + cand
                        url = url.rstrip('/')
                        try:
                            if self.session.get(url + '/ok.txt', timeout=self.TIMEOUT).text.strip().startswith('200'):
                                return url
                        except Exception as e:
                            _trace('ok.txt 探测失败 %s: %r' % (url, e))
                            continue
            except Exception as e:
                _trace('DoH 失败 %s: %r' % (doh, e))
                continue
        return ''

    def _common(self):
        return 'appid=%s&platform=%s&version=%s&versionCode=%s' % (
            self.APPID, self.PLATFORM, self.VERSION, self.VERSION_CODE)

    def _call(self, path, payload=None, extra=''):
        try:
            base = self._base()
            if payload is None:
                r = self.session.get(base + path + '?' + self._common() + (extra or ''), timeout=self.TIMEOUT)
            else:
                body = 'data=' + requests.utils.quote(
                    self._enc(json.dumps(payload, ensure_ascii=False, separators=(',', ':'))))
                r = self.session.post(base + path, data=body, timeout=self.TIMEOUT,
                                      headers={'Content-Type': 'application/x-www-form-urlencoded'})
            text = r.text or ''
            _trace('%s%s HTTP=%d 长度=%d' % (base, path, r.status_code, len(text)))
            if not text.strip():
                _trace('%s 响应为空' % path)
                return None, None, ''
            try:
                obj = json.JSONDecoder().raw_decode(text.lstrip())[0]
            except Exception as e:
                _trace('%s 响应非JSON: %r | 前100: %s' % (path, e, text[:100]))
                return None, None, text[:200]
            if not isinstance(obj, dict):
                return None, obj, ''
            data = obj.get('data')
            if isinstance(data, str) and data:
                try:
                    data = json.JSONDecoder().raw_decode(self._dec(data))[0]
                except Exception as e:
                    _trace('%s 解密失败: %r' % (path, e))
            _trace('%s code=%s msg=%s data=%s' % (
                path, obj.get('code'), obj.get('msg'),
                ('list×%d' % len(data.get('list') or [])) if isinstance(data, dict) and 'list' in data
                else type(data).__name__))
            return obj.get('code'), data, obj.get('msg')
        except Exception as e:
            _trace('%s 调用异常: %r' % (path, e))
            raise

    @staticmethod
    def _txt(v):
        if v is None:
            return ''
        s = str(v)
        for _ in range(3):
            n = html.unescape(s)
            if n == s:
                break
            s = n
        s = re.sub(r'\s+', ' ', s).strip()
        return s.replace('$', ' ').replace('#', ' ').replace('@@', ' ')

    def _pic_data(self, url):
        url = str(url or '').strip()
        if not url or url.startswith('data:') or not url.startswith('http'):
            return url
        ck = 'jd_pic_' + hashlib.md5(url.encode('utf-8')).hexdigest()
        hit = self._cget(ck)
        if hit is not None:
            return hit or url
        out = url
        try:
            raw = _img_session(self.proxy).get(url, timeout=self.IMG_TIMEOUT).content or b''
            if any(raw.startswith(m) for m in self.BIN_MAGIC):
                out = url
            else:
                txt = re.sub(r'\s+', '', raw.decode('utf-8', 'ignore'))
                if len(txt) > 64 and len(txt) <= self.IMG_MAX_ONE \
                        and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', txt):
                    mime = next((mt for mg, mt in self.B64_MAGIC if txt.startswith(mg)), '')
                    if mime:
                        out = 'data:%s;base64,%s' % (mime, txt)
            self._cset(ck, out if out != url else '', self.IMG_CACHE_TTL)
        except Exception:
            pass
        return out

    def _fill_pics(self, items, key='vod_pic'):
        urls = []
        for it in items or []:
            u = str(it.get(key) or '').strip()
            if u.startswith('http') and not u.startswith('data:'):
                urls.append(u)
        urls = list(dict.fromkeys(urls))
        if not urls:
            return items
        pool = _img_pool()
        if pool is None:
            return items
        futs = {pool.submit(self._pic_data, u): u for u in urls}
        done, pending = concurrent.futures.wait(futs, timeout=self.IMG_DEADLINE)
        res = []
        for f in done:
            u = futs[f]
            try:
                v = f.result()
            except Exception:
                v = u
            res.append((len(v) if v.startswith('data:') else 0, u, v))
        res.sort(key=lambda x: x[0])
        got, budget, over = {}, self.IMG_BUDGET, 0
        for n, u, v in res:
            if n and n > budget:
                v, over = u, over + 1
            elif n:
                budget -= n
            got[u] = v
        if pending or over:
            print('[剧岛4K] 封面内联降级：未取回 %d 张 / 超预算 %d 张，本次退回原 URL'
                  % (len(pending), over))
        for it in items:
            u = str(it.get(key) or '').strip()
            if u in got:
                it[key] = got[u]
        return items

    @staticmethod
    def _int(v, d=0):
        try:
            return int(str(v).strip())
        except Exception:
            return d

    @staticmethod
    def _norm(extend):
        out = {}
        if isinstance(extend, dict):
            for k, v in extend.items():
                if isinstance(v, dict):
                    v = v.get('v') or v.get('n') or ''
                if v not in (None, '', '全部'):
                    out[str(k)] = str(v)
        return out

    def _mkid(self, vid, name, pic):
        return '%s@@%s@@%s' % (vid, self._txt(name), pic or '')

    def _unmkid(self, raw):
        parts = str(raw).split('@@')
        vid = parts[0] if parts else ''
        name = parts[1] if len(parts) > 1 else ''
        pic = parts[2] if len(parts) > 2 else ''
        return vid, name, pic

    def _mkplay(self, vid, key, idx):
        return '%s@@%s@@%d' % (vid, key or '', idx)

    def _item(self, x):
        x = x or {}
        vid = str(x.get('vod_id') or '')
        name = self._txt(x.get('vod_name'))
        pic = str(x.get('vod_pic') or '')
        return {
            'vod_id': self._mkid(vid, name, pic),
            'vod_name': name or vid,
            'vod_pic': pic,
            'vod_remarks': self._txt(x.get('vod_remarks')),
            'vod_blurb': self._txt(x.get('vod_blurb'))[:180],
            'vod_tag': 'file',
            'vod_year': self._txt(x.get('vod_year')),
            'vod_area': self._txt(x.get('vod_area')),
        }

    def homeContent(self, filter):
        classes = []
        try:
            _, d, _ = self._call('/api/app/home')
            for t in (d or {}).get('type') or []:
                tid = str(t.get('id') or '')
                if tid:
                    classes.append({'type_id': tid, 'type_name': self._txt(t.get('name')) or tid})
        except Exception as e:
            _trace('home 失败: %r' % e)
            print('[剧岛4K] home 失败: %s' % e)
        if not classes:
            classes = [{'type_id': i, 'type_name': n} for i, n in self.FALLBACK_CLASS]
        vod_ids = [c['type_id'] for c in classes]
        try:
            filters = self._filters(vod_ids)
        except Exception as e:
            _trace('filters 异常: %r' % e)
            filters = {}
        classes += [{'type_id': 'topic', 'type_name': '专题'},
                    {'type_id': 'live', 'type_name': '直播'},
                    {'type_id': 'short', 'type_name': '短片'},
                    {'type_id': 'diag', 'type_name': '诊断'}]
        return {'class': classes, 'filters': filters}

    def _filters(self, type_ids):
        dims = self._cget('jd_dims')
        if dims is None:
            dims = []
            try:
                _, d, _ = self._call('/api/app/vodClass')
                for key, label in (('class', '类型'), ('area', '地区'), ('year', '年份'),
                                   ('lang', '语言'), ('sort', '排序')):
                    vals = []
                    for x in (d or {}).get(key) or []:
                        v = str(x.get('id') or '')
                        n = self._txt(x.get('name')) or (v or '全部')
                        vals.append({'n': n, 'v': v, 'type_id': v, 'type_name': n})
                    if vals:
                        dims.append({'key': key, 'name': label, 'value': vals})
            except Exception as e:
                print('[剧岛4K] filters 失败: %s' % e)
            self._cset('jd_dims', dims, 21600)
        return {tid: [dict(x) for x in dims] for tid in type_ids}

    def homeVideoContent(self):
        try:
            _, d, _ = self._call('/api/app/vodList', extra='&type=1&page=1')
            items = [self._item(x) for x in (d or {}).get('list') or []]
            if not items:
                items = [{'vod_id': 'diag_home', 'vod_name': '首页无数据，请进"诊断"分类查看日志',
                          'vod_tag': 'file', 'vod_pic': '', 'vod_remarks': '诊断',
                          'vod_blurb': '', 'vod_year': '', 'vod_area': ''}]
            return {'list': self._fill_pics(items)}
        except Exception as e:
            _trace('homeVideo 失败: %r' % e)
            print('[剧岛4K] homeVideo 失败: %s' % e)
            return {'list': [{'vod_id': 'diag_home', 'vod_name': '首页异常:%s' % e,
                              'vod_tag': 'file', 'vod_pic': '', 'vod_remarks': '诊断',
                              'vod_blurb': '请进"诊断"分类查看完整日志', 'vod_year': '', 'vod_area': ''}]}

    def _diag_items(self):
        items = []
        for i, t in enumerate(_TRACE[-60:]):
            items.append({'vod_id': 'diag%d' % i, 'vod_name': t, 'vod_tag': 'file',
                          'vod_pic': '', 'vod_remarks': '', 'vod_blurb': ''})
        if not items:
            items = [{'vod_id': 'diag_none', 'vod_name': '无跟踪记录（可能模块加载就失败了）',
                      'vod_tag': 'file', 'vod_pic': '', 'vod_remarks': '', 'vod_blurb': ''}]
        return items

    def categoryContent(self, tid, pg, filter, extend):
        pg = self._int(pg, 1) or 1
        tid = str(tid or '')
        try:
            if tid == 'diag':
                return {'page': 1, 'pagecount': 1, 'limit': 60,
                        'total': len(_TRACE), 'list': self._diag_items()}
            if tid == 'live':
                return self._live_index(pg)
            if tid.startswith('livetype$'):
                return self._live_list(tid.split('$', 1)[1])
            if tid == 'topic':
                return self._topic_index(pg)
            if tid.startswith('topic$'):
                return self._topic_detail(tid.split('$', 1)[1], pg)
            if tid == 'short':
                return self._short_list(pg)
            return self._vod_list(tid, pg, extend)
        except Exception as e:
            _trace('category %s 失败: %r' % (tid, e))
            print('[剧岛4K] category %s 失败: %s' % (tid, e))
            return {'page': pg, 'pagecount': pg, 'limit': 20, 'total': 0,
                    'list': [{'vod_id': 'diag_cat', 'vod_name': '分类异常:%s' % e,
                              'vod_tag': 'file', 'vod_pic': '', 'vod_remarks': '诊断',
                              'vod_blurb': '请进"诊断"分类查看完整日志', 'vod_year': '', 'vod_area': ''}]}

    def _vod_list(self, tid, pg, extend):
        ext = self._norm(extend)
        extra = '&type=%s&page=%d' % (requests.utils.quote(tid), pg)
        for k in ('class', 'area', 'year', 'lang', 'sort'):
            if ext.get(k):
                extra += '&%s=%s' % (k, requests.utils.quote(ext[k]))
        _, d, _ = self._call('/api/app/vodList', extra=extra)
        d = d or {}
        items = [self._item(x) for x in d.get('list') or []]
        total = self._int(d.get('count'))
        pcount = self._int(d.get('page_total')) or (pg + 1 if items else pg)
        return {'page': pg, 'pagecount': max(pcount, pg), 'limit': len(items) or 20,
                'total': total, 'list': self._fill_pics(items)}

    def _live_raw(self):
        cached = self._cget('jd_live')
        if cached:
            return cached
        _, d, _ = self._call('/api/live/list')
        d = d if isinstance(d, dict) else {}
        data = {'types': d.get('types') or [], 'list': d.get('list') or []}
        if data['list']:
            self._cset('jd_live', data, 1800)
        return data

    def _live_index(self, pg):
        data = self._live_raw()
        items = []
        for t in data.get('types') or []:
            name = self._txt(t if isinstance(t, str) else (t or {}).get('name'))
            if not name:
                continue
            items.append({'vod_id': 'livetype$%s' % name, 'vod_name': name, 'vod_pic': '',
                          'vod_remarks': '直播', 'vod_blurb': '', 'vod_tag': 'folder'})
        return {'page': 1, 'pagecount': 1, 'limit': len(items) or 1, 'total': len(items), 'list': items}

    def _live_list(self, ltype):
        data = self._live_raw()
        items = []
        for x in data.get('list') or []:
            if self._txt(x.get('live_type')) != ltype:
                continue
            lid = str(x.get('live_id') or '')
            name = self._txt(x.get('live_name'))
            items.append({
                'vod_id': 'live@@%s@@%s@@%s' % (lid, name, x.get('live_pic') or ''),
                'vod_name': name or lid,
                'vod_pic': str(x.get('live_pic') or ''),
                'vod_remarks': self._txt(x.get('live_type')),
                'vod_blurb': '',
                'vod_tag': 'file',
            })
        return {'page': 1, 'pagecount': 1, 'limit': len(items) or 1, 'total': len(items),
                'list': self._fill_pics(items)}

    def _live_url(self, live_id):
        try:
            _, d, _ = self._call('/api/live/list')
            for x in (d or {}).get('list') or []:
                if str(x.get('live_id')) == str(live_id):
                    return str(x.get('live_url') or '')
        except Exception as e:
            print('[剧岛4K] live_url 失败: %s' % e)
        for x in self._live_raw().get('list') or []:
            if str(x.get('live_id')) == str(live_id):
                return str(x.get('live_url') or '')
        return ''

    def _topic_index(self, pg):
        _, d, _ = self._call('/api/app/topicIndexList')
        items = []
        for x in (d or {}).get('list') or []:
            tid = str(x.get('topic_id') or '')
            if not tid:
                continue
            items.append({
                'vod_id': 'topic$%s' % tid,
                'vod_name': self._txt(x.get('topic_name')) or tid,
                'vod_pic': str(x.get('topic_pic') or ''),
                'vod_remarks': '专题',
                'vod_blurb': self._txt(x.get('topic_blurb'))[:120],
                'vod_tag': 'folder',
            })
        return {'page': 1, 'pagecount': 1, 'limit': len(items) or 1, 'total': len(items),
                'list': self._fill_pics(items)}

    def _topic_detail(self, tid, pg):
        _, d, _ = self._call('/api/app/topicDetails', extra='&id=%s&page=%d&limit=20' % (tid, pg))
        d = d if isinstance(d, dict) else {}
        items = [self._item(x) for x in d.get('vod_list') or []]
        total = self._int(d.get('page_vod_total'))
        pcount = self._int(d.get('page_total')) or 1
        return {'page': pg, 'pagecount': max(pcount, pg), 'limit': len(items) or 20,
                'total': total or len(items), 'list': self._fill_pics(items)}

    def _short_list(self, pg):
        _, d, _ = self._call('/api/short/list', payload={'page': str(pg)})
        d = d if isinstance(d, dict) else {}
        items = []
        for x in d.get('list') or []:
            sid = str(x.get('short_id') or '')
            if not sid:
                continue
            name = self._txt(x.get('short_title'))
            cnt = self._int(x.get('short_play_count'))
            self._cache_short(sid, x.get('short_play_url'))
            items.append({
                'vod_id': 'short@@%s@@%d@@%s@@%s' % (sid, pg, name, x.get('short_pic') or ''),
                'vod_name': name or sid,
                'vod_pic': str(x.get('short_pic') or ''),
                'vod_remarks': ('全%d集' % cnt) if cnt else '短片',
                'vod_blurb': self._txt(x.get('short_content'))[:180],
                'vod_tag': 'file',
            })
        total = self._int(d.get('count'))
        pcount = self._int(d.get('page_total')) or (pg + 1 if items else pg)
        return {'page': pg, 'pagecount': max(pcount, pg), 'limit': len(items) or 20,
                'total': total, 'list': self._fill_pics(items)}

    def _cache_short(self, sid, url):
        url = str(url or '')
        if sid and url:
            self._cset('jd_short_' + str(sid), url, 1800)

    def _short_item(self, sid, pg):
        _, d, _ = self._call('/api/short/list', payload={'page': str(pg)})
        for x in (d or {}).get('list') or []:
            if str(x.get('short_id')) == str(sid):
                self._cache_short(sid, x.get('short_play_url'))
                return x
        return None

    def detailContent(self, ids):
        raw = ids[0] if isinstance(ids, (list, tuple)) and ids else str(ids)
        raw = str(raw)
        try:
            if raw.startswith('live@@'):
                parts = raw.split('@@')
                name = parts[2] if len(parts) > 2 else parts[1]
                pic = self._pic_data(parts[3] if len(parts) > 3 else '')
                vod = {'vod_id': raw, 'vod_name': name, 'vod_pic': pic, 'vod_content': '',
                       'vod_play_from': '直播', 'vod_play_url': '%s$%s' % (name or '直播', 'live@@' + parts[1])}
                return {'list': [vod]}

            if raw.startswith('short@@'):
                parts = raw.split('@@')
                sid = parts[1] if len(parts) > 1 else ''
                pg = parts[2] if len(parts) > 2 else '1'
                name = parts[3] if len(parts) > 3 else sid
                pic = self._pic_data(parts[4] if len(parts) > 4 else '')
                x = self._short_item(sid, pg) or {}
                cnt = self._int(x.get('short_play_count'))
                vod = {'vod_id': raw, 'vod_name': name or sid, 'vod_pic': pic,
                       'vod_content': self._txt(x.get('short_content')),
                       'vod_remarks': ('全%d集' % cnt) if cnt else '短片',
                       'vod_play_from': '短片',
                       'vod_play_url': '正片$short@@%s@@%s' % (sid, pg)}
                return {'list': [vod]}

            vid, name, pic = self._unmkid(raw)
            vod = {'vod_id': raw, 'vod_name': name or vid, 'vod_pic': self._pic_data(pic),
                   'vod_content': '', 'vod_play_from': '', 'vod_play_url': ''}
            froms, urls = [], []
            for ln in self._lines(vid):
                eps = ln.get('list') or []
                if not eps:
                    continue
                key = str(ln.get('key') or '')
                froms.append(self._txt(ln.get('name')) or key or '线路')
                urls.append('#'.join(
                    '%s$%s' % (self._txt(e.get('name')) or ('第%d集' % (i + 1)), self._mkplay(vid, key, i))
                    for i, e in enumerate(eps)))
            vod['vod_play_from'] = '$$$'.join(froms)
            vod['vod_play_url'] = '$$$'.join(urls)
            {'list': [vod]}
        except Exception as e:
            print('[剧岛4K] detail 失败: %s' % e)
            return {'list': []}

    def _lines(self, vid):
        _, d, _ = self._call('/api/vod/play', payload={'id': str(vid), 'player': ''})
        if isinstance(d, list):
            return [x for x in d if isinstance(x, dict) and x.get('list')]
        return []

    def searchContent(self, key, quick, pg=1):
        pg = self._int(pg, 1) or 1
        try:
            extra = '&wd=%s&page=%d' % (requests.utils.quote(str(key)), pg)
            _, d, _ = self._call('/api/app/searchList', extra=extra)
            d = d if isinstance(d, dict) else {}
            items = [self._item(x) for x in d.get('list') or []]
            total = self._int(d.get('count'))
            pcount = self._int(d.get('page_total')) or (pg + 1 if items else pg)
            return {'page': pg, 'pagecount': max(pcount, pg), 'limit': len(items) or 20,
                    'total': total, 'list': self._fill_pics(items)}
        except Exception as e:
            print('[剧岛4K] search 失败: %s' % e)
            return {'page': pg, 'pagecount': pg, 'limit': 20, 'total': 0, 'list': []}

    def playerContent(self, flag, id, vipFlags=None):
        pid = str(id or '')
        if '$' in pid and not pid.startswith(('live@@', 'short@@')):
            pid = pid.split('$', 1)[1]
        url = ''
        try:
            if pid.startswith('live@@'):
                url = self._live_url(pid.split('@@')[1])
            elif pid.startswith('short@@'):
                parts = pid.split('@@')
                sid = parts[1] if len(parts) > 1 else ''
                pg = parts[2] if len(parts) > 2 else '1'
                x = self._short_item(sid, pg) or {}
                url = str(x.get('short_play_url') or '')
                if not url:
                    url = str(self._cget('jd_short_' + sid) or '')
            else:
                parts = pid.split('@@')
                vid = parts[0]
                key = parts[1] if len(parts) > 1 else ''
                idx = self._int(parts[2]) if len(parts) > 2 else 0
                url = self._ep_url(vid, key, idx)
        except Exception as e:
            print('[剧岛4K] play 失败: %s' % e)
        if not url:
            return {'parse': 0, 'jx': 0, 'url': '', 'header': {}}
        return {'parse': 0, 'jx': 0, 'url': url, 'header': {'User-Agent': self.UA}}

    def _ep_url(self, vid, key, idx):
        lines = self._lines(vid)
        if not lines:
            return ''
        target = None
        for ln in lines:
            if str(ln.get('key') or '') == str(key or ''):
                target = ln
                break
        if target is None:
            target = lines[0]
        eps = target.get('list') or []
        if not eps:
            return ''
        if idx < 0 or idx >= len(eps):
            idx = 0
        return str(eps[idx].get('url') or '')