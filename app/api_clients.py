# api_clients.py - API客户端模块
from openai import OpenAI
from bilibili_api import Credential
from .config import ConfigManager
from .logger import logger


class CredentialManager:
    """凭证管理器 - 单例模式"""

    _instance = None
    _credential = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_credential(self):
        if self._credential is None:
            cred_config = ConfigManager().get_bilibili_credential() or {}

            def _clean(key):
                val = cred_config.get(key)
                if val is None:
                    return None
                return str(val).strip()

            sessdata = _clean('sessdata')
            bili_jct = _clean('bili_jct')
            buvid3 = _clean('buvid3')
            dedeuserid = _clean('dedeuserid')
            ac_time_value = _clean('ac_time_value')

            required = {
                'sessdata': sessdata,
                'bili_jct': bili_jct,
                'buvid3': buvid3,
                'dedeuserid': dedeuserid,
            }
            missing = [
                k for k, v in required.items()
                if not v or str(v).startswith('请填写')
            ]
            if missing:
                raise ValueError(
                    f"B站凭证缺少必填项: {', '.join(missing)}。"
                    f"请在 config.yaml 的 bilibili.credential 中填写，"
                    f"或设置对应环境变量（见 README）。"
                )

            self._credential = Credential(
                sessdata=sessdata,
                bili_jct=bili_jct,
                buvid3=buvid3,
                dedeuserid=dedeuserid,
                ac_time_value=ac_time_value
            )
        return self._credential

    def reset(self):
        """清除缓存的凭证（Cookie 更新后可调用）"""
        self._credential = None


class QwenClient:
    """千问API客户端 - 单例模式"""

    _instance = None
    _client = None
    _model = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def get_client(self):
        if self._client is None:
            qwen_config = ConfigManager().get_qwen_config()

            api_key = qwen_config.get('api_key')
            if not api_key:
                raise ValueError("未配置API Key")

            base_url = qwen_config.get('base_url')
            if not base_url:
                raise ValueError("未配置基础URL")

            model = qwen_config.get('model')
            if not model:
                raise ValueError("未配置模型")
            self._client = OpenAI(api_key=api_key, base_url=base_url)
            self._model = model

        return self._client, self._model

    def reset(self):
        """清除缓存的客户端（API Key 更新后可调用）"""
        self._client = None
        self._model = None


def generate_humorous_reply(video_title: str, video_desc: str, video_url: str,
                            comment_username: str, comment_message: str,
                            ai_style: str = None) -> str:
    """使用千问生成回复（支持多种风格）

    Args:
        video_title: 视频标题
        video_desc: 视频简介
        video_url: 视频链接
        comment_username: 评论用户名
        comment_message: 评论内容
        ai_style: AI回复风格（humorous/sharp/warm/professional）

    Returns:
        生成的回复文本
    """
    try:
        client, model = QwenClient().get_client()

        # 获取配置中的风格，如果没有指定则使用默认风格
        if ai_style is None:
            app_config = ConfigManager().get_app_config()
            ai_style = app_config.get('default_ai_style', 'humorous')

        # 获取对应风格的提示词模板
        prompt_template = ConfigManager().get_ai_style_prompt(ai_style)

        # 格式化提示词
        prompt = prompt_template.format(
            video_title=video_title,
            video_desc=video_desc if video_desc else '无',
            video_url=video_url,
            comment_username=comment_username,
            comment_message=comment_message
        )

        response = client.responses.create(model=model, input=prompt)
        reply = response.output_text.strip()

        if not reply:
            logger.warning("AI返回空回复，使用默认回复")
            return f"@{comment_username} 感谢评论！"

        return reply

    except Exception as e:
        logger.error(f"千问API调用失败: {e}")
        return f"@{comment_username} 感谢评论！"
