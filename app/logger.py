# logger.py - 日志配置模块
import logging
import sys
import time
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler


class DailySizeRotatingFileHandler(TimedRotatingFileHandler):
    """按时间和大小轮转的日志handler"""
    
    def __init__(self, filename, when='h', interval=1, backupCount=0, encoding=None, delay=False, utc=False, maxBytes=0):
        super().__init__(filename, when, interval, backupCount, encoding, delay, utc)
        self.maxBytes = maxBytes

    def shouldRollover(self, record):
        # 先检查时间轮转条件（优先级更高）
        if super().shouldRollover(record):
            return True
        
        # 再检查文件大小
        if self.maxBytes > 0:
            if self.stream is None:
                self.stream = self._open()
            # 获取当前文件大小
            current_size = self.stream.tell()
            if current_size >= self.maxBytes:
                return True
        
        return False

    def doRollover(self):
        """重写轮转逻辑，实现app-YYYY-MM-DD.N.log格式"""
        # 关闭当前流（如果打开）
        if self.stream:
            self.stream.close()
            self.stream = None

        # 默认使用当前时间（用于 computeRollover 的调用），保证变量在所有分支都已定义
        currentTime = int(time.time())

        # 获取用于命名被轮转文件的时间点
        # 使用 TimedRotatingFileHandler 内部的 rolloverAt 和 interval
        # 来精确定位被轮转周期的结束时间（即上一周期的结束），
        # 避免在同一天内多次轮转仍然使用同一日期，导致产生大量编号文件。
        try:
            # rolloverAt 是下次轮转的时间戳，上一周期结束时间为 rolloverAt - interval
            rollover_at = int(self.rolloverAt - self.interval)
            time_tuple = time.localtime(rollover_at)
        except Exception:
            # 回退到当前时间的前一天（兼容旧逻辑）
            time_tuple = time.localtime(currentTime - 24 * 60 * 60)

        date_str = time.strftime('%Y-%m-%d', time_tuple)

        # 获取日志文件目录
        log_dir = Path(self.baseFilename).parent

        # 生成备份文件名
        base_backup = f'bilibili_reply_comment-{date_str}.log'
        dfn = log_dir / base_backup

        # 如果存在，添加序号
        if dfn.exists():
            num = 1
            while True:
                dfn = log_dir / f'bilibili_reply_comment-{date_str}.{num}.log'
                if not dfn.exists():
                    break
                num += 1

        # 重命名当前文件
        if Path(self.baseFilename).exists():
            Path(self.baseFilename).rename(dfn)

        # 打开新文件
        if not self.delay:
            self.stream = self._open()

        # 更新下一次轮转时间（使用已确定的 currentTime）
        try:
            self.computeRollover(currentTime)
        except Exception:
            # 如果 computeRollover 出现异常，至少不要让轮转状态保持不变，记录到控制台方便排查
            try:
                logging.getLogger('bilibili_auto_reply').warning('日志 computeRollover 失败，继续运行')
            except Exception:
                pass


def setup_logger(log_level: str = "INFO", log_file: str = None):
    """配置日志系统"""
    logger = logging.getLogger('bilibili_auto_reply')

    # 如果已经有handler，先清除（避免重复添加）
    if logger.handlers:
        logger.handlers.clear()

    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    formatter = logging.Formatter(
        '%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        # 使用单一日志文件（不轮转），直接追加，避免生成大量备份文件
        file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # Web 控制台实时日志
    try:
        from .log_buffer import MemoryLogHandler, log_buffer
        memory_handler = MemoryLogHandler(log_buffer)
        memory_handler.setFormatter(formatter)
        logger.addHandler(memory_handler)
    except Exception:
        pass

    return logger


# 初始创建一个默认logger
logger = setup_logger()