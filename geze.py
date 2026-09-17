# -*- coding: utf-8 -*-
import json
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://gaze.red/"
}
BASE = "https://gaze.red"

def home():
    """首页：分类+推荐列表"""
    res_list = []
    # 注意：该站大量内容是JS异步加载，requests直接GET拿不到渲染后列表
    # 需要抓网站的后端json接口，替换下面url
    # api_url = f"{BASE}/api/home"
    # resp = requests.get(api_url, headers=HEADERS, timeout=12)
    # data = resp.json()

    return {
        "class": [
            {"type_id":"tv","type_name":"电视剧"},
            {"type_id":"movie","type_name":"电影"},
            {"type_id":"comic","type_name":"国漫"}
        ],
        "list": res_list,
        "page":1,
        "pagecount":1,
        "total":0
    }


def detail(vod_id):
    """详情页，vod_id为影片id"""
    play_url = ""
    # detail_api = f"{BASE}/api/detail?id={vod_id}"
    # resp = requests.get(detail_api, headers=HEADERS, timeout=12)
    # j = resp.json()
    # 解析集数与播放地址

    return {
        "vod_id": vod_id,
        "vod_name": "",
        "vod_pic": "",
        "vod_play_from": "gaze线路",
        "vod_play_url": play_url
    }


def search(wd):
    """搜索 wd关键词"""
    res_list = []
    # search_api = f"{BASE}/api/search?q={wd}"
    # resp = requests.get(search_api, headers=HEADERS, timeout=12)
    # j = resp.json()
    # 循环解析搜索结果填入res_list

    return {
        "list": res_list
    }


if __name__ == "__main__":
    print(json.dumps(home(), ensure_ascii=False))