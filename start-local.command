#!/bin/zsh
cd -- "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  print '未找到虚拟环境，请先安装项目依赖。'
  read '?按回车退出'
  exit 1
fi
.venv/bin/python - <<'CHECK'
from dotenv import dotenv_values
import sys
config = dotenv_values('.env')
missing = [key for key in ('COOKIES_STR',) if not config.get(key)]
if missing:
    print('请先在本地 .env 填写：' + '、'.join(missing))
    sys.exit(1)
from utils.xianyu_utils import trans_cookies
cookies = trans_cookies(config['COOKIES_STR'])
if not all(cookies.get(k) for k in ('unb', '_m_h5_tk')):
    print('Cookie 缺少 unb 或 _m_h5_tk，请复制闲鱼已登录请求的完整 Cookie。')
    sys.exit(1)
CHECK
if (( $? != 0 )); then
  read '?按回车退出'
  exit 1
fi
print '启动卡密服务；CARD_DRY_RUN=true 为演练，false 为实际发卡。Ctrl+C 停止。'
exec .venv/bin/python cards.py run
