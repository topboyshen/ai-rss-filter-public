#!/bin/sh
# AI-Filter 启动脚本
# stderr 继承到 Docker 日志
exec 2>&1


# Jin10 通过内置 /jin10/rss 路由读取 RSSHub 官方 /jin10。
exec gunicorn -c /app/gunicorn.conf.py app:app
