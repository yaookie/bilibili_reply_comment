# bilibili_auto_reply.py - 主入口文件
from modules.main import main
from bilibili_api import sync

if __name__ == "__main__":
    sync(main())
