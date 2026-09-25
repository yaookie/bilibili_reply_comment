# video_monitor.py - 视频监控和回复模块
import asyncio
from typing import List, Dict
from bilibili_api import comment, video, user
from .config import ConfigManager
from .database import reply_record_manager, video_discovery_manager
from .api_clients import CredentialManager, generate_humorous_reply
from .logger import logger


async def get_new_comments(bvid: str) -> list:
    """获取视频下的新评论"""
    try:
        v = video.Video(bvid=bvid)
        video_info = await v.get_info()
        oid = video_info["aid"]
    except Exception as e:
        logger.error(f"获取视频信息失败 {bvid}: {e}")
        return []

    comments = []
    pag = ""
    credential = CredentialManager().get_credential()
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

            cursor = c.get("cursor", {})
            pagination_reply = cursor.get("pagination_reply", {})
            pag = pagination_reply.get("next_offset", "")

            replies = c.get('replies')
            if not replies:
                break

            new_replies = [r for r in replies if not reply_record_manager.is_replied(bvid, r['rpid'])]
            comments.extend(new_replies)

            is_end = cursor.get("is_end", True)
            if is_end:
                break

            page += 1

        except Exception as e:
            logger.error(f"获取评论失败 {bvid}: {e}")
            break

    return comments


async def get_uploader_latest_videos(uid: str, count: int = 5) -> List[Dict]:
    """获取UP主的最新投稿视频

    Args:
        uid: UP主UID
        count: 获取数量

    Returns:
        视频信息列表，包含bvid、title等
    """
    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            u = user.User(uid=int(uid))
            videos = await u.get_videos(pn=1, ps=count)

            result = []
            vlist = videos.get('list', {}).get('vlist', [])
            # 获取UP主用户名
            # uname = videos.get('list')['vlist'][0]['author'] if videos.get('list', {}).get('tlist') else ''

            for v in vlist:
                result.append({
                    'bvid': v['bvid'],
                    # 'uname': uname,
                    'title': v['title']
                })

            logger.info(f"获取到UP主 {uid} 的 {len(result)} 个最新视频")
            return result

        except Exception as e:
            error_msg = str(e)

            # 检查是否是 412 风控错误
            if '412' in error_msg:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (attempt + 1)
                    logger.warning(f"触发B站风控(412)，{wait_time}秒后重试 ({attempt + 1}/{max_retries})...")
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    logger.error(
                        f"触发B站风控(412)，已重试{max_retries}次仍失败。建议：1)降低检查频率 2)检查Cookie是否有效 3)稍后再试")
                    return []
            else:
                logger.error(f"获取UP主 {uid} 视频失败: {e}")
                return []

    return []


async def discover_and_add_new_videos(uploader_uid: str, auto_save: bool = False) -> List[str]:
    """发现并添加UP主的新视频到监控列表（写入 SQLite）

    Args:
        uploader_uid: UP主UID
        auto_save: 兼容旧参数，已无实际作用（视频直接落库）

    Returns:
        新添加的视频BVID列表
    """
    logger.info(f"开始检查UP主 {uploader_uid} 的新视频...")

    # 获取最新视频
    latest_videos = await get_uploader_latest_videos(uploader_uid, count=10)

    if not latest_videos:
        logger.warning("未获取到视频列表")
        return []

    new_added = []
    config_manager = ConfigManager()

    # 获取默认的AI风格
    app_config = config_manager.get_app_config()
    default_ai_style = app_config.get('default_ai_style', 'humorous')

    for vid in latest_videos:
        bvid = vid['bvid']
        title = vid['title']

        # 检查是否已发现过
        if video_discovery_manager.is_discovered(uploader_uid, bvid):
            # 已发现的视频改为 DEBUG 级别，减少重复日志噪音
            logger.debug(f"视频已发现: {title} ({bvid})")
            continue

        # 标记为已发现
        video_discovery_manager.mark_discovered(uid=uploader_uid, bvid=bvid, title=title)

        # 添加到监控数据库
        if config_manager.add_video_to_config(bvid, use_ai=True, ai_style=default_ai_style, title=title):
            new_added.append(bvid)
            logger.info(f"✓ 新视频已添加监控: {title} ({bvid}) [风格: {default_ai_style}]")

    if new_added:
        logger.info(f"共发现 {len(new_added)} 个新视频")
    else:
        logger.debug("未发现新视频")

    return new_added


async def reply_to_single_comment(oid: int, bvid: str, cmt: dict,
                                  reply_message: str, credential) -> tuple:
    """回复单条评论，记录并输出收到的评论内容与回复内容"""
    username = cmt['member']['uname']
    rpid = cmt['rpid']
    original_message = cmt.get('content', {}).get('message', '')

    try:
        await comment.send_comment(
            oid=oid,
            type_=comment.CommentResourceType.VIDEO,
            text=reply_message,
            root=rpid,
            credential=credential
        )

        # 标记为已回复（保留原有调用）
        reply_record_manager.mark_replied(bvid, rpid, username=username, message=original_message)

        # 更详细的日志：显示收到的评论与回复的完整内容
        logger.info(f"✓ 成功回复 @{username} (rpid: {rpid})")
        logger.info(f"收到评论: {original_message}")
        logger.info(f"回复内容: {reply_message}")

        return True, None

    except Exception as e:
        error_msg = str(e)
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
        v = video.Video(bvid=bvid)
        video_info = await v.get_info()
    except Exception as e:
        logger.error(f"获取视频信息失败 {bvid}: {e}")
        return {'success': 0, 'failed': 0, 'total': 0}

    oid = video_info["aid"]
    video_title = video_info.get('title', '')
    video_desc = video_info.get('desc', '')
    video_url = f"https://www.bilibili.com/video/{bvid}"
    credential = CredentialManager().get_credential()

    new_comments = await get_new_comments(bvid)

    if not new_comments:
        # 将无新评论的常规状态改为 DEBUG，避免日志被大量占用
        logger.debug(f"视频标题: {video_title}({bvid}) - 没有新评论(已回复: {reply_record_manager.get_replied_count(bvid)})")
        return {'success': 0, 'failed': 0, 'total': 0}

    logger.info(f"视频标题: {video_title}({bvid}) - 发现 {len(new_comments)} 条新评论")

    success_count = 0
    failed_count = 0

    for idx, cmt in enumerate(new_comments, 1):
        member_info = cmt.get('member', {})
        username = member_info.get('uname', '')
        message = cmt['content']['message']
        user_mid = member_info.get('mid')

        if use_ai:
            style_tag = f"[{ai_style or 'default'}]"
            logger.debug(f"[{idx}/{len(new_comments)}] {style_tag} 处理评论 @{username}: {message[:50]}{'...' if len(message) > 50 else ''}")
            reply_message = generate_humorous_reply(video_title, video_desc, video_url, username, message, ai_style)
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
                cmt['rpid'],
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
    try:
        v = video.Video(bvid=bvid)
        info = await v.get_info()
        ai_tag = "[AI]" if use_ai else "[模板]"
        style_info = f" ({ai_style})" if use_ai and ai_style else ""
        logger.info(f"{ai_tag}{style_info} 开始监控: {info['title']} ({bvid})")
    except Exception as e:
        logger.error(f"获取视频信息失败 {bvid}: {e}")
        return

    while True:
        try:
            # 每次循环前检查配置是否有变化
            ConfigManager().check_and_reload()

            await reply_to_new_comments(bvid, reply_template, use_ai, ai_style)
        except asyncio.CancelledError:
            logger.info(f"监控任务已取消: {bvid}")
            break
        except Exception as e:
            logger.error(f"{bvid} - 监控出错: {e}", exc_info=True)

        await asyncio.sleep(interval)
