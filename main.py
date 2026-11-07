#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import logging
import time
import argparse
import requests
import gzip
import xml.etree.ElementTree as ET
import re
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
from collector import IPTVSourceCollector
from checker import IPTVSourceChecker

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("iptv_update.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("IPTV-Main")

def load_config():
    """加载配置文件"""
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info(f"成功从 {config_path} 加载配置")
        return config
    except Exception as e:
        logger.error(f"加载配置文件失败: {str(e)}")
        sys.exit(1)

def parse_m3u_file(filepath):
    """解析M3U文件，提取频道信息和URL"""
    logger.info(f"解析M3U文件: {filepath}")
    
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
    except Exception as e:
        logger.error(f"读取文件失败: {filepath}, 错误: {str(e)}")
        return {}
    
    channels = {}
    
    lines = content.strip().split('\n')
    if not lines or not lines[0].startswith('#EXTM3U'):
        logger.warning(f"不是有效的M3U文件: {filepath}")
        return channels
    
    i = 1
    while i < len(lines):
        line = lines[i].strip()
        
        # 处理EXTINF行
        if line.startswith('#EXTINF'):
            extinf_line = line
            info = parse_extinf(extinf_line)
            
            # 获取频道ID
            channel_id = info.get('tvg-id') or info.get('tvg-name') or info.get('title')
            
            if not channel_id:
                i += 1
                continue
            
            # 查找URL行
            url = None
            j = i + 1
            while j < len(lines):
                next_line = lines[j].strip()
                if next_line and not next_line.startswith('#'):
                    url = next_line
                    break
                j += 1
            
            if url:
                # 将频道添加到集合中
                if channel_id in channels:
                    channels[channel_id][1].append(url)
                else:
                    channels[channel_id] = [info, [url]]
                
                i = j + 1
            else:
                i += 1
        else:
            i += 1
    
    logger.info(f"从文件 {filepath} 解析出 {len(channels)} 个频道")
    return channels

def parse_extinf(extinf_line):
    """解析EXTINF行，提取频道信息"""
    info = {}
    
    try:
        # 提取时长和标题
        parts = extinf_line.split(',', 1)
        if len(parts) > 1:
            info['title'] = parts[1].strip()
        
        # 提取属性
        attrs_part = parts[0]
        import re
        pattern = r'(\w+[-\w]*)\s*=\s*"([^"]*)"'
        matches = re.findall(pattern, attrs_part)
        
        for key, value in matches:
            info[key] = value
            
    except Exception as e:
        logger.error(f"解析EXTINF失败: {str(e)}")
        
    return info

def download_and_parse_epg(config):
    """下载并解析EPG数据"""
    if "epg_urls" not in config or not config["epg_urls"]:
        logger.info("未配置EPG URL，跳过EPG处理")
        return {}
        
    logger.info("开始下载和解析EPG数据")
    
    epg_data = {}  # 格式: {频道ID: {"id": id, "name": name, "icon": icon_url}}
    
    for epg_url in config["epg_urls"]:
        logger.info(f"下载EPG: {epg_url}")
        try:
            response = requests.get(epg_url, timeout=120)
            if response.status_code != 200:
                logger.error(f"下载EPG失败，状态码: {response.status_code}")
                continue
                
            # 检查是否为gzip格式
            if epg_url.endswith('.gz'):
                try:
                    content = gzip.decompress(response.content)
                except Exception as e:
                    logger.error(f"解压EPG数据失败: {str(e)}")
                    continue
            else:
                content = response.content
                
            # 解析XML
            try:
                root = ET.fromstring(content)
                
                # 查找频道信息
                for channel in root.findall(".//channel"):
                    channel_id = channel.get('id')
                    if not channel_id:
                        continue
                        
                    # 获取频道名称
                    display_name = channel.find('.//display-name')
                    name = display_name.text if display_name is not None else ""
                    
                    # 获取频道图标
                    icon = channel.find('.//icon')
                    icon_url = icon.get('src') if icon is not None else ""
                    
                    # 存储频道信息
                    if channel_id not in epg_data:
                        epg_data[channel_id] = {
                            "id": channel_id,
                            "name": name,
                            "icon": icon_url
                        }
                    elif not epg_data[channel_id]["icon"] and icon_url:
                        # 如果当前EPG数据没有图标但新数据有，则更新
                        epg_data[channel_id]["icon"] = icon_url
                        
                logger.info(f"从 {epg_url} 解析出 {len(root.findall('.//channel'))} 个频道信息")
                    
            except ET.ParseError as e:
                logger.error(f"解析EPG XML数据失败: {str(e)}")
                continue
                
        except Exception as e:
            logger.error(f"处理EPG出错: {str(e)}")
            continue
    
    logger.info(f"EPG数据解析完成，共收集 {len(epg_data)} 个频道信息")
    return epg_data

def match_channels_with_epg(sources_data, epg_data, config):
    """将频道与EPG数据匹配"""
    if not epg_data:
        return sources_data
        
    logger.info("开始匹配频道与EPG数据")
    
    # 简化频道名称的函数，用于匹配
    def simplify_name(name):
        if not name:
            return ""
        # 移除空格和特殊字符
        simplified = re.sub(r'[^\w\u4e00-\u9fff]', '', name.lower())
        # 常见频道名称替换
        replacements = {
            'cctv': 'cctv',
            'central': 'cctv',
            'china': 'cctv',
            'hong': 'hk',
            'tai': 'tw',
            'television': 'tv',
            'channel': ''
        }
        for old, new in replacements.items():
            simplified = simplified.replace(old, new)
        return simplified
    
    # 创建EPG索引，用于快速查找
    epg_index = {}
    for epg_id, data in epg_data.items():
        simple_name = simplify_name(data["name"])
        if simple_name:
            epg_index[simple_name] = epg_id
    
    # 匹配频道
    matched_count = 0
    for channel_id, data in sources_data.items():
        info = data["info"]
        title = info.get('title', '')
        
        # 规范化频道名称
        normalized_title = normalize_channel_name(title, config)
        if normalized_title != title:
            info['title'] = normalized_title
            
        # 尝试直接匹配EPG ID
        if channel_id in epg_data:
            # 更新tvg-id和tvg-logo
            info['tvg-id'] = epg_data[channel_id]["id"]
            if not info.get('tvg-logo') and epg_data[channel_id]["icon"]:
                info['tvg-logo'] = epg_data[channel_id]["icon"]
            matched_count += 1
            continue
            
        # 尝试通过简化名称匹配
        simple_title = simplify_name(normalized_title)
        if simple_title in epg_index:
            epg_id = epg_index[simple_title]
            # 更新tvg-id和tvg-logo
            info['tvg-id'] = epg_data[epg_id]["id"]
            if not info.get('tvg-logo') and epg_data[epg_id]["icon"]:
                info['tvg-logo'] = epg_data[epg_id]["icon"]
            matched_count += 1
            continue
            
        # 尝试模糊匹配
        for epg_name, epg_id in epg_index.items():
            if (epg_name in simple_title) or (simple_title in epg_name and len(simple_title) > 3):
                # 更新tvg-id和tvg-logo
                info['tvg-id'] = epg_data[epg_id]["id"]
                if not info.get('tvg-logo') and epg_data[epg_id]["icon"]:
                    info['tvg-logo'] = epg_data[epg_id]["icon"]
                matched_count += 1
                break
    
    logger.info(f"频道与EPG匹配完成，成功匹配 {matched_count} 个频道")
    return sources_data

def normalize_channel_name(name, config):
    """规范化频道名称"""
    if not name:
        return name
        
    name_lower = name.lower()
    
    # 尝试匹配映射表
    if "channel_name_map" in config:
        for pattern, normalized_name in config["channel_name_map"].items():
            if re.search(pattern, name_lower):
                return normalized_name
                
    return name

def should_exclude_channel(info, url, config):
    """检查是否应该排除某个频道或源"""
    # 检查URL是否包含被排除的源
    if "excluded_sources" in config:
        for excluded_source in config["excluded_sources"]:
            if excluded_source in url:
                return True
    
    # 检查频道ID是否为数字
    # 有些源使用纯数字作为频道ID，可能会导致乱码或其他问题
    tvg_id = info.get('tvg-id', '')
    if tvg_id and tvg_id.isdigit() and len(tvg_id) < 5:  # 排除类似"4"这样的频道ID
        return True
        
    # 检查组标题是否包含乱码
    group_title = info.get('group-title', '')
    if any(char in group_title for char in ['å', 'é¢', 'è§', 'é', '¢', '§', 'è', 'æ', 'ç', '¾', 'â']):
        return True
    
    return False

def organize_channels(sources_data, config):
    """整理频道，去除重复，为每个频道保留最多两个源"""
    logger.info("开始整理频道...")
    
    # 按频道名称分组
    channels_by_name = {}
    
    # 整理频道
    for channel_id, data in sources_data.items():
        info = data["info"]
        title = info.get('title', '')
        
        # 跳过没有标题的频道
        if not title:
            continue
            
        # 收集有效源，并排除不需要的源
        valid_sources = []
        for source in data["sources"]:
            if source["valid"] and not should_exclude_channel(info, source["url"], config):
                valid_sources.append((source["url"], source["latency"]))
        
        # 如果没有有效源，跳过此频道
        if not valid_sources:
            continue
            
        # 按延迟排序
        valid_sources.sort(key=lambda x: x[1])
        
        # 保留最多两个源（速度最快和第二快的）
        best_sources = valid_sources[:min(2, len(valid_sources))]
        
        # 将频道添加到按名称分组的集合中
        if title in channels_by_name:
            existing_sources = channels_by_name[title]["sources"]
            existing_latency = channels_by_name[title]["latency"]
            
            # 如果现有的延迟更高（更慢），则替换为新的源
            if best_sources[0][1] < existing_latency:
                channels_by_name[title] = {
                    "info": info,
                    "sources": [source[0] for source in best_sources],
                    "latency": best_sources[0][1]
                }
        else:
            channels_by_name[title] = {
                "info": info,
                "sources": [source[0] for source in best_sources],
                "latency": best_sources[0][1]
            }
    
    logger.info(f"频道整理完成，共 {len(channels_by_name)} 个唯一频道")
    return channels_by_name

# 省份关键词映射表
PROVINCE_KEYWORDS = {
    "北京":  ["北京"],
    "上海":  ["上海"],
    "天津":  ["天津"],
    "重庆":  ["重庆", "渝"],
    "广东":  ["广东", "广州", "深圳", "东莞", "佛山", "珠海", "汕头", "湛江", "中山", "惠州",
              "江门", "肇庆", "茂名", "韶关", "梅州", "潮州", "揭阳", "清远", "阳江", "云浮",
              "河源", "汕尾"],
    "浙江":  ["浙江", "杭州", "宁波", "温州", "嘉兴", "湖州", "绍兴", "金华", "衢州",
              "舟山", "台州", "丽水", "余姚", "慈溪", "义乌", "诸暨", "瑞安", "乐清",
              "缙云", "新昌", "兰溪", "象山", "武义", "永嘉", "苍南", "平湖", "海宁",
              "上虞", "萧山", "余杭", "衢江", "开化", "云和", "庆元", "龙泉", "龙游",
              "普陀", "武义", "兰溪"],
    "江苏":  ["江苏", "南京", "苏州", "无锡", "常州", "南通", "镇江", "扬州", "淮安",
              "连云港", "盐城", "宿迁", "泰州", "徐州", "新沂", "武进", "溧水", "靖江",
              "泰州"],
    "湖南":  ["湖南", "长沙", "株洲", "湘潭", "衡阳", "邵阳", "岳阳", "常德", "张家界",
              "益阳", "郴州", "永州", "怀化", "娄底", "湘西"],
    "湖北":  ["湖北", "武汉", "黄石", "十堰", "荆州", "荆门", "宜昌", "鄂州", "孝感",
              "黄冈", "咸宁", "随州", "恩施", "仙桃", "潜江", "天门", "神农架",
              "江夏", "荆门"],
    "四川":  ["四川", "成都", "绵阳", "德阳", "广元", "遂宁", "内江", "乐山", "南充",
              "眉山", "宜宾", "广安", "达州", "雅安", "巴中", "资阳", "阿坝", "甘孜",
              "凉山", "叙州", "蓬安", "旺苍", "南部", "松潘", "黑水", "汶川", "泸州",
              "广安", "雅安"],
    "山东":  ["山东", "济南", "青岛", "淄博", "枣庄", "东营", "烟台", "潍坊", "济宁",
              "泰安", "威海", "日照", "莱芜", "临沂", "德州", "聊城", "滨州", "菏泽"],
    "河南":  ["河南", "郑州", "开封", "洛阳", "平顶山", "安阳", "鹤壁", "新乡", "焦作",
              "濮阳", "许昌", "漯河", "三门峡", "南阳", "商丘", "信阳", "周口", "驻马店",
              "沁阳", "泌阳", "郸城"],
    "河北":  ["河北", "石家庄", "唐山", "秦皇岛", "邯郸", "邢台", "保定", "张家口",
              "承德", "沧州", "廊坊", "衡水", "双滦", "涉县", "涞水", "清苑", "青县",
              "滦平"],
    "山西":  ["山西", "太原", "大同", "朔州", "忻州", "阳泉", "长治", "晋城", "晋中",
              "运城", "临汾", "吕梁", "太谷", "武乡"],
    "陕西":  ["陕西", "西安", "铜川", "宝鸡", "咸阳", "渭南", "延安", "汉中", "榆林",
              "安康", "商洛"],
    "辽宁":  ["辽宁", "沈阳", "大连", "鞍山", "抚顺", "本溪", "丹东", "锦州", "营口",
              "阜新", "辽阳", "盘锦", "铁岭", "朝阳", "葫芦岛"],
    "吉林":  ["吉林", "长春", "四平", "辽源", "通化", "松原", "白城", "延边", "白山",
              "农安", "舒兰", "辉南", "珲春"],
    "黑龙江": ["黑龙江", "哈尔滨", "齐齐哈尔", "牡丹江", "佳木斯", "大庆", "鸡西",
               "鹤岗", "双鸭山", "伊春", "七台河", "黑河", "绥化", "大兴安岭"],
    "安徽":  ["安徽", "合肥", "芜湖", "蚌埠", "淮南", "马鞍山", "淮北", "铜陵", "安庆",
              "黄山", "滁州", "阜阳", "宿州", "巢湖", "六安", "亳州", "池州", "宣城",
              "固镇"],
    "福建":  ["福建", "福州", "厦门", "莆田", "三明", "泉州", "漳州", "南平", "龙岩",
              "宁德"],
    "江西":  ["江西", "南昌", "景德镇", "萍乡", "九江", "新余", "鹰潭", "赣州", "吉安",
              "宜春", "抚州", "上饶"],
    "广西":  ["广西", "南宁", "柳州", "桂林", "梧州", "北海", "防城港", "钦州", "贵港",
              "玉林", "百色", "贺州", "河池", "来宾", "崇左", "灌阳"],
    "云南":  ["云南", "昆明", "曲靖", "玉溪", "保山", "昭通", "丽江", "普洱", "临沧",
              "楚雄", "红河", "文山", "西双版纳", "大理", "德宏", "怒江", "迪庆"],
    "贵州":  ["贵州", "贵阳", "六盘水", "遵义", "安顺", "毕节", "铜仁", "黔西南",
              "黔东南", "黔南"],
    "甘肃":  ["甘肃", "兰州", "嘉峪关", "金昌", "白银", "天水", "武威", "张掖", "平凉",
              "酒泉", "庆阳", "定西", "陇南", "临夏", "甘南"],
    "新疆":  ["新疆", "乌鲁木齐", "克拉玛依", "吐鲁番", "哈密", "和田", "阿克苏",
              "喀什", "伊犁", "塔城", "阿勒泰", "博尔塔拉", "昌吉", "巴音郭楞",
              "克孜勒苏", "兵团"],
    "西藏":  ["西藏", "拉萨", "昌都", "山南", "日喀则", "那曲", "阿里", "林芝"],
    "青海":  ["青海", "西宁", "海东", "海北", "黄南", "海南", "果洛", "玉树", "海西"],
    "宁夏":  ["宁夏", "银川", "石嘴山", "吴忠", "固原", "中卫"],
    "内蒙古": ["内蒙古", "呼和浩特", "包头", "乌海", "赤峰", "通辽", "鄂尔多斯",
               "呼伦贝尔", "巴彦淖尔", "乌兰察布", "兴安", "锡林郭勒", "阿拉善"],
    "海南":  ["海南", "海口", "三亚", "三沙", "儋州"],
}


def classify_channel(name, group):
    """
    根据频道名和原始 group-title 判断新分类
    返回新的 group-title 字符串
    """
    n = name.lower()
    g = group.lower() if group else ""

    # ── 中国大陆 · 央视 ────────────────────────────────────────────────────
    is_cctv = re.search(r'cctv|央视|中央电视|cgtn', n) or \
              group in ['央视', '央视频道', '📺央视频道']
    if is_cctv:
        if re.search(r'体育|sport|golf|tennis|football|billiard', n):
            return '中国大陆 · 体育'
        if re.search(r'纪录|documentary|地理|nature', n):
            return '中国大陆 · 纪录'
        if re.search(r'新闻|news', n):
            return '中国大陆 · 新闻'
        if re.search(r'电影|movie|film|剧场', n):
            return '中国大陆 · 影视'
        if re.search(r'少儿|儿童|动画|kids', n):
            return '中国大陆 · 儿童'
        return '中国大陆 · 央视'

    # ── 中国大陆 · 卫视 ────────────────────────────────────────────────────
    weishi_names = ['湖南卫视','浙江卫视','江苏卫视','东方卫视','北京卫视','深圳卫视',
                    '广东卫视','安徽卫视','山东卫视','四川卫视','重庆卫视','辽宁卫视',
                    '黑龙江卫视','吉林卫视','河南卫视','湖北卫视','河北卫视','山西卫视',
                    '贵州卫视','云南卫视','广西卫视','新疆卫视','西藏卫视','宁夏卫视',
                    '内蒙古卫视','青海卫视','甘肃卫视','陕西卫视','江西卫视','福建卫视',
                    '海南卫视','天津卫视','上海卫视','大湾区卫视','兵团卫视',
                    '星空卫视','凤凰卫视','香港卫视','人间卫视','安多卫视']
    if any(w in name for w in weishi_names) or group in ['卫视频道', '📡卫视频道']:
        if re.search(r'新闻|news', n):
            return '中国大陆 · 新闻'
        if re.search(r'体育|sport', n):
            return '中国大陆 · 体育'
        return '中国大陆 · 卫视'

    # ── 香港 ───────────────────────────────────────────────────────────────
    hk_keywords = ['翡翠台','明珠台','tvb','凤凰中文','凤凰资讯','凤凰卫视',
                   'now tv','viutv','有线新闻','有线财经','有线娱乐','大湾区']
    if any(k.lower() in n for k in hk_keywords):
        if re.search(r'新闻|news|资讯', n): return '香港 · 新闻'
        if re.search(r'体育|sport', n):     return '香港 · 体育'
        if re.search(r'纪录|documentary', n): return '香港 · 纪录'
        if re.search(r'影视|电影|剧', n):   return '香港 · 影视'
        return '香港 · 综合'

    # ── 台湾 ───────────────────────────────────────────────────────────────
    tw_keywords = ['台视','民视','三立','tvbs','中天','东森','华视','中视','公视',
                   '大爱','年代','纬来','八大','客家','原住民','大立电视','唯心']
    if any(k.lower() in n for k in tw_keywords):
        if re.search(r'新闻|news', n):      return '台湾 · 新闻'
        if re.search(r'体育|sport', n):     return '台湾 · 体育'
        if re.search(r'纪录|documentary', n): return '台湾 · 纪录'
        if re.search(r'影视|电影|剧|戏剧', n): return '台湾 · 影视'
        return '台湾 · 综合'

    # ── 中国大陆地方台（按省份）──────────────────────────────────────────
    cn_local_groups = ['地方','地方频道','直播中国',
                       '☘️吉林频道','☘️四川频道','☘️安徽频道','☘️山东频道',
                       '☘️山西频道','☘️广东频道','☘️广西频道','☘️江苏频道',
                       '☘️河北频道','☘️河南频道','☘️浙江频道','☘️海南频道',
                       '☘️湖北频道','☘️福建频道','☘️辽宁频道','☘️陕西频道',
                       '☘️青海频道','☘️黑龙江频道',
                       '吉林','四川','安徽','山西','内蒙古','新疆','甘肃',
                       '西藏','重庆','江苏','江西','河北','浙江','湖北']
    
    # 先判断是不是地方台（group 在列表里，或者频道名包含中文地名）
    is_cn_local = group in cn_local_groups
    if not is_cn_local:
        # 通过省份关键词判断
        for province, keywords in PROVINCE_KEYWORDS.items():
            if any(kw in name for kw in keywords):
                is_cn_local = True
                break

    if is_cn_local:
        # 先判断内容类型
        content_type = None
        if re.search(r'体育|sport|足球|篮球|劲爆', n):
            content_type = '体育'
        elif re.search(r'新闻|资讯|法治', n):
            content_type = '新闻'
        elif re.search(r'纪录|科教|人文|纪实', n):
            content_type = '纪录'
        elif re.search(r'影视|电影|电视剧|剧场', n):
            content_type = '影视'
        elif re.search(r'少儿|儿童|动画|动漫', n):
            content_type = '儿童'

        # 如果是央视/卫视体育或新闻，已在前面处理，这里不会到
        if content_type in ('体育',):
            return '中国大陆 · 体育'
        if content_type in ('新闻',):
            return '中国大陆 · 新闻'
        if content_type in ('纪录',):
            return '中国大陆 · 纪录'
        if content_type in ('影视',):
            return '中国大陆 · 影视'
        if content_type in ('儿童',):
            return '中国大陆 · 儿童'

        # 判断省份
        for province, keywords in PROVINCE_KEYWORDS.items():
            if any(kw in name for kw in keywords):
                return province
        # group 名称直接是省份
        for province in PROVINCE_KEYWORDS:
            if province in group:
                return province
        return '中国大陆 · 地方频道'

    # ── 美国 ───────────────────────────────────────────────────────────────
    us_news = ['abc news','cbs news','nbc news','fox news','cnn','msnbc',
               'newsmax','newsnation','one america','c-span','pbs newshour']
    us_sports = ['espn','nfl','nba tv','mlb network','nhl network',
                 'fox sports','nbc sports','cbs sports','golf channel',
                 'tennis channel','fubo sports']
    if any(k in n for k in us_news) or \
       re.search(r'(abc|cbs|nbc|fox)\s+news', n):
        return '美国 · 新闻'
    if any(k in n for k in us_sports):
        return '美国 · 体育'
    if re.search(r'\b(abc|cbs|nbc|fox|pbs)\s+\d', name) or \
       re.search(r'\b(WKRN|WSMV|WNBC|KABC|KTTV|WLS|WXYZ|KHOU|KPRC|KSAT|WFAA)\b', name):
        return '美国 · 综合'

    # ── 英国 ───────────────────────────────────────────────────────────────
    uk_keywords = ['bbc','itv','channel 4','channel 5','sky news',
                   'gb news','talk tv','times radio']
    if any(k in n for k in uk_keywords):
        if re.search(r'news|新闻', n):      return '英国 · 新闻'
        if re.search(r'sport|体育', n):     return '英国 · 体育'
        if re.search(r'drama|film|comedy', n): return '英国 · 影视'
        return '英国 · 综合'

    # ── 加拿大 ─────────────────────────────────────────────────────────────
    if re.search(r'\b(cbc|ctv|global tv|tsn|sportsnet|cpac)\b', n):
        if re.search(r'news|新闻', n): return '加拿大 · 新闻'
        return '加拿大 · 综合'

    # ── 澳大利亚 ───────────────────────────────────────────────────────────
    if re.search(r'\b(abc.*au|sbs|nine|seven|ten|foxtel)\b', n) or \
       re.search(r'australia', n):
        return '澳大利亚 · 综合'

    # ── 日本 ───────────────────────────────────────────────────────────────
    if group in ['•日本'] or re.search(r'nhk|fuji tv|tbs japan|tv tokyo', n):
        if re.search(r'news|新闻|world', n): return '日本 · 新闻'
        return '日本 · 综合'

    # ── 韩国 ───────────────────────────────────────────────────────────────
    if group in ['•韩国'] or re.search(r'kbs|mbc|sbs|jtbc|arirang', n):
        if re.search(r'news|新闻', n): return '韩国 · 新闻'
        return '韩国 · 综合'

    # ── 俄罗斯 ─────────────────────────────────────────────────────────────
    if re.search(r'rt news|russia today|первый|нтв|звезда', n):
        if re.search(r'news|новости', n): return '俄罗斯 · 新闻'
        return '俄罗斯 · 综合'

    # ── 法国/德国/土耳其 ───────────────────────────────────────────────────
    if re.search(r'france 24|france info|bfm|lci', n): return '法国 · 新闻'
    if re.search(r'\bdw\b|deutsche welle', n):          return '德国 · 新闻'
    if re.search(r'\btrt\b', n):                        return '土耳其 · 新闻'

    # ── 中东 ───────────────────────────────────────────────────────────────
    if re.search(r'al jazeera|al arabiya|al mayadeen|sky news arabia|'
                 r'al iraqia|asharq|al manar', n):
        return '中东 · 新闻'

    # ── 音乐 MV ────────────────────────────────────────────────────────────
    if group in ['•MTV', 'Music', '音乐频道', '•音乐'] or \
       re.search(r'mtv|music|音乐|mv', n):
        return '音乐 · MV'

    # ── 全球内容分类 ───────────────────────────────────────────────────────
    if re.search(r'news|新闻|noticias|nachrichten|actualit', n) or \
       'news' in g:
        return '国际 · 新闻'
    if re.search(r'sport|体育|deporte|calcio|football|soccer|tennis|golf', n) or \
       'sport' in g:
        return '国际 · 体育'
    if re.search(r'documentary|纪录|national geographic|discovery|history', n) or \
       'documentary' in g:
        return '国际 · 纪录'
    if re.search(r'kids|children|少儿|儿童|cartoon|animation|junior', n) or \
       'kids' in g or 'animation' in g:
        return '国际 · 儿童'

    # ── 宗教 ──────────────────────────────────────────────────────────────
    if re.search(r'church|bible|god|jesus|christ|muslim|prayer|worship|宗教|佛教', n) or \
       'religious' in g:
        return '宗教'

    # ── 购物 ──────────────────────────────────────────────────────────────
    if 'shop' in g or re.search(r'\bshop\b|shopping|teleshopping', n):
        return '购物'

    # ── 议会与立法 ────────────────────────────────────────────────────────
    if 'legislative' in g:
        return '议会与立法'
        
    return '国际 · 综合'

def sort_channels_by_category(channels, config):
    """先分类再排序"""
    category_order = {cat: idx for idx, cat in enumerate(config.get("categories", []))}
    default_order = len(category_order)

    # 先给每个频道设置正确的 group-title
    filtered = {}
    for channel_name, data in channels.items():
        info = data["info"]
        name = info.get('title', channel_name)
        old_group = info.get('group-title', '')
        
        new_group = classify_channel(name, old_group)
        
        if new_group is None:   # 被过滤掉
            continue
        
        info['group-title'] = new_group
        filtered[channel_name] = data

    # 再排序
    def get_order(item):
        _, data = item
        group = data["info"].get("group-title", "其他")
        return category_order.get(group, default_order)

    return sorted(filtered.items(), key=get_order)

def generate_m3u(sorted_channels, output_path):
    """生成M3U文件，包含主源和备用源"""
    logger.info(f"开始生成M3U文件: {output_path}")
    
    with open(output_path, 'w', encoding='utf-8') as f:
        # 写入头部
        f.write("#EXTM3U\n")
        
        # 写入频道信息
        for channel_name, data in sorted_channels:
            info = data["info"]
            sources = data["sources"]
            
            # 构建EXTINF行
            extinf = build_extinf(info)
            f.write(f"{extinf}\n")
            
            # 写入主源
            f.write(f"{sources[0]}\n")
            
            # 如果有备用源，添加备用源标记和URL
            if len(sources) > 1:
                f.write(f"#EXTBURL:{sources[1]}\n")
    
    logger.info(f"M3U文件生成完成: {output_path}, 共 {len(sorted_channels)} 个频道")
    return output_path

def normalize_referrer(value):
    """Add a missing scheme to http-referrer, e.g. salamtelevisi.com -> https://salamtelevisi.com/"""
    value = (value or '').strip()
    if not value:
        return value
    if value.startswith('//'):
        value = 'https:' + value
    elif not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', value):
        value = 'https://' + value
    # Bare host with no path: add a trailing slash
    if re.match(r'^https?://[^/?#]+$', value):
        value += '/'
    return value

def build_extinf(info):
    """构建EXTINF行"""
    attrs = []
    
    for key, value in info.items():
        if key != 'title':
            if key == 'http-referrer':
                value = normalize_referrer(value)
            attrs.append(f'{key}="{value}"')
    
    attrs_str = ' '.join(attrs)
    title = info.get('title', '')
    
    return f"#EXTINF:-1 {attrs_str},{title}"

def main():
    """主函数"""
    parser = argparse.ArgumentParser(description='IPTV直播源收集、检测与整理工具')
    parser.add_argument('--no-check', action='store_true', help='跳过直播源检测步骤')
    parser.add_argument('--no-epg', action='store_true', help='跳过EPG处理')
    parser.add_argument('--max-channels', type=int, default=0, help='最大处理频道数量(用于测试)')
    args = parser.parse_args()
    
    start_time = time.time()
    logger.info("开始IPTV直播源处理流程")
    
    try:
        # 加载配置
        config = load_config()
        logger.info(f"配置加载完成，共 {len(config['sources'])} 个直播源")
        
        # 创建输出目录
        output_dir = os.path.join(os.path.dirname(__file__), config["output_dir"])
        os.makedirs(output_dir, exist_ok=True)
        
        # 收集直播源
        collector = IPTVSourceCollector(config)
        source_files = collector.collect()
        logger.info(f"直播源收集完成，共 {len(source_files)} 个文件")
        
        # 解析所有源文件
        all_channels = {}
        for filepath in source_files:
            channels = parse_m3u_file(filepath)
            
            # 合并到全局频道集合
            for channel_id, (info, urls) in channels.items():
                if channel_id in all_channels:
                    all_channels[channel_id][1].extend(urls)
                else:
                    all_channels[channel_id] = [info, urls]
                    
            # 如果设置了最大频道数限制，用于测试
            if args.max_channels > 0 and len(all_channels) >= args.max_channels:
                logger.info(f"达到最大频道数限制 ({args.max_channels})，停止收集")
                break
        
        # 去重URL
        for channel_id, (info, urls) in all_channels.items():
            all_channels[channel_id][1] = list(set(urls))
        
        logger.info(f"共解析出 {len(all_channels)} 个频道, {sum(len(urls) for _, urls in all_channels.values())} 个直播源")
        
        # 检查直播源
        if not args.no_check:
            checker = IPTVSourceChecker(config)
            check_results = checker.check(all_channels)
            
            # 保存结果为JSON文件（仅用于调试或API访问）
            json_output_path = os.path.join(output_dir, "collected_sources.json")
            
            # 转换结果为可序列化的格式
            serializable_results = {}
            for channel_id, result in check_results.items():
                serializable_results[channel_id] = {
                    "info": result["info"],
                    "sources": [
                        {"url": url, "valid": valid, "latency": latency if latency != float('inf') else -1}
                        for url, valid, latency in result["sources"]
                    ]
                }
            
            with open(json_output_path, 'w', encoding='utf-8') as f:
                json.dump(serializable_results, f, ensure_ascii=False, indent=2)
                
            logger.info(f"JSON格式检测结果已保存到: {json_output_path}")
            
            # 下载和解析EPG数据
            epg_data = {}
            if not args.no_epg:
                epg_data = download_and_parse_epg(config)
                
                # 匹配频道与EPG
                serializable_results = match_channels_with_epg(serializable_results, epg_data, config)
            
            # 整理频道
            channels_by_name = organize_channels(serializable_results, config)
            
            # 按分类排序频道
            sorted_channels = sort_channels_by_category(channels_by_name, config)
            
            # 生成最终M3U文件
            output_file = config.get("output_file", "iptv_collection.m3u")
            output_path = os.path.join(output_dir, output_file)
            generate_m3u(sorted_channels, output_path)
        else:
            logger.info("跳过直播源检测步骤")
        
        end_time = time.time()
        logger.info(f"IPTV直播源处理完成，总耗时: {end_time - start_time:.2f}秒")
        
    except Exception as e:
        logger.error(f"程序运行出错: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
