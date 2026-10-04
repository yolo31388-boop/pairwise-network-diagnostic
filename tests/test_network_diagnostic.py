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


def make_system(**overrides):
    config = {
        "game_server": "game.example.com",
        "sleep_func": lambda seconds: None,
    }
    config.update(overrides)
    return NetworkDiagnostic(config)


class TestNetworkDiagnostic:
    def setup_method(self):
        self.system = NetworkDiagnostic()

    # 延迟测量：多发探测并取平均值
    def test_case_01(self):
        samples = iter([10.0, 20.0, 30.0, 40.0, 50.0])
        system = make_system(ping_func=lambda host, timeout: next(samples))
        result = system.measure_latency(count=5)
        assert result["sent"] == 5
        assert result["received"] == 5
        assert result["avg_ms"] == pytest.approx(30.0)
        assert result["min_ms"] == pytest.approx(10.0)
        assert result["max_ms"] == pytest.approx(50.0)

    # 延迟测量：默认测游戏服务器而不是百度等无关地址
    def test_case_02(self):
        called_hosts = []

        def ping(host, timeout):
            called_hosts.append(host)
            return 25.0

        system = make_system(ping_func=ping)
        result = system.measure_latency()
        assert result["host"] == "game.example.com"
        assert called_hosts and all(h == "game.example.com" for h in called_hosts)

    # 延迟测量：丢包不显示 0ms，全部丢包时延迟为未知
    def test_case_03(self):
        system = make_system(ping_func=lambda host, timeout: None)
        result = system.measure_latency(count=4)
        assert result["avg_ms"] is None
        assert result["received"] == 0
        assert result["loss_percent"] == pytest.approx(100.0)

        samples = iter([10.0, None, 20.0])
        system2 = make_system(ping_func=lambda host, timeout: next(samples))
        result2 = system2.measure_latency(count=3)
        assert result2["avg_ms"] == pytest.approx(15.0)
        assert result2["loss_percent"] == pytest.approx(100.0 / 3.0)

    # 丢包率测量：真正统计丢包而不是永远 0%
    def test_case_04(self):
        replies = iter([10.0, None] * 5)
        system = make_system(ping_func=lambda host, timeout: next(replies))
        result = system.measure_packet_loss(duration=10, interval=1, direction="upload")
        assert result["upload"]["sent"] == 10
        assert result["upload"]["lost"] == 5
        assert result["upload"]["loss_percent"] == pytest.approx(50.0)
        assert result["loss_percent"] == pytest.approx(50.0)

    # 丢包率测量：区分上行和下行
    def test_case_05(self):
        download_replies = iter([True, False] * 5)
        system = make_system(
            ping_func=lambda host, timeout: 10.0,
            download_probe_func=lambda host: next(download_replies),
        )
        result = system.measure_packet_loss(duration=10, interval=1)
        assert result["upload"]["loss_percent"] == pytest.approx(0.0)
        assert result["download"]["sent"] == 10
        assert result["download"]["loss_percent"] == pytest.approx(50.0)

    # 丢包率测量：支持时间范围
    def test_case_06(self):
        sleeps = []
        system = make_system(
            ping_func=lambda host, timeout: 10.0,
            sleep_func=sleeps.append,
        )
        result = system.measure_packet_loss(duration=5, interval=1)
        assert result["upload"]["sent"] == 5
        assert result["window"]["seconds"] == 5
        assert result["window"]["end"] >= result["window"]["start"]
        assert len(sleeps) == 4  # 5 次探测之间间隔 4 次

    # 带宽测试：返回上传/下载速度
    def test_case_07(self):
        system = make_system(
            download_test_func=lambda cb: 80.0,
            upload_test_func=lambda cb: 20.0,
        )
        result = system.bandwidth_test()
        assert result["download_mbps"] == pytest.approx(80.0)
        assert result["upload_mbps"] == pytest.approx(20.0)

    # 带宽测试：考虑游戏流量，给出游戏可用带宽
    def test_case_08(self):
        system = make_system(
            download_test_func=lambda cb: 50.0,
            upload_test_func=lambda cb: 20.0,
            game_traffic_mbps=10.0,
        )
        result = system.bandwidth_test()
        assert result["game_available_download_mbps"] == pytest.approx(40.0)
        assert result["game_available_upload_mbps"] == pytest.approx(10.0)

    # 带宽测试：进度从 0 到 1 单调推进
    def test_case_09(self):
        progresses = []

        def download(cb):
            cb(0.5)
            cb(1.0)
            return 100.0

        def upload(cb):
            cb(0.5)
            cb(1.0)
            return 50.0

        system = make_system(
            download_test_func=download,
            upload_test_func=upload,
        )
        result = system.bandwidth_test(progress_callback=progresses.append)
        assert progresses[0] == 0.0
        assert progresses[-1] == 1.0
        assert len(progresses) >= 3
        assert all(b >= a for a, b in zip(progresses, progresses[1:]))
        assert result["progress"] == 1.0

    # 优化建议：按实际问题生成，延迟高和丢包的建议不同
    def test_case_10(self):
        system = make_system()
        latency_problem = {
            "latency": {"avg_ms": 300.0, "jitter_ms": 5.0},
            "packet_loss": {"loss_percent": 0.0,
                            "upload": {"loss_percent": 0.0},
                            "download": {"loss_percent": 0.0}},
        }
        loss_problem = {
            "latency": {"avg_ms": 20.0, "jitter_ms": 2.0},
            "packet_loss": {"loss_percent": 30.0,
                            "upload": {"loss_percent": 30.0},
                            "download": {"loss_percent": 30.0}},
        }
        latency_suggestions = system.network_optimization(latency_problem)
        loss_suggestions = system.network_optimization(loss_problem)

        latency_issues = {s["issue"] for s in latency_suggestions}
        loss_issues = {s["issue"] for s in loss_suggestions}
        assert "high_latency" in latency_issues
        assert "high_packet_loss" not in latency_issues
        assert "high_packet_loss" in loss_issues
        assert "high_latency" not in loss_issues
        # 不再是写死的“关防火墙/重启路由器”建议
        all_text = " ".join(
            s["suggestion"] for s in latency_suggestions + loss_suggestions
        )
        assert "防火墙" not in all_text

    # 优化建议：按优先级排序，重要建议排在前面
    def test_case_11(self):
        system = make_system()
        measurements = {
            "latency": {"avg_ms": 500.0, "jitter_ms": 80.0},
            "packet_loss": {"loss_percent": 20.0,
                            "upload": {"loss_percent": 20.0},
                            "download": {"loss_percent": 20.0}},
            "bandwidth": {"game_available_download_mbps": 1.0},
        }
        suggestions = system.network_optimization(measurements)
        assert len(suggestions) >= 3
        rank = {"high": 0, "medium": 1, "low": 2}
        priorities = [rank[s["priority"]] for s in suggestions]
        assert priorities == sorted(priorities)
        assert suggestions[0]["priority"] == "high"

    # 连接历史：有记录且有统计（平均延迟/丢包率）
    def test_case_12(self):
        samples = iter([10.0] * 5 + [20.0] * 5 + [30.0] * 5 + [10.0, 10.0])
        system = make_system(ping_func=lambda host, timeout: next(samples))
        system.measure_latency(count=5)
        system.measure_latency(count=5)
        system.measure_latency(count=5)
        system.measure_packet_loss(duration=2, interval=1, direction="upload")

        history = system.connection_history()
        assert len(history) == 4

        stats = system.history_stats()
        assert stats["latency_samples"] == 3
        assert stats["avg_latency_ms"] == pytest.approx(20.0)
        assert stats["min_latency_ms"] == pytest.approx(10.0)
        assert stats["max_latency_ms"] == pytest.approx(30.0)
        assert stats["avg_loss_percent"] == pytest.approx(0.0)

    # 连接历史：网络变差时产生告警
    def test_case_13(self):
        samples = iter([50.0] * 15 + [200.0] * 5)
        system = make_system(
            ping_func=lambda host, timeout: next(samples),
            latency_alert_ms=150.0,
        )
        for _ in range(3):
            system.measure_latency(count=5)
        assert system.get_alerts() == []

        system.measure_latency(count=5)
        alerts = system.get_alerts()
        assert alerts
        alert_types = {a["type"] for a in alerts}
        assert "high_latency" in alert_types
        assert "latency_degradation" in alert_types


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
