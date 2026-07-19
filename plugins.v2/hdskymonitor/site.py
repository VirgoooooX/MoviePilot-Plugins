"""天空站点抓取与页面解析。"""
import re
import urllib.request
from .client import api_get

SITE_ID = 24

def get_site_cookie():
    """从 MP 获取天空站点的 cookie"""
    result = api_get("/api/v1/site/24")
    if result and isinstance(result, dict):
        return result.get("cookie", ""), result.get("ua", "") or "MoviePilot/2.13.6 (Linux 5.17.13-generic; x86_64)"
    return "", ""

def fetch_torrents_from_site(page=1):
    """直接抓取天空站点搜索结果页面，解析匹配的种子"""
    cookie, ua = get_site_cookie()
    if not cookie:
        logger.error("无法获取站点 cookie")
        return []
    
    # 搜索URL参数：电视剧分类 + 搜索关键词"去头尾广告纯享版"
    SEARCH_URL = (
        "https://hdsky.me/torrents.php?"
        "cat402=1&cat411=1&cat412=1&cat413=1"  # 电视剧分类
        "&search=%E5%8E%BB%E5%A4%B4%E5%B0%BE%E5%B9%BF%E5%91%8A%E7%BA%AF%E4%BA%AB%E7%89%88"  # 去头尾广告纯享版
        "&search_area=0"  # 搜索范围：标题+描述
        "&search_mode=0"  # 搜索模式
        "&incldead=1"     # 包含已删除种子
    )
    
    # 只抓取第一页
    if page > 1:
        return []
    
    url = SEARCH_URL
    
    headers = {
        "Cookie": cookie,
        "User-Agent": ua or "MoviePilot/2.13.6 (Linux 5.17.13-generic; x86_64)"
    }
    
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
    except Exception as e:
        logger.error(f"抓取站点页面失败: {e}")
        return []
    
    results = []
    
    # 匹配种子标题链接
    # 格式: <a target="_blank" title="Title"  href="details.php?id=619740&hit=1">
    torrent_pattern = re.compile(
        r'<a[^>]+title="([^"]{10,})"[^>]+href="details\.php\?id=(\d+)(?:&|&amp;)hit=1"',
        re.DOTALL
    )
    
    # 下载链接格式: download.php?id=619740&t=xxx&sign=xxx
    dl_pattern = re.compile(
        r'download\.php\?id=(\d+)(?:&|&amp;)t=(\d+)(?:&|&amp;)sign=([a-f0-9]+)'
    )
    
    # 找到所有种子标题
    torrent_matches = list(torrent_pattern.finditer(html))
    
    for idx, tm in enumerate(torrent_matches):
        title = tm.group(1).strip()
        torrent_id = tm.group(2)
        
        # 找到对应的下载链接（在标题附近搜索）
        # 限制搜索范围：当前种子标题之后、下一个种子标题之前，防止跨种子误匹配
        start_pos = tm.start()
        if idx + 1 < len(torrent_matches):
            end_pos = torrent_matches[idx + 1].start()
        else:
            end_pos = min(len(html), tm.end() + 5000)
        context = html[start_pos:end_pos]
        
        dl_match = dl_pattern.search(context)
        if not dl_match:
            continue
        
        enclosure = f"https://hdsky.me/download.php?id={dl_match.group(1)}&t={dl_match.group(2)}&sign={dl_match.group(3)}"
        
        # 提取描述（在标题后面的span中）
        # 描述格式：韩剧: 铁拳教育 / ... 全10集 | 主演: ...
        # 描述可能包含嵌套的HTML标签（如优惠剩余时间）
        # 优先匹配包含"韩剧"、"美剧"等关键词的内容
        # 使用非贪婪匹配，遇到[优惠剩余时间就停止
        desc_match = re.search(r'>(韩剧|国漫|美剧|日剧|英剧|电影|综艺|动漫)(.*?)(?:\[优惠剩余时间|\]</span>)', context, re.DOTALL)
        description = ""
        if desc_match:
            # 清理HTML标签，但保留文本内容
            desc_text = desc_match.group(1) + desc_match.group(2)
            description = re.sub(r'<[^>]+>', '', desc_text).strip()
        else:
            # 如果没有找到，尝试匹配包含"全X集"的内容
            desc_match2 = re.search(r'>((?:全|共)\d+.*?)(?:\[优惠剩余时间|\]</span>)', context, re.DOTALL)
            if desc_match2:
                desc_text = desc_match2.group(1)
                description = re.sub(r'<[^>]+>', '', desc_text).strip()
        
        # 如果没有找到描述，尝试提取包含"全"或"共"且包含"集"的span
        if not description:
            desc_match2 = re.search(r'<span[^>]*>([^<]*(?:全|共)\d+集[^<]*)</span>', context, re.DOTALL)
            if desc_match2:
                description = re.sub(r'<[^>]+>', '', desc_match2.group(1)).strip()
        
        # 如果还是没有找到，尝试提取包含"去头尾"或"纯享版"的span
        if not description:
            desc_match3 = re.search(r'<span[^>]*>([^<]*(?:去头尾|纯享版|纯净版)[^<]*)</span>', context, re.DOTALL)
            if desc_match3:
                description = re.sub(r'<[^>]+>', '', desc_match3.group(1)).strip()
        
        # 提取大小 - 格式: >20.29<br />GB</td> 或 >20.29<br/>GB</td>
        size_match = re.search(r'>([\d.]+)\s*<br\s*/?>\s*(GB|MB|TB)\s*</td>', context)
        size_bytes = 0
        if size_match:
            size_val = float(size_match.group(1))
            unit = size_match.group(2)
            if unit == "GB":
                size_bytes = int(size_val * 1073741824)
            elif unit == "MB":
                size_bytes = int(size_val * 1048576)
            elif unit == "TB":
                size_bytes = int(size_val * 1099511627776)
        
        # 提取做种数 - 格式: <a href="...#seeders"><font color="...">1</font></a> 或 <a href="...#seeders">1</a>
        seed_match = re.search(r'#seeders[^>]*>(?:<font[^>]*>)?(\d+)(?:</font>)?</a>', context)
        seeders = int(seed_match.group(1)) if seed_match else 0
        
        # 提取下载数 - 格式: <a href="...#leechers">394</a>
        grabs_match = re.search(r'#leechers[^>]*>(\d+)</a>', context)
        grabs = int(grabs_match.group(1)) if grabs_match else 0
        
        # 提取发布时间
        pub_match = re.search(r'title="(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})"', context)
        pubdate = pub_match.group(1) if pub_match else ""
        
        # 构建与 MP API 兼容的数据结构
        page_url = f"https://hdsky.me/details.php?id={torrent_id}&hit=1"
        
        # 提取年份
        year_match = re.search(r'\b(19|20)\d{2}\b', title)
        year = year_match.group(0) if year_match else None
        
        # 提取季集信息
        season_match = re.search(r'S\d+(?:E\d+(?:-E?\d+)?)?', title, re.IGNORECASE)
        season_episode = season_match.group(0) if season_match else None
        
        # 提取制作组
        team_match = re.search(r'@(\w+)$', title)
        resource_team = team_match.group(1) if team_match else None
        
        # 提取中文名（优先从描述中，兜底从标题解析）
        cn_match = re.search(r'(?:韩剧|国漫|美剧|日剧|英剧|国产剧|纪录片)[：:]\s*([^/|]+)', description)
        cn_name = cn_match.group(1).strip() if cn_match else None
        # 兜底：从种子标题提取中文名（格式如"中文名.English.Name.S01.2026.xxx"）
        if not cn_name:
            cn_title_match = re.search(r'^([\u4e00-\u9fff][\u4e00-\u9fff\w·\s]{1,20})[\.\s]', title)
            if cn_title_match:
                cn_name = cn_title_match.group(1).strip()
        # 兜底2：英文标题中提取英文名作为name（去掉年份、分辨率等后缀）
        if not cn_name:
            en_title_match = re.match(r'^([A-Z][A-Za-z\s\'\-]+?)(?:\s+S\d+|\s+\d{4}\s)', title)
            if en_title_match:
                cn_name = en_title_match.group(1).strip()
        
        item = {
            "torrent_info": {
                "site": 24,
                "site_name": "天空",
                "title": title,
                "description": description,
                "imdbid": None,
                "enclosure": enclosure,
                "page_url": page_url,
                "size": size_bytes,
                "seeders": seeders,
                "peers": 0,
                "grabs": grabs,
                "pubdate": pubdate,
                "date_elapsed": "",
                "freedate": None,
                "uploadvolumefactor": 1.0,
                "downloadvolumefactor": 1.0,
                "hit_and_run": False,
                "labels": [],
                "pri_order": 0,
                "category": "未知",
                "volume_factor": "普通",
                "freedate_diff": "",
            },
            "meta_info": {
                "cn_name": cn_name,
                "en_name": None,
                "name": cn_name or title,
                "year": year,
                "season_episode": season_episode,
                "resource_team": resource_team,
            }
        }
        results.append(item)
    
    return results

def search_page(page=1):
    """直接抓取天空站点页面获取种子（包括置顶种子）"""
    return fetch_torrents_from_site(page)
