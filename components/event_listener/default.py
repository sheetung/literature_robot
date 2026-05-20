from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from langbot_plugin.api.definition.components.common.event_listener import EventListener
from langbot_plugin.api.entities import events, context
from langbot_plugin.api.entities.builtin.platform import message as platform_message


def debug(msg: str) -> None:
    print(f"[literature_robot] {msg}", file=sys.stderr, flush=True)


project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from core.robot import (  # noqa: E402
    Candidate,
    RobotConfig,
    candidate_summary,
    load_records,
    monitor_once,
    publish_ablesci_request,
    resolve_plugin_path,
    title_confidence,
    try_open_download,
)


PLUGIN_ROOT = Path(project_root)
STORAGE_KEY_JOBS = "literature_robot:jobs"
ACTIVE_STATUSES = {"waiting", "running"}
CACHE_INDEX_FILE = "data/literature_robot/cache_index.json"
CACHE_MAX_SIZE = 10
CACHE_MATCH_THRESHOLD = 0.7
RECORDS_FILE = "data/literature_robot/ablesci_records.json"


class DefaultEventListener(EventListener):
    def __init__(self):
        super().__init__()
        self._jobs_lock = asyncio.Lock()
        self._monitor_tasks: dict[str, asyncio.Task] = {}

    async def initialize(self):
        await super().initialize()

        @self.handler(events.PersonMessageReceived)
        @self.handler(events.GroupMessageReceived)
        async def handler(event_context: context.EventContext):
            message_chain = event_context.event.message_chain
            text = "".join(
                element.text for element in message_chain
                if isinstance(element, platform_message.Plain)
            ).strip()
            print(f"[literature_robot] received message: {text}", file=sys.stderr, flush=True)

            if not text.startswith("lit"):
                return

            event_context.prevent_default()
            args = text[3:].strip()
            await self._handle_command(event_context, args)

        await self._resume_jobs()

    def __del__(self):
        for task in self._monitor_tasks.values():
            if not task.done():
                task.cancel()

    # ========== Command routing ==========

    async def _handle_command(self, event_context: context.EventContext, args: str) -> None:
        if not args:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=self._help_text()),
            ]))
            return

        space_idx = args.find(" ")
        if space_idx == -1:
            subcmd = args.lower()
            arg = ""
        else:
            subcmd = args[:space_idx].lower()
            arg = args[space_idx + 1:].strip()

        if subcmd in ("help", "h"):
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=self._help_text()),
            ]))
        elif subcmd == "open":
            await self._handle_open(event_context, arg)
        elif subcmd == "monitor":
            await self._handle_monitor(event_context, arg)
        elif subcmd == "once":
            await self._handle_once(event_context, arg)
        elif subcmd in ("status", "jobs", "list"):
            await self._handle_status(event_context, arg)
        elif subcmd == "reset":
            await self._handle_reset(event_context)
        else:
            await self._handle_title_cmd(event_context, args)

    async def _handle_open(self, event_context: context.EventContext, title: str) -> None:
        debug(f"cmd=open title={title!r}")
        if not title:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text="用法：lit open <论文标题>"),
            ]))
            return
        await event_context.reply(platform_message.MessageChain([
            platform_message.Plain(text=f"正在搜索「{title}」的开放 PDF，请稍候..."),
        ]))
        text, file_path = await self._handle_title(event_context, title, publish=False)
        if file_path:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=text),
                platform_message.File(url=f"file://{file_path}", name=Path(file_path).name),
            ]))
            return
        await event_context.reply(platform_message.MessageChain([
            platform_message.Plain(text=text),
        ]))

    async def _handle_monitor(self, event_context: context.EventContext, detail_url: str) -> None:
        cfg = self._config()
        debug(f"cmd=monitor cookie_set=bool({bool(cfg.cookie)})")
        access_error = self._access_error(event_context, cfg)
        if access_error:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=access_error),
            ]))
            return
        if not detail_url:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text="用法：lit monitor <科研通求助详情页URL>"),
            ]))
            return
        if not cfg.cookie:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text="请先在 WebUI 的 literature_robot 插件配置中填写科研通 Cookie。"),
            ]))
            return
        await self._flush_pending_notifications()
        job = self._new_job(
            title="手动监控任务",
            detail_url=detail_url,
            points=cfg.default_points,
            candidate=Candidate(source="manual", title="手动监控任务"),
            cfg=cfg,
        )
        job["notify_bot_uuid"] = await event_context.get_bot_uuid()
        job["notify_target_type"] = event_context.event.launcher_type
        job["notify_target_id"] = str(event_context.event.launcher_id)
        await self._set_job(job)
        self._ensure_monitor_task(job["id"])
        await event_context.reply(platform_message.MessageChain([
            platform_message.Plain(text=f"已加入后台监控。\n任务ID：{job['id']}\n详情页：{detail_url}"),
        ]))

    async def _handle_once(self, event_context: context.EventContext, detail_url: str) -> None:
        cfg = self._config()
        debug(f"cmd=once detail_url={detail_url!r}")
        access_error = self._access_error(event_context, cfg)
        if access_error:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=access_error),
            ]))
            return
        if not detail_url:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text="用法：lit once <科研通求助详情页URL>"),
            ]))
            return
        if not cfg.cookie:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text="请先在 WebUI 的 literature_robot 插件配置中填写科研通 Cookie。"),
            ]))
            return
        out_dir = resolve_plugin_path(PLUGIN_ROOT, cfg.download_dir)
        records_path = resolve_plugin_path(PLUGIN_ROOT, RECORDS_FILE)
        await event_context.reply(platform_message.MessageChain([
            platform_message.Plain(text="正在检查科研通详情页并尝试下载附件。"),
        ]))
        try:
            result = await asyncio.to_thread(
                monitor_once,
                detail_url,
                cfg.cookie,
                out_dir,
                records_path,
                cfg.request_timeout_seconds,
            )
        except Exception as exc:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=f"检查失败：{exc}"),
            ]))
            return
        if result.done:
            if result.status == "closed":
                await event_context.reply(platform_message.MessageChain([
                    platform_message.Plain(text="该科研通求助已关闭，无法下载。"),
                ]))
            else:
                chain = [platform_message.Plain(text=f"下载完成：{result.file_path}\n状态：{result.status}")]
                if result.file_path:
                    chain.append(
                        platform_message.File(url=f"file://{result.file_path}", name=Path(result.file_path).name),
                    )
                await event_context.reply(platform_message.MessageChain(chain))
        else:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=f"暂未发现可下载 PDF。\n链接数：{result.link_count}\n详情：{result.message}"),
            ]))

    async def _handle_status(self, event_context: context.EventContext, job_id: str) -> None:
        await self._flush_pending_notifications()
        cfg = self._config()
        debug(f"cmd=status params={job_id}")
        access_error = self._access_error(event_context, cfg)
        if access_error:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=access_error),
            ]))
            return
        jobs = await self._load_jobs()
        if not jobs:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text="暂无 literature_robot 任务。"),
            ]))
            return
        if job_id:
            job = jobs.get(job_id)
            if not job:
                await event_context.reply(platform_message.MessageChain([
                    platform_message.Plain(text=f"未找到任务：{job_id}"),
                ]))
                return
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=self._format_job_detail(job)),
            ]))
            return
        recent = sorted(jobs.values(), key=lambda item: item.get("created_at", 0), reverse=True)[:8]
        lines = ["literature_robot 最近任务："]
        lines.extend(self._format_job_line(job) for job in recent)
        await event_context.reply(platform_message.MessageChain([
            platform_message.Plain(text="\n".join(lines)),
        ]))

    async def _handle_reset(self, event_context: context.EventContext) -> None:
        cfg = self._config()
        access_error = self._access_error(event_context, cfg)
        if access_error:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=access_error),
            ]))
            return
        for job_id, task in list(self._monitor_tasks.items()):
            if not task.done():
                task.cancel()
        self._monitor_tasks.clear()
        await self._save_jobs({})
        cache_path = self._cache_index_path()
        if cache_path.exists():
            cache_path.unlink()
        debug("cmd=reset: cleared all jobs, tasks, and cache")
        await event_context.reply(platform_message.MessageChain([
            platform_message.Plain(text="已清空所有任务记录、监控任务和本地缓存。"),
        ]))

    async def _handle_title_cmd(self, event_context: context.EventContext, title: str) -> None:
        await self._flush_pending_notifications()
        debug(f"cmd=title title={title!r}")
        if not title:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=self._help_text()),
            ]))
            return
        await event_context.reply(platform_message.MessageChain([
            platform_message.Plain(text=f"正在搜索「{title}」的 PDF 并检查本地缓存，请稍候..."),
        ]))
        text, file_path = await self._handle_title(event_context, title, publish=True)
        if file_path:
            await event_context.reply(platform_message.MessageChain([
                platform_message.Plain(text=text),
                platform_message.File(url=f"file://{file_path}", name=Path(file_path).name),
            ]))
            return
        await event_context.reply(platform_message.MessageChain([
            platform_message.Plain(text=text),
        ]))

    # ========== 本地缓存（最多 CACHE_MAX_SIZE 篇）==========

    def _cache_index_path(self) -> Path:
        return resolve_plugin_path(PLUGIN_ROOT, CACHE_INDEX_FILE)

    def _load_cache(self) -> list[dict[str, Any]]:
        path = self._cache_index_path()
        if not path.exists():
            return []
        try:
            raw = path.read_text("utf-8")
            entries = json.loads(raw)
            return list(entries) if isinstance(entries, list) else []
        except Exception as exc:
            debug(f"_load_cache: failed: {exc}")
            return []

    def _save_cache(self, entries: list[dict[str, Any]]) -> None:
        path = self._cache_index_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, ensure_ascii=False), "utf-8")

    def _search_cache(self, title: str, download_dir: Path | None = None) -> str | None:
        entries = self._load_cache()
        best_score = CACHE_MATCH_THRESHOLD
        best_path: str | None = None
        for entry in entries:
            cached_title = entry.get("title", "")
            fp = entry.get("file_path", "")
            if not cached_title or not fp:
                continue
            if not Path(fp).exists():
                continue
            score = title_confidence(title, cached_title)
            if score > best_score:
                best_score = score
                best_path = fp

        if best_path:
            debug(f"_search_cache: index match score={best_score:.3f} path={best_path}")
            return best_path

        if download_dir and download_dir.is_dir():
            debug(f"_search_cache: scanning download dir {download_dir}")
            for pdf_file in sorted(download_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if pdf_file.suffix.lower() != ".pdf":
                    continue
                file_title = pdf_file.stem
                score = title_confidence(title, file_title)
                if score > best_score:
                    best_score = score
                    best_path = str(pdf_file)
            if best_path:
                debug(f"_search_cache: dir scan match score={best_score:.3f} path={best_path}")
                self._add_to_cache(Path(best_path).stem, best_path)

        if not best_path:
            records_path = resolve_plugin_path(PLUGIN_ROOT, RECORDS_FILE)
            if records_path.exists():
                records = load_records(records_path)
                for entry in records:
                    fp = entry.get("file_path", "")
                    entry_title = entry.get("title", "")
                    if not fp or not entry_title or not Path(fp).exists():
                        continue
                    score = title_confidence(title, entry_title)
                    if score > best_score:
                        best_score = score
                        best_path = fp
                if best_path:
                    debug(f"_search_cache: records match score={best_score:.3f} path={best_path}")

        if not best_path:
            debug(f"_search_cache: no match (best={best_score:.3f} < threshold={CACHE_MATCH_THRESHOLD})")
        return best_path

    def _add_to_cache(self, title: str, file_path: str) -> None:
        entries = self._load_cache()
        entries = [e for e in entries if e.get("file_path") != file_path]
        entries.insert(0, {"title": title, "file_path": file_path, "added_at": int(time.time())})
        while len(entries) > CACHE_MAX_SIZE:
            removed = entries.pop()
            debug(f"_add_to_cache: evicted {removed.get('title','')}")
        self._save_cache(entries)
        debug(f"_add_to_cache: cached ({len(entries)}/{CACHE_MAX_SIZE}) {title}")

    # ========== 配置与帮助 ==========

    def _config(self) -> RobotConfig:
        debug("_config: calling plugin.get_config()")
        try:
            raw = self.plugin.get_config()
            debug(f"_config: get_config returned type={type(raw).__name__}")
        except Exception as exc:
            debug(f"_config: get_config failed: {exc}")
            raw = {}
        cfg = RobotConfig.from_dict(raw if isinstance(raw, dict) else {})
        debug(f"_config: enabled={cfg.enabled} cookie_set={bool(cfg.cookie)} auto_publish={cfg.auto_publish} points={cfg.default_points} interval={cfg.monitor_interval_seconds}s timeout={cfg.request_timeout_seconds}s max_hours={cfg.max_hours}h proxy={cfg.proxy or 'none'}")
        return cfg

    def _help_text(self) -> str:
        return (
            "文献下载机器人\n"
            "用法：\n"
            "  lit <论文标题>                   - 查开放 PDF；找不到则发布科研通求助并后台监控\n"
            "  lit open <论文标题>              - 只尝试开放 PDF 下载\n"
            "  lit monitor <详情页URL>          - 监控已有科研通求助\n"
            "  lit once <详情页URL>             - 立即检查一次详情页并下载附件\n"
            "  lit status                       - 查看后台任务\n"
            "  lit reset                       - 清空所有任务记录和缓存\n"
            "  lit help                         - 显示帮助\n"
            "请在 WebUI 插件配置中填写科研通 Cookie；默认悬赏点数也在 WebUI 配置。"
        )

    def _sender_id(self, event_context: context.EventContext) -> str | None:
        sid = event_context.event.sender_id
        if sid is not None and sid != 0:
            return str(sid)
        return None

    def _group_id(self, event_context: context.EventContext) -> str | None:
        if event_context.event.launcher_type == "group":
            return str(event_context.event.launcher_id)
        return None

    def _access_error(self, event_context: context.EventContext, cfg: RobotConfig) -> str | None:
        if not cfg.enabled:
            return "literature_robot 当前未启用，请在 WebUI 插件配置中开启。"
        sender = self._sender_id(event_context)
        if cfg.allowed_user_ids and (not sender or sender not in cfg.allowed_user_ids):
            return "你不在 literature_robot 允许用户列表中，请联系管理员在 WebUI 插件配置中添加。"
        group_id = self._group_id(event_context)
        if group_id and cfg.allowed_group_ids and group_id not in cfg.allowed_group_ids:
            return "当前群不在 literature_robot 允许群列表中，请联系管理员在 WebUI 插件配置中添加。"
        return None

    async def _handle_title(self, event_context: context.EventContext, title: str, publish: bool) -> tuple[str, str | None]:
        cfg = self._config()
        access_error = self._access_error(event_context, cfg)
        if access_error:
            return access_error, None

        out_dir = resolve_plugin_path(PLUGIN_ROOT, cfg.download_dir)

        cached_path = self._search_cache(title, download_dir=out_dir)
        if cached_path:
            debug(f"_handle_title: cache hit, sending file {cached_path}")
            return (
                "本地缓存命中，已发送文件。\n"
                f"文件：{cached_path}",
                cached_path,
            )
        debug(f"_handle_title: resolving title={title!r} publish={publish} out_dir={out_dir} proxy={cfg.proxy or 'none'}")
        try:
            downloaded, candidate, file_path, open_status = await asyncio.to_thread(
                try_open_download,
                title,
                out_dir,
                cfg.request_timeout_seconds,
                cfg.search_rows,
                cfg.open_pdf_confidence,
                cfg.proxy,
            )
        except Exception as exc:
            debug(f"_handle_title: try_open_download raised {exc}")
            candidate = Candidate(source="none", title=title, confidence=0.0, message=str(exc))
            downloaded = False
            file_path = ""
            open_status = f"open_lookup_failed: {exc}"

        debug(f"_handle_title: downloaded={downloaded} candidate.source={candidate.source} candidate.confidence={candidate.confidence:.3f} candidate.pdf_url={bool(candidate.pdf_url)} open_status={open_status}")
        if downloaded:
            self._add_to_cache(candidate.title or title, file_path)
            return (
                "开放 PDF 已下载。\n"
                f"文件：{file_path}\n"
                f"匹配：{candidate_summary(candidate)}",
                file_path,
            )

        if not publish or not cfg.auto_publish:
            return (
                "未下载到可信开放 PDF。\n"
                f"状态：{open_status}\n"
                f"最佳匹配：{candidate_summary(candidate)}",
                None,
            )

        if not cfg.cookie:
            debug("_handle_title: no cookie configured, aborting publish")
            return (
                "未下载到可信开放 PDF，且未配置科研通 Cookie，无法发布求助。\n"
                f"最佳匹配：{candidate_summary(candidate)}\n"
                "请先在 WebUI 的 literature_robot 插件配置中填写科研通 Cookie。",
                None,
            )

        debug(f"_handle_title: publishing to ablesci ... points={cfg.default_points}")
        try:
            detail_url = await asyncio.to_thread(
                publish_ablesci_request,
                candidate,
                title,
                cfg.default_points,
                cfg.cookie,
                cfg.request_timeout_seconds,
            )
            debug(f"_handle_title: ablesci detail_url={detail_url}")
        except Exception as exc:
            return (
                "未下载到可信开放 PDF，发布科研通求助失败。\n"
                f"错误：{exc}\n"
                f"最佳匹配：{candidate_summary(candidate)}",
                None,
            )

        job = self._new_job(title, detail_url, cfg.default_points, candidate, cfg)
        job["notify_bot_uuid"] = await event_context.get_bot_uuid()
        job["notify_target_type"] = event_context.event.launcher_type
        job["notify_target_id"] = str(event_context.event.launcher_id)
        await self._set_job(job)
        self._ensure_monitor_task(job["id"])
        return (
            "未下载到可信开放 PDF，已发布科研通求助并开始后台监控。\n"
            f"任务ID：{job['id']}\n"
            f"详情页：{detail_url}\n"
            f"悬赏：{cfg.default_points} 点\n"
            f"最佳匹配：{candidate_summary(candidate)}\n"
            "后续可用 lit status 查看下载状态。",
            None,
        )

    def _new_job(
        self,
        title: str,
        detail_url: str,
        points: int,
        candidate: Candidate,
        cfg: RobotConfig,
    ) -> dict[str, Any]:
        now = int(time.time())
        digest = hashlib.sha1(f"{title}\n{detail_url}\n{now}".encode("utf-8")).hexdigest()[:8]
        return {
            "id": f"{now}-{digest}",
            "title": title,
            "detail_url": detail_url,
            "points": points,
            "candidate": candidate.__dict__,
            "status": "waiting",
            "created_at": now,
            "deadline_at": now + cfg.max_seconds,
            "last_checked_at": 0,
            "last_message": "",
            "last_error": "",
            "file_path": "",
            "link_count": 0,
        }

    async def _storage_has_key(self, key: str) -> bool:
        try:
            keys = await self.plugin.get_plugin_storage_keys()
            return key in keys
        except Exception:
            return False

    def _storage_to_text(self, raw: Any) -> str:
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        if isinstance(raw, bytearray):
            return bytes(raw).decode("utf-8")
        return str(raw)

    async def _load_jobs(self) -> dict[str, dict[str, Any]]:
        try:
            if not await self._storage_has_key(STORAGE_KEY_JOBS):
                return {}
            raw = await self.plugin.get_plugin_storage(STORAGE_KEY_JOBS)
            if not raw:
                return {}
            data = json.loads(self._storage_to_text(raw))
            if not isinstance(data, dict):
                return {}
            return {str(key): value for key, value in data.items() if isinstance(value, dict)}
        except Exception:
            return {}

    async def _save_jobs(self, jobs: dict[str, dict[str, Any]]) -> None:
        data = json.dumps(jobs, ensure_ascii=False).encode("utf-8")
        await self.plugin.set_plugin_storage(STORAGE_KEY_JOBS, data)

    async def _set_job(self, job: dict[str, Any]) -> None:
        async with self._jobs_lock:
            jobs = await self._load_jobs()
            jobs[str(job["id"])] = job
            await self._save_jobs(jobs)

    async def _get_job(self, job_id: str) -> dict[str, Any] | None:
        jobs = await self._load_jobs()
        return jobs.get(job_id)

    async def _update_job(self, job_id: str, **updates: Any) -> dict[str, Any] | None:
        async with self._jobs_lock:
            jobs = await self._load_jobs()
            job = jobs.get(job_id)
            if not job:
                return None
            job.update(updates)
            jobs[job_id] = job
            await self._save_jobs(jobs)
            return job

    async def _resume_jobs(self) -> None:
        jobs = await self._load_jobs()
        debug(f"_resume_jobs: loaded {len(jobs)} jobs")
        changed = False
        now = int(time.time())
        for job_id, job in jobs.items():
            if job.get("status") in ACTIVE_STATUSES and int(job.get("deadline_at") or 0) <= now:
                debug(f"_resume_jobs: {job_id} expired, marking timeout")
                job["status"] = "timeout"
                job["last_message"] = "超过最大监控时长，未发现可下载 PDF。"
                changed = True
            elif job.get("status") in ACTIVE_STATUSES:
                self._ensure_monitor_task(job_id)
        if changed:
            await self._save_jobs(jobs)

    def _ensure_monitor_task(self, job_id: str) -> None:
        task = self._monitor_tasks.get(job_id)
        if task and not task.done():
            return
        debug(f"_ensure_monitor_task: starting monitor for {job_id}")
        self._monitor_tasks[job_id] = asyncio.create_task(self._monitor_job(job_id))

    async def _monitor_job(self, job_id: str) -> None:
        debug(f"_monitor_job: start job_id={job_id}")
        while True:
            job = await self._get_job(job_id)
            if not job or job.get("status") not in ACTIVE_STATUSES:
                debug(f"_monitor_job: {job_id} status={job.get('status') if job else 'no_job'} -> exit")
                return

            cfg = self._config()
            now = int(time.time())
            deadline = int(job.get("deadline_at") or now)
            if now >= deadline:
                debug(f"_monitor_job: {job_id} deadline passed, timeout")
                await self._update_job(
                    job_id,
                    status="timeout",
                    last_checked_at=now,
                    last_message="超过最大监控时长，未发现可下载 PDF。",
                )
                return

            if not cfg.cookie:
                await self._update_job(
                    job_id,
                    status="waiting",
                    last_checked_at=now,
                    last_error="科研通 Cookie 未配置，等待 WebUI 配置更新。",
                )
                await asyncio.sleep(min(cfg.monitor_interval_seconds, max(1, deadline - now)))
                continue

            out_dir = resolve_plugin_path(PLUGIN_ROOT, cfg.download_dir)
            records_path = resolve_plugin_path(PLUGIN_ROOT, RECORDS_FILE)
            await self._update_job(job_id, status="running", last_checked_at=now, last_error="")

            debug(f"_monitor_job: {job_id} checking {job.get('detail_url','')}")
            try:
                result = await asyncio.to_thread(
                    monitor_once,
                    str(job["detail_url"]),
                    cfg.cookie,
                    out_dir,
                    records_path,
                    cfg.request_timeout_seconds,
                )
                debug(f"_monitor_job: {job_id} result done={result.done} status={result.status} links={result.link_count} file={result.file_path}")
            except Exception as exc:
                debug(f"_monitor_job: {job_id} exception={exc}")
                await self._update_job(
                    job_id,
                    status="waiting",
                    last_checked_at=int(time.time()),
                    last_error=str(exc),
                )
            else:
                if result.done:
                    if result.status == "closed":
                        debug(f"_monitor_job: {job_id} closed")
                        closed_text = "监控任务已停止：该科研通求助已关闭。"
                        sent = await self._send_notification(job_id, job, text=closed_text)
                        await self._update_job(
                            job_id,
                            status="closed",
                            last_checked_at=int(time.time()),
                            last_message=result.message,
                            link_count=result.link_count,
                            last_error="",
                            pending_notification=None if sent else json.dumps({"text": closed_text}),
                        )
                        return
                    debug(f"_monitor_job: {job_id} completed file={result.file_path}")
                    dl_text = "科研通 PDF 已下载。"
                    sent = await self._send_notification(job_id, job, text=dl_text, file_path=result.file_path)
                    await self._update_job(
                        job_id,
                        status=result.status,
                        last_checked_at=int(time.time()),
                        last_message=result.message,
                        file_path=result.file_path,
                        link_count=result.link_count,
                        last_error="",
                        pending_notification=None if sent else json.dumps({"text": dl_text, "file_path": result.file_path}),
                    )
                    return
                await self._update_job(
                    job_id,
                    status="waiting",
                    last_checked_at=int(time.time()),
                    last_message=result.message,
                    link_count=result.link_count,
                    last_error="",
                )

            remaining = max(1, deadline - int(time.time()))
            await asyncio.sleep(min(cfg.monitor_interval_seconds, remaining))

    async def _send_notification(
        self, job_id: str, job: dict[str, Any] | None,
        text: str, file_path: str | None = None,
    ) -> bool:
        bot_uuid = (job or {}).get("notify_bot_uuid", "")
        target_type = (job or {}).get("notify_target_type", "")
        target_id = (job or {}).get("notify_target_id", "")
        if not bot_uuid or not target_type or not target_id:
            debug(f"_send_notification: missing notify fields for {job_id}")
            return False
        title = (job or {}).get("title", "")
        detail_url = (job or {}).get("detail_url", "")
        msg_text = (
            f"{text}\n"
            f"任务ID：{job_id}\n"
            f"标题：{title}\n"
            f"详情页：{detail_url}"
        )
        chain = [platform_message.Plain(text=msg_text)]
        if file_path:
            chain.append(
                platform_message.File(url=f"file://{file_path}", name=Path(file_path).name),
            )
        try:
            await self.plugin.send_message(
                bot_uuid=bot_uuid,
                target_type=target_type,
                target_id=target_id,
                message_chain=platform_message.MessageChain(chain),
            )
            debug(f"_send_notification: sent for {job_id}")
            return True
        except Exception as exc:
            debug(f"_send_notification: failed for {job_id}: {exc}")
            return False


    async def _flush_pending_notifications(self) -> None:
        jobs = await self._load_jobs()
        changed = False
        for job_id, job in list(jobs.items()):
            raw = job.get("pending_notification")
            if not raw:
                continue
            try:
                data = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                continue
            sent = await self._send_notification(
                job_id, job,
                text=data.get("text", ""),
                file_path=data.get("file_path"),
            )
            if sent:
                job.pop("pending_notification", None)
                changed = True
        if changed:
            await self._save_jobs(jobs)

    def _format_time(self, timestamp: Any) -> str:
        try:
            value = int(timestamp)
        except (TypeError, ValueError):
            return "-"
        if value <= 0:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))

    def _format_job_line(self, job: dict[str, Any]) -> str:
        title = str(job.get("title") or "").strip()
        if len(title) > 48:
            title = title[:45] + "..."
        status = job.get("status", "unknown")
        checked = self._format_time(job.get("last_checked_at"))
        return f"- {job.get('id')} [{status}] {title}（最后检查：{checked}）"

    def _format_job_detail(self, job: dict[str, Any]) -> str:
        lines = [
            f"任务ID：{job.get('id')}",
            f"状态：{job.get('status', 'unknown')}",
            f"标题：{job.get('title', '')}",
            f"详情页：{job.get('detail_url', '')}",
            f"悬赏：{job.get('points', '')} 点",
            f"创建时间：{self._format_time(job.get('created_at'))}",
            f"截止时间：{self._format_time(job.get('deadline_at'))}",
            f"最后检查：{self._format_time(job.get('last_checked_at'))}",
            f"发现链接数：{job.get('link_count', 0)}",
        ]
        if job.get("file_path"):
            lines.append(f"文件：{job['file_path']}")
        if job.get("last_error"):
            lines.append(f"最近错误：{job['last_error']}")
        if job.get("last_message"):
            lines.append(f"最近记录：{job['last_message']}")
        return "\n".join(lines)
