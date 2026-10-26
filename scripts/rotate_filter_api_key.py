#!/usr/bin/env python3
"""生成新 AI-Filter API 密钥（32 位 hex）。

用法:
  python3 scripts/rotate_filter_api_key.py

轮换后请:
  1. 更新 .env 中 AI_FILTER_API_KEY=（建议 chmod 600 .env）
  2. 重启应用容器使进程加载新环境变量
  3. 更新所有订阅/书签里带 key= 的 URL，或改用请求头 X-API-Key
  4. 勿在 crontab 写密钥；host 上已用 health_check.sh / scripts/cron_refresh.sh 读 .env
"""
import secrets

if __name__ == "__main__":
    print(secrets.token_hex(16))
