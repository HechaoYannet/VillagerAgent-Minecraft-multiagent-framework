#!/usr/bin/env python3
"""
VillagerAgent - Minecraft AI 伙伴系统
主入口点

用法:
    python main.py                          # 使用默认配置启动
    python main.py --config config/custom.yaml  # 使用自定义配置启动
    python main.py --web-only               # 仅启动 Web 管理后台
    python main.py --agent-only             # 仅启动 Agent (需要已运行的 Minecraft 服务器)
"""

import argparse
import asyncio
import logging
import signal
import sys
import os

# 在初始化任何其他模块之前，先加载配置
def load_config(config_path: str = "config/default.yaml") -> dict:
    """加载 YAML 配置文件"""
    import yaml
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 加载敏感配置(可选)
    secrets_path = "config/secrets.yaml"
    if os.path.exists(secrets_path):
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = yaml.safe_load(f)
        _deep_merge(config, secrets)

    # 环境变量覆盖
    if os.environ.get("MINECRAFT_HOST"):
        config["minecraft"]["host"] = os.environ["MINECRAFT_HOST"]
    if os.environ.get("MINECRAFT_PORT"):
        config["minecraft"]["port"] = int(os.environ["MINECRAFT_PORT"])
    if os.environ.get("LLM_API_KEY"):
        if "llm" not in config:
            config["llm"] = {}
        config["llm"]["api_key"] = os.environ["LLM_API_KEY"]

    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典"""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def setup_logging(config: dict):
    """配置日志系统"""
    import colorlog
    level = getattr(logging, config.get("logging", {}).get("level", "INFO"))
    structured = config.get("logging", {}).get("structured", True)

    if structured:
        # JSON 结构化日志(用于生产环境)
        os.makedirs("logs", exist_ok=True)
        file_handler = logging.FileHandler("logs/villager.log", encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            '{"time": "%(asctime)s", "level": "%(levelname)s", "name": "%(name)s", "message": "%(message)s"}'
        ))
        file_handler.setLevel(level)
        logging.getLogger().addHandler(file_handler)

    # 控制台彩色日志
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s%(reset)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "green",
            "INFO": "cyan",
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "red,bg_white",
        }
    ))
    console_handler.setLevel(level)
    logging.getLogger().addHandler(console_handler)
    logging.getLogger().setLevel(level)

    return logging.getLogger("VillagerAgent")


async def run_agent(config: dict, logger: logging.Logger):
    """启动 Agent 系统"""
    logger.info("启动 Agent 系统...")
    # TODO: Phase 2 - 初始化事件总线、创建 Agent、连接 Minecraft 服务器
    logger.info("Agent 系统就绪")
    # 保持运行
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("Agent 系统关闭")


async def run_web(config: dict, logger: logging.Logger):
    """启动 Web 管理后台"""
    if not config.get("web", {}).get("enabled", True):
        logger.info("Web 管理后台已禁用")
        return

    logger.info("启动 Web 管理后台...")
    # TODO: Phase 7 - 导入并启动 FastAPI 应用
    logger.info(f"Web 管理后台启动在 http://{config['web']['host']}:{config['web']['port']}")
    # 保持运行
    try:
        while True:
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        logger.info("Web 管理后台关闭")


async def main_async(config: dict, args: argparse.Namespace):
    """异步主函数"""
    logger = setup_logging(config)
    logger.info("=" * 60)
    logger.info("🏰 VillagerAgent - Minecraft AI 伙伴系统")
    logger.info("=" * 60)
    logger.info(f"Minecraft 服务器: {config['minecraft']['host']}:{config['minecraft']['port']}")
    logger.info(f"LLM 模型: {config['llm']['model']}")
    logger.info(f"Web 管理后台: {'启用' if config.get('web', {}).get('enabled', True) else '禁用'}")
    logger.info("=" * 60)

    tasks = []

    if not args.web_only:
        tasks.append(asyncio.create_task(run_agent(config, logger), name="agent"))

    if not args.agent_only:
        tasks.append(asyncio.create_task(run_web(config, logger), name="web"))

    if not tasks:
        logger.error("没有任务可运行(--web-only 和 --agent-only 不能同时使用)")
        return

    # 优雅关闭处理
    def shutdown(sig, frame):
        logger.info(f"收到信号 {sig}，正在关闭...")
        for task in tasks:
            task.cancel()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass

    logger.info("VillagerAgent 已关闭")


def main():
    """主函数"""
    parser = argparse.ArgumentParser(
        description="VillagerAgent - Minecraft AI 伙伴系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config", "-c",
        default="config/default.yaml",
        help="配置文件路径 (默认: config/default.yaml)"
    )
    parser.add_argument(
        "--web-only",
        action="store_true",
        help="仅启动 Web 管理后台"
    )
    parser.add_argument(
        "--agent-only",
        action="store_true",
        help="仅启动 Agent 系统"
    )
    parser.add_argument(
        "--debug", "-d",
        action="store_true",
        help="启用调试模式"
    )

    args = parser.parse_args()

    if args.web_only and args.agent_only:
        print("错误: --web-only 和 --agent-only 不能同时使用")
        sys.exit(1)

    # 加载配置
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"错误: 找不到配置文件 '{args.config}'")
        print("提示: 复制 config/secrets.template.yaml 为 config/secrets.yaml 并编辑")
        sys.exit(1)

    # 调试模式
    if args.debug:
        config.setdefault("logging", {})["level"] = "DEBUG"

    # 运行
    asyncio.run(main_async(config, args))


if __name__ == "__main__":
    main()
