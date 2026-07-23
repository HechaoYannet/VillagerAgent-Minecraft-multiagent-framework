/**
 * Mineflayer 桥接测试脚本
 *
 * 测试 Mineflayer 4.x 与 Minecraft 1.21.1 Fabric 服务器的连接。
 *
 * 用法: node test-bridge.js [host] [port] [username]
 * 默认: localhost:25565, 用户名为 TestBot
 */

const mineflayer = require('mineflayer');
const pathfinder = require('mineflayer-pathfinder');
const collectBlock = require('mineflayer-collectblock');
const pvp = require('mineflayer-pvp').plugin;
const minecraftHawkEye = require('minecrafthawkeye').default;
const Vec3 = require('vec3');
const minecraftData = require('minecraft-data');

const HOST = process.argv[2] || 'localhost';
const PORT = parseInt(process.argv[3] || '25565');
const USERNAME = process.argv[4] || 'VillagerTestBot';

console.log('='.repeat(50));
console.log('VillagerAgent Mineflayer 桥接测试');
console.log('='.repeat(50));
console.log(`服务器: ${HOST}:${PORT}`);
console.log(`用户名: ${USERNAME}`);
console.log(`Mineflayer 版本: ${mineflayer.version || 'unknown'}`);
console.log('');

const mcData = minecraftData('1.21.1');
console.log(`Minecraft 数据版本: ${mcData.version.minecraftVersion}`);
console.log(`方块数: ${mcData.blocksArray.length}`);
console.log(`物品数: ${mcData.itemsArray.length}`);
console.log('');

// 创建 Bot
console.log('正在连接...');
const bot = mineflayer.createBot({
  host: HOST,
  port: PORT,
  username: USERNAME,
  auth: 'offline',
  version: '1.21.1',
  checkTimeoutInterval: 60000,
  hideErrors: false,
});

// 加载插件
bot.loadPlugin(pathfinder.pathfinder);
bot.loadPlugin(collectBlock.plugin);
bot.loadPlugin(pvp);
bot.loadPlugin(minecraftHawkEye);

// ============================================
// 事件处理
// ============================================

let testsPassed = 0;
let testsFailed = 0;
const testResults = [];

function logTest(name, passed, detail) {
  const status = passed ? '✅' : '❌';
  const entry = { name, passed, detail };
  testResults.push(entry);
  if (passed) testsPassed++;
  else testsFailed++;
  console.log(`  ${status} ${name}${detail ? ': ' + detail : ''}`);
}

bot.on('login', () => {
  console.log(`\n✓ 已登录为: ${bot.username}`);
  console.log(`  实体 ID: ${bot.entity?.id}`);
  console.log(`  游戏模式: ${bot.game.gameMode}`);
  console.log(`  世界: ${bot.game.dimension}`);
  console.log(`  位置: ${bot.entity.position}`);
  console.log(`  生命值: ${bot.health}`);
  console.log(`  食物: ${bot.food}`);

  logTest('Bot 登录', true, `用户名=${bot.username}, 维度=${bot.game.dimension}`);

  // 运行更多测试
  runTests();
});

bot.on('spawn', () => {
  console.log('\n✓ Bot 已生成 (spawn 事件)');
  logTest('Bot Spawn', true, `位置=${bot.entity.position.toString()}`);
});

bot.on('error', (err) => {
  console.error(`\n❌ Bot 错误: ${err.message}`);
  logTest('Bot 稳定性', false, err.message);
});

bot.on('kicked', (reason) => {
  const reasonStr = typeof reason === 'string' ? reason : JSON.stringify(reason);
  console.error(`\n❌ 被踢出: ${reasonStr}`);
  logTest('Bot 踢出检测', false, reasonStr);
});

bot.on('end', (reason) => {
  console.log(`\n连接结束: ${reason}`);
});

bot.on('chat', (username, message) => {
  console.log(`[聊天] ${username}: ${message}`);
});

bot.on('message', (jsonMsg) => {
  const text = jsonMsg?.json?.text || jsonMsg?.text || jsonMsg?.toString?.() || '';
  if (text && text.length > 0 && text.length < 200) {
    console.log(`[系统] ${text}`);
  }
});

// ============================================
// 测试流程
// ============================================

async function runTests() {
  // 等待一秒让 bot 完全初始化
  await sleep(1000);

  // 测试 1: 时间查询
  try {
    const time = bot.time.timeOfDay;
    console.log(`\n⏰ 游戏时间: ${time}`);
    logTest('世界时间查询', true, `timeOfDay=${time}`);
  } catch (e) {
    logTest('世界时间查询', false, e.message);
  }

  // 测试 2: 获取附近方块
  try {
    const pos = bot.entity.position;
    const block = bot.blockAt(new Vec3(
      Math.floor(pos.x),
      Math.floor(pos.y - 1),
      Math.floor(pos.z)
    ));
    if (block) {
      console.log(`\n🧱 脚下方块: ${block.displayName} (${block.name})`);
      logTest('方块查询 (blockAt)', true, `name=${block.name}`);
    } else {
      logTest('方块查询 (blockAt)', false, '返回 null');
    }
  } catch (e) {
    logTest('方块查询 (blockAt)', false, e.message);
  }

  // 测试 3: 寻找附近方块
  try {
    const grassBlocks = bot.findBlocks({
      matching: (block) => block.name === 'grass_block',
      maxDistance: 32,
      count: 5,
    });
    console.log(`\n🌿 周围32格内的草方块: ${grassBlocks.length} 个`);
    logTest('方块搜索 (findBlocks)', true, `找到 ${grassBlocks.length} 个 grass_block`);
  } catch (e) {
    logTest('方块搜索 (findBlocks)', false, e.message);
  }

  // 测试 4: 聊天
  try {
    bot.chat('VillagerAgent 桥接测试 - Bot 已连接！');
    console.log(`\n💬 已发送测试消息`);
    logTest('游戏聊天', true, '消息发送成功');
  } catch (e) {
    logTest('游戏聊天', false, e.message);
  }

  // 测试 5: 库存查询
  try {
    const items = bot.inventory.items();
    console.log(`\n🎒 库存物品: ${items.length} 个`);
    if (items.length > 0) {
      const firstItem = items[0];
      console.log(`  首件物品: ${firstItem.displayName} x${firstItem.count}`);
    }
    logTest('库存查询', true, `${items.length} 个物品`);
  } catch (e) {
    logTest('库存查询', false, e.message);
  }

  // 测试 6: 实体查询
  try {
    const entities = Object.values(bot.entities);
    console.log(`\n👥 附近实体: ${entities.length} 个`);
    for (const entity of entities.slice(0, 3)) {
      console.log(`  - ${entity.displayName || entity.name || entity.type} (id=${entity.id})`);
    }
    logTest('实体查询', true, `${entities.length} 个实体`);
  } catch (e) {
    logTest('实体查询', false, e.message);
  }

  // 测试 7: 路径规划器可用性
  try {
    const Pathfinder = require('mineflayer-pathfinder').pathfinder;
    const movements = new pathfinder.Movements(bot, mcData);
    console.log(`\n🗺️ 路径规划器: 可用`);
    logTest('路径规划器', true, 'Movements 已创建');
  } catch (e) {
    logTest('路径规划器', false, e.message);
  }

  // 测试 8: HawkEye 自动攻击
  try {
    const hawkEye = minecraftHawkEye;
    console.log(`\n🏹 HawkEye: ${hawkEye ? '可用' : '不可用'}`);
    logTest('HawkEye', hawkEye !== undefined, 'minecrafthawkeye 插件已加载');
  } catch (e) {
    logTest('HawkEye', false, e.message);
  }

  // ============================================
  // 输出测试结果汇总
  // ============================================
  console.log(`\n${'='.repeat(50)}`);
  console.log('测试结果汇总');
  console.log('='.repeat(50));
  for (const r of testResults) {
    const status = r.passed ? '✅' : '❌';
    console.log(`  ${status} ${r.name}${r.detail ? ': ' + r.detail : ''}`);
  }
  console.log(`\n通过: ${testsPassed}/${testResults.length}`);
  console.log(`失败: ${testsFailed}/${testResults.length}`);

  if (testsFailed === 0) {
    console.log('\n🎉 所有测试通过！Mineflayer 1.21.1 桥接正常。');
  } else {
    console.log(`\n⚠️ ${testsFailed} 个测试失败，需要调查。`);
  }

  // 等待一段时间后断开
  console.log('\n测试完成，5秒后断开连接...');
  await sleep(5000);
  bot.quit();
  process.exit(testsFailed === 0 ? 0 : 1);
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

// 设置超时
setTimeout(() => {
  console.error('\n❌ 测试超时 (30秒)');
  if (bot.entity) {
    bot.quit();
  }
  process.exit(1);
}, 30000);
