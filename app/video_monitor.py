# video_monitor.py - 视频监控和回复模块
import asyncio
import inspect
import json
from datetime import datetime, date, timedelta, timezone
from typing import List, Dict, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from bilibili_api import comment, video, user
from .config import ConfigManager
from .database import reply_record_manager, video_discovery_manager
from .api_clients import CredentialManager, generate_humorous_reply
from .logger import logger


def _get_credential():
    return CredentialManager().get_credential()


def _make_video(bvid: str):
    return video.Video(bvid=bvid, credential=_get_credential())


def _normalize_uid(uid) -> str:
    text = str(uid or "").strip()
    if not text or text.startswith("请填写"):
        raise ValueError(f"uploader_uid 无效: {uid!r}")
    return text


def _make_user(uid) -> "user.User":
    uid_str = _normalize_uid(uid)
    try:
        uid_int = int(uid_str)
    except (TypeError, ValueError) as e:
        raise ValueError(f"uploader_uid 必须是数字: {uid!r}") from e
    return user.User(uid_int, credential=_get_credential())


def _is_risk_control(error) -> bool:
    msg = str(error)
    code = getattr(error, "code", None)
    if code in (412, -352, -401, -412):
        return True
    return any(token in msg for token in ("412", "-352", "风控", "Precondition Failed"))


def _extract_vlist(payload) -> list:
    """兼容 get_videos / 部分空间接口的投稿列表结构。"""
    if not isinstance(payload, dict):
        return []

    lst = payload.get("list")
    if isinstance(lst, dict):
        for key in ("vlist", "archives", "media_list"):
            val = lst.get(key)
            if isinstance(val, list):
                return val
    elif isinstance(lst, list):
        return lst

    for key in ("vlist", "archives", "media_list"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []


def _extract_api_total(payload) -> Optional[int]:
    if not isinstance(payload, dict):
        return None
    page = payload.get("page")
    if isinstance(page, dict) and page.get("count") is not None:
        try:
            return int(page.get("count"))
        except (TypeError, ValueError):
            pass
    for key in ("count", "total"):
        if payload.get(key) is not None:
            try:
                return int(payload.get(key))
            except (TypeError, ValueError):
                pass
    return None


CST = timezone(timedelta(hours=8))


def _item_pubdate_ts(item) -> Optional[int]:
    if not isinstance(item, dict):
        return None
    for key in ("created", "pubdate", "pubtime", "ctime", "created_at", "publish_time"):
        val = item.get(key)
        if val is None or val == "":
            continue
        try:
            ts = int(float(val))
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        if ts > 10_000_000_000:
            ts //= 1000
        return ts
    return None


def _pub_day(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), tz=CST).date()


def _video_in_date_range(pub_ts: Optional[int], after: Optional[date], before: Optional[date]) -> bool:
    if after is None and before is None:
        return True
    if pub_ts is None:
        return False
    day = _pub_day(pub_ts)
    if after and day < after:
        return False
    if before and day > before:
        return False
    return True


def _video_in_any_range(pub_ts: Optional[int], ranges) -> bool:
    if not ranges:
        return True
    return any(_video_in_date_range(pub_ts, after, before) for after, before in ranges)


def _should_reply_by_pubdate(pub_ts: Optional[int]) -> bool:
    ranges = ConfigManager().get_video_date_ranges()
    if not ranges:
        return True
    return _video_in_any_range(pub_ts, ranges)


_SKIPPED_DATE_LOGGED = set()


def _log_skip_out_of_range(bvid: str, title: str, pub_ts: Optional[int]):
    key = (bvid, ConfigManager().describe_video_date_filter())
    day = _pub_day(pub_ts).isoformat() if pub_ts else "未知"
    msg = (
        f"跳过日期范围外的视频（不回复）: {title} ({bvid}) "
        f"投稿日={day}，过滤={ConfigManager().describe_video_date_filter()}"
    )
    if key not in _SKIPPED_DATE_LOGGED:
        logger.info(msg)
        _SKIPPED_DATE_LOGGED.add(key)
    else:
        logger.debug(msg)


def _date_ranges_oldest_start(ranges) -> Optional[date]:
    """所有日期段里最早的起点；若某段没有起点则无法按过旧停翻页。"""
    if not ranges:
        return None
    starts = []
    for after, _before in ranges:
        if after is None:
            return None
        starts.append(after)
    return min(starts) if starts else None


def _item_to_video(item) -> Optional[Dict]:
    if not isinstance(item, dict):
        return None
    bvid = item.get("bvid") or item.get("bv_id")
    if not bvid:
        return None
    title = item.get("title") or item.get("name") or ""
    return {
        "bvid": str(bvid),
        "title": title,
        "pubdate": _item_pubdate_ts(item),
    }


_OWN_MID_CACHE = {"mid": ""}


def _own_mid_from_config() -> str:
    cred = ConfigManager().get_bilibili_credential() or {}
    return str(cred.get("dedeuserid") or "").strip()


def _mid_eq(a, b) -> bool:
    sa, sb = str(a or "").strip(), str(b or "").strip()
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    try:
        return int(sa) == int(sb)
    except (TypeError, ValueError):
        return False


def _comment_mid(item) -> str:
    if not isinstance(item, dict):
        return ""
    mid = item.get("mid")
    if mid is None:
        member = item.get("member") or {}
        mid = member.get("mid") if isinstance(member, dict) else None
    return str(mid or "").strip()


def _comment_rpid(item) -> str:
    if not isinstance(item, dict):
        return ""
    return str(item.get("rpid_str") or item.get("rpid") or "").strip()


def _nested_replies(root) -> list:
    if not isinstance(root, dict):
        return []
    replies = root.get("replies")
    if isinstance(replies, list):
        return replies
    for key in ("reply", "children"):
        val = root.get(key)
        if isinstance(val, list):
            return val
    return []


def _walk_replies(root):
    for item in _nested_replies(root):
        yield item
        yield from _walk_replies(item)


def _preview_has_own_reply(root, own_mid: str) -> bool:
    if not own_mid:
        return False
    return any(_mid_eq(_comment_mid(item), own_mid) for item in _walk_replies(root))


def _is_child_of(item, root_rpid: str) -> bool:
    if not root_rpid or not isinstance(item, dict):
        return False
    parent = str(item.get("parent") or "").strip()
    root = str(item.get("root") or "").strip()
    return _mid_eq(parent, root_rpid) or _mid_eq(root, root_rpid)


def _extract_sub_replies(data) -> list:
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("replies", "root_replies"):
        val = data.get(key)
        if isinstance(val, list):
            return val
    inner = data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("replies"), list):
        return inner.get("replies")
    return []


def _credential_cookies(credential) -> dict:
    cookies = {}
    mapping = {
        "sessdata": "SESSDATA",
        "bili_jct": "bili_jct",
        "buvid3": "buvid3",
        "dedeuserid": "DedeUserID",
        "ac_time_value": "ac_time_value",
    }
    getter = getattr(credential, "get_cookies", None)
    if callable(getter):
        try:
            raw = getter() or {}
            if isinstance(raw, dict):
                cookies.update({str(k): str(v) for k, v in raw.items() if v is not None})
        except Exception:
            pass
    for attr, name in mapping.items():
        val = getattr(credential, attr, None)
        if val:
            cookies[name] = str(val)
    return cookies


def _http_get_json(url: str, cookies: dict) -> dict:
    cookie_header = "; ".join(f"{k}={v}" for k, v in cookies.items() if v)
    req = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com",
            "Cookie": cookie_header,
        },
        method="GET",
    )
    with urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    return json.loads(body)


async def _resolve_own_mid(credential) -> str:
    cached = _OWN_MID_CACHE.get("mid")
    if cached:
        return cached

    mid = ""
    getter = getattr(user, "get_self_info", None)
    if callable(getter):
        try:
            info = await getter(credential)
            if isinstance(info, dict):
                mid = str(info.get("mid") or "").strip()
        except TypeError:
            try:
                info = await getter(credential=credential)
                if isinstance(info, dict):
                    mid = str(info.get("mid") or "").strip()
            except Exception as e:
                logger.warning(f"获取登录账号信息失败，改用配置中的 DedeUserID: {e}")
        except Exception as e:
            logger.warning(f"获取登录账号信息失败，改用配置中的 DedeUserID: {e}")

    if not mid:
        mid = _own_mid_from_config()
    _OWN_MID_CACHE["mid"] = mid
    if mid:
        logger.info(f"当前账号 mid={mid}，将用它识别自己的回复")
    else:
        logger.warning("无法确定当前账号 mid，可能重复回复")
    return mid


def _page_has_own_child(page_comments: list, root_rpid: str, own_mid: str) -> bool:
    if not own_mid or not root_rpid:
        return False
    for item in page_comments or []:
        if _mid_eq(_comment_mid(item), own_mid) and _is_child_of(item, root_rpid):
            return True
        if _comment_rpid(item) == root_rpid:
            if any(_mid_eq(_comment_mid(child), own_mid) for child in _walk_replies(item)):
                return True
        for child in _walk_replies(item):
            if _mid_eq(_comment_mid(child), own_mid) and _is_child_of(child, root_rpid):
                return True
    return False


async def _list_sub_replies(oid, rpid, credential) -> Optional[list]:
    """拉取某条评论的楼中楼。失败返回 None。"""
    collected = []

    CommentCls = getattr(comment, "Comment", None)
    if CommentCls is not None:
        try:
            sub = CommentCls(
                oid=int(oid),
                type_=comment.CommentResourceType.VIDEO,
                rpid=int(rpid),
                credential=credential,
            )
            for pn in range(1, 6):
                try:
                    data = await sub.get_sub_comments(page_index=pn, page_size=20)
                except TypeError:
                    try:
                        data = await sub.get_sub_comments(page_index=pn)
                    except TypeError:
                        data = await sub.get_sub_comments(pn)
                replies = _extract_sub_replies(data)
                if not replies:
                    break
                collected.extend(replies)
                if len(replies) < 10:
                    break
                await asyncio.sleep(0.35)
        except Exception as e:
            logger.warning(f"库接口拉取楼中楼失败 rpid={rpid}: {e}")

    if collected:
        return collected

    try:
        cookies = _credential_cookies(credential)
        for pn in range(1, 6):
            params = urlencode({
                "oid": int(oid),
                "type": 1,
                "root": int(rpid),
                "ps": 20,
                "pn": pn,
            })
            url = f"https://api.bilibili.com/x/v2/reply/reply?{params}"
            payload = await asyncio.to_thread(_http_get_json, url, cookies)
            if int(payload.get("code") or 0) != 0:
                logger.warning(
                    f"楼中楼接口失败 rpid={rpid} code={payload.get('code')} {payload.get('message')}"
                )
                return collected if collected else None
            replies = _extract_sub_replies(payload.get("data") or payload)
            if replies is None:
                replies = []
            if not replies:
                return collected
            collected.extend(replies)
            if len(replies) < 20:
                break
            await asyncio.sleep(0.35)
        return collected
    except Exception as e:
        logger.warning(f"HTTP 拉取楼中楼失败 rpid={rpid}: {e}")
        return collected if collected else None


async def has_own_reply_on_comment(
    oid, root, own_mid: str, credential, page_comments: list = None
) -> Optional[bool]:
    """当前评论下是否已有自己的回复。无法确认时返回 None（本轮不发）。"""
    root_rpid = _comment_rpid(root)
    if not own_mid:
        logger.warning("未配置/未识别当前账号 mid，无法判断是否已回复")
        return None
    if _preview_has_own_reply(root, own_mid):
        return True
    if _page_has_own_child(page_comments or [], root_rpid, own_mid):
        return True

    replies = await _list_sub_replies(oid, root_rpid, credential)
    if replies is None:
        return None
    return any(_mid_eq(_comment_mid(item), own_mid) for item in replies)


def _remember_handled_comment(bvid: str, cmt: dict, reason: str = ""):
    """把已处理/已有自己回复的评论写入本地，避免下轮再请求楼中楼。"""
    rpid = _comment_rpid(cmt)
    if not rpid:
        return
    member = cmt.get("member") or {}
    username = member.get("uname") if isinstance(member, dict) else None
    message = (cmt.get("content") or {}).get("message") if isinstance(cmt.get("content"), dict) else None
    reply_record_manager.mark_replied(bvid, rpid, username=username, message=message)
    if reason:
        logger.info(f"{bvid} rpid={rpid} 跳过回复: {reason}")


async def get_new_comments(bvid: str) -> list:
    """获取视频下的新评论"""
    try:
        v = _make_video(bvid)
        video_info = await v.get_info()
        oid = video_info["aid"]
    except Exception as e:
        logger.error(f"获取视频信息失败 {bvid}: {e}")
        return []

    comments = []
    pag = ""
    credential = _get_credential()
    own_mid = await _resolve_own_mid(credential)
    max_pages = 10
    page = 0

    while page < max_pages:
        try:
            c = await comment.get_comments_lazy(
                oid=oid,
                type_=comment.CommentResourceType.VIDEO,
                offset=pag,
                credential=credential
            )

            cursor = c.get("cursor", {}) or {}
            pagination_reply = cursor.get("pagination_reply", {}) or {}
            pag = pagination_reply.get("next_offset", "")

            replies = c.get('replies')
            if not replies:
                break

            for r in replies:
                rpid = _comment_rpid(r)
                if not rpid:
                    continue
                if own_mid and _mid_eq(_comment_mid(r), own_mid):
                    _remember_handled_comment(bvid, r, "自己的评论")
                    continue
                if reply_record_manager.is_replied(bvid, rpid):
                    continue

                own_replied = await has_own_reply_on_comment(
                    oid, r, own_mid, credential, page_comments=replies
                )
                if own_replied is True:
                    _remember_handled_comment(bvid, r, "评论下已有自己的回复")
                    continue
                if own_replied is None:
                    logger.info(f"{bvid} rpid={rpid} 无法确认是否已回复，本轮跳过以免重复")
                    continue
                comments.append(r)

            is_end = cursor.get("is_end", True)
            if is_end:
                break

            page += 1

        except Exception as e:
            logger.error(f"获取评论失败 {bvid}: {e}")
            break

    return comments


def _collect_from_vlist(vlist, known, stop_at_known, full_sync, count, result, seen) -> bool:
    """把一页投稿写入 result。返回 True 表示应停止翻页（遇到已知视频、过旧投稿或达到数量）。"""
    ranges = ConfigManager().get_video_date_ranges()
    oldest_start = _date_ranges_oldest_start(ranges)
    for item in vlist:
        video_item = _item_to_video(item)
        if not video_item:
            continue
        bvid = video_item["bvid"]
        pub_ts = video_item.get("pubdate")

        if ranges:
            if pub_ts is None:
                continue
            day = _pub_day(pub_ts)
            if oldest_start and day < oldest_start:
                # 比所有区间都更早，后面只会更旧
                return True
            if not _video_in_any_range(pub_ts, ranges):
                continue

        if stop_at_known is not None and bvid in known:
            return True
        if bvid in seen:
            continue
        seen.add(bvid)
        result.append(video_item)
        if not full_sync and stop_at_known is None and len(result) >= count:
            return True
    return False


async def _paginate_get_videos(u, page_size, full_sync, stop_at_known, known, count) -> Dict:
    result = []
    seen = set()
    api_total = None
    pn = 1
    if full_sync:
        max_pages = 200
    elif stop_at_known is not None:
        max_pages = 50
    else:
        max_pages = 1

    incomplete = False
    while pn <= max_pages:
        videos = await u.get_videos(pn=pn, ps=page_size)
        if api_total is None:
            api_total = _extract_api_total(videos)

        vlist = _extract_vlist(videos)
        if not vlist:
            if pn == 1:
                keys = list(videos.keys()) if isinstance(videos, dict) else type(videos).__name__
                logger.warning(f"get_videos 第 1 页未解析到投稿列表，返回字段: {keys}")
            break

        hit_stop = _collect_from_vlist(vlist, known, stop_at_known, full_sync, count, result, seen)
        if hit_stop or (not full_sync and stop_at_known is None):
            break

        if full_sync:
            if api_total is not None and len(result) >= api_total:
                break
            if len(vlist) < page_size and (api_total is None or len(result) >= api_total):
                break
            if len(vlist) < page_size and api_total is not None and len(result) < api_total:
                logger.warning(
                    f"全量同步第 {pn} 页只返回 {len(vlist)} 条，"
                    f"已获取 {len(result)}/{api_total}，继续翻页…"
                )
        elif len(vlist) < page_size:
            break

        pn += 1
        if pn > max_pages:
            incomplete = True
            logger.warning(f"已达翻页上限 {max_pages}，全量同步可能不完整")
            break
        await asyncio.sleep(1.0)

    if full_sync and api_total is not None and len(result) < api_total:
        incomplete = True
    return {"videos": result, "api_total": api_total, "incomplete": incomplete, "pages": pn}


async def _paginate_media_list(u, page_size, full_sync, stop_at_known, known, count) -> Dict:
    """get_videos 失败或空列表时的备用接口（空间合集/稿件列表）。"""
    if not hasattr(u, "get_media_list"):
        return {"videos": [], "api_total": None, "incomplete": True, "pages": 0}

    result = []
    seen = set()
    api_total = None
    oid = None
    max_pages = 200 if full_sync else (50 if stop_at_known is not None else 1)
    incomplete = False

    for pn in range(1, max_pages + 1):
        kwargs = {"ps": min(int(page_size), 100)}
        if oid is not None:
            kwargs["oid"] = oid
        data = await u.get_media_list(**kwargs)
        if api_total is None:
            api_total = _extract_api_total(data)

        items = _extract_vlist(data)
        if not items and isinstance(data, dict) and isinstance(data.get("media_list"), list):
            items = data.get("media_list")
        if not items:
            break

        hit_stop = _collect_from_vlist(items, known, stop_at_known, full_sync, count, result, seen)
        has_more = None
        if isinstance(data, dict):
            if "has_more" in data:
                has_more = bool(data.get("has_more"))
            elif "has_next" in data:
                has_more = bool(data.get("has_next"))

        last = items[-1] if items else {}
        next_oid = None
        if isinstance(last, dict):
            next_oid = last.get("id") or last.get("aid") or last.get("oid")

        if hit_stop or (not full_sync and stop_at_known is None):
            break
        if has_more is False:
            break
        if has_more is None and len(items) < kwargs["ps"]:
            break
        if full_sync and api_total is not None and len(result) >= api_total:
            break
        if next_oid is None or next_oid == oid:
            incomplete = full_sync
            break

        oid = next_oid
        if pn >= max_pages:
            incomplete = True
            break
        await asyncio.sleep(1.0)

    if full_sync and api_total is not None and len(result) < api_total:
        incomplete = True
    return {"videos": result, "api_total": api_total, "incomplete": incomplete, "pages": pn}


async def get_uploader_videos(
    uid: str,
    count: int = 10,
    full_sync: bool = False,
    stop_at_known: set = None,
) -> Dict:
    """获取UP主投稿视频

    Returns:
        {
          "videos": [{"bvid","title"}, ...],
          "complete": bool,
          "api_total": int|None,
        }
    """
    max_retries = 3
    retry_delay = 5
    page_size = 50
    if not full_sync and not stop_at_known:
        page_size = max(1, min(int(count), 50))

    known = stop_at_known or set()
    best_partial = []
    best_total = None
    uid_str = _normalize_uid(uid)

    for attempt in range(max_retries):
        result = []
        api_total = None
        try:
            u = _make_user(uid_str)
            page_data = await _paginate_get_videos(
                u, page_size, full_sync, stop_at_known, known, count
            )
            result = page_data.get("videos") or []
            api_total = page_data.get("api_total")
            incomplete = bool(page_data.get("incomplete"))
            pn = page_data.get("pages", 1)

            if not result:
                logger.warning("get_videos 未拿到投稿，改用 get_media_list 重试")
                page_data = await _paginate_media_list(
                    u, page_size, full_sync, stop_at_known, known, count
                )
                result = page_data.get("videos") or []
                api_total = page_data.get("api_total") if page_data.get("api_total") is not None else api_total
                incomplete = bool(page_data.get("incomplete"))
                pn = page_data.get("pages", pn)

            if full_sync:
                if api_total is not None and len(result) < api_total:
                    incomplete = True
                complete = not incomplete
                mode = f"全量({'完整' if complete else '不完整'}, api_total={api_total})"
            elif stop_at_known is not None:
                complete = True
                mode = f"增量(遇已知即停, 翻页{pn})"
            else:
                complete = True
                mode = "增量(固定窗口)"
                result = result[:count]

            logger.info(f"获取到UP主 {uid_str} 的 {len(result)} 个投稿（{mode}）")
            return {"videos": result, "complete": complete, "api_total": api_total}

        except Exception as e:
            if result:
                best_partial = list(result)
                best_total = api_total

            if _is_risk_control(e):
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (attempt + 1)
                    logger.warning(
                        f"触发B站风控({e})，{wait_time}秒后重试 ({attempt + 1}/{max_retries})..."
                    )
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(
                    f"触发B站风控，已重试{max_retries}次仍失败。"
                    f"将先保存已获取的 {len(best_partial)} 条，并标记全量未完成以便下次继续。"
                )
                return {"videos": best_partial, "complete": False, "api_total": best_total}

            logger.error(f"获取UP主 {uid_str} 视频失败: {e}")
            try:
                logger.warning("主接口异常，尝试 get_media_list")
                fallback_user = _make_user(uid_str)
                page_data = await _paginate_media_list(
                    fallback_user, page_size, full_sync, stop_at_known, known, count
                )
                result = page_data.get("videos") or best_partial
                api_total = page_data.get("api_total")
                if result:
                    if not full_sync and stop_at_known is None:
                        result = result[:count]
                    complete = (not bool(page_data.get("incomplete"))) if full_sync else True
                    logger.info(f"get_media_list 获取到 UP主 {uid_str} 的 {len(result)} 个投稿")
                    return {
                        "videos": result,
                        "complete": complete,
                        "api_total": api_total,
                    }
            except Exception as fallback_err:
                logger.error(f"get_media_list 也失败: {fallback_err}")
            return {"videos": best_partial, "complete": False, "api_total": best_total}

    return {"videos": best_partial, "complete": False, "api_total": best_total}


async def get_uploader_latest_videos(uid: str, count: int = 5) -> List[Dict]:
    """获取UP主的最新投稿视频（兼容旧调用）"""
    data = await get_uploader_videos(uid, count=count, full_sync=False)
    return data.get('videos') or []


async def discover_and_add_new_videos(uploader_uid: str, auto_save: bool = False,
                                     force_full_sync: bool = False) -> List[str]:
    """发现并添加UP主的新视频到监控列表（写入 SQLite）

    未完成「完整全量同步」前，不会走遇已知即停的增量逻辑，避免风控中断后漏掉更早投稿。
    """
    from .database import monitored_video_manager, database_manager

    logger.info(f"开始检查UP主 {uploader_uid} 的新视频...")

    try:
        uid = _normalize_uid(uploader_uid)
    except ValueError as e:
        logger.error(f"无法发现新视频: {e}")
        return []

    config_manager = ConfigManager()
    app_config = config_manager.get_app_config()
    default_ai_style = app_config.get('default_ai_style', 'humorous')
    date_ranges = config_manager.get_video_date_ranges()
    if date_ranges:
        logger.info(f"只发现/监控日期范围内的视频：{config_manager.describe_video_date_filter()}")

    discovered_count = video_discovery_manager.get_discovered_count(uid)
    monitored_count = monitored_video_manager.count(enabled_only=False)
    full_sync_done = database_manager.is_full_sync_done(uid)

    full_sync = (
        force_full_sync
        or discovered_count == 0
        or monitored_count == 0
        or not full_sync_done
    )

    if full_sync:
        logger.info(
            f"执行全量同步（discovered={discovered_count}, monitored={monitored_count}, "
            f"full_sync_done={full_sync_done}）…"
        )
        fetch = await get_uploader_videos(uid, full_sync=True)
    else:
        known = {
            item['bvid']
            for item in database_manager.list_discovered_videos(uid=uid)
        }
        for v in monitored_video_manager.list_videos(enabled_only=False):
            known.add(v['bvid'])

        logger.info(f"执行增量同步（已知 {len(known)} 个，遇已知 bvid 即停止翻页）")
        fetch = await get_uploader_videos(
            uid, full_sync=False, stop_at_known=known
        )

    latest_videos = fetch.get('videos') or []
    sync_complete = bool(fetch.get('complete'))
    api_total = fetch.get('api_total')

    if not latest_videos:
        if full_sync:
            if date_ranges and sync_complete:
                database_manager.set_full_sync_done(uid, True)
                logger.info("该日期范围内没有投稿，全量同步视为完成")
            else:
                database_manager.set_full_sync_done(uid, False)
                logger.warning("全量同步未获取到视频，已标记未完成，下次继续全量重试")
        else:
            logger.debug("未获取到新视频列表（可能没有更新）")
        return []

    new_added = []

    for vid in latest_videos:
        bvid = vid['bvid']
        title = vid['title']
        if date_ranges:
            if not _video_in_any_range(vid.get('pubdate'), date_ranges):
                logger.debug(
                    f"跳过日期范围外的视频: {title} ({bvid})"
                )
                continue

        if video_discovery_manager.is_discovered(uid, bvid):
            if not monitored_video_manager.is_monitored(bvid):
                if config_manager.add_video_to_config(
                    bvid, use_ai=True, ai_style=default_ai_style, title=title
                ):
                    new_added.append(bvid)
                    logger.info(f"✓ 已补入监控: {title} ({bvid}) [风格: {default_ai_style}]")
            else:
                logger.debug(f"视频已发现: {title} ({bvid})")
            continue

        video_discovery_manager.mark_discovered(uid=uid, bvid=bvid, title=title)

        if config_manager.add_video_to_config(bvid, use_ai=True, ai_style=default_ai_style, title=title):
            new_added.append(bvid)
            logger.info(f"✓ 新视频已添加监控: {title} ({bvid}) [风格: {default_ai_style}]")

    if full_sync:
        monitored_after = monitored_video_manager.count(enabled_only=False)
        aligned = True
        if not date_ranges and api_total is not None and monitored_after < max(api_total - 2, 0):
            aligned = False
            logger.warning(
                f"全量同步后监控数 {monitored_after} < 接口投稿数 {api_total}，视为未完成"
            )

        done = sync_complete and aligned
        database_manager.set_full_sync_done(uid, done)
        if done:
            logger.info(f"全量同步已完成（监控 {monitored_after} 个），之后改为增量发现")
        else:
            logger.warning(
                "全量同步不完整（风控/翻页中断/数量不足），"
                "下次仍会全量重试，不会因为已有部分视频而漏掉更早投稿"
            )

    if new_added:
        logger.info(f"共发现/补入 {len(new_added)} 个视频")
    else:
        logger.debug("未发现新视频")

    return new_added


async def reply_to_single_comment(oid: int, bvid: str, cmt: dict,
                                  reply_message: str, credential) -> tuple:
    """回复单条评论，记录并输出收到的评论内容与回复内容"""
    username = cmt['member']['uname']
    rpid = _comment_rpid(cmt) or cmt.get('rpid')
    original_message = cmt.get('content', {}).get('message', '')

    try:
        send_kwargs = {
            "text": reply_message,
            "oid": oid,
            "type_": comment.CommentResourceType.VIDEO,
            "credential": credential,
        }
        try:
            rpid_int = int(str(rpid).strip())
        except (TypeError, ValueError):
            rpid_int = rpid
        sig = inspect.signature(comment.send_comment)
        if "root" in sig.parameters:
            send_kwargs["root"] = rpid_int
        if "parent" in sig.parameters:
            send_kwargs["parent"] = rpid_int
        await comment.send_comment(**send_kwargs)

        # 标记为已回复（保留原有调用）
        reply_record_manager.mark_replied(bvid, rpid, username=username, message=original_message)

        # 更详细的日志：显示收到的评论与回复的完整内容
        logger.info(f"✓ 成功回复 @{username} (rpid: {rpid})")
        logger.info(f"收到评论: {original_message}")
        logger.info(f"回复内容: {reply_message}")

        return True, None

    except Exception as e:
        error_msg = str(e)
        # -101 未登录：Cookie 失效/不完整，无需整段 traceback 刷屏
        if '-101' in error_msg or '账号未登录' in error_msg:
            logger.error(
                f"✗ 回复 @{username} 失败: 账号未登录(-101)。"
                f"请重新从浏览器复制 SESSDATA、bili_jct、buvid3、DedeUserID、ac_time_value "
                f"到 config.yaml（需同一登录态），然后重启。"
            )
        else:
            logger.error(f"✗ 回复 @{username} 失败 (rpid: {rpid}): {error_msg}", exc_info=True)
        logger.error(f"准备回复的内容: {reply_message}")
        logger.error(f"收到评论: {original_message}")
        return False, error_msg


async def reply_to_new_comments(bvid: str, reply_template: str = "感谢评论！",
                                use_ai: bool = False, ai_style: str = None) -> dict:
    """检测并回复新评论

    Args:
        bvid: 视频BV号
        reply_template: 回复模板
        use_ai: 是否使用AI回复
        ai_style: AI回复风格

    Returns:
        回复统计结果
    """
    try:
        v = _make_video(bvid)
        video_info = await v.get_info()
    except Exception as e:
        logger.error(f"获取视频信息失败 {bvid}: {e}")
        return {'success': 0, 'failed': 0, 'total': 0}

    oid = video_info["aid"]
    video_title = video_info.get('title', '')
    video_desc = video_info.get('desc', '')
    video_url = f"https://www.bilibili.com/video/{bvid}"
    pub_ts = _item_pubdate_ts(video_info)
    if not _should_reply_by_pubdate(pub_ts):
        _log_skip_out_of_range(bvid, video_title, pub_ts)
        return {'success': 0, 'failed': 0, 'total': 0}
    credential = _get_credential()

    new_comments = await get_new_comments(bvid)

    if not new_comments:
        # 将无新评论的常规状态改为 DEBUG，避免日志被大量占用
        logger.debug(f"视频标题: {video_title}({bvid}) - 没有新评论(已回复: {reply_record_manager.get_replied_count(bvid)})")
        return {'success': 0, 'failed': 0, 'total': 0}

    logger.info(f"视频标题: {video_title}({bvid}) - 发现 {len(new_comments)} 条新评论")

    success_count = 0
    failed_count = 0

    for idx, cmt in enumerate(new_comments, 1):
        member_info = cmt.get('member', {}) or {}
        username = member_info.get('uname', '')
        message = (cmt.get('content') or {}).get('message', '')
        user_mid = member_info.get('mid')
        rpid = _comment_rpid(cmt)
        own_mid = await _resolve_own_mid(credential)

        if own_mid and _mid_eq(_comment_mid(cmt), own_mid):
            _remember_handled_comment(bvid, cmt, "自己的评论")
            continue
        if rpid and reply_record_manager.is_replied(bvid, rpid):
            continue
        own_replied = await has_own_reply_on_comment(oid, cmt, own_mid, credential)
        if own_replied is True:
            _remember_handled_comment(bvid, cmt, "评论下已有自己的回复")
            logger.info(f"跳过 @{username}：该评论下已有自己的回复")
            continue
        if own_replied is None:
            logger.info(f"跳过 @{username}：无法确认是否已回复，本轮不发送")
            continue

        if use_ai:
            style_tag = f"[{ai_style or 'default'}]"
            logger.debug(f"[{idx}/{len(new_comments)}] {style_tag} 处理评论 @{username}: {message[:50]}{'...' if len(message) > 50 else ''}")
            # 同步 HTTP 调用放到线程池，避免阻塞整条事件循环
            reply_message = await asyncio.to_thread(
                generate_humorous_reply,
                video_title, video_desc, video_url, username, message, ai_style,
            )
            logger.debug(f"[{idx}/{len(new_comments)}] {style_tag} AI回复: {reply_message[:50]}{'...' if len(reply_message) > 50 else ''}")
        else:
            logger.debug(f"[{idx}/{len(new_comments)}] 处理评论 @{username}: {message[:50]}{'...' if len(message) > 50 else ''}")
            reply_message = reply_template.format(username=username, message=message)

        success, error = await reply_to_single_comment(oid, bvid, cmt, reply_message, credential)

        if success:
            success_count += 1
            from datetime import datetime
            user_comment_time = datetime.fromtimestamp(cmt.get('ctime', 0)).strftime('%Y-%m-%d %H:%M:%S')
            reply_record_manager.mark_replied_with_details(
                bvid,
                str(_comment_rpid(cmt) or cmt.get('rpid')),
                username=username,
                message=message,
                mid=user_mid,
                user_comment_time=user_comment_time,
                ai_reply=reply_message
            )
        else:
            failed_count += 1

        await asyncio.sleep(2)

    logger.info(f"本次回复完成 - 成功: {success_count}, 失败: {failed_count}")

    return {
        'success': success_count,
        'failed': failed_count,
        'total': len(new_comments)
    }


async def monitor_single_video(bvid: str, reply_template: str, interval: int,
                               use_ai: bool = False, ai_style: str = None):
    """监控单个视频

    Args:
        bvid: 视频BV号
        reply_template: 回复模板
        interval: 检查间隔（秒）
        use_ai: 是否使用AI回复
        ai_style: AI回复风格
    """
    while True:
        try:
            v = _make_video(bvid)
            info = await v.get_info()
            pub_ts = _item_pubdate_ts(info)
            if not _should_reply_by_pubdate(pub_ts):
                _log_skip_out_of_range(bvid, info.get('title', ''), pub_ts)
                break
            ai_tag = "[AI]" if use_ai else "[模板]"
            style_info = f" ({ai_style})" if use_ai and ai_style else ""
            logger.info(f"{ai_tag}{style_info} 开始监控: {info['title']} ({bvid})")
            break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"获取视频信息失败 {bvid}: {e}，{interval} 秒后重试")
            await asyncio.sleep(interval)

    while True:
        try:
            # 配置热加载由发现循环/低频检查即可，避免上百个监控任务同时刷磁盘
            await reply_to_new_comments(bvid, reply_template, use_ai, ai_style)
        except asyncio.CancelledError:
            logger.info(f"监控任务已取消: {bvid}")
            break
        except Exception as e:
            logger.error(f"{bvid} - 监控出错: {e}", exc_info=True)

        await asyncio.sleep(interval)
