# main.py - 主程序模块
import asyncio
import signal
import sys
from pathlib import Path
from .config import ConfigManager
from .database import database_manager, reply_record_manager, video_discovery_manager, monitored_video_manager
from .video_monitor import discover_and_add_new_videos, monitor_single_video
from .logger import logger, setup_logger


class AutoReplyService:
    """自动回复服务"""

    def __init__(self):
        self.running = False
        self.tasks = []
        self._monitored_bvids = set()  # 防止同一视频重复创建监控任务
        self.uploader_uid = None
        self.video_discovery_interval = 3600  # 默认1小时检查一次新视频
        self._task_cleanup_interval = 3600  # 每小时清理一次已完成的任务
        self._last_config_check = 0.0

    def start(self):
        """启动服务：从数据库加载监控视频并创建任务"""
        self.running = True
        logger.info("=" * 60)
        logger.info("多视频自动回复监控系统启动")
        logger.info("=" * 60)

        app_config = ConfigManager().get_app_config()
        if ConfigManager().get_video_date_ranges():
            logger.info(
                f"日期过滤对库中已有监控视频同样生效（不回复范围外稿件）："
                f"{ConfigManager().describe_video_date_filter()}"
            )
        default_interval = app_config.get('default_check_interval', 60)
        default_ai_style = app_config.get('default_ai_style', 'humorous')
        default_template = "@{username} 感谢你的评论！"

        # 获取UP主UID配置（可选）
        self.uploader_uid = app_config.get('uploader_uid')
        self.video_discovery_interval = app_config.get('video_discovery_interval', 3600)

        # 监控列表只从数据库读取（不再依赖 config.yaml 的 videos）
        if monitored_video_manager.count(enabled_only=False) == 0:
            backfilled = monitored_video_manager.backfill_from_discovered(
                template=default_template,
                interval=default_interval,
                use_ai=True,
                ai_style=default_ai_style,
                uid=str(self.uploader_uid) if self.uploader_uid else None,
            )
            if backfilled:
                logger.info(f"monitored_videos 为空，已从 discovered_videos 回填 {backfilled} 个视频")

        videos_config = monitored_video_manager.list_videos(enabled_only=True)
        logger.info(
            f"从数据库加载监控视频: {len(videos_config)} 个 "
            f"(db={database_manager._db_path})"
        )

        if not videos_config:
            logger.warning(
                "数据库中没有启用的监控视频。"
                "可配置 uploader_uid 自动发现，或在 config.yaml 的 videos 写入种子后重启导入。"
            )

        tasks = []
        for config in videos_config:
            if isinstance(config, str):
                bvid = config
                template = default_template
                interval = default_interval
                use_ai = False
                ai_style = None
            elif isinstance(config, dict):
                bvid = config.get('bvid')
                if not bvid:
                    logger.warning(f"跳过无效的视频配置: {config}")
                    continue
                template = config.get('template') or default_template
                interval = config.get('interval') or default_interval
                use_ai = bool(config.get('use_ai', False))
                ai_style = config.get('ai_style')
                if use_ai and not ai_style:
                    ai_style = default_ai_style
            else:
                logger.warning(f"跳过无效的视频配置: {config}")
                continue

            if bvid in self._monitored_bvids:
                continue

            task = asyncio.create_task(
                monitor_single_video(bvid, template, interval, use_ai, ai_style)
            )
            tasks.append(task)
            self._monitored_bvids.add(bvid)

        self.tasks = tasks
        logger.info(f"共监控 {len(tasks)} 个视频")

        # 如果有UP主UID，启动视频发现任务
        if self.uploader_uid:
            discovery_task = asyncio.create_task(self._video_discovery_loop())
            tasks.append(discovery_task)
            logger.info(f"已启动视频自动发现功能（UP主UID: {self.uploader_uid}）")

        # 启动任务清理任务
        cleanup_task = asyncio.create_task(self._cleanup_completed_tasks())
        tasks.append(cleanup_task)

    async def _video_discovery_loop(self):
        """视频发现循环：首次延迟 60 秒，之后按配置间隔检查"""
        interval = max(60, int(self.video_discovery_interval or 3600))
        logger.info(
            f"视频发现任务已启动（首次 60 秒后检查，之后每 {interval} 秒检查一次）"
        )

        # 首次启动时等待60秒,避免立即触发风控
        logger.info("等待60秒后开始首次视频发现检查...")
        await asyncio.sleep(60)
        logger.info("开始执行首次视频发现检查")

        consecutive_failures = 0
        max_consecutive_failures = 5

        while self.running:
            try:
                # 发现循环顺带做配置热加载（比每个视频任务都检查更轻量）
                ConfigManager().check_and_reload()

                logger.debug("执行视频发现检查")

                # 执行视频发现（写入数据库）
                new_videos = await discover_and_add_new_videos(
                    self.uploader_uid,
                    auto_save=True
                )

                # 如果有新视频,动态添加监控任务
                if new_videos:
                    logger.info(f"发现 {len(new_videos)} 个新视频，准备添加监控任务")
                    await self._add_monitor_tasks(new_videos)
                    consecutive_failures = 0
                else:
                    logger.debug("未发现新视频")

            except asyncio.CancelledError:
                logger.info("视频发现任务已取消")
                break
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"视频发现任务出错 ({consecutive_failures}/{max_consecutive_failures}): {e}",
                             exc_info=True)

                if consecutive_failures >= max_consecutive_failures:
                    logger.warning(f"连续失败{max_consecutive_failures}次,将等待更长时间...")
                    await asyncio.sleep(interval * 3)
                    consecutive_failures = 0
                else:
                    await asyncio.sleep(min(60, interval))
                continue

            # 运行中允许热更新发现间隔
            app_config = ConfigManager().get_app_config()
            interval = max(60, int(app_config.get('video_discovery_interval', interval) or interval))
            await asyncio.sleep(interval)

    async def _add_monitor_tasks(self, new_bvids):
        """动态添加新的监控任务"""
        app_config = ConfigManager().get_app_config()
        default_interval = app_config.get('default_check_interval', 60)
        default_ai_style = app_config.get('default_ai_style', 'humorous')

        for bvid in new_bvids:
            try:
                if bvid in self._monitored_bvids:
                    logger.debug(f"视频已在监控中，跳过: {bvid}")
                    continue

                video_config = monitored_video_manager.get_video(bvid)

                if video_config:
                    template = video_config.get('template', "@{username} 感谢你的评论！")
                    interval = video_config.get('interval', default_interval)
                    use_ai = video_config.get('use_ai', True)
                    ai_style = video_config.get('ai_style', default_ai_style)
                    title = video_config.get('title', '未知标题')
                else:
                    logger.warning(f"未找到视频 {bvid} 的配置，使用默认配置")
                    template = "@{username} 感谢你的评论！"
                    interval = default_interval
                    use_ai = True
                    ai_style = default_ai_style
                    title = '未知标题'

                task = asyncio.create_task(
                    monitor_single_video(bvid, template, interval, use_ai, ai_style)
                )
                self.tasks.append(task)
                self._monitored_bvids.add(bvid)

                logger.info(f"✓ 已开始监控新视频: {title} ({bvid}) [风格: {ai_style}]")
            except Exception as e:
                logger.error(f"添加监控任务失败 {bvid}: {e}", exc_info=True)

    async def _cleanup_completed_tasks(self):
        """定期清理已完成的任务，防止内存泄漏"""
        logger.info("任务清理任务已启动")

        while self.running:
            try:
                await asyncio.sleep(self._task_cleanup_interval)

                # 清理已完成的任务
                before_count = len(self.tasks)
                self.tasks = [t for t in self.tasks if not t.done()]
                after_count = len(self.tasks)

                if before_count != after_count:
                    logger.info(
                        f"任务清理: {before_count} -> {after_count} (清理了 {before_count - after_count} 个已完成任务)")
            except asyncio.CancelledError:
                logger.info("任务清理任务已取消")
                break
            except Exception as e:
                logger.error(f"任务清理出错: {e}", exc_info=True)

    async def run(self):
        """运行服务"""
        self.start()
        try:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        except asyncio.CancelledError:
            logger.info("服务被取消")

    def stop(self):
        """停止服务"""
        logger.info("\n正在停止服务...")
        self.running = False
        for task in self.tasks:
            if not task.done():
                task.cancel()
        logger.info("服务已停止")


service = AutoReplyService()


def handle_signal(signum, frame):
    """处理退出信号（Windows下Ctrl+C）"""
    logger.info(f"收到退出信号，准备停止服务...")
    service.stop()
    sys.exit(0)


async def main():
    """主函数"""
    config_manager = ConfigManager()
    config_manager.initialize()

    app_config = config_manager.get_app_config()
    log_level = app_config.get('log_level', 'INFO')
    log_dir = app_config.get('log_dir', 'logs')
    record_dir = app_config.get('record_dir', 'data')
    log_file = f"{log_dir}/bilibili_reply_comment.log"

    # 确保运行时目录存在
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    Path(record_dir).mkdir(parents=True, exist_ok=True)

    # 重新配置logger（会清除旧的handlers）
    global logger
    logger = setup_logger(log_level, log_file)

    # 初始化数据库管理器
    database_manager.initialize()

    # 初始化各个管理器
    reply_record_manager.initialize()
    video_discovery_manager.initialize()
    monitored_video_manager.initialize()

    # 启动时校验 B 站登录态，提前发现 Cookie 失效
    try:
        from .api_clients import CredentialManager
        cred = CredentialManager().get_credential()
        if await cred.check_refresh():
            logger.info("检测到 Cookie 可刷新，正在尝试 refresh…")
            try:
                await cred.refresh()
                logger.info("Cookie 刷新成功")
            except Exception as e:
                logger.warning(f"Cookie 自动刷新失败（将继续使用现有凭证）: {e}")
        valid = await cred.check_valid()
        if valid:
            logger.info("B站登录态校验通过")
        else:
            logger.error(
                "B站登录态无效（账号未登录）。"
                "发评论会失败(-101)。请重新从浏览器复制 Cookie 到 config.yaml 后重启。"
            )
    except Exception as e:
        logger.error(f"B站登录态校验失败: {e}")

    # 将 config.yaml 中残留的 videos 导入数据库并清空 YAML
    imported = config_manager.migrate_seed_videos_to_db()

    # 若监控表为空，尝试从 discovered_videos 回填
    app_defaults = config_manager.get_app_config()
    if monitored_video_manager.count(enabled_only=False) == 0:
        backfilled = monitored_video_manager.backfill_from_discovered(
            template="@{username} 感谢你的评论！",
            interval=app_defaults.get('default_check_interval', 60),
            use_ai=True,
            ai_style=app_defaults.get('default_ai_style', 'humorous'),
            uid=str(app_defaults['uploader_uid']) if app_defaults.get('uploader_uid') else None,
        )
        if backfilled:
            logger.info(f"已从 discovered_videos 回填监控视频 {backfilled} 个")

    # 空库、强制、或尚未完成过「完整全量同步」：启动前全量
    force_full = bool(app_config.get('video_full_sync_on_start', False))
    uploader_uid = app_config.get('uploader_uid')
    monitored_now = monitored_video_manager.count(enabled_only=False)
    need_full_sync = bool(uploader_uid) and (
        force_full
        or monitored_now == 0
        or not database_manager.is_full_sync_done(str(uploader_uid))
    )
    if need_full_sync:
        logger.info(
            f"准备全量同步 UP 主投稿（当前监控 {monitored_now} 个，"
            f"force={force_full}, full_sync_done="
            f"{database_manager.is_full_sync_done(str(uploader_uid))}）…"
        )
        try:
            synced = await discover_and_add_new_videos(
                str(uploader_uid),
                force_full_sync=True,
            )
            logger.info(f"启动前全量同步结束，本轮新增 {len(synced)} 个视频")
        except Exception as e:
            logger.error(f"启动前全量同步失败: {e}", exc_info=True)

    video_count = monitored_video_manager.count()
    if imported:
        logger.info(f"启动迁移完成：数据库中现有监控视频 {video_count} 个")
    else:
        logger.info(f"当前数据库监控视频数: {video_count} (来源: monitored_videos 表)")

    if video_count == 0 and not app_config.get('uploader_uid'):
        logger.warning(
            "数据库中无监控视频，且未配置 uploader_uid。"
            "可在 config.yaml 的 videos 中临时添加种子条目（启动后会自动导入），"
            "或设置 uploader_uid 启用自动发现。"
        )

    # Windows 下注册信号处理
    try:
        signal.signal(signal.SIGINT, handle_signal)
    except (ValueError, OSError):
        logger.warning("无法注册信号处理器")

    try:
        await service.run()
    except KeyboardInterrupt:
        logger.info("用户中断")
    except Exception as e:
        logger.error(f"服务异常: {e}", exc_info=True)
    finally:
        service.stop()
