"""DSH 沙箱下的临时目录修复。

现象（本机实测）：`os.mkdir(path, 0o700)` 创建出来的目录，其所有者 ACL 只给
owner 完全控制；DSH 的沙箱进程用的是受限令牌，写入这种目录会
`PermissionError: [Errno 13]`。而 `tempfile.mkdtemp()` 正是用 0o700 建目录，
于是 `ensurepip` / `pip` 的临时目录全部写不进去（表现为
"Failed to remove contents in a temporary directory" 与
"ERROR: Could not install ... Check the permissions."）。

修法：把 `os.mkdir` 的 mode 强制成 0o777，使临时目录对所有主体可写。
只在本工具链的安装/运行期通过 PYTHONPATH 注入，不修改系统 Python。
"""
import os

_orig_mkdir = os.mkdir


def _mkdir(path, mode=0o777, *args, **kwargs):
    return _orig_mkdir(path, 0o777, *args, **kwargs)


os.mkdir = _mkdir
