"""Per-workspace delivery text shared by real and simulated delivery."""
import json
import os
import tempfile
from pathlib import Path

DEFAULT_TEMPLATE = '您好，感谢光临小店，以下是发货内容：\n使用教程：xxx\n下载链接：xxxx\n卡密：{card}\n\n请尽快使用，谢谢！'


def validate_template(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 5000:
        raise ValueError('发货文案不能为空，且不能超过5000字')
    if value.count('{card}') != 1:
        raise ValueError('发货文案必须包含且仅包含一个 {card}，用于自动填入卡密')
    return value


def load_template(path=None):
    if path is None or not Path(path).exists():
        return DEFAULT_TEMPLATE
    return validate_template(json.loads(Path(path).read_text(encoding='utf-8'))['template'])


def save_template(path, value):
    validate_template(value)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.delivery-template-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump({'template': value}, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
