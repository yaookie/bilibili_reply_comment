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

            new_replies = [
                r for r in replies
                if not reply_record_manager.is_replied(bvid, str(r['rpid']))
            ]
            comments.extend(new_replies)

            is_end = cursor.get("is_end", True)
            if is_end:
                break

            page += 1

        except Exception as e:
            logger.error(f"获取评论失败 {bvid}: {e}")
            break

    return comments


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

    for attempt in range(max_retries):
        result = []
        api_total = None
        try:
            u = user.User(uid=int(uid))
            pn = 1
            if full_sync:
                max_pages = 200
            elif stop_at_known is not None:
                max_pages = 50
            else:
                max_pages = 1

            hit_known = False
            incomplete = False
            while pn <= max_pages:
                videos = await u.get_videos(pn=pn, ps=page_size)
                page_info = videos.get('page') or {}
                if api_total is None and page_info.get('count') is not None:
                    try:
                        api_total = int(page_info.get('count'))
                    except (TypeError, ValueError):
                        api_total = None

                vlist = videos.get('list', {}).get('vlist', [])
                if not vlist:
                    break

                for v in vlist:
                    bvid = v['bvid']
                    if stop_at_known is not None and bvid in known:
                        hit_known = True
                        break

                    result.append({
                        'bvid': bvid,
                        'title': v['title'],
                    })
                    if not full_sync and stop_at_known is None and len(result) >= count:
                        break

                if hit_known:
                    break
                if not full_sync and stop_at_known is None:
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
                else:
                    if len(vlist) < page_size:
                        break

                pn += 1
                if pn > max_pages:
                    incomplete = True
                    logger.warning(f"已达翻页上限 {max_pages}，全量同步可能不完整")
                    break
                await asyncio.sleep(1.0)

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

            logger.info(f"获取到UP主 {uid} 的 {len(result)} 个投稿（{mode}）")
            return {"videos": result, "complete": complete, "api_total": api_total}

        except Exception as e:
            error_msg = str(e)
            if result:
                best_partial = list(result)
                best_total = api_total

            if '412' in error_msg:
                if attempt < max_retries - 1:
                    wait_time = retry_delay * (attempt + 1)
                    logger.warning(f"触发B站风控(412)，{wait_time}秒后重试 ({attempt + 1}/{max_retries})...")
                    await asyncio.sleep(wait_time)
                    continue
                logger.error(
                    f"触发B站风控(412)，已重试{max_retries}次仍失败。"
                    f"将先保存已获取的 {len(best_partial)} 条，并标记全量未完成以便下次继续。"
                )
                return {"videos": best_partial, "complete": False, "api_total": best_total}

            logger.error(f"获取UP主 {uid} 视频失败: {e}")
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

    config_manager = ConfigManager()
    app_config = config_manager.get_app_config()
    default_ai_style = app_config.get('default_ai_style', 'humorous')
    uid = str(uploader_uid)

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
        fetch = await get_uploader_videos(uploader_uid, full_sync=True)
    else:
        known = {
            item['bvid']
            for item in database_manager.list_discovered_videos(uid=uid)
        }
        for v in monitored_video_manager.list_videos(enabled_only=False):
            known.add(v['bvid'])

        logger.info(f"执行增量同步（已知 {len(known)} 个，遇已知 bvid 即停止翻页）")
        fetch = await get_uploader_videos(
            uploader_uid, full_sync=False, stop_at_known=known
        )

    latest_videos = fetch.get('videos') or []
    sync_complete = bool(fetch.get('complete'))
    api_total = fetch.get('api_total')

    if not latest_videos:
        if full_sync:
            database_manager.set_full_sync_done(uid, False)
            logger.warning("全量同步未获取到视频，已标记未完成，下次继续全量重试")
        else:
            logger.debug("未获取到新视频列表（可能没有更新）")
        return []

    new_added = []

    for vid in latest_videos:
        bvid = vid['bvid']
        title = vid['title']

        if video_discovery_manager.is_discovered(uploader_uid, bvid):
            if not monitored_video_manager.is_monitored(bvid):
                if config_manager.add_video_to_config(
                    bvid, use_ai=True, ai_style=default_ai_style, title=title
                ):
                    new_added.append(bvid)
                    logger.info(f"✓ 已补入监控: {title} ({bvid}) [风格: {default_ai_style}]")
            else:
                logger.debug(f"视频已发现: {title} ({bvid})")
            continue

        video_discovery_manager.mark_discovered(uid=uploader_uid, bvid=bvid, title=title)

        if config_manager.add_video_to_config(bvid, use_ai=True, ai_style=default_ai_style, title=title):
            new_added.append(bvid)
            logger.info(f"✓ 新视频已添加监控: {title} ({bvid}) [风格: {default_ai_style}]")

    if full_sync:
        monitored_after = monitored_video_manager.count(enabled_only=False)
        aligned = True
        if api_total is not None and monitored_after < max(api_total - 2, 0):
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
                str(cmt['rpid']),
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
            # 配置热加载由发现循环/低频检查即可，避免上百个监控任务同时刷磁盘
            await reply_to_new_comments(bvid, reply_template, use_ai, ai_style)
        except asyncio.CancelledError:
            logger.info(f"监控任务已取消: {bvid}")
            break
        except Exception as e:
            logger.error(f"{bvid} - 监控出错: {e}", exc_info=True)

        await asyncio.sleep(interval)
