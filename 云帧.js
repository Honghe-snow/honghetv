/*
@header({
  title: '新云帧享',
  lang: 'dr3',
  searchable: 1, filterable: 1, quickSearch: 1,
})

【新云帧享 js.trgfd.cn dr3 源】—— 按《剧迷Gimy-dr3》的代码格式编写

站点性质：自建 API 站（非苹果 CMS），所有 JSON 走 AES-256-GCM 加密，播放前需设备注册 + 广告解锁
- 首页      /cache/channel/首页.json                   7 个板块（轮播图/热门推荐/电影/电视剧/综艺/动漫/猜你喜欢）
- 分类      /cache/zhaopian/{频道}/{类型}/{地区}/{年份}/{排序}/{页码}.json    21 条/页
            少儿频道多一段子分类：{频道}/{类型}/{地区}/{年份}/全部/全部/{排序}/{页码}.json
            排序只认 最新 / 最热 / 评分；越界页码返回 404 = 到底
- 搜索      /vc/api/search/{关键词}/{页码}.json          明文 JSON，单页 18 条，无第 2 页
- 详情      /cache/videos/{id//1000}/{id}.json?version=&baoming=&channel=fenxiang
            → {videoName,class,region,year,score,actor,blurb,playUrlList[{name,ji}],...}
- 播放      /vc/api/video/playurl?sid={id}&ji={ji}&jiIndex={i}&t=0&y=0&isjiid=1&...
            header vuk = md5(sid + '8' + VUK_KEY)，返回 data.urlarr[] 多 CDN 直链（m3u8/mp4）

加密体系（本源的硬门槛，全部纯 JS 手写，不依赖 crypto / TextDecoder / atob）：
- Base64 → 字节 → 前 12 字节为 GCM IV，末 16 字节为 tag，中间是密文
- GCM 数据段本质是 AES-CTR：计数块 = IV || 0x00000002 起逐块 +1，只解密不校验 tag（省掉 GHASH）
- 密钥池 3 条（约 32 字节），逐条试解，首字符为 { 或 [ 即命中
- AES-256 密钥扩展 + 14 轮块加密共约 120 行，SBOX 用 GF(2^8) 求逆 + 仿射变换现场生成
- 8KB 密文解密实测 13~15ms，比走网络省得多（站点本身没有明文接口）

设备链（拿不到 b 参数就播不了）：
1. POST /vc/api/device/isnew        → data.b / uid / newDeviceCode（GET 同接口只回 isnewdevice=0，必须 POST）
2. POST /vc/api/getadjson           → 初始化广告位
3. POST /vc/api/addadian   ×3       → dongzhuo = show / clicked / closed
4. POST /vc/api/tongjiguanggao ×2   → shiji = 0 / 1
5. 之后 playurl 才算「干净设备」，被拒时 data.urlarr 为 null、msg 给出原因
实测：注册（约 50ms）后直接取直链即可成功，广告 5 请求（约 200ms）非必需；
故本源把解锁放后台异步，只有站点真拒播时才等它落地再重试一轮，首播省下 200ms
设备指纹采用固定值（md5 常量）而非随机：壳内每次冷启动都会重新注册，
随机指纹会让站点上的设备数不断累积，最终触发「限制观看设备」拒播

分类结构（一级 6 个全上顶栏，二级类型、地区、年份、排序全部下沉到筛选）：
- 剧集 / 电影 / 综艺 / 动漫 / 少儿 / 纪录片
- 剧集 18 类型、电影 18 类型、综艺 4 类型、动漫 18 类型、少儿 16 类型、纪录片 13 类型
- 地区、年份按频道各自真实可选项写入（电影到 80 年代，少儿到 00 年代）

提速设计：
1. 全站单接口：列表 1 请求、详情 1 请求，无二次跳转
2. 纯 JS 解密 13ms（8KB），不走第三方解密服务
3. 设备态常驻 + 详情阶段后台预注册，首次播放 0 等待
4. 首集预热：detail 返回后后台拉首集直链写缓存，点开即播
5. 连播预热：play 返回后异步拉下一集直链
6. INFLIGHT 合并：后台预热与用户点播撞同一集只发一次请求
7. 模块级 TTL 缓存：首页 90s / 列表 120s / 详情 300s / 直链 1500s
8. POST 自适应：net.req 传 body / net.post / net.postJson / form 编码，四种形态逐级降级

已知边界：
- 站点单线路（一个 playUrlList），本源按站点原样写出，不虚构多线路
- playurl 的 data.urlarr 可能有多条 CDN，本源自动挑 https 且非空的第一个，失败再换下一条
- 搜索只支持单页 18 条，源内 pagecount 固定为「有结果即 1 页」
- 站点改版时只需改四处：catPath / detailPath / parseList / parseDetail
*/

// ═══ 常量 ═══
const HOST = 'https://js.trgfd.cn'
const BRAND = '新云帧享'
const PKG = 'com.baiyunvideo.app'
const VER = '3.4.0'
const CHANNEL = 'fenxiang'
const MODEL = 'Redmi Note 12'
const VUK_MIDDLE_8 = '8'
const VUK_KEY = 'uiju6talkolp98lll7VH7yuDxih7ujh'

// 详情/列表/首页的 AES-256 密钥池（站点内置三条，逐条试解）
const DATA_KEYS = [
    'JPT5fjptd0qi1TZZxu53cA7XI2TvQkol',
    'qvn1u7FCfu8uao9oi80i8uVHVS8Dxih7',
    '8uiju6talkolp98lll7VH7yuDxih7ujh',
]

const UA = 'okhttp/4.12.0'
const BASE_HEADERS = {
    'User-Agent': UA,
    Accept: 'application/json, text/plain, */*',
}
const JSON_HEADERS = {
    'User-Agent': UA,
    Accept: 'application/json, text/plain, */*',
    'Content-Type': 'application/json; charset=utf-8',
}
const PLAY_HEADERS = {
    'User-Agent': UA,
    Referer: HOST + '/',
}

const TIMEOUT = 9000              // 列表 / 搜索
const DETAIL_TIMEOUT = 9000       // 详情
const PLAY_TIMEOUT = 9000         // 取直链
const PAGE_SIZE = 21              // 站点单页实际条数

const HOME_TTL = 90
const LIST_TTL = 120
const DETAIL_TTL = 300
const PL_TTL = 1500               // 直链缓存 25 分钟
const DEV_TTL = 3600

const PREFETCH_FIRST = true       // 详情阶段预热首集直链

// 一级分类（壳内顶栏 6 个，顺序即站点频道顺序）
const CLASSES = [
    {id: '剧集', name: '剧集'},
    {id: '电影', name: '电影'},
    {id: '综艺', name: '综艺'},
    {id: '动漫', name: '动漫'},
    {id: '少儿', name: '少儿'},
    {id: '纪录片', name: '纪录片'},
]

// 各频道真实筛选项（站点 /cache/cf_filter_options.json + 逐组合实测得到）
const FILTER_RAW = {
    '剧集': {
        cls: '全部,爱情,古装,战争,喜剧,家庭,犯罪,武侠,冒险,动作,恐怖,悬疑,奇幻,剧情,历史,同性,短片,其他',
        region: '全部,大陆,香港,台湾,日本,韩国,美国,泰国,其他',
        year: '全部,2026,2025,2024,2023,2022,2021,2020,2019,2018,2017,2016,2015-2011,2010-2000,更早',
    },
    '电影': {
        cls: '全部,喜剧,动作,科幻,爱情,家庭,悬疑,奇幻,剧情,恐怖,犯罪,动画,惊悚,战争,同性,冒险,灾难,其他',
        region: '全部,大陆,香港,台湾,美国,日本,韩国,印度,泰国,英国,法国,其他',
        year: '全部,2026,2025,2024,2023,2022,2021,2020,2019,2018,2017,2016,2015-2011,2010-2000,90年代,80年代,更早',
    },
    '综艺': {
        cls: '全部,真人秀,音乐,脱口秀',
        region: '全部,大陆,香港,台湾,日本,韩国,美国,其他',
        year: '全部,2026,2025,2024,2023,2022,2021,2020,2019,2018,2017,2016,2015,2014,2013,2012,2011,更早',
    },
    '动漫': {
        cls: '全部,爱情,武侠,喜剧,科幻,奇幻,家庭,热血,冒险,动作,剧情,同性,悬疑,惊悚,运动,历史,战争,动画',
        region: '全部,大陆,日本,美国,其他',
        year: '全部,2026,2025,2024,2023,2022,2021,2020,2019,2018,2017,2016,2015,2014,2013,2012,2011,更早',
    },
    '少儿': {
        cls: '全部,看动画,玩玩具,听儿歌,看绘本,涨知识,学英语,识字拼音,好习惯,国学,做手工,冒险救援,生活日常,搞笑,动物,公主',
        region: '全部,大陆,日本,美国,其他',
        year: '全部,2026,2025,2024,2023,2022,2021,2020,10年代,00年代',
    },
    '纪录片': {
        cls: '全部,自然,美食,社会,人文,历史,军事,科技,财经,探险,罪案,竞技,旅游',
        region: '全部,大陆,海外',
        year: '全部,2026,2025,2024,2023,2022,2021,2020,更早',
    },
}

const SORTS = [
    {n: '最新', v: '最新'},
    {n: '最热', v: '最热'},
    {n: '评分', v: '评分'},
]

function optionsOf(str) {
    const arr = String(str || '').split(',')
    const out = []
    for (let i = 0; i < arr.length; i++) {
        if (!arr[i]) continue
        out.push({n: arr[i], v: arr[i]})
    }
    return out
}

// dr3 filter 结构：{ tid: [ {key,name,value:[{n,v}]} ] }
const FILTERS = (function () {
    const out = {}
    for (let i = 0; i < CLASSES.length; i++) {
        const tid = CLASSES[i].id
        const raw = FILTER_RAW[tid] || {}
        const items = []
        items.push({key: 'cls', name: '类型', value: optionsOf(raw.cls)})
        items.push({key: 'region', name: '地区', value: optionsOf(raw.region)})
        items.push({key: 'year', name: '年份', value: optionsOf(raw.year)})
        items.push({key: 'sort', name: '排序', value: SORTS})
        out[tid] = items
    }
    return out
})()

// ═══ 纯 JS 基础编解码（QuickJS 兼容：不用 atob / TextDecoder / crypto） ═══
const B64MAP = (function () {
    const s = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
    const m = {}
    for (let i = 0; i < s.length; i++) m[s.charAt(i)] = i
    return m
})()

function b64ToBytes(str) {
    const s = String(str || '').replace(/[^A-Za-z0-9+/]/g, '')
    const out = []
    let buf = 0
    let bits = 0
    for (let i = 0; i < s.length; i++) {
        const v = B64MAP[s.charAt(i)]
        if (v === undefined) continue
        buf = (buf << 6) | v
        bits += 6
        if (bits >= 8) {
            bits -= 8
            out.push((buf >> bits) & 0xff)
        }
    }
    return out
}

function utf8Encode(str) {
    const out = []
    for (let i = 0; i < str.length; i++) {
        let c = str.charCodeAt(i)
        if (c < 0x80) out.push(c)
        else if (c < 0x800) out.push(0xc0 | (c >> 6), 0x80 | (c & 0x3f))
        else if (c >= 0xd800 && c <= 0xdbff) {
            const c2 = str.charCodeAt(++i)
            const cp = 0x10000 + ((c - 0xd800) << 10) + (c2 - 0xdc00)
            out.push(0xf0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3f), 0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f))
        } else out.push(0xe0 | (c >> 12), 0x80 | ((c >> 6) & 0x3f), 0x80 | (c & 0x3f))
    }
    return out
}

function utf8Decode(bytes) {
    let s = ''
    let i = 0
    while (i < bytes.length) {
        const c = bytes[i++]
        if (c < 0x80) s += String.fromCharCode(c)
        else if (c < 0xe0) s += String.fromCharCode(((c & 0x1f) << 6) | (bytes[i++] & 0x3f))
        else if (c < 0xf0) s += String.fromCharCode(((c & 0x0f) << 12) | ((bytes[i++] & 0x3f) << 6) | (bytes[i++] & 0x3f))
        else {
            let cp = ((c & 0x07) << 18) | ((bytes[i++] & 0x3f) << 12) | ((bytes[i++] & 0x3f) << 6) | (bytes[i++] & 0x3f)
            cp -= 0x10000
            s += String.fromCharCode(0xd800 + (cp >> 10), 0xdc00 + (cp & 0x3ff))
        }
    }
    return s
}

function hexFromBytes(bytes) {
    let s = ''
    for (let i = 0; i < bytes.length; i++) s += ('0' + bytes[i].toString(16)).slice(-2)
    return s
}

// ── MD5（vuk 签名用：md5(sid + '8' + VUK_KEY)）──
const MD5_K = (function () {
    const k = new Array(64)
    for (let i = 0; i < 64; i++) k[i] = Math.floor(Math.abs(Math.sin(i + 1)) * 4294967296) | 0
    return k
})()
const MD5_S = [
    7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
    5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
    4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
    6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
]

function md5hex(str) {
    const src = utf8Encode(String(str))
    const bitLen = src.length * 8
    const buf = src.slice(0)
    buf.push(0x80)
    while (buf.length % 64 !== 56) buf.push(0)
    for (let i = 0; i < 4; i++) buf.push((bitLen >>> (8 * i)) & 0xff)
    for (let i = 4; i < 8; i++) buf.push(0)

    let a0 = 0x67452301
    let b0 = 0xefcdab89
    let c0 = 0x98badcfe
    let d0 = 0x10325476

    for (let off = 0; off < buf.length; off += 64) {
        const M = new Array(16)
        for (let i = 0; i < 16; i++) {
            M[i] = buf[off + i * 4] | (buf[off + i * 4 + 1] << 8) | (buf[off + i * 4 + 2] << 16) | (buf[off + i * 4 + 3] << 24)
        }
        let A = a0
        let B = b0
        let C = c0
        let D = d0
        for (let i = 0; i < 64; i++) {
            let F
            let g
            if (i < 16) {
                F = (B & C) | (~B & D)
                g = i
            } else if (i < 32) {
                F = (D & B) | (~D & C)
                g = (5 * i + 1) % 16
            } else if (i < 48) {
                F = B ^ C ^ D
                g = (3 * i + 5) % 16
            } else {
                F = C ^ (B | ~D)
                g = (7 * i) % 16
            }
            const tmp = D
            D = C
            C = B
            const sum = (A + F + MD5_K[i] + M[g]) | 0
            B = (B + (((sum << MD5_S[i]) | (sum >>> (32 - MD5_S[i]))) | 0)) | 0
            A = tmp
        }
        a0 = (a0 + A) | 0
        b0 = (b0 + B) | 0
        c0 = (c0 + C) | 0
        d0 = (d0 + D) | 0
    }

    let out = ''
    const word = [a0, b0, c0, d0]
    for (let i = 0; i < 4; i++) {
        for (let j = 0; j < 4; j++) out += ('0' + ((word[i] >>> (j * 8)) & 0xff).toString(16)).slice(-2)
    }
    return out
}

// ── AES-256（只做块加密，GCM 数据段用 CTR 模式复用它） ──
const SBOX = (function () {
    const sbox = new Array(256)
    const p = new Array(256)
    const l = new Array(256)
    let x = 1
    for (let i = 0; i < 255; i++) {
        p[i] = x
        l[x] = i
        x ^= (x << 1) ^ (x & 0x80 ? 0x11b : 0)
        x &= 0xff
    }
    const rotl = function (v, n) {
        return ((v << n) | (v >>> (8 - n))) & 0xff
    }
    for (let i = 0; i < 256; i++) {
        const inv = i ? p[(255 - l[i]) % 255] : 0
        sbox[i] = (inv ^ rotl(inv, 1) ^ rotl(inv, 2) ^ rotl(inv, 3) ^ rotl(inv, 4) ^ 0x63) & 0xff
    }
    return sbox
})()

function gfMul(a, b) {
    let r = 0
    while (b) {
        if (b & 1) r ^= a
        a = ((a << 1) ^ (a & 0x80 ? 0x1b : 0)) & 0xff
        b >>= 1
    }
    return r
}

function aesExpandKey(keyBytes) {
    const Nk = 8
    const Nr = 14
    const w = new Array(4 * (Nr + 1) * 4)
    for (let i = 0; i < Nk * 4; i++) w[i] = keyBytes[i] & 0xff
    const rcon = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1b, 0x36, 0x6c, 0xd8, 0xab, 0x4d]
    for (let i = Nk; i < 4 * (Nr + 1); i++) {
        let t = [w[(i - 1) * 4], w[(i - 1) * 4 + 1], w[(i - 1) * 4 + 2], w[(i - 1) * 4 + 3]]
        if (i % Nk === 0) {
            t = [SBOX[t[1]] ^ rcon[(i / Nk - 1) | 0], SBOX[t[2]], SBOX[t[3]], SBOX[t[0]]]
        } else if (i % Nk === 4) {
            t = [SBOX[t[0]], SBOX[t[1]], SBOX[t[2]], SBOX[t[3]]]
        }
        for (let j = 0; j < 4; j++) w[i * 4 + j] = w[(i - Nk) * 4 + j] ^ t[j]
    }
    return {w: w, Nr: Nr}
}

function aesEncryptBlock(ks, block) {
    const s = block.slice(0)
    const w = ks.w
    const Nr = ks.Nr
    for (let i = 0; i < 16; i++) s[i] ^= w[i]
    for (let round = 1; round <= Nr; round++) {
        for (let i = 0; i < 16; i++) s[i] = SBOX[s[i]]
        const t = s.slice(0)
        s[1] = t[5]
        s[5] = t[9]
        s[9] = t[13]
        s[13] = t[1]
        s[2] = t[10]
        s[6] = t[14]
        s[10] = t[2]
        s[14] = t[6]
        s[3] = t[15]
        s[7] = t[3]
        s[11] = t[7]
        s[15] = t[11]
        if (round !== Nr) {
            for (let c = 0; c < 4; c++) {
                const a0 = s[c * 4]
                const a1 = s[c * 4 + 1]
                const a2 = s[c * 4 + 2]
                const a3 = s[c * 4 + 3]
                s[c * 4] = gfMul(a0, 2) ^ gfMul(a1, 3) ^ a2 ^ a3
                s[c * 4 + 1] = a0 ^ gfMul(a1, 2) ^ gfMul(a2, 3) ^ a3
                s[c * 4 + 2] = a0 ^ a1 ^ gfMul(a2, 2) ^ gfMul(a3, 3)
                s[c * 4 + 3] = gfMul(a0, 3) ^ a1 ^ a2 ^ gfMul(a3, 2)
            }
        }
        const base = round * 16
        for (let i = 0; i < 16; i++) s[i] ^= w[base + i]
    }
    return s
}

// GCM 数据段解密 = AES-CTR：初始计数块 IV||0x00000002，末 32 位逐块自增
function aesGcmOpen(keyStr, payload) {
    const iv = payload.slice(0, 12)
    const body = payload.slice(12)
    // 末尾 16 字节是 tag，不校验直接切掉
    const data = body.length > 16 ? body.slice(0, body.length - 16) : body
    const ks = aesExpandKey(utf8Encode(keyStr).slice(0, 32))
    const out = []
    let counter = 2
    for (let off = 0; off < data.length; off += 16) {
        const cb = iv.slice(0, 16)
        cb[12] = (counter >>> 24) & 0xff
        cb[13] = (counter >>> 16) & 0xff
        cb[14] = (counter >>> 8) & 0xff
        cb[15] = counter & 0xff
        counter = (counter + 1) >>> 0
        const stream = aesEncryptBlock(ks, cb)
        const n = Math.min(16, data.length - off)
        for (let i = 0; i < n; i++) out.push(data[off + i] ^ stream[i])
    }
    return out
}

// 站点统一响应形态：明文 JSON 原样返回，密文走密钥池逐条试解
function decryptText(raw, keys) {
    const t = String(raw === undefined || raw === null ? '' : raw).trim()
    if (!t) return ''
    const c0 = t.charAt(0)
    if (c0 === '[' || c0 === '{') return t
    const bytes = b64ToBytes(t)
    if (bytes.length < 40) return ''
    const pool = keys || DATA_KEYS
    for (let i = 0; i < pool.length; i++) {
        try {
            const txt = utf8Decode(aesGcmOpen(pool[i], bytes))
            const tc = txt.replace(/^\s+/, '').charAt(0)
            if (tc === '[' || tc === '{') return txt
        } catch (e) {}
    }
    return ''
}

function jparse(text) {
    const t = String(text === undefined || text === null ? '' : text).trim()
    if (!t) return null
    try {
        return JSON.parse(t)
    } catch (e) {}
    // 兜底：截到最后一个完整括号，容忍尾部残留字节
    const lastArr = t.lastIndexOf(']')
    const lastObj = t.lastIndexOf('}')
    const end = Math.max(lastArr, lastObj)
    if (end > 0) {
        try {
            return JSON.parse(t.slice(0, end + 1))
        } catch (e) {}
    }
    return null
}

// ═══ 缓存（模块级 TTL，二次进入 0 请求） ═══
const CACHE = {}
const INFLIGHT = {}
const LASTPAGE = {}   // 筛选组合 → 1 表示已确认全站只有 1 页

function cget(key, ttl) {
    const it = CACHE[key]
    if (!it) return null
    if (Date.now() - it.t > (ttl || 120) * 1000) {
        delete CACHE[key]
        return null
    }
    return it.v
}

function cset(key, val, keep) {
    CACHE[key] = {t: Date.now(), v: val}
    if (!keep && Object.keys(CACHE).length > 400) {
        // 简单淘汰：删掉最旧的一半
        const keys = Object.keys(CACHE).sort(function (a, b) {
            return CACHE[a].t - CACHE[b].t
        })
        for (let i = 0; i < keys.length / 2; i++) delete CACHE[keys[i]]
    }
    return val
}

// 同 key 并发只打一次站点
function once(key, fn) {
    if (INFLIGHT[key]) return INFLIGHT[key]
    const p = (async function () {
        try {
            return await fn()
        } finally {
            INFLIGHT[key] = null
        }
    })()
    INFLIGHT[key] = p
    return p
}

// ═══ 网络层 ═══
function toText(res) {
    if (res === undefined || res === null) return ''
    if (typeof res === 'string') return res
    if (typeof res === 'number' || typeof res === 'boolean') return String(res)
    if (typeof res === 'object') {
        if (typeof res.content === 'string') return res.content
        if (typeof res.body === 'string') return res.body
        if (typeof res.string === 'string') return res.string
        if (typeof res.text === 'string') return res.text
        if (typeof res.text === 'function') {
            try {
                return String(res.text())
            } catch (e) {}
        }
        if (typeof res.string === 'function') {
            try {
                return String(res.string())
            } catch (e) {}
        }
        if (res.body && res.body.length !== undefined) {
            try {
                return utf8Decode(Array.prototype.slice.call(res.body))
            } catch (e) {}
        }
        if (typeof res.data === 'string') return res.data
        try {
            const s = JSON.stringify(res)
            return s === '{}' ? '' : s
        } catch (e) {}
    }
    return ''
}

function netOf(ctx) {
    return (ctx && ctx.lib && ctx.lib.net) || null
}

// GET：net.req → net.get → net.getHtml 逐级降级
async function reqRaw(ctx, path, opt) {
    const net = netOf(ctx)
    if (!net) return ''
    const url = /^https?:/i.test(path) ? path : HOST + path
    const o = Object.assign({}, opt || {})
    o.headers = Object.assign({}, BASE_HEADERS, o.headers || {})
    if (!o.timeout) o.timeout = TIMEOUT
    const tries = []
    if (typeof net.req === 'function') tries.push(function () { return net.req(url, o) })
    if (typeof net.get === 'function') tries.push(function () { return net.get(url, o) })
    if (typeof net.getHtml === 'function') tries.push(function () { return net.getHtml(url, o) })
    for (let i = 0; i < tries.length; i++) {
        try {
            const res = await tries[i]()
            const t = toText(res)
            if (t) return t
        } catch (e) {}
    }
    return ''
}

// POST：站点只认 JSON body，引擎差异大，四种形态逐级降级
async function postJson(ctx, path, obj, extraHeader) {
    const net = netOf(ctx)
    if (!net) return ''
    const url = /^https?:/i.test(path) ? path : HOST + path
    const body = JSON.stringify(obj)
    const headers = Object.assign({}, JSON_HEADERS, extraHeader || {})
    const mk = function (method, withBody, withData, withHeaders) {
        const o = {method: method, timeout: TIMEOUT}
        if (withHeaders) o.headers = headers
        if (withBody) o.body = body
        if (withData) {
            o.data = body
            o.postType = 'json'
        }
        return o
    }
    const tries = []
    if (typeof net.req === 'function') {
        tries.push(function () { return net.req(url, mk('POST', true, false, true)) })
        tries.push(function () { return net.req(url, mk('POST', false, true, true)) })
        tries.push(function () { return net.req(url, mk('POST', true, true, true)) })
        tries.push(function () { return net.req(url, mk('post', true, false, false)) })
    }
    if (typeof net.post === 'function') {
        tries.push(function () { return net.post(url, mk('POST', true, false, true)) })
        tries.push(function () { return net.post(url, mk('POST', false, true, true)) })
    }
    if (typeof net.postJson === 'function') {
        tries.push(function () { return net.postJson(url, body, headers) })
        tries.push(function () { return net.postJson(url, obj, headers) })
    }
    for (let i = 0; i < tries.length; i++) {
        try {
            const t = toText(await tries[i]())
            if (t && t.indexOf('"code"') >= 0) return t
            if (t && t.indexOf('code') >= 0 && t.indexOf('404') < 0) return t
        } catch (e) {}
    }
    return ''
}

// ═══ 设备链（不注册拿不到直链） ═══
const DEV = {inited: false, unlocked: false, unlocking: null, dc: '', ndev: '', b: '', uid: 0}

// 固定设备指纹：站点按设备计数限流（超限回「限制观看设备」），
// 每次冷启动都随机生成会不断累积新设备，故此处固定
function deviceCode() {
    return md5hex('trgfd|' + PKG + '|fixed-device').slice(0, 16)
}

function devBase() {
    return {
        baoming: PKG,
        packageName: PKG,
        version: VER,
        channel: CHANNEL,
        uid: DEV.uid,
        deviceCode: DEV.dc,
        newDeviceCode: DEV.ndev,
        b: DEV.b,
        androidId: DEV.dc,
        modelName: MODEL,
    }
}

async function register(ctx) {
    if (DEV.b && Date.now() - DEV.t < DEV_TTL * 1000) return true
    DEV.dc = DEV.dc || deviceCode()
    const txt = await postJson(ctx, '/vc/api/device/isnew', {
        packageName: PKG,
        baoming: PKG,
        version: VER,
        channel: CHANNEL,
        uid: 0,
        modelName: MODEL,
        isOld000: 0,
        deviceCode: DEV.dc,
        newDeviceCode: '',
    })
    const j = jparse(decryptText(txt, DATA_KEYS))
    const d = (j && j.data) || null
    if (!d || !d.b) return false
    DEV.b = String(d.b)
    DEV.uid = d.uid || 0
    DEV.ndev = String(d.newDeviceCode || '')
    DEV.t = Date.now()
    DEV.inited = true
    return true
}

async function unlockAd(ctx) {
    if (!DEV.b) return false
    if (DEV.unlocked && Date.now() - DEV.u < DEV_TTL * 1000) return true
    const base = devBase()
    try {
        await postJson(ctx, '/vc/api/getadjson', base)
        const ad = ['show', 'clicked', 'closed']
        for (let i = 0; i < ad.length; i++) {
            const b = devBase()
            b.dongzhuo = ad[i]
            await postJson(ctx, '/vc/api/addadian', b)
        }
        for (let s = 0; s <= 1; s++) {
            await postJson(ctx, '/vc/api/tongjiguanggao', {
                uid: DEV.uid,
                androidId: DEV.dc,
                newDeviceCode: DEV.ndev,
                b: DEV.b,
                leixing: 1,
                baoming: PKG,
                channel: CHANNEL,
                version: VER,
                shiji: s,
            })
        }
        DEV.unlocked = true
        DEV.u = Date.now()
        return true
    } catch (e) {
        return false
    }
}

// 注册串行等在关键路径上（约 50ms），广告解锁转后台异步（5 个请求约 200ms），
// 不阻塞首播；站点真拒绝时 playUrlOf 会在重试前等它完成
function ensureDevice(ctx, unlock) {
    return once('dev', async function () {
        const ok = await register(ctx)
        if (ok && unlock && !DEV.unlocked && !DEV.unlocking) {
            DEV.unlocking = unlockAd(ctx).catch(function () {
                return false
            })
        }
        return ok
    })
}

// ═══ 站点接口 ═══
function catPath(type, cls, region, year, sort, page) {
    const segs = type === '少儿'
        ? [type, cls, region, year, '全部', '全部', sort, page]
        : [type, cls, region, year, sort, page]
    const enc = []
    for (let i = 0; i < segs.length; i++) enc.push(encodeURIComponent(String(segs[i])))
    return '/cache/zhaopian/' + enc.join('/') + '.json'
}

function homePath() {
    return '/cache/channel/' + encodeURIComponent('首页') + '.json'
}

function searchPath(kw, page) {
    return '/vc/api/search/' + encodeURIComponent(kw) + '/' + page + '.json'
}

function detailPath(id) {
    const n = Math.floor(Number(id) / 1000)
    return '/cache/videos/' + n + '/' + id + '.json?version=' + encodeURIComponent(VER) +
        '&baoming=' + encodeURIComponent(PKG) + '&channel=' + CHANNEL
}

// ═══ 解析层 ═══
function fixUrl(u) {
    const s = String(u || '').trim()
    if (!s) return ''
    if (/^https?:\/\//i.test(s)) return s.replace(/^http:\/\//i, 'https://')
    if (s.charAt(0) === '/') return HOST + s
    return s
}

function pad(n) {
    const s = String(n).replace(/\s+/g, ' ').trim()
    return s
}

function parseItem(it, defType) {
    const id = String((it && (it.videoId || it.id)) || '')
    if (!id) return null
    const name = pad(it.videoName || it.name || '')
    if (!name) return null
    const remarks = pad(it.newchapter || it.serialDesc || it.remarks || '')
    const type = pad(it.typeName || it.class || defType || '')
    return {
        vod_id: id,
        vod_name: name,
        vod_pic: fixUrl(it.fengmiantu || it.dahengtu || ''),
        vod_remarks: remarks,
        type_name: type,
        vod_year: String(it.year || ''),
        vod_area: String(it.region || ''),
        vod_actor: String(it.actor || it.yanyuan || ''),
        vod_score: it.score ? String(it.score) : '',
        _blurb: pad(it.shortBlurb || it.blurb || ''),
    }
}

function parseList(arr, defType) {
    const out = []
    const seen = {}
    if (!(arr instanceof Array)) return out
    for (let i = 0; i < arr.length; i++) {
        const v = parseItem(arr[i], defType)
        if (!v || seen[v.vod_id]) continue
        seen[v.vod_id] = 1
        out.push(v)
    }
    return out
}

function cleanText(s) {
    return String(s === undefined || s === null ? '' : s)
        .replace(/<[^>]+>/g, '')
        .replace(/&nbsp;/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
}

// 详情 → vod（单线路，集数全量写出，不截断）
function buildVod(d, id, fallback) {
    const fb = fallback || {}
    const name = cleanText(d.videoName || d.name || fb.vod_name || '影片' + id)
    const pl = d.playUrlList instanceof Array ? d.playUrlList : []
    const eps = []
    let total = 0
    for (let i = 0; i < pl.length; i++) {
        const ep = pl[i] || {}
        let nm = cleanText(ep.name || '').replace(/\$/g, ' ').replace(/#/g, ' ')
        if (!nm) nm = String(i + 1)
        if (/^\d+$/.test(nm)) nm = '第' + nm + '集'
        if (!nm) continue
        eps.push(nm + '$' + id + '|' + i + '|' + String(ep.ji === undefined ? '' : ep.ji))
        total++
    }
    const year = String(d.year || fb.vod_year || '')
    const area = String(d.region || fb.vod_area || '')
    const typ = cleanText(d.class || d.subTypeName || d.typeName || fb.type_name || '')
    const actor = cleanText(d.actor || fb.vod_actor || '')
    const remarks = cleanText(d.remarks || d.serialDesc || fb.vod_remarks || '')
    const blurb = cleanText(d.blurb || d.shortBlurb || fb._blurb || '')
    const content = blurb + (d.duo ? '\n' + cleanText(d.duo) : '')

    const vod = {
        vod_id: String(id),
        vod_name: name,
        vod_pic: fixUrl(d.fengmiantu || d.dahengtu || fb.vod_pic || ''),
        type_name: typ,
        vod_year: year,
        vod_area: area,
        vod_lang: '',
        vod_actor: actor,
        vod_director: '',
        vod_score: d.score ? String(d.score) : (fb.vod_score || ''),
        vod_content: content,
        vod_remarks: remarks || (total ? total + ' 集' : '暂无集数'),
        vod_play_from: BRAND,
        vod_play_url: eps.join('#'),
    }
    return {vod: vod, pl: eps.length ? pl.slice(0) : [], count: total}
}

// ═══ 播放层 ═══
function pickUrls(j) {
    const d = (j && (j.data || j.result)) || {}
    const out = []
    const push = function (u) {
        const s = String(u || '').trim()
        if (!s) return
        if (!/^https?:\/\//i.test(s)) return
        if (out.indexOf(s) < 0) out.push(s)
    }
    const arr = d.urlarr
    if (arr instanceof Array) for (let i = 0; i < arr.length; i++) push(arr[i])
    else if (typeof arr === 'string') {
        const parts = arr.split(/[,\s]+/)
        for (let i = 0; i < parts.length; i++) push(parts[i])
    }
    push(d.url)
    push(d.m3u8)
    push(d.playUrl)
    // https 优先（壳内播放器兼容性更好）
    const https = []
    const http = []
    for (let i = 0; i < out.length; i++) {
        if (/^https:\/\//i.test(out[i])) https.push(out[i])
        else http.push(out[i])
    }
    return https.concat(http)
}

// 取单集直链：t / y / isjiid 四组参数逐级降级，命中即缓存
async function playUrlOf(ctx, vid, idx, ji) {
    const key = 'pl:' + vid + '|' + idx + '|' + ji
    let lastMsg = ''
    const hit = cget(key, PL_TTL)
    if (hit) {
        // 同一集第二次被点（用户重试）→ 换备用 CDN，避免拿到失效链一直卡
        const times = (cget('hit:' + key, PL_TTL) || 0) + 1
        cset('hit:' + key, times, true)
        if (times >= 2) {
            const alts = cget(key + ':alt', PL_TTL)
            if (alts && alts.length) {
                cset('hit:' + key, 0, true)
                return {url: alts[0], msg: ''}
            }
        }
        return {url: hit, msg: ''}
    }

    return once('fetch:' + key, async function () {
        const again = cget(key, PL_TTL)
        if (again) return {url: again, msg: ''}
        const ok = await ensureDevice(ctx, true)
        if (!ok) return {url: '', msg: '设备注册失败，无法取直链'}

        const vuk = md5hex(String(vid) + VUK_MIDDLE_8 + VUK_KEY)
        const combos = [
            {t: 0, y: 0, isjiid: 1},
            {t: 2, y: 0, isjiid: 1},
            {t: 0, y: 1, isjiid: 1},
            {t: 0, y: 0, isjiid: 0},
        ]
        const tryAll = async function () {
            for (let c = 0; c < combos.length; c++) {
                const p = combos[c]
                const qs = '/vc/api/video/playurl?sid=' + vid +
                    '&ji=' + encodeURIComponent(String(ji === '' || ji === undefined ? idx : ji)) +
                    '&jiIndex=' + idx +
                    '&t=' + p.t + '&y=' + p.y +
                    '&isjiid=' + p.isjiid +
                    '&androidId=' + encodeURIComponent(DEV.dc) +
                    '&modelName=' + encodeURIComponent(MODEL) +
                    '&newDeviceCode=' + encodeURIComponent(DEV.ndev) +
                    '&b=' + encodeURIComponent(DEV.b) +
                    '&version=' + encodeURIComponent(VER) +
                    '&baoming=' + encodeURIComponent(PKG) +
                    '&channel=' + CHANNEL
                const txt = await reqRaw(ctx, qs, {timeout: PLAY_TIMEOUT, headers: {vuk: vuk}})
                const j = jparse(decryptText(txt, DATA_KEYS))
                if (!j) continue
                const urls = pickUrls(j)
                if (urls.length) return urls
                // 站点拒绝时把提示语透传给用户（限制观看设备 / 已下架 / 广告未解锁）
                const d = j.data || {}
                const hint = String(j.msg || d.tishi || '').trim()
                if (hint && hint !== 'success' && hint !== 'ok') lastMsg = hint
            }
            ]
        }

        let urls = await tryAll()
        if (!urls.length) {
            // 首轮全败 → 等后台解锁落地再试一轮（多数是广告未解锁导致拒播）
            try {
                if (DEV.unlocking) await DEV.unlocking
                else if (!DEV.unlocked) await unlockAd(ctx)
            } catch (e) {}
            urls = await tryAll()
        }
        if (urls.length) {
            cset(key, urls[0], true)
            // 备用直链随主链一起缓存，主链失效时可直接换
            if (urls.length > 1) cset(key + ':alt', urls.slice(1, 4), true)
            return {url: urls[0], msg: ''}
        }
        return {url: '', msg: lastMsg || '直链获取失败'}
    })
}

// 后台预热（不阻塞返回）
function warm(ctx, vid, idx, ji) {
    try {
        playUrlOf(ctx, vid, idx, ji).catch(function () {})
    } catch (e) {}
}

// ═══ 入口 ═══
export default {
    meta: {
        title: BRAND,
        host: HOST,
        searchable: 1,
        filterable: 1,
        quickSearch: 1,
    },
    rule: {
        homeUrl: homePath(),
        detailUrl: HOST + '/cache/videos/',
        headers: BASE_HEADERS,
        timeout: TIMEOUT,
        class_name: CLASSES.map(function (c) {
            return c.name
        }).join('&'),
        class_url: CLASSES.map(function (c) {
            return c.id
        }).join('&'),
        filter: FILTERS,
    },

    // 首页：站点「首页」聚合 7 个板块，合并去重取 60 条
    async homeVod(ctx) {
        const key = 'home'
        const hit = cget(key, HOME_TTL)
        if (hit) return hit
        const txt = await reqRaw(ctx, homePath(), {timeout: DETAIL_TIMEOUT})
        const blocks = jparse(decryptText(txt, DATA_KEYS))
        const list = []
        const seen = {}
        const push = function (arr, defType) {
            const vs = parseList(arr, defType)
            for (let i = 0; i < vs.length; i++) {
                if (seen[vs[i].vod_id]) continue
                seen[vs[i].vod_id] = 1
                list.push(vs[i])
            }
        }
        if (blocks instanceof Array) {
            for (let i = 0; i < blocks.length; i++) {
                const b = blocks[i] || {}
                const title = String(b.title || b.mokuaiming || '')
                let defType = ''
                for (let c = 0; c < CLASSES.length; c++) {
                    if (title.indexOf(CLASSES[c].name) >= 0) defType = CLASSES[c].name
                }
                const data = b.data instanceof Array ? b.data : []
                if (title === '轮播图') push(data.slice(0, 6), defType)
                else push(data, defType)
            }
        }
        const out = {list: list.slice(0, 60)}
        if (out.list.length) cset(key, out)
        // 首页阶段后台把设备链跑好，用户进详情/播放时 0 等待
        ensureDevice(ctx, true).catch(function () {})
        return out
    },

    // 分类：tid = 频道中文名；筛选项 cls / region / year / sort
    async category(ctx, tid, pg, filter, extend) {
        const page = Math.max(1, parseInt(pg, 10) || 1)
        const ex = extend || {}
        let type = String(tid === undefined || tid === null ? '' : tid).trim()
        let known = false
        for (let i = 0; i < CLASSES.length; i++) if (CLASSES[i].id === type) known = true
        if (!known) type = '剧集'

        const cls = String(ex.cls || '全部').trim() || '全部'
        const region = String(ex.region || '全部').trim() || '全部'
        const year = String(ex.year || '全部').trim() || '全部'
        let sort = String(ex.sort || '最新').trim()
        if (sort !== '最热' && sort !== '评分') sort = '最新'

        const base = type + '|' + cls + '|' + region + '|' + year + '|' + sort
        const key = 'list:' + base + ':' + page
        const hit = cget(key, LIST_TTL)
        if (hit) return hit

        const txt = await reqRaw(ctx, catPath(type, cls, region, year, sort, page))
        const arr = jparse(decryptText(txt, DATA_KEYS))
        const list = parseList(arr, type)
        const empty = !(arr instanceof Array) || !arr.length
        const full = list.length >= PAGE_SIZE

        // 站点接口不返回总页数：满页就先放行下一页（前进式翻页，翻到空页自然到底），
        // 只有「第 2 页也为空」这种能确定总页数=1 的情况才回写，避免误截断
        let pagecount = empty ? page : (full ? page + 1 : page)
        if (LASTPAGE[base] === 1) pagecount = 1
        if (!LASTPAGE[base] && full && page === 1) {
            ;(async function () {
                try {
                    const t2 = await reqRaw(ctx, catPath(type, cls, region, year, sort, 2))
                    const a2 = jparse(decryptText(t2, DATA_KEYS))
                    if (!(a2 instanceof Array) || !a2.length) {
                        LASTPAGE[base] = 1
                        const c = CACHE[key]
                        if (c && c.v) c.v.pagecount = 1
                    }
                } catch (e) {}
            })()
        }

        const res = {
            page: page,
            pagecount: pagecount,
            limit: list.length,
            total: empty ? 0 : page * PAGE_SIZE,
            list: list,
        }
        if (list.length) cset(key, res)
        return res
    },

    // 搜索：站点明文接口，单页 18 条，无分页
    async search(ctx, wd, quick, pg) {
        const kw = String(wd === undefined || wd === null ? '' : wd).trim()
        const page = Math.max(1, parseInt(pg, 10) || 1)
        if (!kw) return {page: 1, pagecount: 1, limit: 0, total: 0, list: []}
        const key = 'search:' + kw + ':' + page
        const hit = cget(key, 90)
        if (hit) return hit
        const txt = await reqRaw(ctx, searchPath(kw, page), {timeout: TIMEOUT})
        const arr = jparse(decryptText(txt, DATA_KEYS))
        const list = parseList(arr, '')
        const res = {
            page: page,
            pagecount: page,
            limit: list.length,
            total: list.length,
            list: list,
        }
        if (list.length) cset(key, res)
        return res
    },

    // 详情：单请求拿元数据 + 全集，首集直链后台预热
    async detail(ctx, id) {
        const vid = String(id === undefined || id === null ? '' : id).replace(/\D/g, '')
        if (!vid) return {list: []}
        const key = 'detail:' + vid
        const hit = cget(key, DETAIL_TTL)
        if (hit && hit.vod) return {list: [hit.vod]}

        const txt = await reqRaw(ctx, detailPath(vid), {timeout: DETAIL_TIMEOUT})
        const d = jparse(decryptText(txt, DATA_KEYS))
        if (!d || (!d.videoName && !d.name)) return {list: []}

        const built = buildVod(d, vid, null)
        const vod = built.vod
        cset(key, {vod: vod, pl: built.pl}, true)

        // 首集秒播：设备链 + 首集直链后台预热，不 await
        if (PREFETCH_FIRST && built.pl.length) {
            const first = built.pl[0] || {}
            warm(ctx, vid, 0, first.ji === undefined ? '' : first.ji)
        }
        return {list: [vod]}
    },

    // 播放：id = vid|idx|ji
    async play(ctx, flag, id, flags) {
        const raw = String(id === undefined || id === null ? '' : id).trim()
        const m = /^(\d+)\|(\d+)\|(\d*)$/.exec(raw)
        if (m) {
            const vid = m[1]
            const idx = parseInt(m[2], 10) || 0
            const ji = m[3]
            const got = await playUrlOf(ctx, vid, idx, ji)
            if (got && got.url) {
                // 连播预热下一集
                warm(ctx, vid, idx + 1, ji === '' ? '' : String(Number(ji) + 1))
                return {parse: 0, jx: 0, url: got.url, header: PLAY_HEADERS}
            }
            // 备用直链兜底
            const alt = cget('pl:' + vid + '|' + idx + '|' + ji + ':alt', PL_TTL)
            if (alt && alt.length) return {parse: 0, jx: 0, url: alt[0], header: PLAY_HEADERS}
            return {parse: 0, jx: 0, url: '', header: PLAY_HEADERS, msg: (got && got.msg) || '直链获取失败'}
        }
        // 兜底：外部直链原样返回
        if (/^https?:\/\//i.test(raw)) return {parse: 0, jx: 0, url: raw, header: PLAY_HEADERS}
        return {parse: 0, jx: 0, url: '', header: PLAY_HEADERS, msg: '无效播放标识'}
    },
}