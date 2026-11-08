"""PyInstaller 入口：以对象方式启动 uvicorn，避免 frozen 下 import string 解析失败。"""

from server.main import main

if __name__ == "__main__":
    main()
