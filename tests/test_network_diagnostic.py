"""
网络诊断与优化系统 - 验收测试
运行方式：python -m pytest tests/test_network_diagnostic.py -q
共 13 个测试用例
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from network.network_diagnostic import NetworkDiagnostic


def make_system(**config):
    config.setdefault("sleep_fn", lambda seconds: None)
    return NetworkDiagnostic(config=config)


class TestNetworkDiagnostic:
    def setup_method(self):
        self.system = NetworkDiagnostic()

    # ---------------- 延迟测量 ----------------

    def test_case_01(self):
        # 延迟测量发送多个探测包并取平均值
        rtts = iter([10.0, 20.0, 30.0, 40.0, 50.0])
        system = make_system(ping_count=5,
                             ping_fn=lambda host, seq, timeout: next(rtts))
        result = system.measure_latency()
        assert result["sent"] == 5
        assert result["received"] == 5
        assert result["avg_ms"] == pytest.approx(30.0)
        assert result["min_ms"] == 10.0
        assert result["max_ms"] == 50.0

    def test_case_02(self):
        # 丢包的包不计入平均值（不能按 0ms 计算）
        rtts = iter([10.0, None, 20.0, None, 30.0])
        system = make_system(ping_count=5,
                             ping_fn=lambda host, seq, timeout: next(rtts))
        result = system.measure_latency()
        assert result["received"] == 3
        assert result["lost"] == 2
        assert result["loss_percent"] == pytest.approx(40.0)
        assert result["avg_ms"] == pytest.approx(20.0)

    def test_case_03(self):
        # 延迟测量目标是游戏服务器，而不是百度等无关站点
        seen_hosts = []

        def ping_fn(host, seq, timeout):
            seen_hosts.append(host)
            return 25.0

        system = make_system(game_server="game.example.com", ping_fn=ping_fn)
        result = system.measure_latency()
        assert result["host"] == "game.example.com"
        assert seen_hosts
        assert all(host == "game.example.com" for host in seen_hosts)
        assert "baidu" not in result["host"]

    # ---------------- 丢包率测量 ----------------

    def test_case_04(self):
        # 丢包率被真实测量，而不是永远 0%
        system = make_system(
            probe_fn=lambda direction, seq: seq % 4 != 0,  # 每 4 个丢 1 个
            loss_duration=4.0, loss_interval=1.0,
        )
        result = system.measure_packet_loss(direction="uplink")
        assert result["uplink"]["sent"] == 4
        assert result["uplink"]["lost"] == 1
        assert result["uplink"]["loss_percent"] == pytest.approx(25.0)

    def test_case_05(self):
        # 丢包率区分上行和下行
        def probe_fn(direction, seq):
            if direction == "uplink":
                return True
            return seq % 2 == 0  # 下行丢一半

        system = make_system(probe_fn=probe_fn,
                             loss_duration=4.0, loss_interval=1.0)
        result = system.measure_packet_loss()
        assert result["uplink"]["loss_percent"] == pytest.approx(0.0)
        assert result["downlink"]["loss_percent"] == pytest.approx(50.0)

    def test_case_06(self):
        # 丢包率测量覆盖指定时间范围（探测次数 = 时长 / 间隔）
        calls = []
        system = make_system(
            probe_fn=lambda direction, seq: calls.append((direction, seq)) or True,
            loss_duration=2.0, loss_interval=0.5,
        )
        result = system.measure_packet_loss()
        assert result["window_seconds"] == 2.0
        assert result["uplink"]["sent"] == 4
        assert result["downlink"]["sent"] == 4
        assert len(calls) == 8

    # ---------------- 带宽测试 ----------------

    def test_case_07(self):
        # 带宽测试真实测量上传和下载速度
        speeds = {"download": 100.0, "upload": 50.0}
        system = make_system(speed_fn=lambda direction: speeds[direction],
                             bandwidth_steps=4)
        result = system.bandwidth_test()
        assert result["download_mbps"] == pytest.approx(100.0)
        assert result["upload_mbps"] == pytest.approx(50.0)

    def test_case_08(self):
        # 带宽测试考虑游戏流量，给出游戏可用带宽
        speeds = {"download": 100.0, "upload": 50.0}
        system = make_system(speed_fn=lambda direction: speeds[direction],
                             game_traffic_mbps=10.0, bandwidth_steps=4)
        result = system.bandwidth_test()
        assert result["game_traffic_mbps"] == 10.0
        assert result["game_available_download_mbps"] == pytest.approx(90.0)
        assert result["game_available_upload_mbps"] == pytest.approx(40.0)

    def test_case_09(self):
        # 带宽测试报告进度，玩家知道测试进行到哪一步
        progress = []
        system = make_system(speed_fn=lambda direction: 10.0,
                             bandwidth_steps=4)
        system.bandwidth_test(on_progress=progress.append)
        assert len(progress) == 8  # 下载 4 步 + 上传 4 步
        assert progress[-1] == pytest.approx(1.0)
        assert all(later >= earlier for earlier, later in zip(progress, progress[1:]))

    # ---------------- 网络优化建议 ----------------

    def test_case_10(self):
        # 建议基于实际问题：高延迟给延迟建议，而不是写死的"关防火墙"
        system = make_system(
            ping_fn=lambda host, seq, timeout: 300.0,
            probe_fn=lambda direction, seq: True,
            latency_alert_ms=150.0,
        )
        suggestions = system.network_optimization()
        assert suggestions
        assert any("延迟" in s["issue"] for s in suggestions)
        assert not any("防火墙" in s["suggestion"] for s in suggestions)

    def test_case_11(self):
        # 建议按优先级排序，且不同问题给出不同建议
        system = make_system(
            ping_fn=lambda host, seq, timeout: 30.0,
            probe_fn=lambda direction, seq: seq % 2 == 0,  # 50% 丢包
            loss_alert_percent=5.0,
        )
        suggestions = system.network_optimization()
        assert suggestions
        priorities = [s["priority"] for s in suggestions]
        assert priorities == sorted(priorities)
        assert suggestions[0]["priority"] == 1
        assert any("丢包" in s["issue"] for s in suggestions)
        assert not any("延迟过高" in s["issue"] for s in suggestions)

    # ---------------- 连接历史 ----------------

    def test_case_12(self):
        # 连接历史有统计：平均延迟、平均丢包率
        rtts = iter([10.0, 20.0, 30.0, 40.0])
        system = make_system(
            ping_count=2,
            ping_fn=lambda host, seq, timeout: next(rtts),
            probe_fn=lambda direction, seq: seq != 0,
            loss_duration=4.0, loss_interval=1.0,
        )
        system.measure_latency()  # avg 15
        system.measure_latency()  # avg 35
        system.measure_packet_loss(direction="uplink")  # 25% 丢包
        history = system.connection_history()
        stats = history["stats"]
        assert stats["samples"] == 3
        assert stats["avg_latency_ms"] == pytest.approx(25.0)
        assert stats["max_latency_ms"] == pytest.approx(35.0)
        assert stats["min_latency_ms"] == pytest.approx(15.0)
        assert stats["avg_loss_percent"] == pytest.approx(25.0)

    def test_case_13(self):
        # 网络变差时产生告警提醒玩家
        rtts = iter([30.0, 30.0, 300.0, 300.0])
        system = make_system(
            ping_count=2,
            ping_fn=lambda host, seq, timeout: next(rtts),
            latency_alert_ms=150.0,
        )
        system.measure_latency()  # 正常
        assert system.connection_history()["alerts"] == []
        system.measure_latency()  # 变差
        alerts = system.connection_history()["alerts"]
        assert len(alerts) == 1
        assert alerts[0]["level"] == "warning"
        assert "延迟" in alerts[0]["message"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
