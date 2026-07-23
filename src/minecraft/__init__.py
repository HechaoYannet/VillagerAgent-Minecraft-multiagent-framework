"""
Minecraft 集成模块 — Bot 服务端 + 世界状态管理

将 env/ 目录中的 Minecraft 集成代码重新导出到 src/minecraft/ 命名空间。
当 env/ 中的代码完全重构后，这些 re-export 将变为直接实现。

注意: env/ 模块依赖 JSPyBridge (Node.js 桥接)，在纯开发环境中可能不可用。
"""

import logging

_logger = logging.getLogger(__name__)

try:
    # ── Bot HTTP 服务端 (Flask → 未来迁移到 FastAPI) ──
    from env.minecraft_server import app, start_server  # noqa: F401

    # ── Bot 动作函数 (~30 个 Minecraft 交互工具) ──
    from env.env_api import (  # noqa: F401
        getBlock, findBlocks, BlocksNearby, BlocksSearch,
        bfs_search, bfs_search_sample,
        move_to, move_to_nearest_, random_walk,
        place_block, place_block_op, place_axis,
        dig_at, dig_block, dig_check,
        getEntityInfo, get_entity_by, attack, lookAtPlayer,
        get_envs_info, get_envs_info_dict, get_envs_info2str, get_agent_info2str,
        bag_info, find_nearest_, find_everything_, find_nearest_solid_block,
        findSimilarName, is_entity_or_item,
        interact_nearest, readNearestSign,
        chat_long, name_check, distanceTo, mulList,
        load_agent_status,
    )

    # ── Mineflayer 客户端辅助 ──
    from env.minecraft_client import *  # noqa: F401, F403

    # ── 环境协调器 ──
    from env.env import VillagerBench  # noqa: F401

    # ── 环境工具 ──
    from env.utils import init_logger, format_string, smart_truncate  # noqa: F401

except ImportError as e:
    _logger.debug(f"Minecraft 模块部分未加载 (JSPyBridge 可能不可用): {e}")

