# agent运行要求
1. 在进行长时任务时，先使用plan规划任务，并列出任务，完成某一任务的时候要生成简报，告知用户
2. 环境需要安装新的依赖的时候，使用ask-user-question插件询问用户
3. 遇到较新的，没遇见的问题或者技术的时候，使用mcp中的联网搜索功能获取最新信息


# 项目文件解析
- 文件夹：config：储存config.yaml config.py 使用python包读取config.yaml，使用pydantic model作为校验，并建立实例以供其它模块加载配置
- 文件夹dependencies 作为fastapi获取数据库实例的模块
- moels 储存orm模型
- repositories 储存各种数据库的读写删查的类
- routers 储存fastapi的路由
- service 储存fastapi路由的功能实现逻辑
- uv.lock 表明该项目的python环境管理由python实现
