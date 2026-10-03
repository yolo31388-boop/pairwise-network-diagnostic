"""
网络诊断与优化系统 - 核心模块

功能：
- 延迟测量：多发 ICMP 探测取平均值，默认测量游戏服务器
- 丢包率测量：区分上行/下行，支持时间范围
- 带宽测试：测量上传/下载速度，扣除游戏占用流量，带进度回调
- 网络优化建议：根据实际诊断结果生成，按优先级排序
- 连接历史：记录快照、统计分析、网络变差告警
"""
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional
import socket
import time


DEFAULT_CONFIG = {
    # 游戏服务器地址（延迟/丢包测量的目标，而不是百度等无关站点）
    "game_server": "game.server.local",
    # 延迟测量：每次发送的探测包数量，结果取平均值
    "ping_count": 5,
    "ping_timeout": 1.0,
    # 丢包率测量的时间范围与探测间隔（秒）
    "loss_duration": 5.0,
    "loss_interval": 0.5,
    # 带宽测试
    "bandwidth_duration": 4.0,
    "bandwidth_steps": 8,
    # 游戏本身占用的带宽（Mbps），用于计算游戏可用带宽
    "game_traffic_mbps": 0.0,
    # 游戏正常所需的最低带宽（Mbps）
    "game_required_mbps": 1.0,
    # 告警阈值
    "latency_alert_ms": 150.0,
    "loss_alert_percent": 5.0,
    # 历史记录
    "history_max": 1000,
}


def _default_ping(host: str, seq: int, timeout: float) -> Optional[float]:
    """默认探测实现：TCP 连接耗时近似 RTT，失败/超时返回 None（丢包）。"""
    try:
        start = time.monotonic()
        with socket.create_connection((host, 80), timeout=timeout):
            pass
        return (time.monotonic() - start) * 1000.0
    except OSError:
        return None


class NetworkDiagnostic:
    def __init__(self, config: Optional[Dict] = None):
        self.config = dict(DEFAULT_CONFIG)
        if config:
            self.config.update(config)
        self._state = {}
        self._history: List[Dict] = []
        self._alerts: List[Dict] = []

        # 可注入的外部依赖（便于测试与不同平台适配）
        self._ping_fn: Callable = self.config.get("ping_fn", _default_ping)
        self._probe_fn: Callable = self.config.get("probe_fn", self._default_probe)
        self._speed_fn: Callable = self.config.get("speed_fn", self._default_speed)
        self._sleep_fn: Callable = self.config.get("sleep_fn", time.sleep)
        self._time_fn: Callable = self.config.get("time_fn", time.time)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def update(self, dt: float):
        pass

    def reset(self):
        self._state = {}
        self._history = []
        self._alerts = []

    # ------------------------------------------------------------------
    # 延迟测量：多发取平均值，目标为游戏服务器
    # ------------------------------------------------------------------
    def measure_latency(self, host: Optional[str] = None,
                        count: Optional[int] = None) -> Dict:
        host = host or self.config["game_server"]
        count = count or self.config["ping_count"]
        timeout = self.config["ping_timeout"]

        rtts: List[float] = []
        sent = 0
        for seq in range(count):
            sent += 1
            rtt = self._ping_fn(host, seq, timeout)
            if rtt is not None:
                rtts.append(rtt)

        received = len(rtts)
        lost = sent - received
        result = {
            "host": host,
            "sent": sent,
            "received": received,
            "lost": lost,
            "loss_percent": (lost / sent * 100.0) if sent else 0.0,
            # 丢包的包不计入平均值（更不能按 0ms 计）
            "avg_ms": (sum(rtts) / received) if received else None,
            "min_ms": min(rtts) if rtts else None,
            "max_ms": max(rtts) if rtts else None,
            "jitter_ms": (max(rtts) - min(rtts)) if len(rtts) > 1 else 0.0,
        }
        self._record("latency", result)
        return result

    # ------------------------------------------------------------------
    # 丢包率测量：区分上行/下行，支持时间范围
    # ------------------------------------------------------------------
    def measure_packet_loss(self, direction: str = "both",
                            duration: Optional[float] = None,
                            interval: Optional[float] = None) -> Dict:
        duration = duration if duration is not None else self.config["loss_duration"]
        interval = interval if interval is not None else self.config["loss_interval"]
        probe_count = max(1, int(round(duration / interval))) if interval > 0 else 1

        directions = ("uplink", "downlink") if direction == "both" else (direction,)
        result: Dict = {"window_seconds": duration, "interval_seconds": interval}

        for dir_name in directions:
            sent = 0
            received = 0
            for seq in range(probe_count):
                sent += 1
                if self._probe_fn(dir_name, seq):
                    received += 1
                if seq < probe_count - 1:
                    self._sleep_fn(interval)
            lost = sent - received
            result[dir_name] = {
                "sent": sent,
                "received": received,
                "lost": lost,
                "loss_percent": (lost / sent * 100.0) if sent else 0.0,
            }
        self._record("packet_loss", result)
        return result

    # ------------------------------------------------------------------
    # 带宽测试：上传/下载速度，扣除游戏流量，带进度回调
    # ------------------------------------------------------------------
    def bandwidth_test(self, duration: Optional[float] = None,
                       on_progress: Optional[Callable[[float], None]] = None) -> Dict:
        duration = duration if duration is not None else self.config["bandwidth_duration"]
        steps = max(1, int(self.config["bandwidth_steps"]))
        step_seconds = duration / steps
        game_traffic = self.config["game_traffic_mbps"]

        directions = ("download", "upload")
        total_steps = steps * len(directions)
        done_steps = 0
        speeds: Dict[str, float] = {}

        for dir_name in directions:
            samples: List[float] = []
            for _ in range(steps):
                samples.append(self._speed_fn(dir_name))
                self._sleep_fn(step_seconds)
                done_steps += 1
                if on_progress:
                    on_progress(done_steps / total_steps)
            speeds[dir_name] = sum(samples) / len(samples) if samples else 0.0

        download = speeds["download"]
        upload = speeds["upload"]
        result = {
            "download_mbps": download,
            "upload_mbps": upload,
            "game_traffic_mbps": game_traffic,
            # 游戏可用带宽 = 总带宽 - 游戏自身及其他占用
            "game_available_download_mbps": max(0.0, download - game_traffic),
            "game_available_upload_mbps": max(0.0, upload - game_traffic),
        }
        self._record("bandwidth", result)
        return result

    # ------------------------------------------------------------------
    # 网络优化建议：基于实际诊断结果，按优先级排序
    # ------------------------------------------------------------------
    def network_optimization(self, include_bandwidth: bool = False) -> List[Dict]:
        latency = self.measure_latency()
        loss = self.measure_packet_loss()
        bandwidth = self.bandwidth_test() if include_bandwidth else None

        suggestions: List[Dict] = []

        avg_latency = latency["avg_ms"]
        if avg_latency is None:
            suggestions.append({
                "priority": 1,
                "issue": "无法连接游戏服务器",
                "suggestion": "所有探测包均丢失，请检查网络连接与游戏服务器状态",
            })
        elif avg_latency > self.config["latency_alert_ms"]:
            suggestions.append({
                "priority": 1,
                "issue": "到游戏服务器的延迟过高",
                "suggestion": "切换到有线连接、关闭占网应用，或更换更近的游戏服务器节点",
            })

        for dir_name in ("uplink", "downlink"):
            dir_loss = loss.get(dir_name, {}).get("loss_percent", 0.0)
            if dir_loss > self.config["loss_alert_percent"]:
                label = "上行" if dir_name == "uplink" else "下行"
                suggestions.append({
                    "priority": 1,
                    "issue": f"{label}丢包率过高（{dir_loss:.1f}%）",
                    "suggestion": "检查路由器与线路质量，尝试更换信道或联系运营商",
                })

        if latency["jitter_ms"] > self.config["latency_alert_ms"] / 2:
            suggestions.append({
                "priority": 2,
                "issue": "网络抖动明显",
                "suggestion": "避免在网络高峰期游戏，或开启路由器的 QoS 功能",
            })

        if bandwidth is not None:
            required = self.config["game_required_mbps"]
            if bandwidth["game_available_download_mbps"] < required:
                suggestions.append({
                    "priority": 2,
                    "issue": "游戏可用带宽不足",
                    "suggestion": "暂停下载/视频等占带宽的应用，或升级宽带套餐",
                })

        if not suggestions:
            suggestions.append({
                "priority": 3,
                "issue": "网络状况良好",
                "suggestion": "无需优化，保持当前网络设置",
            })

        suggestions.sort(key=lambda item: item["priority"])
        return suggestions

    # ------------------------------------------------------------------
    # 连接历史：记录、统计与告警
    # ------------------------------------------------------------------
    def connection_history(self) -> Dict:
        latencies = [e["data"]["avg_ms"] for e in self._history
                     if e["type"] == "latency" and e["data"].get("avg_ms") is not None]
        losses: List[float] = []
        for e in self._history:
            if e["type"] == "packet_loss":
                for dir_name in ("uplink", "downlink"):
                    if dir_name in e["data"]:
                        losses.append(e["data"][dir_name]["loss_percent"])

        stats = {
            "samples": len(self._history),
            "avg_latency_ms": (sum(latencies) / len(latencies)) if latencies else None,
            "max_latency_ms": max(latencies) if latencies else None,
            "min_latency_ms": min(latencies) if latencies else None,
            "avg_loss_percent": (sum(losses) / len(losses)) if losses else None,
        }
        return {
            "entries": list(self._history),
            "stats": stats,
            "alerts": list(self._alerts),
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _record(self, entry_type: str, data: Dict):
        self._history.append({
            "type": entry_type,
            "timestamp": self._time_fn(),
            "data": data,
        })
        if len(self._history) > self.config["history_max"]:
            self._history = self._history[-self.config["history_max"]:]
        self._check_alerts(entry_type, data)

    def _check_alerts(self, entry_type: str, data: Dict):
        if entry_type == "latency":
            avg = data.get("avg_ms")
            if avg is None:
                self._add_alert("critical", "无法连接游戏服务器，网络可能已断开")
            elif avg > self.config["latency_alert_ms"]:
                self._add_alert(
                    "warning",
                    f"延迟升高：当前 {avg:.0f}ms，超过阈值 "
                    f"{self.config['latency_alert_ms']:.0f}ms",
                )
        elif entry_type == "packet_loss":
            for dir_name in ("uplink", "downlink"):
                if dir_name in data:
                    pct = data[dir_name]["loss_percent"]
                    if pct > self.config["loss_alert_percent"]:
                        label = "上行" if dir_name == "uplink" else "下行"
                        self._add_alert(
                            "warning",
                            f"{label}丢包率升高：当前 {pct:.1f}%，超过阈值 "
                            f"{self.config['loss_alert_percent']:.1f}%",
                        )

    def _add_alert(self, level: str, message: str):
        self._alerts.append({
            "level": level,
            "message": message,
            "timestamp": self._time_fn(),
        })

    def _default_probe(self, direction: str, seq: int) -> bool:
        rtt = self._ping_fn(self.config["game_server"], seq,
                            self.config["ping_timeout"])
        return rtt is not None

    def _default_speed(self, direction: str) -> float:
        # 无测速服务器配置时无法测量，返回 0 而不是假装测过
        return 0.0
