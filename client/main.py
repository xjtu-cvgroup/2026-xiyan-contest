#!/usr/bin/env python3
"""《一骑红尘：荔枝争运战》客户端入口（基于官方 lychee_basic_client 适配）。

两种传参方式：
  平台位置参数（任务书 10.2）: python3 main.py <playerId> <host> <port> [playerName]
  官方命名参数（py-cli-26）  : python3 main.py --host H --port P --player-id ID
                               [--player-name NAME] [--version V]
                               [--strategy planner|warden|hybrid]

策略选择：本分支默认 hybrid（见 DEFAULT_STRATEGY）。公开老图经拓扑证明
后完整沿用守望者；隐藏旁路图自动回退 Planner，并保留 S14 控制机会。
"""
import argparse
import os
import re
import socket
import sys
import time

from lychee_basic_client.config import Config
from lychee.log import get_logger
from lychee.session import StrategySession
from lychee.version import BUILD_VERSION

VERSION = BUILD_VERSION
STRATEGIES = ("planner", "warden", "hybrid")
DEFAULT_STRATEGY = "hybrid"


def build_strategy(name, log):
    """按名字构造策略实例；未知名字回退主线并告警。"""
    if name == "warden":
        from lychee.warden import WardenStrategy
        return WardenStrategy(log)
    if name == "hybrid":
        from lychee.hybrid import HybridStrategy
        return HybridStrategy(log)
    if name != "planner" and log:
        log.warning("unknown strategy %r, falling back to planner", name)
    from lychee.strategy import PlannerStrategy
    return PlannerStrategy(log)


def _parse_player_id(raw):
    """协议要求 playerId 为 Int；容忍 'player1001' 这类传参。"""
    if raw.isdigit():
        return int(raw)
    digits = re.sub(r"\D", "", raw)
    if digits:
        return int(digits)
    raise ValueError(f"cannot parse playerId from {raw!r}")


def parse_cli(argv):
    if argv and argv[0].startswith("-"):
        # 官方命名参数风格（与 py-cli-26 的 config.parse_args 对齐）
        parser = argparse.ArgumentParser(description="Lychee arena Python client")
        parser.add_argument("--host", default="127.0.0.1")
        parser.add_argument("--port", type=int, default=30000)
        parser.add_argument("--player-id", type=int, default=1006)
        parser.add_argument("--player-name", default=None)
        parser.add_argument("--version", default=VERSION)
        parser.add_argument("--strategy", choices=STRATEGIES,
                            default=os.environ.get("LYCHEE_STRATEGY",
                                                   DEFAULT_STRATEGY))
        a = parser.parse_args(argv)
        cfg = Config(host=a.host, port=a.port, player_id=a.player_id,
                     player_name=a.player_name or f"team-{a.player_id}",
                     version=a.version)
        return cfg, a.strategy

    # 平台位置参数风格
    if len(argv) < 3:
        print(f"Usage: {sys.argv[0]} <playerId> <host> <port> [playerName]\n"
              f"   or: {sys.argv[0]} --host H --port P --player-id ID",
              file=sys.stderr)
        raise SystemExit(1)
    player_id = _parse_player_id(argv[0])
    name = argv[3] if len(argv) > 3 else f"team-{player_id}"
    cfg = Config(host=argv[1], port=int(argv[2]), player_id=player_id,
                 player_name=name, version=VERSION)
    return cfg, os.environ.get("LYCHEE_STRATEGY", DEFAULT_STRATEGY)


def connect_with_retry(host, port, log, retries=30, delay=1.0):
    last_err = None
    for i in range(retries):
        try:
            sock = socket.create_connection((host, port), timeout=10)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(None)
            return sock
        except OSError as e:
            last_err = e
            log.warning("connect failed (%s), retry %d/%d", e, i + 1, retries)
            time.sleep(delay)
    raise ConnectionError(f"cannot connect {host}:{port}: {last_err}")


def main():
    config, strategy_name = parse_cli(sys.argv[1:])
    log = get_logger(config.player_id)
    log.info("=== lychee client BUILD %s strategy=%s ===",
             BUILD_VERSION, strategy_name)
    strategy = build_strategy(strategy_name, log)
    sock = connect_with_retry(config.host, config.port, log)
    log.info("connected to %s:%s as player %s (%s)",
             config.host, config.port, config.player_id, config.player_name)
    try:
        return StrategySession(sock, config, strategy=strategy,
                               logger=log).run()
    finally:
        sock.close()


if __name__ == "__main__":
    raise SystemExit(main())
