# database.py - 数据库管理模块
import sqlite3
import json
from pathlib import Path
from datetime import datetime
from .logger import logger


class DatabaseManager:
    """数据库管理器 - 单例模式"""

    _instance = None
    _db_path = None
    _conn = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def initialize(self, db_path: str = None):
        """初始化数据库管理器"""
        if db_path:
            self._db_path = Path(db_path)
        else:
            try:
                from .config import ConfigManager
                record_dir = ConfigManager().get_app_config().get('record_dir', 'data')
            except Exception:
                record_dir = 'data'
            self._db_path = Path(__file__).parent.parent / record_dir / "bilibili_reply.db"

        # 确保数据库目录存在
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._create_tables()
        logger.info(f"数据库初始化完成: {self._db_path}")

    def _get_connection(self):
        """获取数据库连接"""
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")  # 启用WAL模式提高并发性能
        return self._conn

    def _create_tables(self):
        """创建数据库表"""
        conn = self._get_connection()
        cursor = conn.cursor()

        # 创建已回复评论表
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS replied_comments
                       (   id INTEGER PRIMARY KEY AUTOINCREMENT,
                           bvid TEXT NOT NULL,
                           rpid TEXT NOT NULL,
                           username TEXT,
                           message TEXT,
                           mid TEXT,
                           user_comment_time TIMESTAMP,
                           ai_reply TEXT,
                           replied_at TIMESTAMP,
                           UNIQUE ( bvid, rpid))
                       ''')

        # 检查并添加缺失的列
        cursor.execute("PRAGMA table_info(replied_comments)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'mid' not in columns:
            try:
                cursor.execute("ALTER TABLE replied_comments ADD COLUMN mid TEXT")
                logger.info("成功添加 mid 字段到 replied_comments 表")
            except Exception as e:
                logger.error(f"添加 mid 字段失败: {e}")

        if 'user_comment_time' not in columns:
            try:
                cursor.execute("ALTER TABLE replied_comments ADD COLUMN user_comment_time TIMESTAMP")
                logger.info("成功添加 user_comment_time 字段到 replied_comments 表")
            except Exception as e:
                logger.error(f"添加 user_comment_time 字段失败: {e}")

        if 'ai_reply' not in columns:
            try:
                cursor.execute("ALTER TABLE replied_comments ADD COLUMN ai_reply TEXT")
                logger.info("成功添加 ai_reply 字段到 replied_comments 表")
            except Exception as e:
                logger.error(f"添加 ai_reply 字段失败: {e}")

        if 'replied_at' not in columns:
            try:
                cursor.execute("ALTER TABLE replied_comments ADD COLUMN replied_at TIMESTAMP")
                logger.info("成功添加 replied_at 字段到 replied_comments 表")
            except Exception as e:
                logger.error(f"添加 replied_at 字段失败: {e}")

        # 创建已发现视频表
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS discovered_videos
                       (   id INTEGER PRIMARY KEY AUTOINCREMENT,
                           uid TEXT NOT NULL,
                           bvid TEXT NOT NULL,
                           title TEXT,
                           discovered_at TIMESTAMP,
                           UNIQUE (uid, bvid))
                       ''')

        # 检查并添加缺失的列
        cursor.execute("PRAGMA table_info(discovered_videos)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'discovered_at' not in columns:
            try:
                cursor.execute("ALTER TABLE discovered_videos ADD COLUMN discovered_at TIMESTAMP")
                logger.info("成功添加 discovered_at 字段到 discovered_videos 表")
            except Exception as e:
                logger.error(f"添加 discovered_at 字段失败: {e}")

        # 监控视频表（替代 config.yaml 中臃肿的 videos 列表）
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS monitored_videos
                       (
                           bvid       TEXT PRIMARY KEY,
                           title      TEXT,
                           template   TEXT,
                           interval   INTEGER,
                           use_ai     INTEGER DEFAULT 1,
                           ai_style   TEXT,
                           enabled    INTEGER DEFAULT 1,
                           created_at TIMESTAMP,
                           updated_at TIMESTAMP
                       )
                       ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_monitored_videos_enabled ON monitored_videos(enabled)'
        )

        # 键值元数据（如全量同步是否完成）
        cursor.execute('''
                       CREATE TABLE IF NOT EXISTS app_meta
                       (
                           key   TEXT PRIMARY KEY,
                           value TEXT,
                           updated_at TIMESTAMP
                       )
                       ''')

        # 创建索引以提高查询性能
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_replied_comments_bvid ON replied_comments(bvid)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_replied_comments_rpid ON replied_comments(rpid)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_discovered_videos_uid ON discovered_videos(uid)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_discovered_videos_bvid ON discovered_videos(bvid)')

        conn.commit()
        cursor.close()

    def close(self):
        """关闭数据库连接"""
        if self._conn:
            self._conn.close()
            self._conn = None

    # 回复记录相关方法
    def is_comment_replied(self, bvid: str, rpid: str) -> bool:
        """检查评论是否已回复"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT 1 FROM replied_comments WHERE bvid = ? AND rpid = ?',
            (bvid, str(rpid))
        )
        result = cursor.fetchone()
        cursor.close()
        return result is not None

    def mark_comment_replied(self, bvid: str, rpid: str, username: str = None, message: str = None, mid=None, user_comment_time=None, ai_reply=None, replied_at=None):
        """标记评论为已回复"""
        if replied_at is None:
            replied_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            rpid = str(rpid)
            logger.debug(f"标记评论已回复: BVID={bvid}, RPID={rpid}, 用户={username}")
            cursor.execute('''
                INSERT OR REPLACE INTO replied_comments (bvid, rpid, username, message, mid, user_comment_time, ai_reply, replied_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (bvid, rpid, username, message, mid, user_comment_time, ai_reply, replied_at))
            conn.commit()
            logger.debug(f"评论回复标记成功: {bvid}/{rpid}")
        except Exception as e:
            logger.error(f"标记评论回复失败: {e}", exc_info=True)
        finally:
            cursor.close()

    def get_replied_count(self, bvid: str) -> int:
        """获取视频的已回复评论数量"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM replied_comments WHERE bvid = ?', (bvid,))
        count = cursor.fetchone()[0]
        cursor.close()
        return count

    def get_total_replied_count(self) -> int:
        """获取全部已回复评论数量"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM replied_comments')
        count = cursor.fetchone()[0]
        cursor.close()
        return count

    def list_recent_replies(self, limit: int = 50, bvid: str = None) -> list:
        """最近回复记录（新到旧）"""
        conn = self._get_connection()
        cursor = conn.cursor()
        limit = max(1, min(int(limit or 50), 200))
        if bvid:
            cursor.execute(
                '''SELECT bvid, rpid, username, message, mid, user_comment_time, ai_reply, replied_at
                   FROM replied_comments WHERE bvid = ?
                   ORDER BY replied_at DESC, id DESC LIMIT ?''',
                (bvid, limit),
            )
        else:
            cursor.execute(
                '''SELECT bvid, rpid, username, message, mid, user_comment_time, ai_reply, replied_at
                   FROM replied_comments
                   ORDER BY replied_at DESC, id DESC LIMIT ?''',
                (limit,),
            )
        rows = cursor.fetchall()
        cursor.close()
        return [
            {
                'bvid': row[0],
                'rpid': row[1],
                'username': row[2],
                'message': row[3],
                'mid': row[4],
                'user_comment_time': row[5],
                'ai_reply': row[6],
                'replied_at': row[7],
            }
            for row in rows
        ]

    # 视频发现相关方法
    def is_video_discovered(self, uid: str, bvid: str) -> bool:
        """检查视频是否已被发现"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM discovered_videos WHERE uid = ? AND bvid = ?', (str(uid), bvid))
        result = cursor.fetchone()
        cursor.close()
        return result is not None

    def mark_video_discovered(self, uid: str, bvid: str, title: str = None, discovered_at=None):
        """标记视频为已发现"""
        if discovered_at is None:
            discovered_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT OR IGNORE INTO discovered_videos (uid, bvid, title, discovered_at)
                VALUES (?, ?, ?, ?)
            ''', (str(uid), bvid, title, discovered_at))
            conn.commit()
        except Exception as e:
            logger.error(f"标记视频发现失败: {e}")
        finally:
            cursor.close()

    def get_discovered_count(self, uid: str) -> int:
        """获取UP主的已发现视频数量"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM discovered_videos WHERE uid = ?', (str(uid),))
        count = cursor.fetchone()[0]
        cursor.close()
        return count

    def list_discovered_videos(self, uid: str = None) -> list:
        """列出已发现视频（可用于回填监控列表）"""
        conn = self._get_connection()
        cursor = conn.cursor()
        if uid:
            cursor.execute(
                '''SELECT uid, bvid, title, discovered_at FROM discovered_videos
                   WHERE uid = ? ORDER BY discovered_at ASC''',
                (str(uid),)
            )
        else:
            cursor.execute(
                '''SELECT uid, bvid, title, discovered_at FROM discovered_videos
                   ORDER BY discovered_at ASC'''
            )
        rows = cursor.fetchall()
        cursor.close()
        return [
            {'uid': uid_, 'bvid': bvid, 'title': title, 'discovered_at': discovered_at}
            for uid_, bvid, title, discovered_at in rows
        ]

    # ---------- 监控视频 ----------
    def list_monitored_videos(self, enabled_only: bool = True) -> list:
        """获取监控视频列表"""
        conn = self._get_connection()
        cursor = conn.cursor()
        if enabled_only:
            cursor.execute(
                '''SELECT bvid, title, template, interval, use_ai, ai_style, enabled
                   FROM monitored_videos WHERE enabled = 1 ORDER BY created_at ASC, bvid ASC'''
            )
        else:
            cursor.execute(
                '''SELECT bvid, title, template, interval, use_ai, ai_style, enabled
                   FROM monitored_videos ORDER BY created_at ASC, bvid ASC'''
            )
        rows = cursor.fetchall()
        cursor.close()
        result = []
        for bvid, title, template, interval, use_ai, ai_style, enabled in rows:
            result.append({
                'bvid': bvid,
                'title': title,
                'template': template,
                'interval': interval,
                'use_ai': bool(use_ai),
                'ai_style': ai_style,
                'enabled': bool(enabled),
            })
        return result

    def get_monitored_video(self, bvid: str) -> dict | None:
        """按 BV 号获取监控视频"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT bvid, title, template, interval, use_ai, ai_style, enabled
               FROM monitored_videos WHERE bvid = ?''',
            (bvid,)
        )
        row = cursor.fetchone()
        cursor.close()
        if not row:
            return None
        bvid, title, template, interval, use_ai, ai_style, enabled = row
        return {
            'bvid': bvid,
            'title': title,
            'template': template,
            'interval': interval,
            'use_ai': bool(use_ai),
            'ai_style': ai_style,
            'enabled': bool(enabled),
        }

    def is_video_monitored(self, bvid: str) -> bool:
        """检查视频是否已在监控列表"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT 1 FROM monitored_videos WHERE bvid = ?', (bvid,))
        result = cursor.fetchone()
        cursor.close()
        return result is not None

    def upsert_monitored_video(self, bvid: str, title: str = None, template: str = None,
                               interval: int = None, use_ai: bool = True,
                               ai_style: str = None, enabled: bool = True) -> bool:
        """新增或更新监控视频。返回 True 表示新插入，False 表示已存在（已更新）。"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        exists = self.is_video_monitored(bvid)
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            if exists:
                cursor.execute(
                    '''UPDATE monitored_videos
                       SET title = COALESCE(?, title),
                           template = COALESCE(?, template),
                           interval = COALESCE(?, interval),
                           use_ai = ?,
                           ai_style = COALESCE(?, ai_style),
                           enabled = ?,
                           updated_at = ?
                       WHERE bvid = ?''',
                    (
                        title,
                        template,
                        interval,
                        1 if use_ai else 0,
                        ai_style,
                        1 if enabled else 0,
                        now,
                        bvid,
                    )
                )
            else:
                cursor.execute(
                    '''INSERT INTO monitored_videos
                       (bvid, title, template, interval, use_ai, ai_style, enabled, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (
                        bvid,
                        title,
                        template,
                        interval,
                        1 if use_ai else 0,
                        ai_style,
                        1 if enabled else 0,
                        now,
                        now,
                    )
                )
            conn.commit()
            return not exists
        except Exception as e:
            logger.error(f"写入监控视频失败 {bvid}: {e}", exc_info=True)
            return False
        finally:
            cursor.close()

    def get_monitored_count(self, enabled_only: bool = True) -> int:
        """获取监控视频数量"""
        conn = self._get_connection()
        cursor = conn.cursor()
        if enabled_only:
            cursor.execute('SELECT COUNT(*) FROM monitored_videos WHERE enabled = 1')
        else:
            cursor.execute('SELECT COUNT(*) FROM monitored_videos')
        count = cursor.fetchone()[0]
        cursor.close()
        return count

    def set_monitored_enabled(self, bvid: str, enabled: bool) -> bool:
        """启用/停用监控视频"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE monitored_videos SET enabled = ?, updated_at = ? WHERE bvid = ?',
            (1 if enabled else 0, now, bvid),
        )
        changed = cursor.rowcount > 0
        conn.commit()
        cursor.close()
        return changed

    def delete_monitored_video(self, bvid: str) -> bool:
        """从监控列表删除视频"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM monitored_videos WHERE bvid = ?', (bvid,))
        changed = cursor.rowcount > 0
        conn.commit()
        cursor.close()
        return changed

    # ---------- 元数据 ----------
    def get_meta(self, key: str, default: str = None) -> str | None:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM app_meta WHERE key = ?', (key,))
        row = cursor.fetchone()
        cursor.close()
        return row[0] if row else default

    def set_meta(self, key: str, value: str):
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO app_meta (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at''',
            (key, value, now)
        )
        conn.commit()
        cursor.close()

    def is_full_sync_done(self, uid: str) -> bool:
        return self.get_meta(f'full_sync_done:{uid}', '0') == '1'

    def set_full_sync_done(self, uid: str, done: bool = True):
        self.set_meta(f'full_sync_done:{uid}', '1' if done else '0')


database_manager = DatabaseManager()


def migrate_json_to_sqlite():
    """迁移JSON文件数据到SQLite数据库"""
    logger.info("开始迁移JSON数据到SQLite数据库...")

    # 迁移回复记录
    replied_file = Path("data/replied_comments.json")
    if replied_file.exists():
        try:
            with open(replied_file, 'r', encoding='utf-8') as f:
                old_replied_data = json.load(f)

            migrated_count = 0
            for bvid, rpids in old_replied_data.items():
                for rpid in rpids:
                    # 由于旧数据没有username和message，我们只插入bvid和rpid
                    database_manager.mark_comment_replied(bvid, rpid)
                    migrated_count += 1

            logger.info(f"迁移了 {migrated_count} 条回复记录")

            # 备份并删除旧文件
            backup_file = replied_file.with_suffix('.json.backup')
            replied_file.rename(backup_file)
            logger.info(f"旧回复记录文件已备份为: {backup_file}")

        except Exception as e:
            logger.error(f"迁移回复记录失败: {e}")

    # 迁移视频发现记录
    discovered_file = Path("data/discovered_videos.json")
    if discovered_file.exists():
        try:
            with open(discovered_file, 'r', encoding='utf-8') as f:
                old_discovered_data = json.load(f)

            migrated_count = 0
            for uid, bvids in old_discovered_data.items():
                for bvid in bvids:
                    # 由于旧数据没有title，我们只插入uid和bvid
                    database_manager.mark_video_discovered(uid, bvid)
                    migrated_count += 1

            logger.info(f"迁移了 {migrated_count} 条视频发现记录")

            # 备份并删除旧文件
            backup_file = discovered_file.with_suffix('.json.backup')
            discovered_file.rename(backup_file)
            logger.info(f"旧视频发现记录文件已备份为: {backup_file}")

        except Exception as e:
            logger.error(f"迁移视频发现记录失败: {e}")

    logger.info("数据迁移完成")


class ReplyRecordManager:
    """回复记录管理器"""

    def __init__(self):
        self._initialized = False

    def initialize(self):
        """延迟初始化，确保DatabaseManager已就绪"""
        if self._initialized:
            return
        # DatabaseManager会在main中初始化，这里不需要额外操作
        self._initialized = True

    def is_replied(self, bvid, rpid):
        self.initialize()
        return database_manager.is_comment_replied(bvid, rpid)

    def mark_replied(self, bvid, rpid, username=None, message=None, mid=None, user_comment_time=None):
        self.initialize()
        database_manager.mark_comment_replied(bvid, rpid, username, message, mid, user_comment_time)

    def mark_replied_with_details(self, bvid, rpid, username=None, message=None, mid=None, user_comment_time=None, ai_reply=None):
        self.initialize()
        database_manager.mark_comment_replied(bvid, rpid, username, message, mid, user_comment_time, ai_reply)

    def get_replied_count(self, bvid):
        self.initialize()
        return database_manager.get_replied_count(bvid)

    def get_total_count(self):
        self.initialize()
        return database_manager.get_total_replied_count()

    def list_recent(self, limit: int = 50, bvid: str = None):
        self.initialize()
        return database_manager.list_recent_replies(limit=limit, bvid=bvid)


reply_record_manager = ReplyRecordManager()


class VideoDiscoveryManager:
    """视频发现管理器 - 自动获取UP主最新视频"""

    def __init__(self):
        self._initialized = False

    def initialize(self):
        """延迟初始化，确保DatabaseManager已就绪"""
        if self._initialized:
            return
        # DatabaseManager会在main中初始化，这里不需要额外操作
        self._initialized = True

    def is_discovered(self, uid: str, bvid: str) -> bool:
        """检查视频是否已被发现"""
        self.initialize()
        return database_manager.is_video_discovered(uid, bvid)

    def mark_discovered(self, uid: str, bvid: str, title: str = None):
        """标记视频为已发现"""
        self.initialize()
        database_manager.mark_video_discovered(uid, bvid, title)

    def get_discovered_count(self, uid: str) -> int:
        """获取已发现的视频数量"""
        self.initialize()
        return database_manager.get_discovered_count(uid)


video_discovery_manager = VideoDiscoveryManager()


class MonitoredVideoManager:
    """监控视频管理器 - 视频列表持久化到 SQLite，避免 config.yaml 膨胀"""

    def __init__(self):
        self._initialized = False

    def initialize(self):
        if self._initialized:
            return
        # 确保底层数据库已就绪
        if database_manager._db_path is None:
            database_manager.initialize()
        self._initialized = True

    def list_videos(self, enabled_only: bool = True) -> list:
        self.initialize()
        return database_manager.list_monitored_videos(enabled_only=enabled_only)

    def get_video(self, bvid: str):
        self.initialize()
        return database_manager.get_monitored_video(bvid)

    def is_monitored(self, bvid: str) -> bool:
        self.initialize()
        return database_manager.is_video_monitored(bvid)

    def add_or_update(self, bvid: str, title: str = None, template: str = None,
                      interval: int = None, use_ai: bool = True,
                      ai_style: str = None, enabled: bool = True) -> bool:
        """添加监控视频。返回 True 表示新插入。"""
        self.initialize()
        return database_manager.upsert_monitored_video(
            bvid=bvid,
            title=title,
            template=template,
            interval=interval,
            use_ai=use_ai,
            ai_style=ai_style,
            enabled=enabled,
        )

    def count(self, enabled_only: bool = True) -> int:
        self.initialize()
        return database_manager.get_monitored_count(enabled_only=enabled_only)

    def set_enabled(self, bvid: str, enabled: bool) -> bool:
        self.initialize()
        return database_manager.set_monitored_enabled(bvid, enabled)

    def delete(self, bvid: str) -> bool:
        self.initialize()
        return database_manager.delete_monitored_video(bvid)

    def backfill_from_discovered(self, template: str = None, interval: int = None,
                                 use_ai: bool = True, ai_style: str = None,
                                 uid: str = None) -> int:
        """当 monitored_videos 为空时，用 discovered_videos 回填。返回新增条数。"""
        self.initialize()
        if self.count(enabled_only=False) > 0:
            return 0

        discovered = database_manager.list_discovered_videos(uid=uid)
        if not discovered:
            return 0

        # 同一 bvid 可能对应多个 uid 记录，按 bvid 去重
        seen = set()
        added = 0
        for item in discovered:
            bvid = item.get('bvid')
            if not bvid or bvid in seen:
                continue
            seen.add(bvid)
            inserted = self.add_or_update(
                bvid=bvid,
                title=item.get('title'),
                template=template,
                interval=interval,
                use_ai=use_ai,
                ai_style=ai_style,
                enabled=True,
            )
            if inserted:
                added += 1
        return added


monitored_video_manager = MonitoredVideoManager()
