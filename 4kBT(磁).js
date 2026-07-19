import { Crypto, load, _ } from 'assets://js/lib/cat.js';

let siteUrl = 'https://4kbt.org';
let siteKey = '';
let siteType = 0;
let headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36',
    'Referer': siteUrl
};

async function request(reqUrl, postData, agentSp, get) {
    let res = await req(reqUrl, {
        method: get ? 'get' : 'post',
        headers: headers,
        data: postData || {},
        postType: get ? '' : 'form',
    });
    return res.content;
}

async function init(cfg) {
    try {
        siteKey = cfg.skey;
        siteType = cfg.stype;
        return true; 
    } catch (e) {
        console.log('init err', e);
    }
}

async function home(filter) {
    let classes = [{
        type_id: 'movie',
        type_name: '4K电影',
    },{
        type_id: 'episodes',
        type_name: '4K剧集',
    }];

    return JSON.stringify({
        class: classes,
        filters: {}
    });
}

async function homeVod() {
    let videos = await getVideos(siteUrl);
    return JSON.stringify({
        list: videos,
    });
}

async function category(tid, pg, filter, extend) {
    let url = siteUrl + '/category/' + tid;
    if (pg && pg > 1) {
        url = url + '/page/' + pg;
    }
    
    let videos = await getVideos(url);
    return JSON.stringify({
        list: videos,
    });
}

async function detail(id) {
    try {
        let url = id; 
        const html = await request(url, null, null, true);
        const $ = load(html);
        
        let title = $('div.article-header > h1.post-title').text().trim();
        let pic = $('article.post-content img').first().attr('src');
        
        const magnetItems = $('div.magnet-links ul li');
        let playUrls = _.map(magnetItems, (n) => {
            let name = $(n).find('div.magnet-link-name').text().trim();
            let link = $(n).find('div.magnet-link-url a').attr('href');
            return name + '$' + link;
        });

        $('article.post-content script, article.post-content style, .magnet-links, .magnet-subtitle-download, .entry-copyright').remove();
        
        let htmlContent = $('article.post-content').html() || '';
        htmlContent = htmlContent.replace(/<br\s*\/?>/gi, '\n').replace(/<\/p>/gi, '\n');
        
        let cleanText = $(`<div>${htmlContent}</div>`).text();

        let vod_area = (cleanText.match(/◎产\s*地\s*(.*)/) || ['', ''])[1].trim();
        let vod_class = (cleanText.match(/◎类\s*别\s*(.*)/) || ['', ''])[1].trim();
        let vod_year = (cleanText.match(/◎年\s*代\s*(\d{4})/) || cleanText.match(/◎上映日期\s*(\d{4})/) || ['', ''])[1].trim();
        let vod_director = (cleanText.match(/◎导\s*演\s*(.*)/) || ['', ''])[1].trim();
        
        let vod_actor = '';
        let actorMatch = cleanText.match(/◎主\s*演\s*([\s\S]*?)(?=◎|$)/);
        if (actorMatch) {
            vod_actor = actorMatch[1].replace(/\n/g, ' ').replace(/\s+/g, ' ').trim();
        }

        let vod_remarks = (cleanText.match(/◎标\s*签\s*(.*)/) || ['', ''])[1].trim();
        
        let vod_content = '';
        let contentMatch = cleanText.match(/◎简\s*介[\s\n]*([\s\S]*)/);
        if (contentMatch) {
            vod_content = contentMatch[1].trim();
        } else {
            vod_content = cleanText.trim();
        }

        const video = {
            vod_id: id,
            vod_name: title,
            vod_pic: pic,
            vod_area: vod_area,
            vod_class: vod_class,
            vod_year: vod_year,
            vod_director: vod_director,
            vod_actor: vod_actor,
            vod_remarks: vod_remarks,
            vod_content: vod_content,
            vod_play_from: '磁力直连', 
            vod_play_url: playUrls.join('#'),
        };
        
        const list = [video];
        return JSON.stringify({ list });
    } catch (e) {
        console.log('detail err', e);
    }
    return null;
}

async function search(wd, quick, pg) {
    return JSON.stringify({
        list: [],
    });
}

async function play(flag, id, flags) {
    let url = id;
    
    if (url.startsWith('magnet:')) {
        return JSON.stringify({
            parse: 0,
            jx: 0,
            url: url
        });
    }
    
    return JSON.stringify({
        parse: 1,
        jx: 1,
        url: url,
        header: headers
    });
}

async function getVideos(url) {
    const html = await request(url, null, null, true);
    const $ = load(html);
    const cards = $('div.posts-warp article.post-item');
    let videos = _.map(cards, (n) => {
        let node = $(n).find('a.media-img');
        let id = node.attr('href'); 
        let name = node.attr('title');
        let pic = node.attr('data-bg'); 
        let remark = $(n).find('.tips-badge').text().trim(); 

        return {
            vod_id: id,
            vod_name: name,
            vod_pic: pic,
            vod_remarks: remark,
        };
    });
    return videos;
}

export function __jsEvalReturn() {
    return {
        init: init,
        home: home,
        homeVod: homeVod,
        category: category,
        detail: detail,
        play: play,
        search: search,
    };
}