# config.py - 配置管理模块
import yaml
import os
from pathlib import Path
from typing import List, Dict


class ConfigManager:
    """配置管理器 - 单例模式"""

    _instance = None
    _config = None
    _config_path = None
    _last_modified = 0

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def initialize(self, config_path: str = None):
        """初始化配置管理器"""
        if config_path:
            self._config_path = Path(config_path)
        else:
            self._config_path = Path(__file__).parent.parent / "config.yaml"
        self._load_config()

    def _load_config(self):
        """加载配置文件"""
        try:
            with open(self._config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f)

            # 确保加载的是字典
            if not isinstance(self._config, dict):
                raise RuntimeError(f"配置文件格式错误，期望字典类型，实际为: {type(self._config)}")

            self._apply_env_overrides()

            # 记录最后修改时间
            self._last_modified = self._config_path.stat().st_mtime

            # 确保必要字段存在并初始化
            if 'videos' not in self._config or self._config['videos'] is None:
                self._config['videos'] = []
            if 'app' not in self._config or self._config['app'] is None:
                self._config['app'] = {}
            if 'ai_reply_styles' not in self._config:
                self._config['ai_reply_styles'] = self._get_default_ai_styles()
            if 'bilibili' not in self._config or not isinstance(self._config.get('bilibili'), dict):
                self._config['bilibili'] = {'credential': {}}
            if 'credential' not in self._config['bilibili'] or self._config['bilibili']['credential'] is None:
                self._config['bilibili']['credential'] = {}
            if 'qwen' not in self._config or self._config['qwen'] is None:
                self._config['qwen'] = {}

            self._validate_config()

        except FileNotFoundError:
            raise RuntimeError(
                f"配置文件不存在: {self._config_path}\n"
                f"请复制 config.example.yaml 为 config.yaml 并填写凭证信息。"
            )
        except yaml.YAMLError as e:
            raise RuntimeError(f"配置文件 YAML 格式错误: {e}")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"加载配置文件失败: {e}")

    def _validate_config(self):
        """校验关键配置，启动时给出明确提示"""
        from .logger import logger

        cred = self._config.get('bilibili', {}).get('credential', {}) or {}
        required_cred = ['sessdata', 'bili_jct', 'buvid3', 'dedeuserid']
        missing_cred = [k for k in required_cred if not cred.get(k) or str(cred.get(k)).startswith('请填写')]
        if missing_cred:
            logger.warning(
                f"B站凭证未完整配置，缺少: {', '.join(missing_cred)}。"
                f"回复评论前请补全 config.yaml 或设置环境变量。"
            )

        qwen = self._config.get('qwen', {}) or {}
        if not qwen.get('api_key') or str(qwen.get('api_key', '')).startswith('请填写'):
            logger.warning("未配置千问 API Key，启用 use_ai 的视频将无法生成 AI 回复。")

        videos = self._config.get('videos') or []
        uploader_uid = self._config.get('app', {}).get('uploader_uid')
        if not videos and not uploader_uid:
            logger.warning(
                "未在 config.yaml 配置 videos 种子列表，也未设置 app.uploader_uid。"
                "若数据库中也无监控视频，程序将无可监控目标。"
            )

    def _get_default_ai_styles(self):
        """获取默认的AI回复风格配置"""
        return {
            'humorous': {
                'name': '幽默风趣',
                'prompt': '''你是一个B站UP主，擅长用幽默风趣的方式与观众互动。请根据以下信息，生成一条简短、幽默、有趣的回复。

视频信息：
- 标题：{video_title}
- 链接：{video_url}
- 简介：{video_desc}

评论内容：
- 用户：{comment_username}
- 评论：{comment_message}

要求：
1. 回复要幽默风趣，可以适当玩梗、调侃
2. 保持友好和尊重，不要冒犯用户
3. 回复简洁，控制在50字以内
4. 可以结合视频内容进行回应
5. 直接输出回复内容，不要加引号或其他说明

请生成回复：'''
            },
            'sharp': {
                'name': '毒舌犀利',
                'prompt': '''你是一个B站UP主，说话犀利、有个性，善于用毒舌但不失幽默的方式与观众互动。请根据以下信息，生成一条有个性的回复。

视频信息：
- 标题：{video_title}
- 链接：{video_url}
- 简介：{video_desc}

评论内容：
- 用户：{comment_username}
- 评论：{comment_message}

要求：
1. 回复要有个性，可以适当毒舌、吐槽，但要有趣
2. 毒舌不等于恶意攻击，要保持底线和尊重
3. 回复简洁有力，控制在50字以内
4. 可以结合视频内容进行犀利点评
5. 让人会心一笑，而不是感到被冒犯
6. 直接输出回复内容，不要加引号或其他说明

请生成回复：'''
            }
        }

    def _apply_env_overrides(self):
        """应用环境变量覆盖（Windows也支持）"""
        cred = self._config.setdefault('bilibili', {}).setdefault('credential', {})
        if not isinstance(cred, dict):
            self._config['bilibili']['credential'] = {}
            cred = self._config['bilibili']['credential']

        if os.getenv('BILIBILI_SESSDATA'):
            cred['sessdata'] = os.getenv('BILIBILI_SESSDATA')
        if os.getenv('BILIBILI_BILI_JCT'):
            cred['bili_jct'] = os.getenv('BILIBILI_BILI_JCT')
        if os.getenv('BILIBILI_BUVID3'):
            cred['buvid3'] = os.getenv('BILIBILI_BUVID3')
        if os.getenv('BILIBILI_DEDEUSERID'):
            cred['dedeuserid'] = os.getenv('BILIBILI_DEDEUSERID')
        if os.getenv('QWEN_API_KEY'):
            self._config.setdefault('qwen', {})['api_key'] = os.getenv('QWEN_API_KEY')

    def check_and_reload(self):
        """检查配置文件是否被修改，如果是则重新加载"""
        if self._config_path.exists():
            current_modified = self._config_path.stat().st_mtime
            if current_modified > self._last_modified:
                from .logger import logger
                logger.info("检测到配置文件变化，正在重新加载...")
                self._load_config()
                # 若 YAML 里临时写了 videos 种子，导入数据库
                imported = self.migrate_seed_videos_to_db()
                logger.info(f"配置已重新加载（本次导入种子视频: {imported}）")
                return True
        return False

    def reload(self):
        """强制重新加载配置"""
        self._load_config()
        from .logger import logger
        logger.info("配置已重新加载")

    @property
    def config(self):
        if self._config is None:
            self.initialize()
        return self._config

    def get_bilibili_credential(self) -> Dict:
        return self.config['bilibili']['credential']

    def get_qwen_config(self) -> Dict:
        return self.config.get('qwen', {})

    def get_videos_config(self) -> List[Dict]:
        """获取监控视频列表（来自 SQLite，不再依赖 YAML 长列表）"""
        from .database import monitored_video_manager
        return monitored_video_manager.list_videos(enabled_only=True)

    def get_app_config(self) -> Dict:
        return self.config.get('app', {})

    def get_ai_styles_config(self) -> Dict:
        """获取AI回复风格配置"""
        return self.config.get('ai_reply_styles', {})

    def get_ai_style_prompt(self, style_name: str) -> str:
        """获取指定风格的提示词"""
        styles = self.get_ai_styles_config()
        if style_name in styles:
            return styles[style_name].get('prompt', '')

        # 如果找不到，返回默认风格
        from .logger import logger
        logger.warning(f"未找到AI风格 '{style_name}'，使用默认的 'humorous' 风格")
        if 'humorous' in styles:
            return styles['humorous'].get('prompt', '')

        # 如果连默认都没有，返回硬编码的默认值
        return '''你是一个B站UP主，擅长用幽默风趣的方式与观众互动。请根据以下信息，生成一条简短、幽默、有趣的回复。

视频信息：
- 标题：{video_title}
- 链接：{video_url}
- 简介：{video_desc}

评论内容：
- 用户：{comment_username}
- 评论：{comment_message}

要求：
1. 回复要幽默风趣，可以适当玩梗、调侃
2. 保持友好和尊重，不要冒犯用户
3. 回复简洁，控制在50字以内
4. 可以结合视频内容进行回应
5. 直接输出回复内容，不要加引号或其他说明

请生成回复：'''

    def add_video_to_config(self, bvid: str, template: str = None,
                            interval: int = None, use_ai: bool = True,
                            ai_style: str = None, title: str = None) -> bool:
        """添加监控视频到数据库

        Returns:
            True 表示新添加；False 表示已存在
        """
        from .database import monitored_video_manager
        from .logger import logger

        app_config = self.get_app_config()
        if template is None:
            template = "@{username} 感谢你的评论！"
        if interval is None:
            interval = app_config.get('default_check_interval', 60)
        if ai_style is None:
            ai_style = app_config.get('default_ai_style', 'humorous')

        if monitored_video_manager.is_monitored(bvid):
            logger.info(f"视频 {bvid} 已在监控列表中")
            return False

        inserted = monitored_video_manager.add_or_update(
            bvid=bvid,
            title=title,
            template=template,
            interval=interval,
            use_ai=use_ai,
            ai_style=ai_style,
            enabled=True,
        )
        title_info = f" - {title}" if title else ""
        logger.info(f"已添加新视频到监控列表: {bvid}{title_info} [风格: {ai_style}]")
        return inserted

    def migrate_seed_videos_to_db(self) -> int:
        """将 config.yaml 中的 videos 种子导入 SQLite，随后清空 YAML 中的 videos。

        Returns:
            新导入（此前不存在）的视频数量
        """
        from .database import monitored_video_manager, video_discovery_manager
        from .logger import logger

        seed_videos = self._config.get('videos') or []
        if not seed_videos:
            return 0

        app_config = self.get_app_config()
        default_interval = app_config.get('default_check_interval', 60)
        default_ai_style = app_config.get('default_ai_style', 'humorous')
        uploader_uid = app_config.get('uploader_uid')

        imported = 0
        for item in seed_videos:
            if isinstance(item, str):
                bvid = item
                title = None
                template = "@{username} 感谢你的评论！"
                interval = default_interval
                use_ai = False
                ai_style = None
            elif isinstance(item, dict) and item.get('bvid'):
                bvid = item['bvid']
                title = item.get('title')
                template = item.get('template', "@{username} 感谢你的评论！")
                interval = item.get('interval', default_interval)
                use_ai = item.get('use_ai', True)
                ai_style = item.get('ai_style', default_ai_style if use_ai else None)
            else:
                logger.warning(f"跳过无效的种子视频配置: {item}")
                continue

            was_new = not monitored_video_manager.is_monitored(bvid)
            monitored_video_manager.add_or_update(
                bvid=bvid,
                title=title,
                template=template,
                interval=interval,
                use_ai=use_ai,
                ai_style=ai_style,
                enabled=True,
            )
            if was_new:
                imported += 1

            # 同步标记为已发现，避免自动发现再次当成新视频
            if uploader_uid:
                video_discovery_manager.mark_discovered(str(uploader_uid), bvid, title)

        # 清空 YAML 中的长列表，避免配置文件继续膨胀
        self._config['videos'] = []
        self.save_config_to_file()
        logger.info(
            f"已将 config.yaml 中的 {len(seed_videos)} 条视频导入数据库"
            f"（新增 {imported}），并清空 YAML 中的 videos 列表"
        )
        return imported

    def save_config_to_file(self):
        """保存配置到文件（不写监控视频长列表，videos 固定为空数组）"""
        try:
            import io
            from .logger import logger

            # 确保不会把数据库中的视频写回 YAML
            config_to_save = dict(self._config)
            config_to_save['videos'] = []

            output = io.StringIO()

            class CustomDumper(yaml.SafeDumper):
                pass

            def str_representer(dumper, data):
                if '\n' in data or len(data) > 80:
                    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
                return dumper.represent_scalar('tag:yaml.org,2002:str', data)

            CustomDumper.add_representer(str, str_representer)

            # 只读文件需先放开写权限
            if self._config_path.exists() and not os.access(self._config_path, os.W_OK):
                try:
                    os.chmod(self._config_path, 0o644)
                except OSError:
                    pass

            yaml.dump(
                config_to_save,
                output,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
                indent=2,
                width=120,
                Dumper=CustomDumper
            )

            with open(self._config_path, 'w', encoding='utf-8') as f:
                f.write(output.getvalue())

            self._last_modified = self._config_path.stat().st_mtime
            self._config['videos'] = []
            logger.info("配置已保存到文件")
        except Exception as e:
            from .logger import logger
            logger.error(f"保存配置文件失败: {e}")
