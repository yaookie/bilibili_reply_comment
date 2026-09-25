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
                "未配置 videos 列表，也未设置 app.uploader_uid。"
                "请至少配置其一，否则程序无可监控视频。"
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
                old_videos_count = len(self._config.get('videos', []))
                self._load_config()
                new_videos_count = len(self._config.get('videos', []))
                logger.info(f"配置已重新加载 (视频数: {old_videos_count} -> {new_videos_count})")
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
        """获取视频配置列表"""
        videos = self.config.get('videos', [])
        # 确保返回值是列表，而不是 None
        if videos is None:
            return []
        return videos

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
        """动态添加视频到配置（内存中）

        Args:
            bvid: 视频BV号
            template: 回复模板
            interval: 检查间隔
            use_ai: 是否使用AI
            ai_style: AI风格
            title: 视频标题（可选）
        """
        app_config = self.get_app_config()
        if template is None:
            template = "@{username} 感谢你的评论！"
        if interval is None:
            interval = app_config.get('default_check_interval', 60)
        if ai_style is None:
            ai_style = app_config.get('default_ai_style', 'humorous')

        videos = self._config.get('videos', [])

        # 检查是否已存在
        for v in videos:
            if isinstance(v, dict) and v.get('bvid') == bvid:
                from .logger import logger
                logger.info(f"视频 {bvid} 已在监控列表中")
                return False

        # 添加新视频
        new_video = {
            'bvid': bvid,
            'title': title,  # 保存视频标题
            'template': template,
            'interval': interval,
            'use_ai': use_ai,
            'ai_style': ai_style
        }
        videos.append(new_video)
        self._config['videos'] = videos

        title_info = f" - {title}" if title else ""
        from .logger import logger
        logger.info(f"已添加新视频到监控列表: {bvid}{title_info} [风格: {ai_style}]")
        return True

    def save_config_to_file(self):
        """保存配置到文件"""
        try:
            # 自定义 YAML 输出格式，增加可读性
            import io

            # 使用 StringIO 捕获输出
            output = io.StringIO()

            # 自定义 Dumper 来改善格式
            class CustomDumper(yaml.SafeDumper):
                pass

            # 为字符串添加引号处理
            def str_representer(dumper, data):
                if '\n' in data or len(data) > 80:
                    return dumper.represent_scalar('tag:yaml.org,2002:str', data, style='|')
                return dumper.represent_scalar('tag:yaml.org,2002:str', data)

            CustomDumper.add_representer(str, str_representer)

            yaml.dump(
                self._config,
                output,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
                indent=2,  # 缩进2空格
                width=120,  # 每行最大宽度
                Dumper=CustomDumper
            )

            # 写入文件
            with open(self._config_path, 'w', encoding='utf-8') as f:
                f.write(output.getvalue())

            self._last_modified = self._config_path.stat().st_mtime
            from .logger import logger
            logger.info("配置已保存到文件")
        except Exception as e:
            from .logger import logger
            logger.error(f"保存配置文件失败: {e}")
