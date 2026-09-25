# main.py - 主程序模块
import asyncio
import signal
import sys
from pathlib import Path
from .config import ConfigManager
from .database import database_manager, reply_record_manager, video_discovery_manager
from .video_monitor import discover_and_add_new_videos, monitor_single_video
from .logger import logger, setup_logger


class AutoReplyService:
    """自动回复服务"""

    def __init__(self):
        self.running = False
        self.tasks = []
        self.uploader_uid = None
        self.video_discovery_interval = 300  # 默认5分钟检查一次新视频
        self._task_cleanup_interval = 3600  # 每小时清理一次已完成的任务

    def start(self):
        """启动服务"""
        self.running = True
        logger.info("=" * 60)
        logger.info("多视频自动回复监控系统启动")
        logger.info("=" * 60)

        videos_config = ConfigManager().get_videos_config()
        app_config = ConfigManager().get_app_config()
        default_interval = app_config.get('default_check_interval', 60)

        # 获取UP主UID配置（可选）
        self.uploader_uid = app_config.get('uploader_uid')
        self.video_discovery_interval = app_config.get('video_discovery_interval', 300)

        # 确保 videos_config 是列表
        if not videos_config:
            logger.warning("配置文件中没有视频配置，将仅使用自动发现功能（如果配置了 uploader_uid）")
            videos_config = []

        tasks = []
        for config in videos_config:
            if isinstance(config, str):
                bvid = config
                template = "@{username} 感谢你的评论！"
                interval = default_interval
                use_ai = False
                ai_style = None
            elif isinstance(config, dict):
                bvid = config['bvid']
                template = config.get('template', "@{username} 感谢你的评论！")
                interval = config.get('interval', default_interval)
                use_ai = config.get('use_ai', False)
                ai_style = config.get('ai_style', app_config.get('default_ai_style', 'humorous') if use_ai else None)
            else:
                logger.warning(f"跳过无效的视频配置: {config}")
                continue

            task = asyncio.create_task(monitor_single_video(bvid, template, interval, use_ai, ai_style))
            tasks.append(task)

        self.tasks = tasks
        logger.info(f"\n共监控 {len(tasks)} 个视频\n")

        # 如果有UP主UID，启动视频发现任务
        if self.uploader_uid:
            discovery_task = asyncio.create_task(self._video_discovery_loop())
            tasks.append(discovery_task)
            logger.info(f"已启动视频自动发现功能（UP主UID: {self.uploader_uid}）\n")

        # 启动任务清理任务
        cleanup_task = asyncio.create_task(self._cleanup_completed_tasks())
        tasks.append(cleanup_task)

    async def _video_discovery_loop(self):
        """视频发现循环：首次延迟 60 秒，之后按配置间隔检查"""
        interval = max(60, int(self.video_discovery_interval or 300))
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
                logger.debug("执行视频发现检查")

                # 执行视频发现 - 启用自动保存到配置文件
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
                # 从配置中获取该视频的配置（已经由 discover_and_add_new_videos 添加）
                videos_config = ConfigManager().get_videos_config()
                video_config = None

                for vc in videos_config:
                    if isinstance(vc, dict) and vc.get('bvid') == bvid:
                        video_config = vc
                        break

                # 提取配置，如果找不到则使用默认值
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

                # 创建新的监控任务
                task = asyncio.create_task(
                    monitor_single_video(bvid, template, interval, use_ai, ai_style)
                )
                self.tasks.append(task)

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

    # 迁移JSON数据到SQLite（如果存在）
    # migrate_json_to_sqlite()

    # 初始化各个管理器
    reply_record_manager.initialize()
    video_discovery_manager.initialize()

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
