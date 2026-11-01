"""
local_cache_manager.py — 本地缓存管理器

用于将 Google Drive 文件复制到本地临时目录进行处理，
处理完成后再同步回 Google Drive，解决网络延迟导致的卡顿问题。
"""

import os
import json
import shutil
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional, Tuple
from logger import log as logger_log


# 骨架缓存只搬运字幕类小文件；视频/音频等大文件由 ensure_file() 按需拉取。
_SKELETON_EXTENSIONS = {".srt", ".txt", ".vtt", ".json"}
_SKELETON_MAX_SIZE = 10 * 1024 * 1024  # 10 MB

# 这些子目录放的是处理产物（成品、片段、参考音频），不参与骨架同步：
# 一旦把产物也算进镜像集合，下一次同步会把它们当成“源目录已删除的字幕”隔离掉。
_SKELETON_SKIP_DIRS = {"中配", "tmp", "REF_AUDIO_PATH", ".orphaned"}

_SKELETON_MARKER = ".skeleton_ready"
_SKELETON_MANIFEST = ".skeleton_manifest.json"
_SKELETON_ORPHAN_DIR = ".orphaned"


class LocalCacheManager:
    """本地缓存管理器，用于优化云存储文件的处理性能。"""

    def __init__(self, cache_dir: str = None):
        """
        初始化缓存管理器。

        Args:
            cache_dir: 本地缓存目录路径。如果为 None，使用系统临时目录。
        """
        if cache_dir is None:
            # 使用系统临时目录
            import tempfile
            cache_dir = os.path.join(tempfile.gettempdir(), "MultTTS_Cache")

        self.cache_dir = cache_dir
        self._ensure_cache_dir()

        # 统计信息
        self.stats = {
            "copy_to_local_time": 0,
            "sync_to_cloud_time": 0,
            "files_copied": 0,
            "bytes_copied": 0,
        }

    def _ensure_cache_dir(self):
        """确保缓存目录存在。"""
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_local_path(self, cloud_path: str) -> str:
        """
        根据云端路径生成本地缓存路径。

        Args:
            cloud_path: 云端文件/文件夹路径

        Returns:
            本地缓存路径
        """
        # 使用路径的哈希值作为缓存目录名，避免路径过长
        path_hash = hashlib.md5(cloud_path.encode('utf-8')).hexdigest()[:12]
        folder_name = os.path.basename(cloud_path)
        return os.path.join(self.cache_dir, f"{folder_name}_{path_hash}")

    def copy_folder_to_local(self, cloud_path: str) -> str:
        """
        将云端文件夹复制到本地缓存。

        Args:
            cloud_path: 云端文件夹路径

        Returns:
            本地缓存路径

        Raises:
            FileNotFoundError: 云端路径不存在
            PermissionError: 没有访问权限
        """
        if not os.path.exists(cloud_path):
            raise FileNotFoundError(f"云端路径不存在: {cloud_path}")

        local_path = self._get_local_path(cloud_path)

        # 如果本地缓存已存在，检查是否需要更新
        if os.path.exists(local_path):
            logger_log("CACHE", f"本地缓存已存在: {local_path}")
            return local_path

        logger_log("CACHE", f"正在复制文件夹到本地缓存...")
        logger_log("CACHE", f"  源路径: {cloud_path}")
        logger_log("CACHE", f"  目标路径: {local_path}")

        t0 = time.time()

        try:
            # 复制整个文件夹
            shutil.copytree(cloud_path, local_path, dirs_exist_ok=True)

            elapsed = time.time() - t0
            self.stats["copy_to_local_time"] += elapsed

            # 统计复制的文件数和字节数
            file_count = 0
            byte_count = 0
            for root, dirs, files in os.walk(local_path):
                for f in files:
                    file_path = os.path.join(root, f)
                    file_count += 1
                    byte_count += os.path.getsize(file_path)

            self.stats["files_copied"] += file_count
            self.stats["bytes_copied"] += byte_count

            logger_log("CACHE", f"复制完成: {file_count} 个文件, {byte_count / 1024 / 1024:.1f} MB, 耗时 {elapsed:.1f}s")

            return local_path

        except Exception as e:
            logger_log("CACHE", f"复制失败: {e}")
            # 清理可能的部分复制
            if os.path.exists(local_path):
                try:
                    shutil.rmtree(local_path)
                except:
                    pass
            raise

    def init_skeleton(self, cloud_path: str, force: bool = False) -> str:
        """
        智能骨架复制：只同步字幕类小文件，视频/音频大文件由 ensure_file() 按需拉取。

        每次调用都会与源目录对账，而不是见到 .skeleton_ready 就直接返回：
        源目录新增/改过的字幕补进缓存，源目录已删除的字幕移出工作区（.orphaned/）。
        否则新增视频永远排不进队列，历史残留文件反而会被当成任务处理 ——
        表现就是“处理的清单和目录里的文件对不上”。

        Args:
            cloud_path: 云端文件夹路径
            force: 不信任缓存内容，所有字幕重新复制一遍

        Returns:
            本地缓存路径
        """
        if not os.path.exists(cloud_path):
            raise FileNotFoundError(f"云端路径不存在: {cloud_path}")

        local_path = self._get_local_path(cloud_path)
        os.makedirs(local_path, exist_ok=True)

        manifest_file = os.path.join(local_path, _SKELETON_MANIFEST)
        marker_file = os.path.join(local_path, _SKELETON_MARKER)
        is_first_run = not os.path.exists(marker_file)
        previous = {} if force else self._load_manifest(manifest_file)

        t0 = time.time()
        source_files = self._scan_skeleton_sources(cloud_path)

        added, updated, failed = [], [], []
        for rel, (src, size, mtime) in source_files.items():
            dst = os.path.join(local_path, rel)
            state = previous.get(rel)
            existed = os.path.exists(dst)

            if not force:
                if state:
                    try:
                        unchanged = (
                            int(state.get("size", -1)) == size
                            and abs(float(state.get("mtime", 0.0)) - mtime) < 1.0
                            and os.path.getsize(dst) == size
                        )
                    except (TypeError, ValueError, OSError):
                        unchanged = False
                else:
                    # 没有清单的老缓存：直接拿文件本身对账，避免升级后首跑全量重抄。
                    try:
                        unchanged = (
                            os.path.getsize(dst) == size
                            and abs(os.path.getmtime(dst) - mtime) < 1.0
                        )
                    except OSError:
                        unchanged = False
                if unchanged:
                    continue

            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
            except (OSError, shutil.Error) as e:
                failed.append((rel, str(e)))
                continue
            (updated if existed else added).append(rel)

        # 源目录里已经没有、缓存里还留着的字幕：隔离而不是删除。
        # 空结果时不动手 —— 云盘未挂载/目录刚清空都会走到这里，误判的代价太大。
        orphans = []
        if source_files:
            orphans = self._quarantine_orphans(local_path, source_files)
        elif not is_first_run:
            logger_log("CACHE", "源目录未扫描到任何字幕，跳过残留文件隔离（可能云盘未挂载或目录已清空）")

        # 空扫描结果不覆盖清单：云盘掉线时保住上一次的基线，恢复后不会全量重抄。
        if source_files or is_first_run:
            self._save_manifest(manifest_file, source_files)

        # 标记文件保留：用于区分“首次创建”和“增量同步”，不再作为短路条件。
        try:
            with open(marker_file, "w", encoding="utf-8") as mf:
                mf.write("ready\n")
        except OSError:
            pass

        elapsed = time.time() - t0
        self.stats["copy_to_local_time"] += elapsed
        self.stats["files_copied"] += len(added) + len(updated)

        title = "骨架缓存首次创建" if is_first_run else "骨架缓存已同步"
        logger_log("CACHE", f"{title}: {local_path}")
        logger_log("CACHE", f"  源路径: {cloud_path}")
        logger_log("CACHE", f"  字幕 {len(source_files)} 个 | 新增 {len(added)} | 更新 {len(updated)} "
                             f"| 隔离残留 {len(orphans)} | 失败 {len(failed)} | 耗时 {elapsed:.2f}s")
        for rel in added:
            logger_log("CACHE", f"   + 新增: {rel}")
        for rel in updated:
            logger_log("CACHE", f"   ~ 更新: {rel}")
        for rel in orphans:
            logger_log("CACHE", f"   - 源目录已无此文件，移入 {_SKELETON_ORPHAN_DIR}/: {rel}")
        for rel, err in failed[:5]:
            logger_log("CACHE", f"   !! 复制失败: {rel} ({err})")

        return local_path

    def _scan_skeleton_sources(self, cloud_path: str) -> dict:
        """扫描源目录中需要镜像到缓存的小文件：rel_path -> (绝对路径, 大小, mtime)。"""
        found = {}
        for root, dirs, files in os.walk(cloud_path):
            # 剪掉产物目录，避免把缓存里的成品/片段当成源文件再抄一遍。
            dirs[:] = [
                d for d in dirs
                if d not in _SKELETON_SKIP_DIRS and not d.startswith(".")
            ]
            for name in files:
                if name.startswith("."):
                    continue
                if os.path.splitext(name)[1].lower() not in _SKELETON_EXTENSIONS:
                    continue
                src = os.path.join(root, name)
                try:
                    st = os.stat(src)
                except OSError:
                    continue
                if st.st_size > _SKELETON_MAX_SIZE:
                    continue
                found[os.path.relpath(src, cloud_path)] = (src, st.st_size, st.st_mtime)
        return found

    def _quarantine_orphans(self, local_path: str, source_files: dict) -> list:
        """
        把缓存根目录里、源目录已经不存在的字幕移到 .orphaned/<时间戳>/。

        只处理根目录：任务清单就从这一层列举，子目录是处理产物不参与。
        移动而非删除——判断错了用户还能把文件拿回来。
        """
        present = {
            os.path.basename(rel) for rel in source_files
            if os.path.dirname(rel) == ""
        }
        try:
            names = os.listdir(local_path)
        except OSError:
            return []

        moved = []
        stamp_dir = None
        for name in names:
            path = os.path.join(local_path, name)
            if name.startswith(".") or not os.path.isfile(path):
                continue
            if os.path.splitext(name)[1].lower() not in _SKELETON_EXTENSIONS:
                continue
            if name in present:
                continue
            try:
                if stamp_dir is None:
                    stamp_dir = os.path.join(
                        local_path, _SKELETON_ORPHAN_DIR, time.strftime("%Y%m%d-%H%M%S")
                    )
                os.makedirs(stamp_dir, exist_ok=True)
                target = os.path.join(stamp_dir, name)
                if os.path.exists(target):
                    stem, ext = os.path.splitext(name)
                    idx = 1
                    while os.path.exists(os.path.join(stamp_dir, f"{stem}.{idx}{ext}")):
                        idx += 1
                    target = os.path.join(stamp_dir, f"{stem}.{idx}{ext}")
                shutil.move(path, target)
                moved.append(name)
            except OSError as e:
                logger_log("CACHE", f"隔离残留文件失败: {name}: {e}")
        return moved

    def _load_manifest(self, manifest_file: str) -> dict:
        try:
            with open(manifest_file, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_manifest(self, manifest_file: str, source_files: dict):
        payload = {
            rel: {"size": size, "mtime": mtime}
            for rel, (_src, size, mtime) in source_files.items()
        }
        try:
            tmp_file = manifest_file + ".tmp"
            with open(tmp_file, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=1)
            os.replace(tmp_file, manifest_file)
        except OSError as e:
            logger_log("CACHE", f"写入骨架清单失败（不影响本次处理）: {e}")

    def ensure_file(self, cloud_base: str, rel_path: str) -> str:
        """
        确保单个文件存在于本地缓存中。如果不存在，从云端复制。

        Args:
            cloud_base: 云端根目录
            rel_path: 相对于根目录的文件路径

        Returns:
            本地文件路径
        """
        local_base = self._get_local_path(cloud_base)
        local_file = os.path.join(local_base, rel_path)
        cloud_file = os.path.join(cloud_base, rel_path)

        if not os.path.exists(cloud_file):
            return local_file  # 返回预期路径，让调用方处理不存在的情况

        # 已存在的缓存文件不能直接相信：上一次复制若被强制终止，
        # shutil.copy2 可能留下一个“文件存在但内容不完整”的半成品。
        # 至少用文件大小与云端源文件核对；不一致时重新复制。
        cloud_size = os.path.getsize(cloud_file)
        if os.path.exists(local_file):
            try:
                local_size = os.path.getsize(local_file)
            except OSError:
                local_size = -1
            if local_size == cloud_size and local_size > 0:
                return local_file
            logger_log(
                "CACHE",
                f"检测到不完整/过期缓存，重新拉取: {rel_path} "
                f"(本地 {local_size} bytes / 云端 {cloud_size} bytes)"
            )

        os.makedirs(os.path.dirname(local_file), exist_ok=True)
        size_mb = cloud_size / 1024 / 1024
        logger_log("CACHE", f"正在从云端拉取文件: {rel_path} ({size_mb:.1f} MB)...")
        t0 = time.time()

        # 先复制到 .part，再校验大小，最后原子替换。
        # 避免程序被中断后留下一个看起来“已存在”的损坏 MP4。
        tmp_file = local_file + ".part"
        try:
            if os.path.exists(tmp_file):
                os.remove(tmp_file)

            # 分块复制并支持大文件进度打印
            chunk_size = 4 * 1024 * 1024  # 4MB
            copied = 0
            last_log_time = time.time()

            with open(cloud_file, "rb") as fsrc, open(tmp_file, "wb") as fdst:
                while True:
                    buf = fsrc.read(chunk_size)
                    if not buf:
                        break
                    fdst.write(buf)
                    copied += len(buf)
                    if cloud_size > 50 * 1024 * 1024 and (time.time() - last_log_time > 5.0):
                        pct = (copied / cloud_size) * 100 if cloud_size > 0 else 0
                        logger_log(
                            "CACHE",
                            f"  拉取进度: {rel_path} "
                            f"({copied / 1024 / 1024:.1f}/{size_mb:.1f} MB, {pct:.0f}%)"
                        )
                        last_log_time = time.time()

            copied_size = os.path.getsize(tmp_file)
            if copied_size != cloud_size:
                raise IOError(
                    f"文件复制不完整: {rel_path}, "
                    f"expected={cloud_size}, actual={copied_size}"
                )

            os.replace(tmp_file, local_file)
            try:
                shutil.copystat(cloud_file, local_file)
            except OSError:
                pass
        except Exception:
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except OSError:
                pass
            raise

        elapsed = time.time() - t0

        self.stats["files_copied"] += 1
        self.stats["bytes_copied"] += os.path.getsize(local_file)
        logger_log("CACHE", f"按需复制完成: {rel_path} ({size_mb:.1f} MB, {elapsed:.1f}s)")

        return local_file

    def sync_results_to_cloud(self, local_path: str, cloud_path: str, patterns: List[str] = None):
        """
        将本地处理结果同步回云端。

        Args:
            local_path: 本地缓存路径
            cloud_path: 云端目标路径
            patterns: 要同步的文件模式列表（如 ['*.wav', '*.mp4']）。如果为 None，同步所有文件。
        """
        if not os.path.exists(local_path):
            logger_log("CACHE", f"本地路径不存在，跳过同步: {local_path}")
            return

        logger_log("CACHE", f"正在同步结果到云端...")
        logger_log("CACHE", f"  本地路径: {local_path}")
        logger_log("CACHE", f"  云端路径: {cloud_path}")

        t0 = time.time()
        synced_count = 0

        try:
            if patterns is None:
                # 同步所有文件
                for root, dirs, files in os.walk(local_path):
                    for f in files:
                        local_file = os.path.join(root, f)
                        # 计算相对路径
                        rel_path = os.path.relpath(local_file, local_path)
                        cloud_file = os.path.join(cloud_path, rel_path)

                        # 确保云端目录存在
                        os.makedirs(os.path.dirname(cloud_file), exist_ok=True)

                        # 复制文件（如果本地文件更新）
                        if not os.path.exists(cloud_file) or \
                           os.path.getmtime(local_file) > os.path.getmtime(cloud_file):
                            shutil.copy2(local_file, cloud_file)
                            synced_count += 1
            else:
                # 只同步匹配模式的文件
                import fnmatch
                for root, dirs, files in os.walk(local_path):
                    for f in files:
                        if any(fnmatch.fnmatch(f, p) for p in patterns):
                            local_file = os.path.join(root, f)
                            rel_path = os.path.relpath(local_file, local_path)
                            cloud_file = os.path.join(cloud_path, rel_path)

                            os.makedirs(os.path.dirname(cloud_file), exist_ok=True)

                            if not os.path.exists(cloud_file) or \
                               os.path.getmtime(local_file) > os.path.getmtime(cloud_file):
                                shutil.copy2(local_file, cloud_file)
                                synced_count += 1

            elapsed = time.time() - t0
            self.stats["sync_to_cloud_time"] += elapsed
            logger_log("CACHE", f"同步完成: {synced_count} 个文件, 耗时 {elapsed:.1f}s")

        except Exception as e:
            logger_log("CACHE", f"同步失败: {e}")
            raise

    def cleanup(self, local_path: str = None):
        """
        清理本地缓存。

        Args:
            local_path: 要清理的特定路径。如果为 None，清理所有缓存。
        """
        if local_path:
            if os.path.exists(local_path):
                try:
                    shutil.rmtree(local_path)
                    logger_log("CACHE", f"已清理缓存: {local_path}")
                except Exception as e:
                    logger_log("CACHE", f"清理失败: {e}")
        else:
            # 清理所有缓存
            if os.path.exists(self.cache_dir):
                try:
                    shutil.rmtree(self.cache_dir)
                    logger_log("CACHE", f"已清理所有缓存: {self.cache_dir}")
                except Exception as e:
                    logger_log("CACHE", f"清理失败: {e}")

    def get_stats(self) -> dict:
        """获取统计信息。"""
        return {
            **self.stats,
            "cache_dir": self.cache_dir,
        }

    def print_stats(self):
        """打印统计信息。"""
        stats = self.get_stats()
        logger_log("CACHE", "=== 缓存统计 ===")
        logger_log("CACHE", f"  缓存目录: {stats['cache_dir']}")
        logger_log("CACHE", f"  复制到本地耗时: {stats['copy_to_local_time']:.1f}s")
        logger_log("CACHE", f"  同步到云端耗时: {stats['sync_to_cloud_time']:.1f}s")
        logger_log("CACHE", f"  复制文件数: {stats['files_copied']}")
        logger_log("CACHE", f"  复制字节数: {stats['bytes_copied'] / 1024 / 1024:.1f} MB")


def is_cloud_path(path: str) -> bool:
    """
    判断路径是否为云存储路径。

    Args:
        path: 文件路径

    Returns:
        是否为云存储路径
    """
    # 检查常见的云存储路径特征
    cloud_indicators = [
        "我的云端硬盘",  # Google Drive 中文
        "My Drive",  # Google Drive 英文
        "Google Drive",  # Google Drive 英文
        "OneDrive",  # OneDrive
        "Dropbox",  # Dropbox
        "百度网盘",  # 百度网盘
        "阿里云盘",  # 阿里云盘
    ]

    path_upper = path.upper()
    return any(indicator.upper() in path_upper for indicator in cloud_indicators)


def get_cache_manager(cache_dir: str = None) -> LocalCacheManager:
    """
    获取缓存管理器实例。

    Args:
        cache_dir: 缓存目录路径

    Returns:
        LocalCacheManager 实例
    """
    return LocalCacheManager(cache_dir)
