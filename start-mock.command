#!/bin/zsh
cd -- "${0:A:h}"
print '聊天模拟：买家发送“已支付”会收到一条不可使用的 MOCK 测试卡密。Ctrl+C 停止。'
exec .venv/bin/python cards.py mock-chat
