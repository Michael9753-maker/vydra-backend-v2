import os
import time
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from logger_manager import log_event, log_exception

# -------------------------
# CONFIG
# -------------------------

RAM_CACHE_LIMIT = 10_000         # Max links in RAM
MAX_CACHE_MB = 5_000             # 5GB soft RAM/SSD cache limit

EXPIRY_RULES = {
    "guest":   3,   # days
    "free":    7,
    "premium": 14
}

HOT_LINK_MIN_24H = 10
HOT_LINK_MIN_7D  = 30

# -------------------------
# DATA STRUCTURES
# -------------------------


class CacheItem:
    def __init__(self, url, data, user_type):
        self.url = url
        self.data = data
        self.user_type = user_type
        self.created_at = datetime.now(timezone.utc)
        self.last_accessed = datetime.now(timezone.utc)
        self.access_count = 1
        self.is_hot = False
        self.expiry = self._calculate_expiry()

    def _calculate_expiry(self):
        days = EXPIRY_RULES.get(self.user_type, 3)
        return datetime.now(timezone.utc) + timedelta(days=days)

    def refresh_access(self):
        self.last_accessed = datetime.now(timezone.utc)
        self.access_count += 1

    def is_expired(self):
        if self.is_hot:
            return False
        return datetime.now(timezone.utc) > self.expiry

    def mark_hot(self):
        self.is_hot = True
        self.expiry = None
        try:
            log_event("CACHE_HOTLINK", f"Promoted to hot: {self.url}")
        except Exception:
            pass

    def file_exists(self):
        if isinstance(self.data, dict):
            path = self.data.get("file_path")
            if path and os.path.exists(path):
                return True
        return False


# -------------------------
# CACHE MANAGER
# -------------------------


class CacheManager:
    def __init__(self, limit=RAM_CACHE_LIMIT):
        self.limit = limit
        self.cache = OrderedDict()
        self.lock = threading.Lock()
        self.hit_count = 0
        self.miss_count = 0

    # ------------------------
    # SIZE CALCULATION
    # ------------------------

    def _get_file_size_mb(self, path):
        try:
            return os.path.getsize(path) / (1024 * 1024)
        except Exception as e:
            try:
                log_exception("CACHE", e)
            except Exception:
                pass
            return 0

    def get_total_cache_size_mb(self):
        total = 0
        for item in self.cache.values():
            if isinstance(item.data, dict):
                path = item.data.get("file_path")
                if path:
                    total += self._get_file_size_mb(path)
        return total

    # ------------------------
    # SAFE FILE DELETE
    # ------------------------

    def _delete_file_safe(self, path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
                try:
                    log_event("CACHE_FILE_DELETE", f"Deleted file: {path}")
                except Exception:
                    pass
        except Exception as e:
            try:
                log_exception("CACHE_FILE_DELETE", e)
            except Exception:
                pass

    # ------------------------
    # GET
    # ------------------------

    def get(self, url):
        with self.lock:
            try:
                if url not in self.cache:
                    self.miss_count += 1
                    try:
                        log_event("CACHE_MISS", f"URL not found: {url}")
                    except Exception:
                        pass
                    return None

                item = self.cache.pop(url)

                if item.is_expired():
                    self.miss_count += 1
                    try:
                        log_event("CACHE_EXPIRED", f"Expired entry: {url}")
                    except Exception:
                        pass
                    return None

                if not item.file_exists():
                    self.miss_count += 1
                    try:
                        log_event("CACHE_FILE_MISSING", f"File missing for cache entry, purging: {url}")
                    except Exception:
                        pass
                    return None

                item.refresh_access()
                self._check_hot(item)

                self.cache[url] = item
                self.hit_count += 1
                try:
                    log_event("CACHE_HIT", f"Served from cache: {url}")
                except Exception:
                    pass

                return item.data
            except Exception as e:
                try:
                    log_exception("CACHE", e)
                except Exception:
                    pass
                return None

    # ------------------------
    # SET
    # ------------------------

    def set(self, url, data, user_type="guest"):
        with self.lock:
            try:
                if url in self.cache:
                    item = self.cache.pop(url)
                    item.data = data
                    item.user_type = user_type
                    item.expiry = item._calculate_expiry()
                    item.refresh_access()
                    self.cache[url] = item
                    try:
                        log_event("CACHE_UPDATE", f"Updated existing cache item: {url}")
                    except Exception:
                        pass
                    return

                self._smart_evict()

                item = CacheItem(url, data, user_type)
                self.cache[url] = item
                try:
                    log_event("CACHE_STORE", f"New cache entry stored: {url}")
                except Exception:
                    pass
            except Exception as e:
                try:
                    log_exception("CACHE", e)
                except Exception:
                    pass

    # ------------------------
    # SMART EVICTION
    # ------------------------

    def _smart_evict(self):
        try:
            expired = [k for k, v in self.cache.items() if v.is_expired()]
            for k in expired:
                try:
                    v = self.cache.get(k)
                    if isinstance(v.data, dict):
                        self._delete_file_safe(v.data.get("file_path"))
                    self.cache.pop(k, None)
                    try:
                        log_event("CACHE_EVICT_EXPIRED", f"Expired evicted: {k}")
                    except Exception:
                        pass
                except Exception:
                    pass

            while len(self.cache) >= self.limit:
                try:
                    evicted_key, evicted_item = self.cache.popitem(last=False)
                    if isinstance(evicted_item.data, dict):
                        self._delete_file_safe(evicted_item.data.get("file_path"))
                    try:
                        log_event("CACHE_EVICT_LRU", f"LRU eviction: {evicted_key}")
                    except Exception:
                        pass
                except Exception:
                    break

            while self.get_total_cache_size_mb() > MAX_CACHE_MB:
                try:
                    evicted_key, evicted_item = self.cache.popitem(last=False)
                    if isinstance(evicted_item.data, dict):
                        self._delete_file_safe(evicted_item.data.get("file_path"))
                    try:
                        log_event("CACHE_EVICT_SIZE", f"Cache size evicted: {evicted_key}")
                    except Exception:
                        pass
                except Exception:
                    break
        except Exception as e:
            try:
                log_exception("CACHE", e)
            except Exception:
                pass

    # ------------------------
    # HOT LINK CHECK
    # ------------------------

    def _check_hot(self, item):
        if item.is_hot:
            return

        now = datetime.now(timezone.utc)
        delta = now - item.created_at

        if delta < timedelta(hours=24) and item.access_count >= HOT_LINK_MIN_24H:
            try:
                log_event("CACHE_HOTLINK", f"Promoted to hot link (24h rule): {item.url}")
            except Exception:
                pass
            item.mark_hot()
        elif delta < timedelta(days=7) and item.access_count >= HOT_LINK_MIN_7D:
            try:
                log_event("CACHE_HOTLINK", f"Promoted to hot link (7d rule): {item.url}")
            except Exception:
                pass
            item.mark_hot()

    # ------------------------
    # CLEANUP
    # ------------------------

    def cleanup(self):
        with self.lock:
            try:
                try:
                    log_event("CACHE_CLEANUP_START", "Cleanup cycle started")
                except Exception:
                    pass

                remove_keys = []

                for k, v in list(self.cache.items()):
                    expired = v.is_expired()
                    missing = not v.file_exists()
                    if expired or missing:
                        if isinstance(v.data, dict):
                            self._delete_file_safe(v.data.get("file_path"))
                        remove_keys.append(k)

                for k in remove_keys:
                    try:
                        self.cache.pop(k, None)
                        try:
                            log_event("CACHE_CLEANUP_REMOVE", f"Cleanup removed: {k}")
                        except Exception:
                            pass
                    except Exception:
                        pass

                try:
                    log_event("CACHE_CLEANUP_FINISH", "Cleanup cycle finished")
                except Exception:
                    pass

            except Exception as e:
                try:
                    log_exception("CACHE", e)
                except Exception:
                    pass

    # ------------------------
    # STATS
    # ------------------------

    def stats(self):
        with self.lock:
            hot = sum(1 for v in self.cache.values() if v.is_hot)
            return {
                "stored_links": len(self.cache),
                "cache_limit": self.limit,
                "hits": self.hit_count,
                "misses": self.miss_count,
                "hot_links": hot,
                "size_mb": round(self.get_total_cache_size_mb(), 2)
            }


# -------------------------
# SINGLE GLOBAL INSTANCE
# -------------------------

cache_manager = CacheManager()

# -------------------------
# AUTO CLEANUP THREAD
# -------------------------

def _auto_cleanup():
    while True:
        time.sleep(600)
        cache_manager.cleanup()

cleanup_thread = threading.Thread(target=_auto_cleanup, daemon=True)
cleanup_thread.start()
