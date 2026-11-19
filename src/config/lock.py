"""配置文件读写锁；录制、平台适配与 Web 后端共享同一把可重入锁。"""

import threading

file_update_lock: threading.RLock = threading.RLock()
