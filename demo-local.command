#!/bin/zsh
cd -- "${0:A:h}"
.venv/bin/python cards.py demo
read '?演示结束，按回车关闭窗口'
