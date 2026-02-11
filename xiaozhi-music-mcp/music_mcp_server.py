#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
免费音乐MCP服务器（基于 FastMCP 标准协议）
适用于小智AI音响的音乐搜索服务
搜索音乐
"""

import sys
import logging
import os
import hmac
import hashlib
import json
import re
from urllib.parse import urljoin, urlparse

import httpx
from fastmcp import FastMCP, Context

# 修复 Windows 控制台编码
if sys.platform == "win32":
    sys.stderr.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")

logger = logging.getLogger("MusicMCP")
logging.basicConfig(level=logging.INFO)

# Music API 地址，可通过环境变量覆盖，便于切换镜像或自建网关。
MUSIC_API_BASE_URL = os.getenv("MUSIC_API_BASE_URL", "https://api.i-meto.com/meting/api")
# 签名密钥。
MUSIC_API_TOKEN = os.getenv("MUSIC_API_TOKEN", "").strip()
# 签名参数名，默认 auth；服务端通常兼容 token。
MUSIC_SIGN_PARAM = os.getenv("MUSIC_SIGN_PARAM", "auth").strip() or "auth"
# 只有这些 type 需要签名。
MUSIC_SIGN_REQUIRED_TYPES = {"url", "lrc", "pic"}

# 创建标准 MCP 服务器
mcp = FastMCP("music-mcp-server")

# 当前播放状态（内存中维护）
playback_state = {
    "current_song": None,
    "playlist": [],
    "is_playing": False,
    "volume": 50,
}

# 搜索结果缓存：song_id -> 搜索结果 dict（包含带签名的 lrc URL 等）
# 解决大模型不传 lrc 参数时，resolve_music_url 也能自动拿到歌词链接
_search_result_cache: dict[str, dict] = {}


def _normalize_music_url(url: str) -> str:
    """把相对地址补全为完整 URL，避免请求时报协议缺失错误。"""
    normalized = (url or "").strip()
    if not normalized:
        return ""

    parsed = urlparse(normalized)
    if parsed.scheme in ("http", "https"):
        return normalized

    # 兼容 //host/path 这种协议相对地址，默认按 HTTPS 处理。
    if normalized.startswith("//"):
        return f"https:{normalized}"

    # 兼容 /api?... 或 api?... 这种相对路径，基于 MUSIC_API_BASE_URL 补全。
    return urljoin(MUSIC_API_BASE_URL, normalized)


def _build_music_params(server: str, req_type: str, req_id: str) -> dict[str, str]:
    """构造 Music API 请求参数，按 type 规则在必要时追加签名。"""
    req_id_str = str(req_id)
    params: dict[str, str] = {"server": server, "type": req_type, "id": req_id_str}

    if req_type in MUSIC_SIGN_REQUIRED_TYPES:
        if not MUSIC_API_TOKEN:
            raise RuntimeError("缺少 MUSIC_API_TOKEN，无法为受保护接口生成签名")
        payload = f"{server}{req_type}{req_id_str}".encode("utf-8")
        signature = hmac.new(MUSIC_API_TOKEN.encode("utf-8"), payload, hashlib.sha1).hexdigest()
        params[MUSIC_SIGN_PARAM] = signature

    return params


async def _search_api(query: str, limit: int = 10) -> list[dict]:
    """调用 Music API 搜索歌曲（网易云源）"""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            params = _build_music_params("netease", "search", query)
            resp = await client.get(
                MUSIC_API_BASE_URL,
                params=params,
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.error("API 请求失败: %d", resp.status_code)
                return []

            data = resp.json()
            results = []
            for item in data[:limit]:
                raw_url = str(item.get("url", "")).strip()
                normalized_url = _normalize_music_url(raw_url)
                raw_lrc = str(item.get("lrc", "")).strip()
                # 把 lrc 相对路径补全为完整 URL
                normalized_lrc = _normalize_music_url(raw_lrc) if raw_lrc else ""
                song_entry = {
                    "id": item.get("id") or normalized_url,
                    "name": item.get("title", "未知歌曲"),
                    "artist": item.get("author", "未知歌手"),
                    "url": normalized_url,
                    "pic": item.get("pic", ""),
                    "lrc": normalized_lrc,
                }
                results.append(song_entry)
                # 缓存搜索结果，后续 resolve_music_url 可以自动取到 lrc
                song_id_str = str(song_entry["id"])
                if song_id_str:
                    _search_result_cache[song_id_str] = song_entry
            return results
    except Exception as e:
        logger.error("搜索音乐出错: %s", e)
        return []


async def _fetch_song_url(song_id: str) -> str:
    """根据歌曲ID查询播放URL（Music API 接口）。

    接口可能有两种返回方式：
    1. 302 重定向到 MP3 直链 —— 此时直接取重定向后的 URL
    2. 200 返回 JSON（含 url 字段）—— 解析 JSON 提取
    """
    try:
        # 不自动跟随重定向，手动处理 302
        async with httpx.AsyncClient(follow_redirects=False) as client:
            params = _build_music_params("netease", "url", song_id)
            resp = await client.get(
                MUSIC_API_BASE_URL,
                params=params,
                timeout=10.0,
            )

            # 302/301 重定向：Location 头就是播放直链，不需要下载内容
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location", "")
                if location:
                    logger.info("通过重定向获取播放URL: %s", location)
                    return location.strip()
                logger.error("收到重定向但缺少 Location 头")
                return ""

            if resp.status_code != 200:
                logger.error("查询歌曲URL失败: %d", resp.status_code)
                return ""

            # 200 响应：Music API 可能返回字符串或数组，这里统一兼容。
            data = resp.json()
            if isinstance(data, str):
                return data.strip()
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, str):
                    return first.strip()
                if isinstance(first, dict):
                    return str(first.get("url", "")).strip()
            return ""
    except Exception as e:
        logger.error("根据ID查询歌曲URL出错: %s", e)
        return ""


async def _fetch_song_lyric(song_id: str) -> str:
    """根据歌曲ID查询歌词文本（LRC）。"""
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            params = _build_music_params("netease", "lrc", song_id)
            resp = await client.get(
                MUSIC_API_BASE_URL,
                params=params,
                timeout=10.0,
            )
            if resp.status_code != 200:
                logger.warning("查询歌词失败: %d", resp.status_code)
                return ""

            raw_text = (resp.text or "").strip()
            if not raw_text:
                return ""

            # 尝试按 JSON 解析；如果失败则当作纯 LRC 文本返回。
            # LRC 歌词格式如 "[00:01.00]歌词" 以 [ 开头，不是合法 JSON。
            try:
                data = json.loads(raw_text)
            except (json.JSONDecodeError, ValueError):
                # 不是 JSON，当作纯 LRC 文本直接返回
                return raw_text

            if isinstance(data, str):
                return data.strip()
            if isinstance(data, list) and data:
                first = data[0]
                if isinstance(first, str):
                    return first.strip()
                if isinstance(first, dict):
                    # 不同接口可能字段名不同，做容错兼容。
                    return str(first.get("lyric") or first.get("lrc") or "").strip()
            if isinstance(data, dict):
                return str(data.get("lyric") or data.get("lrc") or "").strip()
            return raw_text
    except Exception as e:
        logger.warning("根据ID查询歌词出错: %s", e)
        return ""


async def _fetch_lyric_by_url(lrc_url: str) -> str:
    """通过歌词链接直接获取歌词文本。"""
    if not lrc_url:
        return ""
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(lrc_url, timeout=10.0)
            if resp.status_code != 200:
                logger.warning("通过歌词链接获取失败: %d", resp.status_code)
                return ""
            return (resp.text or "").strip()
    except Exception as e:
        logger.warning("通过歌词链接获取出错: %s", e)
        return ""


async def _resolve_final_url(url: str) -> str:
    """解析重定向，返回最终可播放直链。

    优先使用 HEAD 请求，避免用 GET 提前下载整个文件。
    很多 CDN 的播放链接有访问次数或流量限制，
    GET 会"消耗"一次完整下载配额，导致 ESP32 后续只能读到部分数据。
    """
    normalized_url = _normalize_music_url(url)
    if not normalized_url:
        return ""

    # 优先 HEAD：只获取响应头和重定向后的最终 URL，不下载文件内容。
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.head(normalized_url, timeout=15.0)
            if resp.status_code in (200, 206):
                return str(resp.url)
    except Exception as e:
        logger.warning("HEAD解析重定向失败: %s", e)

    # 退化策略：某些 CDN 不支持 HEAD，用 GET + stream 模式，
    # 只读取响应头就关闭连接，不下载文件体。
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            async with client.stream("GET", normalized_url, timeout=15.0) as resp:
                if resp.status_code == 200:
                    return str(resp.url)
    except Exception as e:
        logger.warning("GET(stream)解析重定向失败: %s", e)

    return normalized_url


def _build_lyric_url(song_id: str | None, lrc: str = "") -> str:
    """构造歌词 URL，优先使用搜索结果中已有的 lrc 链接，否则根据 song_id 生成。"""
    if lrc and lrc.strip():
        return lrc.strip()
    if song_id:
        try:
            params = _build_music_params("netease", "lrc", song_id)
            # 手动拼接 URL，供 ESP32 设备端直接 HTTP GET 拉取歌词
            from urllib.parse import urlencode
            return f"{MUSIC_API_BASE_URL}?{urlencode(params)}"
        except RuntimeError:
            # 缺少签名密钥，无法构造
            return ""
    return ""


def _first_lyric_line(lyric_text: str) -> str:
    """从 LRC 文本中提取第一句可显示歌词。"""
    if not lyric_text:
        return ""
    for raw_line in lyric_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 去掉前缀时间戳，如 [00:12.34]
        line = re.sub(r"(?:\[\d{1,2}:\d{1,2}(?:\.\d{1,3})?\])+", "", line).strip()
        if line:
            return line
    return ""


# ===== MCP 工具注册 =====


@mcp.tool()
async def search_music_pro(query: str, limit: int = 10) -> str:
    """搜索音乐。根据关键词搜索歌曲，返回歌曲列表。
    用户说"搜一下周杰伦"、"找首歌"时使用此工具。"""
    results = await _search_api(query, limit)
    if not results:
        return f"未找到与 '{query}' 相关的音乐"

    lines = [f"搜索 '{query}' 的结果：\n"]
    for i, song in enumerate(results, 1):
        lines.append(f"{i}. {song['name']} - {song['artist']}")
        if song.get("url"):
            lines.append(f"   URL: {song['url']}")
    return "\n".join(lines)


@mcp.tool()
async def resolve_music_url(
    id: str | None = None,
    song_id: str | None = None,
    song_name: str = "未知歌曲",
    artist: str = "未知歌手",
    url: str = "",
    lrc: str = "",
    lyric: str = "",
    ctx: Context | None = None,
) -> str:
    """解析歌曲最终直链，不直接播放。兼容 id/song_id 两种参数命名。"""
    resolved_song_id = song_id or id
    if not resolved_song_id and not url:
        return "缺少歌曲信息，请至少传入 id/song_id 或 url"

    if ctx:
        await ctx.report_progress(10, total=100, message="开始解析播放请求")

    source_url = _normalize_music_url(url)
    if not source_url and resolved_song_id:
        if ctx:
            await ctx.report_progress(25, total=100, message="根据歌曲ID查询播放地址")
        source_url = await _fetch_song_url(resolved_song_id)
        if not source_url:
            return "查询播放地址失败，请稍后重试"

    if ctx:
        await ctx.report_progress(55, total=100, message="解析最终可播放直链")
    final_url = await _resolve_final_url(source_url)

    # 构造歌词 URL：
    # 1. 优先用传入的 lrc 参数
    # 2. 其次从搜索缓存中取（大模型常常不传 lrc）
    # 3. 再其次用 song_id 自行构造（需要签名 token）
    # 4. 最后兜底：直接拉取歌词文本
    effective_lrc = lrc
    cache_key = str(resolved_song_id) if resolved_song_id else ""
    if not effective_lrc and cache_key and cache_key in _search_result_cache:
        cached = _search_result_cache[cache_key]
        effective_lrc = cached.get("lrc", "")
        if effective_lrc:
            logger.info("从搜索缓存获取到歌词 URL: %s", effective_lrc[:80])

    lyric_url = _build_lyric_url(resolved_song_id, effective_lrc)
    lyric_text = ""
    if not lyric_url and resolved_song_id:
        if ctx:
            await ctx.report_progress(70, total=100, message="正在获取歌词...")
        lyric_text = await _fetch_song_lyric(resolved_song_id)
        if lyric_text:
            logger.info("通过 _fetch_song_lyric 直接获取到歌词文本（%d 字符）", len(lyric_text))

    if ctx:
        await ctx.report_progress(80, total=100, message="歌词链接已生成")

    playback_state["current_song"] = {
        "id": resolved_song_id or source_url,
        "name": song_name,
        "artist": artist,
        "url": final_url,
        "source_url": source_url,
        "lyric_url": lyric_url,
    }
    playback_state["is_playing"] = False

    if ctx:
        await ctx.report_progress(100, total=100, message="直链解析完成")

    # 构造传给设备端的参数
    next_args: dict[str, str] = {
        "url": final_url,
        "title": song_name,
        "artist": artist,
    }
    if lyric_url:
        next_args["lyric_url"] = lyric_url
    elif lyric_text:
        # lyric_url 构造失败但直接拿到了歌词文本，作为 fallback 传给设备
        next_args["lyric"] = lyric_text

    has_lyric = bool(lyric_url or lyric_text)
    result = {
        "song_name": song_name,
        "artist": artist,
        "final_url": final_url,
        "lyric_url": lyric_url,
        "has_lyric": has_lyric,
        # 明确告诉大模型下一步调用设备端工具，参数已准备好直接透传。
        "next_tool": "self.music.play_url",
        "next_arguments": next_args,
    }
    logger.info(
        "已解析歌曲: %s - %s, lyric_url=%s, has_lyric_text=%s",
        song_name,
        artist,
        lyric_url,
        bool(lyric_text),
    )
    return json.dumps(result, ensure_ascii=False)



@mcp.tool()
async def stop_music() -> str:
    """停止音乐播放并清除当前曲目。"""
    playback_state["is_playing"] = False
    playback_state["current_song"] = None
    return "音乐已停止"

@mcp.tool()
async def add_to_playlist(
    song_id: str,
    song_name: str = "未知歌曲",
    artist: str = "未知歌手",
    url: str = "",
) -> str:
    """将歌曲添加到播放列表。"""
    song = {"id": song_id, "name": song_name, "artist": artist, "url": url}
    playback_state["playlist"].append(song)
    return f"已添加到播放列表: {song_name} - {artist}（共 {len(playback_state['playlist'])} 首）"


@mcp.tool()
async def get_playlist() -> str:
    """获取当前播放列表中的所有歌曲。"""
    playlist = playback_state["playlist"]
    if not playlist:
        return "播放列表为空"
    lines = ["当前播放列表：\n"]
    for i, song in enumerate(playlist, 1):
        lines.append(f"{i}. {song['name']} - {song['artist']}")
    return "\n".join(lines)


@mcp.tool()
async def clear_playlist() -> str:
    """清空播放列表。"""
    playback_state["playlist"] = []
    return "播放列表已清空"


@mcp.tool()
async def next_song() -> str:
    """切换到播放列表中的下一首歌。"""
    playlist = playback_state["playlist"]
    current = playback_state["current_song"]
    if not playlist:
        return "播放列表为空，无法切换到下一首"

    next_index = 0
    if current:
        for i, song in enumerate(playlist):
            if song["id"] == current["id"]:
                next_index = (i + 1) % len(playlist)
                break

    next_s = playlist[next_index]
    playback_state["current_song"] = next_s
    playback_state["is_playing"] = True
    return f"下一首: {next_s['name']} - {next_s['artist']}"


@mcp.tool()
async def previous_song() -> str:
    """切换到播放列表中的上一首歌。"""
    playlist = playback_state["playlist"]
    current = playback_state["current_song"]
    if not playlist:
        return "播放列表为空，无法切换到上一首"

    prev_index = len(playlist) - 1
    if current:
        for i, song in enumerate(playlist):
            if song["id"] == current["id"]:
                prev_index = (i - 1) % len(playlist)
                break

    prev_s = playlist[prev_index]
    playback_state["current_song"] = prev_s
    playback_state["is_playing"] = True
    return f"上一首: {prev_s['name']} - {prev_s['artist']}"


if __name__ == "__main__":
    logger.info("启动免费音乐MCP服务器（FastMCP 标准协议）...")
    mcp.run(transport="stdio")
