"""
网络诊断与优化系统 - 核心模块

功能：
- 延迟测量：多发 ICMP/TCP 探测取平均值，默认测游戏服务器
- 丢包率测量：区分上行/下行，支持时间范围
- 带宽测试：上传/下载速度，扣除游戏占用带宽，带进度回调
- 网络优化建议：按实际测量结果生成，带优先级
- 连接历史：记录、统计与网络变差告警
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
import math
import socket
import time


@dataclass
class Config:
    pass


_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _default_ping(host: str, timeout: float) -> Optional[float]:
    """默认探测：TCP 连接计时（无需 root 权限）。失败返回 None 表示丢包。"""
    start = time.perf_counter()
    try:
        with socket.create_connection((host, 80), timeout=timeout):
            return (time.perf_counter() - start) * 1000.0
    except OSError:
        return None


def _default_bandwidth_probe(progress_callback: Callable[[float], None]) -> float:
    """默认带宽探测：无真实测速服务端时返回 0，实际部署应注入真实测速函数。"""
    progress_callback(1.0)
    return 0.0


class NetworkDiagnostic:
    DEFAULT_GAME_SERVER = "game.server.local"

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}
        self._state = {}
        self._history: List[Dict] = []
        self._alerts: List[Dict] = []

        # 可注入的依赖（便于测试与真实环境切换）
        self._ping = self.config.get("ping_func", _default_ping)
        self._download_probe = self.config.get(
            "download_probe_func", lambda host: True
        )
        self._download_test = self.config.get(
            "download_test_func", _default_bandwidth_probe
        )
        self._upload_test = self.config.get(
            "upload_test_func", _default_bandwidth_probe
        )
        self._sleep = self.config.get("sleep_func", time.sleep)
        self._time = self.config.get("time_func", time.time)

        # 阈值配置
        self._latency_warn_ms = self.config.get("latency_warn_ms", 100.0)
        self._latency_bad_ms = self.config.get("latency_bad_ms", 200.0)
        self._latency_alert_ms = self.config.get("latency_alert_ms", 150.0)
        self._jitter_warn_ms = self.config.get("jitter_warn_ms", 30.0)
        self._loss_warn_percent = self.config.get("loss_warn_percent", 2.0)
        self._loss_bad_percent = self.config.get("loss_bad_percent", 5.0)
        self._loss_alert_percent = self.config.get("loss_alert_percent", 5.0)
        self._min_game_bandwidth_mbps = self.config.get("min_game_bandwidth_mbps", 4.0)

    # ------------------------------------------------------------------
    # 基础生命周期
    # ------------------------------------------------------------------
    def update(self, dt: float):
        """清理超出保留窗口的历史记录。"""
        retention = self.config.get("history_retention_seconds", 3600.0)
        cutoff = self._time() - retention
        self._history = [
            r for r in self._history if r.get("timestamp", 0) >= cutoff
        ]

    def reset(self):
        self._state = {}
        self._history = []
        self._alerts = []

    # ------------------------------------------------------------------
    # 延迟测量：多发取平均值，默认测游戏服务器
    # ------------------------------------------------------------------
    def measure_latency(
        self,
        host: Optional[str] = None,
        count: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Dict:
        host = host or self.config.get("game_server", self.DEFAULT_GAME_SERVER)
        count = count or self.config.get("ping_count", 5)
        timeout = timeout or self.config.get("ping_timeout", 2.0)

        rtts: List[float] = []
        sent = 0
        for _ in range(count):
            sent += 1
            rtt = self._ping(host, timeout)
            if rtt is not None:
                rtts.append(rtt)

        received = len(rtts)
        lost = sent - received
        loss_percent = lost / sent * 100.0 if sent else 0.0

        if received:
            avg_ms = sum(rtts) / received
            min_ms = min(rtts)
            max_ms = max(rtts)
            variance = sum((r - avg_ms) ** 2 for r in rtts) / received
            jitter_ms = math.sqrt(variance)
        else:
            # 全部丢包时延迟未知，绝不能显示 0ms
            avg_ms = min_ms = max_ms = jitter_ms = None

        result = {
            "host": host,
            "sent": sent,
            "received": received,
            "avg_ms": avg_ms,
            "min_ms": min_ms,
            "max_ms": max_ms,
            "jitter_ms": jitter_ms,
            "loss_percent": loss_percent,
        }
        self._record({
            "type": "latency",
            "timestamp": self._time(),
            "host": host,
            "avg_ms": avg_ms,
            "loss_percent": loss_percent,
        })
        return result

    # ------------------------------------------------------------------
    # 丢包率测量：区分上行/下行，支持时间范围
    # ------------------------------------------------------------------
    def measure_packet_loss(
        self,
        host: Optional[str] = None,
        duration: Optional[float] = None,
        interval: Optional[float] = None,
        direction: str = "both",
    ) -> Dict:
        host = host or self.config.get("game_server", self.DEFAULT_GAME_SERVER)
        duration = duration if duration is not None else self.config.get(
            "loss_test_duration", 10.0
        )
        interval = interval or self.config.get("loss_test_interval", 1.0)
        timeout = self.config.get("ping_timeout", 2.0)
        if direction not in ("both", "upload", "download"):
            raise ValueError("direction 必须是 'both'、'upload' 或 'download'")

        probes = max(1, int(round(duration / interval)))
        upload_sent = upload_lost = 0
        download_sent = download_lost = 0

        start = self._time()
        for i in range(probes):
            if direction in ("both", "upload"):
                upload_sent += 1
                if self._ping(host, timeout) is None:
                    upload_lost += 1
            if direction in ("both", "download"):
                download_sent += 1
                if not self._download_probe(host):
                    download_lost += 1
            if i < probes - 1:
                self._sleep(interval)
        end = self._time()

        upload_loss = upload_lost / upload_sent * 100.0 if upload_sent else 0.0
        download_loss = (
            download_lost / download_sent * 100.0 if download_sent else 0.0
        )
        total_sent = upload_sent + download_sent
        total_lost = upload_lost + download_lost
        overall_loss = total_lost / total_sent * 100.0 if total_sent else 0.0

        result = {
            "host": host,
            "direction": direction,
            "window": {"start": start, "end": end, "seconds": duration},
            "upload": {
                "sent": upload_sent,
                "lost": upload_lost,
                "loss_percent": upload_loss,
            },
            "download": {
                "sent": download_sent,
                "lost": download_lost,
                "loss_percent": download_loss,
            },
            "loss_percent": overall_loss,
        }
        self._record({
            "type": "packet_loss",
            "timestamp": end,
            "host": host,
            "loss_percent": overall_loss,
            "upload_loss_percent": upload_loss,
            "download_loss_percent": download_loss,
        })
        return result

    # ------------------------------------------------------------------
    # 带宽测试：上传/下载，扣除游戏流量，带进度
    # ------------------------------------------------------------------
    def bandwidth_test(
        self,
        host: Optional[str] = None,
        progress_callback: Optional[Callable[[float], None]] = None,
    ) -> Dict:
        def emit(progress: float):
            if progress_callback is not None:
                progress_callback(min(1.0, max(0.0, progress)))

        emit(0.0)
        download_mbps = self._download_test(lambda p: emit(0.5 * p))
        upload_mbps = self._upload_test(lambda p: emit(0.5 + 0.5 * p))
        emit(1.0)

        game_traffic = self.config.get("game_traffic_mbps", 1.0)
        result = {
            "download_mbps": download_mbps,
            "upload_mbps": upload_mbps,
            "game_traffic_mbps": game_traffic,
            "game_available_download_mbps": max(0.0, download_mbps - game_traffic),
            "game_available_upload_mbps": max(0.0, upload_mbps - game_traffic),
            "progress": 1.0,
        }
        self._record({
            "type": "bandwidth",
            "timestamp": self._time(),
            "download_mbps": download_mbps,
            "upload_mbps": upload_mbps,
        })
        return result

    # ------------------------------------------------------------------
    # 网络优化建议：按实际问题生成，带优先级
    # ------------------------------------------------------------------
    def network_optimization(self, measurements: Optional[Dict] = None) -> List[Dict]:
        if measurements is None:
            measurements = {
                "latency": self.measure_latency(),
                "packet_loss": self.measure_packet_loss(),
                "bandwidth": self.bandwidth_test(),
            }

        suggestions: List[Dict] = []
        latency = measurements.get("latency")
        loss = measurements.get("packet_loss")
        bandwidth = measurements.get("bandwidth")

        if latency:
            avg = latency.get("avg_ms")
            if avg is None:
                suggestions.append({
                    "priority": "high",
                    "issue": "connection_lost",
                    "suggestion": "无法连接游戏服务器：检查路由器与外网连接，确认游戏服务器地址配置正确",
                })
            else:
                if avg > self._latency_bad_ms:
                    suggestions.append({
                        "priority": "high",
                        "issue": "high_latency",
                        "suggestion": (
                            "延迟过高（{:.0f}ms）：切换到离游戏服务器更近的节点，"
                            "改用有线连接，并检查路由器负载"
                        ).format(avg),
                    })
                elif avg > self._latency_warn_ms:
                    suggestions.append({
                        "priority": "medium",
                        "issue": "high_latency",
                        "suggestion": (
                            "延迟偏高（{:.0f}ms）：关闭占用带宽的后台程序，"
                            "或更换更近的游戏服务器节点"
                        ).format(avg),
                    })
                jitter = latency.get("jitter_ms")
                if jitter is not None and jitter > self._jitter_warn_ms:
                    suggestions.append({
                        "priority": "medium",
                        "issue": "high_jitter",
                        "suggestion": (
                            "延迟抖动大（{:.0f}ms）：避免 WiFi 干扰改用以太网，"
                            "并在路由器上为游戏流量开启 QoS"
                        ).format(jitter),
                    })

        if loss:
            loss_percent = loss.get("loss_percent", 0.0)
            if loss_percent > self._loss_bad_percent:
                suggestions.append({
                    "priority": "high",
                    "issue": "high_packet_loss",
                    "suggestion": (
                        "丢包率过高（{:.1f}%）：检查网线是否松动、WiFi 信号是否太弱，"
                        "必要时联系运营商检修线路"
                    ).format(loss_percent),
                })
            elif loss_percent > self._loss_warn_percent:
                suggestions.append({
                    "priority": "medium",
                    "issue": "high_packet_loss",
                    "suggestion": (
                        "丢包率偏高（{:.1f}%）：尽量使用有线连接，"
                        "并避开网络高峰时段"
                    ).format(loss_percent),
                })
            up = loss.get("upload", {}).get("loss_percent", 0.0)
            down = loss.get("download", {}).get("loss_percent", 0.0)
            if down > up + 5.0:
                suggestions.append({
                    "priority": "medium",
                    "issue": "download_loss",
                    "suggestion": "下行丢包明显高于上行：多为运营商线路或 WiFi 信号问题，建议有线直连光猫排查",
                })
            elif up > down + 5.0:
                suggestions.append({
                    "priority": "medium",
                    "issue": "upload_loss",
                    "suggestion": "上行丢包明显高于下行：检查本机到路由器的连接，关闭占上传带宽的程序",
                })

        if bandwidth:
            available = bandwidth.get("game_available_download_mbps")
            if available is not None and available < self._min_game_bandwidth_mbps:
                suggestions.append({
                    "priority": "medium",
                    "issue": "insufficient_bandwidth",
                    "suggestion": (
                        "游戏可用带宽不足（{:.1f}Mbps）：暂停后台下载/视频，"
                        "或升级宽带套餐"
                    ).format(available),
                })

        if not suggestions:
            suggestions.append({
                "priority": "low",
                "issue": "healthy",
                "suggestion": "网络状况良好，无需优化",
            })

        suggestions.sort(key=lambda s: _PRIORITY_ORDER[s["priority"]])
        return suggestions

    # ------------------------------------------------------------------
    # 连接历史：记录、统计与告警
    # ------------------------------------------------------------------
    def connection_history(self, limit: Optional[int] = None) -> List[Dict]:
        records = list(self._history)
        if limit is not None:
            records = records[-limit:]
        return records

    def history_stats(self) -> Dict:
        latencies = [
            r["avg_ms"] for r in self._history
            if r["type"] == "latency" and r.get("avg_ms") is not None
        ]
        losses = [
            r["loss_percent"] for r in self._history
            if r["type"] == "packet_loss"
        ]
        return {
            "samples": len(self._history),
            "latency_samples": len(latencies),
            "avg_latency_ms": sum(latencies) / len(latencies) if latencies else None,
            "min_latency_ms": min(latencies) if latencies else None,
            "max_latency_ms": max(latencies) if latencies else None,
            "avg_loss_percent": sum(losses) / len(losses) if losses else None,
            "max_loss_percent": max(losses) if losses else None,
        }

    def get_alerts(self) -> List[Dict]:
        return list(self._alerts)

    # ------------------------------------------------------------------
    # 内部：记录与告警
    # ------------------------------------------------------------------
    def _record(self, record: Dict):
        self._history.append(record)
        self._check_alerts(record)

    def _check_alerts(self, record: Dict):
        now = record.get("timestamp", self._time())
        if record["type"] == "latency":
            avg = record.get("avg_ms")
            if avg is None:
                self._alerts.append({
                    "type": "connection_lost",
                    "level": "critical",
                    "message": "无法连接游戏服务器，网络可能已断开",
                    "timestamp": now,
                })
                return
            if avg > self._latency_alert_ms:
                self._alerts.append({
                    "type": "high_latency",
                    "level": "warning",
                    "message": "延迟 {:.0f}ms 超过告警阈值 {:.0f}ms".format(
                        avg, self._latency_alert_ms
                    ),
                    "timestamp": now,
                })
            previous = [
                r["avg_ms"] for r in self._history[:-1]
                if r["type"] == "latency" and r.get("avg_ms") is not None
            ]
            if len(previous) >= 2:
                baseline = sum(previous) / len(previous)
                if baseline > 0 and avg > 1.5 * baseline:
                    self._alerts.append({
                        "type": "latency_degradation",
                        "level": "warning",
                        "message": "网络变差：延迟从平均 {:.0f}ms 升至 {:.0f}ms".format(
                            baseline, avg
                        ),
                        "timestamp": now,
                    })
        elif record["type"] == "packet_loss":
            loss_percent = record.get("loss_percent", 0.0)
            if loss_percent > self._loss_alert_percent:
                self._alerts.append({
                    "type": "high_packet_loss",
                    "level": "warning",
                    "message": "丢包率 {:.1f}% 超过告警阈值 {:.1f}%".format(
                        loss_percent, self._loss_alert_percent
                    ),
                    "timestamp": now,
                })

