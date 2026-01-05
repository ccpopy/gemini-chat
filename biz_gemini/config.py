"""统一配置管理模块，支持 config.json、环境变量、配置迁移和热重载。

该模块提供：
- 配置文件加载和保存
- 环境变量覆盖
- 旧配置格式迁移
- 配置热重载
- 账号状态管理（JWT 缓存、Cookie 状态等）
"""
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# 配置文件路径
PROJECT_ROOT = Path(__file__).parent.parent
NEW_CONFIG_FILE = PROJECT_ROOT / "config.json"
OLD_CONFIG_FILE = Path(__file__).parent / "business_gemini_session.json"
TIME_FMT = "%Y-%m-%d %H:%M:%S"

# 模块级 logger（不使用 .logger 避免循环导入）
logger = logging.getLogger("config")

# 默认配置
DEFAULT_CONFIG = {
    "server": {
        "host": "0.0.0.0",
        "port": 8000,
        "workers": 4,
        "log_level": "INFO",
        "reload": False,
    },
    "proxy": {
        "enabled": False,
        "url": "",
        "timeout": 30,
    },
    "session": {
        "secure_c_ses": "",
        "host_c_oses": "",
        "nid": "",
        "csesidx": "",
        "group_id": "",
        "project_id": "",
        "cookies_saved_at": "",
        "cookie_raw": "",  # 完整的 raw cookie header（优先使用）
        "cookie_profile_dir": "",  # cookie 来源的浏览器用户数据目录
    },
    # 浏览器保活配置
    "browser_keep_alive": {
        "enabled": False,  # 是否启用浏览器保活
        "interval_minutes": 60,  # 保活间隔（分钟）
        "headless": True,  # 是否无头模式
    },
    # 远程浏览器配置（用于登录和 Cookie 刷新）
    "remote_browser": {
        "headless": True,  # 是否无头模式，Linux服务器设为true，Windows本地可设为false
    },
    # 账号状态（运行时状态，不持久化到配置文件）
    "account_state": {
        "jwt": "",  # 缓存的 JWT
        "jwt_time": 0,  # JWT 获取时间戳
        "jwt_expires_at": 0,  # JWT 过期时间戳
        "available": True,  # 账号是否可用
        "cookie_expired": False,  # Cookie 是否已过期
        "cooldown_until": 0,  # 冷却结束时间戳
        "cooldown_reason": "",  # 冷却原因
        "last_refresh_time": 0,  # 上次刷新时间
        "conversation_sessions": {},  # 对话 ID -> session 映射
    },
    # 安全配置
    "security": {
        "admin_password": "",  # 管理密码（明文存储，方便管理者查看恢复）
        "require_api_key": False,  # 是否要求 API Key 验证
    },
    # Redis 配置（用于多 worker 状态共享）
    "redis": {
        "enabled": False,  # 是否启用 Redis
        "host": "127.0.0.1",  # Redis 主机
        "port": 6379,  # Redis 端口
        "password": "",  # Redis 密码
        "db": 0,  # Redis 数据库编号
        "key_prefix": "gemini_chat:",  # Key 前缀
    },
    # IMAP 配置（用于自动读取验证码）
    "imap": {
        "enabled": False,  # 是否启用 IMAP 验证码读取
        "host": "",  # IMAP 服务器地址
        "port": 993,  # IMAP 端口
        "user": "",  # 邮箱账号
        "password": "",  # 邮箱密码或授权码
        "use_ssl": True,  # 是否使用 SSL
        "folder": "INBOX",  # 邮箱文件夹
        "sender_filter": "noreply-googlecloud@google.com",  # 发件人过滤
        "code_pattern": r'class="x_verification-code">([A-Z0-9]{6})</span>',  # 验证码正则
        "max_age_seconds": 300,  # 只查找最近 5 分钟的邮件
        "timeout_seconds": 180,  # 等待验证码超时（3 分钟）
        "poll_interval": 5,  # 轮询间隔（秒）
        "auto_login": True,  # 配合保活使用：过期时自动登录
    },
}


def sanitize_group_id(group_id: Optional[str]) -> Optional[str]:
    """去掉 group_id 中可能携带的路径或查询参数，只保留裸 UUID。"""
    if not group_id:
        return group_id
    cleaned = group_id.strip()
    for sep in ("/", "?", "#"):
        cleaned = cleaned.split(sep, 1)[0]
    return cleaned


def migrate_old_config() -> bool:
    """迁移旧配置文件到新格式"""
    if not OLD_CONFIG_FILE.exists() or NEW_CONFIG_FILE.exists():
        return False

    try:
        logger.info("检测到旧配置文件，正在迁移...")
        with open(OLD_CONFIG_FILE, "r", encoding="utf-8") as f:
            old_cfg = json.load(f)

        # 创建新配置结构
        new_cfg = DEFAULT_CONFIG.copy()

        # 迁移 session 配置
        session_keys = ["secure_c_ses", "host_c_oses", "nid", "csesidx", "group_id", "project_id", "cookies_saved_at", "saved_at"]
        for key in session_keys:
            if key in old_cfg:
                if key == "saved_at":
                    # 旧字段名映射
                    new_cfg["session"]["cookies_saved_at"] = old_cfg[key]
                else:
                    new_cfg["session"][key] = old_cfg[key]

        # 迁移 proxy 配置
        if "proxy" in old_cfg and old_cfg["proxy"]:
            new_cfg["proxy"]["enabled"] = True
            new_cfg["proxy"]["url"] = old_cfg["proxy"]

        # 保存新配置
        with open(NEW_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(new_cfg, f, ensure_ascii=False, indent=2)

        # 备份旧配置
        backup_file = OLD_CONFIG_FILE.with_suffix(".json.backup")
        shutil.copy2(OLD_CONFIG_FILE, backup_file)
        logger.info(f"配置迁移完成，旧配置已备份到: {backup_file}")

        return True
    except Exception as e:
        logger.warning(f"配置迁移失败: {e}")
        return False


def load_config() -> dict:
    """加载配置，优先级：环境变量 > config.json > 默认配置"""
    # 尝试迁移旧配置
    migrate_old_config()

    # 加载配置文件
    if NEW_CONFIG_FILE.exists():
        try:
            with open(NEW_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except json.JSONDecodeError as e:
            logger.warning(f"配置文件格式错误: {e}")
            cfg = DEFAULT_CONFIG.copy()
    else:
        cfg = DEFAULT_CONFIG.copy()

    # 确保配置结构完整
    for section, defaults in DEFAULT_CONFIG.items():
        if section not in cfg:
            cfg[section] = defaults.copy()
        else:
            for key, value in defaults.items():
                if key not in cfg[section]:
                    cfg[section][key] = value

    # 环境变量覆盖 - Server 配置
    if os.getenv("SERVER_HOST"):
        cfg["server"]["host"] = os.getenv("SERVER_HOST")
    if os.getenv("SERVER_PORT"):
        cfg["server"]["port"] = int(os.getenv("SERVER_PORT"))
    if os.getenv("SERVER_WORKERS"):
        cfg["server"]["workers"] = int(os.getenv("SERVER_WORKERS"))
    if os.getenv("SERVER_LOG_LEVEL"):
        cfg["server"]["log_level"] = os.getenv("SERVER_LOG_LEVEL")

    # 环境变量覆盖 - Proxy 配置
    if os.getenv("PROXY_URL"):
        cfg["proxy"]["enabled"] = True
        cfg["proxy"]["url"] = os.getenv("PROXY_URL")
    if os.getenv("PROXY_TIMEOUT"):
        cfg["proxy"]["timeout"] = int(os.getenv("PROXY_TIMEOUT"))

    # 环境变量覆盖 - Session 配置
    if os.getenv("BIZ_GEMINI_SECURE_C_SES"):
        cfg["session"]["secure_c_ses"] = os.getenv("BIZ_GEMINI_SECURE_C_SES")
    if os.getenv("BIZ_GEMINI_HOST_C_OSES"):
        cfg["session"]["host_c_oses"] = os.getenv("BIZ_GEMINI_HOST_C_OSES")
    if os.getenv("BIZ_GEMINI_NID"):
        cfg["session"]["nid"] = os.getenv("BIZ_GEMINI_NID")
    if os.getenv("BIZ_GEMINI_CSESIDX"):
        cfg["session"]["csesidx"] = os.getenv("BIZ_GEMINI_CSESIDX")
    if os.getenv("BIZ_GEMINI_GROUP_ID"):
        cfg["session"]["group_id"] = os.getenv("BIZ_GEMINI_GROUP_ID")
    if os.getenv("BIZ_GEMINI_PROJECT_ID"):
        cfg["session"]["project_id"] = os.getenv("BIZ_GEMINI_PROJECT_ID")

    # 为了向后兼容，将 session 配置提升到顶层
    # 这样旧代码可以继续使用 config.get("secure_c_ses") 等
    cfg.update(cfg["session"])
    
    # 为向后兼容添加 proxy_url 字段
    if cfg["proxy"]["enabled"] and cfg["proxy"]["url"]:
        cfg["proxy_url"] = cfg["proxy"]["url"]  # 兼容性字段
    else:
        cfg["proxy_url"] = None  # 代理未启用时设为 None

    # 清理 group_id
    cfg["group_id"] = sanitize_group_id(cfg.get("group_id"))
    if "session" in cfg:
        cfg["session"]["group_id"] = cfg["group_id"]

    return cfg


def save_config(update: dict) -> dict:
    """更新并保存配置，返回合并后的结果"""
    cfg = load_config()

    # 如果 update 包含旧格式的顶层字段，映射到新结构
    session_keys = ["secure_c_ses", "host_c_oses", "nid", "csesidx", "group_id", "project_id",
                    "cookies_saved_at", "saved_at", "cookie_raw", "cookie_profile_dir", "username"]
    for key in session_keys:
        if key in update:
            if key == "saved_at":
                cfg["session"]["cookies_saved_at"] = update[key]
            else:
                cfg["session"][key] = update[key]
            # 同时更新顶层（向后兼容）
            if key != "saved_at":
                cfg[key] = update[key]

    # 处理 proxy
    if "proxy" in update:
        if isinstance(update["proxy"], str):
            # 旧格式：直接是 URL 字符串
            cfg["proxy"]["enabled"] = bool(update["proxy"])
            cfg["proxy"]["url"] = update["proxy"]
            cfg["proxy_url"] = update["proxy"]
        elif isinstance(update["proxy"], dict):
            # 新格式：字典
            cfg["proxy"].update(update["proxy"])
    
    # 清理 group_id
    cfg["session"]["group_id"] = sanitize_group_id(cfg["session"].get("group_id"))
    cfg["group_id"] = cfg["session"]["group_id"]
    
    # 处理 security 配置
    if "security" in update:
        if "security" not in cfg:
            cfg["security"] = DEFAULT_CONFIG["security"].copy()
        cfg["security"].update(update["security"])
    
    # 处理 imap 配置
    if "imap" in update:
        if "imap" not in cfg:
            cfg["imap"] = DEFAULT_CONFIG["imap"].copy()
        cfg["imap"].update(update["imap"])
    
    # 处理 browser_keep_alive 配置
    if "browser_keep_alive" in update:
        if "browser_keep_alive" not in cfg:
            cfg["browser_keep_alive"] = DEFAULT_CONFIG["browser_keep_alive"].copy()
        cfg["browser_keep_alive"].update(update["browser_keep_alive"])
    
    # 保存到文件（只保存结构化数据）
    save_data = {
        "server": cfg.get("server", DEFAULT_CONFIG["server"]),
        "proxy": {
            "enabled": cfg.get("proxy", {}).get("enabled", False) if isinstance(cfg.get("proxy"), dict) else bool(cfg.get("proxy")),
            "url": cfg.get("proxy", {}).get("url", "") if isinstance(cfg.get("proxy"), dict) else cfg.get("proxy", ""),
            "timeout": cfg.get("proxy", {}).get("timeout", 30) if isinstance(cfg.get("proxy"), dict) else 30,
        },
        "session": cfg["session"],
        "browser_keep_alive": cfg.get("browser_keep_alive", DEFAULT_CONFIG["browser_keep_alive"]),
        "remote_browser": cfg.get("remote_browser", DEFAULT_CONFIG["remote_browser"]),
        "security": cfg.get("security", DEFAULT_CONFIG["security"]),
        "redis": cfg.get("redis", DEFAULT_CONFIG["redis"]),
        "imap": cfg.get("imap", DEFAULT_CONFIG["imap"]),
    }
    
    NEW_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(NEW_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(save_data, f, ensure_ascii=False, indent=2)
    
    return cfg


def get_proxy(config: dict) -> Optional[str]:
    """返回代理地址（支持 http/socks5/socks5h）。
    
    如果未配置代理，返回 None（直接连接）。
    """
    # 新格式
    if isinstance(config.get("proxy"), dict):
        proxy_cfg = config["proxy"]
        if proxy_cfg.get("enabled") and proxy_cfg.get("url"):
            return proxy_cfg["url"]
    
    # 旧格式兼容
    if isinstance(config.get("proxy"), str) and config["proxy"]:
        return config["proxy"]
    
    # 兼容性字段
    if config.get("proxy_url"):
        return config["proxy_url"]
    
    return None


def cookies_age_seconds(config: dict) -> Optional[float]:
    """返回 cookie 保存时间与当前时间的秒差。"""
    # 尝试从 session 配置获取
    ts_str = None
    if "session" in config:
        ts_str = config["session"].get("cookies_saved_at") or config["session"].get("saved_at")
    
    # 兼容旧格式
    if not ts_str:
        ts_str = config.get("cookies_saved_at") or config.get("saved_at")
    
    if not ts_str:
        return None
    try:
        dt = datetime.strptime(ts_str, TIME_FMT)
    except ValueError:
        return None
    return (datetime.now() - dt).total_seconds()


def cookies_expired(config: dict, max_age_hours: int = 0) -> bool:
    """基于时间戳判断 cookie 是否超过 max_age_hours。

    此函数仅根据保存时间进行粗略判断，无法检测真实的 session 状态。
    推荐使用 auth.check_session_status() 进行实时验证。

    Args:
        config: 配置字典
        max_age_hours: 最大有效小时数，默认 0 表示不检查（始终返回 False）

    Returns:
        True 表示超时，False 表示未超时或未配置
    """
    if max_age_hours <= 0:
        return False
    age = cookies_age_seconds(config)
    if age is None:
        return False
    return age > max_age_hours * 3600


# 配置热重载支持
_config_cache = None
_config_mtime = None

# 账号状态（运行时状态，线程安全）
_account_state_lock = threading.RLock()
_account_state: Dict[str, Any] = {
    "jwt": "",
    "jwt_time": 0,
    "jwt_expires_at": 0,
    "available": True,
    "cookie_expired": False,
    "cooldown_until": 0,
    "cooldown_reason": "",
    "last_refresh_time": 0,
    "conversation_sessions": {},
}


def get_cached_config(force_reload: bool = False) -> dict:
    """获取缓存的配置，支持文件变更检测"""
    global _config_cache, _config_mtime
    
    if force_reload:
        _config_cache = None
        _config_mtime = None
    
    current_mtime = None
    if NEW_CONFIG_FILE.exists():
        current_mtime = NEW_CONFIG_FILE.stat().st_mtime
    
    if _config_cache is None or _config_mtime != current_mtime:
        _config_cache = load_config()
        _config_mtime = current_mtime
    
    return _config_cache


def reload_config() -> dict:
    """强制重新加载配置"""
    return get_cached_config(force_reload=True)


# ==================== 账号状态管理 ====================

def get_account_state() -> Dict[str, Any]:
    """获取账号状态（线程安全）"""
    with _account_state_lock:
        return _account_state.copy()


def update_account_state(updates: Dict[str, Any]) -> None:
    """更新账号状态（线程安全）"""
    with _account_state_lock:
        _account_state.update(updates)


def get_cached_jwt() -> tuple[Optional[str], float]:
    """获取缓存的 JWT 和过期时间戳"""
    with _account_state_lock:
        return _account_state.get("jwt"), _account_state.get("jwt_expires_at", 0)


def set_cached_jwt(jwt: str, expires_at: float) -> None:
    """设置缓存的 JWT。"""
    with _account_state_lock:
        _account_state["jwt"] = jwt
        _account_state["jwt_time"] = time.time()
        _account_state["jwt_expires_at"] = expires_at


def clear_jwt_cache() -> None:
    """清除 JWT 缓存（Cookie 刷新后调用）"""
    with _account_state_lock:
        _account_state["jwt"] = ""
        _account_state["jwt_time"] = 0
        _account_state["jwt_expires_at"] = 0


def mark_cookie_expired(reason: str = "") -> None:
    """标记 Cookie 已过期"""
    with _account_state_lock:
        _account_state["cookie_expired"] = True
        _account_state["available"] = False
        if reason:
            _account_state["cooldown_reason"] = reason
    # 同步到 Redis（多 Worker 一致性）
    _sync_cookie_state_to_redis(expired=True, reason=reason)
    logger.info(f"Cookie 已标记为过期: {reason}")


def mark_cookie_valid() -> None:
    """标记 Cookie 有效（刷新成功后调用）。"""
    with _account_state_lock:
        _account_state["cookie_expired"] = False
        _account_state["available"] = True
        _account_state["cooldown_until"] = 0
        _account_state["cooldown_reason"] = ""
        _account_state["last_refresh_time"] = time.time()
    # 同步到 Redis（多 Worker 一致性）
    _sync_cookie_state_to_redis(expired=False, reason="")


def is_cookie_expired(verify_if_expired: bool = True) -> bool:
    """检查 Cookie 是否已标记为过期
    
    优先从 Redis 读取（多 Worker 一致性），回退到本地内存状态。
    当 verify_if_expired=True 且缓存显示过期时，会尝试通过 getoxsrf 实时验证。
    """
    # 优先从 Redis 读取共享状态
    redis_state = _load_cookie_state_from_redis()
    if redis_state is not None:
        if redis_state and verify_if_expired:
            # 缓存显示过期，尝试实时验证 getoxsrf
            try:
                from .auth import _get_jwt_via_api
                config = load_config()
                _get_jwt_via_api(config)  # 如果成功，说明实际有效
                mark_cookie_valid()  # 更新缓存
                logger.info("Redis cookie_state 显示过期但 getoxsrf 验证成功，已更新状态")
                return False
            except Exception as e:
                logger.debug(f"getoxsrf 验证失败，确认过期: {e}")
                pass  # 验证失败，确认过期
        return redis_state
    # 回退到本地内存状态
    with _account_state_lock:
        return _account_state.get("cookie_expired", False)


def _sync_cookie_state_to_redis(expired: bool, reason: str = "") -> None:
    """将 Cookie 状态同步到 Redis（供其他 Worker 读取）。"""
    try:
        from .redis_manager import get_redis_manager
        config = load_config()
        redis_mgr = get_redis_manager(config)
        if redis_mgr.is_redis_enabled():
            state = {
                "cookie_expired": expired,
                "reason": reason,
                "timestamp": time.time(),
            }
            redis_mgr.set_json("cookie_state", state, ex=600)  # 10 分钟过期
    except Exception as e:
        logger.debug(f"同步 Cookie 状态到 Redis 失败: {e}")


def _load_cookie_state_from_redis() -> Optional[bool]:
    """从 Redis 读取 Cookie 状态（多 Worker 一致性）
    
    Returns:
        Cookie 是否过期，如果 Redis 不可用则返回 None
    """
    try:
        from .redis_manager import get_redis_manager
        config = load_config()
        redis_mgr = get_redis_manager(config)
        if redis_mgr.is_redis_enabled():
            state = redis_mgr.get_json("cookie_state")
            if state and isinstance(state, dict):
                return state.get("cookie_expired", False)
    except Exception as e:
        logger.debug(f"从 Redis 读取 Cookie 状态失败: {e}")
    return None


def set_cooldown(duration_seconds: int, reason: str = "") -> None:
    """设置账号冷却。"""
    with _account_state_lock:
        _account_state["cooldown_until"] = time.time() + duration_seconds
        _account_state["cooldown_reason"] = reason
        _account_state["available"] = False
    logger.info(f"账号进入冷却: {duration_seconds}s, 原因: {reason}")


def is_in_cooldown() -> tuple[bool, float]:
    """检查是否在冷却中，返回 (是否冷却, 剩余秒数)。"""
    with _account_state_lock:
        cooldown_until = _account_state.get("cooldown_until", 0)
        if cooldown_until > time.time():
            return True, cooldown_until - time.time()
        return False, 0


def get_conversation_session(conversation_id: str) -> Optional[str]:
    """获取对话对应的 session"""
    with _account_state_lock:
        return _account_state.get("conversation_sessions", {}).get(conversation_id)


def set_conversation_session(conversation_id: str, session_name: str) -> None:
    """设置对话对应的 session"""
    with _account_state_lock:
        if "conversation_sessions" not in _account_state:
            _account_state["conversation_sessions"] = {}
        _account_state["conversation_sessions"][conversation_id] = session_name


def clear_conversation_sessions() -> None:
    """清除所有对话 session 映射（JWT 刷新后调用）"""
    with _account_state_lock:
        _account_state["conversation_sessions"] = {}
    logger.debug("已清除所有对话 session 映射")


def clear_redis_session_cache() -> None:
    """清除 Redis 中的 session 缓存（登录/Cookie 刷新后调用）
    
    当 csesidx 变化时，旧的 session 归属于旧用户，需要清除缓存以避免
    403 PERMISSION_DENIED "Session is not owned by the provided user" 错误。
    """
    try:
        from .redis_manager import get_redis_manager
        config = load_config()
        redis_mgr = get_redis_manager(config)
        if redis_mgr.is_redis_enabled():
            group_id = config.get("group_id")
            if group_id:
                # 清除 session:{group_id} 缓存
                redis_key = f"session:{group_id}"
                redis_mgr.delete(redis_key)
                logger.info(f"已清除 Redis session 缓存: {redis_key}")
            
            # 同时清除 JWT 缓存（确保一致性）
            redis_mgr.delete("jwt_token")
            logger.debug("已清除 Redis JWT 缓存")
            
            # 清除 cookie_state 缓存
            redis_mgr.delete("cookie_state")
            logger.debug("已清除 Redis cookie_state 缓存")
            
            # 清除 keep_alive_state 缓存
            redis_mgr.delete("keep_alive_state")
            logger.debug("已清除 Redis keep_alive_state 缓存")
    except Exception as e:
        logger.debug(f"清除 Redis session 缓存失败（可忽略）: {e}")
